# ruff: file-ignore[yoda-conditions]
"""Regression coverage for render, view-layer, and pass tools."""

import asyncio
import base64
import importlib
import inspect
import math
import os
import threading
import types

from pathlib import Path

import pytest

from mcp.server.fastmcp import Image
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import ValidationError
from test_mutation_transaction import _load_addon

from blender_mcp.server.tools import _dispatch, _image_transport, rendering

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
    assert server.command_spec("inspect_render_setup").read_only
    assert server.command_spec("inspect_render_output").read_only
    assert not server.command_spec("configure_render_settings").read_only
    assert not server.command_spec("manage_view_layers").read_only


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
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

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
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)
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
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

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
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

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


def test_render_result_origin_warning_reaches_the_envelope_not_the_data(monkeypatch) -> None:
    """An agent reads warnings off the envelope; left in data it would be one it never checks."""
    notice = "Render Result's origin is unknown: render_scene did not render what it holds."

    def fake_send_command(_command, params):
        with open(params["filepath"], "wb") as f:
            f.write(b"fake-png-bytes")
        return {"source": "render_result", "source_path": None, "scene": None, "frame": None, "warnings": [notice]}

    connection = _Connection()
    connection.send_command = fake_send_command
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)
    monkeypatch.setattr(_image_transport, "get_last_handshake", lambda: None)

    _image, envelope = asyncio.run(rendering.inspect_render_output(ctx=None))

    assert envelope["warnings"] == [notice]
    assert "warnings" not in envelope["data"]
    assert (envelope["data"]["scene"], envelope["data"]["frame"]) == (None, None)


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
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)
    # No handshake means no inline support, which is the shared-path transport this covers;
    # pinned rather than left to whichever test last cached one.
    monkeypatch.setattr(_image_transport, "get_last_handshake", lambda: None)

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

    monkeypatch.setattr(_dispatch, "get_blender_connection", _FailingConnection)
    monkeypatch.setattr(_image_transport, "get_last_handshake", lambda: None)
    monkeypatch.setattr(_image_transport.tempfile, "mkstemp", fake_mkstemp)

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
        use_persistent_data=False,
        use_simplify=False,
        simplify_subdivision_render=6,
        filter_size=1.5,
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
            device="CPU",
            pixel_filter_type="BLACKMAN_HARRIS",
            filter_width=1.5,
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
        view_settings=types.SimpleNamespace(view_transform="AgX", look="None", exposure=0.0, gamma=1.0),
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
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

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


def _no_gpu_backend():
    """Preferences on a machine whose Cycles add-on selects no compute backend."""
    return types.SimpleNamespace(
        addons={"cycles": types.SimpleNamespace(preferences=types.SimpleNamespace(compute_device_type="NONE"))}
    )


def test_performance_and_cycles_filter_patches_reach_their_owners(monkeypatch) -> None:
    handler, scene, _handlers = _rendering_handler(monkeypatch)

    # Render cost is engine-independent: no engine guard, unlike the cycles section.
    handler.configure_render_settings("Scene", {"performance": {"use_simplify": True}})
    result = handler.configure_render_settings(
        "Scene",
        {
            "engine": "CYCLES",
            "performance": {"use_persistent_data": True, "filter_size": 2.0},
            "cycles": {"pixel_filter_type": "GAUSSIAN", "filter_width": 2.5},
        },
    )

    assert result["after"] == {
        "engine": "CYCLES",
        "performance.use_persistent_data": True,
        "performance.filter_size": 2.0,
        "cycles.pixel_filter_type": "GAUSSIAN",
        "cycles.filter_width": 2.5,
    }
    assert scene.render.use_simplify is True
    assert scene.render.use_persistent_data is True
    assert scene.render.filter_size == pytest.approx(2.0)
    assert scene.cycles.filter_width == pytest.approx(2.5)


