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
from blender_mcp.server.tools import _dispatch


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
