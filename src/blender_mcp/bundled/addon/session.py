"""
Session-lifecycle state: which .blend is loaded, and when that last changed.

The addon's advertised `capabilities` depend on per-.blend scene flags, and the
MCP server caches them, so opening another file can change what the addon can do
while a client trusts a stale list. `session_epoch` tells clients to re-read it.

The state is a frozen value and every writer runs on the main thread: a pure
transition builds the whole next state, and the imperative shell publishes it in
one statement, by rebinding the single attribute `_STORE.state`.
`session_snapshot()` is also called on client threads to stamp arriving
commands, so nothing it reads may touch `bpy`, and a reader must never see a
state that is part-way through an event - hence the whole-value publish rather
than a field-at-a-time update.

The epoch restarts at 0 whenever this module is imported afresh (a Blender
restart or Reload Scripts), so on its own it can repeat a value a client saw
against a different database. Clients compare `(session_id, session_epoch)`.
"""

import os
import uuid

from contextlib import suppress
from dataclasses import dataclass, replace

import bpy

from bpy.app.handlers import persistent

from . import authored
from .text_hygiene import client_safe_leaf
from .transaction import invalidate_active_transaction, library_replace_in_progress

# New on every import, so it changes whenever the epoch restarts at 0.
SESSION_ID = uuid.uuid4().hex


@dataclass(frozen=True, slots=True)
class _SessionState:
    """
    What the file-lifecycle handlers maintain between commands.

    Frozen, because a client thread reads the aggregate while the main thread
    advances it: an event that changed fields in place would let a reader see
    the new epoch beside the old filepath. Each event builds a whole new value
    instead, and the shell publishes it by rebinding `_STORE.state`.

    Attributes:
        session_epoch: Moves whenever the open database may have changed: a
            completed load, an aborted one, or a different file found when the
            handlers are registered.
        current_filepath: The .blend currently open, or None when the session
            has never been saved or a swap was aborted.
        last_load_error: Client-safe note about the most recent failed load, or
            None once a load has succeeded.
        last_save_error: Client-safe note about the most recent failed save, or
            None once a save has succeeded.
        load_failures: How many times `load_post_fail` has fired in this
            process; never reset. It tells apart two identical failures, whose
            notes match. Left out of `session_snapshot()` so the handshake
            payload does not change.
        load_in_flight: True from `load_pre` until the load it announced is
            accounted for. The abort guard in `server_core._run_session_swap`
            reads it.
        session_indeterminate: True once a swap was aborted part-way, until a
            load completes. Unlike the note in `last_load_error`, it blocks:
            `server_core._drain_batch` refuses all but the commands that report
            or repair the condition.

    """

    session_epoch: int = 0
    current_filepath: str | None = None
    last_load_error: str | None = None
    last_save_error: str | None = None
    load_failures: int = 0
    load_in_flight: bool = False
    session_indeterminate: bool = False


class _StateStore:
    """
    The one mutable cell the session state lives in.

    The state has to be published in one step, because a client thread reads it
    while the main thread advances it. Assigning one attribute gives that, as a
    rebound module global would - but a global needs a `global` statement in
    every writer, which is the wider surface and what ruff's PLW0603 refuses.

    Attributes:
        state: The current state. Only the main thread assigns it, and it
            assigns a whole new value; `_STORE.state = <new>` is one
            `STORE_ATTR`, so a reader never sees a half-applied event.

    """

    __slots__ = ("state",)

    def __init__(self, state: _SessionState) -> None:
        """
        Hold the state the session starts from.

        Args:
            state: The state to publish until the first event applies.

        """
        self.state = state


_STORE = _StateStore(_SessionState())


# Published as `last_load_error` without `client_safe_leaf`, which is safe only
# because nothing client-supplied reaches this text.
INDETERMINATE_SESSION_NOTE = (
    "A session file swap was aborted before it finished; the open database may be "
    "partly replaced. Reopen a known shot before relying on this session."
)


def _reported_path(file_path: object) -> str | None:
    """
    Normalize a path Blender reported, turning "never saved" into None.

    Blender reports an unsaved session as an empty string, which reads as a real
    path in a client's logs and JSON.

    Args:
        file_path: The path Blender passed to a handler.

    Returns:
        str | None: The path, or None when Blender reported none.

    """
    reported = str(file_path or "")
    return reported or None


def _names_a_directory(file_path: object) -> bool:
    """
    Ask the filesystem whether a reported path is a directory.

    The one filesystem touch in this module, and the reason it is here rather
    than in `text_hygiene` or in a transition: given an empty path Blender
    reports the process working directory, which the caller never named, and
    publishing its leaf would disclose more than the failure does. `stat` is I/O
    and its answer is not a function of the state, so the shell asks and the
    transitions take the answer as data.

    Args:
        file_path: The path Blender reported.

    Returns:
        bool: True when the path names a directory on this machine. False for
        anything the filesystem refuses to answer for, including a name holding
        a NUL, which raises `ValueError` rather than reporting absence.

    """
    with suppress(OSError, ValueError):
        return os.path.isdir(str(file_path or ""))
    return False