def test_performance_and_cycles_patch_fields_hold_blender_5_2_identifiers_and_ranges() -> None:
    """Measured on 5.2.2: an unknown enum identifier would reach setattr and raise a bare TypeError."""
    patch = rendering.RenderSettingsPatch(
        performance=rendering.PerformancePatch(use_persistent_data=True, simplify_volumes=0.5),
        cycles=rendering.CyclesPatch(denoising_prefilter="FAST", filter_width=0.01),
    )

    assert patch.model_dump(exclude_none=True) == {
        "performance": {"use_persistent_data": True, "simplify_volumes": 0.5},
        "cycles": {"denoising_prefilter": "FAST", "filter_width": 0.01},
    }
    for invalid in ({"simplify_volumes": 1.5}, {"simplify_subdivision": -1}, {"filter_size": 501}):
        with pytest.raises(ValidationError):
            rendering.PerformancePatch.model_validate(invalid)
    for invalid in (
        {"pixel_filter_type": "TENT"},
        {"denoising_quality": "LOW"},
        {"denoising_input_passes": "ALBEDO"},
        {"filter_width": 0},
    ):
        with pytest.raises(ValidationError):
            rendering.CyclesPatch.model_validate(invalid)


def test_inspect_render_setup_reports_performance_and_the_device_cycles_will_use(monkeypatch) -> None:
    handler, scene, handlers = _rendering_handler(monkeypatch)
    handlers.bpy.context.preferences = _no_gpu_backend()

    eevee = handler.inspect_render_setup("Scene")

    assert eevee["effective_cycles_device"] is None
    assert "warnings" not in eevee
    assert eevee["performance"] == {
        "use_persistent_data": False,
        "use_simplify": False,
        "simplify_subdivision_render": 6,
        "filter_size": 1.5,
    }
    assert eevee["cycles"]["pixel_filter_type"] == "BLACKMAN_HARRIS"

    scene.render.engine = "CYCLES"
    scene.cycles.device = "GPU"
    cycles = handler.inspect_render_setup("Scene")

    assert cycles["cycles"]["device"] == "GPU", "the request is still reported as the request"
    assert cycles["effective_cycles_device"] == "CPU"
    assert len(cycles["warnings"]) == 1
    assert "renders on the CPU" in cycles["warnings"][0]


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


def test_inspect_render_output_reads_back_the_tilde_path_a_render_was_written_to(monkeypatch, tmp_path) -> None:
    """render_scene expands `~` when it writes, so the same text must name the same file when read."""
    handler, _scene, _handlers = _rendering_handler(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    rendered = tmp_path / "renders" / "sh010_0007.png"
    rendered.parent.mkdir()
    rendered.write_bytes(b"")

    result = handler.inspect_render_output(str(tmp_path / "copy.png"), output_path="~/renders/sh010_0007.png")

    assert result["source_path"] == str(rendered)
    assert result["frame"] == 7

    with pytest.raises(ValueError) as missing:
        handler.inspect_render_output(str(tmp_path / "copy.png"), output_path="~/renders/absent.png")

    # The path the caller can go and look at, not the "~" text that names nothing on disk.
    assert str(tmp_path / "renders" / "absent.png") in str(missing.value)


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
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)
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


def test_render_scene_reports_the_engine_and_the_device_cycles_actually_used(monkeypatch, tmp_path) -> None:
    """A GPU request on a machine with no GPU backend renders on the CPU, and the reply says so."""
    handler, scene, fake_bpy = _renderable(monkeypatch, tmp_path)
    fake_bpy.context.preferences = _no_gpu_backend()
    scene.render.engine = "CYCLES"
    scene.cycles.device = "GPU"

    summary = handler.render_scene("Scene", str(tmp_path / "gpu.png"), confirm_render=True, verify_passes=False)

    assert summary["engine"] == "CYCLES"
    assert summary["effective_cycles_device"] == "CPU"
    assert len(summary["warnings"]) == 1
    assert "renders on the CPU" in summary["warnings"][0]


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


