import contextlib

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass

import bpy

from .object_state import (
    backup_datablock_ids,
    capture_object_states,
    discard_backups,
    invalidate_object_states,
    restore_object_states,
)
from .text_hygiene import client_safe_text

# Every bpy.data collection a mutating handler could plausibly create
# datablocks in - modifier/texture/material creation (materials, textures,
# node_groups), imports (objects, meshes, armatures, actions, images, ...),
# and model helpers (objects, collections). Broad on purpose so new handlers
# get rollback coverage for free without updating this list.
_TRACKED_COLLECTIONS = (
    "objects",
    "meshes",
    "curves",
    "materials",
    "textures",
    "images",
    "node_groups",
    "worlds",
    "actions",
    "armatures",
    "cameras",
    "lights",
    "collections",
    "pointclouds",
    "volumes",
    "metaballs",
    "lattices",
    "grease_pencils",
    # So a failed `link_canon_library` does not leak its Library. Removed last:
    # freeing a Library frees its linked datablocks, and a later `remove()` on
    # one raises a ReferenceError that `suppress` would hide.
    "libraries",
)

# Appended to the error message when a rollback was skipped, so the client
# learns its partial work was not undone. Constant text: no path reaches it.
ROLLBACK_SKIPPED_WARNING = (
    "The open database was replaced during this command (a file load or library reload), "
    "so its earlier changes were not rolled back; inspect the scene before relying on it."
)

# A Library's own user count does not describe whether Blender writes it, and linked
# datablocks belong to their own file: neither is this command's authored work.
_AUTHORSHIP_EXEMPT_COLLECTIONS = frozenset({"libraries"})

# The warning rides in every mutating reply, which has 8 KiB for the actual result.
MAX_REPORTED_UNREFERENCED = 5
MAX_REPORTED_NAME_CHARS = 40


def unreferenced_warning(entries: Sequence[Mapping[str, str]]) -> str | None:
    """
    Phrase `Transaction.unreferenced_created()` output as one bounded client warning.

    Args:
        entries: The `{"collection": ..., "name": ...}` records to describe.

    Returns:
        str | None: One warning naming at most `MAX_REPORTED_UNREFERENCED` entries,
        or None when nothing will be discarded.

    """
    if not entries:
        return None
    shown = ", ".join(f"{entry['collection']}:{entry['name']}" for entry in entries[:MAX_REPORTED_UNREFERENCED])
    overflow = len(entries) - MAX_REPORTED_UNREFERENCED
    if overflow > 0:
        shown = f"{shown} (+{overflow} more)"
    noun = "datablock" if len(entries) == 1 else "datablocks"
    return (
        f"{len(entries)} {noun} this command created have no user, so Blender discards them when "
        f"the file is saved: {shown}. Assign them, or set use_fake_user. "
        "validate_scene(scope=['persistence']) lists all of them."
    )


@dataclass
class _DispatchState:
    """
    What the file handlers in `session.py` need to know about the command running now.

    Assigned inside `mutation_transaction` / `replacing_library_contents` and
    read from Blender's file handlers; both run on Blender's main thread, so no
    lock.

    Attributes:
        active: The transaction wrapping the command running now, or None.
        library_replace_in_progress: True while a handler is replacing a
            library's contents in place (`lib.reload()`).

    """

    active: "Transaction | None" = None
    library_replace_in_progress: bool = False


_DISPATCH = _DispatchState()


class RollbackSkippedError(Exception):
    """A command failed after its transaction was invalidated, so nothing was rolled back."""


def _snapshot_ids():
    """
    Capture the session_uid of every datablock in every tracked collection.

    session_uid (not name) is the identity key: it is documented stable across
    renames and internal reallocations, so a datablock renamed during a failed
    request is still recognised as pre-existing and never mistaken for a new
    one. A datablock without a session_uid (should not happen on Blender 5.1)
    is skipped rather than crashing the snapshot.

    Returns:
        dict[str, set[int]]: collection name -> set of session_uids present now.

    """
    snapshot = {}
    for coll_name in _TRACKED_COLLECTIONS:
        ids = set()
        for db in getattr(bpy.data, coll_name, ()):
            uid = getattr(db, "session_uid", None)
            if uid is not None:
                ids.add(uid)
        snapshot[coll_name] = ids
    return snapshot


