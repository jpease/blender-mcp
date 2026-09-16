r"""
Drive the phase-2 gate scenario against a live Blender (plan Task 10; spec Sec.8's phase gate).

Spec Sec.8, verbatim: "Open a shot, link canon, create an override, save, reopen with the link
intact; no hang." This scenario runs that sentence end to end over the **addon
socket only**, per Task 10's ruling (b): the MCP-tool wrapper layer (`open_shot`,
`save_shot`, `link_canon_library`, ... as *server tools*) is a different concern
- a mapping from tool call to socket command - and is proven by ordinary
`pytest` against a stubbed connection. Every command this scenario issues is an
addon command present in `server_core._build_command_handlers()` (`ping` is the
one exception: it is dispatched before that table is consulted, and every prior
rig scenario in this repository sends it the same way).

Run it with the two upfront fixtures, from the repository root::

    /opt/homebrew/bin/blender --background --factory-startup \
        --python scripts/rig_scenarios/make_phase2_gate_fixtures.py -- \
        <work>/canon.blend <work>/shot.blend
    .venv/bin/python scripts/blender_rig.py \
        --work-dir <work>/rig \
        --scenario scripts/rig_scenarios/scenario_phase2_gate.py \
        --blend canon=<work>/canon.blend \
        --blend shot=<work>/shot.blend \
        --blend compressed=tests/fixtures/blend/empty_zstd.blend

The third `.blend` the scenario needs - the save target - is never staged: it is
a path under `rig.work_dir` that `save_shot` itself creates (Step 7).

**Request/response counting (criterion 4) is per-connection first-frame
evidence, not an independent tally.** `BlenderRig._round_trip` opens a fresh
connection per single `rig.send`, so a byte-for-byte transport count has to be
kept by the caller rather than read off the rig - TASK_STATE's "Flagged for
Task 10" note names this as unresolved and leaves the choice to this task.
Chosen here: option 2, amended to something this rig can observe. `_Counted`
wraps every call this scenario makes - both `rig.send` (one connection, one
frame each way) and a pipelined batch (one connection, N frames each way, via
`scenario_file_swap_barrier`'s `_pipelined`) - and increments a request
counter immediately before the call and a response counter immediately after
it returns without raising. This is not two independently-kept counters that
could drift apart: `rig.send` and `_pipelined` each raise before returning if
the frame they read back is missing, malformed, or answers a different request
id than the one just sent, so a response is only ever counted because the
matching request's own reply already passed that check. What the pair proves
is "this connection's first frame back matched what was sent", once per
connection used, not a system-wide delivery guarantee; the multi-process
ordering clause of Sec.07's concurrency dimension is carried by
`tests/server/test_threading.py::test_every_command_spanning_a_swap_is_answered_on_both_sockets`
(Task 3), cited rather than re-built here.

**Step 6's uid discipline, why Step 9 does not repeat it as a name lookup, and
what Step 9 does and does not prove about editability.** After
`create_override`, the scene holds a linked collection and its override under
the *same name* (Task 7 finding 5(d)), so this scenario resolves the
override's object by the `session_uid` `create_override`'s own response
reports, never by name. A `session_uid` is valid only for the
`(session_id, session_epoch)` it was read under (`handlers/file_lifecycle.py`
`_library_summary` docstring) - it does **not** survive the Step 8 reopen. No
addon command re-exposes an object's `is_editable` / `is_system_override` by
uid after a reopen (`set_object_transform` and `get_object_info` both resolve
by bare name, which is exactly the ambiguous lookup Step 6's discipline exists
to avoid), so Step 9 does not attempt to re-read those two fields, and this
live gate carries **no post-reopen evidence of editability at all**. What
Step 9 does instead: resolve the *linked* collection's fresh post-reopen uid
from `list_libraries`' own `datablocks` list (which only ever lists true
library members, never the same-named local override, so this lookup is
unambiguous) and call `create_override` on it again. Route C's
`_refuse_unoverridable` refuses with "already overridden by ..." only while a
real override (`c.override_library.reference`) still points at that
collection, so the refusal is uid-based proof that the override **persisted**
- nothing more. `is_editable=True` / `is_system_override=False` are asserted
once, live, at creation (Step 6). The claim that this pair of fields also
holds *after* a save/reopen is evidenced separately, by
`scripts/blender_probes/linking_handlers_real_blender.py` sections A and B
running the same override, save and reopen against real Blender data
structures directly (no socket), not by this scenario.

**What each of the three `.blend` fixtures is, precisely, for criterion 5a.**
`canon.blend` and `shot.blend` are built by
`scripts/rig_scenarios/make_phase2_gate_fixtures.py`, a separate script that
needs `bpy` and is run once before this scenario - by the pytest wrapper
(`tests/test_phase2_gate.py`) or by hand, per the invocation above - not by an
addon socket command. The `compressed` fixture is not built by anything: it is
`tests/fixtures/blend/empty_zstd.blend`, the real committed binary Task 5's
magic-byte tests already use. Only the third file, the save target
(`_step_save`'s `target`), is created by this scenario itself, and it is
created by issuing `save_shot` - an addon command - never written directly.

Fails by raising; a clean return is a pass.
"""

