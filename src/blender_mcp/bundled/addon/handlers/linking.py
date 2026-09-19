"""
Linking commands: link canon libraries into a shot, override them, and list, reload, relocate or unlink them.

Overrides use Route C, `override_hierarchy_create(..., do_fully_editable=True)`:
the other routes leave objects locked or make system overrides. Reload and
relocate use `lib.filepath` and `lib.reload()`, because the `wm.lib_*` operators
ignore `filepath` and the relocate operator renames the library.

Handles are `session_uid`s, because after an override a collection and its
override share a name. Only `link_canon_library` takes names, of datablocks
inside the library file. A uid lasts until the next file load or library reload.

`reload_library`, `relocate_library` and `unlink_libraries` bypass the mutation
transaction: a reload gives linked datablocks new uids, so a rollback would
delete them. Linking and overriding stay transacted.

The scripts check refuses while `use_scripts_auto_execute` is on, but it is not
the control for script execution: Blender runs drivers per the session's trust
flag (`-y`), which the preference does not reflect. Untrusted deployments would
need that flag checked, which is not implemented.

Blender puts absolute paths in its error text, so every `except` sanitizes with
`_operator_failure_message`.
"""

import os

from collections.abc import Iterable, Iterator, Sequence

import bpy

from ..candidates import MAX_CANDIDATES as _MAX_CANDIDATES
from ..candidates import describe_candidates as _candidates
from ..candidates import display_name as _display_name
from ..candidates import session_uid_of as _uid_of
from ..file_paths import canonical_path
from ..text_hygiene import client_safe_name_leaf, client_safe_text
from ..transaction import replacing_library_contents
from .file_lifecycle import (
    _checked_blend_path,
    _is_indirect_library,
    _library_summary,
    _operator_failure_message,
    _refuse_scripts_auto_execute,
    _require_bool,
)

DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100
# Per library or override: a canon library can link thousands of datablocks, and every
# record costs the agent's context for the rest of the session. The default reply reports
# the exact count and the counts by type beside this many names; `detail` asks for records.
MAX_LISTED_NAMES = 10
MAX_LISTED_DATABLOCKS = 100
MAX_LINK_NAMES = 100
MAX_UNLINK_UIDS = 100
MAX_REPORTED_UIDS = 500

_RELOAD_NOTE = (
    "Every datablock linked from this library now has a new session_uid; references read before this call "
    "name nothing. Re-read them with detail=true here, or with list_libraries, before the next command."
)


class LibraryFileNamesError(ValueError):
    """A requested name is not in the library file; raised inside `libraries.load` so no Library is created."""


