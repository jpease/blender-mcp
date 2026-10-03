"""Sketchfab downloads run on a worker thread and hand Blender only the extracted files."""

import asyncio
import os
import sys
import threading
import time
import types
import zipfile

import pytest

from conftest import load_addon

from blender_mcp.server.tools import _dispatch, _provider_fetch, core, sketchfab


class _FakeObject:
    """A Blender object as the import reads it: identity, parent, type, name, custom props."""

    def __init__(self, name: str, session_uid: int) -> None:
        self.name = name
        self.session_uid = session_uid
        self.parent = None
        self.type = "EMPTY"
        self.props: dict = {}

    def __setitem__(self, key: str, value: object) -> None:
        self.props[key] = value

    def __getitem__(self, key: str) -> object:
        return self.props[key]


def _write_zip(path: str, members: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)


def _load(monkeypatch, objects: list | None = None):
    addon, bpy = load_addon(
        monkeypatch,
        scene=types.SimpleNamespace(
            blendermcp_use_polyhaven=False, blendermcp_use_sketchfab=True, blendermcp_use_nd=False
        ),
        data={"objects": objects if objects is not None else []},
    )
    server = addon.BlenderMCPServer()
    monkeypatch.setattr(server, "get_sketchfab_api_key", lambda: "saved-key")
    handler = addon.handlers.sketchfab
    registry = sys.modules[f"{addon.__name__}.provider_fetches"].REGISTRY

    def fake_get_json(url, *, headers=None, transfer=None, **_kwargs):
        if url.endswith("/download"):
            return {"gltf": {"url": "https://example.invalid/model.zip"}}
        return {"name": "Chair", "user": {"username": "artist"}, "license": {"label": "CC-BY"}}

    monkeypatch.setattr(handler, "get_json", fake_get_json)
    return server, bpy, handler, registry


def _succeeded_download(monkeypatch, objects: list | None = None):
    server, bpy, handler, registry = _load(monkeypatch, objects)
    directories = []

    def fake_download_file(url, path, *, max_bytes=None, transfer=None, **_kwargs):
        directories.append(os.path.dirname(path))
        _write_zip(path, {"scene/model.gltf": b"{}"})

    monkeypatch.setattr(handler, "download_file", fake_download_file)
    started = server.start_sketchfab_download("abc")
    registry.join(started["fetch_id"], timeout=5)
    return server, bpy, started["fetch_id"], directories[0]


def test_a_download_runs_on_a_worker_and_the_import_consumes_its_files(monkeypatch) -> None:
    objects: list = []
    server, bpy, handler, registry = _load(monkeypatch, objects)
    release = threading.Event()
    seen: dict = {}

    def fake_download_file(url, path, *, max_bytes=None, transfer=None, **_kwargs):
        seen["thread"] = threading.get_ident()
        seen["directory"] = os.path.dirname(path)
        assert release.wait(5)
        _write_zip(path, {"scene/model.gltf": b"{}"})

    monkeypatch.setattr(handler, "download_file", fake_download_file)

    started = server.start_sketchfab_download("abc")
    fetch_id = started["fetch_id"]
    assert started["state"] == "RUNNING"
    assert server.get_provider_fetch(fetch_id)["state"] == "RUNNING"
    release.set()
    registry.join(fetch_id, timeout=5)
    assert seen["thread"] != threading.get_ident()
    assert server.get_provider_fetch(fetch_id)["state"] == "SUCCEEDED"

    imported_paths = []

    def fake_gltf(*, filepath):
        imported_paths.append(filepath)
        objects.append(_FakeObject("Chair", 1))
        return {"FINISHED"}

    bpy.ops.import_scene = types.SimpleNamespace(gltf=fake_gltf)

    reply = server.import_sketchfab_model(fetch_id, normalize_size=False)

    assert reply["success"] is True
    assert reply["imported_objects"] == ["Chair"]
    assert reply["provenance"]["author"] == "artist"
    assert imported_paths == [os.path.join(seen["directory"], "scene", "model.gltf")]
    assert objects[0]["blender_mcp_source_uid"] == "abc"
    assert not os.path.exists(seen["directory"])


