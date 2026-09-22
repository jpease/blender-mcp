r"""
Regression coverage for BlenderConnection's newline-delimited framing.

Mirrors tests/test_socket_unicode.py on the addon side: `receive_full_response`
now reads up to a `\n` terminator instead of retrying json.loads() on a
growing buffer, and any bytes past that terminator are kept in
`self._recv_buffer` instead of being discarded. `send_command_locked` also
now tags each command with an "id" and checks the response echoes it back,
so a desynced stream fails loudly instead of returning the wrong response.
"""

from __future__ import annotations

import json
import threading
import types
import uuid

import pytest

from blender_mcp.addon_manager import EXPECTED_ADDON_PROTOCOL_VERSION, AddonHandshake
from blender_mcp.server import connection
from blender_mcp.server.connection import BlenderConnection, BlenderOperationError


class ScriptedSocket:
    """Fake socket returning pre-scripted recv() chunks, one per call."""

    def __init__(self, chunks) -> None:
        self._chunks = list(chunks)
        self.sent: list[bytes] = []

    def settimeout(self, timeout) -> None:
        pass

    def recv(self, bufsize):
        if self._chunks:
            return self._chunks.pop(0)
        return b""

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)


def test_two_frames_in_one_recv_are_not_glued_together() -> None:
    r"""
    Two newline-terminated responses landing in a single recv() must not be
    concatenated into one - the first call to receive_full_response() should
    return only the first frame, leaving the second buffered for the next
    call instead of failing to parse "response1\nresponse2" as one JSON value.
    """
    first = json.dumps({"status": "success", "result": {"n": 1}}).encode("utf-8") + b"\n"
    second = json.dumps({"status": "success", "result": {"n": 2}}).encode("utf-8") + b"\n"

    conn = BlenderConnection(host="localhost", port=0)
    sock = ScriptedSocket([first + second])

    line1 = conn.receive_full_response(sock)
    assert json.loads(line1) == {"status": "success", "result": {"n": 1}}

    line2 = conn.receive_full_response(sock)
    assert json.loads(line2) == {"status": "success", "result": {"n": 2}}


def test_oversized_response_without_terminator_raises_instead_of_growing_forever() -> None:
    conn = BlenderConnection(host="localhost", port=0)
    conn._MAX_MESSAGE_BYTES = 100  # keep the test fast
    sock = ScriptedSocket([b"x" * 200])

    with pytest.raises(Exception, match="exceeded max size"):
        conn.receive_full_response(sock)


def test_oversized_terminated_response_raises() -> None:
    r"""
    A single complete (`\n`-terminated) response must be size-checked too.

    The unterminated-buffer check above only bounds "how long can we wait
    without ever seeing a terminator" - it does not stop a response that
    *does* get a `\n` (e.g. because the terminating chunk lands in the same
    recv() call that pushes the buffer past the limit) from being returned
    at any size.
    """
    conn = BlenderConnection(host="localhost", port=0)
    conn._MAX_MESSAGE_BYTES = 100  # keep the test fast
    sock = ScriptedSocket([b"x" * 200 + b"\n"])

    with pytest.raises(Exception, match="exceeded max size"):
        conn.receive_full_response(sock)


def test_response_id_mismatch_raises_and_drops_the_socket(monkeypatch) -> None:
    """
    send_command_locked must not hand back a response meant for another
    request - the lock already prevents this in practice, but a mismatched
    id should fail loudly rather than silently succeed with the wrong data.
    """
    conn = BlenderConnection(host="localhost", port=0)
    conn.sock = ScriptedSocket([])
    monkeypatch.setattr(
        conn,
        "receive_full_response",
        lambda sock: json.dumps({"status": "success", "result": {}, "id": "not-the-request-id"}).encode("utf-8"),
    )

    with pytest.raises(Exception, match="does not match request id"):
        conn.send_command_locked("ping")

    assert conn.sock is None, "a desynced response must invalidate the connection"


# ---------------------------------------------------------------------------
# Session marker: a changed session must invalidate the cached handshake
# ---------------------------------------------------------------------------


