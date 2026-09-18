# ruff: file-ignore[docstring-missing-exception, magic-value-comparison, module-import-not-at-top-of-file]
"""Run with Blender 5.1+ to smoke-test render and view-layer handlers."""

import importlib.util
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
    assert configured["settings"]["resolution"] == [32, 24, 100]
    assert configured["settings"]["output"]["compression"] == 25
    assert configured["settings"]["film"]["transparent"] is True

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
    assert eevee["settings"]["engine"] == "BLENDER_EEVEE"
    assert scene.eevee.taa_render_samples == 7
    print("RENDERING_SMOKE_OK")


if __name__ == "__main__":
    main()