# ---------------------------------------------------------------------------
# plan_render_animation: read-only, shares validation with render_scene's own
# ANIMATION branch, and must agree with it on every frame's output path.
# ---------------------------------------------------------------------------


def test_plan_render_animation_matches_render_scenes_own_frame_paths(monkeypatch, tmp_path) -> None:
    handler, scene, _bpy = _renderable(monkeypatch, tmp_path)
    scene.frame_end = 3

    plan = handler.plan_render_animation("Scene", str(tmp_path / "beat_"))

    assert plan["requested_filepath"] == str(tmp_path / "beat_")
    assert plan["output"] == str(tmp_path / "beat_")
    assert plan["frame_current"] == 1
    assert [f["frame"] for f in plan["frames"]] == [1, 2, 3]
    assert [f["path"] for f in plan["frames"]] == [
        f"{tmp_path / 'beat_'}0001.png",
        f"{tmp_path / 'beat_'}0002.png",
        f"{tmp_path / 'beat_'}0003.png",
    ]
    # Read-only: scene.render.filepath is unchanged after planning.
    assert not scene.render.filepath


def test_plan_render_animation_refuses_the_untouched_default_range(monkeypatch, tmp_path) -> None:
    handler, _scene, _bpy = _renderable(monkeypatch, tmp_path)

    with pytest.raises(ValueError, match="still Blender's default 1-250"):
        handler.plan_render_animation("Scene", str(tmp_path / "sh010_"))


def test_plan_render_animation_and_render_scene_agree_on_every_frame_path(monkeypatch, tmp_path) -> None:
    """The exact regression this extraction exists to prevent: two implementations drifting apart."""
    handler, scene, _bpy = _renderable(monkeypatch, tmp_path)
    scene.frame_end = 3

    plan = handler.plan_render_animation("Scene", str(tmp_path / "beat_"))
    legacy = handler.render_scene(
        "Scene", str(tmp_path / "beat_"), mode="ANIMATION", confirm_render=True, verify_passes=False, detail=True
    )

    assert [f["path"] for f in plan["frames"]] == [entry["path"] for entry in legacy["files"]]


def test_plan_render_animation_is_registered_and_read_only(monkeypatch) -> None:
    addon, _bpy = _load_addon(monkeypatch, data={})
    server = addon.BlenderMCPServer()

    assert "plan_render_animation" in server._build_command_handlers()
    assert server.command_spec("plan_render_animation").read_only


# ---------------------------------------------------------------------------
# Server-orchestrated ANIMATION: per-frame STILL calls, aggregated to the same
# summary shape a single call returns.
# ---------------------------------------------------------------------------


def _fake_still_reply(frame, path, *, bytes_written=5, render_slot_policy="USE_ACTIVE", created_directory=False):
    """One render_scene(mode="STILL", detail=True) reply, shaped exactly as the addon returns it."""
    return {
        "scene": "Scene",
        "mode": "STILL",
        "engine": "CYCLES",
        "effective_cycles_device": "GPU",
        "filepath": path,
        "frame": frame,
        "frame_count": 1,
        "operator_result": ["FINISHED"],
        "settings_restored": True,
        "status": "COMPLETED",
        "cancelled": False,
        "cancellation_reason": None,
        "duration_seconds": 0.01,
        "render_slot_policy": render_slot_policy,
        "output_persisted": False,
        "first_file": path,
        "last_file": path,
        "bytes_written": bytes_written,
        "passes": ["Combined"],
        "pass_verification": "RENDERED_ONLY",
        "created_directory": created_directory,
        "files": [{"frame": frame, "path": path, "bytes": bytes_written}],
        "progress": [{"frame": frame, "completed": 1, "total": 1, "fraction": 1.0}],
        "progress_truncated": False,
    }


def _animation_plan(frame_count=3):
    """One plan_render_animation reply, as the orchestrator receives it."""
    return {
        "requested_filepath": "//renders/beat_",
        "output": "/tmp/beat_",
        "frame_current": 1,
        "frames": [{"frame": f, "path": f"/tmp/beat_{f:04d}.png"} for f in range(1, frame_count + 1)],
    }