import importlib.util
import json
import time

from pathlib import Path
from typing import Protocol

_BARRIER_PATH = Path(__file__).resolve().parent / "scenario_file_swap_barrier.py"
_BARRIER_SPEC = importlib.util.spec_from_file_location("scenario_file_swap_barrier_shared_for_gate", _BARRIER_PATH)
if _BARRIER_SPEC is None or _BARRIER_SPEC.loader is None:
    raise SystemExit(f"{_BARRIER_PATH} is not loadable - this scenario was copied out of the repository")
# Reused rather than copied, as scenario_file_lifecycle.py already does: the
# pipelining, frame-reading, load stamping and client-safety helpers are that
# scenario's, so every transcript in this repository is judged by one rule.
barrier = importlib.util.module_from_spec(_BARRIER_SPEC)
_BARRIER_SPEC.loader.exec_module(barrier)

_COMMAND_TIMEOUT_SECONDS = 180.0
# Mirrors server_core.py's `_SESSION_SWAP_COMMANDS`: classification of "is this
# a swap" happens on the command's type, before its outcome is known, which is
# exactly why a *failed* member of this set still discards whatever else was
# queued behind it in the same batch (see `_pipelined_negative`).
_SESSION_SWAP_COMMANDS = frozenset({"open_shot", "reset_session"})


class Rig(Protocol):
    """The part of `BlenderRig` this scenario uses (a Protocol: the rig loads scenarios by path)."""

    work_dir: Path
    blends: dict[str, Path]

    def send(self, command_type: str, params: dict | None = None) -> dict:
        """
        Send one addon command and return its decoded response.

        Args:
            command_type: The addon command name.
            params: Command parameters; omitted means none.

        Returns:
            dict: The decoded response.

        """
        ...


class _Counted:
    """Tallies requests sent and responses received, across both single sends and pipelined batches."""

    def __init__(self, rig: Rig) -> None:
        """
        Wrap a rig with counters.

        Args:
            rig: The rig to send through.

        """
        self._rig = rig
        self.requests = 0
        self.responses = 0

    def send(self, command_type: str, params: dict | None = None) -> dict:
        """
        Send one command, counting the request before and the response after.

        Args:
            command_type: The addon command name.
            params: Command parameters; omitted means none.

        Returns:
            dict: The decoded response.

        """
        self.requests += 1
        response = self._rig.send(command_type, params)
        self.responses += 1
        return response

    def pipelined(self, port: int, requests: list[dict]) -> dict[str, dict]:
        """
        Send a whole batch down one connection, counting every frame each way.

        Args:
            port: The addon's socket port.
            requests: The commands to send, each carrying its own id.

        Returns:
            dict[str, dict]: Response keyed by request id.

        """
        self.requests += len(requests)
        responses = barrier._pipelined(port, requests)
        self.responses += len(responses)
        return responses


