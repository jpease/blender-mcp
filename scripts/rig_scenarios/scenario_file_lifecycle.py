r"""
Drive the real `open_shot`, `save_shot` and `reset_session` over the socket of a live GUI Blender (plan Task 6 Step 6).

Run it with a fixture, from the repository root::

    /opt/homebrew/bin/blender --background --factory-startup \
        --python scripts/rig_scenarios/make_fixture.py -- <work>/fixture.blend
    .venv/bin/python scripts/blender_rig.py \
        --work-dir <work>/rig \
        --scenario scripts/rig_scenarios/scenario_file_lifecycle.py \
        --blend fixture=<work>/fixture.blend

No `--blender-script`: unlike `scenario_file_swap_barrier.py`, nothing is grafted
onto the addon, so every command here is production's own handler. The rig sets
`BLENDERMCP_OUTPUT_ROOTS` to the work dir, so the file roots are **enforced**
for this run (asserted from the handshake).

Rounds, each asserted:

1. The handshake advertises all three commands **and** `get_session_info`.
2. Refusals, each with the epoch unchanged and a client-safe message: a
   `use_scripts` parameter, a path outside the roots, a `//` path in an unsaved
   session, and unsaved work without `discard_unsaved` (the session is made
   dirty by a real `create_primitive` - under `--background` `is_dirty` never
   becomes True, which is why this half needs the GUI).
3. A **queued** `open_shot` pipelined as `[get_session_info, open_shot, ping]`
   down one connection: the swap answers success after the load (TASK_STATE
   decision 11), the epoch moves exactly once, and the `ping` behind it is
   answered by Task 3's barrier rather than run.
4. `save_shot` to a new path (header `BLENDER`, not compressed; `is_dirty` False
   afterwards), refused over that same file without `confirm_overwrite` with
   its bytes unchanged, refused in place without confirmation, then accepted.
5. `[save_shot, set_object_transform]` pipelined in one connection, then
   `open_shot` of the same file without `discard_unsaved`: refused, because the
   edit is still unsaved work.
6. `reset_session` refused without `confirm`, then run: the epoch moves once,
   the scene is empty, and the addon is still serving (`ping`, capabilities).

Fails by raising; a clean return is a pass.
"""

import hashlib
import importlib.util
import json

from pathlib import Path
from typing import Protocol

_BARRIER_PATH = Path(__file__).resolve().parent / "scenario_file_swap_barrier.py"
_BARRIER_SPEC = importlib.util.spec_from_file_location("scenario_file_swap_barrier_shared", _BARRIER_PATH)
if _BARRIER_SPEC is None or _BARRIER_SPEC.loader is None:
    raise SystemExit(f"{_BARRIER_PATH} is not loadable - this scenario was copied out of the repository")
# The pipelining, frame-reading and client-safety helpers are that scenario's,
# reused rather than copied so the two transcripts are judged by one rule.
barrier = importlib.util.module_from_spec(_BARRIER_SPEC)
_BARRIER_SPEC.loader.exec_module(barrier)


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


REQUIRED_COMMANDS = ("open_shot", "save_shot", "reset_session", "get_session_info")
OUTSIDE_ROOTS_BLEND = Path(__file__).resolve().parents[2] / "tests/fixtures/blend/empty_zstd.blend"


def _refused(rig: Rig, label: str, request: tuple[str, dict], epoch: int, expect: str) -> None:
    """
    Assert one command is refused with a client-safe message and the epoch unchanged.

    Args:
        rig: The rig.
        label: What the refusal is, for the transcript.
        request: The command to send and its parameters.
        epoch: The epoch the refusal must leave in place.
        expect: A substring the message must carry, so the refusal is the intended one.

    """
    response = rig.send(*request)
    assert response["status"] == "error", f"{label} was not refused: {response}"
    message = response["message"]
    assert expect in message, f"{label} was refused for the wrong reason: {message!r}"
    barrier._assert_client_safe(label, message)
    assert response["session_epoch"] == epoch, f"{label} moved the epoch: {response}"
    print(f"RIG: refused ({label}): {message}", flush=True)


def _digest(path: Path) -> tuple[str, int]:
    """
    Fingerprint a file's bytes and modification time.

    Args:
        path: The file.

    Returns:
        tuple[str, int]: SHA-256 prefix and `st_mtime_ns`.

    """
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16], path.stat().st_mtime_ns