def _new_datablocks(before, exclude_ids: "frozenset[int] | set[int]" = frozenset()):
    """
    Diff the current bpy.data state against an earlier session_uid snapshot.

    Args:
        before: A snapshot previously returned by _snapshot_ids().
        exclude_ids: session_uids to treat as not-new even when absent from the
            snapshot - used for the transaction's own geometry-backup meshes,
            which are rollback scaffolding rather than handler output.

    Returns:
        list[tuple[str, ID]]: (collection name, datablock) pairs created since
        the snapshot, in creation/iteration order.

    """
    created = []
    for coll_name in _TRACKED_COLLECTIONS:
        before_ids = before.get(coll_name, set())
        for db in getattr(bpy.data, coll_name, ()):
            uid = getattr(db, "session_uid", None)
            if uid is None or uid in before_ids or uid in exclude_ids:
                continue
            created.append((coll_name, db))
    return created


def removal_order[T](entries: Sequence[tuple[str, T]]) -> list[tuple[str, T]]:
    """
    Order newly-created datablocks so each is removed before whatever frees it.

    Objects come first (and in reverse creation order) since a later object
    could reference an earlier one; everything else follows, also in reverse;
    libraries go last, because removing a Library frees the datablocks linked
    from it and those must already be gone.

    Pure, so the ordering rule can be exercised without a `bpy.data` that can
    only be observed through the removals it accepts.

    Args:
        entries: (collection name, datablock) pairs in creation order, as
            returned by _new_datablocks().

    Returns:
        list[tuple[str, T]]: The same pairs, in removal order.

    """
    objects = [(coll_name, db) for coll_name, db in entries if coll_name == "objects"]
    others = [(coll_name, db) for coll_name, db in entries if coll_name not in {"objects", "libraries"}]
    libraries = [(coll_name, db) for coll_name, db in entries if coll_name == "libraries"]
    return [*reversed(objects), *reversed(others), *reversed(libraries)]


def _remove_datablocks(entries) -> None:
    """
    Best-effort removal of newly-created datablocks after a failed mutation.

    Removal follows `removal_order`; each removal is isolated so one failure
    does not stop the rest.

    Args:
        entries: (collection name, datablock) pairs, as returned by _new_datablocks().

    """
    for coll_name, datablock in removal_order(entries):
        with contextlib.suppress(Exception):
            getattr(bpy.data, coll_name).remove(datablock, do_unlink=True)


def _undo_unavailable_reason():
    """
    Report why a global-undo checkpoint cannot be recorded, if it cannot.

    Undo is a no-op in background mode and when the user disabled global undo
    in preferences (Blender docs: use_global_undo). Both are documented,
    reliable signals - unlike undo_push's undocumented return value.

    Returns:
        str | None: A short reason, or None when undo should be available.

    """
    if getattr(bpy.app, "background", False):
        return "Blender is running in background mode"
    with contextlib.suppress(Exception):
        if not bpy.context.preferences.edit.use_global_undo:
            return "global undo is disabled in Blender preferences"
    return None


def _push_undo_checkpoint(message):
    """
    Push one named undo step, reporting (never silently suppressing) when undo
    protection is unavailable - a caller-visible warning, not the rollback
    mechanism itself.

    Args:
        message: Label shown in Blender's Undo History for this step.

    Returns:
        str | None: A warning to surface to the client when the checkpoint
        could not be created, or None on success.

    """
    reason = _undo_unavailable_reason()
    if reason is not None:
        print(f"BlenderMCP: undo checkpoint skipped - {reason}")
        return (
            f"Undo checkpoint unavailable ({reason}): this operation is not "
            "individually undoable via Blender's Undo History."
        )
    try:
        result = bpy.ops.ed.undo_push(message=message)
    except Exception as e:
        print(f"BlenderMCP: undo_push failed - {e!s}")
        return "Undo checkpoint could not be created: this operation is not individually undoable via Blender's Undo History."
    # undo_push's return is undocumented ("internal use only"); only an explicit
    # CANCELLED is a reliable failure signal. A None/other return is treated as
    # success rather than risk a false "unavailable" warning on every call.
    if isinstance(result, (set, frozenset)) and "CANCELLED" in result:
        print(f"BlenderMCP: undo_push returned {result}")
        return "Undo checkpoint could not be created: this operation is not individually undoable via Blender's Undo History."
    return None