def test_an_archive_with_a_traversal_member_fails_the_fetch_and_removes_its_files(monkeypatch) -> None:
    server, _bpy, handler, registry = _load(monkeypatch)
    directories = []

    def fake_download_file(url, path, *, max_bytes=None, transfer=None, **_kwargs):
        directories.append(os.path.dirname(path))
        _write_zip(path, {"../escape.gltf": b"{}"})

    monkeypatch.setattr(handler, "download_file", fake_download_file)

    started = server.start_sketchfab_download("abc")
    registry.join(started["fetch_id"], timeout=5)
    status = server.get_provider_fetch(started["fetch_id"])

    assert status["state"] == "FAILED"
    assert "../escape.gltf" in status["failure"]
    assert not os.path.exists(directories[0])


def test_cancelling_a_running_download_stops_it_and_removes_its_files(monkeypatch) -> None:
    server, _bpy, handler, registry = _load(monkeypatch)
    entered = threading.Event()
    directories = []

    def fake_download_file(url, path, *, max_bytes=None, transfer=None, **_kwargs):
        directories.append(os.path.dirname(path))
        entered.set()
        while True:
            transfer.advance(1)
            time.sleep(0.01)

    monkeypatch.setattr(handler, "download_file", fake_download_file)

    started = server.start_sketchfab_download("abc")
    fetch_id = started["fetch_id"]
    assert entered.wait(5)
    cancelled = server.cancel_provider_fetch(fetch_id)
    registry.join(fetch_id, timeout=5)

    assert cancelled["discarded"] is True
    try:
        state = server.get_provider_fetch(fetch_id)["state"]
    except ValueError:
        state = "discarded"
    assert state in {"CANCELLED", "discarded"}
    assert not os.path.exists(directories[0])


def test_an_invalid_target_size_leaves_the_download_to_import(monkeypatch) -> None:
    server, _bpy, fetch_id, directory = _succeeded_download(monkeypatch)

    with pytest.raises(ValueError):
        server.import_sketchfab_model(fetch_id, normalize_size=True, target_size=0)

    assert server.get_provider_fetch(fetch_id)["ready_to_import"] is True
    assert os.path.isdir(directory)
    server.cancel_provider_fetch(fetch_id)


class _FetchConnection:
    """Answers a start command RUNNING, the first poll RUNNING, then SUCCEEDED."""

    def __init__(self, start: str, result: dict | None = None, status: dict | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._start = start
        self._result = result
        self._status = status or {}
        self._polls = 0

    def send_command(self, command: str, params: dict) -> dict:
        self.calls.append((command, params))
        if command in self._status:
            return dict(self._status[command])
        if command == self._start:
            return {"fetch_id": "f1", "state": "RUNNING", "bytes_received": 0}
        if command == "get_provider_fetch":
            self._polls += 1
            if self._polls < 2:
                return {"fetch_id": "f1", "state": "RUNNING", "bytes_received": 0}
            reply = {"fetch_id": "f1", "state": "SUCCEEDED", "bytes_received": 10}
            if self._result is not None:
                reply["result"] = self._result
            return reply
        if command == "cancel_provider_fetch":
            return {"fetch_id": "f1", "discarded": True, "state": "SUCCEEDED"}
        if command == "import_sketchfab_model":
            return {"success": True, "imported_objects": ["Chair"]}
        raise AssertionError(f"unexpected command {command}")


def test_the_import_tool_polls_the_download_then_imports_its_fetch(monkeypatch) -> None:
    connection = _FetchConnection("start_sketchfab_download")
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)
    monkeypatch.setattr(_provider_fetch, "_FIRST_POLL_SECONDS", 0.0)

    result = asyncio.run(sketchfab.import_sketchfab_model(ctx=None, uid="abc", target_size=2.0))

    commands = [command for command, _params in connection.calls]
    assert commands[0] == "start_sketchfab_download"
    assert commands.count("get_provider_fetch") == 2
    assert ("import_sketchfab_model", {"fetch_id": "f1", "normalize_size": True, "target_size": 2.0}) in (
        connection.calls
    )
    assert result["changed_objects"] == ["Chair"]


def test_sketchfab_integration_status_runs_the_account_check(monkeypatch) -> None:
    connection = _FetchConnection(
        "start_sketchfab_account_check",
        result={"enabled": False, "message": "Sketchfab API key seems invalid. Status code: 401"},
        status={"get_sketchfab_status": {"enabled": True, "message": "checking", "verify_api_key": True}},
    )
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)
    monkeypatch.setattr(_provider_fetch, "_FIRST_POLL_SECONDS", 0.0)

    result = asyncio.run(core.get_integration_status(ctx=None, provider="sketchfab"))

    assert result["data"] == {"enabled": False, "message": "Sketchfab API key seems invalid. Status code: 401"}
    assert ("start_sketchfab_account_check", {}) in connection.calls
