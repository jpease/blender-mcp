"""
Linking commands: link canon libraries into a shot, override them, and list, reload, relocate or unlink them.

Plan Task 7. Every ruling below was re-measured on Blender 5.2.2 before this
module was written; the instruments are in `scripts/blender_probes/`:

- **Overrides use Route C**, `override_hierarchy_create(scene, view_layer,
  do_fully_editable=True)` (`linking_override_routes.py`): Route A's objects
  stay locked and Route B's are system overrides. Neither needs `bpy.context`,
  so the scene always comes from `bpy.data`.
- **Reload and relocate use the data API**, `lib.filepath = ...; lib.reload()`
  (`linking_reload_ruling.py`). `wm.lib_reload` / `wm.lib_relocate` ignore
  `filepath`, return `{'CANCELLED'}` for a bad name and the relocate operator
  renames the library; nothing here calls them.
- **Handles are `session_uid`s.** After a Route C override two collections and
  two objects share each name, so a name resolves nothing. The one exception is
  `link_canon_library`'s `collections` / `objects`: they name datablocks inside
  the library *file*, which have no local uid yet. A `session_uid` is valid only
  until the next file load or library reload (both churn every uid they touch).

Routing (`server_core`): `list_libraries` is read-only; `reload_library`,
`relocate_library` and `unlink_libraries` are `_DATABLOCK_REPLACING_COMMANDS` and
bypass `mutation_transaction`, because a reload gives every linked datablock a
fresh uid and a rollback would delete them; `link_canon_library` and
`create_override` stay transacted, so a failure removes what they created.

**Scripts.** The four commands that load or override library data refuse while
`preferences.filepaths.use_scripts_auto_execute` is on, through the same check
`open_shot` uses. **That refusal is not the control for script execution**
(user decision, 2026-09-16). Blender gates drivers on the *session* flag - set by
`-y` / `--enable-autoexec`, or by `open_mainfile` / `revert_mainfile` with
`use_scripts=True` ("Reload Trusted") - which the preference does not reflect:
under `-y` the preference reads False and a linked library's Python driver ran
after a link, an override and a reload (cycle-1 critic, 5.2.2). With the session
flag off nothing ran (`linking_scripts_auto_execute.py`). In the intended trusted
deployments these loads follow Blender's own trust, the same as a manual link;
detecting the session flag is a Phase 4 requirement for pooled or untrusted
deployments.

Blender embeds absolute paths in its own error text, so every `except` around a
data-API call sanitizes with the paths the call held
(`file_lifecycle._operator_failure_message`), and library identity is published
only through `file_lifecycle._library_summary`.
"""

import os

from collections.abc import Iterable, Iterator

import bpy

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
# Per library in a listing, and per override in a report: a canon library can
# link thousands of datablocks, and the exact count is reported beside the cap.
MAX_LISTED_DATABLOCKS = 100
MAX_LINK_NAMES = 100
MAX_UNLINK_UIDS = 100
MAX_REPORTED_UIDS = 500
_MAX_CANDIDATES = 10

_RELOAD_NOTE = (
    "Every datablock linked from this library now has a new session_uid; references read before this call "
    "name nothing. Read the datablocks listed here, or list_libraries, before the next command."
)


class LibraryFileNamesError(ValueError):
    """A requested name is not in the library file; raised inside `libraries.load` so no Library is created."""


