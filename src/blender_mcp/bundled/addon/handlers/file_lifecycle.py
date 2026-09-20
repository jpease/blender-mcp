"""
File-lifecycle commands: which shot is open, and opening, saving or resetting it.

`open_shot` and `reset_session` replace the whole database, so they are session
swap commands: each ends its drain tick and runs outside `mutation_transaction`.
`save_shot` is not one: a save replaces no datablock, so the commands queued
behind it stay safe, and making it a swap would discard them on every save.

Paths go through `file_paths` for the roots and file checks, and operator errors
through `sanitize_blender_error`, because Blender's error text embeds absolute
paths. Published names go through `text_hygiene` so every module applies one
rule. `filepath` in a result is published absolute, like `current_filepath`,
because a client compares it against the file roots.

`get_session_info` touches no datablock and is the cheap poll after a swap, so
it is a read-only command and never runs in `mutation_transaction`.
"""

import json
import os

from collections import Counter
from contextlib import suppress
from datetime import UTC, datetime

import bpy

from .. import ADDON_PROTOCOL_VERSION, authored, bl_info
from ..file_digest import MAX_DIGEST_FILES, MAX_DIGEST_TOTAL_BYTES, file_digest
from ..file_paths import (
    BLENDER_RELATIVE_PREFIX,
    canonical_path,
    create_save_directory,
    enforce_roots,
    resolve_blend_path,
    sanitize_blender_error,
)
from ..output_roots import configured_file_roots
from ..session import session_snapshot
from ..text_hygiene import (
    client_safe_leaf,
    client_safe_name_leaf,
    client_safe_text,
    relative_link_body,
    safe_relative_link,
    strip_unsafe,
)

# Sent in every successful swap result. A constant, so no path or name reaches it.
_REHANDSHAKE_NOTE = (
    "The open database was replaced: session_epoch moved, and the addon's capabilities follow the "
    "file. Re-handshake (get_addon_info) before relying on a cached capability list."
)

# Room for a project-relative link several directories deep, while keeping a
# hostile link from pushing kilobytes into an agent's context. The bound applies
# per library, and the library list itself is unbounded.
_MAX_REPORTED_LINK_CHARS = 256

# The custom property every local scene carries after a save. One name, so a reader does not
# have to know which scene the add-on happened to be looking at.
PROVENANCE_PROPERTY = "blender_mcp"


def _library_summary(library: object) -> dict[str, object]:
    r"""
    Describe one linked library by identity, not by where it sits on this machine.

    `text_hygiene.safe_relative_link` decides whether `filepath` is published
    whole, exactly as it returned it. Anything it refuses (absolute, rooted
    `///...`, traversing, over-long, or with a character outside its allowlist)
    is reduced to a leaf, because it would reveal the studio's storage layout.
    `is_relative` is judged on the same stripped form, so `///Users/...` is not
    reported as relative.

    `name` is reduced to a leaf too: Blender lets a `.blend` author set
    `Library.name` to a path such as `/Users/victim/shots/canon.blend`. Neither
    field is stat'ed: both are author-chosen text, and probing one would reveal
    whether a directory by that name exists on this machine.

    Args:
        library: A `bpy.types.Library`.

    Returns:
        dict[str, object]: `session_uid`, the handle the linking commands
        resolve by, because two libraries' contents can share a name. It is
        valid only for the `(session_id, session_epoch)` it was read under:
        despite Blender's own description, it changes on every load. `name`,
        for display only; `filepath`, whole or reduced to a leaf; `is_relative`;
        and `is_missing`, true when the link is broken now.

    """
    filepath = str(getattr(library, "filepath", "") or "")
    whole = safe_relative_link(filepath, _MAX_REPORTED_LINK_CHARS)
    return {
        "session_uid": getattr(library, "session_uid", None),
        "name": client_safe_name_leaf(getattr(library, "name", "")),
        "filepath": whole if whole is not None else client_safe_leaf(filepath),
        "is_relative": relative_link_body(strip_unsafe(filepath)) is not None,
        "is_missing": bool(getattr(library, "is_missing", False)),
    }


def _require_bool(name: str, value: object) -> bool:
    """
    Refuse a flag that is not a real `bool`.

    `"false"` is truthy, so a JSON client that sends a string would otherwise
    confirm an overwrite it meant to decline.

    Args:
        name: The parameter name, for the message.
        value: What the client sent.

    Returns:
        bool: The value, unchanged.

    Raises:
        ValueError: If it is not a `bool`.

    """
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be true or false")
    return value


