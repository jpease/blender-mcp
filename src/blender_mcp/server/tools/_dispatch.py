"""
The one place a tool sends a command to Blender, and the one place a reply becomes an envelope.

Every tool module imports from here; no package keeps a transport of its own. `call_blender` is
the only function in the server that both sends a command and shapes its reply, so the envelope
rules in `envelope.py` cannot be forgotten at a call site. A tool that has to read the reply
before it knows what changed - a created object's generated name - awaits `send_blender_command`
and hands the reply to `envelope_for` itself; `send_command` is the same round trip without the
thread hop, for code that already runs in a worker thread (`image_capture.capture_png`).

The test and embedding seam is this module's `get_blender_connection`: every layer below resolves
it from these module globals on each call, so replacing it - the `stub_blender_connection` fixture
does exactly that - reroutes every tool in every package, however it imported its dispatch
function. There is no per-package hook.

Blender's two failure modes stay apart all the way to the client. A `BlenderOperationError` means
the addon read the request and refused it, so its message is reported verbatim: it already names
the bone, object or argument at fault. A `BlenderTransportError` means the command was never
answered, and the prose depends on when the connection failed. Before the whole command was
written (`BlenderCommandNotSentError`) Blender cannot have run it, so the message says retrying
reconnects; after, Blender may have run it, so the message says to inspect the scene before any
retry. All of these reach the MCP client as `ToolError`; the distinction is for the prose, not
for the wire.

An addon that advertises `marked_resend` accepts a frame marked as a resend: it answers the id
from its reply cache when it still holds the reply to that id's first run, and otherwise refuses
it without running anything. For it, a command whose outcome is unknown is remembered by its
command and canonical parameters, with the id and the session marker it was sent under, and an
identical call within `_RESEND_WINDOW_SECONDS` is resent under that id and marker. Blender never
runs the resend itself, so the message says an identical retry either returns the first run's
result or is refused. A resend is refused here, unsent, when the cached handshake already shows
another session than the first attempt was sent under. A reply or a refusal ends that, since
the next identical call is then a new request. Any command is remembered, not only mutating
ones: the server cannot tell a read whose params decide it from a mutation, and refusing a
resent read costs only the call that issues it again.
"""

import asyncio
import json
import logging
import threading
import time

from collections import OrderedDict
from collections.abc import Callable, Sequence
from typing import Any

from mcp.server.fastmcp.exceptions import ToolError

from ..connection import (
    BlenderCommandNotSentError,
    BlenderOperationError,
    BlenderTransportError,
    Resend,
    get_blender_connection,
    get_last_handshake,
)
from .envelope import envelope_for

logger = logging.getLogger("BlenderMCPServer")

# Said after a transport failure that happened before the whole command was written. The socket
# is already dropped, so the next command reconnects, and a command Blender never read is worth
# one retry before the request itself is blamed.
_RETRY_HINT = (
    "The connection to Blender was dropped and the next command reconnects, so retry once before changing the request."
)
# Said after a transport failure once the whole command was written. Blender runs commands on its
# main thread and may still be in a long bake, render or import, or may have finished and lost only
# the reply; a blind retry of a non-idempotent command would create objects or insert keys twice.
_OUTCOME_UNKNOWN_HINT = (
    "The command was sent before the connection failed, so Blender may have run it, may still be running it, "
    "or may never have read it: inspect the scene for its effect before retrying, since a blind retry can do "
    "the work twice. The next command reconnects."
)
# Said instead, after the same failure, to an addon that answers a marked resend from its reply
# cache or refuses it, and never runs it.
_RESEND_HINT = (
    "The command was sent before the connection failed, so Blender may have run it, may still be running it, "
    "or may never have read it. For the next 10 minutes an identical call is sent as a retry of that same "
    "request, and Blender never runs the retry itself: it answers with the first run's result if it still holds "
    "it, and otherwise refuses the retry without running anything, in which case inspect the scene for the "
    "command's effect before issuing it again. The next command reconnects."
)
# Said instead of resending, when the session moved since the first attempt was sent: the
# addon would refuse the resend for the same reason.
_RESEND_REFUSED = (
    "Not sent: this repeats a command whose first attempt's outcome was never learned, and Blender's session has "
    "changed since that attempt was sent (another file was opened, or the add-on was reloaded), so whether it ran "
    "cannot be established. Inspect the scene for the command's effect before issuing it again; issued again, it "
    "runs as a new request."
)
# Said instead, after the same failure, for a command the addon's registry marks read-only: running
# a read twice changes nothing, so inspecting the scene first would only cost a call.
_READ_ONLY_RETRY_HINT = (
    "The command was sent before the connection failed, but it only reads the scene, so retrying it is safe. "
    "The next command reconnects."
)


