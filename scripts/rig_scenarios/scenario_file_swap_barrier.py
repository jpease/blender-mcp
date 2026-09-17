r"""
Check the file-swap barrier and the session epoch against a live Blender.

From the repository root::

    /opt/homebrew/bin/blender --background --factory-startup \
        --python scripts/rig_scenarios/make_fixture.py -- <work>/fixture.blend
    .venv/bin/python scripts/blender_rig.py \
        --work-dir <work>/rig \
        --scenario scripts/rig_scenarios/scenario_file_swap_barrier.py \
        --blender-script scripts/rig_scenarios/in_blender_open_shot_spike.py \
        --blend fixture=<work>/fixture.blend

Needs a GUI Blender: the addon's server refuses to start under `--background`.

This reaches the addon socket only. The MCP tool `get_addon_status` runs in an MCP
process the rig never starts, so this transcript says nothing about it.

Asserted batches go down one connection in one `sendall`, and `handle_client`
queues the frames in the order written. With a socket per command, client threads
enqueue in scheduling order and a drain tick can fall between them, so the
four-connection round asserts only that every command is answered once.

Several rounds are wall-clock bounded, so `quiet_box` prints load per core before
and after; a transcript from a busy machine shows it.

Fails by raising; a clean return is a pass.
"""

import importlib.util
import json
import re
import socket
import time
import unicodedata

from pathlib import Path
from typing import Protocol

# The rig loads this file by path from any working directory, so `scripts/` may not
# be on `sys.path`. `quiet_box.load_quiet_box` cannot load its own module, so the
# path is resolved from this file.
_QUIET_BOX_PATH = Path(__file__).resolve().parents[1] / "quiet_box.py"
_QUIET_BOX_SPEC = importlib.util.spec_from_file_location("quiet_box_for_scenario", _QUIET_BOX_PATH)
if _QUIET_BOX_SPEC is None or _QUIET_BOX_SPEC.loader is None:
    raise SystemExit(f"{_QUIET_BOX_PATH} is not loadable - this scenario was copied out of the repository")
quiet_box = importlib.util.module_from_spec(_QUIET_BOX_SPEC)
_QUIET_BOX_SPEC.loader.exec_module(quiet_box)


class Rig(Protocol):
    """
    The part of `BlenderRig` a scenario may use.

    A Protocol because the rig loads scenarios by path; importing the rig here would
    reverse that dependency.
    """

    work_dir: Path
    blends: dict[str, Path]

    def send(self, command_type: str, params: dict | None = None) -> dict:
        """
        Send one addon command and return its decoded response.

        Args:
            command_type: The addon command name, e.g. "ping".
            params: Command parameters; omitted means none.

        Returns:
            dict: The decoded response, including the echoed request id.

        """
        ...


RECEIPT_FILE_NAME = "rig_ready.json"
SPIKE_READY_FILE_NAME = "open_shot_installed.json"
TIMER_PROBE_FILE_NAME = "timer_raise_probe.json"
BATCH_TIMEOUT_SECONDS = 60.0


def _read_frames(sock: socket.socket, expected: int) -> list[dict]:
    """
    Read exactly `expected` newline-delimited JSON frames.

    Args:
        sock: The connected socket to read from.
        expected: How many frames to wait for.

    Returns:
        list[dict]: The decoded responses, in arrival order.

    Raises:
        AssertionError: If Blender closed the connection before answering every
            command.

    """
    sock.settimeout(BATCH_TIMEOUT_SECONDS)
    buffer = b""
    frames: list[dict] = []
    while len(frames) < expected:
        chunk = sock.recv(8192)
        if not chunk:
            raise AssertionError(f"the connection closed after {len(frames)} of {expected} responses")
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            if line:
                frames.append(json.loads(line))
    return frames


def _echo(requests: list[dict], frames: list[dict]) -> dict[str, dict]:
    """
    Print one line per answered command and key the replies by request id.

    Args:
        requests: The commands that were sent.
        frames: The replies that came back.

    Returns:
        dict[str, dict]: Response keyed by request id.

    """
    by_id = {frame["id"]: frame for frame in frames}
    for request in requests:
        response = by_id.get(request["id"], {})
        print(
            f"    {request['id']:<12} type={request['type']:<12} status={response.get('status')}",
            flush=True,
        )
    return by_id


def _pipelined(port: int, requests: list[dict]) -> dict[str, dict]:
    """
    Write a whole batch down one connection, so it queues in the order written.

    Args:
        port: The addon's socket port.
        requests: The commands to send, in order, each carrying its own `id`.

    Returns:
        dict[str, dict]: Response keyed by request id.

    """
    payload = b"".join(json.dumps(request).encode("utf-8") + b"\n" for request in requests)
    with socket.create_connection(("127.0.0.1", port), timeout=BATCH_TIMEOUT_SECONDS) as sock:
        sock.sendall(payload)
        return _echo(requests, _read_frames(sock, len(requests)))


