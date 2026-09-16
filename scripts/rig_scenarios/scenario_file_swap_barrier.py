r"""
Prove the file-swap barrier and the session epoch against a live Blender, for Task 3 Step 7.

Run it with the in-Blender half and a fixture, from the repository root::

    /opt/homebrew/bin/blender --background --factory-startup \
        --python scripts/rig_scenarios/make_fixture.py -- <work>/fixture.blend
    .venv/bin/python scripts/blender_rig.py \
        --work-dir <work>/rig \
        --scenario scripts/rig_scenarios/scenario_file_swap_barrier.py \
        --blender-script scripts/rig_scenarios/in_blender_open_shot_spike.py \
        --blend fixture=<work>/fixture.blend

It needs a GUI Blender: the addon refuses to start under `--background` and
`bpy.app.timers` never fire there, so the drain loop that carries the barrier
never runs.

**Which layer this reaches.** The addon socket only. It shows `get_addon_info`
and `get_session_info` carrying `session_epoch`, and it shows the barrier
answering real sockets. It cannot show the *MCP tool* `get_addon_status`
surfacing the epoch: that lives in `server/tools/core.py`, inside an MCP process
this rig never starts. That half is covered by ordinary `pytest`
(`tests/server/tools/test_core.py`), and reading this transcript as evidence for
it would be a false claim about what was verified.

**How the batch is put into one drain tick, and why not the obvious way.** The
first version of this scenario opened one socket per command and wrote every
frame before reading any reply - the recipe TASK_STATE records from Task 2's
spike. **Measured, it does not hold the ordering this task needs**: on the first
live run the swap landed but both siblings came back `status: success`, because
four independent `handle_client` threads enqueue in whatever order they are
scheduled and the 0.05 s drain timer can fire between two of them. That is a
property of the harness, not of the barrier, and asserting through it would make
this scenario intermittent.

So the asserted batch is written as four frames down **one** connection in a
single `sendall`. `handle_client` splits its receive buffer on the newline
terminator and queues every complete frame from one `recv` in order, so the
batch reaches the queue in the order it was written and does so between drain
ticks. The multi-connection round below is kept, but it asserts only what is
true regardless of tick boundaries - that every command gets exactly one
response. The cross-client ordering claim is evidenced headlessly instead, by
`tests/server/test_threading.py::test_every_command_spanning_a_swap_is_answered_on_both_sockets`,
where the queue can be filled directly and the precondition is guaranteed.

**Every run carries a load stamp.** Several rounds here are wall-clock bounded -
`MID_LOAD_STALL_SECONDS` is a deliberate 3 s stall the send is timed against,
and every frame read has a 60 s ceiling - so `scripts/quiet_box.py` prints
load-per-core before and after, exactly as `scripts/revert_matrix.py` does. A
transcript from a contended box then says so in the transcript instead of being
argued about later.

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

# `blender_rig.py` loads this file through `spec_from_file_location` from an
# arbitrary working directory, so `scripts/` is not reliably on `sys.path` here
# and a plain `import quiet_box` would depend on how the rig itself happened to
# be invoked. It is resolved from this file's own location instead.
#
# **`quiet_box.load_quiet_box` is deliberately not what does this**, and the
# reason is worth a line rather than a puzzled reader: that function *is* the
# general-purpose upward search, but it lives inside the module it would be
# loading, so it cannot be the thing that bootstraps it. A scenario sits one
# directory below `scripts/` and knows it, so the path is direct. Consumers that
# already hold the module - `tests/test_quiet_box.py` is one - use
# `load_quiet_box`, which is where it is covered.
_QUIET_BOX_PATH = Path(__file__).resolve().parents[1] / "quiet_box.py"
_QUIET_BOX_SPEC = importlib.util.spec_from_file_location("quiet_box_for_scenario", _QUIET_BOX_PATH)
if _QUIET_BOX_SPEC is None or _QUIET_BOX_SPEC.loader is None:
    raise SystemExit(f"{_QUIET_BOX_PATH} is not loadable - this scenario was copied out of the repository")
quiet_box = importlib.util.module_from_spec(_QUIET_BOX_SPEC)
_QUIET_BOX_SPEC.loader.exec_module(quiet_box)


class Rig(Protocol):
    """
    The part of `BlenderRig` a scenario is allowed to use.

    Declared as a Protocol rather than imported: `scripts/blender_rig.py` loads a
    scenario by path, so the dependency runs that way round.
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
            command, which is the hang this whole task exists to make impossible.

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
    Write a whole batch down one connection, so it queues in order and in one tick.

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


