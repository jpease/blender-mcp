# ruff: file-ignore[yoda-conditions]
"""Regression coverage for render, view-layer, and pass tools."""

import asyncio
import importlib
import inspect
import math
import os
import types

from pathlib import Path

import pytest

from mcp.server.fastmcp import Image
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
        rendering.RenderSettingsPatch(unknown=True)  # pyright: ignore[reportCallIssue] - rejection is the assertion


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


def test_inspect_render_output_is_async_and_returns_the_image_with_its_envelope(monkeypatch) -> None:
    """Its socket round-trip and tempfile both block; on the MCP event loop they would stall every other request."""
    assert inspect.iscoroutinefunction(rendering.inspect_render_output)

    connection = _Connection()

    def fake_send_command(command, params):
        connection.calls.append((command, params))
        with open(params["filepath"], "wb") as f:
            f.write(b"fake-png-bytes")
        return {"width": 500, "height": 300, "source": "output_path", "source_path": "/tmp/render.png"}

    connection.send_command = fake_send_command
    monkeypatch.setattr(rendering, "get_blender_connection", lambda: connection)

    items = asyncio.run(rendering.inspect_render_output(ctx=None, output_path="/tmp/render.png", max_size=500))

    command, params = connection.calls[0]
    assert command == "inspect_render_output"
    assert params["output_path"] == "/tmp/render.png"
    assert params["frame"] is None
    assert params["max_size"] == 500
    assert params["format"] == "png"

    image, envelope = items
    assert isinstance(image, Image)
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
        asyncio.run(rendering.inspect_render_output(ctx=None))

    assert not rendered.exists()


class _FakeImages(dict):
    """The three `bpy.data.images` calls inspect_render_output makes on a file read from disk."""

    def load(self, filepath, check_existing=False):
        """Return a loadable stand-in small enough that no downscale is triggered."""
        return types.SimpleNamespace(
            size=(64, 36), filepath_raw=filepath, file_format="PNG", save=lambda: None, scale=lambda *_size: None
        )

    def remove(self, _image):
        """Drop the loaded datablock, as the handler does in its finally block."""


class _FakeScene(types.SimpleNamespace):
    """
    A scene stub that also holds Blender ID custom properties.

    `configure_render_settings` records an authored frame range as `scene[...]` and
    `render_scene` reads it back, so a stub without the mapping protocol would make the
    guard untestable.
    """

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.__dict__["_custom_properties"] = {}

    def get(self, key: str, default: object = None) -> object:
        return self._custom_properties.get(key, default)

    def __getitem__(self, key: str) -> object:
        return self._custom_properties[key]

    def __setitem__(self, key: str, value: object) -> None:
        self._custom_properties[key] = value

    def __contains__(self, key: str) -> bool:
        return key in self._custom_properties

    def __delitem__(self, key: str) -> None:
        del self._custom_properties[key]


def _fake_view_layer(handlers, name="ViewLayer"):
    layer = types.SimpleNamespace(
        name=name,
        material_override=None,
        world_override=None,
        # `_render_pass_info` walks this; an empty list is "no passes reported", which is
        # exactly what a stub can honestly claim.
        bl_rna=types.SimpleNamespace(properties=[]),
    )
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
        file_extension=".png",
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
    return _FakeScene(
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
        eevee=types.SimpleNamespace(
            taa_samples=16,
            taa_render_samples=64,
            use_shadows=True,
            use_raytracing=False,
            ray_tracing_method="SCREEN",
            ray_tracing_options=types.SimpleNamespace(
                resolution_scale="2",
                screen_trace_quality=0.25,
                screen_trace_thickness=0.1,
                trace_max_roughness=0.5,
                use_denoise=True,
            ),
        ),
        view_layers=[_fake_view_layer(handlers)],
    )