def _expand_blender_relative(raw: object) -> object:
    """
    Expand a `//` path against the open file, refusing one that has nothing to be relative to.

    With no file open, `bpy.path.abspath` leaves a `//` path relative, and it
    would resolve against the working directory, which the caller never named.

    Args:
        raw: The path the client sent; non-strings pass through to be refused
            by `resolve_blend_path`.

    Returns:
        object: The expanded path, or `raw` unchanged when it is not `//`-relative.

    Raises:
        ValueError: For a `//` path in a session that has never been saved.

    """
    if not (isinstance(raw, str) and raw.startswith(BLENDER_RELATIVE_PREFIX)):
        return raw
    if not bpy.data.filepath:
        raise ValueError(
            "a Blender-relative path (one starting with a double slash) is relative to the open .blend, "
            "and this session has never been saved; pass an absolute path"
        )
    return bpy.path.abspath(raw)


def _checked_blend_path(raw: object, *, must_exist: bool, create_directories: bool = False) -> str:
    """
    Validate a `.blend` path: expand `//`, enforce the roots, then check the file.

    Roots come before the file checks, so a path outside them gets the same
    refusal whether it names a file, a directory or nothing; otherwise any path
    on the machine could be probed for existence.

    That ordering costs one extra `realpath`, because `enforce_roots` and
    `resolve_blend_path` each canonicalize the path they are given; resolving
    first and enforcing on the result would save the call and answer an
    out-of-roots path with "file does not exist" or "not a .blend" instead.

    Hand Blender the returned canonical path, never the raw one: Blender resolves
    the raw form's `..` before symlinks, which is not the path the roots checked.

    Args:
        raw: The path the client sent.
        must_exist: True to open, False to save.
        create_directories: For a save, accept a missing target directory.

    Returns:
        str: The canonical path to hand to Blender. A refusal propagates as the
        `ValueError` the refusing check raised, whose message names no path.

    """
    expanded = _expand_blender_relative(raw)
    if isinstance(expanded, str) and expanded.strip() and "\x00" not in expanded:
        enforce_roots(expanded, configured_file_roots())
    return resolve_blend_path(expanded, must_exist=must_exist, create_directories=create_directories)


def _refuse_scripts_auto_execute(command: str = "open_shot") -> None:
    """
    Refuse a load while Blender is set to run scripts embedded in a `.blend`.

    The linking commands and the Poly Haven `.blend` import call it too. It does
    not fully control script execution: Blender runs drivers based on the
    session's trust flag (`-y`, or a load with `use_scripts=True`), which this
    preference does not reflect. Trusted deployments accept that; an untrusted
    one would need to detect the flag, which is not implemented.

    Refused rather than warned about, and stricter than unset file roots: the
    preference defaults to off, so finding it on means someone enabled it. An
    unreadable preference counts as on.

    Args:
        command: The refusing command, named in the message.

    Raises:
        ValueError: When the preference is on or unreadable.

    """
    filepaths = getattr(getattr(bpy.context, "preferences", None), "filepaths", None)
    if getattr(filepaths, "use_scripts_auto_execute", True) is not False:
        raise ValueError(
            f"{command} refuses to load while Blender's preferences.filepaths.use_scripts_auto_execute "
            "is on (or unreadable), because a .blend could run embedded scripts; turn it off in "
            "Preferences > Save & Load, then retry"
        )


def _operator_failure_message(command: str, exc: BaseException, known_paths: tuple[object, ...]) -> str:
    """
    Build the client-facing message for a file operator that raised.

    Blender's file operators raise `RuntimeError` with the absolute path in the
    text, sometimes twice. The raw exception stays chained for Blender's console;
    the client gets only the sanitized text.

    Args:
        command: The command name, for the message.
        exc: What the operator raised.
        known_paths: Every form of the path the call held.

    Returns:
        str: `"<command> failed: <sanitized text>"`.

    """
    known = tuple(path for path in known_paths if isinstance(path, str))
    return f"{command} failed: {sanitize_blender_error(exc, known_paths=known)}"


