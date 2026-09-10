# ruff: file-ignore[import-private-name, missing-return-type-private-function, missing-type-function-argument, undocumented-public-function, yoda-conditions]
"""Regression coverage for render, view-layer, and pass tools."""

import asyncio
import os

import pytest

from mcp.server.fastmcp.exceptions import ToolError
from pydantic import ValidationError
from test_mutation_transaction import _load_addon

from blender_mcp.server.tools import _image_transport, rendering

RENDER_COMMANDS = {
    "inspect_render_setup",
    "configure_render_settings",
    "manage_view_layers",
    "render_scene",
    "inspect_render_output",
}


class _Connection:
    def __init__(self) -> None:
        self.calls = []

    def send_command(self, command, params):
        self.calls.append((command, params))
        return {"changed_resources": [params.get("scene_name", "Scene")]}


def test_render_tools_are_registered_and_dispatched(monkeypatch) -> None:
    addon, _bpy = _load_addon(monkeypatch, data={})
    server = addon.BlenderMCPServer()

    assert RENDER_COMMANDS <= set(rendering.mcp._tool_manager._tools)
    assert RENDER_COMMANDS <= set(server._build_command_handlers())
    assert "inspect_render_setup" in server._READ_ONLY_COMMANDS
    assert "inspect_render_output" in server._READ_ONLY_COMMANDS
    assert "configure_render_settings" not in server._READ_ONLY_COMMANDS
    assert "manage_view_layers" not in server._READ_ONLY_COMMANDS


def test_render_settings_patch_is_strict_and_bounded() -> None:
    with pytest.raises(ValidationError, match="at least one field"):
        rendering.RenderSettingsPatch()
    with pytest.raises(ValidationError):
        rendering.RenderSettingsPatch(resolution_x=1)
    with pytest.raises(ValidationError):
        rendering.RenderSettingsPatch(frame_start=20, frame_end=10)
    with pytest.raises(ValidationError):
        rendering.RenderSettingsPatch(unknown=True)


def test_view_layer_patch_is_strict_and_cryptomatte_depth_is_even() -> None:
    with pytest.raises(ValidationError, match="at least one field"):
        rendering.ViewLayerPatch()
    with pytest.raises(ValidationError):
        rendering.ViewLayerPatch(pass_cryptomatte_depth=3)
    patch = rendering.ViewLayerPatch(use_pass_position=True, pass_cryptomatte_depth=8)
    assert patch.use_pass_position is True


def test_configure_render_settings_serializes_patch(monkeypatch) -> None:
    connection = _Connection()
    monkeypatch.setattr(rendering, "get_blender_connection", lambda: connection)

    result = asyncio.run(
        rendering.configure_render_settings(
            ctx=None,
            scene_name="Scene",
            patch=rendering.RenderSettingsPatch(
                engine="CYCLES",
                cycles_samples=64,
                cycles_use_denoising=True,
                compression=25,
            ),
        )
    )

    command, params = connection.calls[0]
    assert command == "configure_render_settings"
    assert params["patch"] == {
        "engine": "CYCLES",
        "compression": 25,
        "cycles_samples": 64,
        "cycles_use_denoising": True,
    }
    assert result["changed_resources"] == ["Scene"]


def test_render_settings_nested_engine_and_output_patches_serialize(monkeypatch) -> None:
    connection = _Connection()
    monkeypatch.setattr(rendering, "get_blender_connection", lambda: connection)
    patch = rendering.RenderSettingsPatch(
        engine="CYCLES",
        cycles=rendering.CyclesPatch(samples=128, use_adaptive_sampling=True),
        output=rendering.OutputPatch(image_format="OPEN_EXR_MULTILAYER", color_depth="32"),
        motion_blur=rendering.MotionBlurPatch(enabled=True, shutter=0.5),
    )

    asyncio.run(rendering.configure_render_settings(ctx=None, scene_name="Scene", patch=patch))

    payload = connection.calls[0][1]["patch"]
    assert payload["cycles"] == {"samples": 128, "use_adaptive_sampling": True}
    assert payload["output"]["image_format"] == "OPEN_EXR_MULTILAYER"


def test_render_inspection_serializes_bounded_graph_request(monkeypatch) -> None:
    connection = _Connection()
    monkeypatch.setattr(rendering, "get_blender_connection", lambda: connection)

    asyncio.run(
        rendering.inspect_render_setup(
            ctx=None, scene_name="Scene", graph_sections=["NODES", "DEPENDENCIES"], limit=25, offset=50
        )
    )

    assert connection.calls[0] == (
        "inspect_render_setup",
        {"scene_name": "Scene", "graph_sections": ["NODES", "DEPENDENCIES"], "limit": 25, "offset": 50},
    )


