"""
File-lifecycle commands: which shot is open, and opening, saving or resetting it.

`open_shot` and `reset_session` replace the whole database, so they are session
swap commands: each ends its drain tick and runs outside `mutation_transaction`.
`save_shot` is not one: a save replaces no datablock, so the commands queued
behind it stay safe, and making it a swap would discard them on every save.

Paths, flags, library summaries and operator failures come from `blend_files`,
shared with the linking and delivery commands; the provenance block a save
stamps comes from `provenance`, beside the reader that reads it back.
`filepath` in a result is published absolute, like `current_filepath`, because a
client compares it against the file roots.

A save is validated into one `SaveRequest` before anything is written: the
refusals are what construction does, so no later edit can reorder a check behind
the mutation it guards.

`get_session_info` touches no datablock and is the cheap poll after a swap, so
it is a read-only command and never runs in `mutation_transaction`.
"""

import os

from collections import Counter
from dataclasses import dataclass

import bpy

from ..file_paths import BLENDER_RELATIVE_PREFIX, canonical_path, create_save_directory
from ..helpers import MAX_LISTED_NAMES, count_by_type, record_page
from ..library_digest import require_digest_roots
from ..session import session_snapshot
from ..text_hygiene import client_safe_text
from .blend_files import (
    checked_blend_path,
    is_indirect_library,
    library_summary,
    operator_failure_message,
    path_frame,
    refuse_scripts_auto_execute,
    require_bool,
)
from .provenance import restore_provenance, stamp_provenance

# Sent in every successful swap result. A constant, so no path or name reaches it.
# It names `get_addon_status`, the MCP tool, not the `get_addon_info` command behind it:
# an agent reading this reply can only call tools, and the older wording sent one looking
# for a tool no client mounts. The server re-handshakes on its own when a response reports
# a different session (`connection.note_session_marker`), so this asks for confirmation,
# not for work.
_REHANDSHAKE_NOTE = (
    "The open database was replaced: session_epoch moved, and the addon's capabilities follow the "
    "file. The server re-handshakes automatically on the next command; call get_addon_status to see "
    "the new session before relying on a cached capability list."
)

# What Blender raises while reading back a database it has just replaced: a scene
# that is gone, a freed datablock, an RNA call that cannot answer yet. The swap
# itself has already happened, so none of them may reach the client as a failure.
_POST_SWAP_READ_ERRORS = (AttributeError, ReferenceError, RuntimeError, TypeError)


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
        str(getattr(library, "filepath", "")) for library in bpy.data.libraries if is_indirect_library(library)
    )
    return sum(count for count in relative.values() if count > 0)


@dataclass(frozen=True, slots=True)
class SaveRequest:
    """
    A save that has already been refused or accepted, with nothing left to decide.

    `from_client` is the only way to get one, and it runs every refusal: bad
    flags, an in-place save of a never-saved session, an unauthorized or
    unwritable target, an unconfirmed overwrite, an occupied temporary name, and
    checksums asked for with no roots to confine them to. Holding one therefore
    means the target may be written, which is what keeps refuse-before-mutate
    out of the hands of statement order.

    Attributes:
        canonical: The validated target path to hand Blender.
        requested: The path the client asked for, for the sanitized failure message.
        in_place: Whether this is a save over the open file.
        overwrites_existing: Whether the target already existed, as confirmed.
        compress: Passed to the operator.
        relative_remap: Passed to the operator.
        write_provenance: Whether to stamp the provenance block at all.
        digest_roots: The file roots library checksums are confined to; empty
            when the stamp records no checksum.

    """

    canonical: str
    requested: object
    in_place: bool
    overwrites_existing: bool
    compress: bool
    relative_remap: bool
    write_provenance: bool
    digest_roots: tuple[str, ...]

    @classmethod
    def from_client(
        cls,
        *,
        filepath: object,
        compress: object,
        relative_remap: object,
        confirm_overwrite: object,
        create_directories: object,
        write_provenance: object,
        provenance_checksums: object,
    ) -> "SaveRequest":
        """
        Validate one client's save, refusing before the filesystem is touched.

        Args:
            filepath: Where to save; None to save the open file in place.
            compress: Write a compressed `.blend`.
            relative_remap: Rewrite linked-library paths relative to the new location.
            confirm_overwrite: Required when the target already exists.
            create_directories: Create the target's missing directory and parents.
            write_provenance: Record the provenance block into every local scene.
            provenance_checksums: Also SHA-256 each linked library.

        Returns:
            SaveRequest: The accepted request.

        Raises:
            ValueError: When the request is refused; nothing was written.

        """
        compress = require_bool("compress", compress)
        relative_remap = require_bool("relative_remap", relative_remap)
        confirm_overwrite = require_bool("confirm_overwrite", confirm_overwrite)
        create_directories = require_bool("create_directories", create_directories)
        write_provenance = require_bool("write_provenance", write_provenance)
        provenance_checksums = require_bool("provenance_checksums", provenance_checksums)
        in_place = filepath is None
        if in_place and not bpy.data.filepath:
            raise ValueError("this session has never been saved, so it cannot be saved in place; pass a filepath")
        requested = bpy.data.filepath if in_place else filepath
        canonical = checked_blend_path(requested, must_exist=False, create_directories=create_directories)
        # `os.path.exists`, because the operator's own `check_existing` only
        # drives the file browser and does not stop a scripted overwrite.
        exists = os.path.exists(canonical)
        if exists and not confirm_overwrite:
            raise ValueError("the target .blend already exists; pass confirm_overwrite=true to replace it")
        _refuse_a_leftover_temp_save(canonical)
        hashing = write_provenance and provenance_checksums
        return cls(
            canonical=canonical,
            requested=requested,
            in_place=in_place,
            overwrites_existing=exists,
            compress=compress,
            relative_remap=relative_remap,
            write_provenance=write_provenance,
            digest_roots=tuple(require_digest_roots("provenance_checksums")) if hashing else (),
        )


