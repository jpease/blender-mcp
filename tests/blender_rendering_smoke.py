# ruff: file-ignore[module-import-not-at-top-of-file]
"""Run with Blender 5.1+ to smoke-test render and view-layer handlers."""

import base64
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
    """`~` means the home directory on both sides, and a bad path's error hides Blender's working directory."""
    with tempfile.TemporaryDirectory() as home:
        previous_home = os.environ.get("HOME")
        os.environ["HOME"] = home
        try:
            handler.render_scene(scene.name, "~/tilde.png", confirm_render=True, render_slot_policy="NEW_SLOT")
            assert (Path(home) / "tilde.png").is_file()
            # The defect this pins: the writer expanded `~` and the reader did not, so the very
            # path render_scene had just written was reported missing on the next call.
            inspected = handler.inspect_render_output(str(Path(home) / "copy.png"), output_path="~/tilde.png")
            assert inspected["source_path"] == str(Path(home) / "tilde.png")
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


def _check_missing_directory_is_created_on_request(handler: RenderingHandlersMixin, scene: bpy.types.Scene) -> None:
    """Refuse a render into a missing directory unless asked to create it, then land the render there."""
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / "renders" / "sh030" / "hero.png"
        try:
            handler.render_scene(scene.name, str(target), confirm_render=True)
        except ValueError as exc:
            assert "create_directories=true" in str(exc), str(exc)
        else:
            raise AssertionError("A render into a missing directory was accepted without create_directories")
        assert not target.parent.exists(), "a refused render created a directory"

        rendered = handler.render_scene(scene.name, str(target), confirm_render=True, create_directories=True)
        assert rendered["created_directory"] is True, rendered
        assert target.is_file(), "the render did not land in the directory it created"
        again = handler.render_scene(
            scene.name, str(target), confirm_render=True, confirm_overwrite=True, create_directories=True
        )
        assert again["created_directory"] is False, again

        plan_target = Path(directory) / "shots" / "beat_"
        plan = handler.plan_render_animation(
            scene.name, str(plan_target), confirm_frame_range=True, create_directories=True
        )
        assert plan["frames"], plan
        assert not plan_target.parent.exists(), "planning an animation created its directory"


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
            scene.name, str(Path(directory) / "sh010_"), mode="ANIMATION", confirm_render=True, detail=True
        )
        written = sorted(p.name for p in Path(directory).iterdir())
        assert written == ["sh010_0001.png", "sh010_0002.png"], written
        assert [entry["path"] for entry in rendered["files"]] == [str(Path(directory) / name) for name in written]
        assert rendered["first_file"] == str(Path(directory) / written[0])
        assert rendered["last_file"] == str(Path(directory) / written[-1])


def _check_frame_is_read_back_from_the_filename(handler: RenderingHandlersMixin, scene: bpy.types.Scene) -> None:
    """Report the frame Blender encoded in the name, and leave an unlabelled still's frame null."""
    with tempfile.TemporaryDirectory() as directory:
        sequence = handler.render_scene(
            scene.name, str(Path(directory) / "beat_"), mode="ANIMATION", confirm_render=True
        )
        copy = str(Path(directory) / "copy.png")
        inspected = handler.inspect_render_output(copy, output_path=sequence["last_file"])
        assert inspected["frame"] == scene.frame_end, inspected
        still = handler.render_scene(
            scene.name, str(Path(directory) / "hero.png"), mode="STILL", frame=1, confirm_render=True
        )
        assert handler.inspect_render_output(copy, output_path=still["filepath"])["frame"] is None


def _check_inline_reply_carries_the_bytes(handler: RenderingHandlersMixin, scene: bpy.types.Scene) -> None:
    """`inline` answers with the image itself, writing nothing the caller could read."""
    with tempfile.TemporaryDirectory() as directory:
        still = handler.render_scene(
            scene.name, str(Path(directory) / "inline.png"), mode="STILL", frame=1, confirm_render=True
        )
        before = set(Path(tempfile.gettempdir()).glob("blender_mcp_inline_*"))
        inspected = handler.inspect_render_output(output_path=still["filepath"], inline=True)
        # The destination this process picked is its own; reporting it would invite the caller
        # - on another filesystem, which is the whole point - to try to read it.
        assert "filepath" not in inspected, inspected
        assert base64.b64decode(inspected["image_base64"]).startswith(b"\x89PNG"), "no PNG came back inline"
        leaked = set(Path(tempfile.gettempdir()).glob("blender_mcp_inline_*")) - before
        assert not leaked, f"the inline destination outlived the reply: {sorted(leaked)}"
        assert not list(Path(directory).glob("*.src.png")), "the render-result staging copy was left behind"


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


