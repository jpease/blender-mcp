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


def _send(
    server: object,
    client: _Client,
    request_id: str,
    cmd_type: str = "create_primitive",
    *,
    resend: object = None,
    **params,
) -> dict:
    """
    Queue one frame through the production enqueue path, drain it, and return its answer.

    Args:
        server: The server.
        client: The socket the answer goes back on.
        request_id: The frame's id.
        cmd_type: The command.
        resend: The frame's `resend` field, marking it a resend; None sends a new request.
        **params: Its parameters.

    Returns:
        dict: The response frame.

    """
    with server._clients_lock:
        server._clients.setdefault(client, threading.Lock())
    body: dict[str, object] = {"type": cmd_type, "id": request_id, "params": params}
    if resend is not None:
        body["resend"] = resend
    frame = json.dumps(body).encode("utf-8")
    answered = len(client.frames())
    server._decode_and_queue_frame(frame, client)
    server._drain_batch()
    frames = client.frames()
    assert len(frames) == answered + 1, "the frame was not answered exactly once"
    return frames[-1]


def _session(server: object) -> dict[str, object]:
    """
    Name the open session the way the MCP server marks a resend with it.

    Args:
        server: The server.

    Returns:
        dict: `session_id` and `session_epoch`, as a resend frame carries them.

    """
    session_id, session_epoch = server._session_marker()
    return {"session_id": session_id, "session_epoch": session_epoch}


def _refusal(server: object, reason: str) -> str:
    return server._RESEND_REFUSAL.format(reason=reason)


def _without_replayed(frame: dict) -> dict:
    return {key: value for key, value in frame.items() if key != "replayed"}


def test_a_resent_id_runs_the_handler_once_and_is_answered_with_the_first_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _addon, _bpy, server_core = _setup(monkeypatch)
    create = _Counted({"name": "Cube", "changed_objects": ["Cube"]})
    server = _server(monkeypatch, server_core, {"create_primitive": create})
    client = _Client()
    sent_under = _session(server)

    first = _send(server, client, "r1", primitive_type="CUBE")
    replayed = _send(server, client, "r1", resend=sent_under, primitive_type="CUBE")

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


def test_a_resend_whose_reply_was_evicted_is_refused_rather_than_run_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Sixty-four later mutations push the first reply out, and a miss is no proof it never ran.

    Before resends were marked, the evicted id simply ran again: the cube was created twice.
    """
    _addon, _bpy, server_core = _setup(monkeypatch)
    create = _Counted({"name": "Cube"})
    server = _server(monkeypatch, server_core, {"create_primitive": create, "set_scene_frame": _Counted({})})
    client = _Client()
    sent_under = _session(server)

    _send(server, client, "r1", primitive_type="CUBE")
    for frame in range(64):
        _send(server, client, f"frame-{frame}", "set_scene_frame", frame=frame)
    refused = _send(server, client, "r1", resend=sent_under, primitive_type="CUBE")

    assert create.calls == 1
    assert refused["status"] == "error"
    assert refused["message"] == _refusal(server, server._RESEND_NOT_HELD_REASON)


def test_a_resent_read_is_refused_because_its_reply_is_not_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    """A read is cheap to issue again as a new request, so its reply does not take a mutation's place."""
    _addon, _bpy, server_core = _setup(monkeypatch)
    inspect = _Counted({"name": "Cube"})
    server = _server(monkeypatch, server_core, {"get_object_info": inspect})
    client = _Client()
    sent_under = _session(server)

    _send(server, client, "r1", "get_object_info", name="Cube")
    again = _send(server, client, "r1", "get_object_info", resend=sent_under, name="Cube")

    assert inspect.calls == 1
    assert again["status"] == "error"


def test_the_cache_survives_replacing_the_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop/Start Server builds a new instance, and that restart is what makes a client resend."""
    _addon, _bpy, server_core = _setup(monkeypatch)
    create = _Counted({"name": "Cube"})
    handlers = {"create_primitive": create}
    client = _Client()
    first_server = _server(monkeypatch, server_core, handlers)
    sent_under = _session(first_server)

    _send(first_server, client, "r1")
    replayed = _send(_server(monkeypatch, server_core, handlers), client, "r1", resend=sent_under)

    assert create.calls == 1
    assert replayed["replayed"] is True


def test_a_resend_after_another_file_opened_is_refused_rather_than_run_in_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    A reply written against another database says nothing about this one, and nor does its absence.

    Before resends were marked, the id missed the emptied cache and ran in the file just opened.
    """
    addon, _bpy, server_core = _setup(monkeypatch)
    create = _Counted({"name": "Cube"})
    server = _server(monkeypatch, server_core, {"create_primitive": create})
    client = _Client()
    sent_under = _session(server)

    _send(server, client, "r1")
    addon.session._on_load_post("/shots/sh010.blend")
    assert _session(server) != sent_under, "the load did not move the epoch"
    refused = _send(server, client, "r1", resend=sent_under)

    assert create.calls == 1
    assert refused["status"] == "error"
    assert refused["message"] == _refusal(server, server._RESEND_MOVED_REASON)


