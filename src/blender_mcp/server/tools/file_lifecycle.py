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
        session_indeterminate, last_load_error, last_save_error, and libraries: total, by_type
        (PRESENT/MISSING), up to 10 records of session_uid, name, filepath, filepath_redacted,
        filepath_redaction_reason, is_relative, is_missing; list_libraries pages all. filepath
        is a // link (absolute if never saved) when the library resolves inside the file roots
        (none set: the .blend's folder); otherwise its leaf, with filepath_redacted=true and a
        filepath_redaction_reason: DIRECTORY, UNRESOLVABLE, OUTSIDE_ROOTS, TOO_LONG or
        UNSAFE_COMPONENT. A leaf is not a broken link: is_missing reports breakage, is_relative
        the stored form.

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
        filepath, scene_name, object_count (the reopened scene's own objects, the number
        list_scene_objects reports), datablock_object_count (every object datablock in the
        file, which a linked hierarchy makes larger), libraries (as get_session_info), session_id,
        session_epoch, capabilities_changed, rehandshake_required, discarded_unsaved_changes, note,
        and warnings when part of the swap report could not be read.

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
async def reset_session(ctx: Context, confirm_reset: bool = False) -> dict:
    """
    Replace the open database with an empty factory scene, discarding it and any unsaved work.

    Requires `confirm_reset=true`; discarding is this command's only effect. Invalidates the
    session like `open_shot`: `session_epoch` moves and capabilities may change, so
    re-handshake (`get_addon_status`) and treat every library `session_uid` held before this
    call as stale.

    Args:
        ctx: MCP request context.
        confirm_reset: Required to discard the open file and any unsaved work.

    Returns:
        filepath (None), scene_name, object_count (the scene's own objects),
        datablock_object_count (every object datablock in the file), libraries (empty), session_id,
        session_epoch, capabilities_changed, rehandshake_required, discarded_unsaved_changes,
        note, and warnings when part of the swap report could not be read.

    """
    return await call_blender("reset_session", {"confirm_reset": confirm_reset})


@mcp.tool()
async def link_canon_library(
    ctx: Context,
    filepath: str,
    collections: list[str] | None = None,
    objects: list[str] | None = None,
    world: Annotated[str | None, Field(min_length=1)] = None,
    as_override: bool = False,
    relative: bool = False,
    scene_name: str | None = None,
    detail: bool = False,
) -> dict:
    """
    Link named collections, objects and/or a World from a canon .blend into the open shot.

    Name at least one of `collections`, `objects` or `world` - names inside the library
    file, not handles into this session. A World is in no collection, so only `world`
    links it; it also becomes the scene's world, read-only here.
    `as_override=true` overrides each linked collection's hierarchy (see `create_override`)
    instead of instancing it, and is refused with `objects`. Refuses while Blender's script
    auto-execution preference is on.

    Args:
        ctx: MCP request context.
        filepath: The library .blend; absolute, ~, or // relative to a saved open file.
        collections: Collection names inside the library file to link.
        objects: Object names inside the library file to link.
        world: One World name inside the library file, linked and assigned as the scene's world.
        as_override: Override each linked collection's hierarchy instead of instancing it.
        relative: Store the library path relative to the open file; needs a saved session.
        scene_name: Local scene to link into; needed only when the file has more than one.
        detail: Page the objects brought in as records (session_uid, id_type, indirect and
            missing flags) instead of names; under as_override, each override's objects.

    Returns:
        library (as list_libraries), library_already_linked, scene_name, collections, objects,
        instanced_objects (total, by_type and one page of names, or records under detail;
        None under as_override), world, previous_world, overrides (as create_override).
        changed_objects names only the root objects - those no other linked object parents,
        e.g. a character's rig - not every member of the linked hierarchy.

    """
    return await call_blender(
        "link_canon_library",
        {
            "filepath": filepath,
            "collections": collections,
            "objects": objects,
            "world": world,
            "as_override": as_override,
            "relative": relative,
            "scene_name": scene_name,
            "detail": detail,
        },
    )


@mcp.tool()
async def create_override(
    ctx: Context, collection_uid: int, scene_name: str | None = None, detail: bool = False
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
        scene_name: Local scene to override into; needed only when the file has more than one.
        detail: List the override's objects as records (session_uid, editability, the linked
            original each references) instead of names.

    Returns:
        override (session_uid, is_editable, is_system_override, reference_uid,
        hierarchy_root_uid), scene_name, replaced_instances, and objects (total, by_type and one
        page of names, or records under detail). changed_objects names the override's root
        objects - those no other override object parents - not every member.

    """
    return await call_blender(
        "create_override",
        {"collection_uid": collection_uid, "scene_name": scene_name, "detail": detail},
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
        next_offset. filepath, its redaction flags and their codes are as get_session_info
        describes; a leaf is not a broken link, is_missing reports breakage.

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
    confirm_unlink: bool = False,
    purge_orphans: bool = False,
) -> dict:
    """
    Remove exactly the named libraries and every datablock linked from them.

    Requires `confirm_unlink=true`; touches only the libraries named by `library_uids`
    (`session_uid`s from `list_libraries`), what they link, and the local override objects
    made from what they link. Refuses an indirect library - one reached only through another
    library. `purge_orphans=true` also removes local datablocks this unlink leaves with no
    users; it never touches unrelated zero-user data.

    Args:
        ctx: MCP request context.
        library_uids: The libraries' session_uids to remove, 1 to 100.
        confirm_unlink: Required; this command deletes user data.
        purge_orphans: Also remove local datablocks this unlink leaves without users.

    Returns:
        removed_libraries (each as list_libraries, including filepath_redacted),
        already_removed_uids, removed_count, removed_by_type, removed_sample (up to 10 removed
        datablocks as name and id_type - a removed uid names nothing), purged_orphans,
        purged_by_type, other_libraries_removed.

    """
    return await call_blender(
        "unlink_libraries",
        {"library_uids": library_uids, "confirm_unlink": confirm_unlink, "purge_orphans": purge_orphans},
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

    A path that resolves inside the file roots (none set: the .blend's folder) is published
    as a // link; any other as its leaf, with `path_redacted: true` and a
    `path_redaction_reason` (DIRECTORY, UNRESOLVABLE, OUTSIDE_ROOTS, TOO_LONG,
    UNSAFE_COMPONENT), so the reply never carries this machine's layout outside them. A leaf
    is not a broken link; `verdict` (MISSING) reports breakage.

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