def _animation_outcome(
    *,
    verify_passes=False,
    detail=False,
    cancelled=False,
    cancellation_reason=None,
    duration_seconds=0.05,
    persisted=False,
):
    """Build the request/outcome pair the aggregator reads everything but the plan and replies from."""
    request = rendering._RenderRequest(
        scene_name="Scene",
        filepath="//renders/beat_",
        mode="ANIMATION",
        view_layer_name=None,
        frame=None,
        max_animation_frames=250,
        confirm_render=True,
        confirm_overwrite=False,
        confirm_frame_range=False,
        render_slot_policy="NEW_SLOT",
        verify_outputs=True,
        verify_passes=verify_passes,
        max_duration_seconds=None,
        persist_output=False,
        detail=detail,
        create_directories=False,
    )
    return rendering._AnimationOutcome(
        request=request,
        cancelled=cancelled,
        cancellation_reason=cancellation_reason,
        duration_seconds=duration_seconds,
        persisted=persisted,
    )


def test_aggregate_animation_summary_combines_per_frame_replies(monkeypatch) -> None:
    replies = [
        # Only the first frame can find the directory missing, so its report is the run's.
        _fake_still_reply(1, "/tmp/beat_0001.png", render_slot_policy="NEW_SLOT", created_directory=True),
        _fake_still_reply(2, "/tmp/beat_0002.png"),
        _fake_still_reply(3, "/tmp/beat_0003.png"),
    ]

    summary = rendering._aggregate_animation_summary(
        _animation_plan(3), replies, _animation_outcome(verify_passes=True)
    )

    assert summary == {
        "scene": "Scene",
        "mode": "ANIMATION",
        "engine": "CYCLES",
        "effective_cycles_device": "GPU",
        "filepath": "/tmp/beat_",
        "frame": 1,
        "frame_count": 3,
        "operator_result": ["FINISHED"],
        "settings_restored": True,
        "status": "COMPLETED",
        "cancelled": False,
        "cancellation_reason": None,
        "duration_seconds": 0.05,
        "render_slot_policy": "NEW_SLOT",
        "output_persisted": False,
        "first_file": "/tmp/beat_0001.png",
        "last_file": "/tmp/beat_0003.png",
        "bytes_written": 15,
        "passes": ["Combined"],
        "pass_verification": "RENDERED_ONLY",
        "created_directory": True,
    }


def test_aggregate_animation_summary_reports_the_last_frames_device_and_warnings() -> None:
    """The single-call path reads its device once, after its loop; the orchestrated one reads the last frame."""
    fallback = "cycles.device is GPU, but Preferences on this machine select no GPU backend"
    replies = [
        _fake_still_reply(1, "/tmp/beat_0001.png"),
        {**_fake_still_reply(2, "/tmp/beat_0002.png"), "effective_cycles_device": "CPU", "warnings": [fallback]},
    ]

    summary = rendering._aggregate_animation_summary(_animation_plan(2), replies, _animation_outcome())

    assert summary["engine"] == "CYCLES"
    assert summary["effective_cycles_device"] == "CPU"
    assert summary["warnings"] == [fallback]


def test_aggregate_animation_summary_detail_adds_files_and_progress() -> None:
    replies = [_fake_still_reply(f, f"/tmp/beat_{f:04d}.png") for f in (1, 2, 3)]

    detailed = rendering._aggregate_animation_summary(_animation_plan(3), replies, _animation_outcome(detail=True))

    assert [entry["frame"] for entry in detailed["files"]] == [1, 2, 3]
    assert [entry["completed"] for entry in detailed["progress"]] == [1, 2, 3]
    assert detailed["progress_truncated"] is False