def _reset_handshake_state(monkeypatch: pytest.MonkeyPatch, handshake: object) -> None:
    """
    Put connection.py's module globals into a known state for one test.

    Args:
        monkeypatch: Fixture that restores each global afterwards.
        handshake: The `AddonHandshake` the process is to believe it cached.

    """
    monkeypatch.setattr(connection, "_addon_handshake", handshake)
    monkeypatch.setattr(connection, "_addon_handshake_checked", True)
    monkeypatch.setattr(connection, "_session_marker_stale", threading.Event())
    monkeypatch.setitem(connection._OBSERVED_MARKER, "pending", None)
    monkeypatch.setattr(connection, "_refreshing", threading.local())


def _cached(**fields: object) -> AddonHandshake:
    """
    Build a handshake standing in for one this process already ran.

    Args:
        **fields: Overrides for the session fields under test.

    Returns:
        AddonHandshake: The stand-in.

    """
    defaults: dict[str, object] = {
        "capabilities": ["ping", "get_addon_info"],
        "session_id": "proc-a",
        "session_epoch": 1,
    }
    return AddonHandshake(
        up_to_date=True,
        protocol_version=EXPECTED_ADDON_PROTOCOL_VERSION,
        addon_version=[2, 0, 0],
        blender_version="5.2.2",
        source="native",
        **{**defaults, **fields},  # pyright: ignore[reportArgumentType]
    )


def test_an_unchanged_session_marker_does_not_invalidate_the_cached_handshake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    An unchanged session marker keeps the cached handshake.

    Otherwise every tool call would put a second command on the wire.
    """
    _reset_handshake_state(monkeypatch, _cached())

    connection.note_session_marker({"status": "error", "session_id": "proc-a", "session_epoch": 1})

    assert connection._session_marker_stale.is_set() is False


def test_a_moved_epoch_marks_the_cached_handshake_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    A moved epoch marks the cached handshake stale.

    `send_command` gates every command on capabilities cached once per process,
    and those follow the open `.blend`.
    """
    _reset_handshake_state(monkeypatch, _cached())

    connection.note_session_marker({"status": "error", "session_id": "proc-a", "session_epoch": 2})

    assert connection._session_marker_stale.is_set() is True


def test_a_restarted_addon_at_the_same_epoch_still_marks_the_handshake_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A restarted addon back at the same epoch still marks the handshake stale.

    Epoch 1, restart to 0, one swap back to 1: only the session id shows the change.
    """
    _reset_handshake_state(monkeypatch, _cached())

    connection.note_session_marker({"status": "error", "session_id": "proc-b", "session_epoch": 1})

    assert connection._session_marker_stale.is_set() is True


def test_the_marker_is_read_from_a_command_result_as_well_as_the_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The marker is read from `get_session_info`'s result as well as the frame.

    A client whose commands never sat behind a swap sees only the nested shape.
    """
    _reset_handshake_state(monkeypatch, _cached())

    connection.note_session_marker(
        {"status": "success", "result": {"session_id": "proc-a", "session_epoch": 5}},
        "get_session_info",
    )

    assert connection._session_marker_stale.is_set() is True