def _failure_note(action: str, file_path: object, *, is_directory: bool = False) -> str:
    """
    Describe a failed file operation without disclosing where it happened.

    Blender passes the failed file's absolute path, and this note reaches clients
    through `get_session_info`, so only a bounded, control-free leaf name is
    kept. The operator's own error text repeats the path and never comes here.

    Args:
        action: What was being attempted, e.g. "Loading".
        file_path: The path Blender reported, used only for its final component.
        is_directory: Whether that path names a directory, from
            `_names_a_directory`. A directory is never named in the note.

    Returns:
        str: A one-line note naming at most one leaf, with no separator, control
        character or traceback.

    """
    leaf = client_safe_leaf(file_path, is_directory=is_directory)
    return f"{action} {leaf} failed; the operator's own error text is in Blender's console."


def applied_load_pre(state: _SessionState) -> _SessionState:
    """
    Apply the announcement that Blender has begun a load.

    Nothing else moves: `load_pre` fires while `bpy.data` still holds the old
    file, so the recorded path and epoch still describe what is open.

    Args:
        state: The state before the load was announced.

    Returns:
        _SessionState: The same state with a load marked in flight.

    """
    return replace(state, load_in_flight=True)


def applied_load_post(state: _SessionState, file_path: object) -> _SessionState:
    """
    Apply a completed database swap.

    `wm.open_mainfile` and `wm.read_homefile` both reach here, so
    `reset_session` needs no epoch increment of its own. A completed load is the
    only thing that clears `session_indeterminate`, and it accounts for the load
    `load_pre` announced.

    Args:
        state: The state before the load completed.
        file_path: The .blend Blender loaded; empty for the startup file.

    Returns:
        _SessionState: A state one epoch on, naming the loaded file, with no
        load error, no load in flight and no indeterminate latch.

    """
    return replace(
        state,
        session_epoch=state.session_epoch + 1,
        current_filepath=_reported_path(file_path),
        last_load_error=None,
        load_in_flight=False,
        session_indeterminate=False,
    )


def applied_load_failure(state: _SessionState, file_path: object, *, is_directory: bool = False) -> _SessionState:
    """
    Apply a load that never landed, leaving the epoch alone.

    A failed `open_mainfile` leaves the database and its capabilities untouched;
    moving the epoch would make every client re-handshake for nothing. For the
    same reason `current_filepath` and `session_indeterminate` stay: the file
    Blender had open is still the file Blender has open.

    Clearing `load_in_flight` marks this load accounted for, so a later abort
    does not latch `session_indeterminate` over a database Blender left intact.

    Args:
        state: The state before the load failed.
        file_path: The .blend Blender could not load.
        is_directory: Whether that path names a directory, from
            `_names_a_directory`; the note never names one.

    Returns:
        _SessionState: A state carrying one more counted failure and a
        client-safe note, at the same epoch.

    """
    return replace(
        state,
        load_failures=state.load_failures + 1,
        load_in_flight=False,
        last_load_error=_failure_note("Loading", file_path, is_directory=is_directory),
    )


def applied_save_post(state: _SessionState, written: object, open_path: object) -> _SessionState:
    """
    Follow the file a save moved the session to, without moving the epoch.

    A save changes the path but no capability. The session follows `open_path`,
    Blender's `bpy.data.filepath`: `save_post` reports the file written, which
    after Save Copy (`save_as_mainfile(copy=True)`) is not the file open. The
    written path only decides whether the open file was saved, so a copy leaves
    `last_save_error` alone - a copy is no evidence that the file the error
    belongs to can now be written.

    Args:
        state: The state before the save.
        written: The .blend Blender wrote, which may not be the one open.
        open_path: The .blend now open, read from `bpy.data.filepath`.

    Returns:
        _SessionState: A state naming the open file, at the same epoch, with the
        save error cleared only when the open file is what was written.

    """
    now_open = _reported_path(open_path)
    saved_the_open_file = _reported_path(written) == now_open
    return replace(
        state,
        current_filepath=now_open,
        last_save_error=None if saved_the_open_file else state.last_save_error,
    )


def applied_save_failure(state: _SessionState, file_path: object, *, is_directory: bool = False) -> _SessionState:
    """
    Apply a save that never landed.

    Args:
        state: The state before the save failed.
        file_path: The .blend Blender could not write.
        is_directory: Whether that path names a directory, from
            `_names_a_directory`; the note never names one.

    Returns:
        _SessionState: The same state carrying a client-safe note about the
        failed save.

    """
    return replace(state, last_save_error=_failure_note("Saving", file_path, is_directory=is_directory))