def _refuse_a_leftover_temp_save(canonical: str) -> None:
    """
    Refuse a save while Blender's temporary save name is occupied or cannot be checked.

    Blender writes `<target>@`, then renames it over the target, so a symlink
    planted at that name redirects the write outside the roots. Anything there
    refuses the save, including a dangling symlink, hence `lstat` rather than
    `exists`. Any other `OSError` refuses too, because "cannot tell" must not
    mean "clear".

    Args:
        canonical: The validated target path.

    Raises:
        ValueError: When the temporary name is occupied or cannot be checked.

    """
    try:
        os.lstat(f"{canonical}@")
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ValueError(
            "the temporary save file Blender writes beside the target (the target name followed by '@') "
            "cannot be created or checked; check the target directory, then retry"
        ) from exc
    raise ValueError(
        "a temporary save file (the target name followed by '@') already exists beside the target, possibly "
        "left by an interrupted save; remove it, then retry"
    )


def _is_indirect_library(library: object) -> bool:
    """
    Report whether a library is only reached through another library.

    `Library.parent` is set while the link is made, but after a reopen it is None
    whenever the main file's own entry for the library resolves, because Blender
    then re-reads it as direct; every user of an indirect library carries
    `is_library_indirect` either way. Blender resolves an indirect library
    through its parent rather than from the main file's entry, so its `//` path
    survives the main file moving.

    Args:
        library: A `bpy.types.Library`.

    Returns:
        bool: True when it has users and all of them are indirect.

    """
    users = list(getattr(library, "users_id", ()) or ())
    return bool(users) and all(getattr(user, "is_library_indirect", False) for user in users)


def _unresolvable_relative_paths(canonical: str, relative_remap: bool) -> int:
    """
    Count `//`-relative external file paths a save to another directory will leave pointing nowhere.

    With `relative_remap=False` Blender writes each path verbatim, so it resolves
    against the new directory on reopen, while the open session still reports
    it found. `blend_paths(local=True)` skips paths inside linked datablocks but
    still lists indirect libraries, whose paths Blender re-derives, so those are
    subtracted. Must run before the save, while `bpy.data.filepath` is still the
    old file.

    Args:
        canonical: The validated target path.
        relative_remap: Whether Blender will rewrite the paths itself.

    Returns:
        int: How many paths will not resolve; 0 when remapping, when the
        session has never been saved, or when the directory does not change.

    """
    current = bpy.data.filepath
    if relative_remap or not current:
        return 0
    if os.path.dirname(canonical) == os.path.dirname(canonical_path(current)):
        return 0
    relative = Counter(
        str(path)
        for path in bpy.utils.blend_paths(absolute=False, packed=False, local=True)
        if str(path).startswith(BLENDER_RELATIVE_PREFIX)
    )
    relative.subtract(
        str(getattr(library, "filepath", "")) for library in bpy.data.libraries if _is_indirect_library(library)
    )
    return sum(count for count in relative.values() if count > 0)


def _library_checksums(libraries: list[object]) -> dict[int, tuple[str, str]]:
    """
    SHA-256 each linked library, confined to the configured file roots.

    A `Library.filepath` comes out of the opened `.blend` and is author-controlled, so an
    unconfined checksum would read any file on this host. The resolved path is used for
    reading only and never returned.

    Args:
        libraries: The libraries to hash, in `bpy.data.libraries` order.

    Returns:
        dict[int, tuple[str, str]]: Index into `libraries` -> `(sha256, skipped reason)`;
        exactly one of the two is ever non-empty.

    Raises:
        ValueError: When no file roots are configured, so nothing can be confined.

    """
    roots = configured_file_roots()
    if not roots:
        raise ValueError(
            "provenance_checksums requires BLENDERMCP_FILE_ROOTS (or BLENDERMCP_OUTPUT_ROOTS) to be "
            "configured; without roots a checksum would read any file on this host"
        )
    digests: dict[int, tuple[str, str]] = {}
    budget = MAX_DIGEST_TOTAL_BYTES
    hashed = 0
    for index, library in enumerate(libraries):
        if hashed >= MAX_DIGEST_FILES:
            digests[index] = ("", "call hash file limit reached")
            continue
        raw = str(getattr(library, "filepath", "") or "")
        resolved = canonical_path(bpy.path.abspath(raw, library=getattr(library, "parent", None)))
        try:
            enforce_roots(resolved, roots)
        except ValueError:
            digests[index] = ("", "outside the configured file roots")
            continue
        digest, reason = file_digest(resolved, MAX_DIGEST_TOTAL_BYTES, budget)
        hashed += 1
        if digest is None:
            digests[index] = ("", reason or "unreadable")
            continue
        with suppress(OSError):
            budget -= os.path.getsize(resolved)
        digests[index] = (digest, "")
    return digests


