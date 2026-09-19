"""
Typed tools for the ten file-lifecycle and linking addon commands.

Each tool forwards its parameters unchanged to the addon command of the same name. The
module is in `CORE_MODULES` because both `shot` and `asset` modes open and save files, and
no bundle may belong to both. Its descriptions count toward the mode byte ceilings in
`tests/server/test_bundles.py`, and `_documentation.py` registers hints for these tools.
"""

import asyncio

from mcp.server.fastmcp import Context

from ..app import mcp
from ..connection import get_blender_connection
from .envelope import ok

# Linking a whole set changes hundreds of objects; every name would sit in the agent's context.
CHANGED_OBJECTS_LIMIT = 50


async def _call(command: str, params: dict[str, object]) -> dict:
    """
    Dispatch one addon command and wrap its reply in the standard envelope.

    Args:
        command: Addon command name; identical to this module's tool name.
        params: JSON-serializable parameters, forwarded unchanged.

    Returns:
        dict: The `ok()` envelope. Addon failures, already free of filesystem paths,
        propagate and FastMCP turns them into `ToolError`.

    """
    result = await asyncio.to_thread(get_blender_connection().send_command, command, params)
    changed_objects = result.pop("changed_objects", []) if isinstance(result, dict) else []
    warnings = []
    if len(changed_objects) > CHANGED_OBJECTS_LIMIT:
        warnings.append(f"changed_objects lists the first {CHANGED_OBJECTS_LIMIT} of {len(changed_objects)} objects")
    return ok(result, changed_objects=changed_objects[:CHANGED_OBJECTS_LIMIT], warnings=warnings)


@mcp.tool()
async def get_session_info(ctx: Context) -> dict:
    """
    Report the open .blend, the session/epoch pair, the dirty flag, and linked libraries.

    Poll this after a result says `rehandshake_required`, or whenever `session_epoch` may
    have moved, before trusting a library `session_uid` read before that.

    Args:
        ctx: MCP request context.

    Returns:
        session_id, session_epoch, current_filepath (None if never saved), is_dirty,
        session_indeterminate, last_load_error, last_save_error, and libraries (each with
        session_uid, name, filepath, is_relative, is_missing).

    """
    return await _call("get_session_info", {})


@mcp.tool()
async def open_shot(
    ctx: Context,
    filepath: str,
    load_ui: bool = False,
    discard_unsaved: bool = False,
) -> dict:
    """
    Replace the open database with a .blend file, invalidating the session.

    Refuses when the open session has unsaved changes unless `discard_unsaved=true`, and
    refuses while Blender's script auto-execution preference is on. A successful open moves
    `session_epoch` and may change advertised capabilities: re-handshake (`get_addon_status`)
    before trusting a cached capability list, and treat every library `session_uid` held
    before this call as stale.

    Args:
        ctx: MCP request context.
        filepath: The .blend to open; absolute, ~, or // relative to a saved open file.
        load_ui: Also load the file's saved window layout. Default False.
        discard_unsaved: Required to discard unsaved changes in the open session.

    Returns:
        filepath, scene_name, object_count, libraries, session_id, session_epoch,
        capabilities_changed, rehandshake_required, discarded_unsaved_changes, note, and
        warnings when part of the swap report could not be read.

    """
    return await _call(
        "open_shot",
        {"filepath": filepath, "load_ui": load_ui, "discard_unsaved": discard_unsaved},
    )