def _no_epoch_move(before: int, response: dict, label: str) -> None:
    """
    Assert a response carries the same epoch it started with (Step 4b).

    Args:
        before: The epoch read immediately before the call.
        response: The response, success or error, which always carries `session_epoch`.
        label: What the call was, for the failure text.

    """
    after = response.get("session_epoch")
    assert after == before, f"{label} moved the epoch: {before} -> {after}"


def _step_reset_and_open(counted: _Counted, shot: Path) -> int:
    """
    Start empty, open the shot, and prove the connection still works after it (Steps 2-3).

    Args:
        counted: The counting wrapper around the rig.
        shot: The shot fixture to open.

    Returns:
        int: The epoch after the open.

    """
    reset = counted.send("reset_session", {"confirm": True})
    assert reset["status"] == "success", reset
    epoch = int(reset["result"]["session_epoch"])

    opened = counted.send("open_shot", {"filepath": str(shot)})
    assert opened["status"] == "success", opened
    open_result = opened["result"]
    assert open_result["session_epoch"] == epoch + 1, f"open_shot must move the epoch exactly once: {open_result}"
    epoch += 1
    assert Path(str(open_result["filepath"])).name == shot.name, open_result

    ping = counted.send("ping")
    assert ping["status"] == "success" and ping["result"]["pong"] is True, ping
    print(f"RIG: reset_session + open_shot(shot) -> epoch {epoch}, ping after it still works (no hang)", flush=True)
    return epoch


def _step_handshake(counted: _Counted, epoch: int, shot: Path) -> None:
    """
    Step 4: cross-check `get_addon_info` and `get_session_info`.

    Args:
        counted: The counting wrapper around the rig.
        epoch: The epoch both calls must report.
        shot: The file that must still be the reported one.

    """
    info = counted.send("get_addon_info")["result"]
    session = counted.send("get_session_info")["result"]
    assert info["session_epoch"] == epoch, info
    assert session["session_epoch"] == epoch, session
    assert Path(str(session["current_filepath"])).name == shot.name, session
    assert session["is_dirty"] is False, session
    print(f"RIG: get_addon_info and get_session_info agree: epoch {epoch}", flush=True)


def _step_link(counted: _Counted, epoch: int, canon: Path) -> dict[str, object]:
    """
    Step 5: link the canon collection and confirm it in `list_libraries`.

    Args:
        counted: The counting wrapper around the rig.
        epoch: The epoch, which linking must not move.
        canon: The canon library fixture.

    Returns:
        dict[str, object]: `library_uid`, `library_filepath`, `collection_uid`, `collection_name`.

    """
    linked = counted.send("link_canon_library", {"filepath": str(canon), "collections": ["CanonHero"]})
    assert linked["status"] == "success", linked
    assert linked["session_epoch"] == epoch, "link_canon_library is not a swap and must not move the epoch"
    link_result = linked["result"]
    assert link_result["library"]["is_missing"] is False, link_result["library"]
    library_uid = link_result["library"]["session_uid"]
    library_filepath = link_result["library"]["filepath"]
    assert link_result["collections"], "link_canon_library linked no collection"
    linked_collection = link_result["collections"][0]
    assert linked_collection["name"] == "CanonHero", linked_collection

    listing = counted.send("list_libraries")["result"]
    matches = [lib for lib in listing["libraries"] if lib["session_uid"] == library_uid]
    assert len(matches) == 1 and matches[0]["is_missing"] is False, listing
    print(
        f"RIG: link_canon_library -> library uid={library_uid} filepath={library_filepath!r}, "
        f"collection uid={linked_collection['session_uid']}",
        flush=True,
    )
    return {
        "library_uid": library_uid,
        "library_filepath": library_filepath,
        "collection_uid": linked_collection["session_uid"],
        "collection_name": linked_collection["name"],
    }