def applied_indeterminate(state: _SessionState) -> _SessionState:
    """
    Invalidate every stamp taken against a database a load stopped part-way through.

    The one failure that moves the epoch. A failed load fires `load_post_fail`
    and leaves the database untouched. An abort after `load_pre`, such as Esc's
    `KeyboardInterrupt`, fires neither `load_post` nor `load_post_fail` and may
    leave the database half replaced; without a new epoch, commands stamped
    during the load would still match and run against it. Being wrong costs
    each connected process one re-handshake.

    The note in `last_load_error` only informs; `session_indeterminate` blocks
    commands in `server_core._drain_batch` until a completed load clears it.
    `current_filepath` is cleared because the open file can no longer be named.

    Args:
        state: The state as of the abort.

    Returns:
        _SessionState: A state one epoch on, naming no file, latched
        indeterminate and with no load in flight.

    """
    return replace(
        state,
        session_epoch=state.session_epoch + 1,
        last_load_error=INDETERMINATE_SESSION_NOTE,
        session_indeterminate=True,
        current_filepath=None,
        # The third place a load stops being in flight, and the reason the flag
        # is "a load whose outcome is unaccounted for" rather than "a load_pre
        # has fired at some point". The abort has now been accounted for - by
        # this transition - so leaving the flag set would make the *next* abort,
        # however unrelated, latch on the strength of this one.
        load_in_flight=False,
    )


def applied_registration(state: _SessionState, observed_path: object) -> _SessionState:
    """
    Reconcile the recorded file with the one Blender actually has open.

    The epoch moves only when the open file differs from the recorded one, which
    catches a file opened while the addon was disabled and no handler watched.
    Re-enabling on the same file must not move it, or every toggle would cost
    each connected client a re-handshake.

    Args:
        state: The state left by the previous registration cycle.
        observed_path: The .blend Blender has open, read from `bpy.data.filepath`.

    Returns:
        _SessionState: A state naming the observed file, one epoch on when that
        is not the file already recorded.

    """
    observed = _reported_path(observed_path)
    missed_a_swap = observed != state.current_filepath
    return replace(
        state,
        session_epoch=state.session_epoch + (1 if missed_a_swap else 0),
        current_filepath=observed,
    )


@persistent
def _on_load_pre(_file_path: str = "", _unused: object = None) -> None:
    """
    Record that Blender has begun a load, before it has replaced anything.

    The only direct evidence that a load began. Inferring it from counters fails
    when a failed open is followed by an aborted real one: the failure moves the
    counters, so the abort looks accounted for.

    Blender fires this for every `wm.open_mainfile`, succeeding or failing, and
    for `wm.read_homefile`, while `bpy.data` still holds the old file. So an
    abort before this handler cannot have touched the database. Saves fire no
    load handler.

    Args:
        _file_path: The .blend Blender is about to read; unused, because
            `load_post` records the path once the load has happened.
        _unused: Blender passes a second positional argument, always None.

    """
    _STORE.state = applied_load_pre(_STORE.state)


@persistent
def _on_load_post(file_path: str = "", _unused: object = None) -> None:
    """
    Record a completed database swap.

    Also invalidates any open `mutation_transaction`: its snapshot describes the
    old file, and a rollback would remove the new file's datablocks. Swap
    commands bypass the transaction, so this covers a load made as a side effect
    of another command.

    Args:
        file_path: The .blend Blender loaded; empty for the startup file.
        _unused: Blender passes a second positional argument, always None.

    """
    invalidate_active_transaction()
    # The datablocks this session authored belong to the file that was just replaced; the
    # provenance block of the new one must not claim them.
    authored.clear()
    _STORE.state = applied_load_post(_STORE.state, file_path)


@persistent
def _on_load_post_fail(file_path: str = "", _unused: object = None) -> None:
    """
    Record a load that never landed, leaving the epoch alone.

    Args:
        file_path: The .blend Blender could not load.
        _unused: Blender passes a second positional argument, always None.

    """
    _STORE.state = applied_load_failure(_STORE.state, file_path, is_directory=_names_a_directory(file_path))


@persistent
def _on_save_post(file_path: str = "", _unused: object = None) -> None:
    """
    Follow the file a save moved the session to, without moving the epoch.

    Args:
        file_path: The .blend Blender wrote, which may not be the one open.
        _unused: Blender passes a second positional argument, always None.

    """
    _STORE.state = applied_save_post(_STORE.state, file_path, getattr(bpy.data, "filepath", ""))


@persistent
def _on_save_post_fail(file_path: str = "", _unused: object = None) -> None:
    """
    Record a save that never landed.

    Args:
        file_path: The .blend Blender could not write.
        _unused: Blender passes a second positional argument, always None.

    """
    _STORE.state = applied_save_failure(_STORE.state, file_path, is_directory=_names_a_directory(file_path))


