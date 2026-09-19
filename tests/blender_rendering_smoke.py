# ruff: file-ignore[module-import-not-at-top-of-file]
"""Run with Blender 5.1+ to smoke-test render and view-layer handlers."""

import importlib.util
import math
import os
import sys
import tempfile

from pathlib import Path

import bpy

addon_path = Path(__file__).resolve().parents[1] / "src" / "blender_mcp" / "bundled" / "addon" / "__init__.py"
package_name = "blender_mcp_rendering_smoke"
spec = importlib.util.spec_from_file_location(
    package_name,
    addon_path,
    submodule_search_locations=[str(addon_path.parent)],
)
assert spec is not None
addon = importlib.util.module_from_spec(spec)
sys.modules[package_name] = addon
spec.loader.exec_module(addon)

from blender_mcp_rendering_smoke.handlers.rendering import RenderingHandlersMixin


def _check_output_path_resolution(handler: RenderingHandlersMixin, scene: bpy.types.Scene) -> None:
    """`~` means the home directory, and a bad path's error does not expose Blender's working directory."""
    with tempfile.TemporaryDirectory() as home:
        previous_home = os.environ.get("HOME")
        os.environ["HOME"] = home
        try:
            handler.render_scene(scene.name, "~/tilde.png", confirm_render=True, render_slot_policy="NEW_SLOT")
            assert (Path(home) / "tilde.png").is_file()
        finally:
            if previous_home is None:
                del os.environ["HOME"]
            else:
                os.environ["HOME"] = previous_home
    try:
        handler.render_scene(scene.name, "no_such_dir/x.png", confirm_render=True)
    except ValueError as exc:
        assert os.getcwd() not in str(exc), "the error exposed Blender's working directory"
    else:
        raise AssertionError("A render into a missing directory was accepted")


def _check_directory_output_is_refused(handler: RenderingHandlersMixin, scene: bpy.types.Scene) -> None:
    """Refuse a directory path, which Blender would write as `<dir>0001.png` beside the folder."""
    with tempfile.TemporaryDirectory() as directory:
        renders = Path(directory) / "renders"
        renders.mkdir()
        for mode, requested in (("ANIMATION", f"{renders}/"), ("STILL", f"{renders}/"), ("STILL", str(renders))):
            try:
                handler.render_scene(scene.name, requested, mode=mode, confirm_render=True)
            except ValueError as exc:
                assert "is a directory" in str(exc), str(exc)
                assert "/frame_####.png" in str(exc), "the refusal did not name a path that works"
            else:
                raise AssertionError(f"A {mode} render into a directory was accepted")
        assert list(renders.iterdir()) == [], "a refused render still wrote a file"
        assert sorted(p.name for p in Path(directory).iterdir()) == ["renders"], "a sibling file was written"
    with tempfile.TemporaryDirectory() as directory:
        # Blender writes "sh010.png0001.png" for this; the prefix and template forms both work.
        try:
            handler.render_scene(scene.name, str(Path(directory) / "sh010.png"), mode="ANIMATION", confirm_render=True)
        except ValueError as exc:
            assert "sh010.png0001.png" in str(exc), str(exc)
        else:
            raise AssertionError("An ANIMATION render into a single .png filename was accepted")
        rendered = handler.render_scene(
            scene.name, str(Path(directory) / "sh010_"), mode="ANIMATION", confirm_render=True
        )
        written = sorted(p.name for p in Path(directory).iterdir())
        assert written == ["sh010_0001.png", "sh010_0002.png"], written
        assert [entry["path"] for entry in rendered["files"]] == [str(Path(directory) / name) for name in written]


def _check_frame_is_read_back_from_the_filename(handler: RenderingHandlersMixin, scene: bpy.types.Scene) -> None:
    """Report the frame Blender encoded in the name, and leave an unlabelled still's frame null."""
    with tempfile.TemporaryDirectory() as directory:
        sequence = handler.render_scene(
            scene.name, str(Path(directory) / "beat_"), mode="ANIMATION", confirm_render=True
        )
        copy = str(Path(directory) / "copy.png")
        inspected = handler.inspect_render_output(copy, output_path=sequence["files"][-1]["path"])
        assert inspected["frame"] == scene.frame_end, inspected
        still = handler.render_scene(
            scene.name, str(Path(directory) / "hero.png"), mode="STILL", frame=1, confirm_render=True
        )
        assert handler.inspect_render_output(copy, output_path=still["filepath"])["frame"] is None


def _check_eevee_ray_tracing_is_reachable(handler: RenderingHandlersMixin, scene: bpy.types.Scene) -> None:
    """Blender 5.2 splits the switch and the screen-trace options across two structs."""
    patched = handler.configure_render_settings(
        scene.name,
        {
            "engine": "BLENDER_EEVEE",
            "eevee": {
                "use_raytracing": True,
                "ray_tracing_method": "SCREEN",
                "ray_tracing": {"resolution_scale": "1", "screen_trace_quality": 0.5, "trace_max_roughness": 1.0},
            },
        },
    )
    assert patched["after"]["eevee.use_raytracing"] is True
    assert patched["after"]["eevee.ray_tracing.resolution_scale"] == "1"
    options = scene.eevee.ray_tracing_options
    assert scene.eevee.use_raytracing is True
    assert scene.eevee.ray_tracing_method == "SCREEN"
    assert (options.resolution_scale, round(options.screen_trace_quality, 3)) == ("1", 0.5)
    reported = handler.inspect_render_setup(scene.name)["eevee"]
    assert reported["use_raytracing"] is True
    assert math.isclose(reported["ray_tracing"]["trace_max_roughness"], 1.0)
    try:
        handler.configure_render_settings(scene.name, {"eevee": {"ray_tracing": {"screen_trace_qualtiy": 0.5}}})
    except ValueError as exc:
        assert "EEVEE ray tracing settings are unavailable" in str(exc), str(exc)
    else:
        raise AssertionError("A misspelled ray-tracing option was accepted")