def _require_uid(name: str, value: object) -> int:
    """
    Refuse a handle that is not an integer `session_uid`.

    A bool is refused, since `True == 1` would resolve the datablock with uid 1.

    Args:
        name: The parameter name, for the message.
        value: What the client sent.

    Returns:
        int: The uid.

    Raises:
        ValueError: If it is not a plain `int`.

    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer session_uid, as list_libraries reports it")
    return value


def _bounded_int(name: str, value: object, minimum: int, maximum: int | None) -> int:
    """
    Validate a pagination bound.

    Args:
        name: The parameter name.
        value: What the client sent.
        minimum: The smallest accepted value.
        maximum: The largest accepted value, or None for no upper bound.

    Returns:
        int: The value.

    Raises:
        ValueError: If it is not an `int` in range.

    """
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or (maximum is not None and value > maximum)
    ):
        upper = f" and at most {maximum}" if maximum is not None else ""
        raise ValueError(f"{name} must be an integer of at least {minimum}{upper}")
    return value


def _by_session_uid(datablocks: Iterable[object], uid: int, kind: str) -> object:
    """
    Resolve a datablock by `session_uid`, the only handle that survives a name collision.

    Args:
        datablocks: The `bpy.data` collection to search.
        uid: The uid.
        kind: What it is, for the message.

    Returns:
        object: The datablock.

    Raises:
        ValueError: When no datablock has that uid.

    """
    for datablock in datablocks:
        if getattr(datablock, "session_uid", None) == uid:
            return datablock
    raise ValueError(
        f"no {kind} has session_uid {uid} in the open session; session_uids change on every file load and "
        "library reload, so read a current one from list_libraries"
    )


def _scene(scene_uid: object) -> object:
    """
    Pick the scene from `bpy.data`, never from `bpy.context`, and never by guessing.

    Args:
        scene_uid: The client's `scene_uid`, or None when the file has one scene.

    Returns:
        object: The scene.

    Raises:
        ValueError: For an unknown uid, or no uid when the file has several scenes.

    """
    scenes = list(bpy.data.scenes)
    if scene_uid is not None:
        return _by_session_uid(scenes, _require_uid("scene_uid", scene_uid), "scene")
    if len(scenes) != 1:
        raise ValueError(f"the open file has {len(scenes)} scenes; pass scene_uid, one of: {_candidates(scenes)}")
    return scenes[0]


def _linked_entry(datablock: object) -> dict[str, object]:
    """
    Describe a datablock linked from a library.

    Args:
        datablock: The datablock.

    Returns:
        dict[str, object]: `session_uid`, `name` (display only), `id_type`, `is_library_indirect`, and
        `is_missing` - True for a placeholder the library's file no longer holds.

    """
    return {
        "session_uid": _uid_of(datablock),
        "name": _display_name(datablock),
        "id_type": str(getattr(datablock, "id_type", "")),
        "is_library_indirect": bool(getattr(datablock, "is_library_indirect", False)),
        "is_missing": bool(getattr(datablock, "is_missing", False)),
    }


def _override_entry(datablock: object) -> dict[str, object]:
    """
    Describe a datablock by the state that tells a Route C override from its linked original.

    Args:
        datablock: The datablock.

    Returns:
        dict[str, object]: `session_uid`, `name`, `is_override`, `is_editable`,
        `is_system_override` (None when not an override), `reference_uid` (the
        linked original) and `hierarchy_root_uid`.

    """
    override = getattr(datablock, "override_library", None)
    return {
        "session_uid": _uid_of(datablock),
        "name": _display_name(datablock),
        "is_override": override is not None,
        "is_editable": bool(getattr(datablock, "is_editable", False)),
        "is_system_override": bool(override.is_system_override) if override is not None else None,
        "reference_uid": _uid_of(getattr(override, "reference", None)),
        "hierarchy_root_uid": _uid_of(getattr(override, "hierarchy_root", None)),
    }


def summarize_type_counts(type_names: Iterable[str]) -> dict[str, int]:
    """
    Count how many items carry each type name, so a reply can state what is there.

    Args:
        type_names: One type name per item - an `id_type` for the datablocks a library
            links, a `bpy.data` collection name for the datablocks an unlink removed.

    Returns:
        dict[str, int]: Type name -> count, sorted by name so the reply is stable.

    """
    counts: dict[str, int] = {}
    for name in type_names:
        counts[name] = counts.get(name, 0) + 1
    return dict(sorted(counts.items()))


def _record_page(key: str, items: Sequence[object], describe: object, limit: int) -> dict[str, object]:
    """
    Publish one bounded page of a sub-list, without the resume offset no command accepts.

    The sub-lists this pages - a library's datablocks, an override's objects - belong to
    commands that take no datablock offset, so a `next_offset` here would name a parameter
    every one of them rejects. The exact `total` sits beside the page and `detail` is the
    other view; a caller that needs the rest narrows the request instead of resuming.

    Args:
        key: The page's result key.
        items: Every item; only the first `limit` are described.
        describe: Turns one item into its entry.
        limit: Entries this page may carry.

    Returns:
        dict[str, object]: `limit`, `returned_count`, `truncated`, and `<key>`.

    """
    shown = items[:limit]
    return {
        "limit": limit,
        "returned_count": len(shown),
        "truncated": len(items) > len(shown),
        key: [describe(item) for item in shown],  # type: ignore[operator]
    }


def _library_details(library: object) -> dict[str, object]:
    """
    Describe a library: `_library_summary`'s identity plus what a reload decision needs.

    Args:
        library: A `bpy.types.Library`.

    Returns:
        dict[str, object]: The summary fields, `version` (`[major, minor, file
        subversion]` of the Blender that last saved the file), `needs_liboverride_resync`
        and `users`.

    """
    return {
        **_library_summary(library),
        "version": [int(part) for part in getattr(library, "version", ())],
        "needs_liboverride_resync": bool(getattr(library, "needs_liboverride_resync", False)),
        "users": int(getattr(library, "users", 0)),
    }


def _linked_datablocks(library: object, *, detail: bool) -> dict[str, object]:
    """
    Report what a library links, from `Library.users_id`.

    `users_id` is missing from `bl_rna.properties` but exists, and it avoids
    walking `bpy.data`.

    Args:
        library: A `bpy.types.Library`.
        detail: Page the entries themselves instead of their names.

    Returns:
        dict[str, object]: `{"datablocks": {...}}` - the exact `total`, `by_type`,
        and one page: `names` by default, `records` (uid, name, id_type, indirect
        and missing flags) under `detail`.

    """
    items = list(getattr(library, "users_id", ()) or ())
    page = (
        _record_page("records", items, _linked_entry, MAX_LISTED_DATABLOCKS)
        if detail
        else _record_page("names", items, _display_name, MAX_LISTED_NAMES)
    )
    type_names = (str(getattr(item, "id_type", "")) for item in items)
    return {"datablocks": {"total": len(items), "by_type": summarize_type_counts(type_names), **page}}


def _missing_warnings(library: object) -> dict[str, object]:
    """
    Warn when a reload left linked datablocks as placeholders.

    `Library.is_missing` covers only the file. A file that lacks a linked
    datablock leaves the library looking fine and the datablock a placeholder
    with `is_missing` set.

    Args:
        library: A `bpy.types.Library`, just reloaded.

    Returns:
        dict[str, object]: `{"warnings": [...]}` with the count, or `{}` when nothing is missing.

    """
    missing = sum(1 for datablock in getattr(library, "users_id", ()) or () if getattr(datablock, "is_missing", False))
    if not missing:
        return {}
    if missing == 1:
        return {
            "warnings": [
                "1 datablock linked from this library is missing from its file; it is a placeholder until relinked"
            ]
        }
    return {
        "warnings": [
            f"{missing} datablocks linked from this library are missing from its file; "
            "they are placeholders until relinked"
        ]
    }


def _absolute(paths: Iterable[object]) -> tuple[str, ...]:
    """
    Keep only absolute path strings for the sanitizer.

    The sanitizer replaces plain substrings, so a relative `canon.blend` would
    also mangle the name in `library 'canon.blend'`.

    Args:
        paths: Candidate paths.

    Returns:
        tuple[str, ...]: The absolute ones.

    """
    return tuple(path for path in paths if isinstance(path, str) and os.path.isabs(path))


def _library_paths(library: object) -> tuple[str, ...]:
    """
    Every absolute form of a library's path that Blender may put in an error.

    `reload()` reports a `//` path expanded; the canonical form covers a
    symlinked directory.

    Args:
        library: A `bpy.types.Library`.

    Returns:
        tuple[str, ...]: The stored, expanded and canonical forms that are absolute.

    """
    # An absolute name too: a quoted-name match can stop at an apostrophe inside
    # it and leave a relative tail.
    name = getattr(library, "name", "")
    raw = str(getattr(library, "filepath", "") or "")
    if not raw:
        return _absolute((name,))
    expanded = bpy.path.abspath(raw)
    return _absolute((raw, expanded, canonical_path(expanded), name))


def _library(uid: object) -> object:
    """
    Resolve a library handle.

    Args:
        uid: The client's `library_uid`.

    Returns:
        object: The `bpy.types.Library`.

    """
    return _by_session_uid(bpy.data.libraries, _require_uid("library_uid", uid), "library")


def _reload(library: object, command: str, known_paths: tuple[str, ...]) -> None:
    """
    Reload a library, flagged for the transaction handlers and sanitized on failure.

    The flag lets `blend_import_post` disarm an open transaction, whose rollback
    would otherwise delete the reloaded datablocks. A failed reload changes no uids.

    Args:
        library: The library.
        command: The command name, for the message.
        known_paths: Every absolute path the call held.

    Raises:
        RuntimeError: When Blender could not reload it; the text carries no path.

    """
    try:
        with replacing_library_contents():
            library.reload()  # type: ignore[attr-defined]
    except Exception as exc:
        raise RuntimeError(_operator_failure_message(command, exc, known_paths)) from exc


def _library_file_names(parameter: str, value: object) -> list[str]:
    """
    Validate names of datablocks inside a library file.

    Args:
        parameter: `collections` or `objects`.
        value: What the client sent; None means none of that type.

    Returns:
        list[str]: The names, de-duplicated in order.

    Raises:
        ValueError: If it is not a bounded list of non-empty strings.

    """
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_LINK_NAMES:
        raise ValueError(f"{parameter} must be a list of at most {MAX_LINK_NAMES} names inside the library file")
    if not all(isinstance(name, str) and name for name in value):
        raise ValueError(f"every entry in {parameter} must be a non-empty name")
    return list(dict.fromkeys(value))


def _refuse_absent_names(data_from: object, collections: list[str], objects: list[str]) -> None:
    """
    Refuse, inside the `libraries.load` block, a name the file does not contain.

    Raising inside the block creates no `Library`; an absent name left to the
    block's exit would still create one.

    Args:
        data_from: The file's contents, as `libraries.load` yields them.
        collections: Requested collection names.
        objects: Requested object names.

    Raises:
        LibraryFileNamesError: Naming the absent entries (the client's own strings).

    """
    absent = [name for name in collections if name not in data_from.collections]  # type: ignore[attr-defined]
    absent += [name for name in objects if name not in data_from.objects]  # type: ignore[attr-defined]
    if absent:
        shown = ", ".join(repr(client_safe_text(name)) for name in absent[:_MAX_CANDIDATES])
        raise LibraryFileNamesError(f"the library file does not contain: {shown}; nothing was linked")


def _instance_parents() -> list[object]:
    """
    Every local collection that can hold an instance: scene roots and local collections.

    Returns:
        list[object]: The candidate parents.

    """
    roots = [scene.collection for scene in bpy.data.scenes]
    return [*roots, *(c for c in bpy.data.collections if getattr(c, "library", None) is None)]


def _has_child(parent: object, child: object) -> bool:
    """
    Test collection membership by uid, never by name.

    Args:
        parent: The collection.
        child: The candidate member.

    Returns:
        bool: Whether `child` is a direct child.

    """
    return any(item.session_uid == child.session_uid for item in parent.children)  # type: ignore[attr-defined]


def _refuse_unoverridable(collection: object) -> None:
    """
    Refuse a collection Route C must not be asked to override.

    Args:
        collection: A `bpy.types.Collection`.

    Raises:
        ValueError: For a collection that is not linked, is already overridden, or
            contains a collection that is.

    """
    uid = collection.session_uid  # type: ignore[attr-defined]
    if getattr(collection, "library", None) is None:
        raise ValueError(
            f"collection session_uid {uid} is not linked from a library; create_override takes a linked collection "
            "(list_libraries lists them), not a local collection or an existing override"
        )
    existing = [c for c in bpy.data.collections if _uid_of(getattr(c.override_library, "reference", None)) == uid]
    if existing:
        raise ValueError(f"collection session_uid {uid} is already overridden by {_candidates(existing)}")
    # Overriding a collection overrides every collection inside it, so an inner
    # collection with its own override would end up overridden twice.
    inner = {_uid_of(child) for child in getattr(collection, "children_recursive", ())}
    inner_overrides = [
        c for c in bpy.data.collections if _uid_of(getattr(c.override_library, "reference", None)) in inner
    ]
    if inner_overrides:
        raise ValueError(
            f"collection session_uid {uid} contains a collection that is already overridden, by "
            f"{_candidates(inner_overrides)}; remove that override first, or override only the inner collection"
        )


def _override_hierarchy(
    collection: object, scene: object, unlinked: list[tuple[object, object]], *, detail: bool
) -> dict[str, object]:
    """
    Override one linked collection's hierarchy with Route C, replacing its instances.

    Route C adds the override beside each instance and leaves the instance, so
    the asset would draw twice. Replaced instances are recorded in `unlinked` so a
    caller can restore them if a later step fails. The linked collection stays:
    the override references it. Call `_refuse_unoverridable` first.

    Args:
        collection: A linked `bpy.types.Collection`.
        scene: The scene, from `bpy.data`.
        unlinked: `(parent, child)` pairs this call unlinks are appended here.
        detail: Also page the override's objects as records; their names are in
            `changed_objects` either way.

    Returns:
        dict[str, object]: `override` (see `_override_entry`), `scene_uid`,
        `replaced_instances`, and `objects` with their exact `total` and
        `by_type`, plus a page of `records` under `detail`.

    Raises:
        RuntimeError: When Blender raised or created no override.

    """
    uid = collection.session_uid  # type: ignore[attr-defined]
    parents = [parent for parent in _instance_parents() if _has_child(parent, collection)]
    try:
        # Returns None, rather than raising, for a collection it cannot override.
        override = collection.override_hierarchy_create(  # type: ignore[attr-defined]
            scene,
            scene.view_layers[0],  # type: ignore[attr-defined]
            do_fully_editable=True,
        )
    except Exception as exc:
        known = _library_paths(collection.library)  # type: ignore[attr-defined]
        raise RuntimeError(_operator_failure_message("create_override", exc, known)) from exc
    if override is None:
        raise RuntimeError(f"create_override failed: Blender created no override for collection session_uid {uid}")
    replaced = 0
    for parent in parents:
        if _has_child(parent, override):
            parent.children.unlink(collection)  # type: ignore[attr-defined]
            unlinked.append((parent, collection))
            replaced += 1
    objects = list(override.all_objects)
    type_names = (str(getattr(obj, "id_type", "")) for obj in objects)
    listed: dict[str, object] = {"total": len(objects), "by_type": summarize_type_counts(type_names)}
    if detail:
        listed.update(_record_page("records", objects, _override_entry, MAX_LISTED_DATABLOCKS))
    return {
        "override": _override_entry(override),
        "scene_uid": _uid_of(scene),
        "replaced_instances": replaced,
        "objects": listed,
    }


def _override_object_names(reports: list[dict[str, object]]) -> list[str]:
    """
    Name every object inside the overrides the reports describe, unbounded, for `changed_objects`.

    Args:
        reports: `_override_hierarchy` reports.

    Returns:
        list[str]: Sorted, distinct object names.

    """
    names: set[str] = set()
    for report in reports:
        uid = report["override"]["session_uid"]  # type: ignore[index]
        names.update(obj.name for obj in _by_session_uid(bpy.data.collections, uid, "collection").all_objects)  # type: ignore[attr-defined]
    return sorted(names)


def _refuse_nested_requests(collections: list) -> None:
    """
    Refuse a request that names a collection and a collection inside it.

    The per-collection check would refuse it too, but by naming an override this
    request built and rolled back, which the client cannot act on.

    Args:
        collections: The requested linked collections.

    Raises:
        ValueError: Naming the inner and the outer collection.

    """
    for outer in collections:
        inner_uids = {_uid_of(child) for child in getattr(outer, "children_recursive", ())}
        for inner in collections:
            if _uid_of(inner) in inner_uids:
                raise ValueError(
                    f"{_candidates([inner])} is inside {_display_name(outer)!r}, also requested; "
                    "request only the outermost collection"
                )


def _override_all(collections: list, scene: object, *, detail: bool) -> list[dict[str, object]]:
    """
    Override several linked collections, all or nothing for the user's placement.

    On failure, instances unlinked for earlier collections are re-linked: the
    transaction removes the new overrides but not `children` links, so the
    user's placement would be lost. Re-linking appends, so sibling order may change.

    Args:
        collections: Linked `bpy.types.Collection`s.
        scene: The scene, from `bpy.data`.
        detail: Passed to `_override_hierarchy`.

    Returns:
        list[dict[str, object]]: One `_override_hierarchy` report per collection.

    """
    _refuse_nested_requests(collections)
    for collection in collections:
        _refuse_unoverridable(collection)
    unlinked: list[tuple[object, object]] = []
    reports = []
    try:
        for collection in collections:
            # Again, just before its own override: overriding a parent overrides every collection
            # inside it, so a nested request would otherwise build a second copy (measured, section K).
            _refuse_unoverridable(collection)
            reports.append(_override_hierarchy(collection, scene, unlinked, detail=detail))
    except Exception:
        for parent, child in reversed(unlinked):
            if not _has_child(parent, child):
                parent.children.link(child)  # type: ignore[attr-defined]
        raise
    return reports


def _link_into(members: object, items: list[object]) -> None:
    """
    Instance linked datablocks in the scene root unless already there.

    Blender does not save a linked datablock nobody uses, so an uninstanced link
    would vanish at the next save.

    Args:
        members: `scene.collection.children` or `.objects`.
        items: The linked datablocks.

    """
    for item in items:
        if not any(member.session_uid == item.session_uid for member in members):  # type: ignore[attr-defined]
            members.link(item)  # type: ignore[attr-defined]


def _iter_ids() -> Iterator[tuple[str, object]]:
    """
    Walk every datablock in every `bpy.data` ID collection, once.

    Skips `all_ids`-style aggregates of type `ID`, which repeat every datablock
    and would hand `batch_remove` duplicates.

    Yields:
        tuple[str, object]: `(collection name, datablock)` pairs.

    """
    for prop in bpy.data.bl_rna.properties:
        aggregate = getattr(getattr(prop, "fixed_type", None), "identifier", None) == "ID"
        if prop.type == "COLLECTION" and not aggregate:
            for datablock in getattr(bpy.data, prop.identifier, ()):
                yield prop.identifier, datablock


def _census() -> dict[int, tuple[str, int]]:
    """
    Snapshot every datablock's uid, collection and user count.

    Returns:
        dict[int, tuple[str, int]]: uid -> `(collection name, users)`.

    """
    return {
        datablock.session_uid: (name, int(getattr(datablock, "users", 0)))  # type: ignore[attr-defined]
        for name, datablock in _iter_ids()
        if getattr(datablock, "session_uid", None) is not None
    }


def compute_orphaned(before: dict[int, tuple[str, int]], current: dict[int, tuple[str, int]]) -> list[int]:
    """
    Decide which datablocks an unlink orphaned, from the census before it and the one after.

    Not `orphans_purge`, which also deletes the user's unrelated zero-user datablocks: an
    orphan of this unlink is a datablock that had users before it and has none now.

    Args:
        before: uid -> `(collection name, users)` read before the removal.
        current: uid -> `(collection name, users)` for the datablocks still eligible now;
            the caller has already dropped libraries, linked data and fake users.

    Returns:
        list[int]: The orphaned uids, in `current`'s order.

    """
    return [uid for uid, (_name, users) in current.items() if users == 0 and before.get(uid, ("", 0))[1] > 0]


def _newly_orphaned(before: dict[int, tuple[str, int]]) -> list[tuple[str, object]]:
    """
    Find local datablocks that this unlink left with no users.

    Args:
        before: `_census()` read before the removal.

    Returns:
        list[tuple[str, object]]: `(collection name, datablock)` to remove.

    """
    candidates: dict[int, tuple[str, object]] = {}
    current: dict[int, tuple[str, int]] = {}
    for name, datablock in _iter_ids():
        uid = getattr(datablock, "session_uid", None)
        if name == "libraries" or not isinstance(uid, int) or getattr(datablock, "library", None) is not None:
            continue
        # A fake user keeps a datablock alive deliberately, so it is never this unlink's orphan.
        if getattr(datablock, "use_fake_user", False):
            continue
        candidates[uid] = (name, datablock)
        current[uid] = (name, int(getattr(datablock, "users", 0)))
    return [candidates[uid] for uid in compute_orphaned(before, current)]


def _libraries_to_unlink(library_uids: object, confirm: bool) -> list[object]:
    """
    Validate an unlink request and resolve every library before anything is removed.

    Args:
        library_uids: The client's uid list.
        confirm: The validated confirmation flag.

    Returns:
        list[object]: The libraries, de-duplicated in request order.

    Raises:
        ValueError: For a malformed list, a missing confirmation, an unknown uid
            or an indirect library; nothing has been removed.

    """
    if not isinstance(library_uids, list) or not 0 < len(library_uids) <= MAX_UNLINK_UIDS:
        raise ValueError(f"library_uids must be a list of 1 to {MAX_UNLINK_UIDS} session_uids from list_libraries")
    uids = list(dict.fromkeys(_require_uid("library_uids entry", uid) for uid in library_uids))
    if not confirm:
        raise ValueError(
            "unlink_libraries removes each named library and every datablock linked from it; pass confirm=true"
        )
    libraries = [_library(uid) for uid in uids]
    indirect = [library for library in libraries if _is_indirect_library(library)]
    if indirect:
        raise ValueError(
            f"indirect libraries are reached through another library; unlink that one: {_candidates(indirect)}"
        )
    return libraries


def _remove_libraries(
    libraries: list[object], known_paths: tuple[str, ...]
) -> tuple[list[dict[str, object]], list[int]]:
    """
    Remove each library, re-resolving it by uid first so `remove()` never reaches a freed one.

    Summaries are read first, because a removed library cannot be read.

    Args:
        libraries: The resolved libraries.
        known_paths: Every absolute path the request held, for the sanitizer.

    Returns:
        tuple: Summaries of the libraries removed, and uids already gone when their turn came.

    Raises:
        RuntimeError: When Blender failed; the message lists the uids already removed.

    """
    summaries = [_library_summary(library) for library in libraries]
    removed: list[dict[str, object]] = []
    already_removed: list[int] = []
    for summary in summaries:
        uid = summary["session_uid"]
        current = next((library for library in bpy.data.libraries if library.session_uid == uid), None)
        if current is None:
            already_removed.append(uid)  # type: ignore[arg-type]
            continue
        try:
            bpy.data.libraries.remove(current)
        except Exception as exc:
            done = [entry["session_uid"] for entry in removed]
            message = _operator_failure_message("unlink_libraries", exc, known_paths)
            raise RuntimeError(f"{message} (libraries already removed: {done})") from exc
        removed.append(summary)
    return removed, already_removed


def _purge_newly_orphaned(before: dict[int, tuple[str, int]], known_paths: tuple[str, ...]) -> list[str]:
    """
    Remove the local datablocks this unlink orphaned, with `bpy.data.batch_remove`.

    Args:
        before: `_census()` read before the removal.
        known_paths: Every absolute path the request held, for the sanitizer.

    Returns:
        list[str]: One collection name per datablock removed.

    Raises:
        RuntimeError: When Blender failed; the text carries no path.

    """
    orphans = _newly_orphaned(before)
    if orphans:
        try:
            bpy.data.batch_remove(ids=[datablock for _name, datablock in orphans])  # type: ignore[arg-type]
        except Exception as exc:
            raise RuntimeError(_operator_failure_message("unlink_libraries purge", exc, known_paths)) from exc
    return [name for name, _datablock in orphans]


class LinkingHandlersMixin:
    """Link, override, list, reload, relocate and unlink canon libraries."""

    @staticmethod
    def link_canon_library(
        filepath: object,
        *,
        collections: object = None,
        objects: object = None,
        as_override: object = False,
        relative: object = False,
        scene_uid: object = None,
    ) -> dict[str, object]:
        """
        Link datablocks from a canon `.blend` into the open shot, instanced or overridden.

        Never `create_liboverrides=True`, whose objects stay locked; `as_override`
        uses Route C, as `create_override` does. Without it each datablock is
        instanced in the scene root. Transacted: a failure removes the `Library`
        and everything it linked.

        Args:
            filepath: The library `.blend`; absolute, `~`, or `//` relative to a saved open file. Roots enforced.
            collections: Names of collections inside the library file, which have
                no uid yet. None means none.
            objects: Names of objects inside the library file. None means none.
            as_override: Override each linked collection (Route C). Refused with `objects`.
            relative: Store the library path relative to the open file; refused in a never-saved session,
                where Blender would silently store it absolute.
            scene_uid: The scene to instance or override into; optional when the file has one scene.

        Returns:
            dict[str, object]: `library` (`_library_summary` plus `version`,
            `needs_liboverride_resync`, `users`), `library_already_linked`,
            `scene_uid`, `collections` / `objects` linked (uid, name, id_type),
            and `overrides` (one `create_override` report per collection when
            `as_override`).

        Raises:
            ValueError: When the request is refused; nothing was linked.
            LibraryFileNamesError: A `ValueError` naming requested datablocks the file lacks.
            RuntimeError: When Blender failed; the text carries no path.

        """
        as_override = _require_bool("as_override", as_override)
        relative = _require_bool("relative", relative)
        collection_names = _library_file_names("collections", collections)
        object_names = _library_file_names("objects", objects)
        if not (collection_names or object_names):
            raise ValueError("name at least one datablock inside the library file: collections or objects")
        if as_override and object_names:
            raise ValueError("as_override overrides collection hierarchies; link objects without it")
        if relative and not bpy.data.filepath:
            raise ValueError("relative needs a saved open file to be relative to; save_shot first or pass false")
        canonical = _checked_blend_path(filepath, must_exist=True)
        _refuse_scripts_auto_execute("link_canon_library")
        scene = _scene(scene_uid)
        libraries_before = {library.session_uid for library in bpy.data.libraries}
        try:
            with bpy.data.libraries.load(canonical, link=True, relative=relative) as (data_from, data_to):  # pyright: ignore[reportGeneralTypeIssues]  # libraries.load() is a context manager at runtime
                _refuse_absent_names(data_from, collection_names, object_names)
                data_to.collections = list(collection_names)
                data_to.objects = list(object_names)
        except LibraryFileNamesError:
            raise
        except Exception as exc:
            raise RuntimeError(_operator_failure_message("link_canon_library", exc, (filepath, canonical))) from exc
        linked_collections, linked_objects = list(data_to.collections), list(data_to.objects)
        linked = [*linked_collections, *linked_objects]
        library = getattr(linked[0], "library", None) if None not in linked else None
        if library is None:
            raise RuntimeError("link_canon_library failed: Blender did not link every requested datablock")
        overrides = []
        if as_override:
            overrides = _override_all(list(linked_collections), scene, detail=False)
            changed_objects = _override_object_names(overrides)
        else:
            _link_into(scene.collection.children, linked_collections)  # type: ignore[attr-defined]
            _link_into(scene.collection.objects, linked_objects)  # type: ignore[attr-defined]
            members = [obj for collection in linked_collections for obj in collection.all_objects]  # type: ignore[attr-defined]
            changed_objects = sorted({obj.name for obj in [*members, *linked_objects]})  # type: ignore[attr-defined]
        return {
            "changed_objects": changed_objects,
            "library": _library_details(library),
            "library_already_linked": library.session_uid in libraries_before,
            "scene_uid": _uid_of(scene),
            "collections": [_linked_entry(item) for item in linked_collections],
            "objects": [_linked_entry(item) for item in linked_objects],
            "overrides": overrides,
        }

    @staticmethod
    def create_override(
        collection_uid: object, *, scene_uid: object = None, detail: object = False
    ) -> dict[str, object]:
        """
        Make a linked collection's hierarchy editable in the shot (Route C).

        `do_fully_editable` defaults to False, which makes system overrides.
        Refuses a local collection, an override, and a collection already
        overridden, which would get a second copy.

        Args:
            collection_uid: The **linked** collection's `session_uid`.
            scene_uid: The scene to override into; optional when the file has one scene.
            detail: Also list the override's objects as records; `changed_objects`
                names them either way.

        Returns:
            dict[str, object]: `override` (`session_uid`, `is_system_override`,
            `reference_uid`, `hierarchy_root_uid`, ...), `scene_uid`,
            `replaced_instances`, and `objects` counted by type.

        """
        uid = _require_uid("collection_uid", collection_uid)
        collection = _by_session_uid(bpy.data.collections, uid, "collection")
        _refuse_scripts_auto_execute("create_override")
        report = _override_all([collection], _scene(scene_uid), detail=_require_bool("detail", detail))[0]
        return {**report, "changed_objects": _override_object_names([report])}

    @staticmethod
    def list_libraries(
        *, limit: object = DEFAULT_PAGE_SIZE, offset: object = 0, detail: object = False
    ) -> dict[str, object]:
        """
        List linked libraries, a page at a time, with what each one links.

        Args:
            limit: Libraries per page, 1 to `MAX_PAGE_SIZE`.
            offset: Libraries to skip.
            detail: Page each library's datablocks as records instead of names.

        Returns:
            dict[str, object]: `libraries` (each `_library_summary` plus
            `version`, `needs_liboverride_resync`, `users`, and `datablocks` -
            see `_linked_datablocks`), `total`, `offset`, `limit`,
            `returned_count`, `truncated`, `next_offset`.

        """
        limit = _bounded_int("limit", limit, 1, MAX_PAGE_SIZE)
        offset = _bounded_int("offset", offset, 0, None)
        detail = _require_bool("detail", detail)
        libraries = list(bpy.data.libraries)
        page = libraries[offset : offset + limit]
        remaining = offset + len(page) < len(libraries)
        return {
            "libraries": [{**_library_details(lib), **_linked_datablocks(lib, detail=detail)} for lib in page],
            "total": len(libraries),
            "offset": offset,
            "limit": limit,
            "returned_count": len(page),
            "truncated": remaining,
            "next_offset": offset + len(page) if remaining else None,
        }

    @staticmethod
    def reload_library(library_uid: object, *, detail: object = False) -> dict[str, object]:
        """
        Re-read a library from its file with `lib.reload()`.

        Every datablock linked from it gets a new `session_uid`, so earlier
        references are stale. File roots are not enforced: the path is already
        in the open file.

        Args:
            library_uid: The library's `session_uid`.
            detail: Page the reloaded datablocks as records, with their new uids.

        Returns:
            dict[str, object]: `library`, `datablocks` (see `_linked_datablocks`), and `note`.

        """
        library = _library(library_uid)
        detail = _require_bool("detail", detail)
        _refuse_scripts_auto_execute("reload_library")
        _reload(library, "reload_library", _library_paths(library))
        return {
            "library": _library_details(library),
            **_linked_datablocks(library, detail=detail),
            "note": _RELOAD_NOTE,
            **_missing_warnings(library),
        }

    @staticmethod
    def relocate_library(library_uid: object, filepath: object, *, detail: object = False) -> dict[str, object]:
        """
        Point a library at another `.blend` and reload it: `lib.filepath = <canonical>; lib.reload()`.

        The path is checked against the roots and stored canonical. A failed
        reload restores the previous `filepath`. Refuses a file another library
        already links. Invalidates uids like `reload_library`. Reports
        `name_before` and `name_after` so a client need not assume the name is
        unchanged.

        Args:
            library_uid: The library's `session_uid`.
            filepath: The replacement `.blend`.
            detail: Page the reloaded datablocks as records, with their new uids.

        Returns:
            dict[str, object]: As `reload_library`, plus `name_before` and `name_after`.

        Raises:
            ValueError: When the request is refused; nothing changed.
            RuntimeError: When the reload failed; the library still points at its previous file.

        """
        library = _library(library_uid)
        detail = _require_bool("detail", detail)
        if _is_indirect_library(library):
            raise ValueError(
                "that library is indirect - reached only through another library, which re-derives its path - so "
                "relocating it breaks the parent's link; relocate the parent library instead"
            )
        canonical = _checked_blend_path(filepath, must_exist=True)
        for other in bpy.data.libraries:
            if other.session_uid != library.session_uid and canonical in _library_paths(other):
                raise ValueError(
                    f"that file is already linked as library session_uid {other.session_uid}; reload or unlink "
                    "that library instead"
                )
        # Before the assignment, so a refusal leaves the library untouched.
        _refuse_scripts_auto_execute("relocate_library")
        name_before = client_safe_name_leaf(library.name)  # type: ignore[attr-defined]
        previous = library.filepath  # type: ignore[attr-defined]
        known = _absolute((filepath, canonical, *_library_paths(library)))
        library.filepath = canonical  # type: ignore[attr-defined]
        try:
            _reload(library, "relocate_library", known)
        except RuntimeError:
            library.filepath = previous  # type: ignore[attr-defined]
            raise
        return {
            "library": _library_details(library),
            **_linked_datablocks(library, detail=detail),
            "name_before": name_before,
            "name_after": client_safe_name_leaf(library.name),  # type: ignore[attr-defined]
            "note": _RELOAD_NOTE,
            **_missing_warnings(library),
        }

    @staticmethod
    def unlink_libraries(
        library_uids: object, *, confirm: object = False, purge_orphans: object = False
    ) -> dict[str, object]:
        """
        Remove exactly the named libraries and everything linked from them.

        Every uid is resolved before anything is removed, and again just before its
        own removal, so `remove()` never reaches a library an earlier removal freed.
        An indirect library is refused: removing it edits its parent's contents.
        Blender also frees overrides of the removed data, so the report counts
        everything that went by uid census. Not transacted: a rollback could not
        restore a removed library.

        Args:
            library_uids: The libraries' `session_uid`s, 1 to `MAX_UNLINK_UIDS`.
            confirm: Must be True.
            purge_orphans: Also remove local datablocks this unlink left without users.

        Returns:
            dict[str, object]: `removed_libraries` (summaries read before removal),
            `already_removed_uids`, `removed_count`, `removed_by_type`,
            `removed_uids` (capped, with `removed_uids_truncated`),
            `purged_orphans`, `purged_by_type`, and `other_libraries_removed`
            (unnamed libraries that disappeared, expected empty). A refusal is a
            `ValueError` raised before anything is removed; a Blender failure
            part-way is a `RuntimeError` listing the uids already removed.

        """
        confirm = _require_bool("confirm", confirm)
        purge_orphans = _require_bool("purge_orphans", purge_orphans)
        libraries = _libraries_to_unlink(library_uids, confirm)
        uids = [library.session_uid for library in libraries]  # type: ignore[attr-defined]
        known = tuple(path for library in libraries for path in _library_paths(library))
        other_before = {library.session_uid for library in bpy.data.libraries} - set(uids)
        before = _census()
        removed_libraries, already_removed = _remove_libraries(libraries, known)
        after = _census()
        removed = sorted(uid for uid in before if uid not in after)
        purged = _purge_newly_orphaned(before, known) if purge_orphans else []
        surviving = {library.session_uid for library in bpy.data.libraries}
        return {
            "removed_libraries": removed_libraries,
            "already_removed_uids": already_removed,
            "removed_count": len(removed),
            "removed_by_type": summarize_type_counts(before[uid][0] for uid in removed),
            "removed_uids": removed[:MAX_REPORTED_UIDS],
            "removed_uids_truncated": len(removed) > MAX_REPORTED_UIDS,
            "purged_orphans": len(purged),
            "purged_by_type": summarize_type_counts(purged),
            "other_libraries_removed": sorted(other_before - surviving),
        }