@mcp.tool()
async def save_shot(
    ctx: Context,
    filepath: str | None = None,
    compress: bool = False,
    relative_remap: bool = False,
    confirm_overwrite: bool = False,
    create_directories: bool = False,
) -> dict:
    """
    Write the open database to disk, uncompressed by default; refuses to replace a file unconfirmed.

    `filepath=None` saves the open file in place; any other path saves a copy there, which
    becomes the open file. `confirm_overwrite=true` is required whenever the target already
    exists, including saving in place over the file's own previous contents. Does not move
    `session_epoch` and does not invalidate library `session_uid`s.

    Args:
        ctx: MCP request context.
        filepath: Where to save; omit to save the open file in place.
        compress: Write a compressed .blend. Default False: canon publishes stay uncompressed.
        relative_remap: Rewrite linked-library paths relative to the new location.
        confirm_overwrite: Required whenever the target .blend already exists.
        create_directories: Create the target's missing directories (inside the file roots when configured).

    Returns:
        filepath, saved_in_place, overwrote_existing, created_directory, compress, relative_remap,
        session_id, session_epoch, and warnings when relative external file paths will not resolve
        from a new directory.

    """
    return await _call(
        "save_shot",
        {
            "filepath": filepath,
            "compress": compress,
            "relative_remap": relative_remap,
            "confirm_overwrite": confirm_overwrite,
            "create_directories": create_directories,
        },
    )


@mcp.tool()
async def reset_session(ctx: Context, confirm: bool = False) -> dict:
    """
    Replace the open database with an empty factory scene, discarding it and any unsaved work.

    Requires `confirm=true`; discarding is this command's only effect. Invalidates the
    session like `open_shot`: `session_epoch` moves and capabilities may change, so
    re-handshake (`get_addon_status`) and treat every library `session_uid` held before this
    call as stale.

    Args:
        ctx: MCP request context.
        confirm: Required to discard the open file and any unsaved work.

    Returns:
        filepath (None), scene_name, object_count, libraries (empty), session_id,
        session_epoch, capabilities_changed, rehandshake_required, discarded_unsaved_changes,
        note, and warnings when part of the swap report could not be read.

    """
    return await _call("reset_session", {"confirm": confirm})


@mcp.tool()
async def link_canon_library(
    ctx: Context,
    filepath: str,
    collections: list[str] | None = None,
    objects: list[str] | None = None,
    as_override: bool = False,
    relative: bool = False,
    scene_uid: int | None = None,
) -> dict:
    """
    Link named collections and/or objects from a canon .blend into the open shot.

    Name at least one of `collections` or `objects` - names inside the library file, not
    handles into this session. `as_override=true` overrides each linked collection's
    hierarchy (see `create_override`) instead of instancing it, and is refused with
    `objects`. Refuses while Blender's script auto-execution preference is on.

    Args:
        ctx: MCP request context.
        filepath: The library .blend; absolute, ~, or // relative to a saved open file.
        collections: Collection names inside the library file to link.
        objects: Object names inside the library file to link.
        as_override: Override each linked collection's hierarchy instead of instancing it.
        relative: Store the library path relative to the open file; needs a saved session.
        scene_uid: Scene to link into; needed only when the file has more than one scene.

    Returns:
        library (as list_libraries), library_already_linked, scene_uid, collections, objects,
        overrides.

    """
    return await _call(
        "link_canon_library",
        {
            "filepath": filepath,
            "collections": collections,
            "objects": objects,
            "as_override": as_override,
            "relative": relative,
            "scene_uid": scene_uid,
        },
    )


@mcp.tool()
async def create_override(
    ctx: Context, collection_uid: int, scene_uid: int | None = None, detail: bool = False
) -> dict:
    """
    Make a linked collection's hierarchy editable in the shot by overriding it (Route C).

    `collection_uid` is a `session_uid` from `list_libraries` or `link_canon_library`,
    naming a linked collection that is not local and not already overridden. Refuses when
    the collection, or one inside it, is already overridden, and refuses while Blender's
    script auto-execution preference is on.

    Args:
        ctx: MCP request context.
        collection_uid: The linked collection's session_uid.
        scene_uid: Scene to override into; only needed when the file has more than one scene.
        detail: Also list the override's objects as records (session_uid, editability, the linked
            original each references); changed_objects names them either way.

    Returns:
        override (session_uid, is_editable, is_system_override, reference_uid,
        hierarchy_root_uid), scene_uid, replaced_instances, and objects (total, by_type).

    """
    return await _call(
        "create_override",
        {"collection_uid": collection_uid, "scene_uid": scene_uid, "detail": detail},
    )