# A path-shaped token: either separator family anywhere, or a drive letter.
# An unaccompanied ":" is not one - the barrier's own message opens
# "Discarded without running:" - so the drive-letter case is matched
# structurally rather than by the colon alone.
#
# The previous version of this check asked only whether a whitespace-split token
# started with "/" - the identical heuristic `tests/test_session_state.py` used,
# so the repository had two independent checks sharing one blind spot. Every
# hostile input in that file's `_HOSTILE_PATHS` table passed both.
_PATH_SHAPED = re.compile(r"[/\\]|^[A-Za-z]:")
# Not separators to any filesystem; read as one by every human, log viewer and
# LLM downstream of this message. `\uff0f` in place of "/" renders as an
# absolute path while satisfying `_PATH_SHAPED` completely.
_SEPARATOR_HOMOGLYPHS = ("\u2044", "\u2215", "\uff0f", "\uff3c", "\uff1a")
# Every Unicode general category that must not reach a client, restated from the
# threat rather than imported from `session.py`: a guard that borrows the
# implementation's own set agrees with it even when it is wrong. Cc is the C0/C1
# controls and DEL, Cf the bidi overrides and the zero-width set, Zl/Zp the two
# line separators, Cs/Co/Cn the unencodable, font-defined and reserved.
#
# **This is the second of the two guards cycle 1's finding lives in.** The other
# is `_NOTE_SHAPE` in `tests/test_session_state.py`. Both were ASCII-only, so
# cycle 1's "two checks, one blind spot" had *moved* rather than closed: U+2028
# produced a three-line "one-line" note, U+202E reversed a name, and U+FF0F
# rendered as an absolute path, past both of them. They are widened together, in
# one edit, for that reason.
_UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})


def _assert_client_safe(label: str, message: str) -> None:
    """
    Assert a client-facing message is one line of safe text naming no path.

    Asserted as a positive shape rather than as the absence of a leading "/":
    a Windows path, a UNC path, an embedded newline and an ANSI escape all
    satisfy "no token starts with /" while disclosing exactly what the check
    exists to stop. The character check is a Unicode *category* lookup rather
    than a code-point range, because every case that defeated the previous
    ASCII range sits past the end of it.

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
    Build `[ping, open_shot, ping, ping]` with ids a transcript can be read by.

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
        epoch: The epoch the addon is at now; the message must name it and must
            not claim it moved.

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
    # Stamped before and after, like `scripts/revert_matrix.py`: several rounds
    # below are wall-clock bounded (`MID_LOAD_STALL_SECONDS`, the 60 s frame
    # timeouts), and a transcript from a contended box has to say so itself
    # rather than be reconstructed from memory afterwards.
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


# How long the spike stalls Blender's main thread before `open_mainfile` runs,
# and how long this side waits before pushing a command into that window.
# `wm.open_mainfile` was measured at 4.6 s on a 1.05 GB fixture; the rig's
# fixture loads in milliseconds, so the window has to be created deliberately.
MID_LOAD_STALL_SECONDS = 3.0
MID_LOAD_SEND_AFTER_SECONDS = 1.0


def _mid_load_round(rig: Rig, port: int) -> None:
    """
    Push a command into the load itself - the window the pre-swap snapshot cannot see.

    This is the case the enqueue-time stamp exists for, and nothing else in this
    repository can observe it. The swap's own connection stalls Blender's main
    thread inside `open_shot` **before** the operator runs, so the pre-swap
    queue snapshot has already been taken and is empty. A *second* connection
    then sends a `ping`, which is enqueued with the pre-swap epoch stamped on
    it. `load_post` then moves the epoch, and the ping must come back rejected -
    by the stamp, because the snapshot never saw it.

    A second process would behave identically; a second connection is used
    because it is the same code path (`handle_client` runs one thread per
    connection, with no notion of which process opened it) and needs no second
    Blender.

    **On framing.** One `sendall` does not guarantee one `recv`, and nothing
    here assumes it does: `_read_frames` accumulates bytes and splits on the
    newline terminator until the expected number of complete frames has arrived,
    so a frame split across two reads, or two frames in one read, are both
    handled. What the ordering relies on is the *send* order plus the stall,
    not the read granularity.

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
    Report what Blender does with a `bpy.app.timers` callback that raised.

    Reported rather than asserted: this measures Blender's behaviour, not this
    repository's, and the answer decides how severe an escaping exception in
    `drain_command_queue` is. If Blender unregisters the callback, one
    `BaseException` kills the drain loop permanently and **every** client hangs.

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

    Every command gets exactly one response whatever order the client threads
    enqueue in; how many the barrier discards depends on whether the batch
    landed in one tick, so that number is printed rather than asserted. See the
    module docstring.

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