def test_aggregate_animation_summary_reports_a_cancelled_partial_run() -> None:
    """Only 2 of 3 planned frames completed; the summary must say so, not claim frame_count=3."""
    replies = [_fake_still_reply(1, "/tmp/beat_0001.png"), _fake_still_reply(2, "/tmp/beat_0002.png")]

    summary = rendering._aggregate_animation_summary(
        _animation_plan(3),
        replies,
        _animation_outcome(cancelled=True, cancellation_reason="max_duration_seconds exceeded", duration_seconds=0.02),
    )

    assert summary["status"] == "CANCELLED"
    assert summary["frame_count"] == 2
    assert summary["last_file"] == "/tmp/beat_0002.png"
    assert summary["output_persisted"] is False


def test_aggregate_animation_summary_zero_completed_frames_reports_empty_not_stale() -> None:
    """Cancelled before frame 1 ever rendered: nothing to report, not a guess from a prior render."""
    summary = rendering._aggregate_animation_summary(
        _animation_plan(3),
        [],
        _animation_outcome(cancelled=True, cancellation_reason="max_duration_seconds exceeded", duration_seconds=0.0),
    )

    assert summary["frame_count"] == 0
    assert summary["first_file"] is None
    assert summary["last_file"] is None
    assert summary["bytes_written"] == 0
    assert summary["operator_result"] == ["FINISHED"]
    assert summary["passes"] == []
    assert summary["engine"] is None
    assert summary["effective_cycles_device"] is None


def test_aggregate_animation_summary_raises_when_verify_passes_finds_none() -> None:
    with pytest.raises(RuntimeError, match="no enabled passes could be verified"):
        rendering._aggregate_animation_summary(
            _animation_plan(3),
            [],
            _animation_outcome(verify_passes=True, cancelled=True, cancellation_reason="max_duration_seconds exceeded"),
        )


class _AnimationConnection:
    """Fake connection for the orchestrated ANIMATION path: plan, N STILL calls, an optional persist."""

    def __init__(self, frame_count=3):
        self.calls = []
        self.frame_count = frame_count

    def send_command(self, command, params):
        self.calls.append((command, params))
        if command == "plan_render_animation":
            frames = [{"frame": f, "path": f"/tmp/beat_{f:04d}.png"} for f in range(1, self.frame_count + 1)]
            return {
                "requested_filepath": "//renders/beat_",
                "output": "/tmp/beat_",
                "frame_current": 1,
                "frames": frames,
            }
        if command == "render_scene":
            return _fake_still_reply(
                params["frame"], params["filepath"], render_slot_policy=params["render_slot_policy"]
            )
        if command == "configure_render_settings":
            return {"scene": "Scene", "changed": ["output.filepath"], "changed_resources": ["Scene"]}
        raise AssertionError(f"unexpected command {command}")


class _FakeReportProgressContext:
    """Records report_progress calls with the real Context.report_progress(progress, total, message) signature."""

    def __init__(self):
        self.progress_calls = []

    async def report_progress(self, progress, total=None, message=None):
        self.progress_calls.append((progress, total, message))


def test_orchestrated_animation_calls_plan_then_one_still_per_frame_then_persists(monkeypatch) -> None:
    connection = _AnimationConnection(frame_count=3)
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)
    ctx = _FakeReportProgressContext()

    envelope = asyncio.run(
        rendering.render_scene(
            ctx=ctx,
            scene_name="Scene",
            filepath="//renders/beat_",
            mode="ANIMATION",
            confirm_render=True,
            render_slot_policy="NEW_SLOT",
            persist_output=True,
            detail=True,
        )
    )

    command_sequence = [command for command, _params in connection.calls]
    assert command_sequence == [
        "plan_render_animation",
        "render_scene",
        "render_scene",
        "render_scene",
        "configure_render_settings",
    ]
    render_slot_policies = [
        params["render_slot_policy"] for command, params in connection.calls if command == "render_scene"
    ]
    assert render_slot_policies == ["NEW_SLOT", "USE_ACTIVE", "USE_ACTIVE"]
    persist_patch = connection.calls[-1][1]["patch"]
    assert persist_patch == {"output": {"filepath": "//renders/beat_"}}
    assert [call["frame"] for command, call in connection.calls if command == "render_scene"] == [1, 2, 3]

    assert ctx.progress_calls == [
        (1, 3, "Rendered frame 1 (1/3)"),
        (2, 3, "Rendered frame 2 (2/3)"),
        (3, 3, "Rendered frame 3 (3/3)"),
    ]
    assert envelope["ok"] is True
    assert envelope["data"]["mode"] == "ANIMATION"
    assert envelope["data"]["frame_count"] == 3
    assert envelope["data"]["output_persisted"] is True
    assert [entry["frame"] for entry in envelope["data"]["files"]] == [1, 2, 3]


