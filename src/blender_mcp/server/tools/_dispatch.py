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

An addon that advertises `idempotent_resend` answers a request id it already ran from its reply
cache. For it, a command whose outcome is unknown is remembered by its command and canonical
parameters, and an identical call within `_RESEND_WINDOW_SECONDS` is resent under the first
attempt's id: Blender runs it once whichever attempt arrives, so the message says an identical
retry is safe. A reply or a refusal ends that, since the outcome is then known. Any command is
remembered, not only mutating ones: the addon's own registry decides per call what it caches,
and a resent read simply runs again, which is as safe.
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
# Said instead, after the same failure, to an addon that answers a resent id from its reply cache.
_RESEND_HINT = (
    "The command was sent before the connection failed, so Blender may have run it, may still be running it, "
    "or may never have read it. An identical retry is safe: it is resent under the same request id, and Blender "
    "answers an id it already ran with that run's reply instead of running it again, so for the next 10 minutes "
    "an identical call returns the first run's result. The next command reconnects."
)

_RESEND_WINDOW_SECONDS = 600.0
_RESEND_CAPACITY = 64


class _ResendIds:
    """
    The request ids of recent calls whose outcome is unknown, least recently set first.

    Keyed by `(command, canonical JSON params)`. An entry lasts `ttl_seconds` from the last
    unknown outcome that set it, and past `capacity` the least recently set is dropped. A
    resend takes its entry out, so the answer it gets - a reply or a refusal - leaves nothing
    behind, and only another unknown outcome puts it back. Tools dispatch from worker threads,
    so every access holds the lock.
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
        self._ids: OrderedDict[tuple[str, str], tuple[str, float]] = OrderedDict()
        self._lock = threading.Lock()

    def take(self, key: tuple[str, str]) -> str | None:
        """
        Remove and return the id to resend this call under, or None when it is a new request.

        Args:
            key: The call's command and canonical params.

        Returns:
            str | None: The remembered id, unless it has expired.

        """
        with self._lock:
            entry = self._ids.pop(key, None)
        if entry is None:
            return None
        request_id, expires_at = entry
        if self._clock() >= expires_at:
            return None
        return request_id

    def remember(self, key: tuple[str, str], request_id: str) -> None:
        """
        Record an unknown outcome, restarting the window.

        Args:
            key: The call's command and canonical params.
            request_id: The id it was sent under.

        """
        with self._lock:
            self._ids[key] = (request_id, self._clock() + self._ttl_seconds)
            self._ids.move_to_end(key)
            while len(self._ids) > self._capacity:
                self._ids.popitem(last=False)


_resend_ids = _ResendIds()


def _resend_key(command: str, params: dict[str, Any] | None) -> tuple[str, str] | None:
    """
    Name a call by what it asks for, when the addon would deduplicate its resend.

    Args:
        command: Addon command name.
        params: Its parameters.

    Returns:
        tuple[str, str] | None: The command and its params as sorted-key JSON; None when the
        addon does not advertise `idempotent_resend`, or the params do not serialize, in which
        case the send reports that itself.

    """
    handshake = get_last_handshake()
    if handshake is None or not handshake.idempotent_resend:
        return None
    try:
        return command, json.dumps(params or {}, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return None


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
        ToolError: If Blender refused the operation, or the round trip failed.

    """
    blender = get_blender_connection()
    key = _resend_key(command, params)
    request_id = _resend_ids.take(key) if key is not None else None
    try:
        if request_id is None:
            reply = blender.send_command(command, params)
        else:
            logger.info("Resending %s under request %s, whose outcome was unknown", command, request_id)
            reply = blender.send_command(command, params, request_id=request_id)
    except BlenderOperationError as exc:
        logger.error("Blender refused %s: %s", command, exc)
        raise ToolError(str(exc)) from exc
    except BlenderCommandNotSentError as exc:
        if key is not None and request_id is not None:
            # This resend never left, and the attempt before it may still have run.
            _resend_ids.remember(key, request_id)
        logger.error("Transport failure before sending %s: %s", command, exc)
        raise ToolError(f"{exc} {_RETRY_HINT}") from exc
    except BlenderTransportError as exc:
        logger.error("Transport failure running %s: %s", command, exc)
        if key is not None and exc.request_id is not None:
            _resend_ids.remember(key, exc.request_id)
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
