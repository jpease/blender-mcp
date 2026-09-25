"""Core/meta tools: addon status and integration status."""

import asyncio
import logging
import os

from collections.abc import Sequence
from typing import Annotated, Literal

from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from ...addon_manager import EXPECTED_ADDON_PROTOCOL_VERSION, AddonHandshake
from ..app import mcp
from ..bundles import BUNDLES, TOOLSETS_ENV_VAR
from ..connection import BlenderTransportError, force_addon_handshake, get_blender_connection
from ..mount_map import (
    CORE_BUNDLE,
    bundle_tool_names,
    bundles_providing,
    known_tool_names,
    unmounted_bundle_counts,
)
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


def _mounted_tool_names() -> frozenset[str]:
    """
    Read the tool names this process actually registered.

    Returns:
        The mounted names, read off the live app rather than re-derived from the environment,
        so the report cannot disagree with what the client can call.

    """
    return frozenset(tool.name for tool in mcp._tool_manager.list_tools())


def _mounted_tools_page(mounted: frozenset[str], *, limit: int, offset: int) -> dict[str, object]:
    """
    Enumerate the tool names this process registered, one sorted page at a time.

    Every other mount field is a count: they say how many tools this session has, never which, so
    a tool documented in the project but absent from a live build was detectable only by calling
    it and reading the client's "unknown tool" error. The names are sorted because the registry's
    own order is import order, which shifts with the selection and would make paging unstable.

    Args:
        mounted: The tool names registered in this process.
        limit: How many names this page carries.
        offset: Index of the first name on this page.

    Returns:
        dict[str, object]: The page under `items`, with the pagination fields to resume from;
        `next_offset` is None once the page reaches the end.

    """
    names = sorted(mounted)
    page = names[offset : offset + limit]
    resume = offset + len(page)
    truncated = resume < len(names)
    return {
        "items": page,
        "total": len(names),
        "offset": offset,
        "limit": limit,
        "returned_count": len(page),
        "truncated": truncated,
        "next_offset": resume if truncated else None,
    }


def _suggested_toolsets(bundle: str) -> str:
    """
    Spell the `BLENDER_MCP_TOOLSETS` value that adds one bundle to this process's selection.

    Args:
        bundle: The bundle to add.

    Returns:
        The full replacement value, since the variable is set whole, not appended to.

    """
    current = (os.getenv(TOOLSETS_ENV_VAR) or "").strip().strip(",")
    return f"{current},{bundle}" if current else bundle


def _toolset_payload(mounted: frozenset[str]) -> dict[str, object]:
    """
    Say which slice of the catalog this process mounted, and what it left out.

    A client sees only the mounted tools, so without this an absent tool reads as one the
    project never implemented. Bundle membership is derived from the mounted names rather
    than by re-parsing the environment variable: that is the state the client is actually in.

    Args:
        mounted: The tool names registered in this process.

    Returns:
        dict[str, object]: Counts and bundle names, not the tool names themselves - the full
        catalog is far past the reply budget, so the names are a page of their own. Use
        `get_addon_status(mounted_tools=True)` for them, or `tool_name=...` to resolve one.

    """
    by_bundle = bundle_tool_names()
    unmounted = unmounted_bundle_counts(mounted)
    return {
        "env_var": TOOLSETS_ENV_VAR,
        "requested": os.getenv(TOOLSETS_ENV_VAR),
        "mounted_bundles": [CORE_BUNDLE, *(name for name in BUNDLES if by_bundle[name] <= mounted)],
        "mounted_tool_count": len(mounted),
        # Bundle name to how many of its tools are absent here. Non-empty is the normal case:
        # the whole catalog at once fills a context window, which is why bundles exist.
        "unmounted_bundles": unmounted,
        # Distinct names, not the sum of the counts above: bundles overlap (`lighting` and
        # `lighting-construction` share a module), so summing them counts shared tools twice.
        "unmounted_tool_count": len(known_tool_names() - mounted),
        "selection_note": (
            f"Mounting is chosen per process by {TOOLSETS_ENV_VAR} before startup; no tool call can change "
            "it. A tool absent from this session may still be implemented - pass mounted_tools=True to this "
            "tool for the mounted names, one page at a time, or tool_name for a verdict on one name, rather "
            "than concluding it does not exist."
        ),
    }