def test_orchestrated_animation_rejects_frame_with_animation_mode(monkeypatch) -> None:
    connection = _AnimationConnection()
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    with pytest.raises(ToolError, match="frame is only valid for STILL renders"):
        asyncio.run(
            rendering.render_scene(
                ctx=_FakeReportProgressContext(),
                scene_name="Scene",
                mode="ANIMATION",
                frame=5,
                confirm_render=True,
            )
        )
    assert connection.calls == []


def test_orchestrate_animation_false_uses_the_single_legacy_call(monkeypatch) -> None:
    connection = _Connection()
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    asyncio.run(
        rendering.render_scene(
            ctx=_FakeReportProgressContext(),
            scene_name="Scene",
            filepath="//renders/beat_",
            mode="ANIMATION",
            confirm_render=True,
            orchestrate_animation=False,
        )
    )

    assert len(connection.calls) == 1
    command, params = connection.calls[0]
    assert command == "render_scene"
    assert params["mode"] == "ANIMATION"


def test_orchestrated_animation_cancellation_lands_between_frames_and_skips_persist(monkeypatch) -> None:
    """A real client cancellation must land as CancelledError, not a graceful partial summary."""

    class _GatedAnimationConnection(_AnimationConnection):
        def __init__(self, frame_count=5, block_at_frame=2):
            super().__init__(frame_count=frame_count)
            self.block_at_frame = block_at_frame
            self._gate = threading.Event()

        def send_command(self, command, params):
            if command == "render_scene" and params["frame"] == self.block_at_frame:
                result = super().send_command(command, params)
                self._gate.wait(timeout=1.0)
                return result
            return super().send_command(command, params)

    connection = _GatedAnimationConnection(frame_count=5, block_at_frame=2)
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)
    ctx = _FakeReportProgressContext()

    async def drive():
        task = asyncio.ensure_future(
            rendering.render_scene(
                ctx=ctx,
                scene_name="Scene",
                filepath="//renders/beat_",
                mode="ANIMATION",
                confirm_render=True,
                persist_output=True,
            )
        )

        def render_calls():
            return [c for c, _p in connection.calls if c == "render_scene"]

        while len(render_calls()) < 2:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(drive())
    connection._gate.set()

    assert [c for c, _p in connection.calls] == ["plan_render_animation", "render_scene", "render_scene"]
    assert ctx.progress_calls == [(1, 5, "Rendered frame 1 (1/5)")]
    assert all(command != "configure_render_settings" for command, _params in connection.calls)


def test_inspect_render_output_uses_the_inline_transport_when_the_addon_supports_it(monkeypatch) -> None:
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

    monkeypatch.setattr(_dispatch, "get_blender_connection", Connection)
    monkeypatch.setattr(
        _image_transport,
        "get_last_handshake",
        lambda: types.SimpleNamespace(protocol_version=_image_transport.INLINE_IMAGE_PROTOCOL_VERSION),
    )
    monkeypatch.setattr(
        _image_transport.tempfile,
        "mkstemp",
        lambda **_kwargs: pytest.fail("the inline transport must not touch the local filesystem"),
    )

    image, envelope = asyncio.run(
        rendering.inspect_render_output(ctx=None, output_path="/renders/frame.png", max_size=500)
    )

    command, params = calls[0]
    assert command == "inspect_render_output"
    assert params["inline"] is True
    assert params["output_path"] == "/renders/frame.png"
    assert "filepath" not in params
    assert image.data == b"inline-render"
    assert envelope["data"]["source"] == "output_path"


