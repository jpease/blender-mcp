"""
File-lifecycle commands: which shot is open, and opening, saving or resetting it.

Task 3 created this module with `get_session_info`; Task 6 added `open_shot`,
`save_shot` and `reset_session`. `open_shot` and `reset_session` replace the
whole database, so they are `server_core._SESSION_SWAP_COMMANDS`: the drain loop
runs each as the last command of its tick and keeps it out of
`mutation_transaction`. `save_shot` replaces nothing and is deliberately not a
member (TASK_STATE decision T3-3).

Every path goes through `file_paths` (roots first, then `resolve_blend_path`),
and every operator failure through `sanitize_blender_error` with the paths the
call knew, because Blender's own error text embeds the absolute path.

Client-facing *names* this module publishes (library names and links, the
scene name) go through `..text_hygiene`, never through a rule written here: the
whole reason that module exists is that this field and `session._failure_note`
kept being fixed one at a time. Two other routes are deliberate: operator error
text goes through `file_paths.sanitize_blender_error`, and `filepath` in a
result is the absolute open-file path, published raw like `current_filepath`
(TASK_STATE T3-14).

`get_session_info` is read-only and touches no datablock, which is why it is in
`server_core._READ_ONLY_COMMANDS`: it is the cheap poll a client makes after a
swap it was told about, and it must never be wrapped in `mutation_transaction`.
"""

import os

from collections import Counter

import bpy

