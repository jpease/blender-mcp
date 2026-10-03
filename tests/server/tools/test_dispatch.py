"""
The one dispatch helper every tool module sends its commands through.

Covers what the thirteen per-package copies used to decide separately: which thread the
blocking socket call runs on, and how Blender's two failure kinds reach the client.
"""

import asyncio
import json
import threading

from contextlib import suppress

import pytest

from mcp.server.fastmcp.exceptions import ToolError

from blender_mcp.addon_manager import AddonHandshake
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


# --- an identical call after an unknown outcome resends under the first call's id ---


class _AnsweringSocket:
    """
    A socket whose replies answer whatever was last written, so the test need not know the id.

    Each scripted reply is consumed by one `recv`: an exception is raised, and a dict becomes a
    frame echoing the id of the last frame written, its keys laid over a success envelope. A
    `send_error` fails exactly one `sendall` before any later one succeeds.
    """

    def __init__(self, replies=(), send_errors=()) -> None:
        self.sent = []
        self._replies = list(replies)
        self.send_errors = list(send_errors)

    def sendall(self, data) -> None:
        if self.send_errors:
            raise self.send_errors.pop(0)
        self.sent.append(data)

    def settimeout(self, _value) -> None:
        pass

    def recv(self, _size):
        reply = self._replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        frame = {"id": self.sent_ids()[-1], "status": "success", **reply}
        return json.dumps(frame).encode("utf-8") + b"\n"

    def sent_ids(self) -> list[str]:
        return [json.loads(data)["id"] for data in self.sent]


def _reset() -> ConnectionResetError:
    return ConnectionResetError(54, "Connection reset by peer")


def _install_resending(monkeypatch, sock, *, capable=True, now=None):
    """
    Route dispatch over `sock` behind a handshake that does or does not advertise resend.

    A dropped socket reconnects to the same scripted one. `now` is a one-element list the
    ledger's clock reads, so expiry is crossed by moving a number.
    """
    _install_socket(monkeypatch, sock)
    blender = _dispatch.get_blender_connection()

    def reconnect() -> bool:
        blender.sock = sock
        return True

    monkeypatch.setattr(blender, "connect", reconnect)
    handshake = AddonHandshake(
        up_to_date=True,
        protocol_version=None,
        addon_version=None,
        capabilities=[],
        blender_version=None,
        source="native",
        idempotent_resend=capable,
    )
    monkeypatch.setattr(connection_module, "_addon_handshake", handshake)
    clock = now if now is not None else [0.0]
    monkeypatch.setattr(_dispatch, "_resend_ids", _dispatch._ResendIds(clock=lambda: clock[0]))
    return blender


def _call(command="create_primitive_object", **params):
    return asyncio.run(_dispatch.call_blender(command, params or {"primitive_type": "CUBE"}))


def _fails(command="create_primitive_object", **params) -> str:
    with pytest.raises(ToolError) as failure:
        _call(command, **params)
    return str(failure.value)


def test_a_transport_error_names_the_request_it_was_sent_under_and_whether_it_left() -> None:
    lost = _AnsweringSocket(replies=[_reset()])
    unsent = _AnsweringSocket(send_errors=[BrokenPipeError(32, "Broken pipe")])

    for sock, sent in ((lost, True), (unsent, False)):
        blender = BlenderConnection(host="localhost", port=0)
        blender.sock = sock
        with pytest.raises(BlenderTransportError) as failure:
            blender.send_command_locked("create_primitive_object", {}, request_id="feed")
        assert failure.value.request_id == "feed"
        assert failure.value.sent is sent
    assert lost.sent_ids() == ["feed"]


def test_an_identical_call_after_an_unknown_outcome_resends_under_the_same_id(monkeypatch) -> None:
    """The add-on answers an id it already ran from its reply cache, so the work is not done twice."""
    sock = _AnsweringSocket(replies=[_reset(), {"result": {"name": "Cube"}}])
    _install_resending(monkeypatch, sock)

    _fails()
    envelope = _call()

    first, resent = sock.sent_ids()
    assert resent == first
    assert envelope["data"] == {"name": "Cube"}


def test_the_outcome_unknown_hint_says_an_identical_retry_is_safe_when_the_addon_dedupes(monkeypatch) -> None:
    _install_resending(monkeypatch, _AnsweringSocket(replies=[_reset()]))

    message = _fails()

    assert message == f"Connection to Blender lost: [Errno 54] Connection reset by peer {_dispatch._RESEND_HINT}"
    assert "inspect the scene" not in message


def test_a_call_with_other_params_after_an_unknown_outcome_gets_a_new_id(monkeypatch) -> None:
    sock = _AnsweringSocket(replies=[_reset(), {"result": {}}])
    _install_resending(monkeypatch, sock)

    _fails(primitive_type="CUBE")
    _call(primitive_type="SPHERE")

    first, second = sock.sent_ids()
    assert second != first


def test_the_same_params_in_another_key_order_are_the_same_call(monkeypatch) -> None:
    sock = _AnsweringSocket(replies=[_reset(), {"result": {}}])
    _install_resending(monkeypatch, sock)

    _fails(primitive_type="CUBE", name="Box")
    _call(name="Box", primitive_type="CUBE")

    first, resent = sock.sent_ids()
    assert resent == first


def test_without_the_capability_an_identical_call_gets_a_new_id_and_the_old_hint(monkeypatch) -> None:
    """An add-on with no reply cache would run a resent id again, so nothing changes for it."""
    sock = _AnsweringSocket(replies=[_reset(), {"result": {}}])
    _install_resending(monkeypatch, sock, capable=False)

    message = _fails()
    _call()

    first, second = sock.sent_ids()
    assert second != first
    assert message.endswith(_dispatch._OUTCOME_UNKNOWN_HINT)


