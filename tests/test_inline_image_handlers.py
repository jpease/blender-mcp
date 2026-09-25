"""
Coverage for the addon image handlers answering with inline bytes.

These exercise the handlers themselves (through the mocked-Blender loader) to
prove the `inline` flag reaches the reply, rather than only testing the helper
the handlers delegate to.
"""

import base64
import sys
import types

from conftest import load_addon


class _FakeImage:
    """A bpy image that actually writes its bytes when saved."""

    def __init__(self, size=(200, 100), payload=b"rendered-bytes"):
        self.size = size
        self.filepath_raw = ""
        self.file_format = None
        self._payload = payload
        self.scaled_to = None

    def scale(self, width, height):
        self.scaled_to = (width, height)
        self.size = (width, height)

    def save(self):
        with open(self.filepath_raw, "wb") as handle:
            handle.write(self._payload)


class _FakeImages:
    def __init__(self, image):
        self._image = image
        self.removed = []

    def load(self, _path, check_existing=False):
        return self._image

    def remove(self, image):
        self.removed.append(image)


def _rendering_handler(monkeypatch, image):
    addon, bpy = load_addon(monkeypatch, data={"images": _FakeImages(image)})
    # main's _resolved_path runs every output_path through bpy.path.abspath; tmp_path is
    # absolute already, so the identity keeps this test about the transport.
    bpy.path = types.SimpleNamespace(abspath=lambda path: path)
    bpy.context.scene.frame_current = 7
    module = sys.modules[f"{addon.__name__}.handlers.rendering"]
    handler = module.RenderingHandlersMixin()
    return handler


def test_inline_render_inspection_returns_bytes_and_hides_the_local_path(monkeypatch, tmp_path) -> None:
    rendered = tmp_path / "frame.png"
    rendered.write_bytes(b"on-disk-render")
    handler = _rendering_handler(monkeypatch, _FakeImage(payload=b"rendered-bytes"))

    reply = handler.inspect_render_output(filepath=None, output_path=str(rendered), inline=True)

    assert reply["image_base64"] == base64.b64encode(b"rendered-bytes").decode("ascii")
    assert "filepath" not in reply
    assert reply["source"] == "output_path"
    assert reply["native_width"] == 200


def test_shared_path_render_inspection_still_writes_the_requested_file(monkeypatch, tmp_path) -> None:
    rendered = tmp_path / "frame.png"
    rendered.write_bytes(b"on-disk-render")
    destination = tmp_path / "copy.png"
    handler = _rendering_handler(monkeypatch, _FakeImage(payload=b"rendered-bytes"))

    reply = handler.inspect_render_output(filepath=str(destination), output_path=str(rendered))

    assert reply["filepath"] == str(destination)
    assert "image_base64" not in reply
    assert destination.read_bytes() == b"rendered-bytes"


def _viewport_handler(monkeypatch, image):
    """
    Load the viewport handler with a 3D area available.

    `gpu` is not importable outside Blender, so the offscreen capture path
    raises and the handler falls back to the window grab - which is the path
    these tests drive.
    """
    addon, bpy = load_addon(monkeypatch, data={"images": _FakeImages(image)})

    region = types.SimpleNamespace(type="WINDOW", width=400, height=200)
    space = types.SimpleNamespace(
        # main's _capture_view reads the live view/projection matrices off region_3d and takes
        # the shading type through _shading_override, both before anything is written.
        region_3d=types.SimpleNamespace(view_matrix=object(), window_matrix=object()),
        shading=types.SimpleNamespace(type="SOLID"),
    )
    area = types.SimpleNamespace(type="VIEW_3D", regions=[region], spaces=types.SimpleNamespace(active=space))
    bpy.context.screen = types.SimpleNamespace(areas=[area])
    bpy.context.temp_override = lambda **_kwargs: _NullContext()

    captured = {}

    def screenshot_area(filepath=""):
        captured["filepath"] = filepath
        with open(filepath, "wb") as handle:
            handle.write(b"grabbed")

    bpy.ops.screen = types.SimpleNamespace(screenshot_area=screenshot_area)
    module = sys.modules[f"{addon.__name__}.handlers.viewport"]
    return module.ViewportHandlersMixin(), captured


class _NullContext:
    def __enter__(self):
        return None

    def __exit__(self, *_exc):
        return False


def test_inline_screenshot_returns_bytes_and_hides_the_local_path(monkeypatch) -> None:
    handler, captured = _viewport_handler(monkeypatch, _FakeImage(size=(400, 200), payload=b"grabbed"))

    reply = handler.get_viewport_screenshot(max_size=800, inline=True)

    assert reply["image_base64"] == base64.b64encode(b"grabbed").decode("ascii")
    assert "filepath" not in reply
    assert captured["filepath"].endswith(".png")


def test_screenshot_without_a_filepath_is_still_rejected_when_not_inline(monkeypatch) -> None:
    handler, _captured = _viewport_handler(monkeypatch, _FakeImage())

    reply = handler.get_viewport_screenshot(max_size=800)

    assert "No filepath provided" in reply["error"]