def _require_uid(name: str, value: object) -> int:
    """
    Refuse a handle that is not an integer `session_uid`.

    `True == 1` in Python, so a bool is refused explicitly rather than resolving
    whichever datablock has uid 1.

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
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum or (maximum and value > maximum):
        upper = f" and at most {maximum}" if maximum else ""
        raise ValueError(f"{name} must be an integer of at least {minimum}{upper}")
    return value


def _display_name(datablock: object) -> str:
    """
    Reduce a datablock name for a client, holding a library's name to `_library_summary`'s leaf rule.

    `Library.name` accepts a whole path (TASK_STATE T3-15), so it goes through
    `client_safe_name_leaf` as it does in `_library_summary` (no filesystem call on
    author-chosen text); any other ID name is
    file-author text and goes through `client_safe_text`.

    Args:
        datablock: The datablock.

    Returns:
        str: The publishable name.

    """
    name = getattr(datablock, "name", "")
    if getattr(datablock, "id_type", None) == "LIBRARY":
        return client_safe_name_leaf(name)
    return client_safe_text(name)


def _candidates(datablocks: Iterable[object]) -> str:
    """
    Describe datablocks by name and uid, for a refusal that must let the client choose.

    Args:
        datablocks: The candidates.

    Returns:
        str: `'Name' (session_uid N)` entries, at most `_MAX_CANDIDATES` of them.

    """
    items = list(datablocks)
    shown = ", ".join(f"{_display_name(d)!r} (session_uid {_uid_of(d)})" for d in items[:_MAX_CANDIDATES])
    return shown if len(items) <= _MAX_CANDIDATES else f"{shown}, and {len(items) - _MAX_CANDIDATES} more"


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


def resolve_unique_name(datablocks: Iterable[object], name: str, kind: str) -> object:
    """
    Resolve a datablock by name only when exactly one has it.

    The plan-required name-to-uid resolver; no command calls it yet (Task 9's
    tools may). The sanctioned route **for a caller that can supply a
    `session_uid`**: after a Route C override a name is ambiguous by construction
    and `bpy.data.x[name]` returns whichever Blender ordered first, so an ambiguous
    name is refused with every candidate's uid for the caller to choose from.

    `object_lookup.find_object` is the rule for the object tools, which take no uid
    to supply; it prefers the local (editable) object and refuses only when several
    *linked* objects share a name. The two rules differ deliberately - see that
    module's docstring.

    Args:
        datablocks: The `bpy.data` collection to search.
        name: The name.
        kind: What it is, for the message.

    Returns:
        object: The one datablock with that name.

    Raises:
        ValueError: When no datablock, or more than one, has that name.

    """
    matches = [datablock for datablock in datablocks if getattr(datablock, "name", None) == name]
    if not matches:
        raise ValueError(f"no {kind} is named {client_safe_text(name)!r}")
    if len(matches) > 1:
        raise ValueError(
            f"more than one {kind} is named {client_safe_text(name)!r}: {_candidates(matches)}; pass a session_uid"
        )
    return matches[0]


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


def _uid_of(datablock: object) -> int | None:
    """
    Read a possibly-absent datablock's uid.

    Args:
        datablock: A datablock or None.

    Returns:
        int | None: Its `session_uid`.

    """
    return getattr(datablock, "session_uid", None) if datablock is not None else None


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


def _bounded_list(key: str, items: list[object], describe: object) -> dict[str, object]:
    """
    Publish a list with its exact count and a cap.

    Args:
        key: The list's result key.
        items: Everything.
        describe: The per-item describer.

    Returns:
        dict[str, object]: `<key>`, `<key>_count`-style `count`, and `truncated`.

    """
    return {
        key: [describe(item) for item in items[:MAX_LISTED_DATABLOCKS]],  # type: ignore[operator]
        f"{key.removesuffix('s')}_count": len(items),
        f"{key}_truncated": len(items) > MAX_LISTED_DATABLOCKS,
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


def _linked_datablocks(library: object) -> dict[str, object]:
    """
    List what a library links, from `Library.users_id`.

    `users_id` is a Python-level property absent from `bl_rna.properties`
    (handoff §08), and it is the direct answer; no `bpy.data` walk.

    Args:
        library: A `bpy.types.Library`.

    Returns:
        dict[str, object]: `datablocks`, `datablock_count`, `datablocks_truncated`.

    """
    return _bounded_list("datablocks", list(getattr(library, "users_id", ()) or ()), _linked_entry)


def _missing_warnings(library: object) -> dict[str, object]:
    """
    Warn when a reload left linked datablocks as placeholders.

    `Library.is_missing` only says the *file* is missing. Measured on 5.2.2
    (`linking_handlers_real_blender.py` section H): relocating to a file that
    lacks the linked collection leaves `is_missing=False` on the library and a
    placeholder collection with `ID.is_missing=True`, and a reopen warns that
    linked data-blocks are missing.

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

    A relative known path is replaced as a plain substring, so `canon.blend`
    would mangle the library name in `library 'canon.blend'`.

    Args:
        paths: Candidate paths.

    Returns:
        tuple[str, ...]: The absolute ones.

    """
    return tuple(path for path in paths if isinstance(path, str) and os.path.isabs(path))


def _library_paths(library: object) -> tuple[str, ...]:
    """
    Every absolute form of a library's path that Blender may put in an error.

    `reload()` embeds `bpy.path.abspath(filepath)` for a `//` link (measured,
    `linking_reload_ruling.py` section D); the canonical form covers a symlinked
    directory.

    Args:
        library: A `bpy.types.Library`.

    Returns:
        tuple[str, ...]: The stored, expanded and canonical forms that are absolute.

    """
    # An absolute `Library.name` is replaced whole too: a quoted-name match can
    # end early inside it (an apostrophe then a space) and leave a relative tail.
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

    `replacing_library_contents` wraps exactly the call, so the
    `blend_import_post` it fires disarms any transaction a future caller has open
    (Task 4). A failure raises before any uid moves (measured) and leaves the
    contents as they were.

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

    An absent name still creates a `Library` datablock when the block exits,
    while a raise inside the block creates none (measured,
    `linking_datablock_lifecycle.py` section B).

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
    # Overriding a collection overrides every collection inside it, including one
    # already overridden on its own: `Child` then `Parent` left two `ChildBody`
    # overrides that persisted (measured, `linking_handlers_real_blender.py` section M).
    inner = {_uid_of(child) for child in getattr(collection, "children_recursive", ())}
    inner_overrides = [
        c for c in bpy.data.collections if _uid_of(getattr(c.override_library, "reference", None)) in inner
    ]
    if inner_overrides:
        raise ValueError(
            f"collection session_uid {uid} contains a collection that is already overridden, by "
            f"{_candidates(inner_overrides)}; remove that override first, or override only the inner collection"
        )


def _override_hierarchy(collection: object, scene: object, unlinked: list[tuple[object, object]]) -> dict[str, object]:
    """
    Override one linked collection's hierarchy with Route C, replacing its instances.

    Route C places the override beside every instance of the linked collection
    and leaves the instance in place, so the asset would draw twice (measured,
    `linking_override_routes.py`); each instance whose parent received the
    override is unlinked from that parent and recorded in `unlinked`, so a
    caller can put it back when a later step fails. The linked collection itself
    is not removed: the override references it and it survives a save (measured,
    Step 5). Call `_refuse_unoverridable` first.

    Args:
        collection: A linked `bpy.types.Collection`.
        scene: The scene, from `bpy.data`.
        unlinked: `(parent, child)` pairs this call unlinks are appended here.

    Returns:
        dict[str, object]: `override` (see `_override_entry`), `scene_uid`,
        `replaced_instances`, and `objects` inside the override with their
        count and truncation flag.

    Raises:
        RuntimeError: When Blender raised or created no override.

    """
    uid = collection.session_uid  # type: ignore[attr-defined]
    parents = [parent for parent in _instance_parents() if _has_child(parent, collection)]
    try:
        # Measured: returns None rather than raising for a collection it cannot override.
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
    return {
        "override": _override_entry(override),
        "scene_uid": _uid_of(scene),
        "replaced_instances": replaced,
        **_bounded_list("objects", list(override.all_objects), _override_entry),
    }


def _refuse_nested_requests(collections: list) -> None:
    """
    Refuse a request that names a collection and a collection inside it.

    Overriding the outer one overrides the inner one, so the per-collection
    re-check would refuse anyway - but by naming an override this same request
    built and rolled back, which tells the client nothing it can act on.

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