def _tool_lookup(tool_name: str, capabilities: Sequence[str], mounted: frozenset[str]) -> dict[str, object]:
    """
    Answer, for one tool name, which of four different situations the caller is in.

    Mounted; implemented but not mounted here; absent from this server build while the
    connected add-on still serves the command; or unknown to both. They call for opposite
    responses - retry, change the environment, upgrade the server, fix the spelling - and a
    client's "unknown tool" error cannot tell them apart.

    Args:
        tool_name: The name asked about.
        capabilities: The command names the connected add-on advertises.
        mounted: The tool names registered in this process.

    Returns:
        dict[str, object]: The verdict and the evidence behind it.

    """
    providers = bundles_providing(tool_name)
    addon_command = tool_name in capabilities
    if tool_name in mounted:
        verdict = "Mounted: callable in this session."
    elif providers:
        verdict = (
            f"Implemented in this server build but not mounted by this process. Restart the MCP server with "
            f"{TOOLSETS_ENV_VAR}={_suggested_toolsets(providers[0])} to mount it."
        )
    elif addon_command:
        verdict = (
            "This server build registers no such tool, but the connected add-on serves a command by that "
            "name: the add-on is newer than the server package. Upgrade the server rather than the add-on."
        )
    else:
        verdict = "Neither this server build nor the connected add-on knows this name; check the spelling."
    return {
        "name": tool_name,
        "mounted": tool_name in mounted,
        "in_this_build": bool(providers),
        "bundles": list(providers),
        "addon_command": addon_command,
        "verdict": verdict,
    }


def _status_payload(
    result: AddonHandshake,
    *,
    detail: bool,
    tool_name: str | None,
    mounted_tools: bool,
    tool_limit: int,
    tool_offset: int,
) -> dict[str, object]:
    """
    Turn a handshake into the status an agent reads, summarizing the capability list.

    The list is every command name the addon serves; the server itself gates every
    command on it (`connection.py`), so an agent needs how many there are and which
    optional integrations they cover, not the names.

    Args:
        result: The handshake, already refreshed.
        detail: Also carry the command names themselves.
        tool_name: A tool to resolve against this process's mount state, or None.
        mounted_tools: Also carry a page of the tool names this process registered.
        tool_limit: How many names that page carries.
        tool_offset: Index of the first name on that page.

    Returns:
        dict[str, object]: The payload documented by `get_addon_status`.

    """
    mounted = _mounted_tool_names()
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
        # What Cycles renders on, from the add-on's Preferences: `cycles.device = "GPU"` falls back
        # to the CPU without these. The machine's whole device list is detail-only.
        "render_devices": (
            None
            if result.render_devices is None
            else {key: value for key, value in result.render_devices.items() if key != "available_devices"}
        ),
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
        # What THIS process mounted. The capability count above is the add-on's surface, which
        # is a different and much larger number; reading one for the other is how a mounted-
        # tool question gets answered with an add-on fact.
        "toolsets": _toolset_payload(mounted),
    }
    if tool_name is not None:
        payload["tool_lookup"] = _tool_lookup(tool_name, result.capabilities, mounted)
    if mounted_tools:
        payload["mounted_tools"] = _mounted_tools_page(mounted, limit=tool_limit, offset=tool_offset)
    if detail:
        payload["capabilities"] = result.capabilities
        payload["render_devices"] = result.render_devices
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