def _rendering_handler(monkeypatch):
    addon, fake_bpy = _load_addon(monkeypatch, data={"scenes": {}, "images": _FakeImages()})
    handlers = importlib.import_module(f"{addon.__name__}.handlers.rendering")
    # render_scene resolves "//relative" paths through Blender; outside Blender they are absolute
    # already, so the identity keeps the resolver's own normalisation the only thing under test.
    fake_bpy.path = types.SimpleNamespace(abspath=lambda path: path)
    scene = _fake_scene(handlers)
    fake_bpy.data.scenes["Scene"] = scene
    return handlers.RenderingHandlersMixin(), scene, handlers


def test_configure_render_settings_returns_only_the_patched_values(monkeypatch) -> None:
    handler, scene, _handlers = _rendering_handler(monkeypatch)

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
    handler, _scene, _handlers = _rendering_handler(monkeypatch)

    result = handler.configure_render_settings("Scene", {"engine": "CYCLES", "resolution_y": 720}, detail=True)

    assert result["changed"] == ["engine", "resolution_y"]
    assert result["before"]["engine"] == "BLENDER_EEVEE"
    assert result["before"]["resolution"] == [1920, 1080, 100]
    assert result["after"]["engine"] == "CYCLES"
    assert result["after"]["resolution"] == [1920, 720, 100]
    assert "settings" not in result


def test_configure_render_settings_reports_a_patch_that_writes_nothing(monkeypatch) -> None:
    handler, _scene, _handlers = _rendering_handler(monkeypatch)

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


def test_render_output_path_shapes_that_blender_would_relocate_are_refused(monkeypatch, tmp_path) -> None:
    """Measured on 5.2: Blender appends the frame after the path as given, so these three miss."""
    handler, _scene, _handlers = _rendering_handler(monkeypatch)
    (tmp_path / "renders").mkdir()

    with pytest.raises(ValueError, match="is a directory") as trailing_slash:
        handler.render_scene("Scene", f"{tmp_path / 'renders'}/", mode="ANIMATION", confirm_render=True)
    with pytest.raises(ValueError, match="is a directory"):
        handler.render_scene("Scene", str(tmp_path / "renders"), mode="STILL", confirm_render=True)
    with pytest.raises(ValueError, match="names a single file") as animation_file:
        handler.render_scene("Scene", str(tmp_path / "sh010.png"), mode="ANIMATION", confirm_render=True)
    with pytest.raises(ValueError, match="image-format extension") as still_prefix:
        handler.render_scene("Scene", str(tmp_path / "sh010_"), mode="STILL", confirm_render=True)

    # Each refusal has to name the path that works, or the caller only learns they were wrong.
    assert "/frame_####.png'" in str(trailing_slash.value)
    assert "'sh010.png0001.png'" in str(animation_file.value)
    assert "'sh010_####.png'" in str(animation_file.value)
    assert str(tmp_path / "sh010_.png") in str(still_prefix.value)


def test_render_output_path_shapes_blender_honours_are_resolved(monkeypatch, tmp_path) -> None:
    """A frame prefix, a #### template, and a still's full filename all survive untouched."""
    _handler, scene, handlers = _rendering_handler(monkeypatch)

    for mode, name in (("ANIMATION", "sh010_"), ("ANIMATION", "sh010_####.png"), ("STILL", "sh010.png")):
        assert handlers._resolve_render_output(scene, str(tmp_path / name), mode) == str(tmp_path / name)
    # A prefix ending in digits is honoured too - Blender writes "sh0100001.png", which the reply names.
    assert handlers._resolve_render_output(scene, str(tmp_path / "sh010"), "ANIMATION") == str(tmp_path / "sh010")
    # Without use_file_extension Blender writes the still's path verbatim, so nothing is required of it.
    scene.render.use_file_extension = False
    assert handlers._resolve_render_output(scene, str(tmp_path / "sh010_"), "STILL") == str(tmp_path / "sh010_")


