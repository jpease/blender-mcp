"""
Session-lifecycle state: which .blend is loaded, and when that last changed.

The MCP server caches the addon's advertised `capabilities` and gates every
command on it (`server/connection.py`). That set is **scene-gated** - the
Poly Haven / Sketchfab / ND handlers are added only when
`bpy.context.scene.blendermcp_use_*` says so - and those flags are per-`.blend`
values. So opening a different file can silently change what the addon can do
while a client still believes its cached list. `session_epoch` is the signal
that says "re-read it".

Two readers, on two threads, and the split is deliberate:

- The `bpy.app.handlers` callbacks below run on Blender's **main thread** -
  Blender's own file operators call them - and they are the only writers.
- `session_snapshot()` is read from a **client thread** as well: the drain
  loop's enqueue path (`server_core._decode_and_queue_frame`) stamps the epoch
  onto each command as it arrives, so that a command queued *during* a load can
  be told apart from one queued after it. Reading a plain `int` off module state
  is a single bytecode and needs no lock; **nothing here touches `bpy` off the
  main thread**, and the enqueue path must keep it that way.

`session_id` exists because the counter alone is not monotonic. `_STATE` is
rebuilt at 0 on every fresh import - a Blender restart, or Reload Scripts - so a
client cached at epoch 1 can see 0, watch one swap take it back to 1, compare 1
to 1 and keep a capability set belonging to an entirely different database. The
id is minted once per process and cannot repeat, so `(session_id, session_epoch)`
is the pair a client actually compares.

Every Blender-side fact this module depends on is measured by a committed
instrument rather than a pasted scratchpad - run
`scripts/blender_probes/session_handlers.py` under
`blender --background --factory-startup` to reproduce all of it. Four of its
findings are load-bearing beyond their arity:

- **`load_pre` fires for every `wm.open_mainfile`, succeeding or failing, and
  the old database is still whole when it does.** That is what `load_in_flight`
  below is: one positive signal saying a load was begun and has not yet been
  accounted for. Before it was measured, `server_core._run_session_swap`
  inferred the same condition from three things that had *not* happened, and a
  retry followed by an abort slipped through all three.

- **`wm.read_homefile()` fires `load_post` too**, with an empty path. So
  resetting the session bumps the epoch through the handler below and needs no
  increment of its own; adding one would count a single swap twice.
- **The handler lists are plain Python lists holding the exact function object**
  `@persistent` hands back, so membership testing is exact here. That is *not*
  the bound-method trap documented in `server_core._register_drain_timer`, where
  every attribute access builds a new object and only a held reference can be
  found again.
- **`save_post`'s argument is the file that was written, not the file that is
  open.** After `save_as_mainfile(copy=True)` the two differ, so
  `_on_save_post` reads `bpy.data.filepath` and treats its argument as
  information about *which* save happened, nothing more.
"""

import uuid

from dataclasses import dataclass

import bpy

from bpy.app.handlers import persistent

from .text_hygiene import client_safe_leaf
from .transaction import invalidate_active_transaction, library_replace_in_progress

# Minted once per module import, which is once per Blender process (or per
# Reload Scripts, which is the same thing from a client's point of view: the
# module state it was reasoning about is gone). See the ABA paragraph above.
SESSION_ID = uuid.uuid4().hex