def _is_read_only(command: str) -> bool:
    """
    Report whether the addon named this command read-only whatever its params.

    Only the addon's own registry decides, through the handshake: a guess from the tool's name
    that was wrong would tell an agent to blindly repeat a mutation.

    Args:
        command: Addon command name.

    Returns:
        bool: True only when the handshake lists it.

    """
    handshake = get_last_handshake()
    return handshake is not None and command in handshake.read_only_commands


_RESEND_WINDOW_SECONDS = 600.0
_RESEND_CAPACITY = 64


class _ResendIds:
    """
    The first attempts of recent calls whose outcome is unknown, least recently set first.

    Keyed by `(command, canonical JSON params)`, each holding the id and session marker the
    first attempt was sent under. An entry lasts `ttl_seconds` from the last unknown outcome
    that set it, and past `capacity` the least recently set is dropped. A resend takes its
    entry out, so the answer it gets - a reply or a refusal - leaves nothing behind, and only
    another unknown outcome puts it back. Tools dispatch from worker threads, so every access
    holds the lock.
    """

    def __init__(
        self,
        *,
        capacity: int = _RESEND_CAPACITY,
        ttl_seconds: float = _RESEND_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._capacity = capacity
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._ids: OrderedDict[tuple[str, str], tuple[Resend, float]] = OrderedDict()
        self._lock = threading.Lock()

    def take(self, key: tuple[str, str]) -> Resend | None:
        """
        Remove and return the first attempt to resend this call as, or None when it is a new request.

        Args:
            key: The call's command and canonical params.

        Returns:
            Resend | None: The remembered attempt, unless it has expired.

        """
        with self._lock:
            entry = self._ids.pop(key, None)
        if entry is None:
            return None
        attempt, expires_at = entry
        if self._clock() >= expires_at:
            return None
        return attempt

    def remember(self, key: tuple[str, str], attempt: Resend) -> None:
        """
        Record an unknown outcome, restarting the window.

        Args:
            key: The call's command and canonical params.
            attempt: The id and session marker the first attempt was sent under.

        """
        with self._lock:
            self._ids[key] = (attempt, self._clock() + self._ttl_seconds)
            self._ids.move_to_end(key)
            while len(self._ids) > self._capacity:
                self._ids.popitem(last=False)


_resend_ids = _ResendIds()


def _resend_key(command: str, params: dict[str, Any] | None) -> tuple[str, str] | None:
    """
    Name a call by what it asks for, when the addon would answer or refuse its resend.

    Args:
        command: Addon command name.
        params: Its parameters.

    Returns:
        tuple[str, str] | None: The command and its params as sorted-key JSON; None when the
        addon does not advertise `marked_resend`, or the params do not serialize, in which
        case the send reports that itself.

    """
    handshake = get_last_handshake()
    if handshake is None or not handshake.marked_resend:
        return None
    try:
        return command, json.dumps(params or {}, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return None


def _session_marker() -> tuple[str | None, int | None]:
    """
    Read the session the cached handshake says Blender has open.

    Returns:
        tuple: `(session_id, session_epoch)`; both None before any handshake, which the
        addon matches to no session.

    """
    handshake = get_last_handshake()
    return (None, None) if handshake is None else handshake.session_marker()


def send_command(command: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    Send one command over the socket and return the addon's reply, blocking until it lands.

    For callers already running off the event loop; everything else awaits
    `send_blender_command`.

    Args:
        command: Addon command name.
        params: JSON-serializable parameters for that command.

    Returns:
        Whatever the addon returned, unwrapped from its response frame.

    Raises:
        ToolError: If Blender refused the operation, the round trip failed, or the call
            would resend an attempt sent under a session that is no longer open.

    """
    key = None
    resend = None
    try:
        # Inside the try: a Blender that cannot be reached at all is a command never sent.
        blender = get_blender_connection()
        key = None if _is_read_only(command) else _resend_key(command, params)
        resend = _resend_ids.take(key) if key is not None else None
        if resend is None:
            reply = blender.send_command(command, params)
        elif resend.session_marker != _session_marker():
            logger.error("Not resending %s: the session moved since request %s was sent", command, resend.request_id)
            raise ToolError(_RESEND_REFUSED)
        else:
            logger.info("Resending %s under request %s, whose outcome was unknown", command, resend.request_id)
            reply = blender.send_command(command, params, resend=resend)
    except BlenderOperationError as exc:
        logger.error("Blender refused %s: %s", command, exc)
        raise ToolError(str(exc)) from exc
    except BlenderCommandNotSentError as exc:
        if key is not None and resend is not None:
            # This resend never left, and the attempt before it may still have run.
            _resend_ids.remember(key, resend)
        logger.error("Transport failure before sending %s: %s", command, exc)
        raise ToolError(f"{exc} {_RETRY_HINT}") from exc
    except BlenderTransportError as exc:
        logger.error("Transport failure running %s: %s", command, exc)
        if _is_read_only(command):
            raise ToolError(f"{exc} {_READ_ONLY_RETRY_HINT}") from exc
        if key is not None and exc.request_id is not None:
            # A lost resend changed nothing, so it stays a resend of the first attempt.
            _resend_ids.remember(key, resend or Resend(exc.request_id, _session_marker()))
            raise ToolError(f"{exc} {_RESEND_HINT}") from exc
        raise ToolError(f"{exc} {_OUTCOME_UNKNOWN_HINT}") from exc
    return reply


async def send_blender_command(command: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    Send one command to Blender from the event loop and return the addon's reply.

    The socket round trip blocks for as long as Blender takes to run the command, so it is
    offloaded to a worker thread; awaiting it here is what keeps the MCP server answering other
    requests meanwhile.

    Args:
        command: Addon command name.
        params: JSON-serializable parameters for that command.

    Returns:
        Whatever the addon returned, unwrapped from its response frame.

    Raises:
        ToolError: If Blender refused the operation, or the round trip failed.

    """
    return await asyncio.to_thread(send_command, command, params)


async def call_blender(
    command: str,
    params: dict[str, Any] | None = None,
    *,
    changed_objects: Sequence[str] | None = None,
    changed_resources: Sequence[str] | None = None,
    warnings: Sequence[str] | None = None,
) -> dict:
    """
    Send one command to Blender and wrap its reply in the standard response envelope.

    Args:
        command: Addon command name.
        params: JSON-serializable parameters for that command.
        changed_objects: Object names to report as changed when the addon names none itself.
            An addon `changed_objects` key replaces this rather than extending it. `None` and an
            empty list mean the same thing, so a tool can pass the result of its own "did this
            change anything" decision straight through.
        changed_resources: Non-object datablock names, under that same replacement rule.
        warnings: Notices the tool knows before the call - a destructive action's stale-index
            warning - which have to be in the reply while it is measured against the byte budget.

    Returns:
        The `ok()` envelope, with `changed_objects` and `changed_resources` moved out of `data`
        into envelope fields.

    Raises:
        ToolError: If Blender refused the operation, or the round trip failed.

    """
    reply = await send_blender_command(command, params)
    return envelope_for(
        reply,
        changed_objects=changed_objects or (),
        changed_resources=changed_resources or (),
        warnings=warnings or (),
    )