def test_inspect_render_output_reports_the_frame_its_filename_carries(monkeypatch, tmp_path) -> None:
    """Back "here is frame 24" only when Blender's own four-digit frame is in the name."""
    handler, _scene, _handlers = _rendering_handler(monkeypatch)
    destination = tmp_path / "copy.png"

    def inspect(name: str) -> int | None:
        source = tmp_path / name
        source.write_bytes(b"")
        return handler.inspect_render_output(str(destination), output_path=str(source))["frame"]

    assert inspect("sh010_0024.png") == 24
    assert inspect("sh010.png0001.png") == 1
    # "sh010" + frame 1 and a five-digit frame are the same eight characters; neither is a fact.
    assert inspect("sh0100001.png") is None
    assert inspect("hero_shot.png") is None
    assert inspect("sh010_12345.png") is None


def test_eevee_ray_tracing_patch_reaches_the_nested_options_struct(monkeypatch) -> None:
    """Blender 5.2 keeps screen-trace controls on scene.eevee.ray_tracing_options, not scene.eevee."""
    handler, scene, _handlers = _rendering_handler(monkeypatch)

    result = handler.configure_render_settings(
        "Scene",
        {"eevee": {"use_raytracing": True, "ray_tracing": {"resolution_scale": "1", "screen_trace_quality": 0.5}}},
    )

    assert result["after"] == {
        "eevee.use_raytracing": True,
        "eevee.ray_tracing.resolution_scale": "1",
        "eevee.ray_tracing.screen_trace_quality": 0.5,
    }
    assert scene.eevee.use_raytracing is True
    assert scene.eevee.ray_tracing_options.resolution_scale == "1"


def test_a_rejected_ray_tracing_field_restores_the_whole_eevee_patch(monkeypatch) -> None:
    handler, scene, _handlers = _rendering_handler(monkeypatch)

    with pytest.raises(ValueError, match="EEVEE ray tracing settings are unavailable"):
        handler.configure_render_settings(
            "Scene", {"eevee": {"use_raytracing": True, "ray_tracing": {"no_such_option": 1}}}
        )

    assert scene.eevee.use_raytracing is False


def test_render_setup_reports_whether_ray_tracing_is_on(monkeypatch) -> None:
    """A scene whose windows render black says so here; without this the reply never mentions it."""
    handler, _scene, _handlers = _rendering_handler(monkeypatch)

    eevee = handler.inspect_render_setup("Scene")["eevee"]

    assert eevee["use_raytracing"] is False
    assert math.isclose(eevee["ray_tracing"]["screen_trace_quality"], 0.25)


def test_eevee_patch_carries_the_ray_tracing_controls_to_blender(monkeypatch) -> None:
    connection = _Connection()
    monkeypatch.setattr(rendering, "get_blender_connection", lambda: connection)
    patch = rendering.RenderSettingsPatch(
        engine="BLENDER_EEVEE",
        eevee=rendering.EeveePatch(
            use_raytracing=True,
            ray_tracing_method="SCREEN",
            ray_tracing=rendering.EeveeRayTracingPatch(resolution_scale="1", screen_trace_quality=0.5),
        ),
    )

    asyncio.run(rendering.configure_render_settings(ctx=None, scene_name="Scene", patch=patch))

    assert connection.calls[0][1]["patch"]["eevee"] == {
        "use_raytracing": True,
        "ray_tracing_method": "SCREEN",
        "ray_tracing": {"resolution_scale": "1", "screen_trace_quality": 0.5},
    }
    with pytest.raises(ValidationError):
        rendering.EeveePatch.model_validate({"ray_tracing_method": "RAYTRACE"})
    with pytest.raises(ValidationError):
        rendering.EeveePatch.model_validate({"ray_tracing": {"resolution_scale": "3"}})


# ---------------------------------------------------------------------------
# Render intent: the scene owns the range and the output template.
# ---------------------------------------------------------------------------


