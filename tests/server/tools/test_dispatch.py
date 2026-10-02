"""
The one dispatch helper every tool module sends its commands through.

Covers what the thirteen per-package copies used to decide separately: which thread the
blocking socket call runs on, and how Blender's two failure kinds reach the client.
"""

import asyncio
import threading

import pytest

from mcp.server.fastmcp.exceptions import ToolError

from blender_mcp.server import connection as connection_module
from blender_mcp.server.connection import (
    BlenderCommandNotSentError,
    BlenderConnection,
    BlenderOperationError,
    BlenderTransportError,
)
from blender_mcp.server.tools import _dispatch, core, mesh, model, nd, viewport


class _Connection:
    """Records the thread each command was sent from and answers with a canned reply."""

    def __init__(self, reply=None, error=None) -> None:
        self.calls = []
        self.threads = []
        self._reply = reply if reply is not None else {}
        self._error = error

    def send_command(self, command, params=None):
        self.calls.append((command, params))
        self.threads.append(threading.current_thread())
        if self._error is not None:
            raise self._error
        return self._reply


class _Socket:
    """
    A socket that fails where the test says: while sending, or after the frame was written.

    `recv` hands out its scripted replies in order; an exception among them is raised instead.
    """

    def __init__(self, *, send_error=None, replies=()) -> None:
        self.sent = []
        self._send_error = send_error
        self._replies = list(replies)

    def sendall(self, data) -> None:
        if self._send_error is not None:
            raise self._send_error
        self.sent.append(data)

    def settimeout(self, _value) -> None:
        pass

    def recv(self, _size):
        reply = self._replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


def _install_socket(monkeypatch, sock):
    """Route dispatch through a real `BlenderConnection` over `sock`, with no handshake gating it."""
    monkeypatch.setattr(connection_module, "_addon_handshake", None)
    monkeypatch.setattr(connection_module, "_session_marker_stale", threading.Event())
    blender = BlenderConnection(host="localhost", port=0)
    blender.sock = sock
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: blender)
    return blender


def _install(monkeypatch, connection):
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)
    return connection


def test_the_blocking_socket_call_never_runs_on_the_event_loop(monkeypatch) -> None:
    """A command run on the loop thread stalls every other MCP request until Blender answers."""
    connection = _install(monkeypatch, _Connection())

    async def main():
        return threading.current_thread(), await _dispatch.call_blender("get_session_info", {})

    loop_thread, _envelope = asyncio.run(main())

    assert len(connection.threads) == 1
    assert connection.threads[0] is not loop_thread


def test_the_reply_comes_back_as_the_shared_envelope(monkeypatch) -> None:
    connection = _install(monkeypatch, _Connection({"name": "Cube", "changed_resources": ["Wood"]}))

    envelope = asyncio.run(_dispatch.call_blender("create_primitive", {"name": "Cube"}, changed_objects=["Cube"]))

    assert connection.calls == [("create_primitive", {"name": "Cube"})]
    assert envelope["ok"] is True
    assert envelope["data"] == {"name": "Cube"}
    assert envelope["changed_objects"] == ["Cube"]
    assert envelope["changed_resources"] == ["Wood"]


def test_an_operation_failure_reaches_the_client_as_blenders_own_message(monkeypatch) -> None:
    """Blender already named the object or argument at fault; a prefix only pushes that further away."""
    _install(monkeypatch, _Connection(error=BlenderOperationError("Object 'Cube' has no cloth modifier")))

    with pytest.raises(ToolError) as failure:
        asyncio.run(_dispatch.call_blender("configure_cloth", {}))

    assert str(failure.value) == "Object 'Cube' has no cloth modifier"


def test_a_command_that_never_left_the_socket_is_worth_one_retry(monkeypatch) -> None:
    """The closing newline is the last byte written, so a failed send never handed Blender a command."""
    sock = _Socket(send_error=BrokenPipeError(32, "Broken pipe"))
    _install_socket(monkeypatch, sock)

    with pytest.raises(ToolError) as failure:
        asyncio.run(_dispatch.call_blender("create_primitive_object", {"primitive_type": "CUBE"}))

    message = str(failure.value)
    assert "Broken pipe" in message
    assert "retry once" in message
    assert sock.sent == []


