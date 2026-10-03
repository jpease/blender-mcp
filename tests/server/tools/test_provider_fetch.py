"""
A provider tool waits for Blender's worker-thread fetch by polling, relays progress, and cancels it with the call.

The add-on answers `start_*` at once (`bundled/addon/provider_fetches.py`); these tests stand a
scripted connection in for Blender and check what `server/tools/_provider_fetch.py` sends.
"""

import asyncio
import threading

from collections.abc import Iterator

import pytest

from mcp.server.fastmcp.exceptions import ToolError

from blender_mcp.server.tools import _dispatch, _provider_fetch


class _Blender:
    """Answers each command from a per-command script; the last answer repeats."""

    def __init__(self, script: dict[str, list[dict]], *, block_on: str | None = None) -> None:
        self._script = {command: iter(replies) for command, replies in script.items()}
        self._last: dict[str, dict] = {}
        self.calls: list[tuple[str, dict]] = []
        self.block_on = block_on
        self.blocked = threading.Event()
        self.unblock = threading.Event()

    def send_command(self, command: str, params: dict) -> dict:
        self.calls.append((command, params))
        if command == self.block_on:
            self.blocked.set()
            self.unblock.wait(5)
        replies: Iterator[dict] = self._script[command]
        self._last[command] = next(replies, self._last.get(command, {}))
        return dict(self._last[command])


class _Context:
    """Records progress notifications the way FastMCP's Context would send them."""

    def __init__(self) -> None:
        self.progress: list[tuple[float, float | None, str | None]] = []

    async def report_progress(self, progress: float, total: float | None = None, message: str | None = None) -> None:
        self.progress.append((progress, total, message))


def _running(received: int, total: int | None = 100, **extra: object) -> dict:
    return {
        "fetch_id": "f1",
        "state": "RUNNING",
        "stage": "downloading",
        "bytes_received": received,
        "bytes_total": total,
        **extra,
    }


@pytest.fixture
def blender(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_provider_fetch, "_FIRST_POLL_SECONDS", 0.0)
    monkeypatch.setattr(_provider_fetch, "_MAX_POLL_SECONDS", 0.0)

    def install(script: dict[str, list[dict]], **kwargs) -> _Blender:
        connection = _Blender(script, **kwargs)
        monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)
        return connection

    return install


def test_a_query_is_polled_to_its_result_with_progress_and_released(blender) -> None:
    connection = blender(
        {
            "start_polyhaven_catalog": [_running(0, warnings=["start notice"])],
            "get_provider_fetch": [
                _running(40),
                _running(40),
                {**_running(100), "state": "SUCCEEDED", "result": {"assets": {"a": 1}}, "warnings": ["poll notice"]},
            ],
            "cancel_provider_fetch": [{"fetch_id": "f1", "discarded": True, "state": "SUCCEEDED"}],
        }
    )
    ctx = _Context()

    # A duck-typed stand-in for FastMCP's Context: only `report_progress` is called.
    result = asyncio.run(
        _provider_fetch.fetch_provider_result(ctx, "start_polyhaven_catalog", {"limit": 2})  # pyright: ignore[reportArgumentType]
    )

    assert result == {"assets": {"a": 1}, "warnings": ["start notice", "poll notice"]}
    assert [command for command, _ in connection.calls] == [
        "start_polyhaven_catalog",
        "get_provider_fetch",
        "get_provider_fetch",
        "get_provider_fetch",
        "cancel_provider_fetch",
    ]
    assert all(params == {"fetch_id": "f1"} for command, params in connection.calls[1:])
    # Progress only ever grows: the repeated 40 is not sent twice.
    assert [(progress, total) for progress, total, _message in ctx.progress] == [(40, 100), (100, 100)]


def test_a_failed_fetch_is_a_tool_error_carrying_its_failure_and_is_discarded(blender) -> None:
    connection = blender(
        {
            "start_polyhaven_download": [_running(0)],
            "get_provider_fetch": [{**_running(5), "state": "FAILED", "failure": "Failed to download asset: 404"}],
            "cancel_provider_fetch": [{"fetch_id": "f1", "discarded": True, "state": "FAILED"}],
        }
    )

    with pytest.raises(ToolError, match="Failed to download asset: 404"):
        asyncio.run(
            _provider_fetch.import_fetched(
                None, "start_polyhaven_download", {"asset_id": "a"}, "import_polyhaven_asset"
            )
        )

    assert [command for command, _ in connection.calls][-1] == "cancel_provider_fetch"
    assert "import_polyhaven_asset" not in [command for command, _ in connection.calls]


def test_a_finished_download_is_handed_to_its_import_by_fetch_id(blender) -> None:
    connection = blender(
        {
            "start_sketchfab_download": [_running(0, warnings=["start notice"])],
            "get_provider_fetch": [{**_running(100), "state": "SUCCEEDED", "ready_to_import": True}],
            "import_sketchfab_model": [{"success": True, "imported_objects": ["Chair"], "warnings": ["import notice"]}],
        }
    )

    result = asyncio.run(
        _provider_fetch.import_fetched(
            None,
            "start_sketchfab_download",
            {"uid": "u"},
            "import_sketchfab_model",
            normalize_size=True,
            target_size=2.0,
        )
    )

    assert connection.calls[-1] == (
        "import_sketchfab_model",
        {"fetch_id": "f1", "normalize_size": True, "target_size": 2.0},
    )
    assert result["warnings"] == ["start notice", "import notice"]


def test_cancelling_the_call_cancels_the_fetch_in_blender(blender) -> None:
    connection = blender(
        {
            "start_sketchfab_download": [_running(0)],
            "get_provider_fetch": [_running(10)],
            "cancel_provider_fetch": [{"fetch_id": "f1", "discarded": True, "state": "CANCELLING"}],
        },
        block_on="get_provider_fetch",
    )

    async def call_then_cancel() -> None:
        task = asyncio.create_task(
            _provider_fetch.import_fetched(None, "start_sketchfab_download", {"uid": "u"}, "import_sketchfab_model")
        )
        while not connection.blocked.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        connection.unblock.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(call_then_cancel())

    assert ("cancel_provider_fetch", {"fetch_id": "f1"}) in connection.calls
    assert "import_sketchfab_model" not in [command for command, _ in connection.calls]
