# ruff: file-ignore[import-private-name, missing-return-type-private-function, missing-type-function-argument, undocumented-public-function, yoda-conditions]
"""Regression coverage for render, view-layer, and pass tools."""

import asyncio
import importlib
import os
import types

import pytest

from mcp.server.fastmcp.exceptions import ToolError
from pydantic import ValidationError
from test_mutation_transaction import _load_addon

from blender_mcp.server.tools import rendering

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


def test_render_settings_patch_accepts_the_blender_5_eevee_engine_name() -> None:
    patch = rendering.RenderSettingsPatch(engine="BLENDER_EEVEE", eevee=rendering.EeveePatch(taa_render_samples=16))
    assert patch.engine == "BLENDER_EEVEE"
    with pytest.raises(ValidationError):
        rendering.RenderSettingsPatch.model_validate({"engine": "BLENDER_EEVEE_NEXT"})


def test_addon_render_validation_accepts_the_blender_5_eevee_engine_name(monkeypatch) -> None:
    addon, _bpy = _load_addon(monkeypatch, data={})
    handlers = importlib.import_module(f"{addon.__name__}.handlers.rendering")

    assert handlers._validate_render_patch({"engine": "BLENDER_EEVEE"}) == {"engine": "BLENDER_EEVEE"}
    with pytest.raises(ValueError, match="Unsupported engine"):
        handlers._validate_render_patch({"engine": "BLENDER_EEVEE_NEXT"})


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
    monkeypatch.setattr(rendering, "get_blender_connection", lambda: connection)

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

    monkeypatch.setattr(rendering, "get_blender_connection", _FailingConnection)
    monkeypatch.setattr(rendering.tempfile, "mkstemp", fake_mkstemp)

    with pytest.raises(Exception, match="Render output inspection failed"):
        rendering.inspect_render_output(ctx=None)

    assert not rendered.exists()


def _fake_view_layer(handlers, name="ViewLayer"):
    layer = types.SimpleNamespace(name=name, material_override=None, world_override=None)
    for prop in handlers._VIEW_LAYER_PROPERTIES:
        setattr(layer, prop, 8 if prop == "pass_cryptomatte_depth" else True)
    return layer


def _fake_scene(handlers, name="Scene"):
    image_settings = types.SimpleNamespace(
        file_format="PNG",
        color_mode="RGBA",
        color_depth="8",
        compression=15,
        quality=90,
        exr_codec="ZIP",
        views_format="INDIVIDUAL",
        stereo_3d_format=types.SimpleNamespace(display_mode="ANAGLYPH"),
    )
    render = types.SimpleNamespace(
        engine="BLENDER_EEVEE",
        resolution_x=1920,
        resolution_y=1080,
        resolution_percentage=100,
        pixel_aspect_x=1.0,
        pixel_aspect_y=1.0,
        fps=24,
        fps_base=1.0,
        film_transparent=False,
        filepath="/tmp/render/",
        use_file_extension=True,
        use_overwrite=True,
        use_placeholder=False,
        use_motion_blur=False,
        motion_blur_shutter=0.5,
        motion_blur_position="CENTER",
        use_multiview=False,
        use_stamp=False,
        stamp_note_text="",
        image_settings=image_settings,
    )
    return types.SimpleNamespace(
        name=name,
        camera=None,
        frame_start=1,
        frame_end=250,
        frame_step=1,
        use_nodes=False,
        node_tree=None,
        compositing_node_group=None,
        render=render,
        cycles=types.SimpleNamespace(
            samples=128,
            use_denoising=True,
            film_transparent_glass=False,
            film_transparent_roughness=0.1,
        ),
        eevee=types.SimpleNamespace(taa_samples=16, taa_render_samples=64, use_shadows=True),
        view_layers=[_fake_view_layer(handlers)],
    )


def _rendering_handler(monkeypatch):
    addon, fake_bpy = _load_addon(monkeypatch, data={"scenes": {}})
    handlers = importlib.import_module(f"{addon.__name__}.handlers.rendering")
    scene = _fake_scene(handlers)
    fake_bpy.data.scenes["Scene"] = scene
    return handlers.RenderingHandlersMixin(), scene


def test_configure_render_settings_returns_only_the_patched_values(monkeypatch) -> None:
    handler, scene = _rendering_handler(monkeypatch)

    result = handler.configure_render_settings(
        "Scene",
        {
            "engine": "CYCLES",
            "resolution_x": 1280,
            "output": {"image_format": "OPEN_EXR", "compression": 30},
            "motion_blur": {"enabled": True, "shutter": 0.25},
        },
    )

    assert result == {
        "scene": "Scene",
        "changed": [
            "engine",
            "motion_blur.enabled",
            "motion_blur.shutter",
            "output.compression",
            "output.image_format",
            "resolution_x",
        ],
        "after": {
            "engine": "CYCLES",
            "resolution_x": 1280,
            "output.image_format": "OPEN_EXR",
            "output.compression": 30,
            "motion_blur.enabled": True,
            "motion_blur.shutter": 0.25,
        },
        "changed_resources": ["Scene"],
    }
    assert scene.render.image_settings.file_format == "OPEN_EXR"
    assert scene.render.use_motion_blur is True


def test_configure_render_settings_detail_returns_both_full_state_blocks(monkeypatch) -> None:
    handler, _scene = _rendering_handler(monkeypatch)

    result = handler.configure_render_settings("Scene", {"engine": "CYCLES", "resolution_y": 720}, detail=True)

    assert result["changed"] == ["engine", "resolution_y"]
    assert result["before"]["engine"] == "BLENDER_EEVEE"
    assert result["before"]["resolution"] == [1920, 1080, 100]
    assert result["after"]["engine"] == "CYCLES"
    assert result["after"]["resolution"] == [1920, 720, 100]
    assert "settings" not in result


def test_configure_render_settings_reports_a_patch_that_writes_nothing(monkeypatch) -> None:
    handler, _scene = _rendering_handler(monkeypatch)

    result = handler.configure_render_settings("Scene", {"cycles": {}})

    assert result == {"scene": "Scene", "changed": [], "after": {}, "changed_resources": ["Scene"]}


def test_configure_render_settings_forwards_detail(monkeypatch) -> None:
    connection = _Connection()
    monkeypatch.setattr(rendering, "get_blender_connection", lambda: connection)

    asyncio.run(
        rendering.configure_render_settings(
            ctx=None,
            scene_name="Scene",
            patch=rendering.RenderSettingsPatch(engine="CYCLES"),
            detail=True,
        )
    )
    asyncio.run(
        rendering.configure_render_settings(
            ctx=None, scene_name="Scene", patch=rendering.RenderSettingsPatch(engine="CYCLES")
        )
    )

    assert [params["detail"] for _command, params in connection.calls] == [True, False]