from ..file_paths import (
    BLENDER_RELATIVE_PREFIX,
    canonical_path,
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

# Long enough for a real project-relative link, short enough that this field
# cannot be used to push a multi-kilobyte string into an agent's context.
#
# **It is four times `client_safe_text`'s default, not "well under" it**, and an
# earlier revision of this comment said the opposite.
# `text_hygiene.MAX_NOTE_NAME_CHARS` is 64 and this is 256, so a link published
# whole is the longest single string this module emits. The bound that is
# actually being traded off is the *aggregate*: one entry per linked library
# multiplies it, and the library list is itself unbounded (recorded as a
# residual, owner Task 7). 256 is chosen against a real project-relative link -
# `//lib/chars/hero/hero_rig.blend` and several directories deeper - rather than
# against the leaf rule, which governs a name with no separators left in it and
# has no reason to be the same number.
_MAX_REPORTED_LINK_CHARS = 256


def _library_summary(library: object) -> dict[str, object]:
    r"""
    Describe one linked library by identity, not by where it sits on this machine.

    `Library.filepath` is whatever the link was made with, and the decision
    about how much of it may be published is **not taken here**. It is taken by
    `text_hygiene.safe_relative_link`, which is the single allowlist both this
    field and `session._failure_note` are gated on. Three repair cycles put the
    decision here as a blocklist and three critics walked through it - with
    `C:\\`, then U+FF0F, then U+FE68 and `///Users/...`. The predicate is
    inverted and shared so that the next character nobody has thought of is
    excluded by not being admitted.

    Which reduction applies:

    - A link `safe_relative_link` admits is published **exactly as that function
      returned it** - the normalised, control-stripped form it made its decision
      about. Gating one string and publishing another is what cycle 3 did: the
      gate ran on the raw text and the publisher then stripped format
      characters, manufacturing the `..` the gate had rejected.
    - Everything else - absolute, rooted (`///...`), traversing, over-long, or
      carrying a character the allowlist does not admit - is reduced to a leaf,
      because those are the studio's storage layout or a disguise for it, and
      this socket is unauthenticated.

    `is_relative` says which *kind* of link this is, and says it about the same
    stripped form the gate saw. It is not `filepath.startswith("//")`: that reported
    `///Users/victim/...` as relative, which is how a whole absolute path
    reached a client through the branch that exists to keep absolute paths out.

    **`name` is allowlisted too, and it is the fifth recurrence of the defect
    class above.** It sat one line up from `filepath` going through
    `client_safe_text` - which strips control characters and truncates and
    applies no allowlist at all - on the argument that a Blender ID name is a
    display label rather than a path. It is not: measured on Blender 5.2.2, a
    `Library.name` accepts `/Users/victim/shots/canon.blend`, `../../etc/passwd`
    and `C:\\studio\\vault` verbatim, and this socket is unauthenticated. In the
    benign case the name Blender mints *is* the basename, so the leaf rule
    returns it unchanged and the cost of the allowlist is zero. It goes through
    `client_safe_name_leaf`, the leaf rule without `client_safe_leaf`'s `isdir`
    call, because the name is author-chosen text, not a path on this machine
    (Task 7 cycle 3); `filepath` keeps `client_safe_leaf`, whose directory check
    exists for a path Blender reports.
    `tests/test_session_state.py::test_a_hostile_library_name_is_reduced_to_a_leaf_like_the_filepath_is`
    runs the whole hostile table through it.

    Args:
        library: A `bpy.types.Library`.

    Returns:
        dict[str, object]: `session_uid` - the handle Task 7's commands resolve
        by, *never* by name, because after a Route C override two libraries'
        contents can share a name. **A `session_uid` is valid only for the
        `(session_id, session_epoch)` it was read under.** Blender's own RNA
        description for it says "A session-wide unique identifier ... unchanged
        when reloading the file"; that is wrong, and a Task 7 implementer who
        reads RNA rather than this line will resolve the wrong library. Measured
        on 5.2.2 by `scripts/blender_probes/session_handlers.py`, which links one
        library into a shot and reopens the shot four times::

            uid sequence across four loads: [1452, 1485, 1518, 1551]
            STABLE across loads: False

        **The last line is the finding; the numbers are not.** They are
        allocation counters and move with everything the process did before the
        measurement. An earlier revision of this docstring quoted a three-value
        sequence attributed to "a critic", and no committed instrument produced
        it - the probe above did not measure session uids at all until this
        round. What the probe reproduces on any run is that the uid is different
        after every load, so a uid cached across a swap names nothing. `name`,
        reported for display only
        and reduced to an admissible leaf; `filepath` (whole only for a link the
        allowlist admits, a bare leaf otherwise); `is_relative`; and
        `is_missing`, which says the link is broken now and is what makes a swap
        or a reload worth attempting.

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

    With no file open, `bpy.path.abspath('//x.blend')` returns the bare
    `'x.blend'` (measured on 5.2.2, `scripts/blender_probes/file_path_error_shapes.py`),
    which would then resolve against the process CWD - a directory the caller
    never named.

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


def _checked_blend_path(raw: object, *, must_exist: bool) -> str:
    """
    Validate a `.blend` path: expand `//`, enforce the roots, then check the file.

    **The roots are enforced before `resolve_blend_path` looks at the file**, so a
    path outside them is refused with one message whether it names a file, a
    directory or nothing: checking existence first would answer that question
    about any path on the machine (`enforce_roots` canonicalizes its own input,
    so the order changes no containment decision). With no roots configured
    there is no boundary to protect and the file checks speak for themselves.

    Args:
        raw: The path the client sent.
        must_exist: True to open, False to save.

    Blender must be handed this canonical string, never the raw one: the raw
    form's `..` is resolved textually before symlinks, which is not the path the
    roots were checked against.

    Returns:
        str: The canonical path to hand to Blender. A refusal propagates as the
        `ValueError` the refusing check raised, whose message names no path.

    """
    expanded = _expand_blender_relative(raw)
    if isinstance(expanded, str) and expanded.strip() and "\x00" not in expanded:
        enforce_roots(expanded, configured_file_roots())
    return resolve_blend_path(expanded, must_exist=must_exist)


def _refuse_scripts_auto_execute(command: str = "open_shot") -> None:
    """
    Refuse a load while Blender is set to run scripts embedded in a `.blend`.

    Task 7's linking commands and the Poly Haven `.blend` import call it too.
    **It is not the control for script execution** (user decision, 2026-09-16):
    Blender gates drivers on the session flag (`-y` / `--enable-autoexec`, or
    `open_mainfile` / `revert_mainfile` with `use_scripts=True`, "Reload
    Trusted"), which this preference does not reflect - under `-y` it reads
    False and a linked library's Python driver still ran after a link, an
    override and a reload (Task 7 cycle-1 critic, 5.2.2). With the flag off
    nothing ran (`scripts/blender_probes/linking_scripts_auto_execute.py`). In
    the intended trusted deployments MCP loads follow Blender's own trust, as a
    manual link does; detecting the session flag is a Phase 4 requirement for
    pooled or untrusted deployments.

    `open_mainfile(use_scripts=False)` is always passed, but this preference is
    the same code-execution surface on an unauthenticated socket, and a user
    preference or app template can turn it on. **Refuse, not warn** (plan Task 6
    Step 4b's recommendation, taken): a refusal costs an artist who has it on
    nothing `open_shot` needs, and a warning is a control nobody reads. This is
    stricter than Task 5's permissive-when-unset roots on purpose: there the
    default is *unconfigured*; here the default is *safe* (False, measured) and
    someone changed it. A preference that cannot be read is treated as on.

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

    `open_mainfile`, `save_mainfile` and `save_as_mainfile` raise `RuntimeError`
    on every failure mode measured and never return `{'CANCELLED'}` (plan Task 6
    behaviour 1, 5.2.2; re-run by `scripts/blender_probes/file_lifecycle_handlers_real_blender.py`),
    and their text embeds the absolute path, up to twice. `read_homefile` was not
    seen to fail and is handled the same way. The raw text
    stays chained for Blender's console; only the sanitized form is the message.

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

    Blender writes `<target>@` and then renames it over the target (the `@`
    name is in its own error text). A symlink planted at that name redirected
    the write outside the configured roots and replaced the target with a link
    (cycle-1 critic, reproduced), so anything there - file, directory, or a
    symlink even when dangling, which is why this is `lstat` and not `exists` -
    refuses the save. Any other `OSError` (`EACCES`, `ENAMETOOLONG`) refuses too,
    with its own wording: its text would carry the path, and "cannot tell" must
    not mean "clear".

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

    Measured on 5.2.2 (`scripts/blender_probes/file_lifecycle_handlers_real_blender.py`
    section E): `Library.parent` is not reliable in the session that made the
    link (None until the file is reopened), but every datablock in `users_id`
    of an indirect library carries `is_library_indirect`. Blender re-derives an
    indirect library's path from its parent on load, so its `//` path does not
    break when the main file moves.

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

    With `relative_remap=False` Blender writes each path verbatim, so
    `//libs/lib.blend` or `//textures/t2.png` saved from `projA/` into `projB/`
    resolves against `projB/` on reopen and is missing, while the open session
    still reports it found (measured, section E of the probe above). The paths
    come from `bpy.utils.blend_paths(absolute=False, packed=False, local=True)`,
    which lists the main file's images, libraries and other external files
    (`local=True` skips paths inside linked datablocks, which resolve from their
    library) - but it also lists indirect libraries, which are subtracted. Read
    before the save: `bpy.data.filepath` is still the old file.

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


class FileLifecycleHandlersMixin:
    """
    Report and change which .blend the session holds.

    `get_session_info` is a `staticmethod` because it reads process-wide session
    state and the open database, not anything belonging to one server instance.
    It is still reached as `self.get_session_info` from the dispatch table, like
    every other handler. `open_shot` and `reset_session` are instance methods
    only because they compare `self._build_command_handlers()` across the swap.
    """

    @staticmethod
    def get_session_info() -> dict[str, object]:
        """
        Report the current session: which file, which epoch, and what went wrong.

        `session_epoch` is what a client compares against the epoch it last saw.
        When it has moved, the addon's advertised `capabilities` may have moved
        with it - the Poly Haven / Sketchfab / ND handlers are gated on
        per-`.blend` scene flags - so the client re-handshakes before trusting
        its cached list. `is_dirty` and `libraries` are what make a swap
        destructive, so both are reported before one is attempted.

        Returns:
            dict[str, object]: `session_id` (str, minted once per addon
            process) and `session_epoch` (int) - the pair a client compares,
            because the counter alone restarts at 0 with the process;
            `current_filepath` (str | None; None when the session has never been
            saved **and after an aborted swap, because this process can no
            longer truthfully name the file it has open**), `last_load_error` /
            `last_save_error` (str | None, carrying no filesystem path),
            `session_indeterminate` (bool - true while a swap was aborted
            part-way and no completed load has happened since; the drain loop
            refuses every command except this one, `get_addon_info` and the
            swap commands while it is set), `is_dirty` (bool - unsaved work a
            swap would destroy), and `libraries`, one
            `{"session_uid", "name", "filepath", "is_relative", "is_missing"}`
            entry per linked library (see `_library_summary` for which links are
            reported whole and which are reduced to a leaf).

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

        **It never raises an `Exception`.** It runs after an irreversible load,
        so an error here must not reach the client as "the open failed": the load
        stands and the client has to re-handshake either way. A field that could
        not be read is reported under `warnings` instead. **Known limit:** a
        `BaseException` (Esc's `KeyboardInterrupt`) raised here is not caught, and
        the client receives `server_core`'s abort message, which says the session
        may be indeterminate although `load_post` has already completed the load.

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

        **The protocol was decided by experiment, not preference:** TASK_STATE
        decision 11 (Task 2) - synchronous validate-then-swap, answering after
        `wm.open_mainfile` returns; the drain callback survives the load and
        `bpy.context.window` is None for the rest of that tick. So everything
        that can refuse runs **before** the operator: roots, the file checks,
        unsaved work, and the scripts preference. The drain loop makes this the
        last command of its tick and keeps it out of `mutation_transaction`
        (`server_core._SESSION_SWAP_COMMANDS`).

        `use_scripts=False` is passed explicitly and is not a parameter (spec
        Decision #7). A failure raises `RuntimeError`, never `{'CANCELLED'}`.

        **Known limit of the unsaved-work guard.** Blender clears `is_dirty` for
        a save only when it processes that save's notifier. `save_shot` ends its
        drain tick so nothing runs before that, but a save made by something
        else (another add-on's timer, a script) followed by an edit in the same
        main-thread pass can still leave `is_dirty` falsely clear, and this
        guard then lets the edit be discarded. A GUI Ctrl+S and File -> Save
        Copy are not affected (measured by the cycle-2 critic).

        Args:
            filepath: The `.blend` to open; absolute, `~`, or `//` relative to a
                saved open file.
            load_ui: Load the file's window layout too. Default False; the
                operator's own default is True.
            discard_unsaved: Required when the session has unsaved work, which
                an open destroys silently (measured, Task 2).

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
    ) -> dict[str, object]:
        """
        Write the open database to disk, refusing to replace an existing file unconfirmed.

        `filepath=None` saves in place with `wm.save_mainfile`; a path uses
        `wm.save_as_mainfile`, which moves the session to it. **Every target that
        already exists needs `confirm_overwrite=True`, including the open file
        itself**: an in-place save replaces the copy on disk, which may hold
        someone else's later save. The guard is `os.path.exists` before the
        operator runs; `check_existing` only drives the file browser's dialog and
        overwrites silently when called programmatically (plan Task 6 behaviour 3).
        A save is also refused while anything occupies Blender's temporary
        `<target>@` name (`_refuse_a_leftover_temp_save`).

        `compress` and `relative_remap` are passed on every call. Left out,
        `save_as_mainfile` rewrites library paths to `//` form (its default is
        True) and a factory-settings Blender compresses (`use_file_compression`
        beats the operator default; measured on 5.2.2). A failure raises
        `RuntimeError`, never `{'CANCELLED'}`.

        Args:
            filepath: Where to save; None to save the open file in place.
            compress: Write a compressed `.blend`. Default False: canon publishes
                must be uncompressed (spec §4.1 invariant 1).
            relative_remap: Rewrite linked-library paths relative to the new
                location. Default False.
            confirm_overwrite: Required when the target already exists. A
                confirmed overwrite also replaces an existing `.blend1` backup
                when Blender's `save_version` keeps one (measured, cycle-1 critic).

        Returns:
            dict[str, object]: `filepath` (the open file after the save),
            `saved_in_place`, `overwrote_existing`, `compress`, `relative_remap`,
            `session_id`, `session_epoch` (unchanged by a save), and `warnings`
            when `//`-relative external file paths (images, direct
            libraries) will not resolve from a new directory. **No `is_dirty`:** in the GUI Blender clears the flag
            when it processes the save's notifier, after this tick, so a value
            read here is untrue; poll `get_session_info` instead. The drain loop
            ends its tick after this command (`server_core._TICK_ENDING_COMMANDS`)
            so nothing queued behind it runs before that clear.

        Raises:
            ValueError: When the request is refused; nothing was written.
            RuntimeError: When Blender could not write the file.

        """
        compress = _require_bool("compress", compress)
        relative_remap = _require_bool("relative_remap", relative_remap)
        confirm_overwrite = _require_bool("confirm_overwrite", confirm_overwrite)
        in_place = filepath is None
        if in_place and not bpy.data.filepath:
            raise ValueError("this session has never been saved, so it cannot be saved in place; pass a filepath")
        requested = bpy.data.filepath if in_place else filepath
        canonical = _checked_blend_path(requested, must_exist=False)
        exists = os.path.exists(canonical)
        if exists and not confirm_overwrite:
            raise ValueError("the target .blend already exists; pass confirm_overwrite=true to replace it")
        _refuse_a_leftover_temp_save(canonical)
        broken_links = _unresolvable_relative_paths(canonical, relative_remap)
        operator = bpy.ops.wm.save_mainfile if in_place else bpy.ops.wm.save_as_mainfile
        try:
            # Raises RuntimeError on every failure mode; never returns CANCELLED.
            operator(filepath=canonical, compress=compress, relative_remap=relative_remap)
        except RuntimeError as exc:
            raise RuntimeError(_operator_failure_message("save_shot", exc, (requested, canonical))) from exc
        session = session_snapshot()
        result: dict[str, object] = {
            "filepath": session["current_filepath"],
            "saved_in_place": in_place,
            "overwrote_existing": exists,
            "compress": compress,
            "relative_remap": relative_remap,
            "session_id": session["session_id"],
            "session_epoch": session["session_epoch"],
        }
        if broken_links:
            noun = "path (images, libraries, etc.) is" if broken_links == 1 else "paths (images, libraries, etc.) are"
            result["warnings"] = [
                f"{broken_links} external file {noun} Blender-relative and will not resolve from the new "
                "directory, because relative_remap is false; save again with relative_remap=true, or relink"
            ]
        return result

    def reset_session(self, confirm: object = False) -> dict[str, object]:
        """
        Replace the open database with an empty factory scene (spec §4.5's pool reset).

        **`wm.read_homefile(use_empty=True, use_factory_startup=True)`, not the
        plan's `wm.read_factory_settings`.** Measured on 5.2.2 by
        `scripts/blender_probes/reset_operator_side_effects.py`:
        `read_factory_settings` also loads factory *preferences* and runs every
        enabled add-on's `unregister` - this addon's included, from inside its own
        command - and resets the user's preferences. `read_homefile` with the
        factory startup file leaves both alone, fires `load_post` (so the epoch
        moves once, with no increment here - TASK_STATE T3-4), and ignores the
        user's own startup file, so the reset scene is the same on every machine.

        `confirm=True` is the whole consent: discarding the session is this
        command's only effect, so no second unsaved-work flag is asked for; the
        result says whether unsaved work was discarded.

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