def _check_scene_owned_render_intent(handler: RenderingHandlersMixin, scene: bpy.types.Scene) -> None:
    """Take the output template from the scene, and store one back."""
    with tempfile.TemporaryDirectory() as directory:
        renders = Path(directory) / "renders"
        renders.mkdir()
        # This .blend has never been saved, so a `//` template has nothing to resolve against;
        # the absolute template is what the rig can render, and what `persist_output` stores is
        # still the caller's own text rather than a re-resolved path.
        absolute_template = str(renders / "sh020_")
        handler.configure_render_settings(scene.name, {"output": {"filepath": absolute_template}})
        assert scene.render.filepath == absolute_template

        rendered = handler.render_scene(scene.name, mode="ANIMATION", confirm_render=True, persist_output=True)

        written = sorted(path.name for path in renders.iterdir())
        assert written == ["sh020_0001.png", "sh020_0002.png"], written
        assert rendered["output_persisted"] is True
        assert scene.render.filepath == absolute_template
        assert rendered["first_file"] == str(renders / written[0])
        assert "files" not in rendered

    # A directory is refused at the moment it is stored, not only when a render reads it.
    with tempfile.TemporaryDirectory() as directory:
        try:
            handler.configure_render_settings(scene.name, {"output": {"filepath": f"{directory}/"}})
        except ValueError as exc:
            assert "is a directory" in str(exc), str(exc)
        else:
            raise AssertionError("A directory was accepted as the scene's stored output template")


def _check_default_frame_range_is_refused(handler: RenderingHandlersMixin, scene: bpy.types.Scene) -> None:
    """Blender's untouched 1-250 is a default, not a decision, so an ANIMATION over it is refused."""
    previous_end = scene.frame_end
    marker = "blender_mcp_frame_range_authored"
    had_marker = marker in scene
    scene.frame_end = 250
    if had_marker:
        # bpy's stub types __delitem__ for sequence indexing and loses the ID custom-property
        # protocol a real Scene has.
        del scene[marker]  # pyright: ignore[reportArgumentType]
    try:
        with tempfile.TemporaryDirectory() as directory:
            try:
                handler.render_scene(scene.name, str(Path(directory) / "wide_"), mode="ANIMATION", confirm_render=True)
            except ValueError as exc:
                assert "still Blender's default 1-250" in str(exc), str(exc)
            else:
                raise AssertionError("An ANIMATION over the untouched default range was accepted")
            assert list(Path(directory).iterdir()) == [], "a refused render still wrote a frame"
    finally:
        scene.frame_end = previous_end
        if had_marker:
            scene[marker] = True


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
        # Setting a frame range through this tool is what marks the range as chosen; the
        # render guard reads the same marker back off the scene.
        "frame_range_authored": True,
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
    _check_missing_directory_is_created_on_request(handler, scene)
    _check_directory_output_is_refused(handler, scene)
    _check_frame_is_read_back_from_the_filename(handler, scene)
    _check_inline_reply_carries_the_bytes(handler, scene)
    _check_scene_owned_render_intent(handler, scene)
    _check_default_frame_range_is_refused(handler, scene)

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
    # The display transform is a render input the whole reply otherwise leaves out; an agent
    # swapping engines reads exposure here, so it must be the scene's live value, not a default.
    scene.view_settings.exposure = 1.5
    color = handler.inspect_render_setup(scene.name)["color_management"]
    assert color["view_transform"] == scene.view_settings.view_transform
    assert math.isclose(color["exposure"], 1.5)
    assert math.isclose(color["exposure_multiplier"], 2.0**1.5)
    scene.view_settings.exposure = 0.0

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
