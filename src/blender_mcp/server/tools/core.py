"""Core/meta tools: addon status and integration status."""

import asyncio
import logging

from typing import Literal

from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError

from ...addon_manager import EXPECTED_ADDON_PROTOCOL_VERSION, AddonHandshake
from ..app import mcp
from ..connection import BlenderTransportError, force_addon_handshake, get_blender_connection
from ._dispatch import send_blender_command
from .envelope import ok

logger = logging.getLogger("BlenderMCPServer")

Provider = Literal["polyhaven", "sketchfab", "nd"]

_STATUS_COMMANDS: dict[Provider, str] = {
    "polyhaven": "get_polyhaven_status",
    "sketchfab": "get_sketchfab_status",
    "nd": "get_nd_status",
}

# The provider-gated command each optional integration adds to the addon's handler table
# (`server_core.py _build_command_handlers`). Its presence in the handshake's capability
# list is that integration being enabled for the open .blend.
_INTEGRATION_CAPABILITIES: dict[Provider, str] = {
    "polyhaven": "import_polyhaven_asset",
    "sketchfab": "import_sketchfab_model",
    "nd": "nd_boolean",
}


def _status_payload(result: AddonHandshake, *, detail: bool) -> dict[str, object]:
    """
    Turn a handshake into the status an agent reads, summarizing the capability list.

    The list is every command name the addon serves; the server itself gates every
    command on it (`connection.py`), so an agent needs how many there are and which
    optional integrations they cover, not the names.

    Args:
        result: The handshake, already refreshed.
        detail: Also carry the command names themselves.

    Returns:
        dict[str, object]: The payload documented by `get_addon_status`.

    """
    payload: dict[str, object] = {
        "up_to_date": result.up_to_date,
        "protocol_version": result.protocol_version,
        "expected_protocol_version": EXPECTED_ADDON_PROTOCOL_VERSION,
        "addon_version": result.addon_version,
        "capability_count": len(result.capabilities),
        "integrations_available": {
            provider: command in result.capabilities for provider, command in _INTEGRATION_CAPABILITIES.items()
        },
        "blender_version": result.blender_version,
        "writable_output_roots": result.writable_output_roots,
        "file_roots": result.file_roots,
        "file_roots_enforced": result.file_roots_enforced,
        "current_filepath": result.current_filepath,
        # Both halves: the epoch restarts at 0 with the addon, so it means
        # nothing without its session_id.
        "session_id": result.session_id,
        "session_epoch": result.session_epoch,
        # After an aborted file swap the addon refuses most commands and the
        # open .blend must not be saved; without this it looks like a broken addon.
        "session_indeterminate": result.session_indeterminate,
        "source": result.source,
        "warning": result.warning,
        # Why `up_to_date` is false when the two protocol numbers match: the installed
        # add-on's dispatch table is short of what `addon_surface.json` records for this
        # protocol. Uncapped, unlike `capabilities` above - these are empty in the normal
        # case, and in the abnormal one every name is the evidence the agent needs to stop
        # concluding a command was never implemented.
        "missing_commands": result.missing_commands,
        "missing_parameters": result.missing_parameters,
        "update_command": "blender-mcp install-addon",
        "after_install": (
            "If the addon file was updated: in Blender, Preferences → Add-ons → "
            "disable/enable 'Interface: Blender MCP', or restart Blender, then Start MCP Server."
        ),
    }
    if detail:
        payload["capabilities"] = result.capabilities
    return payload


async def _collect_integration_status(provider: Provider | None) -> dict:
    """
    Ask the addon which optional integrations are enabled.

    Args:
        provider: A single provider to query, or None for all of them.

    Returns:
        dict: The provider's status, or a mapping of provider name to status.

    """
    if provider is not None:
        return await send_blender_command(_STATUS_COMMANDS[provider])
    return {name: await send_blender_command(command) for name, command in _STATUS_COMMANDS.items()}