@dataclass
class _SessionState:
    """
    What the file-lifecycle handlers maintain between commands.

    Attributes:
        session_epoch: Increments once per **completed** database swap. It is a
            capability-invalidation signal, not a change log: it moves only when
            the advertised capability set can actually have changed.
        current_filepath: The .blend currently open, or None when the session
            has never been saved.
        last_load_error: Client-safe note about the most recent failed load, or
            None once a load has succeeded.
        last_save_error: Client-safe note about the most recent failed save, or
            None once a save has succeeded.
        load_failures: How many times `load_post_fail` has fired in this
            process. A monotonic counter, never reset, read only as a
            *difference* across a window - "did a load fail while I was not
            looking?". It is deliberately **not** in `session_snapshot()`: it is
            internal bookkeeping, not a field any client was promised, and the
            handshake payload is backward-compatible on purpose. A counter
            rather than the `last_load_error` string because two consecutive
            failed opens of the same path produce a byte-identical note, so a
            string comparison reads the second failure as no failure at all -
            `test_a_second_identical_failure_then_an_abort_is_still_not_indeterminate`
            is that case.
        load_in_flight: True from the moment Blender fires `load_pre` until the
            load it announced is accounted for by `load_post` or
            `load_post_fail`. **The one positive signal in this module**, and it
            is what `server_core._run_session_swap`'s abort guard now rests on
            instead of three heuristics. Measured on 5.2.2 by
            `scripts/blender_probes/session_handlers.py`: `load_pre` fires for
            `wm.open_mainfile` on the succeeding path *and* on every failing one,
            and when it fires `bpy.data.filepath` and the object table are still
            the old file's - so "no `load_pre`, therefore nothing was replaced"
            is Blender's own ordering rather than an assumption.
        session_indeterminate: True once a swap was aborted part-way and no
            completed load has happened since. **A latch, not a note.**
            `INDETERMINATE_SESSION_NOTE` was advisory - it was written into
            `last_load_error` and nothing in `src/` ever read it back, so the
            command after an abort ran `status: success` against a database
            nobody could describe. This flag is read by
            `server_core._drain_batch`, which refuses to run anything but the
            commands that report or repair the condition.

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

    Blender reports an unsaved session as an empty string - including after
    `wm.read_homefile`, measured above. An empty string reads as a real path in
    a client's logs and in a JSON payload; None says "no file" unambiguously.

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

    Blender hands the *absolute* path of the failed file to both failure
    handlers, and this string is returned to the client through
    `get_session_info`. An absolute path reaching a client is a disclosure the
    addon has no business making, so the path is reduced by `client_safe_leaf`
    to a single bounded, control-free file name - or, when no such name can be
    extracted, to a neutral phrase. The operator's own error text - which embeds
    the path again, up to twice in one message - never reaches here at all; it
    goes to the caller of the operator.

    Args:
        action: What was being attempted, e.g. "Loading".
        file_path: The path Blender reported, used only for its final component.

    Returns:
        str: A client-safe one-line explanation: exactly one leaf name, inside a
        fixed sentence, with no separator, control character or traceback.

    """
    return f"{action} {client_safe_leaf(file_path)} failed; the operator's own error text is in Blender's console."


@persistent
def _on_load_pre(_file_path: str = "", _unused: object = None) -> None:
    """
    Record that Blender has begun a load, before it has replaced anything.

    This is the module's only *positive* signal, and it exists because the
    alternative - inferring "a load was attempted" from things that did not
    happen - was wrong in both directions. `server_core._run_session_swap`'s
    abort guard previously asked three questions (was the swap dispatched, did
    the marker move, did the failure counter move) and a retry followed by an
    abort slipped through all three: the first open failed, so the counter had
    already moved, so the abort part-way through the *second*, real load left
    `session_indeterminate` False.

    Measured on 5.2.2 by `scripts/blender_probes/session_handlers.py`, and both
    halves matter:

    - `load_pre` fires for `wm.open_mainfile` on the succeeding path and on
      each failing one the probe drives - `no such file`, a directory, and a
      non-`.blend` carrying a `.blend` extension - as well as for
      `wm.read_homefile`. Those are the same failure modes the plan's own
      "a failed open leaves the database completely untouched" measurement
      covers, so a load that *began* is observable across all of them;
    - when it fires, `bpy.data.filepath` and `bpy.data.objects` are still the
      **old** file's. So an abort that lands before this handler cannot have
      half-replaced a database Blender has not started reading over, and not
      latching there is a statement about Blender's ordering rather than a hope.

    A save cannot be mistaken for a load: the same probe drives
    `wm.save_as_mainfile` with all five of this module's lists attached and
    records `['save_post']` - no load handler fires at all.

    Args:
        _file_path: The .blend Blender is about to read; empty for the startup
            file. Unused - this handler records that a load began, not which
            one; `load_post` is where the path is recorded, once it is true.
        _unused: Blender passes a second positional argument, always None.

    """
    _STATE.load_in_flight = True