def _swap_round(rig: Rig, port: int, fixture: Path, epoch: int) -> int:
    """
    Pipeline a queued `open_shot` between two other commands and check the barrier and epoch.

    Args:
        rig: The rig.
        port: The addon's port.
        fixture: The staged `.blend` to open.
        epoch: The epoch before the swap.

    Returns:
        int: The epoch after the swap.

    """
    requests = [
        {"id": "t6-A-before", "type": "get_session_info", "params": {}},
        {"id": "t6-B-swap", "type": "open_shot", "params": {"filepath": str(fixture), "discard_unsaved": True}},
        {"id": "t6-C-after", "type": "ping", "params": {}},
    ]
    responses = barrier._pipelined(port, requests)
    assert responses["t6-A-before"]["status"] == "success", responses["t6-A-before"]
    assert responses["t6-A-before"]["result"]["is_dirty"] is True, "the session was not dirty before the swap"
    swap = responses["t6-B-swap"]
    assert swap["status"] == "success", f"the queued open_shot failed: {swap}"
    result = swap["result"]
    print(f"RIG: open_shot result = {json.dumps(result)}", flush=True)
    assert result["session_epoch"] == epoch + 1, f"the swap must move the epoch exactly once: {epoch} -> {result}"
    assert swap["session_epoch"] == epoch + 1, "the swap's own frame does not carry the moved epoch"
    assert result["rehandshake_required"] is True and result["discarded_unsaved_changes"] is True, result
    assert Path(str(result["filepath"])).name == fixture.name, result
    assert result["object_count"] == 1, result
    behind = responses["t6-C-after"]
    assert behind["status"] == "error" and "swap" in behind["message"].lower(), (
        f"the command queued behind the swap was run against the new file: {behind}"
    )
    barrier._assert_client_safe("barrier rejection", behind["message"])
    print(f"RIG: behind the swap: {behind['message']}", flush=True)
    session = rig.send("get_session_info")["result"]
    assert session["session_epoch"] == epoch + 1 and session["is_dirty"] is False, session
    assert Path(str(session["current_filepath"])).name == fixture.name, session
    return epoch + 1


def _save_round(rig: Rig, epoch: int) -> None:
    """
    Save to a new file, refuse the unconfirmed overwrites, then overwrite in place with confirmation.

    Args:
        rig: The rig.
        epoch: The current epoch, which no save may move.

    """
    target = rig.work_dir / "t6_saved.blend"
    saved = rig.send("save_shot", {"filepath": str(target)})
    assert saved["status"] == "success", saved
    header = target.read_bytes()[:7]
    print(f"RIG: save_shot wrote {target.name}, header={header!r}, result={json.dumps(saved['result'])}", flush=True)
    assert header == b"BLENDER", f"compress=False did not beat use_file_compression: {header!r}"
    assert rig.send("get_session_info")["result"]["is_dirty"] is False, "is_dirty went True after save_shot"
    before = _digest(target)
    _refused(rig, "overwrite without confirm", ("save_shot", {"filepath": str(target)}), epoch, "confirm_overwrite")
    _refused(rig, "in-place save without confirm", ("save_shot", {}), epoch, "confirm_overwrite")
    assert _digest(target) == before, "a refused save changed the file on disk"
    print(f"RIG: bytes+mtime unchanged after both refusals: {before}", flush=True)
    confirmed = rig.send("save_shot", {"confirm_overwrite": True})
    assert confirmed["status"] == "success" and confirmed["result"]["saved_in_place"] is True, confirmed
    assert confirmed["session_epoch"] == epoch, "a save moved the epoch"