def _override_all(collections: list, scene: object) -> list[dict[str, object]]:
    """
    Override several linked collections, all or nothing for the user's placement.

    Every collection is validated before the first instance is replaced, and
    again just before its own override - a nested request (`[Parent, Child]`)
    passes the first pass and is refused by the second, because overriding
    `Parent` already overrode `Child`. Such a request is now refused before
    either pass by `_refuse_nested_requests`, whose message names both
    collections; the re-check stays as the guard for anything that pass misses
    (a hierarchy Blender builds that `children_recursive` did not show). An
    instance unlinked for an earlier collection is re-linked when a later one
    fails. `mutation_transaction` removes the overrides a failed request created,
    but it does not restore `children` links, so without this a refused second
    collection destroyed the first one's existing placement (reproduced on 5.2.2,
    `linking_handlers_real_blender.py` section G). Re-linking appends, so the
    placement returns but its position among its siblings may not.

    Args:
        collections: Linked `bpy.types.Collection`s.
        scene: The scene, from `bpy.data`.

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
            reports.append(_override_hierarchy(collection, scene, unlinked))
    except Exception:
        for parent, child in reversed(unlinked):
            if not _has_child(parent, child):
                parent.children.link(child)  # type: ignore[attr-defined]
        raise
    return reports


def _link_into(members: object, items: list[object]) -> None:
    """
    Instance linked datablocks in the scene root unless already there.

    A linked datablock nobody uses is not written on save (measured,
    `linking_datablock_lifecycle.py` section A), so a link that is not
    instanced silently disappears at the next save.

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

    `bpy.data.all_ids` is itself a `COLLECTION` property (fixed type `ID`) that
    repeats every datablock (measured on 5.2.2, `linking_handlers_real_blender.py`
    section E, whose first run reported all five removals as `all_ids`), so a
    walk that included it would count each datablock twice and hand duplicates
    to `batch_remove`.

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