def _step_override(counted: _Counted, epoch: int, collection_uid: object) -> None:
    """
    Step 6: override the linked collection (Route C) and check one object by uid.

    Args:
        counted: The counting wrapper around the rig.
        epoch: The epoch, which overriding must not move.
        collection_uid: The linked collection's session_uid, from Step 5.

    """
    override = counted.send("create_override", {"collection_uid": collection_uid})
    assert override["status"] == "success", override
    assert override["session_epoch"] == epoch, "create_override is not a swap and must not move the epoch"
    override_result = override["result"]
    assert override_result["override"]["hierarchy_root_uid"] is not None, override_result["override"]
    assert override_result["override"]["is_system_override"] is False, override_result["override"]
    override_objects = override_result["objects"]
    assert override_objects, "create_override reported no objects inside the override"
    probe_object = override_objects[0]
    assert probe_object["is_editable"] is True, probe_object
    assert probe_object["is_system_override"] is False, probe_object
    print(
        f"RIG: create_override -> hierarchy_root_uid={override_result['override']['hierarchy_root_uid']}, "
        f"object uid={probe_object['session_uid']} is_editable=True is_system_override=False",
        flush=True,
    )


def _step_save(counted: _Counted, epoch: int, target: Path, canon: Path) -> None:
    """
    Step 7: save to a new path, uncompressed and un-remapped, with the epoch unmoved.

    Asserts the **kwargs the call actually reported**, not just the outcome:
    `relative_remap=False` and `compress=False` in `save_shot`'s own result,
    plus a direct on-disk check that the canon library's absolute path was
    written verbatim. The two are not redundant. For an absolute library link,
    `relative_remap=True` still leaves `Library.filepath` absolute (measured)
    and Step 9's `list_libraries` reports the same reduced leaf name either
    way, so a `list_libraries` comparison alone cannot tell a correct call from
    one that silently inherited `relative_remap`'s `True` default - only
    reading the byte written to disk can.

    Args:
        counted: The counting wrapper around the rig.
        epoch: The epoch, which a successful save must not move.
        target: Where to save.
        canon: The canon library fixture, whose absolute path must appear
            verbatim in the saved file.

    """
    saved = counted.send(
        "save_shot",
        {"filepath": str(target), "confirm_overwrite": True, "relative_remap": False},
    )
    assert saved["status"] == "success", saved
    assert saved["session_epoch"] == epoch, f"save_shot moved the epoch: {saved}"
    assert saved["result"]["relative_remap"] is False, saved["result"]
    assert saved["result"]["compress"] is False, saved["result"]
    assert target.is_file(), f"save_shot reported success but wrote nothing to {target.name}"
    data = target.read_bytes()
    header = data[:7]
    assert header == b"BLENDER", f"save_shot wrote a compressed file despite compress=False: {header!r}"
    assert str(canon).encode("utf-8") in data, (
        "the canon library's absolute path was not written verbatim - relative_remap may not actually be False"
    )
    print(
        f"RIG: save_shot -> {target.name}, header={header!r}, relative_remap=False, compress=False "
        f"(canon's absolute path verified present on disk), epoch unchanged at {epoch}",
        flush=True,
    )


def _step_reopen(counted: _Counted, epoch_before_save: int, target: Path) -> int:
    """
    Step 8: reopen the saved file.

    Args:
        counted: The counting wrapper around the rig.
        epoch_before_save: The epoch from before Step 7's save.
        target: The saved file to reopen.

    Returns:
        int: The epoch after the reopen.

    """
    reopened = counted.send("open_shot", {"filepath": str(target)})
    assert reopened["status"] == "success", reopened
    reopen_result = reopened["result"]
    assert reopen_result["session_epoch"] == epoch_before_save + 1, (
        f"reopening the saved file must move the epoch exactly once: {reopen_result}"
    )
    epoch = reopen_result["session_epoch"]
    print(f"RIG: open_shot(saved) -> epoch {epoch}", flush=True)
    return epoch