def _save_then_edit_round(rig: Rig, port: int, epoch: int) -> None:
    """
    Pipeline a save and an edit into one connection, then check the edit is still protected.

    Blender clears the dirty flag when it processes the save's notifier, after the
    drain tick. An edit run in the same tick as the save was therefore marked dirty
    and then silently un-marked, and a later `open_shot` without `discard_unsaved`
    destroyed it (cycle-1 critic, reproduced live). The drain loop now ends its
    tick after `save_shot`, so the edit runs after the clear.

    Args:
        rig: The rig.
        port: The addon's port.
        epoch: The current epoch.

    """
    requests = [
        {"id": "t6-S-save", "type": "save_shot", "params": {"confirm_overwrite": True}},
        {
            "id": "t6-S-edit",
            "type": "set_object_transform",
            "params": {"object_name": "RigFixtureCube", "patch": {"location": [9, 9, 9]}},
        },
        {"id": "t6-S-poll", "type": "get_session_info", "params": {}},
    ]
    responses = barrier._pipelined(port, requests)
    assert responses["t6-S-save"]["status"] == "success", responses["t6-S-save"]
    assert responses["t6-S-edit"]["status"] == "success", responses["t6-S-edit"]
    later = rig.send("get_session_info")["result"]
    print(f"RIG: after [save, edit] pipelined: later poll is_dirty = {later['is_dirty']}", flush=True)
    target = rig.work_dir / "t6_saved.blend"
    _refused(rig, "edit pipelined behind a save", ("open_shot", {"filepath": str(target)}), epoch, "discard_unsaved")


def _reset_round(rig: Rig, epoch: int) -> None:
    """
    Refuse an unconfirmed reset, run a confirmed one, and prove the addon survived it.

    Args:
        rig: The rig.
        epoch: The epoch before the reset.

    """
    _refused(rig, "reset without confirm", ("reset_session", {}), epoch, "confirm")
    reset = rig.send("reset_session", {"confirm": True})
    assert reset["status"] == "success", reset
    result = reset["result"]
    assert result["session_epoch"] == epoch + 1, f"reset must move the epoch exactly once: {result}"
    assert result["filepath"] is None and result["object_count"] == 0, result
    assert rig.send("ping")["result"]["pong"] is True, "the addon stopped serving after reset_session"
    info = rig.send("get_addon_info")["result"]
    assert all(name in info["capabilities"] for name in REQUIRED_COMMANDS), "reset_session unregistered the addon"
    assert info["session_epoch"] == epoch + 1 and info["current_filepath"] is None, info
    print(f"RIG: reset_session epoch {epoch} -> {info['session_epoch']}, addon still serving", flush=True)


def run(rig: Rig) -> None:
    """
    Execute the scenario; see the module docstring.

    Args:
        rig: The `BlenderRig` the harness built, already serving.

    """
    print(barrier.quiet_box.stamp("before"), flush=True)
    port = json.loads((rig.work_dir / barrier.RECEIPT_FILE_NAME).read_text(encoding="utf-8"))["port"]
    fixture = rig.blends["fixture"]

    info = rig.send("get_addon_info")["result"]
    print(f"RIG: blender_version = {info['blender_version']}, protocol = {info['protocol_version']}", flush=True)
    missing = [name for name in REQUIRED_COMMANDS if name not in info["capabilities"]]
    assert not missing, f"the handshake does not advertise {missing}"
    assert info["file_roots_enforced"] is True, "the rig's roots are not enforced; the outside-roots case means nothing"
    print(f"RIG: advertised {list(REQUIRED_COMMANDS)}; file_roots_enforced = True", flush=True)
    epoch = int(info["session_epoch"])

    _refused(
        rig,
        "use_scripts parameter",
        ("open_shot", {"filepath": str(fixture), "use_scripts": True}),
        epoch,
        "use_scripts",
    )
    _refused(
        rig, "outside the roots", ("open_shot", {"filepath": str(OUTSIDE_ROOTS_BLEND)}), epoch, "BLENDERMCP_FILE_ROOTS"
    )
    _refused(rig, "// in an unsaved session", ("open_shot", {"filepath": "//fixture.blend"}), epoch, "never been saved")
    created = rig.send("create_primitive", {"primitive_type": "CUBE", "name": "T6UnsavedWork"})
    assert created["status"] == "success", created
    assert rig.send("get_session_info")["result"]["is_dirty"] is True, "a GUI edit did not make the session dirty"
    _refused(rig, "unsaved work", ("open_shot", {"filepath": str(fixture)}), epoch, "discard_unsaved")

    epoch = _swap_round(rig, port, fixture, epoch)
    _save_round(rig, epoch)
    _save_then_edit_round(rig, port, epoch)
    _reset_round(rig, epoch)
    print("RIG: file lifecycle passed end to end", flush=True)
    print(barrier.quiet_box.stamp("after"), flush=True)