def _across_connections(port: int, requests: list[dict]) -> dict[str, dict]:
    """
    Send each command on its own socket, every frame written before any reply is read.

    Args:
        port: The addon's socket port.
        requests: The commands to send, each carrying its own `id`.

    Returns:
        dict[str, dict]: Response keyed by request id.

    """
    sockets = [socket.create_connection(("127.0.0.1", port), timeout=BATCH_TIMEOUT_SECONDS) for _ in requests]
    try:
        for sock, request in zip(sockets, requests, strict=True):
            sock.sendall(json.dumps(request).encode("utf-8") + b"\n")
        frames = [_read_frames(sock, 1)[0] for sock in sockets]
        return _echo(requests, frames)
    finally:
        for sock in sockets:
            sock.close()


# A separator of either kind anywhere, or a leading drive letter. Not a bare ":",
# which the barrier's own message contains.
_PATH_SHAPED = re.compile(r"[/\\]|^[A-Za-z]:")
# Not separators to any filesystem, but read as one: `\uff0f` renders like "/"
# and passes `_PATH_SHAPED`.
_SEPARATOR_HOMOGLYPHS = ("\u2044", "\u2215", "\uff0f", "\uff3c", "\uff1a")
# Unicode categories that must not reach a client: controls, format characters
# (bidi overrides, zero-width), line and paragraph separators, and surrogate,
# private-use and unassigned code points. Written out rather than imported from the
# addon, so a gap in its set cannot hide here too. `_NOTE_SHAPE` in
# `tests/test_session_state.py` guards the same leak; widen both together.
_UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})


def _assert_client_safe(label: str, message: str) -> None:
    """
    Assert a client-facing message is one line of safe text naming no path.

    Checking only for a leading "/" would pass Windows and UNC paths, newlines and
    escape codes. Characters are checked by Unicode category because the harmful
    ones are not all ASCII.

    Args:
        label: What the message is, for the failure text.
        message: The message to check.

    """
    assert message, f"{label} is empty"
    unsafe = [
        f"U+{ord(character):04X} ({unicodedata.category(character)})"
        for character in message
        if unicodedata.category(character) in _UNSAFE_CATEGORIES
    ]
    assert not unsafe, f"{label} carries unsafe characters {unsafe}: {message!r}"
    assert len(message.splitlines()) <= 1, f"{label} spans {len(message.splitlines())} lines: {message!r}"
    disguised = [homoglyph for homoglyph in _SEPARATOR_HOMOGLYPHS if homoglyph in message]
    assert not disguised, f"{label} carries a separator homoglyph {disguised!r}: {message!r}"
    leaked = [token for token in message.split() if _PATH_SHAPED.search(token)]
    assert not leaked, f"{label} leaked a path-shaped token {leaked}: {message!r}"


def _swap_batch(label: str, filepath: str) -> list[dict]:
    """
    Build `[ping, open_shot, ping, ping]` with ids that label the transcript.

    Args:
        label: Prefix for the request ids.
        filepath: The .blend the swap should open.

    Returns:
        list[dict]: The four commands, in send order.

    """
    return [
        {"id": f"{label}-A-before", "type": "ping", "params": {}},
        {"id": f"{label}-B-swap", "type": "open_shot", "params": {"filepath": filepath}},
        {"id": f"{label}-C-after", "type": "ping", "params": {}},
        {"id": f"{label}-D-after", "type": "ping", "params": {}},
    ]


def _assert_discarded(responses: dict[str, dict], label: str, epoch: int) -> None:
    """
    Assert both siblings behind the swap were answered with the barrier's error.

    Args:
        responses: The batch's replies.
        label: The batch's id prefix.
        epoch: The addon's current epoch, which the message must name without
            claiming it changed.

    """
    for suffix in ("C-after", "D-after"):
        response = responses[f"{label}-{suffix}"]
        assert response["status"] == "error", f"{label}-{suffix} was executed against the new file: {response}"
        message = response["message"]
        assert "swap" in message.lower(), f"{label}-{suffix} was not told why it was discarded: {message!r}"
        assert str(epoch) in message, f"{label}-{suffix} does not name the current epoch {epoch}: {message!r}"
        assert "changed" not in message.lower(), (
            f"{label}-{suffix} asserts a change the addon cannot know about: {message!r}"
        )
        _assert_client_safe(f"{label}-{suffix}", message)
    print(f"    barrier message: {responses[f'{label}-C-after']['message']}", flush=True)