def _newly_orphaned(before: dict[int, tuple[str, int]]) -> list[tuple[str, object]]:
    """
    Find local datablocks that this unlink left with no users.

    **Not `orphans_purge`.** That purges every orphan in the file, and measured
    (`linking_datablock_lifecycle.py` section D2) it deleted a zero-user
    material the user had made, unrelated to any library. This takes only
    datablocks that had users before the unlink and have none now; libraries
    and datablocks linked from a library that was not named are never taken.

    Args:
        before: `_census()` read before the removal.

    Returns:
        list[tuple[str, object]]: `(collection name, datablock)` to remove.

    """
    orphans = []
    for name, datablock in _iter_ids():
        if name == "libraries" or getattr(datablock, "library", None) is not None:
            continue
        previous = before.get(getattr(datablock, "session_uid", None))  # type: ignore[arg-type]
        if previous and previous[1] > 0 and datablock.users == 0 and not datablock.use_fake_user:  # type: ignore[attr-defined]
            orphans.append((name, datablock))
    return orphans


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

    Summaries are read before any removal: a removed library's attributes
    cannot be read afterwards.

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


def _count_by_type(names: Iterable[str]) -> dict[str, int]:
    """
    Count collection names.

    Args:
        names: One collection name per datablock.

    Returns:
        dict[str, int]: Name -> count.

    """
    counts: dict[str, int] = {}
    for name in names:
        counts[name] = counts.get(name, 0) + 1
    return counts