class Transaction:
    """
    One mutating MCP request's rollback bookkeeping.

    Holds the pre-mutation session_uid snapshot and the captured state of the
    objects the request touches, so a failure can both remove datablocks the
    request created and restore the existing objects it changed.
    """

    def __init__(self, cmd_type) -> None:
        self.cmd_type = cmd_type
        self._before_ids = {}
        self._states = []
        self._backup_ids: frozenset[int] | set[int] = frozenset()
        self.committed = False
        self.invalidated = False

    def begin(self, targets, capture_geometry) -> None:
        self._before_ids = _snapshot_ids()
        self._states = capture_object_states(targets, capture_geometry=capture_geometry)
        self._backup_ids = backup_datablock_ids(self._states)

    def invalidate(self) -> None:
        """
        Disarm this transaction because the database it snapshotted was replaced.

        A load or library reload gives the replaced datablocks fresh
        session_uids, so a rollback would take the new contents for this
        request's and remove them. The captured object states may reference
        freed datablocks, so they are released unread.
        """
        self.invalidated = True
        self._before_ids = {}
        self._backup_ids = frozenset()
        invalidate_object_states(self._states)
        self._states = []

    def _created_pairs(self):
        """
        Yield `(collection, datablock)` for local datablocks this request created.

        Live datablocks, not sanitized records: `users` and `library` exist on the
        object only.

        Returns:
            list[tuple[str, ID]]: Pairs in creation order, empty when invalidated.

        """
        if self.invalidated:
            return []
        return [
            (coll_name, datablock)
            for coll_name, datablock in _new_datablocks(self._before_ids, exclude_ids=self._backup_ids)
            if coll_name not in _AUTHORSHIP_EXEMPT_COLLECTIONS and getattr(datablock, "library", None) is None
        ]

    def created_datablocks(self) -> list[dict[str, str]]:
        """
        Name every local datablock this request created, sanitized for the wire and the file.

        Returns:
            list[dict[str, str]]: `{"collection": ..., "name": ...}` records, sorted.

        """
        return sorted(
            (
                {
                    "collection": coll_name,
                    "name": client_safe_text(getattr(db, "name", ""), MAX_REPORTED_NAME_CHARS),
                }
                for coll_name, db in self._created_pairs()
            ),
            key=lambda entry: (entry["collection"], entry["name"]),
        )

    def unreferenced_created(self) -> list[dict[str, str]]:
        """
        Name the created datablocks Blender will discard at save.

        `users == 0` is the whole test: a fake user counts as a user, so a datablock
        kept deliberately never appears here. Linked datablocks and the `libraries`
        collection are already excluded by `_created_pairs`. Read before `commit()`.

        Returns:
            list[dict[str, str]]: `{"collection": ..., "name": ...}` records, sorted.

        """
        return sorted(
            (
                {
                    "collection": coll_name,
                    "name": client_safe_text(getattr(db, "name", ""), MAX_REPORTED_NAME_CHARS),
                }
                for coll_name, db in self._created_pairs()
                if int(getattr(db, "users", 1) or 0) == 0
            ),
            key=lambda entry: (entry["collection"], entry["name"]),
        )

    def rollback(self) -> str | None:
        """
        Undo a failed request, unless the transaction was invalidated.

        Returns:
            str | None: `ROLLBACK_SKIPPED_WARNING` when invalidated (nothing was
            touched), else None.

        """
        if self.invalidated:
            return ROLLBACK_SKIPPED_WARNING
        # Restore existing objects first: a slot/parent may reference a
        # datablock this request created, and we want the reference put back to
        # its pre-request target before that created datablock is removed.
        restore_object_states(self._states)
        _remove_datablocks(_new_datablocks(self._before_ids, exclude_ids=self._backup_ids))
        # Any backups not consumed by a geometry restore are pure scaffolding.
        discard_backups(self._states)
        return None

    def commit(self):
        """
        Finalise a successful request: drop rollback scaffolding and leave one
        named undo checkpoint.

        Returns:
            str | None: A warning to surface when undo protection was
            unavailable, else None. Idempotent - a second call is a no-op.

        """
        if self.committed:
            return None
        self.committed = True
        discard_backups(self._states)
        return _push_undo_checkpoint(f"MCP: {self.cmd_type}")

    def finish_without_checkpoint(self) -> None:
        """Discard rollback backups for a confirmed no-change/cancelled result."""
        if self.committed:
            return
        self.committed = True
        discard_backups(self._states)