@persistent
def _on_load_post(file_path: str = "", _unused: object = None) -> None:
    """
    Record a completed database swap.

    This is the only *handler* that moves the epoch, and the only place it moves
    on the ordinary path. `grep -n "_STATE.session_epoch += " session.py` reports
    three sites in this module and they are deliberately not one: the other two are
    `mark_session_indeterminate` (an abort, where no handler fires at all) and
    `register_handlers` (a swap that happened while the addon was disabled).
    Both are documented where they sit, and an earlier revision of this
    docstring claimed to be the "only" site while those two already existed.

    `wm.open_mainfile`, `wm.read_homefile` and `wm.read_factory_settings` all
    reach here, so Task 6's `reset_session` is covered without a second
    increment.

    A completed load is also the **one** event that clears
    `session_indeterminate`: the database is now wholly one file's, which is the
    only thing that makes the previous abort no longer true.

    It also invalidates the open `mutation_transaction`, if a handler loaded a
    file from inside one: that transaction's snapshot describes the old file,
    and a rollback against the new one would remove it. The swap commands
    themselves bypass the transaction in `server_core._run_handler`, so this
    covers a load made as a side effect of some other command.

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

    A failed `open_mainfile` leaves the old database completely untouched -
    same `filepath`, same datablocks, same scene flags, therefore the same
    `capabilities`. Bumping the epoch here would invalidate every connected
    client's cache and force a re-handshake across every process, on an event
    that changed nothing.

    **This is one of the two places a load stops being in flight**, and that is
    what makes a clean failure observable to `server_core._run_session_swap`'s
    abort guard. The epoch deliberately does not move here, which is correct and
    is the ruling's whole point - but it also means the guard cannot tell "the
    load failed cleanly" from "nothing happened at all" by watching the epoch,
    and a guard that could not tell them apart latched `session_indeterminate`
    on the first, publishing "the open database may be partly replaced" about a
    database Blender had just reported as untouched. Clearing the flag here says
    the load Blender announced in `load_pre` has been accounted for.

    `load_failures` is kept as well, and it is now bookkeeping rather than the
    guard's input: it is a monotonic count of clean failures, exercised by
    `tests/test_session_state.py`, and it remains the only way to tell two
    consecutive identical failures apart (the note is byte-identical).

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

    A save changes no capability: the `blendermcp_use_*` scene flags and the
    handler table are identical either side of it, so `get_addon_info`'s
    `capabilities` payload is unchanged and no client's cache went stale. What a
    save *does* change - the path - is observable through `current_filepath`.

    **The argument is not the answer.** `save_post` reports the file that was
    *written*; `bpy.data.filepath` reports the file that is *open*, and
    `save_as_mainfile(copy=True)` - File -> Save Copy, which a human can do in
    the artist's own Blender at any moment - makes them differ. Measured on
    5.2.2 by `scripts/blender_probes/session_handlers.py`::

        after copy=True  -> save_post arg: SIDECOPY.blend
        after copy=True  -> bpy.data.filepath: real.blend

    Trusting the argument there would permanently publish the name of a file
    nobody has open, through `get_session_info`, `get_addon_info` and
    `get_addon_status` alike. So the open file is read from `bpy.data`, and the
    argument is used only to decide *whose* save succeeded: clearing
    `last_save_error` on a copy would tell a client that the open file's save
    problem had gone away when nothing about it changed.

    Args:
        file_path: The .blend Blender wrote - which may not be the one open.
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

    Measured on 5.2.2 by `scripts/blender_probes/library_replace_handlers.py`:
    `lib.reload()` (and a relocate) fires `blend_import_pre` / `blend_import_post`,
    never `load_post`, and gives every datablock linked from that library a fresh
    session_uid. But `libraries.load()` fires the same handler for a link or an
    append, whose new datablocks a failed request must still roll back. Whether
    the `BlendImportContext` argument distinguishes a reload is unmeasured, so
    this ignores it and acts only inside `transaction.replacing_library_contents`.

    Defence in depth: `reload_library` and `relocate_library` already bypass the
    transaction via `server_core._DATABLOCK_REPLACING_COMMANDS`. `unlink_libraries`
    fires no handler at all, so for it that routing is the only mechanism.
    The epoch does not move: a reload is not a session swap.

    Args:
        _context: Blender's `BlendImportContext`; unused.
        _unused: Blender passes a second positional argument, always None.

    """
    if library_replace_in_progress():
        invalidate_active_transaction()


