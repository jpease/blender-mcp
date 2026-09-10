"""
Coverage for the addon-side half of the inline image transport.

This module is deliberately free of `bpy` so it can be loaded straight from
source here, without the mocked-Blender scaffolding the handler tests need.
"""

import base64
import importlib.util
import os

import pytest

from conftest import ROOT_ADDON


def _load_image_reply():
    path = ROOT_ADDON.parent / "image_reply.py"
    spec = importlib.util.spec_from_file_location("addon_image_reply_under_test", path)
    assert spec is not None and spec.loader is not None, f"{path} is not loadable as a module"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_shared_path_destination_is_the_callers_filepath() -> None:
    image_reply = _load_image_reply()

    with image_reply.image_destination(False, "/tmp/caller-chose-this.png") as path:
        assert path == "/tmp/caller-chose-this.png"


def test_shared_path_destination_requires_a_filepath() -> None:
    image_reply = _load_image_reply()

    with pytest.raises(ValueError, match="No filepath provided"), image_reply.image_destination(False, None):
        pass


def test_shared_path_destination_is_left_on_disk_for_the_caller_to_read(tmp_path) -> None:
    image_reply = _load_image_reply()
    destination = tmp_path / "keep-me.png"

    with image_reply.image_destination(False, str(destination)) as path, open(path, "wb") as handle:
        handle.write(b"png")

    assert destination.exists()


def test_inline_destination_ignores_the_callers_filepath_and_is_cleaned_up() -> None:
    image_reply = _load_image_reply()

    with image_reply.image_destination(True, "/nonexistent/host/path.png") as path:
        assert path != "/nonexistent/host/path.png"
        with open(path, "wb") as handle:
            handle.write(b"png")
        assert os.path.exists(path)

    assert not os.path.exists(path)


def test_inline_destination_is_cleaned_up_when_the_handler_raises() -> None:
    image_reply = _load_image_reply()
    captured = {}

    with pytest.raises(RuntimeError, match="capture failed"), image_reply.image_destination(True, None) as path:
        captured["path"] = path
        with open(path, "wb") as handle:
            handle.write(b"png")
        raise RuntimeError("capture failed")

    assert not os.path.exists(captured["path"])


def test_finalize_inline_reply_carries_the_bytes_and_drops_the_local_path(tmp_path) -> None:
    image_reply = _load_image_reply()
    written = tmp_path / "shot.png"
    written.write_bytes(b"png-bytes")

    reply = image_reply.finalize_image_reply(
        {"success": True, "width": 640, "filepath": str(written)}, inline=True, path=str(written)
    )

    assert reply["image_base64"] == base64.b64encode(b"png-bytes").decode("ascii")
    assert "filepath" not in reply
    assert reply["width"] == 640


def test_finalize_shared_path_reply_is_unchanged(tmp_path) -> None:
    image_reply = _load_image_reply()
    written = tmp_path / "shot.png"
    written.write_bytes(b"png-bytes")

    reply = image_reply.finalize_image_reply(
        {"success": True, "filepath": str(written)}, inline=False, path=str(written)
    )

    assert reply == {"success": True, "filepath": str(written)}
