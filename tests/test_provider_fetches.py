"""
Provider network I/O runs on a worker thread, and the main-thread commands only start, report and take it.

`bundled/addon/provider_fetches.py` is the registry every Poly Haven and Sketchfab command
goes through. These tests drive it with jobs that block on an event the test holds, so
"the call returned before the download finished" is observed, not inferred.
"""

import os
import sys
import threading
import types

from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

from conftest import load_addon


def _fetches(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """
    Load the add-on and return its provider fetch module, with a fresh registry.

    Args:
        monkeypatch: Fixture used to install the fake bpy.

    Returns:
        ModuleType: `provider_fetches`.

    """
    addon, _bpy = load_addon(monkeypatch)
    return sys.modules[f"{addon.__name__}.provider_fetches"]


class _Clock:
    """A monotonic clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def _blocked_job(release: threading.Event, seen: dict, body: Callable | None = None) -> Callable:
    """
    Make a job that records its thread, waits for the test, then returns.

    Args:
        release: Set by the test to let the job finish.
        seen: Receives the job's thread id and context.
        body: Run after the release with the context; its return is the job's.

    Returns:
        Callable: The job.

    """

    def job(context):
        seen["thread"] = threading.get_ident()
        seen["context"] = context
        assert release.wait(5), "the test never released the job"
        return body(context) if body is not None else {"answer": 42}

    return job


def test_start_returns_while_the_job_is_still_running_on_another_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    fetches = _fetches(monkeypatch)
    registry = fetches.FetchRegistry()
    release, seen = threading.Event(), {}

    started = registry.start("polyhaven", "catalog", _blocked_job(release, seen), keeps_files=False, failure_label="x")

    assert started["state"] == "RUNNING"
    assert "result" not in started
    assert registry.status(started["fetch_id"])["state"] == "RUNNING"
    release.set()
    registry.join(started["fetch_id"], timeout=5)
    assert seen["thread"] != threading.get_ident()
    finished = registry.status(started["fetch_id"])
    assert finished["state"] == "SUCCEEDED"
    assert finished["result"] == {"answer": 42}
    assert finished["ready_to_import"] is False


def test_a_download_keeps_its_files_for_the_import_that_takes_it_once(monkeypatch: pytest.MonkeyPatch) -> None:
    fetches = _fetches(monkeypatch)
    registry = fetches.FetchRegistry()
    release, seen = threading.Event(), {}

    def body(context) -> dict:
        path = os.path.join(context.directory, "model.glb")
        Path(path).write_bytes(b"glb")
        return {"main_file": path}

    started = registry.start(
        "sketchfab", "model", _blocked_job(release, seen, body), keeps_files=True, failure_label="x"
    )
    with pytest.raises(ValueError, match="still running"):
        registry.take(started["fetch_id"], provider="sketchfab", kind="model")
    release.set()
    registry.join(started["fetch_id"], timeout=5)

    status = registry.status(started["fetch_id"])
    assert status["ready_to_import"] is True and "result" not in status
    with pytest.raises(ValueError, match="polyhaven asset"):
        registry.take(started["fetch_id"], provider="polyhaven", kind="asset")
    payload, directory = registry.take(started["fetch_id"], provider="sketchfab", kind="model")
    assert Path(payload["main_file"]).read_bytes() == b"glb"
    assert Path(payload["main_file"]).parent == Path(directory)
    with pytest.raises(ValueError, match="Unknown provider fetch"):
        registry.take(started["fetch_id"], provider="sketchfab", kind="model")


def test_status_reports_bytes_as_they_arrive(monkeypatch: pytest.MonkeyPatch) -> None:
    fetches = _fetches(monkeypatch)
    registry = fetches.FetchRegistry()
    midway, release = threading.Event(), threading.Event()

    def job(context) -> dict:
        context.stage("downloading archive")
        context.begin(10)
        context.advance(4)
        midway.set()
        assert release.wait(5)
        context.begin(None)
        context.advance(6)
        return {}

    started = registry.start("sketchfab", "model", job, keeps_files=True, failure_label="x")
    assert midway.wait(5)

    during = registry.status(started["fetch_id"])
    assert (during["stage"], during["bytes_received"], during["bytes_total"]) == ("downloading archive", 4, 10)
    release.set()
    registry.join(started["fetch_id"], timeout=5)
    after = registry.status(started["fetch_id"])
    # A response that declared no size makes the total unknown rather than wrong.
    assert (after["bytes_received"], after["bytes_total"]) == (10, None)


def test_cancelling_a_running_fetch_stops_its_transfer_and_removes_its_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetches = _fetches(monkeypatch)
    registry = fetches.FetchRegistry()
    streaming, chunks = threading.Event(), []

    def job(context) -> dict:
        Path(context.directory, "partial.zip").write_bytes(b"half")
        while True:
            context.advance(1)
            chunks.append(1)
            streaming.set()
            threading.Event().wait(0.001)

    started = registry.start("sketchfab", "model", job, keeps_files=True, failure_label="x")
    assert streaming.wait(5)
    directory = registry._fetches[started["fetch_id"]].directory

    cancelled = registry.cancel(started["fetch_id"])
    registry.join(started["fetch_id"], timeout=5)

    assert cancelled == {"fetch_id": started["fetch_id"], "discarded": True, "state": "CANCELLING"}
    assert not os.path.exists(directory)
    with pytest.raises(ValueError, match="Unknown provider fetch"):
        registry.status(started["fetch_id"])
    # Idempotent: a second cancel of a gone fetch is not an error.
    assert registry.cancel(started["fetch_id"])["discarded"] is False


def test_a_failed_job_reports_a_sanitized_failure_and_leaves_no_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fetches = _fetches(monkeypatch)
    registry = fetches.FetchRegistry()
    leak = f"{tmp_path}/cache/asset_1k.hdr"

    def job(context) -> dict:
        Path(context.directory, "partial").write_bytes(b"x")
        raise OSError(f"[Errno 28] No space left on device: '{leak}'")

    started = registry.start("polyhaven", "asset", job, keeps_files=True, failure_label="Failed to download asset")
    directory = registry._fetches[started["fetch_id"]].directory
    registry.join(started["fetch_id"], timeout=5)

    status = registry.status(started["fetch_id"])
    assert status["state"] == "FAILED"
    assert status["failure"].startswith("Failed to download asset: ")
    assert "No space left on device" in status["failure"] and str(tmp_path) not in status["failure"]
    assert "error" not in status
    assert not os.path.exists(directory)
    with pytest.raises(ValueError, match="No space left on device"):
        registry.take(started["fetch_id"], provider="polyhaven", kind="asset")


def test_a_job_returning_an_error_shape_fails_with_that_message(monkeypatch: pytest.MonkeyPatch) -> None:
    fetches = _fetches(monkeypatch)
    registry = fetches.FetchRegistry()

    started = registry.start(
        "sketchfab",
        "search",
        lambda _context: {"error": "Authentication failed (401)."},
        keeps_files=False,
        failure_label="x",
    )
    registry.join(started["fetch_id"], timeout=5)

    status = registry.status(started["fetch_id"])
    assert (status["state"], status["failure"]) == ("FAILED", "Authentication failed (401).")
    assert "result" not in status


def test_only_a_bounded_number_of_fetches_run_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    fetches = _fetches(monkeypatch)
    registry = fetches.FetchRegistry()
    release = threading.Event()
    ids = [
        registry.start("polyhaven", "asset", _blocked_job(release, {}), keeps_files=True, failure_label="x")["fetch_id"]
        for _ in range(fetches.MAX_RUNNING_FETCHES)
    ]

    with pytest.raises(RuntimeError, match="already running"):
        registry.start("polyhaven", "asset", _blocked_job(release, {}), keeps_files=True, failure_label="x")
    release.set()
    for fetch_id in ids:
        registry.join(fetch_id, timeout=5)
    registry.start("polyhaven", "catalog", lambda _context: {}, keeps_files=False, failure_label="x")


def test_a_finished_fetch_expires_and_an_abandoned_one_is_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    fetches = _fetches(monkeypatch)
    clock = _Clock()
    registry = fetches.FetchRegistry(clock=clock)
    finished = registry.start(
        "sketchfab", "model", lambda _context: {"main_file": "m"}, keeps_files=True, failure_label="x"
    )
    registry.join(finished["fetch_id"], timeout=5)
    finished_directory = registry._fetches[finished["fetch_id"]].directory
    release, seen = threading.Event(), {}
    abandoned = registry.start("sketchfab", "model", _blocked_job(release, seen), keeps_files=True, failure_label="x")
    abandoned_directory = registry._fetches[abandoned["fetch_id"]].directory

    clock.now += max(fetches.RESULT_TTL_SECONDS, fetches.ABANDONED_AFTER_SECONDS) + 1
    # Any registry call sweeps; a third, unrelated status poll is enough.
    third = registry.start("polyhaven", "catalog", lambda _context: {}, keeps_files=False, failure_label="x")
    registry.join(third["fetch_id"], timeout=5)

    assert not os.path.exists(finished_directory)
    assert registry._fetches[abandoned["fetch_id"]].cancel.is_set()
    release.set()
    registry.join(abandoned["fetch_id"], timeout=5)
    assert abandoned["fetch_id"] not in registry._fetches
    assert not os.path.exists(abandoned_directory)


def test_polling_keeps_a_long_download_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    fetches = _fetches(monkeypatch)
    clock = _Clock()
    registry = fetches.FetchRegistry(clock=clock)
    release = threading.Event()
    started = registry.start("polyhaven", "asset", _blocked_job(release, {}), keeps_files=True, failure_label="x")

    for _ in range(3):
        clock.now += fetches.ABANDONED_AFTER_SECONDS - 1
        assert registry.status(started["fetch_id"])["state"] == "RUNNING"
    release.set()
    registry.join(started["fetch_id"], timeout=5)
    assert registry.status(started["fetch_id"])["state"] == "SUCCEEDED"


def test_shutdown_cancels_running_fetches_and_removes_finished_files(monkeypatch: pytest.MonkeyPatch) -> None:
    fetches = _fetches(monkeypatch)
    registry = fetches.FetchRegistry()
    done = registry.start("polyhaven", "asset", lambda _context: {"file": "f"}, keeps_files=True, failure_label="x")
    registry.join(done["fetch_id"], timeout=5)
    done_directory = registry._fetches[done["fetch_id"]].directory
    release = threading.Event()
    running = registry.start("polyhaven", "asset", _blocked_job(release, {}), keeps_files=True, failure_label="x")

    registry.shutdown()

    assert not os.path.exists(done_directory)
    assert registry._fetches[running["fetch_id"]].cancel.is_set()
    release.set()
    registry.join(running["fetch_id"], timeout=5)
    assert registry._fetches == {}


def test_the_addon_commands_report_and_cancel_through_the_shared_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    addon, _bpy = load_addon(monkeypatch)
    fetches = sys.modules[f"{addon.__name__}.provider_fetches"]
    server = addon.BlenderMCPServer()
    release = threading.Event()
    started = fetches.REGISTRY.start(
        "polyhaven", "asset", _blocked_job(release, {}), keeps_files=True, failure_label="x"
    )

    assert server.get_provider_fetch(started["fetch_id"])["state"] == "RUNNING"
    assert server.cancel_provider_fetch(started["fetch_id"])["state"] == "CANCELLING"
    release.set()
    fetches.REGISTRY.join(started["fetch_id"], timeout=5)
    with pytest.raises(ValueError, match="Unknown provider fetch"):
        server.get_provider_fetch(started["fetch_id"])


def test_unregistering_the_addon_cancels_its_running_fetches(monkeypatch: pytest.MonkeyPatch) -> None:
    addon, bpy = load_addon(monkeypatch, scene=None)
    bpy.utils = types.SimpleNamespace(register_class=lambda _cls: None, unregister_class=lambda _cls: None)
    bpy.app.handlers.render_init = []
    registry = sys.modules[f"{addon.__name__}.provider_fetches"].REGISTRY
    release = threading.Event()
    addon.register()
    running = registry.start("sketchfab", "model", _blocked_job(release, {}), keeps_files=True, failure_label="x")

    addon.unregister()

    assert registry._fetches[running["fetch_id"]].cancel.is_set()
    release.set()
    registry.join(running["fetch_id"], timeout=5)
    assert registry._fetches == {}
