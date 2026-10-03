"""Core/meta tools: addon status, integration status, and this session's toolsets."""

import asyncio
import logging
import os

from collections.abc import Sequence
from typing import Annotated, Literal

from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.session import ServerSession
from pydantic import Field

from ...addon_manager import EXPECTED_ADDON_PROTOCOL_VERSION, AddonHandshake
from ..app import mcp
from ..bundles import BUNDLES, MODES, TOOLSETS_ENV_VAR
from ..connection import BlenderTransportError, force_addon_handshake, get_blender_connection
from ..integrations import INTEGRATIONS, Provider, advertises, disabled_note, provider_of, withheld_providers
from ..mount_map import (
    CORE_BUNDLE,
    bundle_tool_names,
    bundles_providing,
    known_tool_names,
    unmounted_bundle_counts,
)
from ..toolsets_runtime import STARTUP_BUNDLES, bundle_catalog, ensure_bundles_registered, resolve_requested_bundles
from ._dispatch import send_blender_command
from .envelope import ok

logger = logging.getLogger("BlenderMCPServer")

_STATUS_COMMANDS: dict[Provider, str] = {
    "polyhaven": "get_polyhaven_status",
    "sketchfab": "get_sketchfab_status",
    "nd": "get_nd_status",
}

# How many names one `get_addon_status(tool_names=...)` resolves; the reply stays inside its budget.
_MAX_TOOL_NAMES = 50


def _calling_session(ctx: Context | None) -> ServerSession | None:
    """
    Read the session a tool call came from.

    Args:
        ctx: The call's context; None or one built outside a request when a tool is called
            directly, as the tests and `scripts/measure_reply_sizes.py` do.

    Returns:
        The session, or None, which every caller reads as the startup selection.

    """
    try:
        return getattr(ctx, "session", None)
    except ValueError:  # `Context.session` outside a request
        return None


def _mounted_tool_names(session: ServerSession | None = None) -> frozenset[str]:
    """
    Read the tool names the calling session lists.

    Args:
        session: The calling session, or None outside one, which reads the startup selection.

    Returns:
        The mounted names, read off the live app rather than re-derived from the environment,
        so the report cannot disagree with what the client can call. Integration tools a
        disabled checkbox withholds are included; the lookups report them as WITHHELD.

    """
    return mcp.session_tool_names(session)


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


# Where the startup selection is read, said once. "Restart the server" was the old remedy, and it
# fails under a client that respawns a killed server from the env it cached at load: the process
# came back with the old selection, and restarting Blender changed nothing either.
_REMOUNT_NOTE = (
    f"{TOOLSETS_ENV_VAR} is read once, when the MCP server process starts, from the env of this "
    "server's entry in the MCP client's config. Set it there and have the client reload its MCP "
    "configuration; a client that respawns the server from a cached config keeps the old value. "
    "Restarting Blender changes nothing: the server, not the add-on, decides what is mounted."
)


def _enable_call(bundles: Sequence[str]) -> str:
    """
    Spell the `manage_toolsets` call that lists bundles in the calling session.

    Args:
        bundles: The bundles to enable, in order; repeats collapse.

    Returns:
        The call, as an agent would write it.

    """
    names = ", ".join(f'"{bundle}"' for bundle in dict.fromkeys(bundles))
    return f'manage_toolsets(action="ENABLE", toolsets=[{names}])'


def _suggested_toolsets(bundles: Sequence[str]) -> str:
    """
    Spell the `BLENDER_MCP_TOOLSETS` value that adds bundles to this process's selection.

    Args:
        bundles: The bundles to add, in order; one already selected is not repeated.

    Returns:
        The full replacement value, since the variable is set whole, not appended to.

    """
    current = [name.strip() for name in (os.getenv(TOOLSETS_ENV_VAR) or "").split(",") if name.strip()]
    added = [bundle for bundle in dict.fromkeys(bundles) if bundle not in current]
    return ",".join([*current, *added])