def test_an_ordinary_commands_result_cannot_trip_a_re_handshake(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    A `session_epoch` key inside an ordinary command's `result` is not a marker.

    Otherwise any handler returning such a field would cost a re-handshake per call.
    """
    _reset_handshake_state(monkeypatch, _cached())

    connection.note_session_marker(
        {"status": "success", "result": {"session_id": "proc-z", "session_epoch": 99}},
        "get_mesh_data",
    )

    assert connection._session_marker_stale.is_set() is False


def test_a_non_conforming_epoch_does_not_re_arm_the_flag_forever(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    A JSON-string epoch compares equal to the same cached number.

    Compared raw, a peer's `"1"` would never equal the cached `1`, and every
    response would re-arm the flag and cost an extra round trip.
    """
    _reset_handshake_state(monkeypatch, _cached())

    connection.note_session_marker({"status": "success", "session_id": "proc-a", "session_epoch": "1"})

    assert connection._session_marker_stale.is_set() is False, "a normalized value must equal its cached twin"


def test_a_response_carrying_no_marker_changes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Most responses carry no session fields, and absence is not a change."""
    _reset_handshake_state(monkeypatch, _cached())

    connection.note_session_marker({"status": "success", "result": {"pong": True}})

    assert connection._session_marker_stale.is_set() is False


class _SessionScriptedSocket:
    """
    A fake addon socket that answers every command with the session it is told to be in.

    Responses are built from each request, not scripted, because the refresh's
    own `get_addon_info` travels the same path as a `ping`.

    Attributes:
        wire: Every command type sent, in order; tests count `get_addon_info` here.
        session_id: The session the addon currently reports.
        session_epoch: The epoch the addon currently reports.

    """

    def __init__(self, session_id: str, session_epoch: int, capabilities: list[str]) -> None:
        """
        Start the fake addon in one session.

        Args:
            session_id: The session id to report.
            session_epoch: The epoch to report.
            capabilities: The capability set `get_addon_info` advertises.

        """
        self.wire: list[str] = []
        self.session_id = session_id
        self.session_epoch = session_epoch
        self._capabilities = capabilities
        self._pending: bytes = b""

    def settimeout(self, timeout: float) -> None:
        """
        Accept a timeout the way a socket does.

        Args:
            timeout: Ignored.

        """

    def sendall(self, data: bytes) -> None:
        """
        Answer one command, echoing its id and stamping the current session.

        Args:
            data: The framed command the connection wrote.

        """
        command = json.loads(data.decode("utf-8"))
        self.wire.append(command["type"])
        body: dict[str, object] = {
            "status": "success",
            "id": command["id"],
            # The real addon stamps the pair on every frame.
            "session_id": self.session_id,
            "session_epoch": self.session_epoch,
        }
        if command["type"] == "get_addon_info":
            body["result"] = {
                "protocol_version": EXPECTED_ADDON_PROTOCOL_VERSION,
                "addon_version": [2, 0, 0],
                "blender_version": "5.2.2",
                "capabilities": self._capabilities,
                "session_id": self.session_id,
                "session_epoch": self.session_epoch,
            }
        else:
            body["result"] = {"pong": True}
        self._pending += json.dumps(body).encode("utf-8") + b"\n"

    def recv(self, _bufsize: int) -> bytes:
        """
        Hand back whatever the last `sendall` queued.

        Args:
            _bufsize: Ignored; a whole frame is always available.

        Returns:
            bytes: The queued response bytes.

        """
        chunk, self._pending = self._pending, b""
        return chunk


def test_one_swap_costs_exactly_one_re_handshake_over_a_real_round_trip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    One observed swap puts exactly one extra `get_addon_info` on the wire.

    The refresh's own response reaches `note_session_marker` while the old
    handshake is still cached; without the re-entrancy guard it re-arms the flag.
    The round trip is real because a stubbed `force_addon_handshake` cannot re-enter.
    """
    _reset_handshake_state(monkeypatch, _cached())
    sock = _SessionScriptedSocket("proc-a", 2, ["ping", "get_addon_info"])
    blender = BlenderConnection(host="localhost", port=0)
    blender.sock = sock
    monkeypatch.setattr(connection, "get_blender_connection", lambda: blender)

    # Without the guard, the second handshake goes out on the third command.
    for _call in range(3):
        blender.send_command("ping")

    handshakes = sock.wire.count("get_addon_info")
    assert handshakes == 1, f"one swap cost {handshakes} handshakes: {sock.wire}"
    assert connection._session_marker_stale.is_set() is False, "the refresh's own response re-armed the flag"


# Long enough to tell a one-off retry from one repeated on every command.
_COMMANDS_AFTER_A_DOUBLE_MOVE = 20


def test_a_refresh_that_learns_a_newer_session_than_the_one_observed_stops_retrying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A refresh that finds the addon past the observed session records the newer pair.

    Requiring the observed pair instead would re-arm the flag on every command once
    the addon had moved twice, as after two File > Opens.
    """
    _reset_handshake_state(monkeypatch, _cached(session_id="proc-a", session_epoch=1))
    # The addon is already at epoch 3, past the observed epoch 2.
    sock = _SessionScriptedSocket("proc-a", 3, ["ping", "get_addon_info"])
    blender = BlenderConnection(host="localhost", port=0)
    blender.sock = sock
    monkeypatch.setattr(connection, "get_blender_connection", lambda: blender)

    connection.note_session_marker({"status": "error", "session_id": "proc-a", "session_epoch": 2})
    assert connection._session_marker_stale.is_set() is True

    for _call in range(_COMMANDS_AFTER_A_DOUBLE_MOVE):
        blender.send_command("ping")

    handshakes = sock.wire.count("get_addon_info")
    assert handshakes == 1, (
        f"{handshakes} handshakes for {_COMMANDS_AFTER_A_DOUBLE_MOVE} commands - "
        f"the staleness flag re-armed permanently: {sock.wire}"
    )
    assert connection._session_marker_stale.is_set() is False, (
        "a refresh that reported a well-formed - and newer - session must clear the signal"
    )
    assert connection._OBSERVED_MARKER["pending"] == ("proc-a", 3), (
        f"the pair the refresh learned was discarded: {connection._OBSERVED_MARKER['pending']}"
    )


def test_a_refresh_whose_round_trip_dies_leaves_the_staleness_signal_standing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A re-handshake that never completes leaves the staleness signal set.

    Rewritten from a stubbed `source="error"` handshake: a transport failure no longer
    produces a handshake at all, it raises, and the refresh must absorb that without
    failing the command it is pre-flight for. The flag is cleared before the refresh, so
    leaving it cleared would gate later commands on the capabilities of a file no longer
    open.
    """
    _reset_handshake_state(monkeypatch, _cached())

    def _never_answered(_blender: BlenderConnection) -> AddonHandshake:
        raise connection.BlenderPeerClosedError("Connection closed before receiving any data")

    monkeypatch.setattr(connection, "force_addon_handshake", _never_answered)
    blender = BlenderConnection(host="localhost", port=0)

    connection.note_session_marker({"status": "error", "session_id": "proc-b", "session_epoch": 1})
    assert connection._session_marker_stale.is_set() is True

    assert connection.refresh_handshake_if_session_changed(blender) is connection._addon_handshake, (
        "a failed refresh must answer with the handshake still cached, not raise into the command"
    )

    assert connection._session_marker_stale.is_set() is True, (
        "a refresh that never reported a session must leave the signal standing"
    )


def test_a_refresh_that_reports_no_usable_session_leaves_the_staleness_signal_standing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A re-handshake that completes but names half a session pair leaves the signal set.

    The other failure path is a dead round trip, which raises and is caught. This one
    succeeds: the addon answers, and the answer carries no session id or no epoch - which is
    what an addon too old to publish the pair, or one mid-swap, returns. Recording that as the
    session now in force would gate every later command on the capabilities of a file that may
    no longer be open, and nothing would ever ask again, because the signal is cleared before
    the refresh runs.
    """
    _reset_handshake_state(monkeypatch, _cached())
    monkeypatch.setattr(connection, "force_addon_handshake", lambda _blender: _cached(session_epoch=None))
    blender = BlenderConnection(host="localhost", port=0)

    connection.note_session_marker({"status": "error", "session_id": "proc-b", "session_epoch": 1})
    assert connection._session_marker_stale.is_set() is True

    connection.refresh_handshake_if_session_changed(blender)

    assert connection._session_marker_stale.is_set() is True, (
        "a refresh that learned only half a session pair must leave the signal standing"
    )
    assert connection._OBSERVED_MARKER["pending"] == ("proc-b", 1), (
        "half a pair must not be recorded as the session now in force"
    )


def test_the_command_gate_reads_the_refreshed_capability_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    After a refresh, a command the new file supports is no longer refused.

    Capabilities follow the open `.blend`, so the gate must read the refreshed set.
    """
    _reset_handshake_state(monkeypatch, _cached(capabilities=["ping"]))
    connection._session_marker_stale.set()
    monkeypatch.setattr(
        connection,
        "force_addon_handshake",
        lambda _blender: (
            monkeypatch.setattr(
                connection, "_addon_handshake", _cached(capabilities=["ping", "import_polyhaven_asset"])
            )
            or connection._addon_handshake
        ),
    )
    conn = BlenderConnection(host="localhost", port=0)
    conn.sock = ScriptedSocket([json.dumps({"id": "x", "status": "success", "result": {}}).encode() + b"\n"])
    conn.send_command_locked = lambda command_type, _params=None: {"ok": command_type}  # pyright: ignore[reportAttributeAccessIssue]

    assert conn.send_command("import_polyhaven_asset") == {"ok": "import_polyhaven_asset"}


def test_the_command_gate_refuses_a_parameter_the_addon_predates(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    A command name the addon supports can still refuse one of its parameters.

    save_shot existed before write_provenance was added to it; the addon advertising the
    command name was not enough to catch that drift, and the caller found out only from a
    raw TypeError deep in the addon's own handler.
    """
    _reset_handshake_state(
        monkeypatch,
        _cached(
            capabilities=["set_object_transform"],
            capability_params={"set_object_transform": ["object_name", "space"]},
        ),
    )
    conn = BlenderConnection(host="localhost", port=0)
    calls: list[str] = []
    conn.send_command_locked = lambda command_type, _params=None: calls.append(command_type)  # pyright: ignore[reportAttributeAccessIssue]

    with pytest.raises(Exception, match="patch"):
        conn.send_command("set_object_transform", {"object_name": "Cube", "patch": {"location": [0, 0, 0]}})

    assert calls == [], "the gate must refuse before the round trip, not after"


def test_the_command_gate_does_not_filter_when_the_addon_omits_capability_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An addon built before capability_params existed must not be gated as though it accepts nothing."""
    _reset_handshake_state(monkeypatch, _cached(capabilities=["set_object_transform"]))
    conn = BlenderConnection(host="localhost", port=0)
    calls: list[str] = []
    conn.send_command_locked = lambda command_type, params=None: calls.append(command_type) or {"ok": True}  # pyright: ignore[reportAttributeAccessIssue]

    conn.send_command("set_object_transform", {"object_name": "Cube", "patch": {}, "space": "WORLD"})

    assert calls == ["set_object_transform"]


def test_the_command_gate_never_filters_a_command_marked_as_accepting_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_handshake_state(
        monkeypatch,
        _cached(capabilities=["run_geometry_nodes_tool"], capability_params={"run_geometry_nodes_tool": "*"}),
    )
    conn = BlenderConnection(host="localhost", port=0)
    calls: list[str] = []
    conn.send_command_locked = lambda command_type, params=None: calls.append(command_type) or {"ok": True}  # pyright: ignore[reportAttributeAccessIssue]

    conn.send_command("run_geometry_nodes_tool", {"anything": 1, "goes": 2})

    assert calls == ["run_geometry_nodes_tool"]


def test_a_barrier_rejection_read_off_the_socket_marks_the_handshake_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A barrier rejection read off the socket marks the handshake stale.

    The rejection is an error frame, so `send_command_locked` must note the marker
    before its error branch raises.
    """
    _reset_handshake_state(monkeypatch, _cached())
    frame = {
        "id": "cmd-1",
        "status": "error",
        "message": "Discarded without running: a session file swap was attempted ...",
        "session_id": "proc-a",
        "session_epoch": 2,
    }
    conn = BlenderConnection(host="localhost", port=0)
    conn.sock = ScriptedSocket([json.dumps(frame).encode() + b"\n"])  # pyright: ignore[reportAttributeAccessIssue]
    monkeypatch.setattr(uuid, "uuid4", lambda: types.SimpleNamespace(hex="cmd-1"))

    with pytest.raises(BlenderOperationError):
        conn.send_command_locked("ping")

    assert connection._session_marker_stale.is_set() is True, (
        "the barrier's own rejection did not reach note_session_marker"
    )