def _collect_addon_status(
    *, detail: bool, tool_name: str | None, mounted_tools: bool, tool_limit: int, tool_offset: int
) -> dict[str, object]:
    """
    Force a fresh handshake and render it as the status payload.

    Blocking: the handshake round-trip runs in a worker thread.

    Args:
        detail: Include every advertised command name.
        tool_name: A tool to resolve against this process's mount state, or None.
        mounted_tools: List the tool names this process registered.
        tool_limit: How many names that page carries.
        tool_offset: Index of the first name on that page.

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
    return _status_payload(
        result,
        detail=detail,
        tool_name=tool_name,
        mounted_tools=mounted_tools,
        tool_limit=tool_limit,
        tool_offset=tool_offset,
    )


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
    return ok(await _collect_integration_status(provider))


@mcp.tool()
async def get_addon_status(
    ctx: Context,
    detail: bool = False,
    tool_name: str | None = None,
    mounted_tools: bool = False,
    tool_limit: Annotated[int, Field(ge=1, le=300)] = 100,
    tool_offset: Annotated[int, Field(ge=0, le=9999)] = 0,
) -> dict:
    """
    Check whether the connected Blender addon matches this MCP server version.

    Args:
        ctx: MCP request context.
        detail: Also return every command name the addon advertises, and render_devices'
            "available_devices"; the server already refuses a command the addon does not
            advertise, so the names only explain such a refusal.
        tool_name: Resolve one tool name against this process. Use it before concluding a tool
            you cannot call does not exist.
        mounted_tools: Also list the tool names this process registered, paged. The counts say how
            many tools are mounted, never which, so a documented tool absent from a build is
            otherwise found only by calling it.
        tool_limit: How many tool names one page of "mounted_tools" carries.
        tool_offset: Where to resume that page, from the previous reply's "next_offset".

    Returns:
        Most fields are what the add-on reported about itself; a handshake that
        never completed raises instead of being rendered as one.
        "up_to_date" (bool), "protocol_version"/"expected_protocol_version", "addon_version",
        "capability_count" and "integrations_available" (per-provider, whether the addon advertises that
        integration's commands), "capabilities" (the command names, only with detail), "blender_version",
        "missing_commands"/"missing_parameters" (empty when current; non-empty means the installed add-on
        predates this server even though its protocol number matches, and must be reinstalled via
        "update_command" before those commands will work - they were not omitted from the project),
        "writable_output_roots" (empty when none), "render_devices" (Cycles Preferences on the Blender
        machine: "compute_device_type" backend, "NONE" for none, and the ticked "enabled_devices"; with
        detail also "available_devices"; null from an older add-on - a GPU request with no enabled
        device of that backend renders on the CPU), "file_roots"/"file_roots_enforced" (false:
        paths unconfined), "current_filepath", "session_id"/"session_epoch" (re-read capabilities if the pair
        moves), "session_indeterminate" (true: a swap aborted; most commands refused, don't save over the file),
        "source", "warning", "update_command", "after_install".
        "toolsets" describes THIS process, not the add-on: "mounted_bundles",
        "mounted_tool_count", "unmounted_bundles" (bundle to how many of its tools are absent here) and
        "unmounted_tool_count". Non-empty is normal, and means implemented tools this session cannot call.
        Three surfaces, three numbers, routinely confused: "capability_count" counts the add-on's
        Blender-side socket commands, a different and larger surface; "toolsets"."mounted_tool_count"
        counts the MCP tools this process registered; and "mounted_tools" (only with mounted_tools)
        enumerates those same registered tool names - "items" (one sorted page of names), "total",
        "offset", "limit", "returned_count", "truncated", "next_offset" (null on the last page).
        "tool_lookup" (only with tool_name): "mounted", "in_this_build", "bundles", "addon_command",
        and a "verdict" naming the remedy.

    Raises:
        ToolError: If Blender never answered the handshake, which is a transport failure and
            says nothing about which add-on version is installed - retry rather than reinstall.
            Also if the status could not be determined for any other reason.

    """
    try:
        return ok(
            await asyncio.to_thread(
                _collect_addon_status,
                detail=detail,
                tool_name=tool_name,
                mounted_tools=mounted_tools,
                tool_limit=tool_limit,
                tool_offset=tool_offset,
            )
        )
    except ToolError:
        raise
    except Exception as e:
        logger.error(f"Error checking addon status: {e}")
        raise ToolError(f"Error checking addon status: {e}") from e