def _collect_addon_status(*, detail: bool) -> dict[str, object]:
    """
    Force a fresh handshake and render it as the status payload.

    Blocking: the handshake round-trip runs in a worker thread.

    Args:
        detail: Include every advertised command name.

    Returns:
        dict[str, object]: The addon status payload.

    Raises:
        ToolError: When the handshake round trip never completed, or when no handshake
            has ever been read.

    """
    blender = get_blender_connection()
    try:
        result = force_addon_handshake(blender)
    except (BlenderTransportError, ConnectionError) as exc:
        # A dead socket used to arrive here as a handshake and be reported as a version
        # verdict - `up_to_date: false`, `capability_count: 0`, `protocol_version: null` -
        # so an agent whose first call landed on a connection Blender had already retired
        # was told to reinstall a current add-on. Nothing was learned, and the payload
        # must not pretend otherwise.
        raise ToolError(
            f"Blender did not answer the handshake ({exc}). This is a transport failure, not a "
            "version verdict: the installed add-on's version is unknown, so do not reinstall on "
            "this basis. Retry the call - a socket the add-on has already closed is reconnected "
            "on the next command."
        ) from exc
    if result is None:
        raise ToolError("Could not determine addon status.")
    return _status_payload(result, detail=detail)


@mcp.tool()
async def get_integration_status(ctx: Context, provider: Provider | None = None) -> dict:
    """
    Check whether an optional third-party integration is enabled in Blender.

    Args:
        ctx: MCP request context.
        provider: One of "polyhaven", "sketchfab", "nd". If omitted, checks all three and
            returns a dict keyed by provider name. Each provider's result is {"enabled": bool, "message": str}
            describing whether that integration's features are available and, if not, how to enable it.

    Returns:
        when provider is given, that provider's {"enabled": bool, "message": str}; when omitted, a dict keyed
        by "polyhaven"/"sketchfab"/"nd", each mapping to that same shape.

    Raises:
        ToolError: If the operation cannot be completed.

    """
    try:
        return ok(await _collect_integration_status(provider))
    except Exception as e:
        logger.error(f"Error checking integration status: {e}")
        raise ToolError(f"Error checking integration status: {e}") from e


@mcp.tool()
async def get_addon_status(ctx: Context, detail: bool = False) -> dict:
    """
    Check whether the connected Blender addon matches this MCP server version.

    Args:
        ctx: MCP request context.
        detail: Also return every command name the addon advertises; the server already refuses
            a command it does not, so the names only explain such a refusal.

    Returns:
        Every field below is something the add-on reported about itself; a handshake that
        never completed raises instead of being rendered as one.
        "up_to_date" (bool), "protocol_version"/"expected_protocol_version", "addon_version",
        "capability_count" and "integrations_available" (per-provider, whether the addon advertises that
        integration's commands), "capabilities" (the command names, only with detail), "blender_version",
        "missing_commands"/"missing_parameters" (empty when current; non-empty means the installed add-on
        predates this server even though its protocol number matches, and must be reinstalled via
        "update_command" before those commands will work - they were not omitted from the project),
        "writable_output_roots" (empty when none), "file_roots"/"file_roots_enforced" (false:
        paths unconfined), "current_filepath", "session_id"/"session_epoch" (re-read capabilities if the pair
        moves), "session_indeterminate" (true: a swap aborted; most commands refused, don't save over the file),
        "source", "warning", "update_command", "after_install".

    Raises:
        ToolError: If Blender never answered the handshake, which is a transport failure and
            says nothing about which add-on version is installed - retry rather than reinstall.
            Also if the status could not be determined for any other reason.

    """
    try:
        return ok(await asyncio.to_thread(_collect_addon_status, detail=detail))
    except ToolError:
        raise
    except Exception as e:
        logger.error(f"Error checking addon status: {e}")
        raise ToolError(f"Error checking addon status: {e}") from e
