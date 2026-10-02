"""
The add-on's reply cache: a resent request id is answered from the first run, not run again.

Driven through the drain loop the socket server uses: a frame is decoded and queued the way a
client thread queues it, and `_drain_batch` runs it on the "main thread". The handlers are
stubs that count their calls, so "runs once" is a count, not an inference from a reply.
"""

from __future__ import annotations

import json
import sys
import threading

from collections.abc import Mapping
from types import ModuleType

import pytest

from conftest import load_addon
from datablock_doubles import TRACKED_COLLECTIONS, FakeCollection

UNDO_NOTICE = "The scene was undone once since the last command - re-inspect anything you read before."


class _Client:
    """A client socket that keeps every frame written to it."""

    def __init__(self) -> None:
        """Start with nothing written."""
        self.writes: list[bytes] = []

    def sendall(self, payload: bytes) -> None:
        """
        Record one write.

        Args:
            payload: The framed bytes.

        """
        self.writes.append(payload)

    def frames(self) -> list[dict]:
        """
        Decode everything written so far.

        Returns:
            list[dict]: One response per frame, in write order.

        """
        return [json.loads(line) for line in b"".join(self.writes).split(b"\n") if line]


class _Counted:
    """A handler that counts its calls and answers with a fixed result."""

    def __init__(self, result: dict) -> None:
        """
        Fix the result every call returns.

        Args:
            result: The handler's result.

        """
        self.calls = 0
        self._result = result

    def __call__(self, **_params: object) -> dict:
        """
        Run once more.

        Returns:
            dict: A copy of the fixed result.

        """
        self.calls += 1
        return dict(self._result)


def _setup(monkeypatch: pytest.MonkeyPatch) -> tuple[ModuleType, ModuleType, ModuleType]:
    """
    Load the add-on with a database a transaction can snapshot.

    Args:
        monkeypatch: Installs the fakes.

    Returns:
        tuple: The add-on package, the fake `bpy`, and `server_core`.

    """
    data: dict[str, object] = {name: FakeCollection() for name in TRACKED_COLLECTIONS}
    data["filepath"] = ""
    addon, bpy = load_addon(monkeypatch, data=data, use_global_undo=True)
    return addon, bpy, sys.modules[f"{addon.__name__}.server_core"]


def _server(monkeypatch: pytest.MonkeyPatch, server_core: ModuleType, handlers: Mapping[str, object]) -> object:
    """
    Build a server whose dispatch table is `handlers`.

    Args:
        monkeypatch: Restores the table afterwards.
        server_core: The loaded `server_core` module.
        handlers: Command name -> handler.

    Returns:
        object: The server.

    """
    server = server_core.BlenderMCPServer()
    monkeypatch.setattr(server, "_build_command_handlers", lambda: handlers)
    return server


def _send(server: object, client: _Client, request_id: str, cmd_type: str = "create_primitive", **params) -> dict:
    """
    Queue one frame through the production enqueue path, drain it, and return its answer.

    Args:
        server: The server.
        client: The socket the answer goes back on.
        request_id: The frame's id.
        cmd_type: The command.
        **params: Its parameters.

    Returns:
        dict: The response frame.

    """
    with server._clients_lock:
        server._clients.setdefault(client, threading.Lock())
    frame = json.dumps({"type": cmd_type, "id": request_id, "params": params}).encode("utf-8")
    answered = len(client.frames())
    server._decode_and_queue_frame(frame, client)
    server._drain_batch()
    frames = client.frames()
    assert len(frames) == answered + 1, "the frame was not answered exactly once"
    return frames[-1]


def _without_replayed(frame: dict) -> dict:
    return {key: value for key, value in frame.items() if key != "replayed"}


def test_a_resent_id_runs_the_handler_once_and_is_answered_with_the_first_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _addon, _bpy, server_core = _setup(monkeypatch)
    create = _Counted({"name": "Cube", "changed_objects": ["Cube"]})
    server = _server(monkeypatch, server_core, {"create_primitive": create})
    client = _Client()

    first = _send(server, client, "r1", primitive_type="CUBE")
    replayed = _send(server, client, "r1", primitive_type="CUBE")

    assert create.calls == 1
    assert first["status"] == "success"
    assert "replayed" not in first
    assert replayed["replayed"] is True
    assert _without_replayed(replayed) == first


def test_a_new_id_with_the_same_params_runs_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cache answers a request id, not a request: a deliberate repeat is new work."""
    _addon, _bpy, server_core = _setup(monkeypatch)
    create = _Counted({"name": "Cube"})
    server = _server(monkeypatch, server_core, {"create_primitive": create})
    client = _Client()

    _send(server, client, "r1", primitive_type="CUBE")
    again = _send(server, client, "r2", primitive_type="CUBE")

    assert create.calls == 2
    assert "replayed" not in again


def test_a_read_only_command_is_run_again_rather_than_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rerunning a read is safe and answers with the scene as it is now."""
    _addon, _bpy, server_core = _setup(monkeypatch)
    inspect = _Counted({"name": "Cube"})
    server = _server(monkeypatch, server_core, {"get_object_info": inspect})
    client = _Client()

    _send(server, client, "r1", "get_object_info", name="Cube")
    again = _send(server, client, "r1", "get_object_info", name="Cube")

    assert inspect.calls == 2
    assert "replayed" not in again