@dataclass(frozen=True, slots=True)
class SaveOutcome:
    """
    What the save did, beside what the request asked for.

    Attributes:
        created_directory: Whether the target's directory had to be created.
        ingredients: How many linked libraries the provenance block records.
        broken_links: `_unresolvable_relative_paths`' count, read before the save.

    """

    created_directory: bool
    ingredients: int
    broken_links: int


def _save_with_provenance(request: SaveRequest) -> int:
    """
    Stamp the provenance block, run Blender's save, and undo the stamp if it fails.

    The stamp lives inside the same `try` as the operator, so a save Blender
    rejects leaves no scene carrying a claim about a file that was never written.

    Args:
        request: The accepted save.

    Returns:
        int: How many ingredients the stamp recorded; 0 when nothing was stamped.

    Raises:
        RuntimeError: When Blender could not write the file.

    """
    operator = bpy.ops.wm.save_mainfile if request.in_place else bpy.ops.wm.save_as_mainfile
    backup: list[tuple[object, bool, object]] = []
    ingredients = 0
    try:
        if request.write_provenance:
            backup, ingredients = stamp_provenance(request.digest_roots, request.canonical)
        # Raises RuntimeError on every failure mode; never returns CANCELLED.
        operator(filepath=request.canonical, compress=request.compress, relative_remap=request.relative_remap)
    except RuntimeError as exc:
        restore_provenance(backup)
        message = operator_failure_message("save_shot", exc, (request.requested, request.canonical))
        raise RuntimeError(message) from exc
    except Exception:
        restore_provenance(backup)
        raise
    return ingredients


def _save_report(request: SaveRequest, outcome: SaveOutcome) -> dict[str, object]:
    """
    Describe a completed save.

    Args:
        request: What was asked for and accepted.
        outcome: What the save itself did.

    Returns:
        dict[str, object]: See `save_shot`'s Returns.

    """
    session = session_snapshot()
    result: dict[str, object] = {
        "filepath": session["current_filepath"],
        "saved_in_place": request.in_place,
        "overwrote_existing": request.overwrites_existing,
        "created_directory": outcome.created_directory,
        "compress": request.compress,
        "relative_remap": request.relative_remap,
        "session_id": session["session_id"],
        "session_epoch": session["session_epoch"],
        "provenance_written": request.write_provenance,
    }
    if request.write_provenance:
        result["provenance_ingredients"] = outcome.ingredients
    if outcome.broken_links:
        count = outcome.broken_links
        noun = "path (images, libraries, etc.) is" if count == 1 else "paths (images, libraries, etc.) are"
        result["warnings"] = [
            f"{count} external file {noun} Blender-relative and will not resolve from the new "
            "directory, because relative_remap is false; save again with relative_remap=true, or relink"
        ]
    return result