class LinkingHandlersMixin:
    """Link, override, list, reload, relocate and unlink canon libraries."""

    @staticmethod
    def link_canon_library(  # ruff: ignore[too-many-arguments]
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

        The link is `bpy.data.libraries.load(link=True)` - never with
        `create_liboverrides=True`, whose objects stay locked. With
        `as_override=True` each linked collection is overridden by Route C, as
        `create_override` does; otherwise each linked datablock is instanced in
        the scene root, because an unused link is dropped on save. Transacted: a
        failure removes the `Library` and everything it linked.

        Args:
            filepath: The library `.blend`; absolute, `~`, or `//` relative to a saved open file. Roots enforced.
            collections: Names of collections **inside the library file** (not
                handles into `bpy.data`, which is why they are names). None means none.
            objects: Names of objects inside the library file. None means none.
            as_override: Override each linked collection (Route C). Refused with `objects`.
            relative: Store the library path relative to the open file; refused in a never-saved session,
                where Blender silently stores it absolute (measured).
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
            overrides = _override_all(list(linked_collections), scene)
        else:
            _link_into(scene.collection.children, linked_collections)  # type: ignore[attr-defined]
            _link_into(scene.collection.objects, linked_objects)  # type: ignore[attr-defined]
        return {
            "library": _library_details(library),
            "library_already_linked": library.session_uid in libraries_before,
            "scene_uid": _uid_of(scene),
            "collections": [_linked_entry(item) for item in linked_collections],
            "objects": [_linked_entry(item) for item in linked_objects],
            "overrides": overrides,
        }

    @staticmethod
    def create_override(collection_uid: object, *, scene_uid: object = None) -> dict[str, object]:
        """
        Make a linked collection's hierarchy editable in the shot (Route C).

        `override_hierarchy_create(scene, view_layer, do_fully_editable=True)`
        with the kwarg passed explicitly: its default is False, which produces
        system overrides. Not per-object `override_create`, which overrides one
        ID at a time. Refuses a local collection, an override, and a collection
        already overridden (a second call would make `CanonHero.001`, measured).

        Args:
            collection_uid: The **linked** collection's `session_uid`.
            scene_uid: The scene to override into; optional when the file has one scene.

        Returns:
            dict[str, object]: `override` (`session_uid`, `is_system_override` -
            expected False, `reference_uid`, `hierarchy_root_uid`, ...),
            `scene_uid`, `replaced_instances`, and `objects` inside the override,
            each with the state that tells it from its same-named linked original.

        """
        uid = _require_uid("collection_uid", collection_uid)
        collection = _by_session_uid(bpy.data.collections, uid, "collection")
        _refuse_scripts_auto_execute("create_override")
        return _override_all([collection], _scene(scene_uid))[0]

    @staticmethod
    def list_libraries(*, limit: object = DEFAULT_PAGE_SIZE, offset: object = 0) -> dict[str, object]:
        """
        List linked libraries, a page at a time, with what each one links.

        Args:
            limit: Libraries per page, 1 to `MAX_PAGE_SIZE`.
            offset: Libraries to skip.

        Returns:
            dict[str, object]: `libraries` (each `_library_summary` plus
            `version`, `needs_liboverride_resync`, `users`, and `datablocks` with
            uid and name, capped at `MAX_LISTED_DATABLOCKS` beside an exact
            `datablock_count`), `total`, `offset`, `limit`, `has_more`.

        """
        limit = _bounded_int("limit", limit, 1, MAX_PAGE_SIZE)
        offset = _bounded_int("offset", offset, 0, None)
        libraries = list(bpy.data.libraries)
        page = libraries[offset : offset + limit]
        return {
            "libraries": [{**_library_details(lib), **_linked_datablocks(lib)} for lib in page],
            "total": len(libraries),
            "offset": offset,
            "limit": limit,
            "has_more": offset + len(page) < len(libraries),
        }

    @staticmethod
    def reload_library(library_uid: object) -> dict[str, object]:
        """
        Re-read a library from its file with `lib.reload()`.

        Replaces every datablock linked from it in place, with fresh
        `session_uid`s: anything holding a reference or uid from before is stale.
        Not transacted (`_DATABLOCK_REPLACING_COMMANDS`) and not a session swap.
        File roots are not enforced here: the path is the one already in the
        open file, and only `relocate_library` can change it.

        Args:
            library_uid: The library's `session_uid`.

        Returns:
            dict[str, object]: `library`, its `datablocks` with their new uids, and `note`.

        """
        library = _library(library_uid)
        _refuse_scripts_auto_execute("reload_library")
        _reload(library, "reload_library", _library_paths(library))
        return {
            "library": _library_details(library),
            **_linked_datablocks(library),
            "note": _RELOAD_NOTE,
            **_missing_warnings(library),
        }

    @staticmethod
    def relocate_library(library_uid: object, filepath: object) -> dict[str, object]:
        """
        Point a library at another `.blend` and reload it: `lib.filepath = <canonical>; lib.reload()`.

        The new path goes through the roots and `resolve_blend_path` first and
        is stored in its canonical absolute form. A failed reload restores the
        previous `filepath`, leaving the library as it was (measured). Refuses a
        file another library already links. Invalidates references like
        `reload_library`. The data API does not rename the library (measured),
        but `name_before` / `name_after` are reported so a client never relies on it.

        Args:
            library_uid: The library's `session_uid`.
            filepath: The replacement `.blend`.

        Returns:
            dict[str, object]: As `reload_library`, plus `name_before` and `name_after`.

        Raises:
            ValueError: When the request is refused; nothing changed.
            RuntimeError: When the reload failed; the library still points at its previous file.

        """
        library = _library(library_uid)
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
        # Before the filepath assignment: a refusal must leave the library untouched.
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
            **_linked_datablocks(library),
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

        Deletes user data, so it requires `confirm=True` and an explicit uid
        list; every uid is resolved before anything is removed, and each is
        re-resolved just before its own removal so `remove()` never reaches a
        datablock an earlier removal freed. An indirect library (reached only
        through another library) is refused: removing it edits the parent
        library's contents. Blender also frees the local override objects of
        the removed datablocks (measured); the report counts everything that
        went, by uid census, not by assumption. Not transacted: `libraries.remove`
        fires no handler and there is nothing a rollback could restore.

        Args:
            library_uids: The libraries' `session_uid`s, 1 to `MAX_UNLINK_UIDS`.
            confirm: Must be True.
            purge_orphans: Also remove local datablocks this unlink left without users - never
                `orphans_purge`, which also deletes unrelated orphans the user made.

        Returns:
            dict[str, object]: `removed_libraries` (summaries read before removal),
            `already_removed_uids`, `removed_count`, `removed_by_type`,
            `removed_uids` (capped, with `removed_uids_truncated`),
            `purged_orphans`, `purged_by_type`, and `other_libraries_removed` -
            unnamed libraries that disappeared, expected empty. A refusal is a
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
            "removed_by_type": _count_by_type(before[uid][0] for uid in removed),
            "removed_uids": removed[:MAX_REPORTED_UIDS],
            "removed_uids_truncated": len(removed) > MAX_REPORTED_UIDS,
            "purged_orphans": len(purged),
            "purged_by_type": _count_by_type(purged),
            "other_libraries_removed": sorted(other_before - surviving),
        }