def _renderable(monkeypatch, tmp_path):
    """
    Build a handler whose stub render operator actually writes the frames it reports.

    Args:
        monkeypatch: The test's monkeypatch.
        tmp_path: The directory `//` paths resolve against, as the open .blend's would.

    Returns:
        tuple: The handler, the scene, and the stub `bpy`.

    """
    addon, fake_bpy = _load_addon(monkeypatch, data={"scenes": {}, "images": _FakeImages()})
    handlers = importlib.import_module(f"{addon.__name__}.handlers.rendering")
    fake_bpy.path = types.SimpleNamespace(
        abspath=lambda path: str(tmp_path / path[2:]) if path.startswith("//") else path
    )
    scene = _fake_scene(handlers)
    scene.frame_current = 1
    scene.frame_set = lambda frame: setattr(scene, "frame_current", frame)
    scene.render.filepath = ""
    scene.render.frame_path = lambda frame=1: f"{scene.render.filepath}{frame:04d}.png"
    fake_bpy.data.scenes["Scene"] = scene

    def render(**_kwargs):
        Path(scene.render.filepath).write_bytes(b"frame")
        return {"FINISHED"}

    fake_bpy.ops.render = types.SimpleNamespace(render=render)
    return handlers.RenderingHandlersMixin(), scene, fake_bpy


def test_render_scene_without_a_filepath_names_the_tool_that_sets_one(monkeypatch, tmp_path) -> None:
    """An empty scene output path is not a render target; the refusal must say where to set one."""
    handler, _scene, _bpy = _renderable(monkeypatch, tmp_path)

    with pytest.raises(ValueError, match="configure_render_settings"):
        handler.render_scene("Scene", confirm_render=True, verify_passes=False)


def test_render_scene_renders_to_the_scenes_own_output_path(monkeypatch, tmp_path) -> None:
    """Intent stored on the scene is intent a later render can use with no arguments."""
    handler, scene, _bpy = _renderable(monkeypatch, tmp_path)
    scene.render.filepath = str(tmp_path / "sh010.png")

    result = handler.render_scene("Scene", confirm_render=True, verify_passes=False)

    assert result["filepath"] == str(tmp_path / "sh010.png")
    assert (tmp_path / "sh010.png").is_file()
    assert result["first_file"] == result["last_file"] == str(tmp_path / "sh010.png")


def test_render_scene_refuses_an_animation_over_blenders_untouched_default_range(monkeypatch, tmp_path) -> None:
    """1-250 is Blender's default, not a decision; rendering it unasked is the defect."""
    handler, scene, _bpy = _renderable(monkeypatch, tmp_path)

    with pytest.raises(ValueError, match="still Blender's default 1-250"):
        handler.render_scene(
            "Scene", str(tmp_path / "sh010_"), mode="ANIMATION", confirm_render=True, verify_passes=False
        )

    scene.frame_end = 2
    handler.render_scene("Scene", str(tmp_path / "sh010_"), mode="ANIMATION", confirm_render=True, verify_passes=False)


def test_render_scene_accepts_the_default_range_when_it_was_chosen(monkeypatch, tmp_path) -> None:
    """Either an explicit confirmation or a range this MCP set clears the guard."""
    handler, scene, _bpy = _renderable(monkeypatch, tmp_path)
    scene.frame_end = 3
    scene.frame_start = 1

    handler.configure_render_settings("Scene", {"frame_end": 250})
    assert scene["blender_mcp_frame_range_authored"] is True
    authored = handler.render_scene(
        "Scene", str(tmp_path / "authored_"), mode="ANIMATION", confirm_render=True, verify_passes=False
    )
    assert authored["frame_count"] == 250

    del scene["blender_mcp_frame_range_authored"]
    confirmed = handler.render_scene(
        "Scene",
        str(tmp_path / "confirmed_"),
        mode="ANIMATION",
        confirm_render=True,
        confirm_frame_range=True,
        verify_passes=False,
    )
    assert confirmed["frame_count"] == 250


