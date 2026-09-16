import functools
import itertools
import json
import os
import queue
import socket
import tempfile
import threading
import time
import traceback

from contextlib import suppress

import bpy
import mathutils

from . import ADDON_PROTOCOL_VERSION, bl_info
from .handlers.animation import AnimationHandlersMixin
from .handlers.camera import CameraHandlersMixin
from .handlers.character_rigging import CharacterRiggingHandlersMixin
from .handlers.cloth import ClothHandlersMixin
from .handlers.file_lifecycle import FileLifecycleHandlersMixin
from .handlers.lighting import LightingHandlers
from .handlers.liquid import LiquidHandlersMixin
from .handlers.mesh import MeshHandlersMixin
from .handlers.model import ModelHandlersMixin
from .handlers.nd import NDHandlersMixin
from .handlers.object_animation import ObjectAnimationHandlersMixin
from .handlers.polyhaven import PolyhavenHandlersMixin
from .handlers.rendering import RenderingHandlersMixin
from .handlers.retopology import RetopologyHandlersMixin
from .handlers.rigid_body import RigidBodyHandlersMixin
from .handlers.scene import SceneHandlersMixin
from .handlers.scene_physics import ScenePhysicsHandlersMixin
from .handlers.sketchfab import SketchfabHandlersMixin
from .handlers.viewport import ViewportHandlersMixin
from .helpers import get_blendermcp_addon_preferences, get_mesh_object, paginate, sync_from_editmode
from .output_roots import configured_roots, writable_roots
from .session import load_in_flight, mark_session_indeterminate, session_is_indeterminate, session_snapshot
from .transaction import mutation_transaction


