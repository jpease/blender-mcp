"""
Poly Haven downloads run on a fetch worker, never on the thread that sent the command.

The start command answers while the download is still running; the import takes the
finished files and removes their directory; cancellation and failure clean up too.
"""

import os
import sys
import threading
import time
import types

from pathlib import Path
from typing import Any

import pytest

from conftest import load_addon

_FILES = {"gltf": {"1k": {"gltf": {"url": "https://dl.polyhaven.org/file/m/model_1k.gltf"}}}}


def _server(monkeypatch: pytest.MonkeyPatch, download) -> tuple[Any, Any, list]:
    """
    Build an addon server whose Poly Haven model download is `download`.

    Args:
        monkeypatch: Fixture used to install the stubs.
        download: Stands in for `network.download_file`.

    Returns:
        tuple: The server, the fetch registry, and the stub `bpy.data.objects` list.

    """
    objects: list = []
    addon, bpy = load_addon(monkeypatch, data={"filepath": "", "objects": objects})

    def gltf(filepath: str) -> set[str]:
        assert Path(filepath).read_bytes() == b"gltf"
        objects.append(types.SimpleNamespace(name="Model", session_uid=1, type="MESH", parent=None))
        return {"FINISHED"}

    bpy.ops = types.SimpleNamespace(import_scene=types.SimpleNamespace(gltf=gltf))
    handler = sys.modules[f"{addon.__name__}.handlers.polyhaven"]
    monkeypatch.setattr(handler, "get_json", lambda *_a, **_k: _FILES)
    monkeypatch.setattr(handler, "download_file", download)
    registry = sys.modules[f"{addon.__name__}.provider_fetches"].REGISTRY
    server = sys.modules[f"{addon.__name__}.server_core"].BlenderMCPServer()
    return server, registry, objects


def test_a_download_runs_on_a_worker_and_the_import_consumes_its_files(monkeypatch: pytest.MonkeyPatch) -> None:
    """The start returns RUNNING at once; the import uses the file, removes the directory, and cannot repeat."""
    release = threading.Event()
    seen: dict = {}

    def download(_url: str, destination: str, **_kwargs: object) -> None:
        seen["thread"] = threading.get_ident()
        seen["directory"] = os.path.dirname(destination)
        assert release.wait(5)
        Path(destination).write_bytes(b"gltf")

    server, registry, _objects = _server(monkeypatch, download)

    began = time.monotonic()
    started = server.start_polyhaven_download("model", "models")
    assert time.monotonic() - began < 1
    assert started["state"] == "RUNNING"
    assert server.get_provider_fetch(started["fetch_id"])["state"] == "RUNNING"

    release.set()
    registry.join(started["fetch_id"], timeout=5)
    assert server.get_provider_fetch(started["fetch_id"])["state"] == "SUCCEEDED"
    assert seen["thread"] != threading.get_ident()

    result = server.import_polyhaven_asset(started["fetch_id"])

    assert result.get("success") is True, result
    assert result["changed_objects"] == ["Model"]
    assert not os.path.exists(seen["directory"])
    with pytest.raises(ValueError, match="Unknown provider fetch"):
        server.import_polyhaven_asset(started["fetch_id"])


def test_a_running_download_reports_its_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bytes the transfer counted reach the status while the download runs."""
    advanced = threading.Event()
    release = threading.Event()

    def download(_url: str, destination: str, *, transfer, **_kwargs: object) -> None:
        transfer.begin(10)
        transfer.advance(4)
        advanced.set()
        assert release.wait(5)
        transfer.advance(6)
        Path(destination).write_bytes(b"gltf")

    server, registry, _objects = _server(monkeypatch, download)
    started = server.start_polyhaven_download("model", "models")
    assert advanced.wait(5)

    status = server.get_provider_fetch(started["fetch_id"])

    assert (status["state"], status["bytes_received"], status["bytes_total"]) == ("RUNNING", 4, 10)
    release.set()
    registry.join(started["fetch_id"], timeout=5)
    status = server.get_provider_fetch(started["fetch_id"])
    assert (status["state"], status["bytes_received"], status["bytes_total"]) == ("SUCCEEDED", 10, 10)


def test_a_cancelled_download_stops_and_leaves_no_files(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cancelling stops the worker at its next chunk and removes the fetch's directory."""
    looping = threading.Event()
    seen: dict = {}

    def download(_url: str, destination: str, *, transfer, **_kwargs: object) -> None:
        seen["directory"] = os.path.dirname(destination)
        Path(destination).write_bytes(b"partial")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            transfer.advance(1)
            looping.set()
            time.sleep(0.01)

    server, registry, _objects = _server(monkeypatch, download)
    started = server.start_polyhaven_download("model", "models")
    assert looping.wait(5)

    reply = server.cancel_provider_fetch(started["fetch_id"])
    registry.join(started["fetch_id"], timeout=5)

    assert reply["state"] == "CANCELLING"
    with pytest.raises(ValueError, match="Unknown provider fetch"):
        server.get_provider_fetch(started["fetch_id"])
    assert not os.path.exists(seen["directory"])


def test_a_failed_download_is_sanitized_and_leaves_no_files(monkeypatch: pytest.MonkeyPatch) -> None:
    """A worker exception fails the fetch with no absolute path in its text, and its files go."""
    seen: dict = {}

    def download(_url: str, destination: str, **_kwargs: object) -> None:
        seen["directory"] = os.path.dirname(destination)
        Path(destination).write_bytes(b"partial")
        raise OSError(f"[Errno 28] No space left on device: '{destination}'")

    server, registry, _objects = _server(monkeypatch, download)
    started = server.start_polyhaven_download("model", "models")
    registry.join(started["fetch_id"], timeout=5)

    status = server.get_provider_fetch(started["fetch_id"])

    assert status["state"] == "FAILED"
    assert status["failure"].startswith("Failed to download asset: ")
    assert "No space left on device" in status["failure"]
    assert seen["directory"] not in status["failure"] and "model_1k" not in status["failure"], status
    assert not os.path.exists(seen["directory"])