def _provenance_block(with_checksums: bool) -> dict[str, object]:
    """
    Describe who authored this file, from what, in C2PA's vocabulary.

    Keys are named for the C2PA assertions they will later serialize into (`claim_generator`,
    `ingredients`, `actions`), so attaching a signed manifest to a render becomes a
    serialization step rather than a second provenance model. Unsigned and editable: it
    records authorship, it does not prove it.

    Args:
        with_checksums: Also hash every linked library, which reads them from disk.

    Returns:
        dict[str, object]: The block, JSON-serializable and free of host paths.

    Raises:
        ValueError: When checksums were asked for with no file roots configured.

    """
    addon_version = ".".join(str(part) for part in bl_info["version"])
    libraries = list(bpy.data.libraries)
    digests = _library_checksums(libraries) if with_checksums else {}
    ingredients = []
    for index, library in enumerate(libraries):
        summary = _library_summary(library)
        sha256, skipped = digests.get(index, ("", ""))
        ingredients.append(
            {"name": summary["name"], "filepath": summary["filepath"], "sha256": sha256, "skipped": skipped}
        )
    return {
        "claim_generator": f"blender-mcp/{addon_version}",
        "protocol_version": ADDON_PROTOCOL_VERSION,
        "blender_version": bpy.app.version_string,
        "saved_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "ingredients": ingredients,
        "actions": [
            {
                "action": "c2pa.edited",
                "software_agent": f"blender-mcp/{addon_version}",
                "datablocks": [f"{entry['collection']}:{entry['name']}" for entry in authored.snapshot()],
                "datablocks_truncated": authored.was_truncated(),
            }
        ],
    }


def _stamp_provenance(with_checksums: bool) -> tuple[list[tuple[object, bool, object]], int]:
    """
    Write the provenance block into every local scene, remembering what was there.

    Stored as a JSON string, not a nested ID-property group: an `IDPropertyArray` cannot hold
    groups, which is why every complex value this repo puts on a datablock is `json.dumps`'d.
    A linked scene is another file's data and assigning to it raises, so it is skipped.

    Args:
        with_checksums: Passed through to `_provenance_block`.

    Returns:
        tuple[list[tuple[object, bool, object]], int]: The `(scene, key existed, previous
        value)` backup `_restore_provenance` undoes, and how many ingredients were recorded.

    """
    block = _provenance_block(with_checksums)
    encoded = json.dumps(block, sort_keys=True)
    backup: list[tuple[object, bool, object]] = []
    for scene in bpy.data.scenes:
        if scene.library is not None:
            continue
        backup.append((scene, PROVENANCE_PROPERTY in scene, scene.get(PROVENANCE_PROPERTY)))
        scene[PROVENANCE_PROPERTY] = encoded
    return backup, len(block["ingredients"])  # pyright: ignore[reportArgumentType]


def _restore_provenance(backup: list[tuple[object, bool, object]]) -> None:
    """
    Put every scene's `blender_mcp` property back as it was, in reverse order.

    Args:
        backup: `(scene, key existed, previous value)` triples recorded before writing.

    """
    for scene, existed, previous in reversed(backup):
        with suppress(Exception):
            if existed:
                scene[PROVENANCE_PROPERTY] = previous  # pyright: ignore[reportIndexIssue]
            else:
                del scene[PROVENANCE_PROPERTY]  # pyright: ignore[reportIndexIssue]


def _save_with_provenance(
    canonical: str,
    requested: object,
    *,
    in_place: bool,
    compress: bool,
    relative_remap: bool,
    write_provenance: bool,
    provenance_checksums: bool,
) -> int:
    """
    Stamp the provenance block, run Blender's save, and undo the stamp if it fails.

    The stamp lives inside the same `try` as the operator and after every refusal, so a save
    that is refused or that Blender rejects leaves no scene carrying a claim about a file that
    was never written.

    Args:
        canonical: The validated target path to hand Blender.
        requested: The path the client asked for, for the sanitized failure message.
        in_place: Whether this is a save over the open file.
        compress: Passed to the operator.
        relative_remap: Passed to the operator.
        write_provenance: Whether to stamp at all.
        provenance_checksums: Whether the stamp hashes linked libraries.

    Returns:
        int: How many ingredients the stamp recorded; 0 when nothing was stamped.

    Raises:
        RuntimeError: When Blender could not write the file.

    """
    operator = bpy.ops.wm.save_mainfile if in_place else bpy.ops.wm.save_as_mainfile
    backup: list[tuple[object, bool, object]] = []
    ingredients = 0
    try:
        if write_provenance:
            backup, ingredients = _stamp_provenance(provenance_checksums)
        # Raises RuntimeError on every failure mode; never returns CANCELLED.
        operator(filepath=canonical, compress=compress, relative_remap=relative_remap)
    except RuntimeError as exc:
        _restore_provenance(backup)
        raise RuntimeError(_operator_failure_message("save_shot", exc, (requested, canonical))) from exc
    except Exception:
        _restore_provenance(backup)
        raise
    return ingredients


