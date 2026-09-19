import asyncio
import os

import pytest

from blender_mcp.server.tools import viewport
from blender_mcp.server.tools.sketchfab import _preview_metadata
from blender_mcp.server.tools.viewport import _screenshot_metadata


def test_preview_metadata_reports_uid_model_name_and_author() -> None:
    result = {"model_name": "Low Poly Tree", "author": "Jane Doe", "thumbnail_width": 512, "thumbnail_height": 512}

    metadata = _preview_metadata(result, "abc123")

    assert metadata == {
        "uid": "abc123",
        "model_name": "Low Poly Tree",
        "author": "Jane Doe",
        "thumbnail_width": 512,
        "thumbnail_height": 512,
    }


def test_preview_metadata_defaults_missing_fields() -> None:
    metadata = _preview_metadata({}, "abc123")

    assert metadata == {
        "uid": "abc123",
        "model_name": "Unknown",
        "author": "Unknown",
        "thumbnail_width": None,
        "thumbnail_height": None,
    }


def test_screenshot_metadata_reports_width_height_and_method() -> None:
    result = {"width": 1000, "height": 562, "method": "offscreen"}

    metadata = _screenshot_metadata(result)

    assert metadata == {"width": 1000, "height": 562, "method": "offscreen"}


def test_screenshot_metadata_defaults_missing_fields_to_none() -> None:
    metadata = _screenshot_metadata({})

    assert metadata == {"width": None, "height": None, "method": None}


def test_screenshot_tempfile_is_removed_when_blender_fails(monkeypatch, tmp_path) -> None:
    screenshot = tmp_path / "request.png"

    class Connection:
        def send_command(self, *_args, **_kwargs):
            raise RuntimeError("capture failed")

    def fake_mkstemp(**_kwargs):
        descriptor = os.open(screenshot, os.O_CREAT | os.O_RDWR)
        return descriptor, str(screenshot)

    monkeypatch.setattr(viewport, "get_blender_connection", Connection)
    monkeypatch.setattr(viewport.tempfile, "mkstemp", fake_mkstemp)

    with pytest.raises(Exception, match="Screenshot failed"):
        # The tool is async so its blocking socket work stays off the event loop;
        # asyncio.run drives it without the suite needing an async plugin.
        asyncio.run(viewport.get_viewport_screenshot(ctx=None))

    assert not screenshot.exists()