@contextlib.contextmanager
def mutation_transaction(cmd_type, targets=(), capture_geometry=False):
    """
    Wrap one mutating MCP request with identity-based rollback.

    Guarantees when an `Exception` leaves the block:
    - every datablock the request *created* is removed, identified by
      session_uid so a renamed pre-existing datablock is never deleted; and
    - the captured state of each target object is restored: name, data name,
      local transform, parent, material-slot assignments, modifiers added
      during the request, and (for geometry-editing commands) the mesh itself.

    On success it leaves exactly one named undo checkpoint, returning a warning
    via `Transaction.commit()` when that checkpoint could not be created.

    If the database is replaced while the block runs (`load_post`, or
    `blend_import_post` inside `replacing_library_contents`), the transaction is
    invalidated: a later failure removes and restores nothing, and the error
    message carries `ROLLBACK_SKIPPED_WARNING`.

    Explicitly NOT guaranteed (documented limitations, not silent gaps):
    deleted pre-existing datablocks are not resurrected; applied modifiers
    (e.g. nd_apply_modifiers) are irreversible; object state outside the
    captured fields is not restored. Rollback removes tracked datablocks
    directly rather than calling bpy.ops.ed.undo(): the undo stack is bounded
    (undo_steps, default 32) and evictable, is a no-op in background mode /
    when global undo is off, and is documented "internal use only" - none of
    which is a sound basis for correctness.

    A `BaseException` does NOT roll back. The one that reaches here in practice
    is the `KeyboardInterrupt` Blender raises when the user presses Esc: the
    partial mutation is left in place, `_DISPATCH.active` is still cleared by
    the `finally`, and `drain_command_queue`'s abort guard answers the client
    and latches the session indeterminate when the abort interrupted a load.

    Args:
        cmd_type: The MCP command type, used to label the undo checkpoint.
        targets: Existing objects the request will touch, whose state is
            captured for restore-on-failure.
        capture_geometry: When True, back up each target's mesh so a failed
            geometry edit can be reverted.

    Yields:
        Transaction: call .commit() after the handler succeeds to push the
        checkpoint and collect any undo-unavailability warning.

    Raises:
        RollbackSkippedError: When the block failed after the transaction was
            invalidated; chained from the original exception.

    """
    txn = Transaction(cmd_type)
    txn.begin(targets, capture_geometry)
    previous = _DISPATCH.active
    _DISPATCH.active = txn
    try:
        yield txn
    # `Exception`, not `BaseException`, deliberately: a rollback started from
    # Esc's KeyboardInterrupt does removals and restores that a second Esc can
    # interrupt part-way, leaving a database that is neither what the handler
    # built nor what the snapshot describes. Leaving the partial mutation alone
    # is the recoverable outcome, and the drain loop's abort guard reports it.
    except Exception as exc:
        warning = txn.rollback()
        if warning is None:
            raise
        # The error envelope carries only `message`, so the warning rides in it.
        raise RollbackSkippedError(f"{exc} ({warning})") from exc
    else:
        # A caller that didn't commit explicitly (so it could merge the
        # warning into its result) still gets a checkpoint here.
        txn.commit()
    finally:
        # `finally`, not `except`: a BaseException (Esc's KeyboardInterrupt)
        # must not leave a finished command reachable from the next handler.
        _DISPATCH.active = previous


def active_transaction() -> Transaction | None:
    """
    Return the transaction wrapping the command running now.

    Returns:
        Transaction | None: The open transaction, or None between commands and
        for commands that bypass the transaction.

    """
    return _DISPATCH.active


def invalidate_active_transaction() -> bool:
    """
    Invalidate the open transaction, if there is one.

    Called from `session.py`'s file handlers on the main thread, inside the
    command that triggered the load or reload. Only the innermost transaction
    is invalidated, so transactions must not nest: an outer one would still
    roll back after a load.

    Returns:
        bool: True when a transaction was open and is now invalidated.

    """
    if _DISPATCH.active is None:
        return False
    _DISPATCH.active.invalidate()
    return True


def library_replace_in_progress() -> bool:
    """
    Report whether a handler is inside `replacing_library_contents`.

    Returns:
        bool: True while a library's contents are being replaced in place.

    """
    return _DISPATCH.library_replace_in_progress


@contextlib.contextmanager
def replacing_library_contents() -> Iterator[None]:
    """
    Mark a `lib.reload()` so `blend_import_post` can tell it from a link or an append.

    Blender fires `blend_import_post` for all three, but only a reload replaces
    existing datablocks with fresh session_uids; a link or append adds new ones
    a failed request must roll back. So the handler invalidates the transaction
    only while this flag is set. Wrap exactly the reload call.

    `reload_library` and `relocate_library` use it but bypass the transaction,
    so it matters only to a command that reloads a library inside one.

    Yields:
        None: The flag is set for the duration of the block and restored after
        it, including when the reload raises.

    """
    previous = _DISPATCH.library_replace_in_progress
    _DISPATCH.library_replace_in_progress = True
    try:
        yield
    finally:
        _DISPATCH.library_replace_in_progress = previous