def run(rig: Rig) -> None:
    """
    Execute the scenario; see the module docstring.

    Args:
        rig: The `BlenderRig` the harness built, already serving.

    """
    # Several rounds below are wall-clock bounded; the stamps show a busy machine.
    print(quiet_box.stamp("before"), flush=True)
    port = json.loads((rig.work_dir / RECEIPT_FILE_NAME).read_text(encoding="utf-8"))["port"]
    spike = json.loads((rig.work_dir / SPIKE_READY_FILE_NAME).read_text(encoding="utf-8"))
    print(f"RIG: spike = {spike}", flush=True)
    assert spike["advertised"], "the open_shot spike is not in the advertised capability set"
    assert spike["in_session_swap_commands"], "open_shot is not in the production _SESSION_SWAP_COMMANDS"
    assert not spike["shadowed_read_only_commands"], "the spike shadowed _READ_ONLY_COMMANDS"

    info = rig.send("get_addon_info")["result"]
    session = rig.send("get_session_info")["result"]
    print(f"RIG: blender_version = {info['blender_version']}", flush=True)
    print(f"RIG: protocol_version = {info['protocol_version']}", flush=True)
    print(f"RIG: get_addon_info session_epoch = {info['session_epoch']}", flush=True)
    print(f"RIG: get_session_info = {json.dumps(session)}", flush=True)
    assert "get_session_info" in info["capabilities"], "get_session_info is not advertised"
    assert info["session_epoch"] == session["session_epoch"], "the two surfaces disagree about the epoch"
    epoch_before = int(info["session_epoch"])

    # --- a successful swap -------------------------------------------------
    print("--- batch r1 (one connection): swapping to fixture.blend", flush=True)
    requests = _swap_batch("r1", str(rig.blends["fixture"]))
    responses = _pipelined(port, requests)
    assert responses["r1-A-before"]["status"] == "success", "the command ahead of the swap was not run"
    swap = responses["r1-B-swap"]
    assert swap["status"] == "success", f"the swap itself failed: {swap}"
    assert swap["result"]["objects"] == ["RigFixtureCube"], f"the swap did not land: {swap['result']}"

    after = rig.send("get_addon_info")["result"]
    assert after["session_epoch"] == epoch_before + 1, (
        f"a completed swap must move the epoch exactly once: {epoch_before} -> {after['session_epoch']}"
    )
    assert Path(str(after["current_filepath"])).name == "fixture.blend", (
        f"get_addon_info does not follow the swapped file: {after['current_filepath']}"
    )
    _assert_discarded(responses, "r1", after["session_epoch"])
    print(f"RIG: epoch after a completed swap = {epoch_before} -> {after['session_epoch']}", flush=True)

    _failed_swap_round(rig, port, after)
    _mid_load_round(rig, port)
    _across_connection_round(rig, port)
    _report_timer_after_raise(rig)

    assert rig.send("ping")["result"]["pong"] is True, "the server stopped answering after the barrier ran"
    print("RIG: barrier held on both paths, server still serving", flush=True)
    print(quiet_box.stamp("after"), flush=True)


def _failed_swap_round(rig: Rig, port: int, before: dict) -> None:
    """
    Fail a swap and assert the epoch does not move while the batch is still discarded.

    Args:
        rig: The rig, for the single commands that need no batching.
        port: The addon's socket port.
        before: `get_addon_info`'s result from before this round.

    """
    print("--- batch r2 (one connection): swapping to a file that does not exist", flush=True)
    requests = _swap_batch("r2", str(rig.work_dir / "does-not-exist.blend"))
    responses = _pipelined(port, requests)
    assert responses["r2-A-before"]["status"] == "success", "the command ahead of the failed swap was not run"
    assert responses["r2-B-swap"]["status"] == "error", "the failed swap was reported as a success"

    failed = rig.send("get_addon_info")["result"]
    assert failed["session_epoch"] == before["session_epoch"], (
        f"a failed swap must not move the epoch: {before['session_epoch']} -> {failed['session_epoch']}"
    )
    assert failed["current_filepath"] == before["current_filepath"], "a failed swap must leave the old file open"
    _assert_discarded(responses, "r2", failed["session_epoch"])
    print(f"RIG: epoch after a failed swap = {failed['session_epoch']} (unmoved)", flush=True)

    diagnosed = rig.send("get_session_info")["result"]
    print(f"RIG: get_session_info after the failure = {json.dumps(diagnosed)}", flush=True)
    assert diagnosed["last_load_error"], "load_post_fail did not record anything"
    assert diagnosed["session_epoch"] == failed["session_epoch"], "get_session_info disagrees about the epoch"
    _assert_client_safe("last_load_error", str(diagnosed["last_load_error"]))


# The spike stalls the main thread before the load, and this side sends into that
# window. The rig's fixture loads in milliseconds, too fast to hit otherwise.
MID_LOAD_STALL_SECONDS = 3.0
MID_LOAD_SEND_AFTER_SECONDS = 1.0


