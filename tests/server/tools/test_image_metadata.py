import os

import pytest

from blender_mcp.server.tools import _image_transport, viewport
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

    monkeypatch.setattr(_image_transport, "get_blender_connection", Connection)
    monkeypatch.setattr(_image_transport.tempfile, "mkstemp", fake_mkstemp)

    with pytest.raises(Exception, match="Screenshot failed"):
        viewport.get_viewport_screenshot(ctx=None)

    assert not screenshot.exists()


def test_screenshot_uses_the_inline_transport_when_the_addon_supports_it(monkeypatch) -> None:
    import base64
    import types

    from blender_mcp.server.tools import _image_transport

    calls = []

    class Connection:
        def send_command(self, command, params):
            calls.append((command, params))
            return {
                "image_base64": base64.b64encode(b"inline-shot").decode("ascii"),
                "width": 800,
                "height": 450,
                "method": "offscreen",
            }

    monkeypatch.setattr(_image_transport, "get_blender_connection", Connection)
    monkeypatch.setattr(
        _image_transport,
        "get_last_handshake",
        lambda: types.SimpleNamespace(protocol_version=_image_transport.INLINE_IMAGE_PROTOCOL_VERSION),
    )

    image, envelope = viewport.get_viewport_screenshot(ctx=None, max_size=800)

    command, params = calls[0]
    assert command == "get_viewport_screenshot"
    assert params["inline"] is True
    assert params["max_size"] == 800
    assert "filepath" not in params
    assert image.data == b"inline-shot"
    assert envelope["data"] == {"width": 800, "height": 450, "method": "offscreen"}
