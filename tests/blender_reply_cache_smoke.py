# ruff: file-ignore[module-import-not-at-top-of-file]
"""
Run with Blender 5.1+ to prove a resent request id is answered from the first run or refused, never run again.

Frames go through the socket server's own enqueue path (`_decode_and_queue_frame`) and drain
loop (`_drain_batch`), the way a client thread and Blender's main-thread timer drive them;
headless Blender runs no timers, so the drain is called here. The command is a real mutating
handler, `create_primitive`, inside its real transaction. A resend carries the `resend` mark the
MCP server puts on it: the session its first attempt was sent under.

1. A resent id creates one object, and its answer is the first answer marked `replayed`.
2. A new id with the same params creates a second: the cache answers ids, not requests.
3. A new `BlenderMCPServer` (Stop/Start Server) still answers the resent id from the cache.
4. A resend whose reply sixty-four later mutations evicted is refused, and creates nothing.
5. A resend after `open_shot` replaced the database is refused, and creates nothing in the new file.
6. A resent `open_shot` is answered from the cache, though its reply was written under the file it opened.
"""

import json
import sys
import tempfile
import threading

from pathlib import Path

import bpy

sys.path.append(str(Path(__file__).resolve().parent))
from smoke_addon import load_addon

addon = load_addon("blender_mcp_reply_cache_smoke")

from blender_mcp_reply_cache_smoke.server_core import BlenderMCPServer


class _Client:
    """A client socket that keeps every frame written to it."""

    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def sendall(self, payload: bytes) -> None:
        self.writes.append(payload)

    def frames(self) -> list[dict]:
        return [json.loads(line) for line in b"".join(self.writes).split(b"\n") if line]


def _session() -> dict:
    session_id, session_epoch = BlenderMCPServer._session_marker()
    return {"session_id": session_id, "session_epoch": session_epoch}


def _answer(server, client: _Client, request_id: str, cmd_type: str, resend: dict | None, params: dict) -> dict:
    with server._clients_lock:
        server._clients.setdefault(client, threading.Lock())
    answered = len(client.frames())
    body = {"type": cmd_type, "id": request_id, "params": params}
    if resend is not None:
        body["resend"] = resend
    server._decode_and_queue_frame(json.dumps(body).encode("utf-8"), client)
    server._drain_batch()
    frames = client.frames()
    assert len(frames) == answered + 1, f"{request_id} was not answered exactly once"
    return frames[-1]


def _send(
    server, client: _Client, request_id: str, cmd_type: str = "create_primitive", resend: dict | None = None, **params
) -> dict:
    response = _answer(server, client, request_id, cmd_type, resend, params)
    assert response["status"] == "success", response
    return response


def _refused(server, client: _Client, request_id: str, resend: dict, reason: str, **params) -> dict:
    response = _answer(server, client, request_id, "create_primitive", resend, params)
    assert response["status"] == "error", response
    assert response["message"] == BlenderMCPServer._RESEND_REFUSAL.format(reason=reason), response
    assert "replayed" not in response, response
    return response


def _named(prefix: str) -> list[str]:
    return sorted(obj.name for obj in bpy.data.objects if obj.name.startswith(prefix))


def case_resent_id_runs_once(server, client: _Client) -> None:
    sent_under = _session()
    first = _send(server, client, "smoke-1", primitive_type="CUBE", name="ResendCube")
    replayed = _send(server, client, "smoke-1", resend=sent_under, primitive_type="CUBE", name="ResendCube")
    assert _named("ResendCube") == ["ResendCube"], _named("ResendCube")
    assert "replayed" not in first
    assert replayed.pop("replayed") is True
    assert replayed == first, (replayed, first)


def case_new_id_runs_again(server, client: _Client) -> None:
    _send(server, client, "smoke-2", primitive_type="CUBE", name="ResendCube")
    assert _named("ResendCube") == ["ResendCube", "ResendCube.001"], _named("ResendCube")


def case_survives_restart(client: _Client) -> None:
    replayed = _send(BlenderMCPServer(), client, "smoke-1", resend=_session(), primitive_type="CUBE", name="ResendCube")
    assert replayed["replayed"] is True
    assert len(_named("ResendCube")) == 2, _named("ResendCube")


def case_evicted_reply_is_refused(server, client: _Client) -> None:
    sent_under = _session()
    _send(server, client, "smoke-evicted", primitive_type="CUBE", name="ReviewDuplicate")
    for frame in range(64):
        _send(server, client, f"smoke-frame-{frame}", "set_scene_frame", frame=frame + 1)
    _refused(
        server,
        client,
        "smoke-evicted",
        sent_under,
        BlenderMCPServer._RESEND_NOT_HELD_REASON,
        primitive_type="CUBE",
        name="ReviewDuplicate",
    )
    assert _named("ReviewDuplicate") == ["ReviewDuplicate"], _named("ReviewDuplicate")


def case_resend_into_another_file_is_refused(server, client: _Client, blend_path: Path) -> None:
    _send(server, client, "smoke-save", "save_shot", filepath=str(blend_path))
    sent_under = _session()
    _send(server, client, "smoke-cross", primitive_type="CUBE", name="CrossFile")
    assert _named("CrossFile") == ["CrossFile"], _named("CrossFile")
    swap_sent_under = _session()
    opened = _send(server, client, "smoke-open", "open_shot", filepath=str(blend_path), discard_unsaved=True)
    assert _session() != sent_under, "open_shot did not move the session epoch"
    _refused(
        server,
        client,
        "smoke-cross",
        sent_under,
        BlenderMCPServer._RESEND_MOVED_REASON,
        primitive_type="CUBE",
        name="CrossFile",
    )
    assert _named("CrossFile") == [], _named("CrossFile")

    replayed = _send(
        server,
        client,
        "smoke-open",
        "open_shot",
        resend=swap_sent_under,
        filepath=str(blend_path),
        discard_unsaved=True,
    )
    assert replayed.pop("replayed") is True
    assert replayed == opened, (replayed, opened)


def main() -> None:
    addon.register()
    server = BlenderMCPServer()
    client = _Client()
    work = Path(tempfile.mkdtemp(prefix="reply_cache_smoke_"))
    case_resent_id_runs_once(server, client)
    case_new_id_runs_again(server, client)
    case_survives_restart(client)
    case_evicted_reply_is_refused(server, client)
    case_resend_into_another_file_is_refused(server, client, work / "sh070.blend")
    print("REPLY_CACHE_SMOKE_OK")


main()