@pytest.mark.parametrize(
    "reply",
    [
        TimeoutError("timed out"),
        ConnectionResetError(54, "Connection reset by peer"),
        b"",
        b"not json\n",
    ],
    ids=["timeout", "reset", "closed-without-a-byte", "unreadable-reply"],
)
def test_a_command_sent_but_never_answered_is_inspected_before_any_retry(monkeypatch, reply) -> None:
    """
    Once the frame is written Blender may run it, finish it, or still be running it.

    A blind retry of `create_primitive_object` there makes a second cube; of an `INSERT_ONLY`
    keyframe call, a refusal for the keys the first one wrote.
    """
    sock = _Socket(replies=[reply])
    _install_socket(monkeypatch, sock)

    with pytest.raises(ToolError) as failure:
        asyncio.run(_dispatch.call_blender("create_primitive_object", {"primitive_type": "CUBE"}))

    message = str(failure.value)
    assert len(sock.sent) == 1, "the command must have been written for this case to mean anything"
    assert "retry once" not in message
    assert "may have run it" in message
    assert "before retrying" in message


def test_a_failure_the_taxonomy_does_not_claim_is_left_alone(monkeypatch) -> None:
    """A capability rejection is neither: it never reached Blender, and its message is self-contained."""
    _install(monkeypatch, _Connection(error=Exception("'set_action_cycle' is not supported by the installed addon")))

    with pytest.raises(Exception, match="is not supported by the installed addon") as failure:
        asyncio.run(_dispatch.call_blender("set_action_cycle", {}))

    assert not isinstance(failure.value, ToolError)


# One tool per module that used to wrap its dispatch in a prefixed `except Exception`, over
# both dispatch paths: `call_blender`, and `send_blender_command` read before the envelope.
_TOOL_CALLS = {
    "create_primitive_object": lambda: mesh.create_primitive_object(ctx=None, primitive_type="CUBE"),
    "mesh_extrude": lambda: mesh.mesh_extrude(ctx=None, object_name="Cube"),
    "nd_boolean": lambda: nd.nd_boolean(ctx=None, object_name="Cube", cutter_object_name="Cutter"),
    "nd_capture_utils": lambda: nd.nd_capture_utils(ctx=None),
    "sync_data_name": lambda: model.sync_data_name(ctx=None, object_names=["Cube"]),
    "get_mesh_data": lambda: viewport.get_mesh_data(ctx=None, object_name="Cube"),
    "get_viewport_screenshot": lambda: viewport.get_viewport_screenshot(ctx=None),
    "get_integration_status": lambda: core.get_integration_status(ctx=None, provider="nd"),
}


@pytest.mark.parametrize("call", _TOOL_CALLS.values(), ids=_TOOL_CALLS.keys())
def test_a_tool_reports_blenders_refusal_as_blender_worded_it(stub_blender_connection, call) -> None:
    """A tool that re-wraps the refusal buries the object Blender named behind its own label."""
    refusal = "Object 'Cube' is not a mesh"
    stub_blender_connection(error=BlenderOperationError(refusal))

    with pytest.raises(ToolError) as failure:
        asyncio.run(call())

    assert str(failure.value) == refusal


@pytest.mark.parametrize("call", _TOOL_CALLS.values(), ids=_TOOL_CALLS.keys())
def test_a_tool_keeps_the_retry_hint_when_the_command_was_never_sent(stub_blender_connection, call) -> None:
    unsent = BlenderCommandNotSentError("Could not send the command to Blender: [Errno 32] Broken pipe")
    stub_blender_connection(error=unsent)

    with pytest.raises(ToolError) as failure:
        asyncio.run(call())

    assert str(failure.value) == f"{unsent} {_dispatch._RETRY_HINT}"


@pytest.mark.parametrize("call", _TOOL_CALLS.values(), ids=_TOOL_CALLS.keys())
def test_a_tool_warns_against_a_blind_retry_once_the_command_was_sent(stub_blender_connection, call) -> None:
    lost = BlenderTransportError("Connection to Blender lost: [Errno 54] Connection reset by peer")
    stub_blender_connection(error=lost)

    with pytest.raises(ToolError) as failure:
        asyncio.run(call())

    assert str(failure.value) == f"{lost} {_dispatch._OUTCOME_UNKNOWN_HINT}"
