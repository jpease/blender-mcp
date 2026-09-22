"""
Typed tools for the eleven file-lifecycle and linking addon commands.

Each tool forwards its parameters unchanged to the addon command of the same name. The
module is in `CORE_MODULES` because both `shot` and `asset` modes open and save files, and
no bundle may belong to both. Its descriptions count toward the mode byte ceilings in
`tests/server/test_bundles.py`, and `_documentation.py` registers hints for these tools.
"""

from typing import Annotated

from mcp.server.fastmcp import Context
from pydantic import Field

from ..app import mcp
from ._dispatch import call_blender


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
        session_uid, name, filepath, filepath_redacted, filepath_redaction_reason,
        is_relative, is_missing). A library path outside the configured file roots is
        reported as its leaf name with filepath_redacted=true - a display leaf, not a
        resolvable path and not a broken link; is_missing is what reports breakage, and
        is_relative is judged on the unredacted path.

    """
    return await call_blender("get_session_info", {})


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
        warnings when part of the swap report could not be read. Each library reports
        filepath_redacted=true when its path lies outside the configured file roots and was
        reduced to its leaf name; that is a display leaf, not a broken link, and is_missing
        is the field that reports breakage.

    """
    return await call_blender(
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
    write_provenance: bool = True,
    provenance_checksums: bool = False,
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
        write_provenance: Record add-on version, protocol, linked-library names and the datablocks
            this session authored into every local scene as the custom property "blender_mcp",
            shaped for C2PA (claim_generator/ingredients/actions). Pass false for a byte-stable
            canon publish.
        provenance_checksums: Additionally hash each linked library, which reads them from disk
            and needs configured file roots.

    Returns:
        filepath, saved_in_place, overwrote_existing, created_directory, compress, relative_remap,
        session_id, session_epoch, provenance_written, provenance_ingredients, and warnings when
        relative external file paths will not resolve from a new directory.

    """
    return await call_blender(
        "save_shot",
        {
            "filepath": filepath,
            "compress": compress,
            "relative_remap": relative_remap,
            "confirm_overwrite": confirm_overwrite,
            "create_directories": create_directories,
            "write_provenance": write_provenance,
            "provenance_checksums": provenance_checksums,
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
    return await call_blender("reset_session", {"confirm": confirm})


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
    return await call_blender(
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
    return await call_blender(
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
        libraries (each with session_uid, name, filepath, filepath_redacted,
        filepath_redaction_reason, is_relative, is_missing, version,
        needs_liboverride_resync, users, and datablocks: total, by_type and one page of
        names, or of records under detail), total, offset, limit, returned_count, truncated,
        next_offset. A library path outside the configured file roots is reported as its
        leaf name with filepath_redacted=true - a display leaf, not a resolvable path and
        not a broken link; is_missing is what reports breakage, and is_relative is judged on
        the unredacted path.

    """
    return await call_blender("list_libraries", {"limit": limit, "offset": offset, "detail": detail})


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
        library (as list_libraries, including filepath_redacted), datablocks (total, by_type
        and one page of names, or of records with their new session_uids under detail),
        note, and warnings when a linked datablock is now missing.

    """
    return await call_blender("reload_library", {"library_uid": library_uid, "detail": detail})


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
        library (as list_libraries, including filepath_redacted), datablocks (as
        reload_library), name_before, name_after, note, and warnings when a linked datablock
        is now missing.

    """
    return await call_blender(
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
        removed_libraries (each as list_libraries, including filepath_redacted),
        already_removed_uids, removed_count, removed_by_type, removed_uids, purged_orphans,
        purged_by_type, other_libraries_removed.

    """
    return await call_blender(
        "unlink_libraries",
        {"library_uids": library_uids, "confirm": confirm, "purge_orphans": purge_orphans},
    )


@mcp.tool()
async def inspect_delivery(
    ctx: Context,
    scene_name: str,
    limit: Annotated[int, Field(ge=1, le=200)] = 50,
    offset: Annotated[int, Field(ge=0)] = 0,
    hash_libraries: bool = False,
    max_hash_bytes: Annotated[int, Field(ge=1, le=8 * 1024**3)] = 268_435_456,
) -> dict:
    """
    Report whether this .blend would resolve on another machine, reference by reference.

    Lists linked libraries, file-backed images, fonts, sounds, simulation caches, the render
    output template and any remaining external path, each with a verdict: PACKED,
    RELATIVE_OK, ABSOLUTE, MISSING or UNSET. `portable` is only true when the file is saved,
    the scan was complete, and nothing is ABSOLUTE or MISSING - it is a proof, not a guess.

    A path outside the configured file roots - anything not `//`-relative - is reported as
    its leaf name with `path_redacted: true` and a `path_redaction_reason`, so the reply
    never carries this machine's directory layout; that leaf is a display name, not a
    resolvable path and not a broken link, and `verdict` (MISSING) is what reports breakage.

    Args:
        ctx: MCP request context.
        scene_name: Scene whose render output and rigid-body cache are included; every other
            source is file-wide.
        limit: Entries per page, 1 to 200.
        offset: Entries to skip.
        hash_libraries: Also SHA-256 each linked library on the returned page. This reads
            those .blend files from disk and requires configured file roots
            (BLENDERMCP_FILE_ROOTS); a library outside them is skipped with a reason.
        max_hash_bytes: Largest single library to hash.

    Returns:
        scene, blend_filepath, saved, portable, classes (per kind: total, unportable),
        entries (kind, name, path, path_redacted, path_redaction_reason, absolute, verdict,
        detail), limit, offset, total, truncated, next_offset, provenance, warnings,
        limitations.

    """
    return await call_blender(
        "inspect_delivery",
        {
            "scene_name": scene_name,
            "limit": limit,
            "offset": offset,
            "hash_libraries": hash_libraries,
            "max_hash_bytes": max_hash_bytes,
        },
    )