def test_the_cache_survives_replacing_the_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop/Start Server builds a new instance, and that restart is what makes a client resend."""
    _addon, _bpy, server_core = _setup(monkeypatch)
    create = _Counted({"name": "Cube"})
    handlers = {"create_primitive": create}
    client = _Client()

    _send(_server(monkeypatch, server_core, handlers), client, "r1")
    replayed = _send(_server(monkeypatch, server_core, handlers), client, "r1")

    assert create.calls == 1
    assert replayed["replayed"] is True


def test_an_epoch_move_clears_the_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reply written against another database says nothing about this one."""
    addon, _bpy, server_core = _setup(monkeypatch)
    create = _Counted({"name": "Cube"})
    server = _server(monkeypatch, server_core, {"create_primitive": create})
    client = _Client()

    _send(server, client, "r1")
    epoch = addon.session.session_snapshot()["session_epoch"]
    addon.session._on_load_post("/shots/sh010.blend")
    assert addon.session.session_snapshot()["session_epoch"] != epoch, "the load did not move the epoch"
    again = _send(server, client, "r1")

    assert create.calls == 2
    assert "replayed" not in again


def test_an_oversized_reply_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """The client was sent the size-limit error, never the reply, so there is nothing to replay."""
    _addon, _bpy, server_core = _setup(monkeypatch)
    create = _Counted({"name": "Cube", "padding": "x" * 4096})
    server = _server(monkeypatch, server_core, {"create_primitive": create})
    monkeypatch.setattr(server, "_MAX_MESSAGE_BYTES", 1024)
    client = _Client()

    first = _send(server, client, "r1")
    again = _send(server, client, "r1")

    assert first["message"] == "Response exceeded the configured message-size limit"
    assert create.calls == 2
    assert "replayed" not in again


def test_an_image_reply_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """Holding inline image bytes would crowd every other reply out of the cache."""
    _addon, _bpy, server_core = _setup(monkeypatch)
    render = _Counted({"image_base64": "iVBORw0KGgo=", "width": 1})
    server = _server(monkeypatch, server_core, {"render_contact_sheet": render})
    client = _Client()

    _send(server, client, "r1", "render_contact_sheet")
    again = _send(server, client, "r1", "render_contact_sheet")

    assert render.calls == 2
    assert "replayed" not in again


def test_a_replay_neither_consumes_nor_repeats_a_pending_scene_notice(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    The replay is the original reply, notice included; a notice pending since waits for real work.

    The first reply consumed the undo it reported, and its client never read it, so the replay
    carries it unchanged. The undo after it belongs to the next command that actually runs.
    """
    addon, bpy, server_core = _setup(monkeypatch)
    addon.scene_watch.register_handlers()
    create = _Counted({"name": "Cube"})
    server = _server(monkeypatch, server_core, {"create_primitive": create, "get_object_info": _Counted({})})
    client = _Client()

    for handler in list(bpy.app.handlers.undo_post):
        handler(object(), None)
    first = _send(server, client, "r1")
    for handler in list(bpy.app.handlers.undo_post):
        handler(object(), None)
    replayed = _send(server, client, "r1")
    after = _send(server, client, "r2", "get_object_info")

    assert first["result"]["warnings"] == [UNDO_NOTICE]
    assert _without_replayed(replayed) == first
    assert after["result"]["warnings"] == [UNDO_NOTICE]


def test_the_handshake_advertises_idempotent_resend(monkeypatch: pytest.MonkeyPatch) -> None:
    _addon, _bpy, server_core = _setup(monkeypatch)

    assert server_core.BlenderMCPServer().get_addon_info()["idempotent_resend"] is True


def test_the_cache_keeps_sixty_four_replies_and_no_more_bytes_than_the_reply_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both bounds evict the least recently used reply, so a reply just looked up outlives older ones."""
    addon, _bpy, _server_core = _setup(monkeypatch)
    cache = sys.modules[f"{addon.__name__}.reply_cache"]
    marker = ("session", 1)

    for index in range(65):
        assert cache.store(f"r{index}", marker, b"x", 1_000)
    assert cache.lookup("r0", marker) is None, "the 65th reply pushed out the first"
    assert cache.lookup("r1", marker) == b"x"

    assert cache.store("big", marker, b"y" * 990, 1_000)
    survivors = [f"r{index}" for index in range(65) if cache.lookup(f"r{index}", marker) is not None]
    # 990 bytes under a 1,000-byte cap leave room for ten 1-byte replies: r1, looked up last, and
    # the nine stored most recently.
    assert survivors == ["r1", *(f"r{index}" for index in range(56, 65))]
    assert not cache.store("huge", marker, b"z" * 1_001, 1_000)
    assert cache.lookup("big", marker) == b"y" * 990