def _step_assert_link_survived(counted: _Counted, epoch: int, link: dict[str, object]) -> None:
    """
    Step 9: the link and the override must both have survived the round trip.

    Args:
        counted: The counting wrapper around the rig.
        epoch: The epoch, which this read-only probe must not move.
        link: The dict `_step_link` returned.

    """
    listing = counted.send("list_libraries")["result"]
    # Filtering by the exact filepath recorded at Step 5 *is* the byte-identical
    # comparison criterion 2 asks for; a further `==` on the same field would
    # only restate what the filter already required to reach this line.
    matches = [lib for lib in listing["libraries"] if lib["filepath"] == link["library_filepath"]]
    assert len(matches) == 1, f"expected exactly one library with filepath {link['library_filepath']!r}: {matches}"
    reopened_library = matches[0]
    assert reopened_library["is_missing"] is False, reopened_library

    candidates = [entry for entry in reopened_library.get("datablocks", []) if entry["name"] == link["collection_name"]]
    assert len(candidates) == 1, reopened_library.get("datablocks")
    reopened_collection_uid = candidates[0]["session_uid"]
    probe = counted.send("create_override", {"collection_uid": reopened_collection_uid})
    assert probe["status"] == "error", f"the override must still exist if it survived the round trip: {probe}"
    message = probe["message"]
    assert "already overridden" in message, message
    barrier._assert_client_safe("override-persistence probe", message)
    assert probe["session_epoch"] == epoch, "the persistence probe must not move the epoch"
    print(
        f"RIG: list_libraries after reopen -> filepath byte-identical ({reopened_library['filepath']!r}), "
        f"is_missing=False; override persistence confirmed: {message!r}",
        flush=True,
    )


def _pipelined_negative(
    counted: _Counted, port: int, request_id: str, command_type: str, params: dict
) -> tuple[dict, dict]:
    """
    Pipeline one command expected to fail behind a `ping`, on a single connection.

    Proves criterion 3's "a connection that still works afterwards" directly:
    the `ping` is read back on the *same socket* the bad command was refused
    on, not on a fresh one `rig.send` would open.

    Whether the trailing `ping` is answered *normally* depends on `command_type`.
    `server_core._run_session_swap`'s own docstring: "**A refused swap discards
    the queue too.** ... an `open_shot` that its own validation refuses (before
    `load_pre`, nothing loaded) still answers every command queued behind it
    ... with a rejection" (recorded there as a Task 6 backlog item, not a bug).
    `open_shot` and `reset_session` are `_SESSION_SWAP_COMMANDS`; classification
    happens on the command's *type*, before its outcome is known, so a failed
    `open_shot` discards the ping exactly as a successful one would. `save_shot`
    is not a member, so a failed `save_shot` answers a queued ping normally.
    Either way the ping gets back a real frame - a barrier discard or a pong -
    which is what proves this connection did not hang or drop. Which shape to
    check for is derived from `command_type` against `_SESSION_SWAP_COMMANDS`
    above, not passed in, so it cannot say one thing while the request sent
    says another.

    Args:
        counted: The counting wrapper around the rig.
        port: The addon's socket port.
        request_id: A unique id prefix for this pair.
        command_type: The command expected to fail.
        params: Its parameters.

    Returns:
        tuple[dict, dict]: The bad command's response, then the ping's.

    """
    requests = [
        {"id": f"{request_id}-bad", "type": command_type, "params": params},
        {"id": f"{request_id}-ping", "type": "ping", "params": {}},
    ]
    responses = counted.pipelined(port, requests)
    ping = responses[f"{request_id}-ping"]
    if command_type in _SESSION_SWAP_COMMANDS:
        assert ping["status"] == "error", f"expected the ping behind {command_type!r} to be barrier-discarded: {ping}"
        assert "swap" in ping["message"].lower(), f"the discard did not say why: {ping['message']!r}"
        barrier._assert_client_safe(f"{request_id} barrier discard", ping["message"])
    else:
        assert ping["status"] == "success" and ping["result"]["pong"] is True, (
            f"the connection did not still work after {command_type!r} was refused: {ping}"
        )
    return responses[f"{request_id}-bad"], ping