def test_a_remembered_id_expires_ten_minutes_after_its_last_unknown_outcome(monkeypatch) -> None:
    now = [0.0]
    sock = _AnsweringSocket(replies=[_reset(), _reset(), _reset(), {"result": {}}])
    _install_resending(monkeypatch, sock, now=now)

    _fails()
    now[0] = 599.0
    _fails()
    now[0] = 1_100.0
    _fails()
    now[0] = 1_100.0 + 600.001
    _call()

    first, within, restarted, after = sock.sent_ids()
    assert within == first, "an identical call inside the window resends"
    assert restarted == first, "each unknown outcome restarts the window"
    assert after != first, "ten minutes after the last unknown outcome the id is forgotten"


def test_the_remembered_ids_are_evicted_least_recently_used_first_past_sixty_four(monkeypatch) -> None:
    sock = _AnsweringSocket(replies=[_reset()] * 65 + [{"result": {}}, {"result": {}}])
    _install_resending(monkeypatch, sock)

    for index in range(65):
        _fails(name=f"Box{index}")
    _call(name="Box0")
    _call(name="Box64")

    ids = sock.sent_ids()
    assert ids[65] not in ids[:65], "the 65th entry pushed out the oldest"
    assert ids[66] == ids[64]


@pytest.mark.parametrize(
    "answer",
    [{"result": {"name": "Cube"}}, {"status": "error", "message": "Object 'Cube' already exists"}],
    ids=["reply", "refusal"],
)
def test_an_answer_forgets_the_id_so_a_later_identical_call_runs_anew(monkeypatch, answer) -> None:
    """A deliberate repeat after the outcome is known is a new request, not a resend."""
    sock = _AnsweringSocket(replies=[_reset(), answer, {"result": {}}])
    _install_resending(monkeypatch, sock)

    _fails()
    with suppress(ToolError):
        _call()
    _call()

    first, resent, fresh = sock.sent_ids()
    assert resent == first
    assert fresh != first


def test_a_resend_that_never_left_the_socket_keeps_the_id(monkeypatch) -> None:
    """The first send may still have run, so a resend that failed to go out must not lose its id."""
    sock = _AnsweringSocket(replies=[_reset(), {"result": {}}])
    _install_resending(monkeypatch, sock)

    _fails()
    sock.send_errors.append(BrokenPipeError(32, "Broken pipe"))
    unsent = _fails()
    _call()

    first, resent = sock.sent_ids()
    assert resent == first
    assert unsent.endswith(_dispatch._RETRY_HINT)


def test_a_replayed_reply_is_enveloped_like_any_other(monkeypatch) -> None:
    result = {"name": "Cube", "changed_objects": ["Cube"], "warnings": ["Scale is not applied"]}
    sock = _AnsweringSocket(replies=[_reset(), {"result": result, "replayed": True}, {"result": result}])
    _install_resending(monkeypatch, sock)

    _fails()
    replayed = _call()
    ordinary = _call()

    assert replayed == ordinary
    assert replayed["data"] == {"name": "Cube"}
    assert replayed["changed_objects"] == ["Cube"]


def test_a_reconnect_that_fails_is_reported_as_never_sent_and_worth_a_retry(monkeypatch) -> None:
    """
    A dropped socket that cannot be reopened never carried the command.

    It used to surface as a bare `ConnectionError`, so the agent got neither hint and could not
    tell a Blender that is gone from a command that may have run.
    """
    blender = _install_socket(monkeypatch, None)
    monkeypatch.setattr(blender, "connect", lambda: False)

    message = _fails()

    assert message.endswith(_dispatch._RETRY_HINT)


def test_no_connection_at_all_is_reported_as_never_sent_and_worth_a_retry(monkeypatch) -> None:
    """The first connection failing is the same fact: nothing reached Blender."""
    monkeypatch.setattr(_dispatch, "get_blender_connection", connection_module.get_blender_connection)
    monkeypatch.setattr(connection_module, "_blender_connection", None)
    monkeypatch.setattr(connection_module, "connect_with_retry", lambda *_args, **_kwargs: False)

    message = _fails()

    assert message.startswith("Could not connect to Blender")
    assert message.endswith(_dispatch._RETRY_HINT)


def _install_read_only(monkeypatch, sock, read_only_commands):
    blender = _install_resending(monkeypatch, sock, capable=False)
    handshake = connection_module._addon_handshake
    monkeypatch.setattr(handshake, "read_only_commands", frozenset(read_only_commands))
    return blender


def test_a_read_only_command_that_was_sent_and_lost_is_worth_a_retry(monkeypatch) -> None:
    """Running a read twice changes nothing, so inspecting the scene first would only cost a call."""
    _install_read_only(monkeypatch, _AnsweringSocket(replies=[TimeoutError("timed out")]), {"get_object_info"})

    message = _fails("get_object_info", name="Cube")

    assert message.endswith(_dispatch._READ_ONLY_RETRY_HINT)
    assert "inspect the scene" not in message


def test_a_mutating_command_lost_after_sending_still_asks_for_inspection(monkeypatch) -> None:
    _install_read_only(monkeypatch, _AnsweringSocket(replies=[TimeoutError("timed out")]), {"get_object_info"})

    assert _fails().endswith(_dispatch._OUTCOME_UNKNOWN_HINT)


def test_an_addon_that_does_not_name_its_reads_gets_the_cautious_hint(monkeypatch) -> None:
    """Guessing read-only from a tool's name would tell an agent to blindly repeat a mutation."""
    _install_resending(monkeypatch, _AnsweringSocket(replies=[TimeoutError("timed out")]), capable=False)

    assert _fails("get_object_info", name="Cube").endswith(_dispatch._OUTCOME_UNKNOWN_HINT)
