"""
Tests that a Blender-side failure never comes back as a successful result.

Many addon handlers catch their own exceptions and return an ad-hoc failure
shape ({"error": ...}, {"succeed": False, "error": ...}, or a bare
"Error: ..." string) instead of raising. The addon's own dispatcher then
wraps that as {"status": "success", "result": <value>}, since only a raised
exception produces {"status": "error", ...}. `ad_hoc_failure_message` and
`decode_response` are the safety net that catches this on the MCP server
side regardless of which addon version is installed; `send_command_locked`
is only the socket shell around them.
"""

from __future__ import annotations

import json
import re

import pytest

from blender_mcp.server.connection import (
    BlenderConnection,
    BlenderOperationError,
    BlenderPeerClosedError,
    BlenderTransportError,
    ad_hoc_failure_message,
    decode_response,
)


class FakeSocket:
    """Minimal stand-in so send_command_locked never touches a real socket."""

    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)

    def settimeout(self, value: float) -> None:
        pass


def _connection_returning(payload: dict, monkeypatch) -> BlenderConnection:
    conn = BlenderConnection(host="localhost", port=0)
    sock = FakeSocket()
    conn.sock = sock

    def fake_receive_full_response(_sock):
        # Echo back whatever id send_command_locked generated for the
        # command it just sent, same as a real addon response would.
        sent_command = json.loads(sock.sent[-1].decode("utf-8"))
        return json.dumps({**payload, "id": sent_command.get("id")}).encode("utf-8")

    monkeypatch.setattr(conn, "receive_full_response", fake_receive_full_response)
    return conn


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({"error": "API key is not given"}, "API key is not given"),
        ({"succeed": False, "error": "No mesh objects imported from GLB"}, "No mesh objects imported from GLB"),
        ({"succeed": False}, "{'succeed': False}"),
        ("Error: Unknown import mode!", "Error: Unknown import mode!"),
    ],
)
def test_ad_hoc_failure_message_detects_known_failure_shapes(result, expected) -> None:
    assert ad_hoc_failure_message(result) == expected


@pytest.mark.parametrize(
    "result",
    [
        {"succeed": True, "name": "Cube", "type": "MESH"},
        {"enabled": True, "message": "Sketchfab integration is enabled and ready to use."},
        {"name": "Scene", "object_count": 3, "objects": []},
        {"pong": True},
        None,
        "job_12345",
        [1, 2, 3],
    ],
)
def test_ad_hoc_failure_message_leaves_real_success_alone(result) -> None:
    assert ad_hoc_failure_message(result) is None


def test_send_command_raises_on_nested_error_dict(monkeypatch) -> None:
    conn = _connection_returning({"status": "success", "result": {"error": "API key is not given"}}, monkeypatch)

    with pytest.raises(Exception, match="API key is not given"):
        conn.send_command_locked("import_sketchfab_model")

    # A clean operation failure is not a transport problem - the socket must survive it.
    assert conn.sock is not None


def test_send_command_raises_on_succeed_false(monkeypatch) -> None:
    conn = _connection_returning(
        {
            "status": "success",
            "result": {"succeed": False, "error": "No mesh objects imported from GLB"},
        },
        monkeypatch,
    )

    with pytest.raises(Exception, match="No mesh objects imported from GLB"):
        conn.send_command_locked("import_polyhaven_asset")

    assert conn.sock is not None


def test_send_command_still_raises_cleanly_on_top_level_error_status(monkeypatch) -> None:
    conn = _connection_returning({"status": "error", "message": "Unknown command type: bogus"}, monkeypatch)

    with pytest.raises(Exception, match="Unknown command type: bogus"):
        conn.send_command_locked("bogus")

    # Regression check: this used to get relabeled "Communication error with
    # Blender: ..." and needlessly drop a working socket.
    assert conn.sock is not None


def test_send_command_passes_through_real_success(monkeypatch) -> None:
    conn = _connection_returning({"status": "success", "result": {"succeed": True, "name": "Cube"}}, monkeypatch)

    assert conn.send_command_locked("import_polyhaven_asset") == {"succeed": True, "name": "Cube"}


def test_decode_response_rejects_a_frame_answering_another_request() -> None:
    """A desynced stream is a transport fault, so it must not arrive as an operation failure."""
    with pytest.raises(Exception, match="does not match request id") as excinfo:
        decode_response({"id": "somebody-elses", "status": "success", "result": {}}, "mine")

    assert not isinstance(excinfo.value, BlenderOperationError)


@pytest.mark.parametrize(
    ("frame", "expected"),
    [
        ({"status": "error", "message": "Unknown command type: bogus"}, "Unknown command type: bogus"),
        ({"status": "error"}, "Unknown error from Blender"),
        ({"status": "success", "result": {"error": "API key is not given"}}, "API key is not given"),
        ({"status": "success", "result": {"succeed": False}}, "{'succeed': False}"),
        # A bare "Error: ..." string is what an unmatched dispatch branch returns; it
        # reaches `result` unwrapped, so only the envelope path proves it is caught.
        ({"status": "success", "result": "Error: Unknown import mode!"}, "Error: Unknown import mode!"),
    ],
)
def test_decode_response_raises_a_blender_operation_error_on_every_failure_shape(frame, expected) -> None:
    with pytest.raises(BlenderOperationError, match=re.escape(expected)):
        decode_response({**frame, "id": "req-1"}, "req-1")