def _negative_missing_file(counted: _Counted, port: int, epoch: int, work_dir: Path) -> None:
    """
    Negative case: a file that does not exist.

    Args:
        counted: The counting wrapper around the rig.
        port: The addon's socket port.
        epoch: The epoch, which a refusal must not move.
        work_dir: The rig's work dir, so the path is at least inside the configured roots.

    """
    missing = work_dir / "phase2_gate_does_not_exist.blend"
    response, _ping = _pipelined_negative(counted, port, "neg-missing", "open_shot", {"filepath": str(missing)})
    assert response["status"] == "error", response
    assert "does not exist" in response["message"], response["message"]
    barrier._assert_client_safe("missing file", response["message"])
    _no_epoch_move(epoch, response, "open_shot on a missing file")
    print(f"RIG: negative/missing-file refused: {response['message']!r}; connection still works", flush=True)


def _negative_outside_roots(counted: _Counted, port: int, epoch: int) -> None:
    """
    Negative case: a real, valid `.blend` outside the configured roots.

    Args:
        counted: The counting wrapper around the rig.
        port: The addon's socket port.
        epoch: The epoch, which a refusal must not move.

    """
    outside_roots = Path(__file__).resolve().parents[2] / "tests/fixtures/blend/empty_zstd.blend"
    response, _ping = _pipelined_negative(
        counted, port, "neg-outside-roots", "open_shot", {"filepath": str(outside_roots)}
    )
    assert response["status"] == "error", response
    assert "BLENDERMCP_FILE_ROOTS" in response["message"], response["message"]
    barrier._assert_client_safe("outside the configured roots", response["message"])
    _no_epoch_move(epoch, response, "open_shot outside the configured roots")
    print(f"RIG: negative/outside-roots refused: {response['message']!r}; connection still works", flush=True)


def _negative_unconfirmed_overwrite(counted: _Counted, port: int, epoch: int, target: Path) -> None:
    """
    Negative case: saving over an existing file without `confirm_overwrite`.

    `save_shot` is not a `_SESSION_SWAP_COMMANDS` member, so unlike the two
    cases above, the trailing ping here is expected to run normally.

    Args:
        counted: The counting wrapper around the rig.
        port: The addon's socket port.
        epoch: The epoch, which a refusal must not move.
        target: The already-saved file from Step 7, whose bytes must survive untouched.

    """
    before = target.read_bytes()
    response, _ping = _pipelined_negative(counted, port, "neg-unconfirmed", "save_shot", {"filepath": str(target)})
    assert response["status"] == "error", response
    assert "confirm_overwrite" in response["message"], response["message"]
    barrier._assert_client_safe("save without confirm_overwrite", response["message"])
    _no_epoch_move(epoch, response, "save_shot without confirm_overwrite")
    assert target.read_bytes() == before, "a refused save changed the target file's bytes on disk"
    print(
        f"RIG: negative/unconfirmed-overwrite refused: {response['message']!r}; "
        "target bytes unchanged; connection still works",
        flush=True,
    )


def _negative_compressed_open_succeeds(counted: _Counted, epoch: int, compressed: Path) -> int:
    """
    Negative-in-name-only case: a compressed `.blend` must be *accepted* by `open_shot`.

    Args:
        counted: The counting wrapper around the rig.
        epoch: The epoch before the open.
        compressed: A real zstd-compressed `.blend` fixture.

    Returns:
        int: The epoch after the (successful) open.

    """
    response = counted.send("open_shot", {"filepath": str(compressed), "discard_unsaved": True})
    assert response["status"] == "success", f"a compressed .blend must pass open_shot's magic-byte check: {response}"
    new_epoch = response["result"]["session_epoch"]
    assert new_epoch == epoch + 1, "opening the compressed fixture must move the epoch exactly once"
    print(f"RIG: negative/compressed-.blend open_shot SUCCEEDED as required -> epoch {new_epoch}", flush=True)
    return new_epoch