# ---------------------------------------------------------------------------
# Duration bounds: refused where nothing can apply them, reported where overrun.
# ---------------------------------------------------------------------------


def _timed_renderable(monkeypatch, tmp_path, seconds_per_frame):
    """
    Build a renderable handler whose clock advances by a fixed amount per rendered frame.

    Returns:
        tuple: The handler, the scene, and the clock as a one-element list.

    """
    handler, scene, fake_bpy = _renderable(monkeypatch, tmp_path)
    clock = [0.0]
    handlers = importlib.import_module(type(handler).__module__)
    monkeypatch.setattr(handlers, "time", types.SimpleNamespace(monotonic=lambda: clock[0], time=lambda: 0.0))
    write_frame = fake_bpy.ops.render.render

    def slow_render(**kwargs):
        clock[0] += seconds_per_frame
        return write_frame(**kwargs)

    fake_bpy.ops.render = types.SimpleNamespace(render=slow_render)
    return handler, scene, clock


def test_the_addon_refuses_a_duration_bound_on_a_still_and_renders_nothing(monkeypatch, tmp_path) -> None:
    """One frame is one blocking call; a bound checked only before it starts would be silently ignored."""
    handler, _scene, _bpy = _renderable(monkeypatch, tmp_path)

    with pytest.raises(ValueError, match="manage_render_job"):
        handler.render_scene(
            "Scene", str(tmp_path / "sh010.png"), confirm_render=True, max_duration_seconds=60, verify_passes=False
        )

    assert not (tmp_path / "sh010.png").exists()


def test_the_server_refuses_a_duration_bound_on_a_still_before_dispatch(monkeypatch) -> None:
    connection = _Connection()
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    with pytest.raises(ToolError, match="manage_render_job"):
        asyncio.run(
            rendering.render_scene(
                ctx=None, scene_name="Scene", filepath="/tmp/sh010.png", confirm_render=True, max_duration_seconds=60
            )
        )

    assert connection.calls == []


def test_an_addon_animation_that_overruns_its_duration_bound_warns(monkeypatch, tmp_path) -> None:
    """The bound is checked between frames, so a frame started just under it finishes over it."""
    handler, scene, clock = _timed_renderable(monkeypatch, tmp_path, seconds_per_frame=10.0)
    scene.frame_end = 2

    def animate(prefix, bound):
        clock[0] = 0.0
        return handler.render_scene(
            "Scene",
            str(tmp_path / prefix),
            mode="ANIMATION",
            confirm_render=True,
            max_duration_seconds=bound,
            verify_passes=False,
        )

    within = animate("within_", 25.0)
    over = animate("over_", 15.0)

    assert "warnings" not in within
    # Frame 2 started at 10 s, under the bound, and was never going to be interrupted.
    assert (over["frame_count"], over["status"], over["duration_seconds"]) == (2, "COMPLETED", 20.0)
    assert len(over["warnings"]) == 1
    assert "manage_render_job" in over["warnings"][0]


class _TimedAnimationConnection(_AnimationConnection):
    """An orchestrated run whose every frame takes a fixed ten seconds on a shared fake clock."""

    def __init__(self, clock, frame_count=3):
        super().__init__(frame_count=frame_count)
        self.clock = clock

    def send_command(self, command, params):
        if command == "render_scene":
            self.clock[0] += 10.0
        return super().send_command(command, params)