@pytest.mark.parametrize(
    "claimed",
    [{"session_id": "another-session", "session_epoch": 0}, True],
    ids=["another-session", "no-session"],
)
def test_a_resend_naming_another_session_than_its_first_run_is_refused(
    monkeypatch: pytest.MonkeyPatch, claimed: object
) -> None:
    """
    The reply is held, but the first attempt ran in a file the server did not send it for.

    That happens when Blender opened a file the server had not yet heard about: the run is
    real, and only an inspection can say whether it is the work the agent meant.
    """
    _addon, _bpy, server_core = _setup(monkeypatch)
    create = _Counted({"name": "Cube"})
    server = _server(monkeypatch, server_core, {"create_primitive": create})
    client = _Client()

    _send(server, client, "r1")
    refused = _send(server, client, "r1", resend=claimed)

    assert create.calls == 1
    assert refused["status"] == "error"
    assert "replayed" not in refused


def test_a_resent_file_swap_is_answered_from_the_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    A swap's reply is written under the database it opened, but the resend names the one it left.

    The entry remembers the session the swap ran under, so the resend still matches it.
    """
    addon, _bpy, server_core = _setup(monkeypatch)

    def open_shot(**_params: object) -> dict:
        addon.session._on_load_post("/shots/sh020.blend")
        return {"filepath": "/shots/sh020.blend"}

    server = _server(monkeypatch, server_core, {"open_shot": open_shot})
    client = _Client()
    sent_under = _session(server)

    first = _send(server, client, "swap", "open_shot", filepath="/shots/sh020.blend")
    assert _session(server) != sent_under, "the swap did not move the epoch"
    replayed = _send(server, client, "swap", "open_shot", resend=sent_under, filepath="/shots/sh020.blend")

    assert first["status"] == "success"
    assert replayed["replayed"] is True
    assert _without_replayed(replayed) == first


def test_an_oversized_reply_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """The client was sent the size-limit error, never the reply, so there is nothing to replay."""
    _addon, _bpy, server_core = _setup(monkeypatch)
    create = _Counted({"name": "Cube", "padding": "x" * 4096})
    server = _server(monkeypatch, server_core, {"create_primitive": create})
    monkeypatch.setattr(server, "_MAX_MESSAGE_BYTES", 1024)
    client = _Client()
    sent_under = _session(server)

    first = _send(server, client, "r1")
    again = _send(server, client, "r1", resend=sent_under)

    assert first["message"] == "Response exceeded the configured message-size limit"
    assert create.calls == 1
    assert again["message"] == _refusal(server, server._RESEND_NOT_HELD_REASON)


def test_an_image_reply_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """Holding inline image bytes would crowd every other reply out of the cache."""
    _addon, _bpy, server_core = _setup(monkeypatch)
    render = _Counted({"image_base64": "iVBORw0KGgo=", "width": 1})
    server = _server(monkeypatch, server_core, {"render_contact_sheet": render})
    client = _Client()
    sent_under = _session(server)

    _send(server, client, "r1", "render_contact_sheet")
    again = _send(server, client, "r1", "render_contact_sheet", resend=sent_under)

    assert render.calls == 1
    assert again["message"] == _refusal(server, server._RESEND_NOT_HELD_REASON)


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
    sent_under = _session(server)

    for handler in list(bpy.app.handlers.undo_post):
        handler(object(), None)
    first = _send(server, client, "r1")
    for handler in list(bpy.app.handlers.undo_post):
        handler(object(), None)
    replayed = _send(server, client, "r1", resend=sent_under)
    after = _send(server, client, "r2", "get_object_info")

    assert first["result"]["warnings"] == [UNDO_NOTICE]
    assert _without_replayed(replayed) == first
    assert after["result"]["warnings"] == [UNDO_NOTICE]


def test_the_handshake_advertises_marked_resend(monkeypatch: pytest.MonkeyPatch) -> None:
    """The earlier `idempotent_resend` is gone: a server that read it would resend unmarked, and that runs."""
    _addon, _bpy, server_core = _setup(monkeypatch)

    info = server_core.BlenderMCPServer().get_addon_info()

    assert info["marked_resend"] is True
    assert "idempotent_resend" not in info


def test_the_cache_keeps_sixty_four_replies_and_no_more_bytes_than_the_reply_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both bounds evict the least recently used reply, so a reply just looked up outlives older ones."""
    addon, _bpy, _server_core = _setup(monkeypatch)
    cache = sys.modules[f"{addon.__name__}.reply_cache"]
    marker = ("session", 1)

    def held(request_id: str) -> bytes | None:
        entry = cache.lookup(request_id, marker)
        return None if entry is None else entry.payload

    for index in range(65):
        assert cache.store(f"r{index}", marker, b"x", 1_000, ran_under=marker)
    assert held("r0") is None, "the 65th reply pushed out the first"
    assert held("r1") == b"x"

    assert cache.store("big", marker, b"y" * 990, 1_000, ran_under=marker)
    survivors = [f"r{index}" for index in range(65) if held(f"r{index}") is not None]
    # 990 bytes under a 1,000-byte cap leave room for ten 1-byte replies: r1, looked up last, and
    # the nine stored most recently.
    assert survivors == ["r1", *(f"r{index}" for index in range(56, 65))]
    assert not cache.store("huge", marker, b"z" * 1_001, 1_000, ran_under=marker)
    assert held("big") == b"y" * 990


def test_the_handshake_names_the_commands_that_never_mutate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only unconditionally read-only commands: one whose params decide is not safe to name."""
    _addon, _bpy, server_core = _setup(monkeypatch)
    server = server_core.BlenderMCPServer()

    named = set(server.get_addon_info()["read_only_commands"])

    expected = {
        name for name in server._build_command_handlers() if server_core.BlenderMCPServer.command_spec(name).read_only
    }
    assert named == expected
    assert "list_scene_objects" in named
    assert "create_primitive_object" not in named