@persistent
def _on_blend_import_post(_context: object = None, _unused: object = None) -> None:
    """
    Invalidate the open transaction when a library's contents were replaced in place.

    A library reload or relocate fires `blend_import_post`, not `load_post`, and
    gives each datablock linked from that library a fresh session_uid. A link or
    append fires it too, and those new datablocks must still roll back, so this
    acts only inside `transaction.replacing_library_contents`. The context
    argument is not known to tell them apart. The epoch does not move: a reload
    is not a session swap.

    Args:
        _context: Blender's `BlendImportContext`; unused.
        _unused: Blender passes a second positional argument, always None.

    """
    if library_replace_in_progress():
        invalidate_active_transaction()


def mark_session_indeterminate() -> None:
    """
    Latch the session as indeterminate after a swap was aborted part-way through.

    Called from the main thread by `server_core._run_session_swap`. See
    `applied_indeterminate` for why this is the one failure that moves the epoch.
    """
    _STORE.state = applied_indeterminate(_STORE.state)
    # The swap was aborted part-way: what is open cannot be described truthfully, so the
    # authorship claim goes with it.
    authored.clear()


# One table, so registration, removal and the duplicate check cannot drift apart.
_HANDLER_BINDINGS = (
    ("load_pre", _on_load_pre),
    ("load_post", _on_load_post),
    ("load_post_fail", _on_load_post_fail),
    ("save_post", _on_save_post),
    ("save_post_fail", _on_save_post_fail),
    ("blend_import_post", _on_blend_import_post),
)


def session_snapshot() -> dict[str, object]:
    """
    Read the current session state as a JSON-serializable dict.

    Also called on client threads to stamp arriving commands, so it must not
    touch `bpy`. It needs no lock, but only because of how the state is written:
    each event builds a whole new frozen `_SessionState` and the handler
    publishes it by assigning `_STORE.state`, which is a single `STORE_ATTR`.
    Reading that attribute once into a local therefore yields either the state
    before the event or the state after it, never a mix - the pairing
    `server_core._drain_batch` trusts, a bumped epoch beside a still-False
    `session_indeterminate`, cannot be observed.

    Returns:
        dict[str, object]: `session_id`, `session_epoch`, `current_filepath`,
        `last_load_error`, `last_save_error` and `session_indeterminate`.
        Clients compare the pair `(session_id, session_epoch)`.

    """
    state = _STORE.state
    return {
        "session_id": SESSION_ID,
        "session_epoch": state.session_epoch,
        "current_filepath": state.current_filepath,
        "last_load_error": state.last_load_error,
        "last_save_error": state.last_save_error,
        "session_indeterminate": state.session_indeterminate,
    }


def load_failures_seen() -> int:
    """
    Report how many times `load_post_fail` has fired in this process.

    Unpublished and reset with the module, so only a difference across a window
    means anything. The abort guard must not use it: a failed open followed by an
    aborted real load moves it on the first, hiding the abort.

    Returns:
        int: A monotonically increasing count of failed loads.

    """
    return _STORE.state.load_failures


def load_in_flight() -> bool:
    """
    Report whether Blender announced a load whose outcome is still unaccounted for.

    True from `load_pre` until `load_post`, `load_post_fail` or
    `mark_session_indeterminate`. Read on the main thread by
    `server_core._run_session_swap` after an abort.

    Returns:
        bool: True while a load is part-way through, when an abort may leave the
        database part of two files.

    """
    return _STORE.state.load_in_flight


def session_is_indeterminate() -> bool:
    """
    Report whether a swap was aborted with no completed load since.

    Separate from `session_snapshot()` because `server_core._drain_batch` asks
    once per command and has no use for the dict.

    Returns:
        bool: True while the open database may be part of two files.

    """
    return _STORE.state.session_indeterminate


def register_handlers() -> None:
    """
    Attach the file-lifecycle handlers, exactly once.

    Blender's handler lists accept duplicates, and a doubled `load_post` would
    move the epoch twice per swap. The lists hold the exact function objects, so
    the membership test finds an earlier registration.
    """
    for list_name, handler in _HANDLER_BINDINGS:
        handler_list = getattr(bpy.app.handlers, list_name)
        if handler not in handler_list:
            handler_list.append(handler)
    _STORE.state = applied_registration(_STORE.state, getattr(bpy.data, "filepath", ""))


def unregister_handlers() -> None:
    """
    Detach the file-lifecycle handlers, including any a previous cycle stacked.

    Runs after a `register()` that may have failed part-way, so a missing
    handler is normal; `list.remove` would raise for it.
    """
    for list_name, handler in _HANDLER_BINDINGS:
        handler_list = getattr(bpy.app.handlers, list_name)
        while handler in handler_list:
            handler_list.remove(handler)