def test_render_scene_persists_the_callers_template_not_the_resolved_path(monkeypatch, tmp_path) -> None:
    """Storing the resolved path would replace a portable // template with this machine's layout."""
    handler, scene, _bpy = _renderable(monkeypatch, tmp_path)
    scene.frame_end = 2
    (tmp_path / "renders").mkdir()

    result = handler.render_scene(
        "Scene",
        "//renders/sh010_",
        mode="ANIMATION",
        confirm_render=True,
        persist_output=True,
        verify_passes=False,
    )

    assert scene.render.filepath == "//renders/sh010_"
    assert result["output_persisted"] is True
    assert result["settings_restored"] is False


def test_render_scene_leaves_the_output_path_alone_by_default(monkeypatch, tmp_path) -> None:
    """A render is not a settings change unless the caller asked for one."""
    handler, scene, _bpy = _renderable(monkeypatch, tmp_path)
    scene.frame_end = 2
    scene.render.filepath = "//previous_"

    result = handler.render_scene(
        "Scene", str(tmp_path / "once_"), mode="ANIMATION", confirm_render=True, verify_passes=False
    )

    assert scene.render.filepath == "//previous_"
    assert result["output_persisted"] is False
    assert result["settings_restored"] is True


def test_render_scene_does_not_persist_a_cancelled_render(monkeypatch, tmp_path) -> None:
    """A run that stopped early never proved the template works, so it must not become the default."""
    handler, scene, _bpy = _renderable(monkeypatch, tmp_path)
    scene.frame_end = 4
    scene.render.filepath = "//previous_"

    result = handler.render_scene(
        "Scene",
        str(tmp_path / "stopped_"),
        mode="ANIMATION",
        confirm_render=True,
        persist_output=True,
        max_duration_seconds=0.000001,
        verify_passes=False,
    )

    assert result["cancelled"] is True
    assert result["output_persisted"] is False
    assert scene.render.filepath == "//previous_"


def test_render_scene_refuses_to_persist_a_still_path(monkeypatch, tmp_path) -> None:
    """Blender appends the frame number to a stored path, so a still template breaks the next animation."""
    handler, _scene, _bpy = _renderable(monkeypatch, tmp_path)

    with pytest.raises(ValueError, match="persist_output stores a per-frame template"):
        handler.render_scene(
            "Scene", str(tmp_path / "sh010.png"), mode="STILL", confirm_render=True, persist_output=True
        )


def test_configure_render_settings_refuses_a_directory_as_the_stored_template(monkeypatch, tmp_path) -> None:
    """The same shape `render_scene` refuses must be refused at the moment it is stored."""
    handler, scene, _handlers = _rendering_handler(monkeypatch)
    renders = tmp_path / "renders"
    renders.mkdir()

    with pytest.raises(ValueError, match="is a directory"):
        handler.configure_render_settings("Scene", {"output": {"filepath": f"{renders}/"}})

    assert scene.render.filepath == "/tmp/render/"


def test_render_scene_reply_summarises_and_detail_restores_the_per_frame_arrays(monkeypatch, tmp_path) -> None:
    """Per-frame bookkeeping is what spent 86% of the budget; it is now opt-in."""
    handler, scene, _bpy = _renderable(monkeypatch, tmp_path)
    scene.frame_end = 3

    summary = handler.render_scene(
        "Scene", str(tmp_path / "beat_"), mode="ANIMATION", confirm_render=True, verify_passes=False
    )

    assert "files" not in summary
    assert "progress" not in summary
    assert "progress_truncated" not in summary
    assert summary["first_file"] == str(tmp_path / "beat_0001.png")
    assert summary["last_file"] == str(tmp_path / "beat_0003.png")
    assert summary["bytes_written"] == 3 * len(b"frame")

    detailed = handler.render_scene(
        "Scene",
        str(tmp_path / "beat_"),
        mode="ANIMATION",
        confirm_render=True,
        confirm_overwrite=True,
        verify_passes=False,
        detail=True,
    )

    assert [entry["frame"] for entry in detailed["files"]] == [1, 2, 3]
    assert [entry["completed"] for entry in detailed["progress"]] == [1, 2, 3]
    assert detailed["progress_truncated"] is False
