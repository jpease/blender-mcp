import functools
import logging
import os
import queue
import tempfile
import threading
import time

from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass

import bpy

from . import ADDON_PROTOCOL_VERSION, authored, bl_info
from .capability_introspection import capability_params
from .command_registry import CommandRegistryMixin, target_names
from .file_paths import canonical_path
from .handlers.animation import AnimationHandlersMixin
from .handlers.camera import CameraHandlersMixin
from .handlers.character_rigging import CharacterRiggingHandlersMixin
from .handlers.cloth import ClothHandlersMixin
from .handlers.delivery import DeliveryHandlersMixin
from .handlers.file_lifecycle import FileLifecycleHandlersMixin
from .handlers.lighting import LightingHandlers
from .handlers.linking import LinkingHandlersMixin
from .handlers.liquid import LiquidHandlersMixin
from .handlers.mesh import MeshHandlersMixin
from .handlers.model import ModelHandlersMixin
from .handlers.nd import NDHandlersMixin
from .handlers.object_animation import ObjectAnimationHandlersMixin
from .handlers.polyhaven import PolyhavenHandlersMixin
from .handlers.render_jobs import RenderJobHandlersMixin
from .handlers.rendering import RenderingHandlersMixin
from .handlers.retopology import RetopologyHandlersMixin
from .handlers.rigid_body import RigidBodyHandlersMixin
from .handlers.scene import SceneHandlersMixin
from .handlers.scene_inspection import SceneInspectionHandlersMixin
from .handlers.scene_physics import ScenePhysicsHandlersMixin
from .handlers.sketchfab import SketchfabHandlersMixin
from .handlers.viewport import ViewportHandlersMixin
from .helpers import get_blendermcp_addon_preferences
from .object_lookup import find_object
from .output_roots import configured_file_roots, configured_roots, writable_roots
from .render_devices import cycles_device_report
from .session import load_in_flight, mark_session_indeterminate, session_is_indeterminate, session_snapshot
from .socket_transport import SocketTransportMixin
from .transaction import mutation_transaction, unreferenced_warning

# The add-on's own logger. Blender installs no handler for it, so by default these
# records reach the console through the root logger exactly as the `print` calls they
# replaced did - but a level now separates one dispatched command from a dropped
# connection, and an operator can silence or raise either without editing the add-on.
logger = logging.getLogger(__name__)


@functools.lru_cache(maxsize=1)
def _canonical_file_roots(roots: tuple[str, ...]) -> tuple[str, ...]:
    """
    Canonicalize the configured file roots, once per distinct configuration.

    Memoized because `realpath` stats every path component, on Blender's main
    thread, during each handshake. A root that does not exist is kept: dropping
    it would turn a misconfigured enforced deployment into an unenforced one.

    Args:
        roots: The configured roots, in order; a tuple so it can key the cache.

    Returns:
        tuple[str, ...]: Canonical roots, deduplicated, order kept.

    """
    return tuple(dict.fromkeys(canonical_path(root) for root in roots))


@functools.lru_cache(maxsize=1)
def _probe_writable_roots(candidates: tuple[str | None, ...]) -> tuple[str, ...]:
    """
    Probe candidate roots for writability, once per distinct candidate list.

    Every MCP connection's handshake reaches this on Blender's main thread, and
    a stat on a hung network mount blocks there, freezing the UI and the command
    queue. The drain time budget cannot help: it is checked only between
    commands. Keying on the list, rather than caching outright, re-probes when
    the open .blend changes.

    Args:
        candidates: Paths to consider, most preferred first; a tuple so it can
            key the cache.

    Returns:
        tuple[str, ...]: Absolute writable directories, preference order kept.
        Immutable so a caller cannot change the cached answer.

    """
    return tuple(writable_roots(candidates))


class HandlerReportedError(Exception):
    """
    A handler reported failure by *returning* a failure shape instead of
    raising (e.g. {"error": ...} from a provider import).

    Raised inside the mutation transaction so a partially-applied request rolls
    back like any other failure, instead of committing the partial state and
    pushing an undo checkpoint. Its message is what the client receives.
    """


