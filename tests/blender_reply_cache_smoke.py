# ruff: file-ignore[module-import-not-at-top-of-file]
"""
Run with Blender 5.1+ to prove a resent request id is answered from the first run, not run again.

Frames go through the socket server's own enqueue path (`_decode_and_queue_frame`) and drain
loop (`_drain_batch`), the way a client thread and Blender's main-thread timer drive them;
headless Blender runs no timers, so the drain is called here. The command is a real mutating
handler, `create_primitive`, inside its real transaction.

1. A resent id creates one object, and its answer is the first answer marked `replayed`.
2. A new id with the same params creates a second: the cache answers ids, not requests.
3. A new `BlenderMCPServer` (Stop/Start Server) still answers the resent id from the cache.
4. After `open_shot` moves the session epoch, the same id runs again.
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

PARAMS = {"primitive_type": "CUBE", "name": "ResendCube"}


class _Client:
    """A client socket that keeps every frame written to it."""

    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def sendall(self, payload: bytes) -> None:
        self.writes.append(payload)

    def frames(self) -> list[dict]:
        return [json.loads(line) for line in b"".join(self.writes).split(b"\n") if line]


def _send(server, client: _Client, request_id: str, cmd_type: str = "create_primitive", **params) -> dict:
    with server._clients_lock:
        server._clients.setdefault(client, threading.Lock())
    answered = len(client.frames())
    frame = json.dumps({"type": cmd_type, "id": request_id, "params": params}).encode("utf-8")
    server._decode_and_queue_frame(frame, client)
    server._drain_batch()
    frames = client.frames()
    assert len(frames) == answered + 1, f"{request_id} was not answered exactly once"
    response = frames[-1]
    assert response["status"] == "success", response
    return response


def _cubes() -> list[str]:
    return sorted(obj.name for obj in bpy.data.objects if obj.name.startswith("ResendCube"))


def case_resent_id_runs_once(server, client: _Client) -> None:
    first = _send(server, client, "smoke-1", **PARAMS)
    replayed = _send(server, client, "smoke-1", **PARAMS)
    assert _cubes() == ["ResendCube"], _cubes()
    assert "replayed" not in first
    assert replayed.pop("replayed") is True
    assert replayed == first, (replayed, first)


def case_new_id_runs_again(server, client: _Client) -> None:
    _send(server, client, "smoke-2", **PARAMS)
    assert _cubes() == ["ResendCube", "ResendCube.001"], _cubes()


def case_survives_restart(client: _Client) -> None:
    replayed = _send(BlenderMCPServer(), client, "smoke-1", **PARAMS)
    assert replayed["replayed"] is True
    assert len(_cubes()) == 2, _cubes()


def case_epoch_move_clears(server, client: _Client, blend_path: Path) -> None:
    _send(server, client, "smoke-save", "save_shot", filepath=str(blend_path))
    _send(server, client, "smoke-open", "open_shot", filepath=str(blend_path), discard_unsaved=True)
    before = len(_cubes())
    again = _send(server, client, "smoke-1", **PARAMS)
    assert "replayed" not in again
    assert len(_cubes()) == before + 1, _cubes()


def main() -> None:
    addon.register()
    server = BlenderMCPServer()
    client = _Client()
    work = Path(tempfile.mkdtemp(prefix="reply_cache_smoke_"))
    case_resent_id_runs_once(server, client)
    case_new_id_runs_again(server, client)
    case_survives_restart(client)
    case_epoch_move_clears(server, client, work / "sh070.blend")
    print("REPLY_CACHE_SMOKE_OK")


main()