# A constant, deliberately: it is published as `last_load_error`, where every
# other value is derived from an attacker-choosable path and has to be reduced
# by `client_safe_leaf` first. Nothing the client supplied reaches this sentence,
# so there is nothing here to sanitize and nothing to get wrong.
INDETERMINATE_SESSION_NOTE = (
    "A session file swap was aborted before it finished; the open database may be "
    "partly replaced. Reopen a known shot before relying on this session."
)


def mark_session_indeterminate() -> None:
    """
    Invalidate every stamp taken against a database a load stopped part-way through.

    **This is the one place the epoch moves on a failure, and the plan's ruling
    that it never should is not being read literally here.** That ruling rests on
    a measurement - "a *failed* `wm.open_mainfile` leaves the old database
    completely untouched" - taken across the operator's own failure modes:
    missing file, a directory, a non-`.blend`, a corrupt magic header, an empty
    path. In every one of those Blender fires `load_post_fail`, the database is
    the one it was, and `_on_load_post_fail` above correctly leaves the counter
    alone.

    An **abort** is none of those. When a `BaseException` - `KeyboardInterrupt`
    from Blender's own Esc, `SystemExit`, `GeneratorExit` - escapes a swap
    command's execution after `load_pre` has fired, `load_post` never fires
    **and neither does `load_post_fail`**, so nothing moves the marker and the
    database may be half replaced: a command queued during the load carries a
    stamp that still matches, executes on the next tick against that
    half-replaced database, and only the swap's own client is told anything.
    That is exactly the shape `load_in_flight` names, and it is why the guard
    reads one flag rather than inferring the condition from three others.
    `tests/server/test_threading.py::test_an_aborted_swap_invalidates_the_stamps_taken_during_its_load`
    observes precisely that with this call reverted. Moving the marker here invalidates
    every such stamp, which is the outcome the barrier exists to produce.

    The cost of being wrong is one re-handshake per connected process on an event
    that turns out to have changed nothing - the same cost the ruling rejects for
    `load_post_fail`, accepted here because the alternative is executing against a
    database nobody can describe. Recorded in TASK_STATE as Task 3 decision
    "an aborted swap moves the marker although the plan's ruling says a failure
    never does".

    **The latch is what makes any of this more than advice.** The note alone was
    written into `last_load_error` and read by nothing in `src/`: after an abort
    the next command still ran, answered `status: success` against a
    half-replaced database, and `current_filepath` still named the shot that was
    being replaced - which is the value Task 6's `save_shot` would write over.
    So the flag is set here and `current_filepath` is cleared, because after an
    abort this process cannot truthfully name the file it has open.
    `server_core._drain_batch` enforces the flag; `session_snapshot`,
    `get_session_info` and `get_addon_info` publish it; only a completed
    `load_post` clears it.
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


# One (handler list name, callback) pair per event, so registration,
# deregistration and the idempotence guard cannot drift apart.
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

    The one read accessor, so nothing copies the counter into a stale local.
    `from .session import session_epoch` would bind a value, not a live name;
    this returns today's.

    Called from Blender's main thread (the handlers, `get_session_info`) and
    from a **client** thread (`server_core._decode_and_queue_frame`, stamping an
    arriving command with the epoch it was queued under). Every field is an
    immutable `int` or `str | None` read out of module state, so a reader gets a
    consistent value without a lock; no `bpy` attribute is touched here, which
    is what makes the client-thread call legal.

    Returns:
        dict[str, object]: `session_id`, `session_epoch`, `current_filepath`,
        `last_load_error`, `last_save_error` and `session_indeterminate`. A
        client compares the *pair* `(session_id, session_epoch)`: the counter
        restarts at 0 with the process, so on its own it can repeat a value it
        has already seen. `session_indeterminate` is the one field that is not
        advisory - while it is true the drain loop refuses every command but the
        ones that report or repair the condition.

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

    Only the *difference* across a window is meaningful: the absolute value
    restarts at 0 with the module and is not published to anyone.

    **It is no longer the abort guard's input.** It was, as one of three
    heuristic predicates, and that arrangement had a false negative a critic
    reproduced: a handler that fails one open and is then aborted part-way
    through a second, real load moved this counter on the *first* open, so the
    guard read "a failure was already accounted for" and left
    `session_indeterminate` False while the second load really had been cut in
    half. `load_in_flight` answers that directly. This stays as bookkeeping and
    as the only way to tell two consecutive identical failures apart, since
    `_failure_note` produces a byte-identical string for both.

    A separate accessor rather than a `session_snapshot()` field for the same
    reason `session_is_indeterminate` is one, plus a second: the snapshot is the
    handshake payload, and this is internal bookkeeping no client was promised.

    Returns:
        int: A monotonically increasing count of failed loads.

    """
    return _STATE.load_failures