def test_an_orchestrated_animation_that_overruns_its_duration_bound_warns_in_the_envelope(monkeypatch) -> None:
    clock = [0.0]
    monkeypatch.setattr(rendering, "time", types.SimpleNamespace(monotonic=lambda: clock[0]))
    connection = _TimedAnimationConnection(clock, frame_count=3)
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    def render(bound):
        clock[0] = 0.0
        return asyncio.run(
            rendering.render_scene(
                ctx=_FakeReportProgressContext(),
                scene_name="Scene",
                filepath="//renders/beat_",
                mode="ANIMATION",
                confirm_render=True,
                max_duration_seconds=bound,
            )
        )

    within = render(30.0)
    over = render(15.0)

    assert (within["data"]["status"], within["warnings"]) == ("COMPLETED", [])
    # Frame 2 started at 10 s and finished at 20 s; frame 3 was never sent.
    assert (over["data"]["status"], over["data"]["frame_count"]) == ("CANCELLED", 2)
    assert len(over["warnings"]) == 1
    assert "manage_render_job" in over["warnings"][0]
    assert "warnings" not in over["data"]


# ---------------------------------------------------------------------------
# Render Result: its pixels are labelled with the render that made them.
# ---------------------------------------------------------------------------


class _FakeRenderResult:
    """The in-memory Render Result, which only ever saves the last render's pixels."""

    def save_render(self, filepath):
        Path(filepath).write_bytes(b"render-result")


def _render_result_harness(monkeypatch, tmp_path):
    """
    Build a renderable handler whose renders fire Blender's render_init handlers, with a Render Result.

    Returns:
        tuple: The handler, the scene, and a callable standing in for a render from Blender's UI.

    """
    handler, scene, fake_bpy = _renderable(monkeypatch, tmp_path)
    addon_name = type(handler).__module__.rsplit(".handlers.", 1)[0]
    monkeypatch.setattr(fake_bpy.app.handlers, "render_init", [], raising=False)
    importlib.import_module(f"{addon_name}.render_result_record").register_handlers()
    fake_bpy.data.images["Render Result"] = _FakeRenderResult()
    write_frame = fake_bpy.ops.render.render

    def ui_render():
        for callback in fake_bpy.app.handlers.render_init:
            callback(scene)

    def render(**kwargs):
        ui_render()
        return write_frame(**kwargs)

    fake_bpy.ops.render = types.SimpleNamespace(render=render)
    return handler, scene, ui_render


def test_render_result_reports_the_frame_render_scene_rendered_not_the_playhead(monkeypatch, tmp_path) -> None:
    handler, scene, _ui_render = _render_result_harness(monkeypatch, tmp_path)
    copy = str(tmp_path / "copy.png")

    handler.render_scene("Scene", str(tmp_path / "sh010.png"), frame=12, confirm_render=True, verify_passes=False)
    reply = handler.inspect_render_output(copy)

    assert scene.frame_current == 1, "render_scene puts the playhead back"
    assert (reply["source"], reply["scene"], reply["frame"]) == ("render_result", "Scene", 12)
    assert reply["source_path"] == str(tmp_path / "sh010.png")
    assert "warnings" not in reply
    assert handler.inspect_render_output(copy, frame=12)["frame"] == 12
    with pytest.raises(ValueError, match="holds frame 12"):
        handler.inspect_render_output(copy, frame=1)


def test_render_result_from_a_render_render_scene_did_not_record_is_not_labelled(monkeypatch, tmp_path) -> None:
    """A render from Blender's UI replaces the pixels, so render_scene's frame no longer describes them."""
    handler, _scene, ui_render = _render_result_harness(monkeypatch, tmp_path)
    copy = str(tmp_path / "copy.png")
    handler.render_scene("Scene", str(tmp_path / "sh010.png"), frame=12, confirm_render=True, verify_passes=False)

    ui_render()
    reply = handler.inspect_render_output(copy)

    assert (reply["scene"], reply["frame"], reply["source_path"]) == (None, None, None)
    assert len(reply["warnings"]) == 1
    with pytest.raises(ValueError, match="origin is unknown"):
        handler.inspect_render_output(copy, frame=12)
