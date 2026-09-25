# ruff: file-ignore[module-import-not-at-top-of-file]
"""Run with Blender 5.1+ to smoke-test generic layered Action handlers."""

import math
import sys

from pathlib import Path

import bpy

sys.path.append(str(Path(__file__).resolve().parent))
from smoke_addon import load_addon

load_addon("blender_mcp_animation_smoke")

from blender_mcp_animation_smoke.handlers.animation import AnimationHandlersMixin


def _check_rig_control_properties(handler) -> None:
    """
    Key a rig's custom properties through a bone name that contains a dot.

    Only real Blender can confirm the two assumptions the resolver makes: that a
    pose bone's custom properties live in `id_properties_ensure()`, and that an
    F-Curve on `pose.bones["hand_ik.L"]["IK_FK"]` actually drives the property.
    """
    bpy.ops.object.armature_add(enter_editmode=False, location=(0.0, 0.0, 0.0))
    rig = bpy.context.object
    rig.name = "ControlRig"
    rig.data.bones[0].name = "hand_ik.L"
    bone = rig.pose.bones["hand_ik.L"]
    bone["IK_FK"] = 1.0
    bone["limits"] = [0.0, 1.0]

    keyed = handler.edit_keyframes(
        {"type": "OBJECT", "name": rig.name},
        [
            {"data_path": 'pose.bones["hand_ik.L"]["IK_FK"]', "frame": 1, "value": 1.0},
            {"data_path": 'pose.bones["hand_ik.L"]["IK_FK"]', "frame": 10, "value": 0.0},
            {"data_path": 'pose.bones["hand_ik.L"]["limits"]', "frame": 1, "value": [0.25, 0.75]},
        ],
        action_name="Rig Controls",
    )
    # Two scalar keys plus one array edit expanded into its two components.
    assert len(keyed["changed_keyframes"]) == 4

    bpy.context.scene.frame_set(10)
    evaluated_bone = rig.evaluated_get(bpy.context.evaluated_depsgraph_get()).pose.bones["hand_ik.L"]
    assert abs(evaluated_bone["IK_FK"]) < 1e-5
    assert abs(evaluated_bone["limits"][0] - 0.25) < 1e-5
    assert abs(evaluated_bone["limits"][1] - 0.75) < 1e-5

    for path, expected in (
        ("location[0]", "array_index"),
        ('pose.bones["hand_ik.L"]', "custom property holder"),
        ('pose.bones["hand_ik.L"]["missing"]', "Custom property not found"),
    ):
        try:
            handler.edit_keyframes(
                {"type": "OBJECT", "name": rig.name},
                [{"data_path": path, "frame": 1, "value": 0.0}],
                action_name="Rig Controls",
            )
        except ValueError as exc:
            assert expected in str(exc), f"{path}: {exc}"
        else:
            raise AssertionError(f"{path} was accepted")