def _handler_failure_message(result):
    """
    Detect a handler that returned a failure shape rather than raising.

    Deliberately mirrors connection.ad_hoc_failure_message on the server side;
    the two live in separate runtimes (the addon must not import server code,
    and vice versa), so the shared shape contract is duplicated on purpose -
    keep the two in sync. A {"cancelled": True} outcome (ND operators the user
    cancelled) is NOT a failure and must fall through.

    Args:
        result: The value a handler returned.

    Returns:
        str | None: The failure message if `result` is a known failure shape,
        else None.

    """
    if isinstance(result, dict):
        if result.get("cancelled"):
            return None
        if result.get("succeed") is False:
            return str(result.get("error") or result)
        error = result.get("error")
        if error:
            return str(error)
    return None


@dataclass(slots=True)
class AnswerReceipt:
    """
    Who owes one command's client its single response frame.

    `_execute_and_answer` and `_answer` are the only writers and `_run_session_swap`
    the only reader, but the question outlives the call that answers it: on an abort
    the swap has to know whether anything already began writing, because a second
    frame desyncs a client that matches responses by order and no frame at all costs
    it a timeout.

    Attributes:
        answered: True once `_answer` began writing, whether or not the write
            finished.

    """

    answered: bool = False


class BlenderMCPServer(
    SocketTransportMixin,
    CommandRegistryMixin,
    SceneInspectionHandlersMixin,
    ViewportHandlersMixin,
    AnimationHandlersMixin,
    CameraHandlersMixin,
    LightingHandlers,
    CharacterRiggingHandlersMixin,
    RigidBodyHandlersMixin,
    RetopologyHandlersMixin,
    MeshHandlersMixin,
    ModelHandlersMixin,
    ClothHandlersMixin,
    LiquidHandlersMixin,
    FileLifecycleHandlersMixin,
    DeliveryHandlersMixin,
    LinkingHandlersMixin,
    SceneHandlersMixin,
    ScenePhysicsHandlersMixin,
    ObjectAnimationHandlersMixin,
    RenderingHandlersMixin,
    RenderJobHandlersMixin,
    NDHandlersMixin,
    PolyhavenHandlersMixin,
    SketchfabHandlersMixin,
):
    """Serve MCP commands from clients through the Blender addon."""

    def __init__(self, host="localhost", port=9876) -> None:
        self.host = host
        self.port = port
        self.running = False
        self.socket = None
        self.server_thread = None
        # Commands are pushed here by client threads and drained by a single
        # timer running on Blender's main thread. bpy.app.timers is not
        # thread-safe, so registering a timer per command (the previous
        # approach) could silently drop the callback - on Windows especially -
        # leaving the client blocked in recv() until its socket timeout.
        self.command_queue = queue.Queue(maxsize=self._MAX_QUEUED_COMMANDS)
        # Client socket -> its write lock. stop() shuts the sockets down to free
        # threads blocked in recv(); the lock keeps two writers' frames apart.
        self._clients = {}
        self._clients_lock = threading.Lock()
        # The exact object registered with bpy.app.timers; see _register_drain_timer.
        self._drain_timer = None
        # When the last command was taken off the queue, for _poll_interval below.
        self._last_command_at = 0.0
        # Bound handler maps, one per distinct provider-flag combination; see
        # `_build_command_handlers`. Per instance, because the values are this
        # server's bound methods.
        self._handlers_by_gate: dict[tuple[bool, ...], Mapping[str, Callable[..., object]]] = {}

    def _get_config_value(self, scene_attr, pref_attr=None, env_var=None):
        """
        Read config in order: addon preferences -> scene -> env var.

        Args:
            scene_attr: Value for scene attr.
            pref_attr: Value for pref attr.
            env_var: Value for env var.

        Returns:
            Result produced by the operation.

        """
        prefs = get_blendermcp_addon_preferences()
        if prefs and pref_attr:
            pref_value = getattr(prefs, pref_attr, "")
            if pref_value:
                return pref_value

        scene_value = getattr(bpy.context.scene, scene_attr, "")
        if scene_value:
            return scene_value

        if env_var:
            env_value = os.getenv(env_var, "")
            if env_value:
                return env_value
        return ""

    def get_sketchfab_api_key(self):
        return self._get_config_value(
            "blendermcp_sketchfab_api_key",
            "sketchfab_api_key",
            "BLENDERMCP_SKETCHFAB_API_KEY",
        )

    def _register_drain_timer(self) -> None:
        """
        Register the drain callback, keeping the exact object Blender was given.

        `bpy.app.timers` matches callbacks by identity, and each access to
        `self.drain_command_queue` creates a new bound method, which
        `is_registered()` and `unregister()` do not recognize.

        Main thread only: `bpy.app.timers` is not thread-safe, and a registration
        from another thread can be silently dropped.
        """
        if self._drain_timer is not None and bpy.app.timers.is_registered(self._drain_timer):
            return
        self._drain_timer = self.drain_command_queue
        bpy.app.timers.register(self._drain_timer, persistent=True)

    def _unregister_drain_timer(self) -> None:
        """
        Remove the drain callback, so restarts cannot pile up timers.

        Each extra timer brings its own per-tick command and time allowance,
        multiplying the main-thread load those limits exist to cap. Blender may
        already have dropped the timer, which is not an error.
        """
        timer = self._drain_timer
        self._drain_timer = None
        if timer is None:
            return
        with suppress(Exception):
            if bpy.app.timers.is_registered(timer):
                bpy.app.timers.unregister(timer)

    def drain_command_queue(self) -> float | None:
        """
        Run queued commands on Blender's main thread.

        Registered once by start(); returns the poll interval so Blender keeps
        calling it. All bpy access happens here, on the main thread.

        Two checks keep a command from running against a file it was not sent
        for, and each covers the other's gap. The queue snapshot in
        `_run_session_swap` rejects what was queued before a swap, but not what
        arrives during the load. The stamp from `_stamp_session` rejects those,
        but not commands queued before a failed swap, because a failed load
        leaves the epoch alone. The stamp is checked first, so a stale swap
        command cannot trip the barrier again.

        Returns:
            Result produced by the operation.

        """
        if not self.running:
            return None

        try:
            self._drain_batch()
        except BaseException:
            # Blender unregisters a timer callback that raises, even a persistent
            # one, but an abort must still propagate. Without a successor timer
            # the drain loop ends and every client waits out its 180 s timeout.
            self._replace_this_dying_timer()
            raise
        return self._poll_interval()

    def _poll_interval(self) -> float:
        """
        Say how long Blender should wait before draining the queue again.

        `bpy.app.timers` is the only thread-safe way to reach Blender's main thread from a
        socket thread, so this interval is the latency floor under every command: a client
        that sends its next command immediately after reading a reply always misses the
        tick that just ran and waits out a whole interval. At a flat 50 ms that measured as
        54.86 ms of pure protocol cost per frame on a 240-frame orchestrated animation -
        13.2 s a shot, for renders whose frames took 2.5 ms each
        (`scripts/rig_scenarios/scenario_render_overhead.py`).

        So the poll follows the traffic: fast while a session is active, and back to the
        cheap idle rate once it goes quiet, which is what keeps an idle Blender idle. The
        window is generous because the gap between two commands of one sequence includes
        whatever Blender spent on the first - a slow frame must not drop the session back
        to the idle rate before its successor arrives.

        Returns:
            float: Seconds until the next drain.

        """
        if time.monotonic() - self._last_command_at < self._ACTIVE_WINDOW_SECONDS:
            return self._ACTIVE_POLL_SECONDS
        return self._IDLE_POLL_SECONDS

    def _drain_batch(self) -> None:
        """
        Run up to one tick's worth of queued commands, applying both barriers.

        A separate method so the recovery in `drain_command_queue` covers all of
        it, including the first `get_nowait`. A command whose client has
        disconnected is dropped unrun: nobody can receive its answer, and a
        client that timed out while an earlier render held the main thread
        would otherwise see its abandoned renders run later, in order.
        """
        processed = 0
        deadline = time.monotonic() + self._DRAIN_TIME_BUDGET_SECONDS
        while processed < self._MAX_COMMANDS_PER_TICK and time.monotonic() < deadline:
            try:
                command, client = self.command_queue.get_nowait()
            except queue.Empty:
                break

            if self._client_departed(client):
                logger.warning(
                    "Discarding orphaned command type=%s id=%s: its client disconnected before it ran",
                    command.get("type"),
                    command.get("id"),
                )
                processed += 1
                continue

            # Popped so handlers never see it.
            stamp = command.pop(self._SESSION_STAMP_KEY, None)
            refusal = self._refusal_reason(command, stamp)
            if refusal is not None:
                self._discard_superseded([(command, client)], refusal)
                processed += 1
                continue

            # Only a swap the addon can run may discard the queue, which holds
            # other server processes' commands; any other name falls through and
            # is answered "Unknown command type". The command is already off the
            # queue, so a failure to classify it must be answered here.
            try:
                is_swap = self.command_spec(command.get("type")).session_swap and self._is_dispatchable(
                    command.get("type")
                )
            except Exception:
                logger.exception("Could not classify a dequeued command; answering its client instead")
                self._answer(command, client, {"status": "error", "message": "Command could not be dispatched"})
                processed += 1
                continue

            if is_swap:
                self._run_session_swap(command, client)
                break

            self._execute_and_answer(command, client)
            processed += 1
            if self.command_spec(command.get("type")).tick_ending:
                break
        if processed:
            self._last_command_at = time.monotonic()

    def _replace_this_dying_timer(self) -> None:
        """
        Hand the drain loop to a fresh callback before Blender drops this one.

        Registering is safe here because timer callbacks run on the main thread.
        Clearing `_drain_timer` stops `_register_drain_timer` returning early, and
        the bound method it registers is a new object, distinct from the dying
        one. A stopped server gets no successor, so this cannot undo `stop()`.

        The dying timer is unregistered explicitly, so exactly one drain timer
        remains even where raising callbacks are not dropped, as in the test
        stub. A failed handoff is logged, because clients would otherwise time
        out with nothing in the console to say why.
        """
        if not self.running:
            return
        dying = self._drain_timer
        self._drain_timer = None
        if dying is not None:
            with suppress(Exception):
                bpy.app.timers.unregister(dying)
        try:
            self._register_drain_timer()
        except Exception:
            logger.exception("Could not hand the drain loop to a fresh timer - restart the MCP server")

    @staticmethod
    def _session_marker() -> tuple[object, object]:
        """
        Read the pair that identifies the open database.

        The epoch restarts at 0 when the addon's modules reload, so a client can
        see the same epoch for a different database. `session_id` is new on
        every reload, which makes the pair unique.

        Returns:
            tuple: `(session_id, session_epoch)`, both immutable.

        """
        snapshot = session_snapshot()
        return (snapshot["session_id"], snapshot["session_epoch"])

    def _stamp_session(self, command: dict) -> None:
        """
        Record, on the client thread, which database a command was queued against.

        Nothing in the queue shows whether a command arrived before, during or
        after a load, so the marker is taken at enqueue and compared at dequeue.
        The pre-swap queue snapshot cannot do this: the epoch moves only when a
        load ends, and client threads keep queuing throughout it.

        Safe off the main thread because it reads module state, not `bpy`.

        Args:
            command: The decoded command, mutated in place. The key is
                overwritten, so a client cannot forge it.

        """
        command[self._SESSION_STAMP_KEY] = self._session_marker()

    def _execute_and_answer(
        self,
        command: dict,
        client: object,
        *,
        receipt: "AnswerReceipt | None" = None,
    ) -> None:
        """
        Run one command and write exactly one response frame back to its client.

        Catches `BaseException` because an abort such as `KeyboardInterrupt`
        lands wherever the main thread is, including inside a long file load.
        The client is answered first and the abort re-raised, so Blender still
        sees it.

        Args:
            command: The decoded command to run.
            client: The socket its response belongs on.
            receipt: Optional receipt `_answer` marks as it starts, so
                `_run_session_swap` can tell whether it still owes the client an
                answer.

        """
        response: dict
        try:
            response = self.execute_command(command)
        except Exception as e:
            logger.exception("Command %s failed before it produced a response", command.get("type"))
            response = {"status": "error", "message": str(e)}
        except BaseException as e:
            logger.error("Command aborted by %s: answering the client before re-raising", type(e).__name__)
            self._answer(
                command,
                client,
                {
                    "status": "error",
                    "message": (
                        f"Blender aborted this command ({type(e).__name__}) before it produced a result; "
                        "the session may be in an indeterminate state - poll get_session_info before resending."
                    ),
                },
                receipt=receipt,
            )
            raise

        self._answer(command, client, response, receipt=receipt)

    def _answer(self, command: dict, client: object, response: dict, *, receipt: "AnswerReceipt | None" = None) -> None:
        """
        Write one response frame, whatever the handler did or did not produce.

        Every response carries the session marker, so the MCP server notices a
        file opened from Blender's own UI and re-reads its cached handshake. The
        fallback error frames from `_encode_response` carry no marker.

        The receipt is written before anything that can fail, so it records an
        answer begun, not finished. An abort mid-send can then leave the client
        with no frame, which costs a timeout; recording later could send two,
        which desyncs a client that matches responses by order.

        Args:
            command: The command being answered, for its echoed id.
            client: The socket the frame belongs on.
            response: The response body.
            receipt: Optional receipt to record the answer in; see above.

        """
        if receipt is not None:
            receipt.answered = True

        # Echo the id so the client can match responses without relying on order.
        response["id"] = command.get("id")
        response["session_id"], response["session_epoch"] = self._session_marker()

        # Outside the try, so an encoding failure is never reported as a disconnect.
        payload = self._encode_response(response, command.get("id"))
        try:
            self._send_frame(client, payload)
        except Exception:
            logger.warning("Failed to send a response frame - the client disconnected")

    def _drain_queue_into(self, superseded: list[tuple[dict, object]]) -> None:
        """
        Empty the command queue into a list the caller already owns.

        Appending, rather than returning a list, means an abort part-way through
        loses nothing: the caller still answers every command already taken.
        The loop has its own bound because client threads can refill the queue
        as fast as it empties.

        Args:
            superseded: The list to append `(command, client)` pairs to.

        """
        for _slot in range(self._MAX_QUEUED_COMMANDS):
            try:
                superseded.append(self.command_queue.get_nowait())
            except queue.Empty:
                break

    def _run_session_swap(self, command: dict, client: object) -> None:
        """
        Run a command that replaces the database, and discard the batch behind it.

        The queue is emptied before the swap runs, so everything discarded was
        queued against the old file. Rejections go out in the `finally`, after
        the swap, so they name the epoch that holds once the outcome is known. A
        swap its own validation refuses still discards the queue; fixing that
        means moving validation out of the handler.

        On an abort, only `load_in_flight()` decides whether the database may
        now mix two files. The `try` also spans the queue drain, and inferring a
        load from what did not happen can latch falsely or miss a real abort. If
        a load was in flight, the session is marked indeterminate, moving the
        epoch before the `finally` sends rejections: no load handler fires on an
        abort, so commands stamped during the load would otherwise still match.
        The swap's own client is answered here only if the receipt shows nobody
        began answering it.

        Args:
            command: The session-swap command to run.
            client: The socket its response belongs on.

        """
        superseded: list[tuple[dict, object]] = []
        receipt = AnswerReceipt()

        try:
            self._drain_queue_into(superseded)
            self._execute_and_answer(command, client, receipt=receipt)
        except BaseException:
            # Both `if`s can apply at once, so neither may become an `elif`. Read
            # the flag before the latch clears it; the message depends on it.
            mid_load = load_in_flight()
            if mid_load:
                mark_session_indeterminate()
            if not receipt.answered:
                self._answer(command, client, {"status": "error", "message": self._abort_message(mid_load)})
            raise
        finally:
            self._discard_superseded(superseded)

    # Sent to the swap's own client when nothing began answering it. It says the
    # database is unchanged, so it is used only when no load is in flight; an
    # unwritten receipt alone does not prove that. It names no path, because
    # this socket is unauthenticated.
    _ABORTED_BEFORE_HANDOFF = (
        "Blender aborted before this file swap was dispatched; nothing was loaded and the open "
        "database is unchanged. Poll get_session_info, then resend."
    )
    # The same case with a load in flight. It asks for a known shot, not a
    # resend, because the database may now mix two files.
    _ABORTED_MID_LOAD = (
        "Blender aborted this file swap while a load was in flight; the open database may be part of "
        "two files and this session is now marked indeterminate. Poll get_session_info and open a "
        "known shot before resending anything."
    )
    # Why a dequeued command was answered without running. An unstamped command
    # gets its own reason because no swap was involved.
    _SUPERSEDED_REASON = (
        "a session file swap was attempted while this command was queued, "
        "so it was never run against the file it was sent for"
    )
    _UNSTAMPED_REASON = (
        "it reached the queue without the session stamp the enqueue path applies, "
        "so which database it was sent for cannot be established"
    )
    # Unlike the reasons above, this lasts until a load completes, so a resend
    # would be refused again; the client must open a known shot instead.
    _INDETERMINATE_REASON = (
        "a session file swap was aborted part-way and no load has completed since, "
        "so the open database may be part of two files; poll get_session_info and "
        "open a known shot before resending anything"
    )

    def _abort_message(self, mid_load: bool) -> str:
        """
        Pick the abort message by whether a load was in flight.

        Args:
            mid_load: Whether a load was in flight when the abort was observed.

        Returns:
            str: The message for that case.

        """
        return self._ABORTED_MID_LOAD if mid_load else self._ABORTED_BEFORE_HANDOFF

    def _refusal_reason(self, command: Mapping[str, object], stamp: object) -> str | None:
        """
        Say why a dequeued command must be answered instead of run, or that it may run.

        The stamp is checked first, so a command queued before a failed swap is
        reported as stale rather than tripping the indeterminate barrier again. A
        missing stamp is its own refusal: the only enqueue path always stamps, so
        only a bug gets here. The indeterminate check comes after, so a stale
        command is still reported as stale, and before dispatch, so nothing runs
        on a database that may mix two files.

        Mutates nothing: it reads the command, the stamp and the session module.

        Args:
            command: The dequeued command, its stamp already popped off.
            stamp: The session marker popped off it, or None when it carried none.

        Returns:
            str | None: The clause `_discard_superseded` folds into its message,
            or None when the command may run.

        """
        if stamp != self._session_marker():
            return self._UNSTAMPED_REASON if stamp is None else self._SUPERSEDED_REASON
        if session_is_indeterminate() and not self.command_spec(command.get("type")).indeterminate_safe:
            return self._INDETERMINATE_REASON
        return None

    def _discard_superseded(self, superseded: list[tuple[dict, object]], reason: str | None = None) -> None:
        """
        Answer every command a swap invalidated, under a deadline, so no client waits.

        The message gives the current epoch without claiming it moved. After a
        successful swap it differs from the client's cached epoch, so the client
        re-handshakes; after a failed swap it does not, and the client can simply
        resend. It names no path, because this layer must not hand a client an
        absolute path.

        Every peer gets a send attempt; once the deadline passes, each attempt
        just waits less. Skipping peers instead would drop healthy connections,
        and this frame is their only prompt to re-handshake. A peer is closed
        after its first failed write, because a timed-out `sendall` may have
        written part of a frame and another write would break the newline
        framing. Closing turns the partial line into an EOF the client can act on.

        Worst case on the main thread is about 1.26 s: the 0.25 s budget, one
        send still in flight (lock and write, 0.25 s each), then 2 ms for each of
        up to `_MAX_QUEUED_COMMANDS` entries past the deadline.

        Args:
            superseded: `(command, client)` pairs taken off the queue, bounded
                by `_drain_queue_into`.
            reason: Why these commands were not run; defaults to the swap case.

        """
        snapshot = session_snapshot()
        epoch = snapshot["session_epoch"]
        message = (
            f"Discarded without running: {reason or self._SUPERSEDED_REASON}. "
            f"The session epoch is now {epoch} - "
            "re-handshake first if that differs from the epoch you last saw, then resend."
        )
        deadline = time.monotonic() + self._REJECTION_TIME_BUDGET_SECONDS
        abandoned: set[object] = set()
        for command, client in superseded:
            # Closed earlier in this pass, so a write could only fail.
            if client in abandoned:
                continue
            frame = self._encode_frame(
                {
                    "id": command.get("id"),
                    "status": "error",
                    "message": message,
                    "session_id": snapshot["session_id"],
                    "session_epoch": epoch,
                }
            )
            timeout = (
                self._REJECTION_SEND_TIMEOUT_SECONDS
                if time.monotonic() < deadline
                else self._PAST_BUDGET_SEND_TIMEOUT_SECONDS
            )
            if not self._send_bounded(client, frame, timeout):
                self._abandon_unreachable_client(client)
                abandoned.add(client)

    _MAX_QUEUED_COMMANDS = 256
    _MAX_COMMANDS_PER_TICK = 8
    _DRAIN_TIME_BUDGET_SECONDS = 0.02

    # The drain timer's two rates; `_poll_interval` chooses between them. 5 ms is the
    # floor a command pays while a session is active, down from the flat 50 ms this timer
    # used to return, and the idle rate is that old value - an unused Blender polls an
    # empty queue twenty times a second, exactly as before.
    _ACTIVE_POLL_SECONDS = 0.005
    _IDLE_POLL_SECONDS = 0.05
    _ACTIVE_WINDOW_SECONDS = 5.0

    # Main-thread bounds for the rejection path; `_discard_superseded` adds them
    # up. The send timeout only has to catch a peer that has stopped reading: a
    # slow one, such as a client still draining a large response, must not be
    # closed and lose its queued commands.
    _REJECTION_SEND_TIMEOUT_SECONDS = 0.25
    _REJECTION_TIME_BUDGET_SECONDS = 0.25
    # Past the budget a frame is still attempted, with this brief timeout. It
    # must stay above zero: `settimeout(0)` makes the socket non-blocking, and
    # the `recv()` running on it in `handle_client` would raise `BlockingIOError`
    # and spin instead of timing out.
    _PAST_BUDGET_SEND_TIMEOUT_SECONDS = 0.001

    # Where `_stamp_session` puts the marker. Kept on the command dict so queue
    # items stay `(command, client)` pairs; popped before dispatch.
    _SESSION_STAMP_KEY = "_blendermcp_session_marker"

    def execute_command(self, command):
        """
        Execute a command in the main Blender thread.

        Args:
            command: Command requested by the client.

        Returns:
            Result produced by the operation.

        """
        try:
            return self.execute_command_internal(command)
        except Exception as e:
            logger.exception("Command %s failed outside its handler", command.get("type"))
            return {"status": "error", "message": str(e)}

    @staticmethod
    def ping() -> dict[str, bool]:
        """
        Answer a liveness check without reading anything out of Blender.

        Deliberately free of `bpy`, and a staticmethod so it stays that way: a
        successful ping beside a failing command tells the client the transport
        and the command queue are healthy and the fault is in the data access,
        which is the only reason this command exists.

        Returns:
            dict[str, bool]: `{"pong": True}`.

        """
        return {"pong": True}

    def execute_command_internal(self, command):
        """
        Internal command execution with proper context.

        Args:
            command: Command requested by the client.

        Returns:
            Result produced by the operation.

        """
        cmd_type = command.get("type")
        params = command.get("params", {})

        handler = self._build_command_handlers().get(cmd_type)
        if handler is None:
            return {"status": "error", "message": f"Unknown command type: {cmd_type}"}
        try:
            logger.debug("Dispatching %s", cmd_type)
            return {"status": "success", "result": self._run_handler(cmd_type, handler, params)}
        except ValueError as refusal:
            # A refusal, not a fault: the handler layer raises ValueError when the client's
            # own arguments are rejected - an unconfirmed destructive call, a path outside
            # the file roots, a name that does not resolve. Printing a traceback for each one
            # teaches whoever watches Blender's console to skim past tracebacks, which is
            # where an actual fault will be. The client is told exactly the same either way.
            logger.info("Command %s refused: %s", cmd_type, refusal)
            return {"status": "error", "message": str(refusal)}
        except Exception as error:
            logger.exception("Command %s failed in its handler", cmd_type)
            return {"status": "error", "message": str(error)}

    @staticmethod
    def _resolve_targets(params):
        """
        Resolve the existing objects a mutating request will touch, from its params.

        `target_names` decides *which* names a request claims; this decides which
        of them exist. Missing objects are skipped (the handler will raise its own
        clear error). Names resolve through `find_object`, as in the handlers, so a
        rollback restores the objects the handler actually changed. An ambiguous
        name is skipped like a missing one; the handler's own lookup refuses it.

        Args:
            params: The command's params dict.

        Returns:
            list: Existing bpy objects named by the target params, first mention first.

        """
        objects = []
        for name in target_names(params):
            try:
                obj = find_object(bpy.data.objects, name)
            except ValueError:
                continue
            if obj is not None:
                objects.append(obj)
        return objects

    def _run_handler(self, cmd_type, handler, params):
        """
        Call a resolved handler, wrapping mutating commands in mutation_transaction.

        Args:
            cmd_type: The MCP command type, used to pick read-only vs. mutating dispatch.
            handler: The bound handler method to call.
            params: Keyword arguments to call the handler with.

        Returns:
            Result produced by the handler.

        """
        if self.bypasses_transaction(cmd_type, params):
            return handler(**params)

        targets = self._resolve_targets(params)
        with mutation_transaction(cmd_type, targets, self.command_spec(cmd_type).geometry) as txn:
            return self._finish_transaction(txn, handler(**params))

    @staticmethod
    def _finish_transaction(txn, result):
        """
        Settle a transaction the handler has already run inside, by what it returned.

        A handler that reports failure by *returning* a failure shape rather than
        raising is converted to a HandlerReportedError here, still inside the
        transaction, so its partial mutation rolls back instead of being committed
        and pushing an undo checkpoint. A cancelled outcome is neither: nothing
        happened, so nothing is checkpointed.

        Args:
            txn: The open transaction wrapping this command.
            result: What the handler returned.

        Returns:
            The handler's result, with any undo-unavailability and unreferenced-
            datablock warnings merged in.

        Raises:
            HandlerReportedError: When `result` is a failure shape.

        """
        if isinstance(result, dict) and result.get("cancelled"):
            txn.finish_without_checkpoint()
            return result
        failure = _handler_failure_message(result)
        if failure is not None:
            raise HandlerReportedError(failure)
        unreferenced = unreferenced_warning(txn.unreferenced_created())
        authored.record(txn.created_datablocks())
        notices = [note for note in (txn.commit(), unreferenced) if note]
        if notices and isinstance(result, dict):
            return {**result, "warnings": [*result.get("warnings", []), *notices]}
        return result

    def get_addon_info(self):
        """
        Version/capability handshake for the MCP server (and install tooling).

        `capabilities` depends on the open .blend's `blendermcp_use_*` flags, so
        a client re-handshakes when `session_epoch` moves; a save or a failed
        load leaves it alone. `session_indeterminate` tells a client why most
        of its commands are being refused. `capability_params` reports each
        command's accepted keyword names, or "*" for one that takes **kwargs,
        for the server's preflight parameter gate.

        Returns:
            Result produced by the operation.

        """
        session = session_snapshot()
        handlers = self._build_command_handlers()
        return {
            "name": bl_info.get("name", "Blender MCP"),
            "addon_version": list(bl_info.get("version", (0, 0))),
            "protocol_version": ADDON_PROTOCOL_VERSION,
            "capabilities": sorted(handlers),
            "capability_params": capability_params(handlers),
            "blender_version": bpy.app.version_string,
            "writable_output_roots": self._writable_output_roots(),
            # Like the roots, a machine fact the MCP server cannot observe: what Cycles renders on.
            "render_devices": cycles_device_report(),
            # Enforced containment, unlike the advisory roots above.
            **self._file_path_policy(),
            # The pair, because the epoch restarts at 0 when the addon reloads.
            "session_id": session["session_id"],
            "session_epoch": session["session_epoch"],
            "current_filepath": session["current_filepath"],
            # Also here, so a re-handshaking client learns why it is refused.
            "session_indeterminate": session["session_indeterminate"],
        }

    @staticmethod
    def _writable_output_roots() -> list[str]:
        """
        List directories this Blender process can write renders and exports to.

        Reported because the MCP server may not share this filesystem. The list
        only ranks preferences; `_file_path_policy` publishes the enforced roots,
        which never come from these defaults because the defaults reach `~`.
        These paths are `abspath`'d, not `realpath`'d, so a symlinked root is
        listed under a different name than the one enforced. With no .blend open
        and no roots configured, `bpy.app.tempdir` ranks first, and Blender
        deletes it on exit.

        Returns:
            list[str]: Absolute, writable directories, most preferred first.
            A fresh list each call, so a caller cannot change the memoized answer.

        """
        blend_file = bpy.data.filepath
        candidates = (
            *configured_roots(),
            os.path.dirname(blend_file) if blend_file else None,
            getattr(bpy.app, "tempdir", None),
            tempfile.gettempdir(),
            os.path.expanduser("~"),
        )
        return list(_probe_writable_roots(candidates))

    @staticmethod
    def _file_path_policy() -> dict[str, object]:
        """
        Publish the roots `.blend` file commands are confined to, and whether any are.

        `file_roots_enforced` makes the unenforced default visible to clients.
        Roots are published canonical because containment compares canonical
        paths.

        Returns:
            dict[str, object]: `file_roots` (list[str]) and `file_roots_enforced` (bool).

        """
        roots = list(_canonical_file_roots(tuple(configured_file_roots())))
        return {"file_roots": roots, "file_roots_enforced": bool(roots)}