def test_view_layer_and_render_confirmation_rules(monkeypatch) -> None:
    connection = _Connection()
    monkeypatch.setattr(rendering, "get_blender_connection", lambda: connection)

    with pytest.raises(ToolError, match="PATCH requires"):
        asyncio.run(
            rendering.manage_view_layers(
                ctx=None,
                scene_name="Scene",
                action="PATCH",
                view_layer_name="Main",
            )
        )
    with pytest.raises(ToolError, match="does not accept"):
        asyncio.run(
            rendering.manage_view_layers(
                ctx=None,
                scene_name="Scene",
                action="REMOVE",
                view_layer_name="Main",
                patch=rendering.ViewLayerPatch(use=False),
                confirm_remove=True,
            )
        )
    with pytest.raises(ToolError, match="confirm_render"):
        asyncio.run(rendering.render_scene(ctx=None, scene_name="Scene", filepath="/tmp/output.png"))
    assert connection.calls == []


def test_render_output_metadata_reports_all_fields() -> None:
    result = {
        "width": 800,
        "height": 450,
        "native_width": 1920,
        "native_height": 1080,
        "source": "output_path",
        "source_path": "/tmp/render.png",
        "frame": 12,
    }

    assert rendering._render_output_metadata(result) == result


def test_render_output_metadata_defaults_missing_fields_to_none() -> None:
    assert rendering._render_output_metadata({}) == {
        "width": None,
        "height": None,
        "native_width": None,
        "native_height": None,
        "source": None,
        "source_path": None,
        "frame": None,
    }


def test_inspect_render_output_serializes_request_and_returns_image(monkeypatch) -> None:
    connection = _Connection()

    def fake_send_command(command, params):
        connection.calls.append((command, params))
        with open(params["filepath"], "wb") as f:
            f.write(b"fake-png-bytes")
        return {"width": 500, "height": 300, "source": "output_path", "source_path": "/tmp/render.png"}

    connection.send_command = fake_send_command
    monkeypatch.setattr(_image_transport, "get_blender_connection", lambda: connection)

    items = rendering.inspect_render_output(ctx=None, output_path="/tmp/render.png", max_size=500)

    command, params = connection.calls[0]
    assert command == "inspect_render_output"
    assert params["output_path"] == "/tmp/render.png"
    assert params["frame"] is None
    assert params["max_size"] == 500
    assert params["format"] == "png"

    image, envelope = items
    assert image.data == b"fake-png-bytes"
    assert envelope["data"]["source"] == "output_path"


def test_inspect_render_output_tempfile_is_removed_when_blender_fails(monkeypatch, tmp_path) -> None:
    rendered = tmp_path / "request.png"

    class _FailingConnection:
        def send_command(self, *_args, **_kwargs):
            raise RuntimeError("inspection failed")

    def fake_mkstemp(**_kwargs):
        descriptor = os.open(rendered, os.O_CREAT | os.O_RDWR)
        return descriptor, str(rendered)

    monkeypatch.setattr(_image_transport, "get_blender_connection", _FailingConnection)
    monkeypatch.setattr(_image_transport.tempfile, "mkstemp", fake_mkstemp)

    with pytest.raises(Exception, match="Render output inspection failed"):
        rendering.inspect_render_output(ctx=None)

    assert not rendered.exists()


def test_inspect_render_output_uses_the_inline_transport_when_the_addon_supports_it(monkeypatch) -> None:
    import base64
    import types

    from blender_mcp.server.tools import _image_transport

    calls = []

    class Connection:
        def send_command(self, command, params):
            calls.append((command, params))
            return {
                "image_base64": base64.b64encode(b"inline-render").decode("ascii"),
                "width": 500,
                "height": 300,
                "source": "output_path",
                "source_path": "/renders/frame.png",
            }

    monkeypatch.setattr(_image_transport, "get_blender_connection", Connection)
    monkeypatch.setattr(
        _image_transport,
        "get_last_handshake",
        lambda: types.SimpleNamespace(protocol_version=_image_transport.INLINE_IMAGE_PROTOCOL_VERSION),
    )

    image, envelope = rendering.inspect_render_output(ctx=None, output_path="/renders/frame.png", max_size=500)

    command, params = calls[0]
    assert command == "inspect_render_output"
    assert params["inline"] is True
    assert params["output_path"] == "/renders/frame.png"
    assert "filepath" not in params
    assert image.data == b"inline-render"
    assert envelope["data"]["source"] == "output_path"