def _save_report(
    *,
    in_place: bool,
    exists: bool,
    created_directory: bool,
    compress: bool,
    relative_remap: bool,
    write_provenance: bool,
    ingredients: int,
    broken_links: int,
) -> dict[str, object]:
    """
    Describe a completed save.

    Args:
        in_place: Whether the save went to the open file.
        exists: Whether the target already existed.
        created_directory: Whether the target's directory was created.
        compress: What was passed to the operator.
        relative_remap: What was passed to the operator.
        write_provenance: Whether a provenance block was written.
        ingredients: How many linked libraries that block records.
        broken_links: `_unresolvable_relative_paths`' count.

    Returns:
        dict[str, object]: See `save_shot`'s Returns.

    """
    session = session_snapshot()
    result: dict[str, object] = {
        "filepath": session["current_filepath"],
        "saved_in_place": in_place,
        "overwrote_existing": exists,
        "created_directory": created_directory,
        "compress": compress,
        "relative_remap": relative_remap,
        "session_id": session["session_id"],
        "session_epoch": session["session_epoch"],
        "provenance_written": write_provenance,
    }
    if write_provenance:
        result["provenance_ingredients"] = ingredients
    if broken_links:
        noun = "path (images, libraries, etc.) is" if broken_links == 1 else "paths (images, libraries, etc.) are"
        result["warnings"] = [
            f"{broken_links} external file {noun} Blender-relative and will not resolve from the new "
            "directory, because relative_remap is false; save again with relative_remap=true, or relink"
        ]
    return result