def _mid_load_round(rig: Rig, port: int) -> None:
    """
    Send a command during the load, where the pre-swap queue snapshot cannot see it.

    The swap stalls the main thread inside `open_shot` after the snapshot is taken.
    A second connection then sends a `ping`, stamped with the old epoch; once
    `load_post` moves the epoch, only the stamp can reject it. A second connection
    takes the same path as a second server process, since `handle_client` runs one
    thread per connection.

    Args:
        rig: The rig, for the fixture path and for single commands.
        port: The addon's socket port.

    """
    print("--- batch r4 (two connections): a command sent DURING the load", flush=True)
    epoch_before = int(rig.send("get_addon_info")["result"]["session_epoch"])

    swap = {
        "id": "r4-swap",
        "type": "open_shot",
        "params": {"filepath": str(rig.blends["fixture"]), "probe_delay": MID_LOAD_STALL_SECONDS},
    }
    ping = {"id": "r4-mid-load", "type": "ping", "params": {}}

    with (
        socket.create_connection(("127.0.0.1", port), timeout=BATCH_TIMEOUT_SECONDS) as swap_sock,
        socket.create_connection(("127.0.0.1", port), timeout=BATCH_TIMEOUT_SECONDS) as other_sock,
    ):
        swap_sock.sendall(json.dumps(swap).encode("utf-8") + b"\n")
        # Long enough that the swap has been dequeued and the snapshot taken,
        # short enough that the stall has not finished.
        time.sleep(MID_LOAD_SEND_AFTER_SECONDS)
        other_sock.sendall(json.dumps(ping).encode("utf-8") + b"\n")
        swap_response = _read_frames(swap_sock, 1)[0]
        mid_load_response = _read_frames(other_sock, 1)[0]

    print(f"    r4-swap      status={swap_response.get('status')}", flush=True)
    print(f"    r4-mid-load  status={mid_load_response.get('status')}", flush=True)
    assert swap_response["status"] == "success", f"the stalled swap itself failed: {swap_response}"

    after = rig.send("get_addon_info")["result"]
    assert after["session_epoch"] == epoch_before + 1, "the stalled swap did not move the epoch"
    assert mid_load_response["status"] == "error", (
        "a command that arrived DURING the load was executed against the new file - "
        f"the pre-swap snapshot cannot see this one: {mid_load_response}"
    )
    assert mid_load_response["session_epoch"] == after["session_epoch"], (
        f"the rejection does not carry the epoch as a field: {mid_load_response}"
    )
    assert mid_load_response["session_id"] == after["session_id"], (
        f"the rejection does not carry the session id as a field: {mid_load_response}"
    )
    _assert_client_safe("r4-mid-load", mid_load_response["message"])
    print(
        f"RIG: mid-load arrival rejected by the enqueue stamp; epoch {epoch_before} -> {after['session_epoch']}, "
        f"frame carries session_epoch={mid_load_response['session_epoch']}",
        flush=True,
    )


def _report_timer_after_raise(rig: Rig) -> None:
    """
    Print whether Blender kept a timer callback that raised.

    Printed, not asserted, because it describes Blender, not this repository. If
    Blender drops such a callback, the drain loop survives an escaping exception only
    through `_replace_this_dying_timer`.

    Args:
        rig: The rig, for its work directory.

    """
    probe = rig.work_dir / TIMER_PROBE_FILE_NAME
    if not probe.exists():
        print("RIG: timer-after-raise probe did not report (no file)", flush=True)
        return
    result = json.loads(probe.read_text(encoding="utf-8"))
    print(f"RIG: timer after it raised once -> {json.dumps(result)}", flush=True)
    print(
        "RIG:   calls_after_raising_once > 1 means Blender KEEPS a timer that raised; "
        "== 1 means it drops it and the drain loop would die with it",
        flush=True,
    )


def _across_connection_round(rig: Rig, port: int) -> None:
    """
    Swap with four separate clients, asserting only what tick boundaries cannot change.

    Every command gets one response whatever order the client threads enqueue in.
    How many the barrier discards depends on how those enqueues fall across ticks,
    so that count is printed, not asserted.

    Args:
        rig: The rig, for the fixture path.
        port: The addon's socket port.

    """
    print("--- batch r3 (four connections): reported, not asserted, except for the response count", flush=True)
    requests = _swap_batch("r3", str(rig.blends["fixture"]))
    responses = _across_connections(port, requests)
    assert len(responses) == len(requests), f"{len(requests) - len(responses)} command(s) were never answered"
    for request in requests:
        assert responses[request["id"]].get("status") in {"success", "error"}, (
            f"{request['id']} came back without a status: {responses[request['id']]}"
        )
    discarded = [key for key, value in responses.items() if value.get("status") == "error"]
    print(f"RIG: four-connection round discarded {len(discarded)} of 4: {sorted(discarded)}", flush=True)