def _counted_libraries() -> dict[str, object]:
    """
    Count the linked libraries beside a sample of their summaries.

    A shot can link any number of libraries and this rides on every session poll and
    swap, so it carries the count and the first few; `list_libraries` pages them all.

    Returns:
        dict[str, object]: `total`, `by_type` (`PRESENT` or `MISSING`, by whether the
        library's file was found - the one fact a poll must not miss past the sample),
        and a `record_page` of up to `MAX_LISTED_NAMES` `records`, each as
        `library_summary` describes.

    """
    frame = path_frame()
    libraries = list(bpy.data.libraries)
    return {
        "total": len(libraries),
        "by_type": count_by_type(
            "MISSING" if getattr(library, "is_missing", False) else "PRESENT" for library in libraries
        ),
        **record_page("records", libraries, lambda library: library_summary(library, frame=frame), MAX_LISTED_NAMES),
    }


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
            work a swap would destroy); and `libraries`, as `_counted_libraries`
            counts them.

        """
        return {
            **session_snapshot(),
            "is_dirty": bool(bpy.data.is_dirty),
            "libraries": _counted_libraries(),
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

        Never raises what reading a just-replaced database raises: the load has
        already happened, so a read failure here must not reach the client as a
        failed open. An unreadable field is reported under `warnings`. A
        `BaseException` such as Esc's `KeyboardInterrupt` is not caught, and the
        client is then told the session may be indeterminate although the load
        completed.

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
            # The scene's own objects, which is what `object_count` means everywhere else in
            # this surface (`list_scene_objects` reports the same number for the same moment).
            # `len(bpy.data.objects)` is a different question - it counts every object datablock
            # in the file, linked hierarchies included - and reporting it under this name had
            # two tools disagreeing by exactly the linked set: 31 against 16.
            report["object_count"] = len(bpy.context.scene.objects)
            report["datablock_object_count"] = len(bpy.data.objects)
            report["libraries"] = _counted_libraries()
            report["capabilities_changed"] = self._capability_names() != capabilities_before
        except _POST_SWAP_READ_ERRORS as exc:
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
            `object_count` (the scene's own objects), `datablock_object_count`
            (every object datablock in the file), `libraries`, `session_id`, `session_epoch`,
            `capabilities_changed`, `rehandshake_required` (always True) with a
            `note`, and `discarded_unsaved_changes`.

        Raises:
            ValueError: When the request is refused; nothing was loaded.
            RuntimeError: When Blender could not load it; the database is unchanged.

        """
        load_ui = require_bool("load_ui", load_ui)
        discard_unsaved = require_bool("discard_unsaved", discard_unsaved)
        canonical = checked_blend_path(filepath, must_exist=True)
        dirty = bool(bpy.data.is_dirty)
        if dirty and not discard_unsaved:
            raise ValueError(
                "the open session has unsaved changes that opening a file would destroy; "
                "save_shot first, or pass discard_unsaved=true"
            )
        refuse_scripts_auto_execute()
        capabilities_before = self._capability_names()
        try:
            # Raises RuntimeError on every failure mode; never returns CANCELLED.
            bpy.ops.wm.open_mainfile(filepath=canonical, load_ui=load_ui, use_scripts=False)
        except RuntimeError as exc:
            raise RuntimeError(operator_failure_message("open_shot", exc, (filepath, canonical))) from exc
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
        because the copy on disk may hold someone else's later save.

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
                Requires configured file roots, and a library past the per-file bound is
                recorded as skipped rather than read.

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
        request = SaveRequest.from_client(
            filepath=filepath,
            compress=compress,
            relative_remap=relative_remap,
            confirm_overwrite=confirm_overwrite,
            create_directories=create_directories,
            write_provenance=write_provenance,
            provenance_checksums=provenance_checksums,
        )
        # Counted while `bpy.data.filepath` still names the old file.
        broken_links = _unresolvable_relative_paths(request.canonical, request.relative_remap)
        created_directory = create_save_directory(request.canonical)
        ingredients = _save_with_provenance(request)
        return _save_report(request, SaveOutcome(created_directory, ingredients, broken_links))

    def reset_session(self, confirm_reset: object = False) -> dict[str, object]:
        """
        Replace the open database with an empty factory scene, as a pooled worker's reset step.

        Uses `wm.read_homefile` with factory startup, not
        `wm.read_factory_settings`, which also resets the user's preferences and
        unregisters every add-on, this one included. The factory startup file
        makes the reset scene the same on every machine. The load fires
        `load_post`, which moves the epoch, so this does not increment it.

        `confirm_reset=True` is the only consent needed, because discarding the session
        is the command's whole purpose.

        Args:
            confirm_reset: Must be True.

        Returns:
            dict[str, object]: As `open_shot`, with `filepath` None.

        Raises:
            ValueError: Without `confirm_reset=True`; nothing was reset.
            RuntimeError: When Blender could not reset.

        """
        if not require_bool("confirm_reset", confirm_reset):
            raise ValueError("reset_session discards the open file and any unsaved work; pass confirm_reset=true")
        dirty = bool(bpy.data.is_dirty)
        capabilities_before = self._capability_names()
        previous = bpy.data.filepath
        try:
            # Raises RuntimeError on failure; never returns CANCELLED.
            bpy.ops.wm.read_homefile(use_empty=True, use_factory_startup=True, load_ui=False)
        except RuntimeError as exc:
            raise RuntimeError(operator_failure_message("reset_session", exc, (previous,))) from exc
        return self._swap_report(capabilities_before, dirty)