@mcp.tool()
async def list_libraries(ctx: Context, limit: int = 25, offset: int = 0, detail: bool = False) -> dict:
    """
    List libraries linked into the open shot, a page at a time, with what each one links.

    Every `session_uid` reported here is valid only until the next file load or library
    reload; re-read it from this call rather than caching it across one of those.

    Args:
        ctx: MCP request context.
        limit: Libraries per page, 1 to 100.
        offset: Libraries to skip.
        detail: Page each library's linked datablocks as records - session_uid, id_type and
            whether the datablock is indirect or missing - instead of their names.

    Returns:
        libraries (each with session_uid, name, filepath, is_relative, is_missing, version,
        needs_liboverride_resync, users, and datablocks: total, by_type and one page of
        names, or of records under detail), total, offset, limit, returned_count, truncated,
        next_offset.

    """
    return await _call("list_libraries", {"limit": limit, "offset": offset, "detail": detail})


@mcp.tool()
async def reload_library(ctx: Context, library_uid: int, detail: bool = False) -> dict:
    """
    Re-read a library from its file, replacing every datablock it links in place.

    Every datablock this library links gets a fresh `session_uid`: discard any held from
    before this call and re-read them with `detail=true` or `list_libraries`. Refuses while
    Blender's script auto-execution preference is on.

    Args:
        ctx: MCP request context.
        library_uid: The library's session_uid, from list_libraries.
        detail: Page the reloaded datablocks as records, carrying the new session_uid of each.

    Returns:
        library, datablocks (total, by_type and one page of names, or of records with their
        new session_uids under detail), note, and warnings when a linked datablock is now
        missing.

    """
    return await _call("reload_library", {"library_uid": library_uid, "detail": detail})


@mcp.tool()
async def relocate_library(ctx: Context, library_uid: int, filepath: str, detail: bool = False) -> dict:
    """
    Point a library at another .blend and reload it, replacing every datablock it links.

    Like `reload_library`, but also changes which file the library loads from next time.
    Refuses an indirect library (reached only through another library), a file another
    library already links, and refuses while Blender's script auto-execution preference is
    on. A failed reload leaves the library pointed at its previous file, unchanged.

    Args:
        ctx: MCP request context.
        library_uid: The library's session_uid, from list_libraries.
        filepath: The replacement .blend; absolute, ~, or // relative to a saved open file.
        detail: Page the reloaded datablocks as records, carrying the new session_uid of each.

    Returns:
        library, datablocks (as reload_library), name_before, name_after, note, and warnings
        when a linked datablock is now missing.

    """
    return await _call(
        "relocate_library",
        {"library_uid": library_uid, "filepath": filepath, "detail": detail},
    )


@mcp.tool()
async def unlink_libraries(
    ctx: Context,
    library_uids: list[int],
    confirm: bool = False,
    purge_orphans: bool = False,
) -> dict:
    """
    Remove exactly the named libraries and every datablock linked from them.

    Requires `confirm=true`; touches only the libraries named by `library_uids`
    (`session_uid`s from `list_libraries`), what they link, and the local override objects
    made from what they link. Refuses an indirect library - one reached only through another
    library. `purge_orphans=true` also removes local datablocks this unlink leaves with no
    users; it never touches unrelated zero-user data.

    Args:
        ctx: MCP request context.
        library_uids: The libraries' session_uids to remove, 1 to 100.
        confirm: Required; this command deletes user data.
        purge_orphans: Also remove local datablocks this unlink leaves without users.

    Returns:
        removed_libraries, already_removed_uids, removed_count, removed_by_type,
        removed_uids, purged_orphans, purged_by_type, other_libraries_removed.

    """
    return await _call(
        "unlink_libraries",
        {"library_uids": library_uids, "confirm": confirm, "purge_orphans": purge_orphans},
    )