def main() -> None:
    """Exercise Action creation, vector/scalar key edits, removal, and pagination."""
    handler = AnimationHandlersMixin()
    cube = bpy.data.objects["Cube"]
    created = handler.manage_animation_action(
        {"type": "OBJECT", "name": cube.name},
        "CREATE",
        action_name="Cube Procedural Motion",
    )
    assert created["action"] == "Cube Procedural Motion"

    edited = handler.edit_keyframes(
        {"type": "OBJECT", "name": cube.name},
        [
            {"data_path": "location", "frame": 1, "value": [0, 0, 0], "interpolation": "LINEAR"},
            {"data_path": "location", "frame": 20, "value": [2, 3, 4], "interpolation": "BEZIER"},
            {"data_path": "rotation_euler", "array_index": 2, "frame": 20, "value": 1.5},
        ],
    )
    assert len(edited["changed_keyframes"]) == 7
    assert tuple(cube.location) == (0.0, 0.0, 0.0)

    inspected = handler.inspect_animation({"type": "OBJECT", "name": cube.name}, offset=0, limit=4)
    assert inspected["action"]["is_layered"] is True
    assert inspected["total_keyframes"] == 7
    assert len(inspected["keyframes"]) == 4
    assert inspected["truncated"] is True
    assert inspected["next_offset"] == 4

    removed = handler.edit_keyframes(
        {"type": "OBJECT", "name": cube.name},
        [{"operation": "REMOVE", "data_path": "location", "frame": 1}],
    )
    assert len(removed["changed_keyframes"]) == 3
    assert handler.inspect_animation({"type": "OBJECT", "name": cube.name})["total_keyframes"] == 4

    handler.manage_animation_action(
        {"type": "OBJECT", "name": cube.name},
        "UNASSIGN",
        action_name="Cube Procedural Motion",
    )
    handler.manage_nla_tracks(
        {"type": "OBJECT", "name": cube.name},
        "CREATE_TRACK",
        "Procedural Takes",
        track_patch={"mute": False, "solo": False},
    )
    strip = handler.manage_nla_tracks(
        {"type": "OBJECT", "name": cube.name},
        "ADD_STRIP",
        "Procedural Takes",
        strip_name="Take 01",
        action_name="Cube Procedural Motion",
        frame_start=10,
        strip_patch={"blend_type": "REPLACE", "influence": 0.75, "repeat": 2.0},
    )
    assert strip["strip"] == "Take 01"
    nla = handler.inspect_animation({"type": "OBJECT", "name": cube.name})["nla_tracks"]
    assert math.isclose(nla[0]["strips"][0]["repeat"], 2.0)

    camera = bpy.data.objects["Camera"]
    duplicated = handler.manage_animation_action(
        {"type": "OBJECT", "name": camera.name},
        "DUPLICATE",
        action_name="Camera Procedural Motion",
        source_action_name="Cube Procedural Motion",
    )
    assert duplicated["action"] == "Camera Procedural Motion"
    assert camera.animation_data.action.name == "Camera Procedural Motion"

    cube.location = (0.0, 0.0, 0.0)
    cube.keyframe_insert("location", frame=1)
    cube.location = (2.0, 1.0, -1.0)
    cube.keyframe_insert("location", frame=3)
    baked = handler.bake_evaluated_animation(
        {
            "object_name": cube.name,
            "space": "LOCAL",
            "transforms": ["LOCATION"],
            "bone_names": [],
            "properties": [],
        },
        1,
        3,
        action_name="Cube Evaluated Bake",
        confirm_bake=True,
    )
    assert baked["action"] == "Cube Evaluated Bake"
    assert baked["new_non_shared_action"] is True
    assert baked["sampled_key_count"] == 9
    assert baked["key_count"] == 9
    driver_host = bpy.data.objects.new("DriverHost", None)
    bpy.context.scene.collection.objects.link(driver_host)
    driven = handler.manage_animation_driver(
        {"type": "OBJECT", "name": driver_host.name},
        "ADD",
        "location",
        array_index=2,
        driver_type="SCRIPTED",
        expression="frame * 0.25 + lift",
        variables=[
            {
                "name": "lift",
                "type": "TRANSFORMS",
                "target": {"type": "OBJECT", "name": cube.name},
                "transform_type": "LOC_Z",
                "transform_space": "WORLD_SPACE",
            }
        ],
    )
    assert driven["expression"] == "frame * 0.25 + lift"
    # Blender itself must accept the expression: a rejected one leaves the driver invalid and
    # the channel unevaluated, so evaluate it rather than trusting the reply.
    bpy.context.scene.frame_set(8)
    evaluated = driver_host.evaluated_get(bpy.context.evaluated_depsgraph_get())
    assert abs(evaluated.location.z - (8 * 0.25 + cube.matrix_world.translation.z)) < 1e-5

    _check_rig_control_properties(handler)

    print("ANIMATION_SMOKE_OK")


if __name__ == "__main__":
    main()
