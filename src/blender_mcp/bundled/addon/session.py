"""
Session-lifecycle state: which .blend is loaded, and when that last changed.

The addon's advertised `capabilities` depend on per-.blend scene flags, and the
MCP server caches them, so opening another file can change what the addon can do
while a client trusts a stale list. `session_epoch` tells clients to re-read it.

The `bpy.app.handlers` callbacks here are the only writers and run on the main
thread. `session_snapshot()` is also called on client threads to stamp arriving
commands, so nothing it reads may touch `bpy`.

The epoch restarts at 0 whenever this module is imported afresh (a Blender
restart or Reload Scripts), so on its own it can repeat a value a client saw
against a different database. Clients compare `(session_id, session_epoch)`.
"""

import uuid

from dataclasses import dataclass

import bpy

from bpy.app.handlers import persistent

from .text_hygiene import client_safe_leaf
from .transaction import invalidate_active_transaction, library_replace_in_progress

# New on every import, so it changes whenever `_STATE` restarts at 0.
SESSION_ID = uuid.uuid4().hex


@dataclass
class _SessionState:
    """
    What the file-lifecycle handlers maintain between commands.

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


_STATE = _SessionState()


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


def _failure_note(action: str, file_path: object) -> str:
    """
    Describe a failed file operation without disclosing where it happened.

    Blender passes the failed file's absolute path, and this note reaches clients
    through `get_session_info`, so only a bounded, control-free leaf name is
    kept. The operator's own error text repeats the path and never comes here.

    Args:
        action: What was being attempted, e.g. "Loading".
        file_path: The path Blender reported, used only for its final component.

    Returns:
        str: A one-line note naming at most one leaf, with no separator, control
        character or traceback.

    """
    return f"{action} {client_safe_leaf(file_path)} failed; the operator's own error text is in Blender's console."


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
    _STATE.load_in_flight = True


@persistent
def _on_load_post(file_path: str = "", _unused: object = None) -> None:
    """
    Record a completed database swap.

    `wm.open_mainfile` and `wm.read_homefile` both reach here, so
    `reset_session` needs no epoch increment of its own. A completed load is the
    only thing that clears `session_indeterminate`.

    Also invalidates any open `mutation_transaction`: its snapshot describes the
    old file, and a rollback would remove the new file's datablocks. Swap
    commands bypass the transaction, so this covers a load made as a side effect
    of another command.

    Args:
        file_path: The .blend Blender loaded; empty for the startup file.
        _unused: Blender passes a second positional argument, always None.

    """
    invalidate_active_transaction()
    _STATE.session_epoch += 1
    _STATE.current_filepath = _reported_path(file_path)
    _STATE.last_load_error = None
    _STATE.load_in_flight = False
    _STATE.session_indeterminate = False


@persistent
def _on_load_post_fail(file_path: str = "", _unused: object = None) -> None:
    """
    Record a load that never landed, leaving the epoch alone.

    A failed `open_mainfile` leaves the database and its capabilities untouched;
    moving the epoch would make every client re-handshake for nothing.

    Clearing `load_in_flight` marks this load accounted for, so a later abort
    does not latch `session_indeterminate` over a database Blender left intact.

    Args:
        file_path: The .blend Blender could not load.
        _unused: Blender passes a second positional argument, always None.

    """
    _STATE.load_failures += 1
    _STATE.load_in_flight = False
    _STATE.last_load_error = _failure_note("Loading", file_path)


@persistent
def _on_save_post(file_path: str = "", _unused: object = None) -> None:
    """
    Follow the file a save moved the session to, without moving the epoch.

    A save changes the path but no capability. The path comes from
    `bpy.data.filepath`: `save_post` reports the file written, which after Save
    Copy (`save_as_mainfile(copy=True)`) is not the file open. The argument only
    decides whether the open file was saved, so a copy leaves `last_save_error`
    alone.

    Args:
        file_path: The .blend Blender wrote, which may not be the one open.
        _unused: Blender passes a second positional argument, always None.

    """
    written = _reported_path(file_path)
    _STATE.current_filepath = _reported_path(getattr(bpy.data, "filepath", ""))
    if written == _STATE.current_filepath:
        _STATE.last_save_error = None


@persistent
def _on_save_post_fail(file_path: str = "", _unused: object = None) -> None:
    """
    Record a save that never landed.

    Args:
        file_path: The .blend Blender could not write.
        _unused: Blender passes a second positional argument, always None.

    """
    _STATE.last_save_error = _failure_note("Saving", file_path)


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


# Published as `last_load_error` without `client_safe_leaf`, which is safe only
# because nothing client-supplied reaches this text.
INDETERMINATE_SESSION_NOTE = (
    "A session file swap was aborted before it finished; the open database may be "
    "partly replaced. Reopen a known shot before relying on this session."
)


def mark_session_indeterminate() -> None:
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
    """
    _STATE.session_epoch += 1
    _STATE.last_load_error = INDETERMINATE_SESSION_NOTE
    _STATE.session_indeterminate = True
    _STATE.current_filepath = None
    # The third place a load stops being in flight, and the reason the flag is
    # "a load whose outcome is unaccounted for" rather than "a load_pre has
    # fired at some point". The abort has now been accounted for - by this
    # call - so leaving the flag set would make the *next* abort, however
    # unrelated, latch on the strength of this one.
    _STATE.load_in_flight = False


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
    touch `bpy`. Every field is immutable, so it needs no lock.

    Returns:
        dict[str, object]: `session_id`, `session_epoch`, `current_filepath`,
        `last_load_error`, `last_save_error` and `session_indeterminate`.
        Clients compare the pair `(session_id, session_epoch)`.

    """
    return {
        "session_id": SESSION_ID,
        "session_epoch": _STATE.session_epoch,
        "current_filepath": _STATE.current_filepath,
        "last_load_error": _STATE.last_load_error,
        "last_save_error": _STATE.last_save_error,
        "session_indeterminate": _STATE.session_indeterminate,
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
    return _STATE.load_failures


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
    return _STATE.load_in_flight


def session_is_indeterminate() -> bool:
    """
    Report whether a swap was aborted with no completed load since.

    Separate from `session_snapshot()` because `server_core._drain_batch` asks
    once per command and has no use for the dict.

    Returns:
        bool: True while the open database may be part of two files.

    """
    return _STATE.session_indeterminate


def register_handlers() -> None:
    """
    Attach the file-lifecycle handlers, exactly once.

    Blender's handler lists accept duplicates, and a doubled `load_post` would
    move the epoch twice per swap. The lists hold the exact function objects, so
    the membership test finds an earlier registration.

    The epoch moves only when the open file differs from the recorded one, which
    catches a file opened while the addon was disabled and no handler watched.
    """
    for list_name, handler in _HANDLER_BINDINGS:
        handler_list = getattr(bpy.app.handlers, list_name)
        if handler not in handler_list:
            handler_list.append(handler)
    observed = _reported_path(getattr(bpy.data, "filepath", ""))
    if observed != _STATE.current_filepath:
        _STATE.session_epoch += 1
    _STATE.current_filepath = observed


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