def _negative_queued_behind_open_shot(counted: _Counted, port: int, epoch: int, shot: Path) -> int:
    """
    Negative case: a command queued behind an `open_shot` must be cleanly discarded, not run.

    Args:
        counted: The counting wrapper around the rig.
        port: The addon's socket port.
        epoch: The epoch before the pipelined swap.
        shot: A file to swap to, so this case does not depend on Step 3's shot still being current.

    Returns:
        int: The epoch after the swap.

    """
    requests = [
        {"id": "gate-swap", "type": "open_shot", "params": {"filepath": str(shot), "discard_unsaved": True}},
        {"id": "gate-behind", "type": "ping", "params": {}},
    ]
    responses = counted.pipelined(port, requests)
    swap = responses["gate-swap"]
    assert swap["status"] == "success", swap
    new_epoch = swap["result"]["session_epoch"]
    assert new_epoch == epoch + 1, f"the queued open_shot must move the epoch exactly once: {swap}"
    behind = responses["gate-behind"]
    assert behind["status"] == "error", f"the command behind open_shot must be discarded, not run: {behind}"
    behind_message = behind["message"]
    assert "swap" in behind_message.lower(), f"the discard did not say why: {behind_message!r}"
    assert str(new_epoch) in behind_message, (
        f"the discard does not name the current epoch {new_epoch}: {behind_message!r}"
    )
    assert "changed" not in behind_message.lower(), (
        f"the discard asserts a change the addon cannot know about: {behind_message!r}"
    )
    barrier._assert_client_safe("command queued behind open_shot", behind_message)
    print(
        f"RIG: negative/queued-behind-open_shot -> swap succeeded (epoch {new_epoch}), "
        f"behind command cleanly discarded: {behind_message!r}",
        flush=True,
    )
    still_alive = counted.send("ping")
    assert still_alive["status"] == "success" and still_alive["result"]["pong"] is True, (
        "the connection must still work after the queued-behind-open_shot case (no hang, no dropped socket)"
    )
    return new_epoch


def run(rig: Rig) -> None:
    """
    Execute the scenario; see the module docstring.

    Args:
        rig: The `BlenderRig` the harness built, already serving.

    """
    print(barrier.quiet_box.stamp("before"), flush=True)
    port = json.loads((rig.work_dir / barrier.RECEIPT_FILE_NAME).read_text(encoding="utf-8"))["port"]
    canon = rig.blends["canon"]
    shot = rig.blends["shot"]
    counted = _Counted(rig)
    wall_start = time.monotonic()

    epoch = _step_reset_and_open(counted, shot)
    _step_handshake(counted, epoch, shot)
    link = _step_link(counted, epoch, canon)
    _step_override(counted, epoch, link["collection_uid"])
    target = rig.work_dir / "phase2_gate_saved.blend"
    _step_save(counted, epoch, target, canon)
    epoch = _step_reopen(counted, epoch, target)
    _step_assert_link_survived(counted, epoch, link)
    print("RIG: phase-2 gate scenario (steps 1-10) passed", flush=True)

    # Step 4 / 4b: negative cases, run after the gate itself so none of them
    # can perturb the sequence above.
    _negative_missing_file(counted, port, epoch, rig.work_dir)
    _negative_outside_roots(counted, port, epoch)
    _negative_unconfirmed_overwrite(counted, port, epoch, target)
    epoch = _negative_compressed_open_succeeds(counted, epoch, rig.blends["compressed"])
    epoch = _negative_queued_behind_open_shot(counted, port, epoch, shot)

    wall_seconds = time.monotonic() - wall_start
    print(
        f"RIG: totals -> requests={counted.requests} responses={counted.responses} "
        f"wall_clock_seconds={wall_seconds:.2f}",
        flush=True,
    )
    assert counted.requests == counted.responses, (
        f"every request must receive exactly one response (single-client, per-connection evidence; "
        f"the multi-process clause is carried by test_threading.py): "
        f"requests={counted.requests} responses={counted.responses}"
    )
    assert wall_seconds < _COMMAND_TIMEOUT_SECONDS, (
        f"the whole scenario took {wall_seconds:.2f}s, which is not far below the client's "
        f"{_COMMAND_TIMEOUT_SECONDS:g}s single-command timeout"
    )
    print("RIG: phase-2 gate scenario passed end to end, negative cases included", flush=True)
    print(barrier.quiet_box.stamp("after"), flush=True)