def _toolset_payload(mounted: frozenset[str]) -> dict[str, object]:
    """
    Say which slice of the catalog the calling session lists, and what it left out.

    A client sees only the mounted tools, so without this an absent tool reads as one the
    project never implemented. Bundle membership is derived from the mounted names rather
    than by re-parsing the environment variable: that is the state the client is actually in.

    Args:
        mounted: The tool names the calling session lists.

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
            f"A session starts with core plus the {TOOLSETS_ENV_VAR} selection; "
            'manage_toolsets(action="ENABLE", toolsets=[...]) lists more in this session. A tool absent '
            "from this session may still be implemented - pass mounted_tools=True to this tool for the "
            "mounted names, one page at a time, or tool_name for a verdict on one name, rather than "
            "concluding it does not exist."
        ),
    }


def _lookup_status(
    tool_name: str,
    capabilities: Sequence[str],
    mounted: frozenset[str],
    withheld: frozenset[Provider],
) -> str:
    """
    Classify one tool name into which of five different situations the caller is in.

    Mounted; mounted but withheld because its integration is disabled; implemented but not
    listed in this session; absent from this server build while the connected add-on still serves the
    command; or unknown to both. They call for opposite responses - retry, tick a checkbox,
    change the environment, upgrade the server, fix the spelling - and a client's "unknown
    tool" error cannot tell them apart.

    Args:
        tool_name: The name asked about.
        capabilities: The command names the connected add-on advertises.
        mounted: The tool names the calling session lists.
        withheld: The integrations the handshake shows disabled.

    Returns:
        str: MOUNTED, WITHHELD, UNMOUNTED, NEWER_ADDON or UNKNOWN.

    """
    if tool_name in mounted:
        return "WITHHELD" if provider_of(tool_name) in withheld else "MOUNTED"
    if bundles_providing(tool_name):
        return "UNMOUNTED"
    return "NEWER_ADDON" if tool_name in capabilities else "UNKNOWN"


def _tool_lookup(
    tool_name: str,
    capabilities: Sequence[str],
    mounted: frozenset[str],
    withheld: frozenset[Provider] = frozenset(),
) -> dict[str, object]:
    """
    Answer, for one tool name, which situation the caller is in and what remedies it.

    Args:
        tool_name: The name asked about.
        capabilities: The command names the connected add-on advertises.
        mounted: The tool names the calling session lists.
        withheld: The integrations the handshake shows disabled.

    Returns:
        dict[str, object]: The verdict and the evidence behind it.

    """
    providers = bundles_providing(tool_name)
    integration = provider_of(tool_name)
    status = _lookup_status(tool_name, capabilities, mounted, withheld)
    if status == "WITHHELD" and integration is not None:
        verdict = f"Mounted, but withheld from the tool list and refused. {disabled_note(integration)}"
    elif status == "MOUNTED":
        verdict = "Mounted: callable in this session."
    elif status == "UNMOUNTED":
        verdict = (
            f"Implemented in this server build but not enabled in this session. Call {_enable_call(providers[:1])}"
            "; it lists the tools at once. A client that ignores tools/list_changed must mount it at startup "
            f"instead, with {TOOLSETS_ENV_VAR}={_suggested_toolsets(providers[:1])}. {_REMOUNT_NOTE}"
        )
    elif status == "NEWER_ADDON":
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
        "addon_command": tool_name in capabilities,
        "verdict": verdict,
    }


def _tool_lookups(
    tool_names: Sequence[str],
    capabilities: Sequence[str],
    mounted: frozenset[str],
    withheld: frozenset[Provider] = frozenset(),
) -> dict[str, object]:
    """
    Preflight every tool a request needs in one reply, with one combined remedy.

    Each name gets a status code rather than `_tool_lookup`'s prose, which repeated per name
    would not fit the reply budget; the remedies are stated once for the whole batch.

    Args:
        tool_names: The names asked about, in the caller's order; duplicates collapse.
        capabilities: The command names the connected add-on advertises.
        mounted: The tool names the calling session lists.
        withheld: The integrations the handshake shows disabled.

    Returns:
        dict[str, object]: `items` ({name, status, bundles} per name), `all_callable`,
        `toolsets_value` (the whole BLENDER_MCP_TOOLSETS value mounting every UNMOUNTED name, or
        None), `remount_note` (the `manage_toolsets` call enabling them and where the value is
        set instead; None when nothing is unmounted) and `withheld_notes` (integration to the
        checkbox that releases it).

    """
    items: list[dict[str, object]] = []
    needed_bundles: list[str] = []
    withheld_notes: dict[str, str] = {}
    for name in dict.fromkeys(tool_names):
        providers = bundles_providing(name)
        status = _lookup_status(name, capabilities, mounted, withheld)
        if status == "UNMOUNTED":
            needed_bundles.append(providers[0])
        elif status == "WITHHELD" and (integration := provider_of(name)) is not None:
            withheld_notes[integration] = disabled_note(integration)
        items.append({"name": name, "status": status, "bundles": list(providers)})
    return {
        "items": items,
        "all_callable": all(item["status"] == "MOUNTED" for item in items),
        "toolsets_value": _suggested_toolsets(needed_bundles) if needed_bundles else None,
        "remount_note": (
            f"Call {_enable_call(needed_bundles)}. A client that ignores tools/list_changed needs toolsets_value "
            f"at startup instead. {_REMOUNT_NOTE}"
            if needed_bundles
            else None
        ),
        "withheld_notes": withheld_notes,
    }


def _status_payload(
    result: AddonHandshake,
    *,
    mounted: frozenset[str],
    detail: bool,
    tool_name: str | None,
    tool_names: Sequence[str] | None,
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
        mounted: The tool names the calling session lists, which the mount fields describe.
        detail: Also carry the command names themselves.
        tool_name: A tool to resolve against the session's mount state, or None.
        tool_names: Tools to preflight together, or None.
        mounted_tools: Also carry a page of the tool names the session lists.
        tool_limit: How many names that page carries.
        tool_offset: Index of the first name on that page.

    Returns:
        dict[str, object]: The payload documented by `get_addon_status`.

    """
    payload: dict[str, object] = {
        "up_to_date": result.up_to_date,
        "protocol_version": result.protocol_version,
        "expected_protocol_version": EXPECTED_ADDON_PROTOCOL_VERSION,
        "addon_version": result.addon_version,
        "capability_count": len(result.capabilities),
        # A false one's tools are withheld from tools/list and refused (`app.py`).
        "integrations_available": {provider: advertises(result, provider) for provider in INTEGRATIONS},
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
        # What THIS session lists. The capability count above is the add-on's surface, which
        # is a different and much larger number; reading one for the other is how a mounted-
        # tool question gets answered with an add-on fact.
        "toolsets": _toolset_payload(mounted),
    }
    withheld = withheld_providers(result)
    if tool_name is not None:
        payload["tool_lookup"] = _tool_lookup(tool_name, result.capabilities, mounted, withheld=withheld)
    if tool_names is not None:
        payload["tool_lookups"] = _tool_lookups(tool_names, result.capabilities, mounted, withheld=withheld)
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
    *,
    mounted: frozenset[str],
    detail: bool,
    tool_name: str | None,
    tool_names: Sequence[str] | None,
    mounted_tools: bool,
    tool_limit: int,
    tool_offset: int,
) -> dict[str, object]:
    """
    Force a fresh handshake and render it as the status payload.

    Blocking: the handshake round-trip runs in a worker thread.

    Args:
        mounted: The tool names the calling session lists.
        detail: Include every advertised command name.
        tool_name: A tool to resolve against the session's mount state, or None.
        tool_names: Tools to preflight together, or None.
        mounted_tools: List the tool names the session lists.
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
        mounted=mounted,
        detail=detail,
        tool_name=tool_name,
        tool_names=tool_names,
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
    status = await _collect_integration_status(provider)
    if provider is not None:
        return ok(status)
    # Each provider's reply may carry the add-on's notice; nested a level down, `ok()` would not lift it.
    notices = [warning for reply in status.values() for warning in reply.get("warnings", [])]
    return ok(
        {name: {key: value for key, value in reply.items() if key != "warnings"} for name, reply in status.items()},
        warnings=notices,
    )


@mcp.tool()
async def get_addon_status(
    ctx: Context,
    detail: bool = False,
    tool_name: str | None = None,
    tool_names: Annotated[list[Annotated[str, Field(min_length=1)]], Field(min_length=1)] | None = None,
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
        tool_name: Resolve one tool name against this session. Use it before concluding a tool
            you cannot call does not exist.
        tool_names: Preflight every tool a request needs in one call (up to 50): one status per
            name and the one manage_toolsets call that enables all the unmounted ones.
        mounted_tools: Also list the tool names this session lists, paged. The counts say how
            many tools are mounted, never which, so a documented tool absent from a build is
            otherwise found only by calling it.
        tool_limit: How many tool names one page of "mounted_tools" carries.
        tool_offset: Where to resume that page, from the previous reply's "next_offset".

    Returns:
        Most fields are what the add-on reported about itself; a handshake that
        never completed raises instead of being rendered as one.
        "up_to_date" (bool), "protocol_version"/"expected_protocol_version", "addon_version",
        "capability_count" and "integrations_available" (per-provider; false: its tools are unlisted and
        refused), "capabilities" (the command names, only with detail), "blender_version",
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
        "toolsets" describes THIS session, not the add-on: "mounted_bundles",
        "mounted_tool_count", "unmounted_bundles" (bundle to how many of its tools are absent here) and
        "unmounted_tool_count". Non-empty is normal, and means implemented tools this session cannot call.
        Three surfaces, three numbers, routinely confused: "capability_count" counts the add-on's
        Blender-side socket commands, a different and larger surface; "toolsets"."mounted_tool_count"
        counts the MCP tools this session lists; and "mounted_tools" (only with mounted_tools)
        enumerates those same registered tool names - "items" (one sorted page of names), "total",
        "offset", "limit", "returned_count", "truncated", "next_offset" (null on the last page).
        "tool_lookup" (only with tool_name): "mounted", "in_this_build", "bundles", "addon_command",
        and a "verdict" naming the remedy.
        "tool_lookups" (only with tool_names): "items" ({"name", "status": MOUNTED, WITHHELD (its
        integration is disabled), UNMOUNTED (implemented, not enabled here), NEWER_ADDON (upgrade the
        server) or UNKNOWN, "bundles"}), "all_callable", "toolsets_value" (the whole
        BLENDER_MCP_TOOLSETS value mounting every UNMOUNTED name at startup, else null), "remount_note"
        (the manage_toolsets call enabling them now, and where that value is set) and "withheld_notes".

    Raises:
        ToolError: If Blender never answered the handshake, which is a transport failure and
            says nothing about which add-on version is installed - retry rather than reinstall.
            Also if the status could not be determined for any other reason, or if tool_names
            carries more than 50 names.

    """
    # Bounded here, not by a schema `max_length`: every advertised list cap is mirrored by an
    # add-on dispatch row (`list_caps.py`), and this tool never reaches the add-on.
    if tool_names is not None and len(tool_names) > _MAX_TOOL_NAMES:
        raise ToolError(f"tool_names carries {len(tool_names)} names; one call resolves at most {_MAX_TOOL_NAMES}")
    # Read on the event loop, the only thread that registers tools.
    mounted = _mounted_tool_names(_calling_session(ctx))
    try:
        return ok(
            await asyncio.to_thread(
                _collect_addon_status,
                mounted=mounted,
                detail=detail,
                tool_name=tool_name,
                tool_names=tool_names,
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


# A client that ignores tools/list_changed keeps the list it had; only this reply can say so.
_LIST_CHANGED_HINT = (
    "If the new tools are not in your tool list, your client did not act on tools/list_changed: "
    f"list tools again, or set {TOOLSETS_ENV_VAR} and restart the client."
)


def _toolsets_payload(session: ServerSession | None, *, changed: bool) -> dict[str, object]:
    """
    Describe one session's toolsets and what each available bundle would cost it.

    Args:
        session: The session asking, or None for the startup selection.
        changed: Whether the call being answered changed the session's tool list.

    Returns:
        dict[str, object]: The payload documented by `manage_toolsets`.

    """
    chosen = mcp.enabled_bundles(session)
    enabled = frozenset(STARTUP_BUNDLES) if chosen is None else chosen
    catalog = bundle_catalog()
    return {
        "enabled_bundles": [CORE_BUNDLE, *(name for name in BUNDLES if name in enabled)],
        "bundles": {name: {"enabled": name in enabled, **catalog[name]} for name in BUNDLES},
        "modes": {name: list(members) for name, members in MODES.items()},
        "listed_tool_count": len(mcp.session_tool_names(session)),
        "changed": changed,
    }


@mcp.tool()
async def manage_toolsets(
    ctx: Context,
    action: Literal["LIST", "ENABLE", "DISABLE"],
    toolsets: list[str] | None = None,
) -> dict:
    """
    List the tool bundles this server offers, or enable or disable some for this session only.

    A session starts with core plus the BLENDER_MCP_TOOLSETS selection. ENABLE lists a bundle's
    tools in this session at once and sends tools/list_changed; other sessions are unaffected.
    Every listed tool costs context on every turn, so read a bundle's catalog_bytes first and
    DISABLE what the task no longer needs. Core cannot be disabled.

    Args:
        ctx: MCP request context.
        action: LIST reports; ENABLE adds the named toolsets to this session; DISABLE removes them.
        toolsets: Mode names, bundle names, or "all" - the BLENDER_MCP_TOOLSETS vocabulary.
            Required for ENABLE and DISABLE.

    Returns:
        "enabled_bundles" (core first), "bundles" (each bundle's "enabled", "tool_count" and
        "catalog_bytes", the bytes its tools add to tools/list), "modes" (each mode's bundles),
        "listed_tool_count", and "changed" (whether this call changed this session's tool list).

    Raises:
        ToolError: If ENABLE or DISABLE names no toolset or an unknown one, DISABLE names core,
            or the call comes from outside a client session.

    """
    session = _calling_session(ctx)
    if action == "LIST":
        return ok(_toolsets_payload(session, changed=False))
    names = toolsets or []
    if not names:
        raise ToolError(f"{action} needs at least one toolset name in toolsets; LIST names them.")
    if action == "DISABLE" and CORE_BUNDLE in names:
        raise ToolError("core cannot be disabled: it holds manage_toolsets itself, so nothing could be enabled again.")
    if session is None:
        raise ToolError(f"{action} changes one client session's tool list, and this call came from no session.")
    try:
        requested = frozenset(resolve_requested_bundles(name for name in names if name != CORE_BUNDLE))
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    chosen = mcp.enabled_bundles(session)
    current = frozenset(STARTUP_BUNDLES) if chosen is None else chosen
    if action == "ENABLE":
        ensure_bundles_registered(requested)
        current |= requested
    else:
        current -= requested
    changed = await mcp.choose_bundles(session, current)
    warnings = [_LIST_CHANGED_HINT] if action == "ENABLE" and changed else None
    return ok(_toolsets_payload(session, changed=changed), warnings=warnings)
