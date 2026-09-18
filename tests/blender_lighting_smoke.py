"""Run with Blender 5.1+ to smoke-test production lighting data and world operations."""

from __future__ import annotations

import importlib.util
import math
import os
import sys
import tempfile

from pathlib import Path

import bpy
import mathutils

addon_path = Path(__file__).resolve().parents[1] / "src" / "blender_mcp" / "bundled" / "addon" / "__init__.py"
package_name = "blender_mcp_lighting_smoke"
spec = importlib.util.spec_from_file_location(
    package_name, addon_path, submodule_search_locations=[str(addon_path.parent)]
)
assert spec is not None
addon = importlib.util.module_from_spec(spec)
sys.modules[package_name] = addon
spec.loader.exec_module(addon)

from blender_mcp_lighting_smoke.handlers.lighting import LightingHandlers  # noqa: E402


def main() -> None:
    """Exercise representative light, aiming, linking, world, color, and inspection workflows."""
    handler = LightingHandlers()
    scene = bpy.context.scene
    scene.name = "Lighting Smoke"

    try:
        handler.create_light(
            scene.name,
            "Failed Lighting",
            "Invalid Light",
            "AREA",
            (0.0, 0.0, 0.0),
            settings={"shape": "INVALID"},
        )
    except ValueError:
        pass
    else:
        raise AssertionError("Invalid light shape must fail")
    assert bpy.data.objects.get("Invalid Light") is None
    assert bpy.data.lights.get("Invalid Light Light") is None
    assert bpy.data.collections.get("Failed Lighting") is None
    receivers = bpy.data.collections.new("Lighting Receivers")
    scene.collection.children.link(receivers)

    created = handler.create_light(
        scene.name,
        "Lighting",
        "Key Light",
        "AREA",
        (4.0, -4.0, 5.0),
        (0.0, 0.0, 0.0),
        {"energy": 800.0, "shape": "RECTANGLE", "size": 3.0, "size_y": 2.0},
    )
    assert created["object"] == "Key Light"
    assert created["settings"]["shape"] == "RECTANGLE"

    conflicting_constraint = bpy.data.objects["Key Light"].constraints.new("COPY_LOCATION")
    conflicting_constraint.name = "Conflicting Aim"
    try:
        handler.aim_light(
            scene.name,
            "Key Light",
            target_point=(0.0, 0.0, 0.0),
            method="TRACK_TO",
            constraint_name="Conflicting Aim",
            helper_name="Unwanted Aim Helper",
            helper_collection_name="Unwanted Aim Helpers",
        )
    except ValueError:
        pass
    else:
        raise AssertionError("An incompatible named constraint must fail")
    assert bpy.data.objects.get("Unwanted Aim Helper") is None
    assert bpy.data.collections.get("Unwanted Aim Helpers") is None
    bpy.data.objects["Key Light"].constraints.remove(conflicting_constraint)

    configured = handler.configure_light("Key Light", {"exposure": 1.0, "use_shadow": True})
    assert configured["new"]["exposure"] == 1.0

    aimed = handler.aim_light(scene.name, "Key Light", target_point=(0.0, 0.0, 0.0))
    assert aimed["method"] == "STATIC_ROTATION"
    direction = bpy.data.objects["Key Light"].matrix_world.to_quaternion() @ mathutils.Vector((0, 0, -1))
    expected = (mathutils.Vector((0, 0, 0)) - bpy.data.objects["Key Light"].matrix_world.translation).normalized()
    assert sum(left * right for left, right in zip(direction, expected, strict=True)) > 0.99999

    linking = handler.configure_light_linking(scene.name, "Key Light", "Lighting Receivers")
    assert linking["after"]["receiver"]["collection"] == "Lighting Receivers"

    if bpy.data.objects.get("Cube") is None:
        bpy.ops.mesh.primitive_cube_add(size=2.0, location=(0.0, 0.0, 1.0))
        bpy.context.active_object.name = "Cube"
    studio_target = bpy.data.objects["Cube"]
    studio_camera = scene.camera
    assert studio_camera is not None

    try:
        handler.create_studio_lighting(scene.name, studio_target.name, studio_camera.name, mood="INVALID")
    except ValueError:
        pass
    else:
        raise AssertionError("Invalid mood must fail")
    assert bpy.data.objects.get("Cube Key") is None

    studio = handler.create_studio_lighting(
        scene.name, studio_target.name, studio_camera.name, mood="SOFT", rig_name="Studio Rig"
    )
    assert {entry["role"] for entry in studio["lights"]} == {"key", "fill", "rim"}
    energy_by_role = {entry["role"]: entry["energy"] for entry in studio["lights"]}
    assert energy_by_role["key"] > energy_by_role["fill"] > 0
    assert energy_by_role["key"] > energy_by_role["rim"] > 0
    for role in ("key", "fill", "rim"):
        rig_light = bpy.data.objects[f"Studio Rig {role.capitalize()}"]
        assert rig_light.data.type == "AREA"
        assert rig_light.users_collection[0].name == "Studio Lighting"

    try:
        handler.create_studio_lighting(scene.name, studio_target.name, studio_camera.name, rig_name="Studio Rig")
    except ValueError:
        pass
    else:
        raise AssertionError("Colliding rig name must fail")
    assert bpy.data.objects.get("Studio Rig Key") is not None  # the earlier successful rig, untouched

    world = handler.configure_world_background(scene.name, (0.04, 0.05, 0.08), 0.7, False, "Lighting World", True)
    assert world["source"] == "BACKGROUND"
    # A world this call created carries only the managed pair, not Blender's default nodes beside it.
    created_types = sorted(node.bl_idname for node in bpy.data.worlds["Lighting World"].node_tree.nodes)
    assert created_types == ["ShaderNodeBackground", "ShaderNodeOutputWorld"], created_types

    sky = handler.configure_procedural_sky(
        scene.name,
        {
            "sky_type": "MULTIPLE_SCATTERING",
            "sun_elevation": math.radians(35),
            "sun_rotation": math.radians(110),
            "sun_size": math.radians(0.53),
            "background_strength": 0.8,
        },
        "BOTH",
        True,
        "Sky Sun",
        "Lighting",
        2.0,
    )
    assert sky["synchronized_sun"] == "Sky Sun"

    with tempfile.TemporaryDirectory(prefix="blender_mcp_lighting_") as temp_directory:
        environment_path = os.path.join(temp_directory, "smoke_environment.hdr")
        source_image = bpy.data.images.new("Smoke Environment Source", width=4, height=2, float_buffer=True)
        source_image.generated_color = (0.2, 0.3, 0.5, 1.0)
        source_image.filepath_raw = environment_path
        source_image.file_format = "HDR"
        source_image.save()
        bpy.data.images.remove(source_image)
        hdri = handler.configure_hdri_environment(scene.name, environment_path, strength=0.5, rotation=0.25)
        assert hdri["source"] == "HDRI"
        assert hdri["projection"] == "EQUIRECTANGULAR"

        quality = handler.configure_lighting_quality(
            scene.name,
            "EEVEE",
            eevee={"render_samples": 4, "shadow_ray_count": 1, "shadow_step_count": 4},
        )
        assert quality["expanded_values"]["eevee"]["render_samples"] == 4

        original_resolution = (scene.render.resolution_x, scene.render.resolution_y)
        preview_path = os.path.join(temp_directory, "lighting_preview.png")
        preview = handler.render_lighting_preview(
            scene.name,
            scene.camera.name,
            scene.frame_current,
            "EEVEE",
            32,
            32,
            1,
            {"EEVEE": preview_path},
        )
        assert preview["outputs"][0]["size_bytes"] > 0
        assert preview["warnings"] == []
        assert (scene.render.resolution_x, scene.render.resolution_y) == original_resolution

    view = bpy.context.scene.view_settings.view_transform
    color = handler.configure_color_management(scene.name, view_transform=view, exposure=0.0)
    assert color["after"]["view_transform"] == view

    inventory = handler.list_lights(scene.name)
    assert inventory["total"] >= 2
    assert {"Key Light", "Sky Sun"}.issubset({item["object"] for item in inventory["lights"]})
    inspected = handler.inspect_lighting_setup(scene.name)
    assert inspected["world"]["name"] == "Lighting World"
    validated = handler.validate_lighting_setup(scene.name, "EEVEE")
    assert "findings" in validated

    print("LIGHTING_SMOKE_OK")


if __name__ == "__main__":
    main()