class FileLifecycleHandlersMixin:
    """
    Report and change which .blend the session holds.

    `get_session_info` is a `staticmethod` because it reads process-wide state,
    not the server instance; the dispatch table still reaches it through `self`.
    """

    @staticmethod
    def get_session_info() -> dict[str, object]:
        """
        Report the current session: which file, which epoch, and what went wrong.

        When `session_epoch` has moved, the client must re-handshake, because
        some advertised capabilities depend on the open file's scene settings.

        Returns:
            dict[str, object]: `session_id` (str, new per addon process) and
            `session_epoch` (int), compared as a pair because the epoch restarts
            at 0 with the process; `current_filepath` (str | None; None when the
            session has never been saved, or after an aborted swap, when the
            open file cannot be named truthfully); `last_load_error` /
            `last_save_error` (str | None, with no filesystem path);
            `session_indeterminate` (bool, true after an aborted swap until a
            load completes, while the drain loop refuses all but this command,
            `get_addon_info` and the swap commands); `is_dirty` (bool, unsaved
            work a swap would destroy); and `libraries`, one entry per linked
            library as `_library_summary` describes.

        """
        return {
            **session_snapshot(),
            "is_dirty": bool(bpy.data.is_dirty),
            "libraries": [_library_summary(library) for library in bpy.data.libraries],
        }

    def _capability_names(self) -> frozenset[str]:
        """
        Read the command names this addon would advertise right now.

        Returns:
            frozenset[str]: `_build_command_handlers()`'s keys, which is what
            `get_addon_info` publishes as `capabilities`.

        """
        return frozenset(self._build_command_handlers())  # type: ignore[attr-defined]

    def _swap_report(self, capabilities_before: frozenset[str], discarded_unsaved: bool) -> dict[str, object]:
        """
        Describe the database a completed swap left open.

        Never raises an `Exception`: the load has already happened, so a read
        failure here must not reach the client as a failed open. An unreadable
        field is reported under `warnings`. A `BaseException` such as Esc's
        `KeyboardInterrupt` is not caught, and the client is then told the
        session may be indeterminate although the load completed.

        Args:
            capabilities_before: `_capability_names()` read before the swap.
            discarded_unsaved: Whether the replaced session held unsaved work.

        Returns:
            dict[str, object]: See `open_shot`'s Returns.

        """
        session = session_snapshot()
        report: dict[str, object] = {
            "session_id": session["session_id"],
            "session_epoch": session["session_epoch"],
            "filepath": session["current_filepath"],
            "rehandshake_required": True,
            "note": _REHANDSHAKE_NOTE,
            "discarded_unsaved_changes": discarded_unsaved,
        }
        warnings: list[str] = []
        try:
            report["scene_name"] = client_safe_text(bpy.context.scene.name)
            report["object_count"] = len(bpy.data.objects)
            report["libraries"] = [_library_summary(library) for library in bpy.data.libraries]
            report["capabilities_changed"] = self._capability_names() != capabilities_before
        except Exception as exc:
            print(f"BlenderMCP: the swap completed but its report is partial: {exc!s}")
            report.setdefault("capabilities_changed", True)
            warnings.append("The file was replaced, but part of this report could not be read; poll get_session_info.")
        if warnings:
            report["warnings"] = warnings
        return report

    def open_shot(
        self, filepath: object, load_ui: object = False, discard_unsaved: object = False
    ) -> dict[str, object]:
        """
        Replace the open database with a `.blend`, synchronously, and answer after the swap.

        Everything that can refuse runs before `wm.open_mainfile`, because once
        it runs the old database is gone. `use_scripts=False` is always passed and
        is not a parameter, so no tool can run code embedded in a file.

        Known limit: Blender clears `is_dirty` for a save only when it handles the
        save's notifier. A save by something else, such as another add-on's timer,
        followed by an edit in the same main-thread pass can leave `is_dirty`
        clear, and this guard then lets the edit be discarded.

        Args:
            filepath: The `.blend` to open; absolute, `~`, or `//` relative to a
                saved open file.
            load_ui: Load the file's window layout too. Default False; the
                operator's own default is True.
            discard_unsaved: Required when the session has unsaved work, which
                an open destroys without asking.

        Returns:
            dict[str, object]: `filepath` (the open file, as
            `get_session_info.current_filepath` publishes it), `scene_name`,
            `object_count`, `libraries`, `session_id`, `session_epoch`,
            `capabilities_changed`, `rehandshake_required` (always True) with a
            `note`, and `discarded_unsaved_changes`.

        Raises:
            ValueError: When the request is refused; nothing was loaded.
            RuntimeError: When Blender could not load it; the database is unchanged.

        """
        load_ui = _require_bool("load_ui", load_ui)
        discard_unsaved = _require_bool("discard_unsaved", discard_unsaved)
        canonical = _checked_blend_path(filepath, must_exist=True)
        dirty = bool(bpy.data.is_dirty)
        if dirty and not discard_unsaved:
            raise ValueError(
                "the open session has unsaved changes that opening a file would destroy; "
                "save_shot first, or pass discard_unsaved=true"
            )
        _refuse_scripts_auto_execute()
        capabilities_before = self._capability_names()
        try:
            # Raises RuntimeError on every failure mode; never returns CANCELLED.
            bpy.ops.wm.open_mainfile(filepath=canonical, load_ui=load_ui, use_scripts=False)
        except RuntimeError as exc:
            raise RuntimeError(_operator_failure_message("open_shot", exc, (filepath, canonical))) from exc
        return self._swap_report(capabilities_before, dirty)

    @staticmethod
    def save_shot(
        filepath: object = None,
        compress: object = False,
        relative_remap: object = False,
        confirm_overwrite: object = False,
        create_directories: object = False,
        write_provenance: object = True,
        provenance_checksums: object = False,
    ) -> dict[str, object]:
        """
        Write the open database to disk, refusing to replace an existing file unconfirmed.

        `filepath=None` saves in place; a path moves the session to it. Every
        existing target needs `confirm_overwrite=True`, the open file included,
        because the copy on disk may hold someone else's later save. The check is
        `os.path.exists` here, because the operator's `check_existing` only drives
        the file browser and does not stop a scripted overwrite.

        `compress` and `relative_remap` are always passed. Omitted,
        `save_as_mainfile` would rewrite library paths to `//` form, and the
        user's compression preference would apply.

        Args:
            filepath: Where to save; None to save the open file in place.
            compress: Write a compressed `.blend`. Default False: canon publishes
                must be uncompressed, because compression turns a re-save's tiny
                byte difference into most of the file and breaks content digests.
            relative_remap: Rewrite linked-library paths relative to the new
                location. Default False.
            confirm_overwrite: Required when the target already exists. A
                confirmed overwrite also replaces an existing `.blend1` backup
                when Blender keeps one.
            create_directories: Create the target's missing directory and
                parents, inside the file roots when any are configured. They are
                created after every refusal check, and stay if Blender's save then
                fails.
            write_provenance: Record who authored this file into every local scene as the
                custom property `blender_mcp`, shaped for C2PA. Default True.
            provenance_checksums: Also SHA-256 each linked library. Default False: hashing
                every library on every save would read gigabytes on Blender's main thread.
                Requires configured file roots.

        Returns:
            dict[str, object]: `filepath` (the open file after the save),
            `saved_in_place`, `overwrote_existing`, `created_directory`,
            `compress`, `relative_remap`, `session_id`, `session_epoch`
            (unchanged by a save), `provenance_written` and, when written,
            `provenance_ingredients`; and `warnings` when `//`-relative external
            file paths (images, direct libraries) will not resolve from a new
            directory. No `is_dirty`: Blender clears it only after this tick, so
            poll `get_session_info` instead.

        Raises:
            ValueError: When the request is refused; nothing was written.
            RuntimeError: When Blender could not write the file.

        """
        compress = _require_bool("compress", compress)
        relative_remap = _require_bool("relative_remap", relative_remap)
        confirm_overwrite = _require_bool("confirm_overwrite", confirm_overwrite)
        create_directories = _require_bool("create_directories", create_directories)
        write_provenance = _require_bool("write_provenance", write_provenance)
        provenance_checksums = _require_bool("provenance_checksums", provenance_checksums)
        in_place = filepath is None
        if in_place and not bpy.data.filepath:
            raise ValueError("this session has never been saved, so it cannot be saved in place; pass a filepath")
        requested = bpy.data.filepath if in_place else filepath
        canonical = _checked_blend_path(requested, must_exist=False, create_directories=create_directories)
        exists = os.path.exists(canonical)
        if exists and not confirm_overwrite:
            raise ValueError("the target .blend already exists; pass confirm_overwrite=true to replace it")
        _refuse_a_leftover_temp_save(canonical)
        broken_links = _unresolvable_relative_paths(canonical, relative_remap)
        created_directory = create_save_directory(canonical)
        ingredients = _save_with_provenance(
            canonical,
            requested,
            in_place=in_place,
            compress=compress,
            relative_remap=relative_remap,
            write_provenance=write_provenance,
            provenance_checksums=provenance_checksums,
        )
        return _save_report(
            in_place=in_place,
            exists=exists,
            created_directory=created_directory,
            compress=compress,
            relative_remap=relative_remap,
            write_provenance=write_provenance,
            ingredients=ingredients,
            broken_links=broken_links,
        )

    def reset_session(self, confirm: object = False) -> dict[str, object]:
        """
        Replace the open database with an empty factory scene, as a pooled worker's reset step.

        Uses `wm.read_homefile` with factory startup, not
        `wm.read_factory_settings`, which also resets the user's preferences and
        unregisters every add-on, this one included. The factory startup file
        makes the reset scene the same on every machine. The load fires
        `load_post`, which moves the epoch, so this does not increment it.

        `confirm=True` is the only consent needed, because discarding the session
        is the command's whole purpose.

        Args:
            confirm: Must be True.

        Returns:
            dict[str, object]: As `open_shot`, with `filepath` None.

        Raises:
            ValueError: Without `confirm=True`; nothing was reset.
            RuntimeError: When Blender could not reset.

        """
        if not _require_bool("confirm", confirm):
            raise ValueError("reset_session discards the open file and any unsaved work; pass confirm=true")
        dirty = bool(bpy.data.is_dirty)
        capabilities_before = self._capability_names()
        previous = bpy.data.filepath
        try:
            # Raises RuntimeError on failure; never returns CANCELLED.
            bpy.ops.wm.read_homefile(use_empty=True, use_factory_startup=True, load_ui=False)
        except RuntimeError as exc:
            raise RuntimeError(_operator_failure_message("reset_session", exc, (previous,))) from exc
        return self._swap_report(capabilities_before, dirty)