def _check_detail_reply(handler: RenderingHandlersMixin, scene: bpy.types.Scene) -> None:
    """`detail=True` restores both whole-state blocks; the default reply carries neither."""
    detailed = handler.configure_render_settings(scene.name, {"resolution_percentage": 50}, detail=True)
    assert detailed["changed"] == ["resolution_percentage"]
    assert detailed["before"]["resolution"] == [32, 24, 100]
    assert detailed["after"]["resolution"] == [32, 24, 50]
    assert detailed["after"]["output"]["compression"] == 25
    assert detailed["after"]["film"]["transparent"] is True
    assert "settings" not in detailed
    handler.configure_render_settings(scene.name, {"resolution_percentage": 100})


def main() -> None:
    """Exercise settings, passes, rollback, view layers, and a tiny still render."""
    handler = RenderingHandlersMixin()
    scene = bpy.context.scene
    configured = handler.configure_render_settings(
        scene.name,
        {
            "engine": "BLENDER_WORKBENCH",
            "resolution_x": 32,
            "resolution_y": 24,
            "resolution_percentage": 100,
            "image_format": "PNG",
            "color_mode": "RGBA",
            "color_depth": "8",
            "compression": 25,
            "quality": 80,
            "frame_start": 1,
            "frame_end": 2,
            "film": {"transparent": True},
            "output": {
                "filepath": "//unused-smoke-output",
                "use_file_extension": True,
            },
            "metadata": {"use_stamp": True, "use_stamp_frame": True},
        },
    )
    assert "before" not in configured
    assert "settings" not in configured
    assert configured["scene"] == scene.name
    assert configured["after"] == {
        "engine": "BLENDER_WORKBENCH",
        "resolution_x": 32,
        "resolution_y": 24,
        "resolution_percentage": 100,
        "image_format": "PNG",
        "color_mode": "RGBA",
        "color_depth": "8",
        "compression": 25,
        "quality": 80,
        "frame_start": 1,
        "frame_end": 2,
        "film.transparent": True,
        "output.filepath": "//unused-smoke-output",
        "output.use_file_extension": True,
        "metadata.use_stamp": True,
        "metadata.use_stamp_frame": True,
    }
    assert configured["changed"] == sorted(configured["after"])

    _check_detail_reply(handler, scene)

    original_start = scene.frame_start
    try:
        handler.configure_render_settings(scene.name, {"frame_start": scene.frame_end + 1})
    except ValueError as exc:
        assert "Resulting frame_end" in str(exc)
    else:
        raise AssertionError("Invalid resulting frame range was accepted")
    assert scene.frame_start == original_start

    layer = handler.manage_view_layers(
        scene.name,
        "CREATE",
        "Smoke Passes",
        {
            "use_pass_z": True,
            "use_pass_normal": True,
            "use_pass_position": True,
            "use_pass_cryptomatte_object": True,
            "pass_cryptomatte_depth": 8,
        },
    )
    assert layer["view_layer"]["passes"]["use_pass_position"] is True

    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "render.png"
        rendered = handler.render_scene(
            scene.name,
            str(output),
            mode="STILL",
            view_layer_name="Smoke Passes",
            frame=1,
            confirm_render=True,
            render_slot_policy="NEW_SLOT",
        )
        assert rendered["operator_result"] == ["FINISHED"]
        assert output.is_file()
        assert rendered["passes"]
        assert rendered["pass_verification"] in {"RENDER_RESULT", "VIEW_LAYER_CONFIGURATION"}

    _check_output_path_resolution(handler, scene)
    _check_directory_output_is_refused(handler, scene)
    _check_frame_is_read_back_from_the_filename(handler, scene)

    removed = handler.manage_view_layers(scene.name, "REMOVE", "Smoke Passes", confirm_remove=True)
    assert removed["removed"] == "Smoke Passes"
    node_tree = bpy.data.node_groups.new("Smoke Compositor", "CompositorNodeTree")
    scene.compositing_node_group = node_tree
    node_tree.nodes.clear()
    render_layers = node_tree.nodes.new("CompositorNodeRLayers")
    viewer = node_tree.nodes.new("CompositorNodeViewer")
    node_tree.links.new(render_layers.outputs["Image"], viewer.inputs["Image"])
    inspected = handler.inspect_render_setup(scene.name, graph_sections=["NODES", "LINKS"], limit=10)
    assert inspected["engine"] == "BLENDER_WORKBENCH"
    assert inspected["compositor"]["nodes"]["returned_count"] == 2
    assert inspected["compositor"]["links"]["returned_count"] == 1

    eevee = handler.configure_render_settings(
        scene.name,
        {"engine": "BLENDER_EEVEE", "eevee": {"taa_render_samples": 7}},
    )
    assert eevee["after"] == {"engine": "BLENDER_EEVEE", "eevee.taa_render_samples": 7}
    assert scene.eevee.taa_render_samples == 7
    _check_eevee_ray_tracing_is_reachable(handler, scene)
    print("RENDERING_SMOKE_OK")


if __name__ == "__main__":
    main()
