"""Core/meta tools: addon status and integration status."""

import logging

from typing import Literal

from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError

from ...addon_manager import EXPECTED_ADDON_PROTOCOL_VERSION
from ..app import mcp
from ..connection import force_addon_handshake, get_blender_connection
from .envelope import ok

logger = logging.getLogger("BlenderMCPServer")

Provider = Literal["polyhaven", "sketchfab", "nd"]

_STATUS_COMMANDS: dict[Provider, str] = {
    "polyhaven": "get_polyhaven_status",
    "sketchfab": "get_sketchfab_status",
    "nd": "get_nd_status",
}


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
        blender = get_blender_connection()
        if provider is not None:
            result = blender.send_command(_STATUS_COMMANDS[provider])
            return ok(result)
        results = {name: blender.send_command(command) for name, command in _STATUS_COMMANDS.items()}
        return ok(results)
    except Exception as e:
        logger.error(f"Error checking integration status: {e}")
        raise ToolError(f"Error checking integration status: {e}") from e


@mcp.tool()
async def get_addon_status(ctx: Context) -> dict:
    """
    Check whether the connected Blender addon matches this MCP server version.

    Args:
        ctx: MCP request context.

    Returns:
        "up_to_date" (bool), "protocol_version"/"expected_protocol_version" (ints to compare), "addon_version",
        "capabilities" (feature flags reported by the addon), "blender_version", "writable_output_roots"
        (directories Blender can write to; empty when the addon reports none), "source" (how the
        handshake was obtained), "warning" (non-None if something looks off), "update_command" (run it when
        up_to_date is false), and "after_install" (what to do in Blender afterwards).

    Raises:
        ToolError: If the operation cannot be completed.

    """
    try:
        blender = get_blender_connection()
        result = force_addon_handshake(blender)
        if result is None:
            raise ToolError("Could not determine addon status.")
        payload = {
            "up_to_date": result.up_to_date,
            "protocol_version": result.protocol_version,
            "expected_protocol_version": EXPECTED_ADDON_PROTOCOL_VERSION,
            "addon_version": result.addon_version,
            "capabilities": result.capabilities,
            "blender_version": result.blender_version,
            "writable_output_roots": result.writable_output_roots,
            "source": result.source,
            "warning": result.warning,
            "update_command": "blender-mcp install-addon",
            "after_install": (
                "If the addon file was updated: in Blender, Preferences → Add-ons → "
                "disable/enable 'Interface: Blender MCP', or restart Blender, then Start MCP Server."
            ),
        }
        return ok(payload)
    except ToolError:
        raise
    except Exception as e:
        logger.error(f"Error checking addon status: {e}")
        raise ToolError(f"Error checking addon status: {e}") from e