@pytest.mark.parametrize(
    ("frame", "expected"),
    [
        ({"id": "req-1", "status": "success", "result": {"name": "Cube"}}, {"name": "Cube"}),
        # Handlers that answer with nothing still have to yield a mapping: every
        # caller of send_command reads the result with .get().
        ({"id": "req-1", "status": "success"}, {}),
    ],
)
def test_decode_response_unwraps_a_successful_result(frame, expected) -> None:
    assert decode_response(frame, "req-1") == expected


class _ScriptedRecvSocket:
    """
    A socket that answers with pre-scripted recv() chunks and records what was sent.

    Attributes:
        sent: Every frame written to this socket, in order.

    """

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self.sent: list[bytes] = []

    def settimeout(self, value: float) -> None:
        pass

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)

    def recv(self, bufsize: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""


class _EchoSocket(_ScriptedRecvSocket):
    """A socket that answers each command with one success frame carrying its id."""

    def __init__(self, result: dict) -> None:
        super().__init__([])
        self._result = result

    def sendall(self, data: bytes) -> None:
        super().sendall(data)
        request = json.loads(data.decode("utf-8"))
        frame = {"id": request["id"], "status": "success", "result": self._result}
        self._chunks.append(json.dumps(frame).encode("utf-8") + b"\n")


def _with_reconnect(conn: BlenderConnection, replacement, monkeypatch) -> list[object]:
    """
    Make `connect()` hand the connection a new socket, and count the reconnects.

    Args:
        conn: The connection under test.
        replacement: The socket a reconnect installs.
        monkeypatch: Fixture restoring the patched method.

    Returns:
        list[object]: One entry per reconnect, appended as it happens.

    """
    reconnects: list[object] = []

    def fake_connect() -> bool:
        reconnects.append(replacement)
        conn.sock = replacement
        conn._recv_buffer = b""
        return True

    monkeypatch.setattr(conn, "connect", fake_connect)
    return reconnects


def test_a_side_effect_free_command_is_resent_once_on_a_reconnected_socket(monkeypatch) -> None:
    """
    A peer that closed before answering `get_addon_info` costs a reconnect, not a failure.

    Blender retires its socket without warning, so the first command after that writes
    into a dead connection and reads EOF with zero bytes. The live incident was
    `get_addon_status` failing on the first call and succeeding on an identical retry.
    """
    conn = BlenderConnection(host="localhost", port=0)
    dead = _ScriptedRecvSocket([])  # recv() returns b"" immediately: zero bytes, then EOF
    conn.sock = dead
    live = _EchoSocket({"protocol_version": 35})
    reconnects = _with_reconnect(conn, live, monkeypatch)

    assert conn.send_command_locked("get_addon_info") == {"protocol_version": 35}

    assert len(reconnects) == 1, "the dead socket was reused instead of being replaced"
    assert len(dead.sent) == 1 and len(live.sent) == 1, (
        f"the command must cross the wire twice, once per socket: {dead.sent} / {live.sent}"
    )


def test_a_mutating_command_is_never_resent_after_the_peer_closed(monkeypatch) -> None:
    """
    The same zero-byte EOF on a scene-changing command fails instead of being resent.

    Zero bytes back proves Blender said nothing, not that it did nothing: a handler can
    finish and die before its reply reaches the wire. Resending would be a second, silent
    edit to the user's scene. This is the test that fails if the retry set is widened.
    """
    conn = BlenderConnection(host="localhost", port=0)
    dead = _ScriptedRecvSocket([])
    conn.sock = dead
    reconnects = _with_reconnect(conn, _EchoSocket({}), monkeypatch)

    with pytest.raises(BlenderTransportError):
        conn.send_command_locked("set_object_transform", {"object_name": "Cube"})

    assert reconnects == [], "a mutating command must not be resent on a fresh socket"
    assert len(dead.sent) == 1, "the command crossed the wire more than once"
    assert conn.sock is None, "a dead socket must still be invalidated"


def test_a_reply_cut_off_mid_message_is_not_resent_even_for_a_read_only_command(monkeypatch) -> None:
    """
    Bytes that start arriving and then stop mean the command was serviced, so no retry.

    Only the zero-byte case proves nothing happened; a truncated reply is a command
    Blender read, and possibly ran, on a connection that then died.
    """
    conn = BlenderConnection(host="localhost", port=0)
    dead = _ScriptedRecvSocket([b'{"status": "success", "resu'])
    conn.sock = dead
    reconnects = _with_reconnect(conn, _EchoSocket({}), monkeypatch)

    with pytest.raises(BlenderTransportError) as failure:
        conn.send_command_locked("get_addon_info")

    assert not isinstance(failure.value, BlenderPeerClosedError)
    assert reconnects == [], "a half-delivered response must not be retried"
    assert len(dead.sent) == 1
