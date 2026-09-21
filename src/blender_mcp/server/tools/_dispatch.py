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
answered, so the message says the connection was dropped and that retrying reconnects. Both reach
the MCP client as `ToolError`; the distinction is for the prose, not for the wire.
"""

import asyncio
import logging

from collections.abc import Sequence
from typing import Any

from mcp.server.fastmcp.exceptions import ToolError

from ..connection import BlenderOperationError, BlenderTransportError, get_blender_connection
from .envelope import envelope_for

logger = logging.getLogger("BlenderMCPServer")

# Said after a transport failure's own message. The socket is already dropped by the time the
# error arrives, so the next command reconnects, and a command that was never answered is worth
# one retry before the request itself is blamed.
_RETRY_HINT = (
    "The connection to Blender was dropped and the next command reconnects, so retry once before changing the request."
)


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
    try:
        return get_blender_connection().send_command(command, params)
    except BlenderOperationError as exc:
        logger.error("Blender refused %s: %s", command, exc)
        raise ToolError(str(exc)) from exc
    except BlenderTransportError as exc:
        logger.error("Transport failure running %s: %s", command, exc)
        raise ToolError(f"{exc} {_RETRY_HINT}") from exc


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