def load_in_flight() -> bool:
    """
    Report whether Blender announced a load whose outcome is still unaccounted for.

    The single predicate `server_core._run_session_swap`'s abort guard reads.
    True between `load_pre` and whichever of `load_post` / `load_post_fail`
    follows it, and cleared by `mark_session_indeterminate` once an abort has
    been accounted for.

    Read on Blender's main thread only, from the swap's `except BaseException`.
    It is a plain `bool` on module state, so a reader needs no lock, but nothing
    off the main thread has a reason to ask.

    Returns:
        bool: True while a load is part-way through. An abort observed while
        this holds is the one case where the open database may be part of two
        files.

    """
    return _STATE.load_in_flight


def session_is_indeterminate() -> bool:
    """
    Report whether a swap was aborted with no completed load since.

    Read by `server_core._drain_batch` on Blender's main thread. A separate
    accessor rather than a `session_snapshot()` lookup because the drain loop
    asks once per dequeued command and a dict build per command is cost the
    barrier does not need to pay.

    Returns:
        bool: True while the open database may be part of two files.

    """
    return _STATE.session_indeterminate


def register_handlers() -> None:
    """
    Attach the file-lifecycle handlers, exactly once.

    Blender's handler lists accept duplicates without complaint - appending the
    same callback twice really does leave two entries (measured) - and a stacked
    `load_post` would move the epoch twice per swap, so every connected client
    would see a change it cannot account for. Re-enabling the addon without
    disabling it first is the path users actually take, which is why the guard
    lives here rather than in a caller.

    **The re-read below can move the epoch, and that is the point.** Between
    `unregister_handlers()` and this call there is a window with no handler
    coverage at all, and a user who disables the addon, opens a different shot
    and re-enables it walks straight through it. A previous implementation
    overwrote `current_filepath` and left the counter where it was, so the addon
    observed a different database and discarded the observation;
    `tests/test_session_state.py::test_a_swap_while_the_addon_was_disabled_still_moves_the_marker`
    is that case, and it fails when the conditional below is removed
    (revert-matrix row "a swap across a disable/enable cycle is observed and
    discarded"). So the epoch
    moves **when, and only when, the re-read path differs from the recorded
    one**. A bump on every registration was the other candidate and was
    rejected: it would move the counter on every Blender start in every process
    when nothing had changed, and `register()` runs before any client can have
    connected, so there is nothing there to invalidate. The conditional form
    moves it exactly when the observation says the database changed underneath
    the gap. Recorded in TASK_STATE as Task 3 decision "re-registration moves
    the marker only when the observed file differs".
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

    `list.remove` raises `ValueError` when the callback is absent (measured), and
    this runs on paths where `register()` may have half-failed, so absence has to
    be a normal outcome rather than an exception that leaves the addon unloadable.
    """
    for list_name, handler in _HANDLER_BINDINGS:
        handler_list = getattr(bpy.app.handlers, list_name)
        while handler in handler_list:
            handler_list.remove(handler)