@functools.lru_cache(maxsize=1)
def _probe_writable_roots(candidates: tuple[str | None, ...]) -> tuple[str, ...]:
    """
    Probe a candidate root list for writability, once per distinct list.

    `writable_roots` runs an `os.path.isdir` plus an `os.access` per candidate,
    and it is reached from the `get_addon_info` handshake - which every MCP
    connection performs, on Blender's main thread, inside
    `drain_command_queue`. On a hung network mount (the operator-supplied
    `BLENDERMCP_OUTPUT_ROOTS` entries come first, and those are exactly the
    ones likely to be mounts) those stat calls block uninterruptibly, freezing
    the UI and every queued command. `_DRAIN_TIME_BUDGET_SECONDS` cannot bound
    that, because the budget is only checked *between* commands.

    Keyed on the candidate list rather than cached outright: building that list
    is pure string work, while `bpy.data.filepath` changes whenever the user
    opens or saves a .blend. Keying re-probes exactly when the answer could
    have changed and never alters which roots are advertised or their order.
    A single entry is enough - the candidate list is near-static - and keeps
    the cache bounded.

    Args:
        candidates: Paths to consider, most preferred first. A tuple because an
            lru_cache key must be hashable.

    Returns:
        tuple[str, ...]: Absolute writable directories, preference order kept.
        Immutable so a caller cannot mutate the cached answer in place.

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


class BlenderMCPServer(
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
    SceneHandlersMixin,
    ScenePhysicsHandlersMixin,
    ObjectAnimationHandlersMixin,
    RenderingHandlersMixin,
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
        # Live client sockets mapped to the lock that serializes writes to each
        # one, so stop() can unblock threads parked in recv() and two writers
        # cannot splice their frames together. See _send_frame.
        self._clients = {}
        self._clients_lock = threading.Lock()
        # The one bound-method object registered with bpy.app.timers, held for
        # the timer's whole lifetime - see _register_drain_timer.
        self._drain_timer = None

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

    def start(self) -> None:
        if bpy.app.background:
            print(
                "BlenderMCP: cannot start server in background mode (blender -b) - commands would never execute\n"
                "BlenderMCP: run Blender with a GUI, or use a virtual display: xvfb-run -a blender"
            )
            return

        if self.running:
            print("Server is already running")
            return

        self.running = True

        try:
            # Create socket
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.socket.bind((self.host, self.port))
            # Backlog of 1 meant a reconnecting client could complete the TCP
            # handshake and then never be accept()ed - a connection that looks
            # established but is never serviced.
            self.socket.listen(5)

            # Start server thread
            self.server_thread = threading.Thread(target=self._server_loop)
            self.server_thread.daemon = True
            self.server_thread.start()

            # start() is called from the operator, i.e. the main thread, so
            # this is the only safe place to touch bpy.app.timers.
            self._register_drain_timer()

            print(f"BlenderMCP server started on {self.host}:{self.port}")
        except Exception as e:
            print(f"Failed to start server: {e!s}")
            self.stop()

    def _register_drain_timer(self) -> None:
        """
        Register the drain callback, holding the exact object Blender is given.

        `bpy.app.timers` matches registrations by **identity**, and
        `self.drain_command_queue` builds a *new* bound method on every
        attribute access - so a fresh access can neither be found by
        `is_registered()` nor removed by `unregister()`, which raises
        `ValueError: function is not registered` for it. Measured against
        Blender 5.2.2 and recorded in `PHASE2_TASK_STATE.md` under Task 1's
        timer-identity probe; no transcript is pasted here, because a transcript
        in a docstring that no committed script emits cannot be re-run and three
        of them were found in this task alone.

        Capturing one reference here is what makes both the
        guard below and `_unregister_drain_timer` mean what they read as
        meaning.

        Must only be called from Blender's main thread: `bpy.app.timers` is not
        thread-safe and a registration made elsewhere can be silently dropped.
        """
        if self._drain_timer is not None and bpy.app.timers.is_registered(self._drain_timer):
            return
        self._drain_timer = self.drain_command_queue
        bpy.app.timers.register(self._drain_timer, persistent=True)

    def _unregister_drain_timer(self) -> None:
        """
        Remove the drain callback, so restarts cannot accumulate timers.

        Passing the held reference is mandatory for the identity reasons in
        `_register_drain_timer`; a fresh `self.drain_command_queue` removed
        nothing, which left one extra live timer per start/stop cycle. Each
        duplicate carries its own `_MAX_COMMANDS_PER_TICK` /
        `_DRAIN_TIME_BUDGET_SECONDS` allowance, so accumulation multiplies
        exactly the budget that protects Blender's main thread.

        A timer that Blender has already dropped is an expected outcome, not an
        error: `drain_command_queue` returns None once `running` is false and
        Blender discards a timer that returns None. That self-heal is kept as a
        belt-and-braces path, but it is not the mechanism this code relies on.
        """
        timer = self._drain_timer
        self._drain_timer = None
        if timer is None:
            return
        with suppress(Exception):
            if bpy.app.timers.is_registered(timer):
                bpy.app.timers.unregister(timer)

    def stop(self) -> None:
        self.running = False
        self._unregister_drain_timer()

        # Close socket
        if self.socket:
            with suppress(Exception):
                self.socket.close()
            self.socket = None

        # Shut down live client sockets. Without this, handler threads stay
        # parked in a blocking recv() forever; being daemon threads they then
        # outlive the restart and close connections the new server owns
        # (the WinError 10054 seen after toggling the addon).
        with self._clients_lock:
            clients = list(self._clients)
            self._clients.clear()
        for client in clients:
            with suppress(Exception):
                client.shutdown(socket.SHUT_RDWR)
            with suppress(Exception):
                client.close()

        # Drop any commands that will never be serviced now.
        while True:
            try:
                self.command_queue.get_nowait()
            except queue.Empty:
                break

        # Wait for thread to finish
        if self.server_thread:
            try:
                if self.server_thread.is_alive():
                    self.server_thread.join(timeout=1.0)
            except Exception:
                pass
            self.server_thread = None

        print("BlenderMCP server stopped")

    def _server_loop(self) -> None:
        """Main server loop in a separate thread."""
        print("Server thread started")
        self.socket.settimeout(1.0)  # Timeout to allow for stopping

        while self.running:
            try:
                # Accept new connection
                try:
                    client, address = self.socket.accept()
                    print(f"Connected to client: {address}")

                    # Handle client in a separate thread
                    client_thread = threading.Thread(target=self.handle_client, args=(client,))
                    client_thread.daemon = True
                    client_thread.start()
                except TimeoutError:
                    # Just check running condition
                    continue
                except Exception as e:
                    print(f"Error accepting connection: {e!s}")
                    time.sleep(0.5)
            except Exception as e:
                print(f"Error in server loop: {e!s}")
                if not self.running:
                    break
                time.sleep(0.5)

        print("Server thread stopped")

    def drain_command_queue(self) -> float | None:
        """
        Run queued commands on Blender's main thread.

        Registered once by start(); returns the poll interval so Blender keeps
        calling it. All bpy access happens here, on the main thread.

        Two mechanisms keep a command from running against a file it was not
        sent for, and they are **not** alternatives - they cover disjoint cases:

        =========================================  =========  =========
        case                                       snapshot   stamp
        =========================================  =========  =========
        queued before, swap succeeds               rejects    rejects
        queued before, swap **fails**              rejects    silent
        queued before, swap is **aborted**         rejects    rejects
        arrives **during** the load                misses     rejects
        arrives **during** an aborted load         misses     rejects
        arrives after the client saw the response  executes   executes
        never stamped at all                       misses     rejects
        =========================================  =========  =========

        The two abort rows are why `session.mark_session_indeterminate` exists:
        `load_post` does not fire on an aborted load and neither does
        `load_post_fail`, so without moving the marker in
        `_run_session_swap`'s `except` the stamp column read "silent" there and
        a command queued mid-load executed against a half-replaced database.

        The stamp is checked here, first, because a stale command must not be
        able to trip the barrier a second time. See `_stamp_session` for the
        recorded design and `_run_session_swap` for the snapshot half.

        Returns:
            Result produced by the operation.

        """
        if not self.running:
            return None

        try:
            self._drain_batch()
        except BaseException:
            # Blender **drops** a timer callback that raises - measured live on
            # 5.2.2 via the rig's in-Blender probe:
            #
            #   {"calls_after_raising_once": 1, "still_registered": false,
            #    "successor_calls": 9, "successor_still_registered": true}
            #
            # `persistent=True` does not change it. So re-raising out of here -
            # which `_execute_and_answer` has to do rather than swallow an abort
            # - would end the drain loop for good, and **every** connected
            # client would then wait out its own 180 s timeout with no response
            # and no error, forever after. The same measurement gives the fix:
            # a successor registered from inside the failing callback, as a
            # different function object, survives and keeps ticking.
            self._replace_this_dying_timer()
            raise
        return 0.05

    def _drain_batch(self) -> None:
        """
        Run up to one tick's worth of queued commands, applying both barriers.

        Split out of `drain_command_queue` so the recovery path there wraps
        every statement of it, including the `get_nowait` that starts the loop.
        """
        processed = 0
        deadline = time.monotonic() + self._DRAIN_TIME_BUDGET_SECONDS
        while processed < self._MAX_COMMANDS_PER_TICK and time.monotonic() < deadline:
            try:
                command, client = self.command_queue.get_nowait()
            except queue.Empty:
                break

            # Popped rather than read: the stamp is transport bookkeeping and
            # has no business reaching a handler's `command` dict.
            #
            # **Fails closed.** An *absent* stamp used to execute, on the
            # argument that `_stamp_session` is the sole producer and
            # `test_the_enqueue_path_is_the_only_producer_and_it_stamps` proves
            # it. That test walks `ast.FunctionDef` only, matches only
            # `.put_nowait`, and requires the receiver spelled
            # `<x>.command_queue`; five producer shapes were demonstrated to
            # evade it - a blocking `put()`, an `async def`, a local alias, a
            # lambda at class scope, and a helper that takes the queue as an
            # argument. The last two are exactly what Task 6's re-queue path
            # looks like. Failing open bought nothing, because the sole producer
            # already stamps: rejecting costs a correct system nothing and costs
            # an incorrect one one error frame instead of a command run against
            # a database it was not sent for.
            stamp = command.pop(self._SESSION_STAMP_KEY, None)
            if stamp != self._session_marker():
                self._discard_superseded([(command, client)], self._reject_reason(stamp))
                processed += 1
                continue

            # The indeterminate latch, checked **after** the stamp so a stale
            # command is still reported as stale, and **before** dispatch so
            # nothing runs against a database that may be part of two files.
            # Without it the flag was advisory: `session.INDETERMINATE_SESSION_NOTE`
            # had no read site anywhere in `src/`, so the command after an abort
            # answered `status: success` against a half-replaced database.
            if session_is_indeterminate() and command.get("type") not in self._INDETERMINATE_SAFE_COMMANDS:
                self._discard_superseded([(command, client)], self._INDETERMINATE_REASON)
                processed += 1
                continue

            # Membership, not just name: `open_shot` and `reset_session` are not
            # dispatchable until Task 6, and a barrier that fires for a command
            # the addon cannot run lets one 40-byte frame discard up to
            # `_MAX_QUEUED_COMMANDS` commands belonging to *other* server
            # processes. An undispatchable name falls through to the ordinary
            # path, which answers it "Unknown command type" and costs nobody
            # else anything.
            #
            # Asked **before** the dequeue result is committed to: if
            # `_build_command_handlers()` raises, the command has already been
            # popped and would be lost with no response on any path, so the
            # answer is produced from the `except` rather than left to a caller
            # that no longer has it.
            try:
                is_swap = command.get("type") in self._SESSION_SWAP_COMMANDS and self._is_dispatchable(
                    command.get("type")
                )
            except Exception as e:
                print(f"Could not classify a dequeued command: {e!s}")
                traceback.print_exc()
                self._answer(command, client, {"status": "error", "message": "Command could not be dispatched"})
                processed += 1
                continue

            if is_swap:
                self._run_session_swap(command, client)
                break

            self._execute_and_answer(command, client)
            processed += 1

    def _replace_this_dying_timer(self) -> None:
        """
        Hand the drain loop to a fresh callback before this one is dropped.

        Runs from a timer callback, which is Blender's main thread, so
        registering here is legal - the prohibition in `_register_drain_timer`
        is about *client* threads.

        `self._drain_timer` is cleared first so `_register_drain_timer` does not
        short-circuit on the doomed registration, and because every access to
        `self.drain_command_queue` builds a **new** bound-method object, the
        successor is a different object from the one Blender is about to drop
        (measured; see `_register_drain_timer` for the identity rules).

        A stopped server is left alone: resurrecting a timer that `stop()`
        deliberately removed is the accumulation `_unregister_drain_timer`
        exists to prevent.

        **The dying timer is unregistered explicitly, rather than trusted to be
        dropped.** Blender really does drop a callback that raises, and
        `persistent=True` does not change it - the live rig reports
        `calls_after_raising_once: 1, still_registered: false`. But "exactly one
        live drain timer" then rests entirely on a behaviour this stub does
        not model, so no test could ever see a duplicate. Asking Blender to
        remove it first makes the invariant hold by construction on both sides.
        The call is suppressed because the drop usually got there first, which
        raises `ValueError: function is not registered`.

        A failed handoff is **printed**. `suppress(Exception)` around the
        registration was silent, and its consequence is not: with no drain timer
        and `running` still true, every connected client waits out its own 180 s
        timeout having received nothing, forever after. Blender's console is the
        only place an operator can see that it happened.
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
        except Exception as e:
            print(f"BlenderMCP: could not hand the drain loop to a fresh timer ({e!s}) - restart the MCP server")
            traceback.print_exc()

    def _is_dispatchable(self, cmd_type: object) -> bool:
        """
        Report whether a command name has a handler behind it right now.

        `_build_command_handlers()` is rebuilt per call and is not cheap, which
        is why this is asked only for the two names in
        `_SESSION_SWAP_COMMANDS` - never per command.

        Args:
            cmd_type: The command name from the frame.

        Returns:
            bool: True when `execute_command` would find a handler for it.

        """
        return cmd_type in self._build_command_handlers()

    @staticmethod
    def _session_marker() -> tuple[object, object]:
        """
        Read the pair that identifies "which database, in which process".

        The epoch alone is not monotonic - module state is rebuilt at 0 by a
        Blender restart or Reload Scripts - so a bare counter can hand back a
        value a client has already seen against a different database. Pairing it
        with the process-unique `session_id` closes that.

        Returns:
            tuple: `(session_id, session_epoch)`, both immutable.

        """
        snapshot = session_snapshot()
        return (snapshot["session_id"], snapshot["session_epoch"])

    def _stamp_session(self, command: dict) -> None:
        """
        Record, on the client thread, which database a command was queued against.

        **This is the recorded design**, `docs/superpowers/plans/PHASE2_TASK_STATE.md:2265-2275`:

            "Post-load inspection therefore **cannot** distinguish 'queued
            before the swap', 'arrived during the swap' and 'sent after the
            client saw the swap response': all three sit in one FIFO with no
            marker. The epoch has to be stamped **at enqueue time, on the client
            thread**, and compared at dequeue."

        The pre-swap queue snapshot in `_run_session_swap` cannot substitute for
        it, because `session_epoch` moves in `load_post` - at the *end* of the
        load. `wm.open_mainfile` was measured at 4.6 s on a 1.05 GB fixture
        (TASK_STATE decision 11), every `handle_client` thread keeps running
        throughout, and a second server *process* has no way to know a swap is
        under way at all. Everything those threads enqueue during that window
        arrives after the snapshot was taken and before the epoch moved.

        Stamping is legal here because it reads two immutable values off module
        state and touches **no `bpy`**; the whole point of this thread boundary
        is that Blender data is not read from it.

        An *unstamped* command is **rejected**, not executed. This being the sole
        producer is why that costs nothing: nothing else in `src/` puts anything
        on `command_queue`, so in a correct tree the closed branch is never
        taken. It is the incorrect tree the branch is for -
        `test_the_enqueue_path_is_the_only_producer_and_it_stamps` asserts the
        sole-producer property over the AST, but five producer shapes were
        demonstrated to evade its previous form, so the static check is a second
        line of defence behind the runtime one rather than in front of it.

        Args:
            command: The decoded command, mutated in place. The key is
                overwritten unconditionally, so a client cannot forge it.

        """
        command[self._SESSION_STAMP_KEY] = self._session_marker()

    def _execute_and_answer(
        self,
        command: dict,
        client: object,
        *,
        receipt: "dict | None" = None,
    ) -> None:
        """
        Run one command and write exactly one response frame back to its client.

        `BaseException` is caught, not just `Exception`. `MemoryError` is an
        `Exception` and was already handled; `KeyboardInterrupt`, `SystemExit`
        and `GeneratorExit` are not, and a 1 GB `wm.open_mainfile` on Blender's
        main thread is exactly where a blender-side abort lands. Before this,
        the abort propagated out of `drain_command_queue` and the swap's own
        client received no frame at all;
        `test_the_swaps_own_client_is_answered_when_the_swap_raises_a_base_exception`
        reproduces it on demand (revert-matrix row "only Exception is caught, so
        a blender-side abort strands the swap's own client"), which is a
        re-runnable instrument where the pasted transcript that used to sit here
        was not.

        The client is answered **first** and the exception is then re-raised
        unchanged, so the abort still reaches Blender rather than being
        swallowed into a silent no-op.

        **`receipt` is an observational receipt, not a prediction, and that is
        the whole of its meaning.** `_run_session_swap` has to know whether this
        function answered the swap's own client, because it is the only thing
        that does and the `except` there must not leave that client in `recv()`
        for its 180 s timeout. The flag it replaces was `handed_off = True` in
        the *caller*, set immediately before this call - so a `BaseException`
        between that statement and this function's own `try:` was answered by
        nobody at all (a critic measured `frames=0` through that window). A
        prediction cannot be made accurate by moving it closer; the receipt is
        written by `_answer` itself, as its first statement, so "answered" means
        an answer was actually begun on this socket. `dict`, not `bool`, only
        because the callee has to be able to write to the caller's binding.

        **The window this leaves, stated rather than implied.** The receipt marks
        `_answer` as *entered*, not as completed, so a `BaseException` raised
        inside `_answer` after that first statement and before the frame reaches
        the socket is recorded as answered. That direction is chosen
        deliberately: recording completion instead would let the same abort
        produce two frames on one socket, and a duplicate response is a
        correctness problem for a client that matches by ordering, where a
        missing one is a timeout.

        Args:
            command: The decoded command to run.
            client: The socket its response belongs on.
            receipt: Optional dict whose `"answered"` key is set True by
                `_answer`; see above. Keyword-only, so it cannot be passed
                positionally into the slot a future argument takes.

        """
        response: dict
        try:
            response = self.execute_command(command)
        except Exception as e:
            print(f"Error executing command: {e!s}")
            traceback.print_exc()
            response = {"status": "error", "message": str(e)}
        except BaseException as e:
            print(f"Command aborted by {type(e).__name__}: answering the client before re-raising")
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

    def _answer(self, command: dict, client: object, response: dict, *, receipt: "dict | None" = None) -> None:
        """
        Write one response frame, whatever the handler did or did not produce.

        **Every frame this function builds carries the session marker**, not
        only a barrier rejection, `get_addon_info` and `get_session_info`. The
        qualifier is exact and an earlier revision dropped it: `_encode_response`
        falls back to `_error_frame` when a response cannot be serialized or
        exceeds `_MAX_MESSAGE_BYTES`, and that frame carries an id, a status and
        a message and no marker at all. Those two paths are already telling the
        client its command produced nothing usable, so they are not a silent
        staleness hole - but "every frame" without the qualifier was false, and
        a client written against it would be wrong twice.

        Before the marker was here, it reached
        the server only when one of those three happened, so an artist doing
        File -> Open in Blender's own UI - no MCP command in flight - moved the
        epoch and nothing noticed: `send_command` kept gating on the cached
        `capabilities`, and `writable_output_roots`, which decides where files
        may be written, stayed wrong. Two integers on a frame the server is
        already parsing closes it, and the server-side reader
        (`connection._reported_session_marker`) already looks at frame level
        first because the rejection path put them there.

        **The receipt is written first, before anything that can fail.** It is
        how `_run_session_swap` knows the swap's own client has an answer coming
        from here rather than from its own `except`, and the whole value of it
        over the `handed_off` flag it replaces is that it is written by the
        function that does the work instead of guessed by the caller one
        statement earlier. Everything below can raise; the receipt must not
        depend on which of it ran, because a caller that concludes "not
        answered" after this function began answering produces two frames on one
        socket.

        Args:
            command: The command being answered, for its echoed id.
            client: The socket the frame belongs on.
            response: The response body.
            receipt: Optional dict to record the answer in; see above. Only
                `_run_session_swap`'s path passes one.

        """
        if receipt is not None:
            receipt["answered"] = True

        # Echo the request id (if any) back so the client can match this
        # response to the command it sent instead of relying purely on
        # stream ordering.
        response["id"] = command.get("id")
        response["session_id"], response["session_epoch"] = self._session_marker()

        # Encoded *before* the try that guards the send, so a failure to
        # serialize can never be mistaken for a disconnect.
        payload = self._encode_response(response, command.get("id"))
        try:
            self._send_frame(client, payload)
        except Exception:
            print("Failed to send response - client disconnected")

    def _drain_queue_into(self, superseded: list[tuple[dict, object]]) -> None:
        """
        Empty the command queue into a list the **caller** already owns.

        Appending into the caller's list rather than returning a new one is the
        whole point of the signature. A `BaseException` raised part-way through
        this loop must not lose the commands already taken off the queue: they
        are unreachable by `stop()`'s own drain and by every other rejection
        path, so their clients would wait out a 180 s timeout for a response no
        code path could still produce. `_run_session_swap`'s `finally` answers
        whatever is in the list, including a partial one.

        The bound is the loop's own. `while True: get_nowait()` races producers
        that free a slot as fast as it frees one, so an earlier docstring's claim
        that the drain was "bounded by `_MAX_QUEUED_COMMANDS`" was not enforced
        by anything.

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

        The queue is emptied **before** the swap is executed, on this thread.
        That timing is what makes this half of the barrier mean anything:
        everything taken out here was queued while the old file was still open.
        Measured on Blender 5.2.2 (Task 2 Step 7), the drain loop otherwise keeps
        draining after `wm.open_mainfile` returns - inside the same tick - and
        answers a command queued against one shot from a different file, with
        `status: success` and nothing marking the change.

        What this snapshot **cannot** see is anything a client thread enqueues
        during the load, which is `_stamp_session`'s half. What the stamp cannot
        see is a **failed** swap, where the epoch never moves and every stamp
        still matches - so both exist, and neither is redundant.

        The drain itself is `_drain_queue_into`, which appends into the list
        built here rather than returning one of its own - so a `BaseException`
        raised part-way through it still leaves everything already dequeued
        reachable by the `finally` below. Commands taken off the queue and then
        dropped are unreachable by this rejection path and by `stop()`'s own
        drain alike, and their clients would wait out a 180 s timeout for a
        response no code path can still produce.

        The rejections go out in the `finally`, and *after* the swap, so the
        epoch they name is the one that is current once the outcome is known.

        **The abort guard is one positive condition, and it used to be three
        negative ones.** The `try` spans the pre-swap drain as well as the swap,
        so a bare `except BaseException: mark_session_indeterminate()` asserted
        something false about the user's data whenever the abort landed anywhere
        but mid-load. The previous repair excluded those cases by asking what had
        *not* happened - was the swap dispatched, did the marker stay put, did
        the failure counter stay put - and inferring "then it must be mid-load".
        Three things were wrong with that, and the third is why it is gone:

        - it latched for an abort between the dispatch and `wm.open_mainfile`
          actually starting (`execute_command`'s own dispatch, and from Task 6
          the swap handler's path validation), which is a false positive
          recorded as a Task 6 residual;
        - it needed two separate flags to avoid double-answering the swap's own
          client, because one of them was also the caller's hand-off marker;
        - **it had a false negative a critic reproduced.** A handler that fails
          one open and is then aborted part-way through a second, real load had
          already moved `load_failures` on the first open, so the counter
          condition read "a failure accounted for this abort" and
          `session_indeterminate` stayed False while the second load really had
          been cut in half. Every negative predicate has this shape: it is an
          inference, and an inference can be satisfied by the wrong event.

        `session.load_in_flight()` replaces all three with the thing itself.
        Measured on 5.2.2 by `scripts/blender_probes/session_handlers.py`:
        `load_pre` fires for `wm.open_mainfile` on the succeeding path and on
        every failing one, and when it fires `bpy.data.filepath` and the object
        table are still the old file's. So the flag is True exactly between "a
        load was begun" and "that load was accounted for", `load_post` clears it
        on success, `load_post_fail` clears it on a clean failure, and an abort
        observed while it holds is the one case where the open database may be
        part of two files. An abort before `load_pre` reads False, which is not a
        guess: Blender had not begun reading over the old database yet.

        **The swap's own client is answered when the abort beat the answer.**
        `_drain_queue_into` runs inside the `try` and before `_execute_and_answer`,
        which is the only thing that answers the swap command; its superseded
        siblings are answered in the `finally` and it is not, so an abort during
        the pre-swap drain left it in `recv()` for its full 180 s timeout with
        neither a response nor an error - §07's automatically-critical outcome.
        The `receipt` dict is how that is decided, and it is an **observation
        rather than a prediction**: `_answer` sets it as its own first statement,
        so `not receipt["answered"]` means no answer was begun on this socket by
        anyone. The `handed_off = True` it replaces was set here, in the caller,
        immediately before the call - which left a real window (a critic measured
        `frames=0` through it) where a `BaseException` raised after that
        statement and before `_execute_and_answer`'s own `try:` was answered by
        nobody, while this docstring said the window was closed.

        **An abort moves the session marker before it is re-raised**, which is
        `session.mark_session_indeterminate`'s whole reason to exist. `load_post`
        never fires on an aborted `wm.open_mainfile` and neither does
        `load_post_fail`, so without this the epoch stayed put, every command a
        client thread enqueued *during* that load carried a stamp that still
        matched, and it executed on the next tick against a half-replaced
        database. The swap's own client was told the session might be
        indeterminate; nobody else was told anything. Pinned by
        `test_an_aborted_swap_invalidates_the_stamps_taken_during_its_load`,
        which observes exactly that when the call below is reverted (revert-matrix
        row "an aborted swap leaves the marker where it was"). Moving the marker in the `except` - before
        the `finally` runs - means the rejection frames below name the moved
        epoch as well, so every peer that had something queued learns it too.

        Args:
            command: The session-swap command to run.
            client: The socket its response belongs on.

        """
        superseded: list[tuple[dict, object]] = []
        receipt = {"answered": False}

        try:
            self._drain_queue_into(superseded)
            self._execute_and_answer(command, client, receipt=receipt)
        except BaseException:
            # Two independent obligations, so two independent `if`s. An `elif`
            # here made them exclusive, and the case it dropped was the one that
            # matters: a second abort that lands before `_answer` writes the
            # receipt leaves the receipt False while `load_pre` has already
            # fired, so the latch was skipped exactly when a load was in flight.
            # Read before the latch, because `mark_session_indeterminate` clears
            # the flag (T3-19); the message below depends on it.
            mid_load = load_in_flight()
            if mid_load:
                mark_session_indeterminate()
            if not receipt["answered"]:
                self._answer(command, client, {"status": "error", "message": self._abort_message(mid_load)})
            raise
        finally:
            self._discard_superseded(superseded)

    # What the swap's own client is told when nothing had begun answering it -
    # the pre-swap drain, and the prologue between it and `_execute_and_answer`'s
    # own `try:`, both of which are windows where no other code path in this
    # class can still answer. No path: the swap's filepath is sitting in the
    # command being processed and this socket is unauthenticated.
    #
    # **It claims the database is unchanged, so it is sent only when
    # `load_in_flight()` is False**, which is an observation rather than the
    # inference this comment used to carry. The old text read "every route out of
    # `_execute_and_answer` writes the receipt through `_answer` first, so
    # reaching this string means ... `load_pre` cannot have run". Two routes do
    # not write the receipt - a `BaseException` raised inside the
    # `except Exception` body (`print`, `traceback.print_exc()`), and one inside
    # the `except BaseException` body before `_answer` is reached - and through
    # either of them this string was sent with a load genuinely in flight.
    # `_abort_message` now asks the flag instead of arguing from the receipt.
    _ABORTED_BEFORE_HANDOFF = (
        "Blender aborted before this file swap was dispatched; nothing was loaded and the open "
        "database is unchanged. Poll get_session_info, then resend."
    )
    # The same window with a load actually in flight. It says the opposite thing
    # about the database, and it points at the repair rather than at a resend,
    # because a resend against a half-replaced database is the move the latch
    # exists to prevent.
    _ABORTED_MID_LOAD = (
        "Blender aborted this file swap while a load was in flight; the open database may be part of "
        "two files and this session is now marked indeterminate. Poll get_session_info and open a "
        "known shot before resending anything."
    )
    # The two reasons a dequeued command is answered without being run. Named
    # constants rather than an inline string because the second one is *not* a
    # swap and saying it was would be the same class of lie the epoch wording
    # below exists to avoid.
    _SUPERSEDED_REASON = (
        "a session file swap was attempted while this command was queued, "
        "so it was never run against the file it was sent for"
    )
    _UNSTAMPED_REASON = (
        "it reached the queue without the session stamp the enqueue path applies, "
        "so which database it was sent for cannot be established"
    )
    # The third reason, and the only one that persists across ticks. Its own
    # constant rather than a reuse of `_SUPERSEDED_REASON`, because a client
    # that reads "a swap was attempted while this command was queued" will
    # resend, and resending is precisely the wrong move here: the condition
    # outlives the batch and is cleared only by a completed load.
    _INDETERMINATE_REASON = (
        "a session file swap was aborted part-way and no load has completed since, "
        "so the open database may be part of two files; poll get_session_info and "
        "open a known shot before resending anything"
    )
    # What may still run while `session.session_is_indeterminate()` holds, and
    # why each one: the two swap commands are how a client **repairs** the
    # condition (a completed `load_post` is the only thing that clears it), and
    # the two read-only reports are how a client **observes** it - both publish
    # `session_indeterminate`, so refusing them would hide the reason for every
    # other refusal. Nothing else runs: this is the whole point of the latch.
    _INDETERMINATE_SAFE_COMMANDS = frozenset({"get_addon_info", "get_session_info", "open_shot", "reset_session"})

    def _abort_message(self, mid_load: bool) -> str:
        """
        Say what an abort did to the database, rather than what it probably did.

        Both strings are sent from the same window - no answer had begun on the
        swap's socket - and they differ on the only question the client needs
        settled: whether Blender had begun reading over the open database. That
        is `load_in_flight()`, read before the latch clears it.

        Args:
            mid_load: Whether a load was in flight when the abort was observed.

        Returns:
            str: The message for that case.

        """
        return self._ABORTED_MID_LOAD if mid_load else self._ABORTED_BEFORE_HANDOFF

    def _reject_reason(self, stamp: object) -> str:
        """
        Say why a dequeued command is being answered instead of run.

        Args:
            stamp: The session marker popped off the command, or None when the
                command carried none at all.

        Returns:
            str: The clause `_discard_superseded` folds into its message.

        """
        return self._UNSTAMPED_REASON if stamp is None else self._SUPERSEDED_REASON

    def _discard_superseded(self, superseded: list[tuple[dict, object]], reason: str | None = None) -> None:
        """
        Answer every command a swap invalidated, under a deadline, so no client waits.

        **The message names the current epoch; it does not assert that the epoch
        moved.** After a successful swap the epoch has changed, so a client
        comparing it against its cached value re-handshakes before resending -
        the advertised capability set is scene-gated and therefore follows the
        swapped file. After a *failed* swap the epoch is unchanged (a failed
        `open_mainfile` leaves the old database completely untouched), so the
        same message tells that client to skip a pointless re-handshake and
        simply resend. Wording that hard-coded "the epoch changed" would be a lie
        on the second path.

        The epoch and the session id are also **fields** on the frame, not only
        prose. A client that has to regex an English sentence to notice its
        cached `capabilities` went stale will not notice, which is how
        `connection.py` came to hold a handshake across a swap with nothing
        watching.

        No path appears in the message. The swap's own filepath is sitting right
        there in the command being processed, and an absolute path reaching a
        client is a disclosure this layer has no business making.

        **Every peer is sent to; the budget decides how long a send may block,
        never whether one is attempted.** The previous form read
        `if time.monotonic() >= deadline or not self._send_bounded(...)`, and
        Python short-circuits `or`: once the *global* budget was spent
        `_send_bounded` was never called again, so every remaining client was
        closed with **zero** send attempts and its health never tested. Five slow
        peers are enough:
        `test_a_healthy_peer_queued_behind_stalled_ones_is_still_answered` builds
        exactly that queue and sees the healthy peer receive no frame at all when
        the short-circuit is restored (revert-matrix row "`or` short-circuits
        again"). That defeated the rejection's own purpose as well as the plan's
        no-dropped-socket rule: this frame is the only carrier of the
        `session_id`/`session_epoch` pair, so the peers the barrier dropped were
        exactly the ones that never learned to re-handshake.

        Past the deadline the send is still made, under a 1 ms floor
        (`_PAST_BUDGET_SEND_TIMEOUT_SECONDS`). A ~250-byte frame fits any send
        buffer that is not already full, so a healthy peer queued behind a
        stalled one is answered rather than closed, and a stalled one fails
        almost immediately instead of costing another timeout. The floor is
        positive rather than zero because `settimeout(0.0)` takes the socket out
        of timeout mode entirely, and its own `handle_client` thread is sitting
        in `recv()` on it - see the constant for the connection that cost.

        **"Almost immediately" is 1 ms, twice, per peer - not zero.** Three
        revisions of this file called the past-budget pass a "non-blocking
        tail", and since the floor stopped being `0.0` that has been false: the
        write lock is acquired under the same timeout before `sendall` blocks
        under it. For **one** peer the cost is paid once, because the first
        failure abandons it and every later entry belonging to it is skipped -
        which is exactly why
        `test_rejecting_a_full_queue_to_a_stalled_peer_is_bounded` cannot see
        this. For `_MAX_QUEUED_COMMANDS` entries belonging to as many *distinct*
        peers - the multi-process case this barrier exists for - nothing is
        skipped and the cost is paid 255 times.
        `test_rejecting_a_full_queue_to_distinct_stalled_peers_is_bounded` is
        that shape; measured on a quiet box (0.12 load per core), the whole pass
        takes 0.644 / 0.644 / 0.641 s.

        **A peer is abandoned on the first failed write, and deliberately not on
        the second.** P1b asked for a second-consecutive-failure rule; it is not
        safe here and the reason is `sendall`: a `sendall` that times out or
        raises `BlockingIOError` may already have written *part* of the frame,
        and this stream is parsed by newline framing on both sides. Writing
        again after a failure would splice a truncated line into it. So the
        tolerance for a merely slow peer is bought the other way - by raising
        `_REJECTION_SEND_TIMEOUT_SECONDS` to a value a live loopback reader
        cannot hit - and a peer that still fails has its socket closed, which is
        also what makes the half-written frame harmless: the peer sees a partial
        line then EOF, which is an answer, rather than silence, which is a hang.

        **The bound, stated as arithmetic rather than as a measurement.** One
        blocking send may be in flight when the deadline passes, and
        `_send_frame` takes the per-client write lock under the same timeout, so
        the blocking phase costs at most
        `_REJECTION_TIME_BUDGET_SECONDS + 2 * _REJECTION_SEND_TIMEOUT_SECONDS`
        = 0.75 s. **The tail adds to that rather than being free**: at most
        `_MAX_QUEUED_COMMANDS` entries, each costing up to
        `2 * _PAST_BUDGET_SEND_TIMEOUT_SECONDS` = 2 ms (the lock, then the
        write), so up to 0.512 s more. The whole pass is bounded by **1.262 s**,
        and the 0.75 s this docstring used to name was the blocking phase
        mistaken for the total. Before any of these bounds existed the
        worst case was `_MAX_QUEUED_COMMANDS` entries at the socket's own
        `_CLIENT_SOCKET_TIMEOUT_SECONDS` each - 256 x 1.0 s - on Blender's main
        thread, with the UI frozen throughout and every *other* connected
        process exceeding its own 180 s timeout having received nothing at all.

        Args:
            superseded: `(command, client)` pairs taken off the queue, bounded
                by `_run_session_swap`'s own loop.
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
            # A socket this pass already closed is skipped outright rather than
            # written to again: the write would fail, and paying a syscall - or
            # a timeout - to rediscover that is exactly the cost the budget is
            # trying not to spend.
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

    def _send_bounded(self, client: object, payload: bytes, timeout: float) -> bool:
        """
        Write one frame under a caller-chosen deadline, then put the socket's own back.

        The timeout is restored because this socket belongs to a `handle_client`
        thread that is concurrently sitting in `recv()` with
        `_CLIENT_SOCKET_TIMEOUT_SECONDS` of its own; leaving it at the rejection
        path's much shorter value would turn that loop into a spin. **No caller
        may pass 0**: that leaves the socket non-blocking rather than merely
        impatient, and a non-blocking `recv` raises `BlockingIOError`, which is
        not a `TimeoutError` and which `handle_client` therefore treated as a
        dead peer. `_PAST_BUDGET_SEND_TIMEOUT_SECONDS` is the floor that keeps
        that from being expressible from inside this class.

        **A failed restore reports the send as not delivered**, even when the
        write itself succeeded. The previous form restored inside a `finally`
        under `suppress(Exception)` and returned True regardless, so a socket
        left at 50 ms - or, now, non-blocking - stayed in the registry with its
        `recv` loop spinning at 20 Hz for the life of the process. Reporting
        False routes it to `_abandon_unreachable_client`, which closes it: a peer
        whose timeout cannot be set is one this server can no longer talk to on
        the terms the rest of the code assumes.

        Args:
            client: The socket to write to.
            payload: The framed bytes.
            timeout: Seconds the write may block. `_PAST_BUDGET_SEND_TIMEOUT_SECONDS`
                means "attempt it, and barely wait" - it is a 1 ms floor, not
                zero, and it is spent twice (the write lock, then `sendall`).
                Reading it as "do not wait" is what made the past-budget pass
                look free; see `_discard_superseded` for what it actually costs.

        Returns:
            bool: True when the frame was written and the socket was handed back
            in the state its own thread expects; False otherwise.

        """
        settimeout = getattr(client, "settimeout", None)
        try:
            if settimeout is not None:
                settimeout(timeout)
            self._send_frame(client, payload, timeout)
        except Exception:
            delivered = False
        else:
            delivered = True

        if settimeout is None:
            return delivered
        try:
            settimeout(self._CLIENT_SOCKET_TIMEOUT_SECONDS)
        except Exception:
            print("Could not restore a client socket's own timeout - closing it rather than leaving it spinning")
            return False
        return delivered

    def _abandon_unreachable_client(self, client: object) -> None:
        """
        Close a peer this thread could not answer, so it sees EOF rather than silence.

        Silence is the one outcome §07 makes automatically critical: the client
        waits out its own 180 s timeout having received no response and no
        error. A closed socket ends its `recv()` immediately with an error it
        can act on, and its `handle_client` thread then unwinds through the same
        cleanup a normal disconnect takes.

        Args:
            client: The socket to drop.

        """
        with self._clients_lock:
            self._clients.pop(client, None)
        with suppress(Exception):
            client.shutdown(socket.SHUT_RDWR)
        with suppress(Exception):
            client.close()

    # Messages are newline-delimited JSON. Bound how large a single message
    # can grow before we give up on it - without this, malformed input (or
    # a client that never sends a terminator) would make `buffer` grow
    # forever. The largest legitimate payloads are paginated mesh/element
    # dumps (capped well under 1000 elements); screenshots are written to
    # disk and never cross the socket. 64 MiB is generous headroom above that.
    _MAX_MESSAGE_BYTES = 64 * 1024 * 1024
    _MAX_QUEUED_COMMANDS = 256
    _MAX_COMMANDS_PER_TICK = 8
    _DRAIN_TIME_BUDGET_SECONDS = 0.02

    # How long a client socket waits in recv() before re-checking self.running.
    # Named because `_send_bounded` has to put it back after borrowing the
    # socket for a shorter write, and two copies of 1.0 would eventually differ.
    _CLIENT_SOCKET_TIMEOUT_SECONDS = 1.0
    # The rejection path's bounds, all on Blender's main thread and all
    # deliberately below `_CLIENT_SOCKET_TIMEOUT_SECONDS`. See
    # `_discard_superseded` for the worst case they add up to.
    #
    # **The send timeout is a health threshold, not a performance target**, and
    # 0.05 s was the wrong number for the job. A reader that takes tens of
    # milliseconds to drain its buffer - backpressured by a previous large
    # paginated response, which `_discard_superseded` itself names as a cause -
    # received zero frames and had its connection destroyed, forfeiting every
    # other command it had queued.
    # `test_a_peer_slower_than_a_loopback_reader_is_not_destroyed_for_it` pins
    # that case at 60 ms. 0.25 s is chosen so a peer failing it is genuinely not
    # reading: 4x that test's reader, and still a quarter of the timeout the
    # peer's own recv loop runs on.
    _REJECTION_SEND_TIMEOUT_SECONDS = 0.25
    _REJECTION_TIME_BUDGET_SECONDS = 0.25
    # Past the batch budget, a frame is still *attempted* - it just barely
    # waits. See `_discard_superseded` for why attempting it matters.
    #
    # **A positive floor, not zero, and the difference is a dropped connection.**
    # `settimeout(0.0)` puts the socket in **non-blocking** mode, and this socket
    # is concurrently being read by its own `handle_client` thread. A
    # non-blocking `recv` with no data raises `BlockingIOError`, which is
    # `OSError` -> `Exception` and **not** a subclass of `TimeoutError`
    # (`issubclass(BlockingIOError, TimeoutError)` is False), so it fell past
    # `handle_client`'s `except TimeoutError: continue` into its `except
    # Exception: break` and closed the peer. A critic reproduced ~100 of 150
    # rejection frames lost that way, 3 of 3 runs, against a clean control -
    # i.e. the fix for "no dropped healthy socket" reintroduced the defect it
    # was fixing. 1 ms keeps the socket in **timeout** mode throughout, so the
    # only thing a concurrent `recv` can raise is `TimeoutError`, which that
    # loop has always handled. It is still far below
    # `_CLIENT_SOCKET_TIMEOUT_SECONDS` and far below one tick's budget.
    #
    # **It did change the bound, and saying it did not was the third copy of
    # that claim in this file.** At 0.0 the past-budget pass really was
    # non-blocking; at 0.001 each entry can cost this twice - `_send_frame`
    # acquires the per-client write lock under it before `sendall` blocks under
    # it - and up to `_MAX_QUEUED_COMMANDS` entries belonging to distinct peers
    # are attempted, none of which the `abandoned` set can skip. That is 0.512 s
    # added to a blocking phase of 0.75 s. The trade is still the right one: 1 ms
    # of main-thread time per peer buys a peer that is not destroyed for being
    # slow, where 0.0 bought a `BlockingIOError` that closed it. The arithmetic
    # is restated in `_discard_superseded` rather than waved at here.
    _PAST_BUDGET_SEND_TIMEOUT_SECONDS = 0.001

    # Where `_stamp_session` records which database a command was queued
    # against. It lives on the command dict because the queue's `(command,
    # client)` 2-tuple shape is load-bearing for four existing test sites
    # (`tests/server/test_socket_unicode.py:104,127,155` and
    # `tests/server/test_threading.py:625`), none of which reads anything but
    # `type` and `params`. Popped at dequeue, so no handler ever sees it, and
    # overwritten unconditionally at enqueue, so no client can forge it.
    _SESSION_STAMP_KEY = "_blendermcp_session_marker"

    @staticmethod
    def _encode_frame(response: dict) -> bytes:
        """
        Serialize one response as a newline-terminated frame.

        Args:
            response: The JSON-serializable response body.

        Returns:
            bytes: The encoded frame, terminator included - see handle_client
            for why this protocol needs explicit framing.

        """
        return json.dumps(response).encode("utf-8") + b"\n"

    def _encode_response(self, response: dict, request_id: str | None) -> bytes:
        """
        Turn a handler's response into a frame that is always sendable.

        `json.dumps` used to run inside the `try` that guards `sendall`, whose
        `except` only printed "client disconnected". A handler returning a
        non-serializable value - a `mathutils.Vector`, say, and `handlers/`
        constructs those in ~95 places - therefore wrote no frame of any kind
        and was indistinguishable from a dropped socket, leaving the client to
        wait out its own 180s timeout. Failing over to a well-formed error
        frame, the same shape the oversize branch produces, turns a hang into
        an actionable reply.

        The client is told only *that* the response was unsendable: a repr of
        scene data, an absolute path, or a traceback in a client-facing message
        is a disclosure the transport layer has no business making. The detail
        goes to Blender's console instead, where the operator can see it.

        Args:
            response: The response body to encode.
            request_id: The request's id, echoed back so a fallback frame stays
                matchable to the command that produced it.

        Returns:
            bytes: The encoded frame, or an error frame if `response` could not
            be serialized or exceeds `_MAX_MESSAGE_BYTES`.

        """
        try:
            payload = self._encode_frame(response)
        except (TypeError, ValueError):
            print("Failed to serialize response - sending an error frame instead")
            traceback.print_exc()
            return self._error_frame(request_id, "Response could not be serialized to JSON")

        if len(payload) > self._MAX_MESSAGE_BYTES:
            return self._error_frame(request_id, "Response exceeded the configured message-size limit")
        return payload

    def _error_frame(self, request_id: str | None, message: str) -> bytes:
        """
        Build the one error frame shape every failure path returns.

        Args:
            request_id: The request's id, or None when it could not be read.
            message: Client-safe explanation; never a path or a traceback.

        Returns:
            bytes: The encoded error frame.

        """
        return self._encode_frame({"id": request_id, "status": "error", "message": message})

    def _send_frame(self, client, payload: bytes, lock_timeout: float | None = None) -> None:
        """
        Write one frame to a client, excluding any other writer to that socket.

        Two threads write to a single client socket: this server's client
        handler sends transport-validation errors while `drain_command_queue`
        sends responses from Blender's main thread. `sendall` is not atomic, so
        for a large response (the cap is 64 MiB) an error frame could splice
        into the middle of it and desync a stream both sides parse by newline
        framing.

        Lock ordering is deliberately trivial: `_clients_lock` guards only the
        registry and is released before the per-client lock is taken, and
        nothing else - no queue operation, no bpy access - happens while that
        lock is held.

        **The acquisition itself is what `lock_timeout` bounds, and without it
        the rejection path's own bounds did not hold.** `with send_lock:` waits
        forever. The other writer is `_send_protocol_error`, on a `handle_client`
        thread whose socket timeout is `_CLIENT_SOCKET_TIMEOUT_SECONDS`, so a
        single malformed frame arriving while a rejection batch is running
        parked Blender's main thread for as long as that thread held the lock,
        *inside* a call `_discard_superseded` documented as capped at 50 ms.
        `test_a_malformed_frame_arriving_mid_rejection_cannot_park_the_main_thread`
        holds it for `_LOCK_HELD_SECONDS` and asserts this call does not wait it
        out. A caller that passes a timeout
        gets `TimeoutError` instead, which `_send_bounded` reads as
        not-delivered; the default stays unbounded for the ordinary response
        path, where waiting for the lock is the correct behaviour and no budget
        is being promised.

        Args:
            client: The socket to write to.
            payload: The framed bytes to write.
            lock_timeout: Seconds to wait for the per-client write lock, or None
                to wait indefinitely. 0 attempts the acquisition without waiting.

        Raises:
            TimeoutError: When `lock_timeout` elapsed with the write lock still
                held by the other writer.

        """
        with self._clients_lock:
            send_lock = self._clients.get(client)
        if send_lock is None:
            # Untracked, or already torn down by stop(): there is no concurrent
            # writer left to exclude, and the send will simply fail if the
            # socket is gone.
            client.sendall(payload)
            return
        if lock_timeout is None:
            with send_lock:
                client.sendall(payload)
            return
        # `acquire(timeout=0)` is rejected by threading.Lock; the non-blocking
        # form is the same request spelled the way the API accepts it.
        acquired = send_lock.acquire(False) if lock_timeout <= 0 else send_lock.acquire(timeout=lock_timeout)
        if not acquired:
            raise TimeoutError("another thread holds this client's write lock")
        try:
            client.sendall(payload)
        finally:
            send_lock.release()

    def _send_protocol_error(self, client, request_id, message) -> None:
        """
        Return a bounded transport-level validation error without touching bpy data.

        One caller: `_decode_and_queue_frame`, answering a frame that never
        became a command. It has no handler result to report and must be
        incapable of raising - a failure here is one client left waiting in
        `recv()` until its own 180 s timeout. (The file-swap rejection path
        builds its own frame instead, because it carries the session fields and
        needs a deadline; see `_discard_superseded`.)

        Args:
            client: The socket the offending frame arrived on.
            request_id: The request's id, or None when it could not be read.
            message: Client-safe explanation; never a path or a traceback.

        """
        with suppress(Exception):
            self._send_frame(client, self._error_frame(request_id, message))

    def _decode_and_queue_frame(self, line: bytes, client) -> bool:
        r"""
        Decode one framed line, parse it as JSON, and queue it as a command.

        Args:
            line: The bytes of one `\n`-delimited frame (never containing
                the terminator itself).
            client: The socket the frame arrived on, passed through to the
                queued command so its response goes back to the right peer.

        Returns:
            False if `line` exceeds `_MAX_MESSAGE_BYTES` - the caller must
            treat this as a protocol violation and drop the connection.
            True otherwise, including when the line is malformed (discarded,
            not fatal).

        """
        if len(line) > self._MAX_MESSAGE_BYTES:
            print(f"Client sent an oversized frame ({len(line)} bytes) - disconnecting")
            return False
        try:
            command = json.loads(line.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            print(f"Discarding malformed message: {e!s}")
            self._send_protocol_error(client, None, "Malformed UTF-8 JSON command")
            return True

        if not isinstance(command, dict):
            self._send_protocol_error(client, None, "Command must be a JSON object")
            return True
        request_id = command.get("id")
        if request_id is not None and not isinstance(request_id, str):
            self._send_protocol_error(client, None, "Command id must be a string when provided")
            return True
        if not isinstance(command.get("type"), str) or not command["type"].strip():
            self._send_protocol_error(client, request_id, "Command type must be a non-empty string")
            return True
        if not isinstance(command.get("params", {}), dict):
            self._send_protocol_error(client, request_id, "Command params must be a JSON object")
            return True

        # Hand off to the main thread. Never call
        # bpy.app.timers.register() from here - it is not thread-safe and
        # the callback can be silently lost.
        #
        # Stamped here, on this client thread, and nowhere else: this is the one
        # moment at which "which database was this command sent for?" is still
        # knowable. See `_stamp_session` for the recorded design and for why a
        # post-load inspection cannot answer the same question.
        self._stamp_session(command)
        print(f"Queued command: {command.get('type')}")
        try:
            self.command_queue.put_nowait((command, client))
        except queue.Full:
            self._send_protocol_error(client, request_id, "Blender command queue is full; retry later")
        return True

    def handle_client(self, client) -> None:
        """
        Handle connected client.

        Args:
            client: Value for client.

        """
        print("Client handler started")
        # A finite timeout keeps this loop responsive to self.running instead
        # of parking in recv() forever.
        client.settimeout(self._CLIENT_SOCKET_TIMEOUT_SECONDS)
        with self._clients_lock:
            # One write lock per client, created here so both this thread and
            # the main-thread drain find the same one. See _send_frame.
            self._clients[client] = threading.Lock()
        buffer = b""

        try:
            while self.running:
                # Receive data
                try:
                    data = client.recv(8192)
                    if not data:
                        print("Client disconnected")
                        break

                    buffer += data

                    # A single json.loads() over the whole buffer can't tell
                    # "incomplete message" apart from "complete message plus
                    # the start of the next one" - both raise
                    # json.JSONDecodeError, and treating the latter as
                    # "incomplete, wait for more" means the buffer can never
                    # parse again (the trailing bytes are never valid on
                    # their own). Splitting on the newline terminator each
                    # side appends after every message removes that
                    # ambiguity: each complete line is exactly one message.
                    oversized_frame = False
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        if not line:
                            continue
                        if not self._decode_and_queue_frame(line, client):
                            oversized_frame = True
                            break

                    if oversized_frame:
                        break

                    if len(buffer) > self._MAX_MESSAGE_BYTES:
                        print(
                            f"Client sent an oversized message without a terminator "
                            f"({len(buffer)} bytes) - disconnecting"
                        )
                        break
                except TimeoutError:
                    # Expected; loop round and re-check self.running.
                    continue
                except BlockingIOError:
                    # "Nothing to read *right now*", which is not a dead peer.
                    # `BlockingIOError` is `OSError` -> `Exception` and **not** a
                    # subclass of `TimeoutError`, so before this branch existed
                    # it reached the `break` below and closed a healthy
                    # connection. `_PAST_BUDGET_SEND_TIMEOUT_SECONDS` is what
                    # makes it unreachable from this server's own code; this
                    # branch is what stops it being fatal if anything else ever
                    # hands the socket back non-blocking. It cannot become a
                    # busy loop while that floor holds, because a socket in
                    # timeout mode raises `TimeoutError`, never this.
                    continue
                except Exception as e:
                    print(f"Error receiving data: {e!s}")
                    break
        except Exception as e:
            print(f"Error in client handler: {e!s}")
        finally:
            with self._clients_lock:
                self._clients.pop(client, None)
            with suppress(Exception):
                client.close()
            print("Client handler stopped")

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
            print(f"Error executing command: {e!s}")
            traceback.print_exc()
            return {"status": "error", "message": str(e)}

    def _build_command_handlers(self):
        """
        Build the cmd_type -> handler map, including conditionally-enabled providers.

        Shared by execute_command_internal (dispatch) and get_addon_info
        (advertised capabilities), so the two can never drift apart.

        Returns:
            Result produced by the operation.

        """
        # Base handlers that are always available
        handlers = {
            "list_scene_objects": self.list_scene_objects,
            "get_addon_info": self.get_addon_info,
            "get_session_info": self.get_session_info,
            "get_object_info": self.get_object_info,
            "get_mesh_data": self.get_mesh_data,
            "inspect_animation": self.inspect_animation,
            "manage_animation_action": self.manage_animation_action,
            "edit_keyframes": self.edit_keyframes,
            "bake_evaluated_animation": self.bake_evaluated_animation,
            "manage_nla_tracks": self.manage_nla_tracks,
            "manage_animation_driver": self.manage_animation_driver,
            "list_procedural_systems": self.list_procedural_systems,
            "get_geometry_node_graph": self.get_geometry_node_graph,
            "get_geometry_node_type_info": self.get_geometry_node_type_info,
            "create_geometry_node_group": self.create_geometry_node_group,
            "attach_geometry_nodes_modifier": self.attach_geometry_nodes_modifier,
            "edit_node_group_interface": self.edit_node_group_interface,
            "patch_geometry_node_graph": self.patch_geometry_node_graph,
            "set_geometry_nodes_inputs": self.set_geometry_nodes_inputs,
            "manage_geometry_nodes_modifier": self.manage_geometry_nodes_modifier,
            "copy_geometry_node_group": self.copy_geometry_node_group,
            "evaluate_procedural_geometry": self.evaluate_procedural_geometry,
            "validate_geometry_node_graph": self.validate_geometry_node_graph,
            "create_procedural_scatter": self.create_procedural_scatter,
            "create_curve_generator": self.create_curve_generator,
            "create_procedural_array": self.create_procedural_array,
            "create_surface_paneling": self.create_surface_paneling,
            "create_procedural_boolean": self.create_procedural_boolean,
            "create_procedural_deformer": self.create_procedural_deformer,
            "create_volume_generator": self.create_volume_generator,
            "manage_named_attributes": self.manage_named_attributes,
            "manage_procedural_instances": self.manage_procedural_instances,
            "run_geometry_nodes_tool": self.run_geometry_nodes_tool,
            "publish_procedural_asset": self.publish_procedural_asset,
            "create_repeat_zone": self.create_repeat_zone,
            "create_simulation_zone": self.create_simulation_zone,
            "manage_geometry_nodes_bake": self.manage_geometry_nodes_bake,
            "realize_procedural_output": self.realize_procedural_output,
            "analyze_procedural_performance": self.analyze_procedural_performance,
            "get_viewport_screenshot": self.get_viewport_screenshot,
            "create_geometry_object": self.create_geometry_object,
            "set_object_transform": self.set_object_transform,
            "duplicate_or_instance_objects": self.duplicate_or_instance_objects,
            "manage_scene_collections": self.manage_scene_collections,
            "manage_object_hierarchy": self.manage_object_hierarchy,
            "manage_object_constraints": self.manage_object_constraints,
            "manage_modifiers": self.manage_modifiers,
            "remove_scene_objects": self.remove_scene_objects,
            "reset_scene": self.reset_scene,
            "validate_scene": self.validate_scene,
            "inspect_render_setup": self.inspect_render_setup,
            "configure_render_settings": self.configure_render_settings,
            "get_scene_physics_info": self.get_scene_physics_info,
            "configure_scene_physics": self.configure_scene_physics,
            "keyframe_object_transform": self.keyframe_object_transform,
            "manage_view_layers": self.manage_view_layers,
            "render_scene": self.render_scene,
            "inspect_render_output": self.inspect_render_output,
            "get_polyhaven_status": self.get_polyhaven_status,
            "get_sketchfab_status": self.get_sketchfab_status,
            "create_primitive": self.create_primitive,
            "mesh_extrude": self.mesh_extrude,
            "mesh_inset": self.mesh_inset,
            "mesh_bevel": self.mesh_bevel,
            "mesh_bridge": self.mesh_bridge,
            "mesh_boolean": self.mesh_boolean,
            "mesh_subdivide": self.mesh_subdivide,
            "mesh_remesh": self.mesh_remesh,
            "mesh_solidify": self.mesh_solidify,
            "mesh_symmetrize": self.mesh_symmetrize,
            "create_retopology_target": self.create_retopology_target,
            "inspect_retopology": self.inspect_retopology,
            "analyze_surface_conformity": self.analyze_surface_conformity,
            "manage_retopology_checkpoint": self.manage_retopology_checkpoint,
            "configure_surface_projection": self.configure_surface_projection,
            "project_mesh_elements": self.project_mesh_elements,
            "build_quad_patch": self.build_quad_patch,
            "extend_boundary": self.extend_boundary,
            "fill_boundary_quads": self.fill_boundary_quads,
            "reroute_topology": self.reroute_topology,
            "relax_topology": self.relax_topology,
            "redistribute_edge_loop": self.redistribute_edge_loop,
            "configure_retopology_symmetry": self.configure_retopology_symmetry,
            "validate_retopology": self.validate_retopology,
            "create_retopology_guides": self.create_retopology_guides,
            "create_surface_section": self.create_surface_section,
            "set_retopology_features": self.set_retopology_features,
            "add_support_loops": self.add_support_loops,
            "transfer_mesh_attributes": self.transfer_mesh_attributes,
            "unwrap_retopology_uvs": self.unwrap_retopology_uvs,
            "create_bake_cage": self.create_bake_cage,
            "bake_retopology_maps": self.bake_retopology_maps,
            "test_deformation": self.test_deformation,
            "generate_quadriflow_draft": self.generate_quadriflow_draft,
            "fit_surface_primitive": self.fit_surface_primitive,
            "bind_surface_deformation": self.bind_surface_deformation,
            "generate_retopology_lods": self.generate_retopology_lods,
            "copy_object_transform": self.copy_object_transform,
            "add_radial_array_modifier": self.add_radial_array_modifier,
            "set_viewport_overlay": self.set_viewport_overlay,
            "clear_materials": self.clear_materials,
            "clear_vertex_groups": self.clear_vertex_groups,
            "clear_edge_marks": self.clear_edge_marks,
            "sync_data_name": self.sync_data_name,
            "get_character_rig_info": self.get_character_rig_info,
            "get_skinning_info": self.get_skinning_info,
            "create_armature": self.create_armature,
            "patch_armature_bones": self.patch_armature_bones,
            "mirror_armature_bones": self.mirror_armature_bones,
            "manage_bone_collections": self.manage_bone_collections,
            "configure_armature_bones": self.configure_armature_bones,
            "bind_mesh_to_armature": self.bind_mesh_to_armature,
            "set_skin_weights": self.set_skin_weights,
            "clean_skin_weights": self.clean_skin_weights,
            "add_pose_bone_constraint": self.add_pose_bone_constraint,
            "validate_character_rig": self.validate_character_rig,
            "transfer_skin_weights": self.transfer_skin_weights,
            "create_ik_chain": self.create_ik_chain,
            "create_ik_fk_limb": self.create_ik_fk_limb,
            "create_spline_ik_rig": self.create_spline_ik_rig,
            "configure_bendy_bones": self.configure_bendy_bones,
            "create_rig_property_driver": self.create_rig_property_driver,
            "assign_bone_custom_shapes": self.assign_bone_custom_shapes,
            "set_character_pose": self.set_character_pose,
            "keyframe_character_pose": self.keyframe_character_pose,
            "create_shape_key_controls": self.create_shape_key_controls,
            "get_rigid_body_scene_info": self.get_rigid_body_scene_info,
            "get_rigid_body_object_info": self.get_rigid_body_object_info,
            "get_rigid_body_constraint_info": self.get_rigid_body_constraint_info,
            "configure_rigid_body_world": self.configure_rigid_body_world,
            "add_rigid_bodies": self.add_rigid_bodies,
            "configure_rigid_bodies": self.configure_rigid_bodies,
            "set_rigid_body_mass": self.set_rigid_body_mass,
            "set_rigid_body_collision_layers": self.set_rigid_body_collision_layers,
            "create_rigid_body_collision_proxy": self.create_rigid_body_collision_proxy,
            "create_rigid_body_constraint": self.create_rigid_body_constraint,
            "configure_rigid_body_constraint": self.configure_rigid_body_constraint,
            "validate_rigid_body_setup": self.validate_rigid_body_setup,
            "remove_rigid_body_components": self.remove_rigid_body_components,
            "animate_rigid_body_release": self.animate_rigid_body_release,
            "create_compound_rigid_body": self.create_compound_rigid_body,
            "create_rigid_body_constraint_network": self.create_rigid_body_constraint_network,
            "prepare_fracture_rigid_bodies": self.prepare_fracture_rigid_bodies,
            "create_rigid_body_chain": self.create_rigid_body_chain,
            "setup_animated_passive_collider": self.setup_animated_passive_collider,
            "configure_rigid_body_force_fields": self.configure_rigid_body_force_fields,
            "sample_rigid_body_simulation": self.sample_rigid_body_simulation,
            "manage_rigid_body_cache": self.manage_rigid_body_cache,
            "bake_rigid_bodies_to_keyframes": self.bake_rigid_bodies_to_keyframes,
            "create_rigid_body_debris_field": self.create_rigid_body_debris_field,
            "create_rigid_body_proxy_rig": self.create_rigid_body_proxy_rig,
            "create_ragdoll_rig": self.create_ragdoll_rig,
            "bake_ragdoll_to_armature": self.bake_ragdoll_to_armature,
            "export_rigid_body_animation": self.export_rigid_body_animation,
            "analyze_rigid_body_performance": self.analyze_rigid_body_performance,
            "get_cloth_simulation_info": self.get_cloth_simulation_info,
            "get_cloth_object_info": self.get_cloth_object_info,
            "get_liquid_simulation_info": self.get_liquid_simulation_info,
            "get_fluid_object_info": self.get_fluid_object_info,
            "inspect_fluid_simulation": self.inspect_fluid_simulation,
            "create_fluid_domain": self.create_fluid_domain,
            "configure_fluid_solver": self.configure_fluid_solver,
            "add_fluid_flow": self.add_fluid_flow,
            "add_fluid_effector": self.add_fluid_effector,
            "manage_fluid_cache": self.manage_fluid_cache,
            "get_camera_rig_info": self.get_camera_rig_info,
            "create_camera": self.create_camera,
            "configure_camera": self.configure_camera,
            "set_scene_camera": self.set_scene_camera,
            "point_camera_at": self.point_camera_at,
            "create_camera_target": self.create_camera_target,
            "frame_camera_on_objects": self.frame_camera_on_objects,
            "create_orbit_camera_rig": self.create_orbit_camera_rig,
            "create_dolly_camera_rig": self.create_dolly_camera_rig,
            "create_crane_camera_rig": self.create_crane_camera_rig,
            "create_camera_path_rig": self.create_camera_path_rig,
            "configure_camera_dof": self.configure_camera_dof,
            "keyframe_camera_rig": self.keyframe_camera_rig,
            "set_camera_interpolation": self.set_camera_interpolation,
            "create_focus_pull": self.create_focus_pull,
            "create_dolly_zoom": self.create_dolly_zoom,
            "add_camera_shake": self.add_camera_shake,
            "create_camera_markers": self.create_camera_markers,
            "match_camera_transform": self.match_camera_transform,
            "duplicate_camera_rig": self.duplicate_camera_rig,
            "add_camera_constraint": self.add_camera_constraint,
            "configure_camera_render_gate": self.configure_camera_render_gate,
            "validate_camera_rig": self.validate_camera_rig,
            "list_lights": self.list_lights,
            "inspect_light": self.inspect_light,
            "inspect_lighting_setup": self.inspect_lighting_setup,
            "validate_lighting_setup": self.validate_lighting_setup,
            "create_light": self.create_light,
            "configure_light": self.configure_light,
            "aim_light": self.aim_light,
            "configure_light_linking": self.configure_light_linking,
            "create_studio_lighting": self.create_studio_lighting,
            "configure_world_background": self.configure_world_background,
            "configure_hdri_environment": self.configure_hdri_environment,
            "configure_procedural_sky": self.configure_procedural_sky,
            "configure_lighting_quality": self.configure_lighting_quality,
            "configure_color_management": self.configure_color_management,
            "render_lighting_preview": self.render_lighting_preview,
            "list_materials": self.list_materials,
            "inspect_material": self.inspect_material,
            "get_shader_node_type_info": self.get_shader_node_type_info,
            "patch_shader_graph": self.patch_shader_graph,
            "create_pbr_material": self.create_pbr_material,
            "configure_pbr_material": self.configure_pbr_material,
            "assign_material": self.assign_material,
            "configure_texture_mapping": self.configure_texture_mapping,
            "list_texture_images": self.list_texture_images,
            "load_texture_image": self.load_texture_image,
            "configure_texture_image": self.configure_texture_image,
            "apply_pbr_texture_set": self.apply_pbr_texture_set,
            "save_texture_image": self.save_texture_image,
            "render_pbr_material_preview": self.render_pbr_material_preview,
            "manage_uv_maps": self.manage_uv_maps,
            "set_uv_seams": self.set_uv_seams,
            "unwrap_uvs": self.unwrap_uvs,
            "optimize_uv_layout": self.optimize_uv_layout,
            "inspect_uv_layout": self.inspect_uv_layout,
            "bake_texture_map": self.bake_texture_map,
            "validate_pbr_asset": self.validate_pbr_asset,
            "add_cloth_simulation": self.add_cloth_simulation,
            "configure_cloth_material": self.configure_cloth_material,
            "configure_cloth_solver": self.configure_cloth_solver,
            "set_cloth_vertex_weights": self.set_cloth_vertex_weights,
            "configure_cloth_pinning": self.configure_cloth_pinning,
            "configure_cloth_collisions": self.configure_cloth_collisions,
            "add_cloth_collider": self.add_cloth_collider,
            "configure_cloth_collider": self.configure_cloth_collider,
            "estimate_cloth_resources": self.estimate_cloth_resources,
            "validate_cloth_setup": self.validate_cloth_setup,
            "configure_cloth_sewing": self.configure_cloth_sewing,
            "configure_cloth_pressure": self.configure_cloth_pressure,
            "configure_cloth_internal_springs": self.configure_cloth_internal_springs,
            "configure_cloth_rest_shape": self.configure_cloth_rest_shape,
            "configure_cloth_field_weights": self.configure_cloth_field_weights,
            "animate_cloth_parameters": self.animate_cloth_parameters,
            "create_cloth_attachment": self.create_cloth_attachment,
            "create_character_cloth_setup": self.create_character_cloth_setup,
            "sample_cloth_simulation": self.sample_cloth_simulation,
            "manage_cloth_cache": self.manage_cloth_cache,
            "remove_cloth_components": self.remove_cloth_components,
            "create_cloth_proxy_rig": self.create_cloth_proxy_rig,
            "duplicate_cloth_setup_variant": self.duplicate_cloth_setup_variant,
            "prepare_cloth_render_surface": self.prepare_cloth_render_surface,
            "export_cloth_simulation": self.export_cloth_simulation,
            "analyze_cloth_performance": self.analyze_cloth_performance,
            "create_liquid_domain": self.create_liquid_domain,
            "fit_liquid_domain": self.fit_liquid_domain,
            "configure_liquid_solver": self.configure_liquid_solver,
            "add_liquid_flow": self.add_liquid_flow,
            "configure_liquid_flow": self.configure_liquid_flow,
            "add_liquid_effector": self.add_liquid_effector,
            "configure_liquid_effector": self.configure_liquid_effector,
            "configure_liquid_scope_and_boundaries": self.configure_liquid_scope_and_boundaries,
            "estimate_liquid_resources": self.estimate_liquid_resources,
            "validate_liquid_setup": self.validate_liquid_setup,
            "configure_liquid_mesh": self.configure_liquid_mesh,
            "apply_liquid_quality_profile": self.apply_liquid_quality_profile,
            "configure_liquid_secondary_particles": self.configure_liquid_secondary_particles,
            "configure_liquid_diffusion": self.configure_liquid_diffusion,
            "animate_liquid_flow": self.animate_liquid_flow,
            "create_liquid_guide": self.create_liquid_guide,
            "configure_liquid_force_fields": self.configure_liquid_force_fields,
            "create_liquid_material": self.create_liquid_material,
            "create_secondary_particle_render_setup": self.create_secondary_particle_render_setup,
            "sample_liquid_simulation": self.sample_liquid_simulation,
            "manage_liquid_cache": self.manage_liquid_cache,
            "remove_fluid_components": self.remove_fluid_components,
            "create_liquid_proxy_rig": self.create_liquid_proxy_rig,
            "duplicate_liquid_setup_variant": self.duplicate_liquid_setup_variant,
            "prepare_liquid_render_mesh": self.prepare_liquid_render_mesh,
            "export_liquid_simulation": self.export_liquid_simulation,
            "analyze_liquid_performance": self.analyze_liquid_performance,
            "setup_liquid_shot": self.setup_liquid_shot,
            "validate_liquid_result": self.validate_liquid_result,
        }

        # Add Polyhaven handlers only if enabled
        if bpy.context.scene.blendermcp_use_polyhaven:
            polyhaven_handlers = {
                "get_polyhaven_categories": self.get_polyhaven_categories,
                "list_polyhaven_assets": self.list_polyhaven_assets,
                "import_polyhaven_asset": self.import_polyhaven_asset,
                "apply_polyhaven_texture": self.apply_polyhaven_texture,
            }
            handlers.update(polyhaven_handlers)

        # Add Sketchfab handlers only if enabled
        if bpy.context.scene.blendermcp_use_sketchfab:
            sketchfab_handlers = {
                "search_sketchfab_models": self.search_sketchfab_models,
                "get_sketchfab_model_preview": self.get_sketchfab_model_preview,
                "import_sketchfab_model": self.import_sketchfab_model,
            }
            handlers.update(sketchfab_handlers)

        # Add ND (HugeMenace) handlers only if enabled
        if bpy.context.scene.blendermcp_use_nd:
            nd_handlers = {
                "nd_boolean": self.nd_boolean,
                "nd_mark_as_util": self.nd_mark_as_util,
                "nd_clean_utils": self.nd_clean_utils,
                "nd_create_id_material": self.nd_create_id_material,
                "nd_bulk_create_id_materials": self.nd_bulk_create_id_materials,
                "nd_set_lod_suffix": self.nd_set_lod_suffix,
                "nd_single_vertex": self.nd_single_vertex,
                "nd_apply_modifiers": self.nd_apply_modifiers,
                "nd_pulse_viewport_toggle": self.nd_pulse_viewport_toggle,
                "nd_capture_utils": self.nd_capture_utils,
            }
            handlers.update(nd_handlers)

        return handlers

    # Commands that never mutate bpy.data. Everything else gets wrapped in
    # mutation_transaction() - snapshotting/diffing/rolling back these would
    # just be pointless overhead and undo-stack noise.
    _READ_ONLY_COMMANDS = frozenset(
        {
            "list_scene_objects",
            "get_addon_info",
            "get_session_info",
            "get_object_info",
            "get_mesh_data",
            "inspect_animation",
            "list_procedural_systems",
            "get_geometry_node_graph",
            "get_geometry_node_type_info",
            "evaluate_procedural_geometry",
            "validate_geometry_node_graph",
            "analyze_procedural_performance",
            "get_viewport_screenshot",
            "get_polyhaven_status",
            "get_sketchfab_status",
            "get_nd_status",
            "get_polyhaven_categories",
            "list_polyhaven_assets",
            "search_sketchfab_models",
            "get_sketchfab_model_preview",
            "get_cloth_simulation_info",
            "get_cloth_object_info",
            "get_camera_rig_info",
            "get_character_rig_info",
            "get_skinning_info",
            "validate_character_rig",
            "get_rigid_body_scene_info",
            "get_rigid_body_object_info",
            "get_rigid_body_constraint_info",
            "validate_rigid_body_setup",
            "validate_camera_rig",
            "list_lights",
            "inspect_light",
            "inspect_lighting_setup",
            "validate_lighting_setup",
            "list_materials",
            "inspect_material",
            "get_shader_node_type_info",
            "list_texture_images",
            "inspect_uv_layout",
            "validate_pbr_asset",
            "estimate_cloth_resources",
            "validate_cloth_setup",
            "get_liquid_simulation_info",
            "get_fluid_object_info",
            "inspect_fluid_simulation",
            "estimate_liquid_resources",
            "validate_liquid_setup",
            "inspect_retopology",
            "validate_retopology",
            "test_deformation",
            "inspect_render_setup",
            "inspect_render_output",
            "get_scene_physics_info",
            "validate_scene",
        }
    )

    # Commands that replace Blender's entire database. Two consequences, both
    # enforced here: drain_command_queue makes such a command the last one its
    # tick runs (see _run_session_swap), and _run_handler keeps it out of
    # mutation_transaction.
    #
    # Task 6 implements these; the constant lands here because Task 3's barrier
    # and Task 4's transaction invalidation have to agree on one definition of
    # "the session was swapped", and two copies would eventually disagree.
    #
    # **save_shot is deliberately absent.** `wm.save_as_mainfile` moves
    # `bpy.data.filepath` but replaces no datablock: every id a queued command
    # named still exists with the same session_uid, so the batch behind a save
    # remains safe to run, and the epoch does not move for a save either (a save
    # changes no capability). Including it would discard a whole batch every
    # time a client checkpointed its work. Task 2's decision 11 - synchronous
    # validate-then-swap for `open_shot` - requires nothing of `save_shot`.
    #
    # `_DATABLOCK_REPLACING_COMMANDS` below is a *second, separate* constant
    # (`reload_library` / `relocate_library` / `unlink_libraries`), which also
    # bypasses the transaction but does **not** trip this barrier: those replace
    # linked content in place, they do not swap the session. Do not merge them.
    _SESSION_SWAP_COMMANDS = frozenset({"open_shot", "reset_session"})

    # Commands that replace or free linked datablocks in place: `lib.reload()`
    # gave all 4 datablocks linked from the probe's library fresh session_uids, and
    # `libraries.remove` / `orphans_purge` free them. Measured on 5.2.2 by
    # `scripts/blender_probes/library_replace_handlers.py`. A transaction wrapped
    # around them would read the reloaded contents as created by the request and
    # remove them on any later raise. They bypass the transaction in
    # `_run_handler`, but they are not session swaps (no barrier, no epoch) and
    # not read-only. `link_canon_library` is deliberately absent: its new
    # datablocks are the request's own and a failed link must roll them back.
    _DATABLOCK_REPLACING_COMMANDS = frozenset({"reload_library", "relocate_library", "unlink_libraries"})

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

        # Trivial liveness check. Touches no bpy data, so a successful ping
        # alongside a failing command isolates data access from transport.
        if cmd_type == "ping":
            return {"status": "success", "result": {"pong": True}}

        # Add a handler for checking PolyHaven status
        if cmd_type == "get_polyhaven_status":
            return {"status": "success", "result": self.get_polyhaven_status()}

        # Add a handler for checking ND status
        if cmd_type == "get_nd_status":
            return {"status": "success", "result": self.get_nd_status()}

        handlers = self._build_command_handlers()

        handler = handlers.get(cmd_type)
        if handler:
            try:
                print(f"Executing handler for {cmd_type}")
                result = self._run_handler(cmd_type, handler, params)
                print("Handler execution complete")
                return {"status": "success", "result": result}
            except Exception as e:
                print(f"Error in handler: {e!s}")
                traceback.print_exc()
                return {"status": "error", "message": str(e)}
        else:
            return {"status": "error", "message": f"Unknown command type: {cmd_type}"}

    # Params that name an *existing* object a mutating command touches, so the
    # transaction can capture that object's state and restore it on failure.
    # "name" is excluded: in create_primitive it names a new object, not one to
    # protect.
    _TARGET_NAME_PARAMS = (
        "object_name",
        "camera_name",
        "light_name",
        "curve_object_name",
        "cutter_object_name",
        "reference_object_name",
        "target_object_name",
        "cloth_object_name",
        "garment_object_name",
        "armature_object_name",
        "mesh_object_name",
        "source_mesh_name",
        "target_mesh_name",
        "constraint_object_name",
        "object1_name",
        "object2_name",
        "low_resolution_source_name",
        "render_object_name",
        "proxy_object_name",
        "source_object_name",
        "destination_name",
        "movement_object_name",
        "owner_name",
        "source_root_name",
        "root_object_name",
        "domain_object_name",
        "guide_object_name",
        "guide_parent_domain_object_name",
        "instance_object_name",
    )
    _TARGET_NAMES_PARAMS = (
        "object_names",
        "camera_names",
        "body_collider_object_names",
        "source_object_names",
        "collider_object_names",
        "mesh_object_names",
        "armature_object_names",
        "body_names",
        "child_object_names",
        "piece_object_names",
    )

    # Commands that edit an existing object's mesh geometry. Only these back up
    # the mesh datablock (a full copy) so a failed edit can be swapped back;
    # transform-only commands (e.g. copy_object_transform) skip that cost.
    _GEOMETRY_MUTATING_COMMANDS = frozenset(
        {
            "mesh_extrude",
            "mesh_inset",
            "mesh_bevel",
            "mesh_bridge",
            "mesh_boolean",
            "mesh_subdivide",
            "mesh_remesh",
            "mesh_solidify",
            "mesh_symmetrize",
            "manage_named_attributes",
            "run_geometry_nodes_tool",
            "analyze_surface_conformity",
            "manage_retopology_checkpoint",
            "configure_surface_projection",
            "project_mesh_elements",
            "build_quad_patch",
            "extend_boundary",
            "fill_boundary_quads",
            "reroute_topology",
            "relax_topology",
            "redistribute_edge_loop",
            "set_retopology_features",
            "add_support_loops",
            "transfer_mesh_attributes",
            "unwrap_retopology_uvs",
            "configure_cloth_sewing",
            "fit_liquid_domain",
        }
    )

    def _resolve_targets(self, params):
        """
        Resolve the existing objects a mutating request will touch, from its params.

        Missing objects are skipped (the handler will raise its own clear error);
        duplicates are collapsed while preserving order.

        Args:
            params: The command's params dict.

        Returns:
            list: Existing bpy objects named by the target params.

        """
        names = []
        for key in self._TARGET_NAME_PARAMS:
            value = params.get(key)
            if isinstance(value, str):
                names.append(value)
        for key in self._TARGET_NAMES_PARAMS:
            value = params.get(key)
            if isinstance(value, (list, tuple)):
                names.extend(name for name in value if isinstance(name, str))
        for record in params.get("targets", ()):
            if isinstance(record, dict) and isinstance(record.get("object_name"), str):
                names.append(record["object_name"])
        for record in params.get("fields", ()):
            if isinstance(record, dict) and isinstance(record.get("object_name"), str):
                names.append(record["object_name"])
        for record in params.get("keyframes", ()):
            if isinstance(record, dict) and isinstance(record.get("object_name"), str):
                names.append(record["object_name"])
        for record in params.get("assignments", ()):
            if not isinstance(record, dict):
                continue
            for name_key in ("child_object_name", "parent_object_name"):
                if isinstance(record.get(name_key), str):
                    names.append(record[name_key])
        constraint = params.get("constraint")
        if isinstance(constraint, dict) and isinstance(constraint.get("target_object_name"), str):
            names.append(constraint["target_object_name"])
        for record_key in ("sources", "mappings", "bodies"):
            for record in params.get(record_key, ()):
                if not isinstance(record, dict):
                    continue
                for name_key in (
                    "object_name",
                    "render_object_name",
                    "proxy_object_name",
                    "low_resolution_source_name",
                    "convex_source_object_name",
                ):
                    if isinstance(record.get(name_key), str):
                        names.append(record[name_key])

        objects = []
        seen = set()
        for name in names:
            if name in seen:
                continue
            seen.add(name)
            obj = bpy.data.objects.get(name)
            if obj is not None:
                objects.append(obj)
        return objects

    def _run_handler(self, cmd_type, handler, params):
        """
        Call a resolved handler, wrapping mutating commands in mutation_transaction.

        A handler that reports failure by returning a failure shape (rather than
        raising) is converted to a HandlerReportedError inside the transaction,
        so its partial mutation rolls back instead of being committed. On
        success, any undo-unavailability warning is merged into the result.

        Args:
            cmd_type: The MCP command type, used to pick read-only vs. mutating dispatch.
            handler: The bound handler method to call.
            params: Keyword arguments to call the handler with.

        Returns:
            Result produced by the handler.

        """
        dynamic_read_only = (
            cmd_type == "manage_retopology_checkpoint" and str(params.get("action", "")).upper() in {"LIST", "COMPARE"}
        ) or (cmd_type == "analyze_surface_conformity" and not params.get("create_heat_map", False))
        dynamic_read_only = dynamic_read_only or (cmd_type == "configure_cloth_sewing" and params.get("dry_run", True))
        dynamic_read_only = dynamic_read_only or (
            cmd_type == "manage_cloth_cache" and str(params.get("action", "INSPECT")).upper() == "INSPECT"
        )
        dynamic_read_only = dynamic_read_only or (
            cmd_type == "manage_liquid_cache" and str(params.get("action", "STATUS")).upper() == "STATUS"
        )
        dynamic_read_only = dynamic_read_only or (
            cmd_type == "analyze_liquid_performance" and not params.get("measure_replay_evaluation", False)
        )
        dynamic_read_only = dynamic_read_only or (
            cmd_type == "create_camera_markers" and str(params.get("action", "")).upper() == "LIST"
        )
        dynamic_read_only = dynamic_read_only or (
            cmd_type == "manage_rigid_body_cache" and str(params.get("action", "INSPECT")).upper() == "INSPECT"
        )
        dynamic_read_only = dynamic_read_only or (
            cmd_type == "analyze_rigid_body_performance" and not params.get("sample_frames")
        )
        dynamic_read_only = dynamic_read_only or (
            cmd_type == "manage_named_attributes" and str(params.get("action", "LIST")).upper() == "LIST"
        )
        dynamic_read_only = dynamic_read_only or (
            cmd_type == "manage_geometry_nodes_bake" and str(params.get("action", "INSPECT")).upper() == "INSPECT"
        )
        dynamic_read_only = dynamic_read_only or (
            cmd_type == "manage_procedural_instances"
            and not any(
                params.get(key) is not None
                for key in (
                    "source_type",
                    "source_name",
                    "pick_instance",
                    "rotation",
                    "scale",
                    "translation",
                    "realize_instances",
                )
            )
        )
        non_undo_commands = {"set_viewport_overlay", "nd_pulse_viewport_toggle", "nd_capture_utils", "render_scene"}
        # _SESSION_SWAP_COMMANDS are not read-only - they mutate more than any
        # other command does. They bypass the transaction because it cannot
        # describe them: Transaction.begin() snapshots the session_uids of the
        # *pre-load* database, so after a swap every id in the new file is
        # "new" and a rollback would enumerate the whole file and remove it.
        # This line is what *makes* the bypass happen; `session._on_load_post`
        # invalidating an open transaction covers a load made as a side effect
        # of any other command. _DATABLOCK_REPLACING_COMMANDS bypass for the
        # same reason (see the constant); `unlink_libraries` fires no handler,
        # so for it this routing is the only protection.
        bypasses_transaction = (
            cmd_type in self._READ_ONLY_COMMANDS
            or dynamic_read_only
            or cmd_type in non_undo_commands
            or cmd_type in self._SESSION_SWAP_COMMANDS
            or cmd_type in self._DATABLOCK_REPLACING_COMMANDS
        )
        if bypasses_transaction:
            return handler(**params)

        targets = self._resolve_targets(params)
        capture_geometry = cmd_type in self._GEOMETRY_MUTATING_COMMANDS
        with mutation_transaction(cmd_type, targets, capture_geometry) as txn:
            result = handler(**params)
            if isinstance(result, dict) and result.get("cancelled"):
                txn.finish_without_checkpoint()
                return result
            failure = _handler_failure_message(result)
            if failure is not None:
                raise HandlerReportedError(failure)
            warning = txn.commit()
            if warning and isinstance(result, dict):
                result = {**result, "warnings": [*result.get("warnings", []), warning]}
            return result

    def get_addon_info(self):
        """
        Version/capability handshake for the MCP server (and install tooling).

        `capabilities` is scene-gated - the Poly Haven / Sketchfab / ND handlers
        are advertised only when this .blend's `blendermcp_use_*` flags say so -
        so it follows the file. `session_epoch` is what lets a client notice: it
        moves once per completed database swap and never for a save or a failed
        load, so a client that sees the same number knows its cached capability
        set is still the right one. `session_indeterminate` is the other half of
        that story: while it is true the drain loop is refusing everything but
        this call, `get_session_info` and the swap commands, and a client that
        cannot see it has no way to tell those refusals from a broken addon.

        Returns:
            Result produced by the operation.

        """
        session = session_snapshot()
        return {
            "name": bl_info.get("name", "Blender MCP"),
            "addon_version": list(bl_info.get("version", (0, 0))),
            "protocol_version": ADDON_PROTOCOL_VERSION,
            "capabilities": sorted({"ping", "get_polyhaven_status", "get_nd_status", *self._build_command_handlers()}),
            "blender_version": bpy.app.version_string,
            "writable_output_roots": self._writable_output_roots(),
            # The pair, not the counter alone: module state is rebuilt at epoch
            # 0 by a Blender restart or Reload Scripts, so a client comparing
            # only the number can see a value it has already seen against an
            # entirely different database. See `session.SESSION_ID`.
            "session_id": session["session_id"],
            "session_epoch": session["session_epoch"],
            "current_filepath": session["current_filepath"],
            # Published here as well as on `get_session_info`, because this is
            # the surface a client re-handshakes against and it must be able to
            # learn *why* its commands are being refused from the one call the
            # barrier still lets through.
            "session_indeterminate": session["session_indeterminate"],
        }

    @staticmethod
    def _writable_output_roots() -> list[str]:
        """
        List directories this Blender process can write renders and exports to.

        The MCP server cannot work this out for itself once the two no longer
        share a filesystem, so it is reported here. Deployment-specific roots
        come first (see BLENDERMCP_OUTPUT_ROOTS), then this process's own
        defaults.

        This list is an **advisory preference ranking, not an enforced root
        set**. Nothing here validates a later write against it. Three caveats a
        containment boundary must not be built on without deciding them first:

        - `bpy.app.tempdir` is session-scoped and Blender deletes it on exit,
          yet it ranks *first* whenever no .blend is open and no roots are
          configured. An agent that writes a render to the first offered root
          loses it silently when Blender quits.
        - `writable_roots` normalizes with `abspath`, not `realpath`, so a
          symlinked root is reported under one name and would be enforced under
          another.
        - The default candidate set ends at `~`, so deriving enforced roots from
          these defaults on an unconfigured desktop install makes the whole home
          directory the boundary.

        Building the candidate list touches no filesystem; the probing of it is
        memoized by `_probe_writable_roots`, which is what keeps a handshake
        off Blender's main thread I/O path.

        Returns:
            list[str]: Absolute, writable directories, most preferred first.
            A fresh list each call, so a caller editing the handshake response
            cannot corrupt the memoized answer.

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

    _SCENE_INFO_MAX_LIMIT = 200

    def list_scene_objects(self, limit=25, offset=0):
        """
        Get information about the current Blender scene, paginated over its objects.

        Args:
            limit: Maximum number of items to return.
            offset: Zero-based starting position.

        Returns:
            Result produced by the operation.

        """
        try:
            print("Getting scene info...")
            scene_objects = sorted(bpy.context.scene.objects, key=lambda item: item.name.casefold())
            total = len(scene_objects)
            start, end, truncated, next_offset = paginate(total, offset, limit, self._SCENE_INFO_MAX_LIMIT)

            objects = []
            for obj in scene_objects[start:end]:
                objects.append(
                    {
                        "name": obj.name,
                        "type": obj.type,
                        # Only include basic location data
                        "location": [
                            float(obj.location.x),
                            float(obj.location.y),
                            float(obj.location.z),
                        ],
                        "parent": obj.parent.name if obj.parent else None,
                        "collections": sorted(collection.name for collection in obj.users_collection),
                        "selected": bool(obj.select_get()),
                        "visible": bool(obj.visible_get()),
                        "hide_viewport": bool(obj.hide_viewport),
                        "hide_render": bool(obj.hide_render),
                    }
                )

            scene_info = {
                "name": bpy.context.scene.name,
                "object_count": total,
                "objects": objects,
                "materials_count": len(bpy.data.materials),
                "active_object": getattr(getattr(bpy.context, "view_layer", None), "objects", None).active.name
                if getattr(getattr(getattr(bpy.context, "view_layer", None), "objects", None), "active", None)
                else None,
                "selected_objects": sorted(obj.name for obj in getattr(bpy.context, "selected_objects", ())),
                "mode": getattr(bpy.context, "mode", "UNKNOWN"),
                "unit_settings": {
                    "system": bpy.context.scene.unit_settings.system,
                    "scale_length": bpy.context.scene.unit_settings.scale_length,
                    "length_unit": bpy.context.scene.unit_settings.length_unit,
                },
                "offset": start,
                "limit": limit,
                "returned_count": len(objects),
                "truncated": truncated,
                "next_offset": next_offset,
            }

            print(f"Scene info collected: {len(objects)} of {total} objects")
            return scene_info
        except Exception as e:
            print(f"Error in list_scene_objects: {e!s}")
            traceback.print_exc()
            return {"error": str(e)}

    @staticmethod
    def get_aabb(obj):
        """
        Returns the world-space axis-aligned bounding box (AABB) of an object.

        Args:
            obj: Value for obj.

        Returns:
            the world-space axis-aligned bounding box (AABB) of an object.

        Raises:
            TypeError: If the operation cannot be completed.

        """
        if obj.type != "MESH":
            raise TypeError("Object must be a mesh")

        # Get the bounding box corners in local space
        local_bbox_corners = [mathutils.Vector(corner) for corner in obj.bound_box]

        # Convert to world coordinates
        world_bbox_corners = [obj.matrix_world @ corner for corner in local_bbox_corners]

        # Compute axis-aligned min/max coordinates
        min_corner = mathutils.Vector(map(min, zip(*world_bbox_corners, strict=False)))
        max_corner = mathutils.Vector(map(max, zip(*world_bbox_corners, strict=False)))

        return [[*min_corner], [*max_corner]]

    def get_object_info(self, name, sections=None, limit=100, offset=0):
        """
        Get detailed information about a specific object.

        `location`/`rotation`/`scale` are the object's local (parent-relative)
        transform; `world_bounding_box` (mesh objects only) is the world-space
        AABB computed via `matrix_world` (see `get_aabb`) - the two live in
        different spaces and are not directly comparable for a parented or
        transformed object.

        `rotation_mode` names how to read `rotation`: one of the six Euler
        orders ("XYZ", "XZY", "YXZ", "YZX", "ZXY", "ZYX") means
        `[x, y, z]` radians in that order; "QUATERNION" means `[w, x, y, z]`;
        "AXIS_ANGLE" means `[angle, x, y, z]`. Reading `rotation` without
        checking `rotation_mode` will misinterpret non-Euler objects.

        `mesh.vertices`/`edges`/`polygons` are base-mesh (pre-modifier)
        counts, same caveat as `get_mesh_data`.

        Args:
            name: Name to assign or look up.

        Returns:
            Result produced by the operation.

        Raises:
            ValueError: If the operation cannot be completed.

        """
        if sections is not None:
            allowed_sections = {
                "GEOMETRY",
                "ATTRIBUTES",
                "VOLUME_GRIDS",
                "GREASE_PENCIL",
                "PARTICLES",
                "SOFT_BODY",
                "DYNAMIC_PAINT",
            }
            sections = {str(section).upper() for section in sections}
            unknown = sorted(sections - allowed_sections)
            if unknown:
                raise ValueError(f"Unsupported object-info sections: {unknown}")
        else:
            sections = {
                "GEOMETRY",
                "ATTRIBUTES",
                "VOLUME_GRIDS",
                "GREASE_PENCIL",
                "PARTICLES",
                "SOFT_BODY",
                "DYNAMIC_PAINT",
            }
        start, _end, _truncated, _next = paginate(0, offset, limit, 1000)
        obj = bpy.data.objects.get(name)
        if not obj:
            raise ValueError(f"Object not found: {name}")
        sync_from_editmode(obj)

        if obj.rotation_mode == "QUATERNION":
            q = obj.rotation_quaternion
            rotation = [q.w, q.x, q.y, q.z]
        elif obj.rotation_mode == "AXIS_ANGLE":
            angle, x, y, z = obj.rotation_axis_angle
            rotation = [angle, x, y, z]
        else:
            rotation = [obj.rotation_euler.x, obj.rotation_euler.y, obj.rotation_euler.z]

        # Basic object info
        obj_info = {
            "name": obj.name,
            "type": obj.type,
            "location": [obj.location.x, obj.location.y, obj.location.z],
            "rotation_mode": obj.rotation_mode,
            "rotation": rotation,
            "scale": [obj.scale.x, obj.scale.y, obj.scale.z],
            "matrix_world": [[float(value) for value in row] for row in obj.matrix_world],
            "dimensions": [float(value) for value in obj.dimensions],
            "parent": obj.parent.name if obj.parent else None,
            "parent_type": obj.parent_type,
            "parent_bone": obj.parent_bone or None,
            "collections": sorted(collection.name for collection in obj.users_collection),
            "data_name": obj.data.name if obj.data else None,
            "selected": bool(obj.select_get()),
            "visible": obj.visible_get(),
            "hide_viewport": bool(obj.hide_viewport),
            "hide_render": bool(obj.hide_render),
            "materials": [],
            "modifiers": [
                {
                    "name": m.name,
                    "type": m.type,
                    "show_viewport": m.show_viewport,
                    "show_render": m.show_render,
                }
                for m in obj.modifiers
            ],
        }

        if obj.type == "MESH":
            bounding_box = self.get_aabb(obj)
            obj_info["world_bounding_box"] = bounding_box

        # Add material slots
        for slot in obj.material_slots:
            if slot.material:
                obj_info["materials"].append(slot.material.name)

        # Add mesh data if applicable
        if obj.type == "MESH" and obj.data:
            mesh = obj.data
            obj_info["mesh"] = {
                "vertices": len(mesh.vertices),
                "edges": len(mesh.edges),
                "polygons": len(mesh.polygons),
            }

        type_data = self._object_type_data(obj, sections, limit, start)
        if type_data:
            obj_info["type_data"] = type_data

        return obj_info

    @staticmethod
    def _page_records(records, offset, limit):
        total = len(records)
        start, end, truncated, next_offset = paginate(total, offset, limit, 1000)
        return {
            "total": total,
            "offset": start,
            "limit": limit,
            "returned_count": end - start,
            "truncated": truncated,
            "next_offset": next_offset,
            "records": records[start:end],
        }

    @staticmethod
    def _attribute_records(data):
        return [
            {
                "name": attribute.name,
                "data_type": attribute.data_type,
                "domain": attribute.domain,
                "count": len(attribute.data),
            }
            for attribute in getattr(data, "attributes", ())
        ]

    def _object_type_data(self, obj, sections, limit, offset):
        """Return bounded, explicitly local-space native data and simulation state."""
        result = {"coordinate_space": "OBJECT_LOCAL", "evaluated": False}
        data = obj.data
        if data is not None and "ATTRIBUTES" in sections and hasattr(data, "attributes"):
            result["attributes"] = self._page_records(self._attribute_records(data), offset, limit)

        if obj.type in {"CURVE", "SURFACE"} and "GEOMETRY" in sections:
            splines = []
            for index, spline in enumerate(data.splines):
                splines.append(
                    {
                        "index": index,
                        "type": spline.type,
                        "point_count_u": spline.point_count_u,
                        "point_count_v": spline.point_count_v,
                        "cyclic_u": spline.use_cyclic_u,
                        "cyclic_v": getattr(spline, "use_cyclic_v", False),
                        "order_u": getattr(spline, "order_u", None),
                        "order_v": getattr(spline, "order_v", None),
                        "resolution_u": spline.resolution_u,
                        "resolution_v": getattr(spline, "resolution_v", None),
                    }
                )
            result["curve"] = {
                "dimensions": data.dimensions,
                "resolution_u": data.resolution_u,
                "resolution_v": data.resolution_v,
                "bevel_depth": data.bevel_depth,
                "splines": self._page_records(splines, offset, limit),
            }
        elif obj.type == "CURVES" and "GEOMETRY" in sections:
            result["curves"] = {
                "point_count": len(data.points),
                "curve_count": len(data.curves),
                "surface": data.surface.name if getattr(data, "surface", None) else None,
            }
        elif obj.type == "POINTCLOUD" and "GEOMETRY" in sections:
            result["pointcloud"] = {"point_count": len(data.points)}
        elif obj.type == "VOLUME" and "VOLUME_GRIDS" in sections:
            grids = [
                {
                    "name": grid.name,
                    "data_type": grid.data_type,
                    "channels": grid.channels,
                    "voxel_size": list(grid.voxel_size),
                    "is_loaded": grid.is_loaded,
                }
                for grid in data.grids
            ]
            result["volume"] = {
                "filepath": data.filepath,
                "is_sequence": data.is_sequence,
                "frame_start": data.frame_start,
                "frame_duration": data.frame_duration,
                "grids": self._page_records(grids, offset, limit),
            }
        elif obj.type == "GREASEPENCIL" and "GREASE_PENCIL" in sections:
            layers = []
            for layer in data.layers:
                frames = list(layer.frames)
                layers.append(
                    {
                        "name": layer.name,
                        "frame_count": len(frames),
                        "frames": [
                            {
                                "frame_number": frame.frame_number,
                                "stroke_count": len(frame.drawing.strokes),
                                "point_count": len(frame.drawing.attributes["position"].data),
                            }
                            for frame in frames[:limit]
                        ],
                        "frames_truncated": len(frames) > limit,
                    }
                )
            result["grease_pencil"] = {"layers": self._page_records(layers, offset, limit)}

        if "PARTICLES" in sections:
            systems = [
                {
                    "name": system.name,
                    "settings": system.settings.name if system.settings else None,
                    "particle_count": len(system.particles),
                    "seed": system.seed,
                }
                for system in getattr(obj, "particle_systems", ())
            ]
            if systems:
                result["particle_systems"] = self._page_records(systems, offset, limit)
        if "SOFT_BODY" in sections and getattr(obj, "soft_body", None) is not None:
            soft_body = obj.soft_body
            point_cache = soft_body.point_cache
            result["soft_body"] = {
                "goal": soft_body.settings.use_goal,
                "self_collision": soft_body.settings.use_self_collision,
                "cache": {
                    "frame_start": point_cache.frame_start,
                    "frame_end": point_cache.frame_end,
                    "is_baked": point_cache.is_baked,
                    "is_baking": point_cache.is_baking,
                },
            }
        if "DYNAMIC_PAINT" in sections:
            states = []
            for modifier in obj.modifiers:
                if modifier.type != "DYNAMIC_PAINT":
                    continue
                states.append(
                    {
                        "modifier": modifier.name,
                        "ui_type": modifier.ui_type,
                        "canvas_active": modifier.canvas_settings is not None,
                        "brush_active": modifier.brush_settings is not None,
                    }
                )
            if states:
                result["dynamic_paint"] = self._page_records(states, offset, limit)
        return result if len(result) > 2 else {}

    _MESH_DATA_ELEMENT_TYPES = ("vertices", "edges", "faces", "loops")
    _MESH_DATA_MAX_LIMIT = 1000

    @staticmethod
    def _mesh_data_vertex(v):
        return {
            "index": v.index,
            "co": [v.co.x, v.co.y, v.co.z],
            "normal": [v.normal.x, v.normal.y, v.normal.z],
            "select": bool(v.select),
        }

    @staticmethod
    def _mesh_data_edge(e):
        return {
            "index": e.index,
            "vertices": list(e.vertices),
            "select": bool(e.select),
        }

    @staticmethod
    def _mesh_data_face(f):
        return {
            "index": f.index,
            "vertices": list(f.vertices),
            "normal": [f.normal.x, f.normal.y, f.normal.z],
            "select": bool(f.select),
            "material_index": f.material_index,
        }

    def get_mesh_data(self, object_name, element_type="vertices", limit=100, offset=0, selected_only=False):
        """
        Paginated inspection of a mesh's vertices/edges/faces/loops (indices, coords, normals, selection).

        Prerequisite for index-based edits: mesh_extrude/mesh_inset/mesh_bevel/
        mesh_bridge/mesh_subdivide take raw indices with no way to discover them
        otherwise, since get_object_info only reports element counts.

        Coordinates and normals come from the object's base mesh (`obj.data`)
        in local (object-space) coordinates - modifiers are not evaluated. To
        get world-space positions, transform by the object's `matrix_world`
        (see `get_object_info`).

        Args:
            object_name: Name of the Blender object to operate on.
            element_type: Value for element type.
            limit: Maximum number of items to return.
            offset: Zero-based starting position.
            selected_only: Value for selected only.

        Returns:
            Result produced by the operation.

        Raises:
            ValueError: If the operation cannot be completed.

        """
        if element_type not in self._MESH_DATA_ELEMENT_TYPES:
            raise ValueError(f"Invalid element_type: {element_type}. Must be one of {self._MESH_DATA_ELEMENT_TYPES}")
        obj = get_mesh_object(object_name)
        sync_from_editmode(obj)
        mesh = obj.data

        if element_type == "vertices":
            all_elements = mesh.vertices
            to_dict = self._mesh_data_vertex
        elif element_type == "edges":
            all_elements = mesh.edges
            to_dict = self._mesh_data_edge
        elif element_type == "faces":
            all_elements = mesh.polygons
            to_dict = self._mesh_data_face
        else:
            if selected_only:
                raise ValueError(
                    "selected_only is not supported for element_type='loops': "
                    "MeshLoop has no selection state of its own (use 'vertices', "
                    "'edges', or 'faces' instead)"
                )
            all_elements = mesh.loops
            face_of_loop = {}
            for face in mesh.polygons:
                for loop_index in face.loop_indices:
                    face_of_loop[loop_index] = face.index
            if hasattr(mesh, "calc_normals_split"):
                mesh.calc_normals_split()

            def to_dict(loop):
                normal = loop.normal
                return {
                    "index": loop.index,
                    "vertex_index": loop.vertex_index,
                    "edge_index": loop.edge_index,
                    "face_index": face_of_loop.get(loop.index),
                    "normal": [normal.x, normal.y, normal.z],
                }

        total_unfiltered = len(all_elements)
        if selected_only:
            universe = [el for el in all_elements if el.select]
            total = len(universe)
            start, end, truncated, next_offset = paginate(total, offset, limit, self._MESH_DATA_MAX_LIMIT)
            page = universe[start:end]
        else:
            total = total_unfiltered
            start, end, truncated, next_offset = paginate(total, offset, limit, self._MESH_DATA_MAX_LIMIT)
            # islice avoids materializing the whole (possibly huge) collection
            # when the caller only asked for a small page of it.
            page = itertools.islice(all_elements, start, end)
        elements = [to_dict(el) for el in page]

        return {
            "name": obj.name,
            "element_type": element_type,
            "total": total,
            "total_unfiltered": total_unfiltered,
            "offset": start,
            "limit": limit,
            "returned_count": len(elements),
            "truncated": truncated,
            "next_offset": next_offset,
            "elements": elements,
        }
