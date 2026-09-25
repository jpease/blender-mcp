"""
The one dispatch helper every tool module sends its commands through.

Covers what the thirteen per-package copies used to decide separately: which thread the
blocking socket call runs on, and how Blender's two failure kinds reach the client.
"""

import asyncio
import threading

import pytest

from mcp.server.fastmcp.exceptions import ToolError

from blender_mcp.server.connection import BlenderOperationError, BlenderTransportError
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


def test_a_transport_failure_says_the_connection_was_dropped_and_is_worth_a_retry(monkeypatch) -> None:
    _install(monkeypatch, _Connection(error=BlenderTransportError("Connection to Blender lost: [Errno 32]")))

    with pytest.raises(ToolError) as failure:
        asyncio.run(_dispatch.call_blender("configure_cloth", {}))

    message = str(failure.value)
    assert message.startswith("Connection to Blender lost: [Errno 32]")
    assert "retry once" in message


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
def test_a_tool_keeps_the_retry_hint_on_a_dropped_connection(stub_blender_connection, call) -> None:
    lost = BlenderTransportError("Connection to Blender lost: [Errno 32]")
    stub_blender_connection(error=lost)

    with pytest.raises(ToolError) as failure:
        asyncio.run(call())

    assert str(failure.value) == f"{lost} {_dispatch._RETRY_HINT}"
