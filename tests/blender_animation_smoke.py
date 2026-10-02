# ruff: file-ignore[module-import-not-at-top-of-file]
"""Run with Blender 5.1+ to smoke-test generic layered Action handlers."""

import math
import sys

from pathlib import Path

import bpy
import mathutils

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


def _world(obj):
    return obj.evaluated_get(bpy.context.evaluated_depsgraph_get()).matrix_world.copy()


def _matrix_error(first, second) -> float:
    return max(
        abs(a - b) for row_a, row_b in zip(first, second, strict=True) for a, b in zip(row_a, row_b, strict=True)
    )


def _animated_arm(name: str):
    """Build an armature whose Root turns between frames 1 and 10, with a Hand bone under it."""
    arm = bpy.data.objects.new(name, bpy.data.armatures.new(name))
    bpy.context.scene.collection.objects.link(arm)
    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode="EDIT")
    root = arm.data.edit_bones.new("Root")
    root.head, root.tail = (0.0, 0.0, 0.0), (0.0, 0.0, 1.0)
    hand = arm.data.edit_bones.new("Hand")
    hand.head, hand.tail = (0.0, 0.0, 1.0), (0.0, 1.0, 1.0)
    hand.parent = root
    bpy.ops.object.mode_set(mode="OBJECT")
    arm.location = (3.0, 0.0, 0.0)
    root_bone = arm.pose.bones["Root"]
    root_bone.rotation_mode = "XYZ"
    for frame, angle in ((1, 0.0), (10, 1.2)):
        root_bone.rotation_euler = (0.0, 0.0, angle)
        root_bone.keyframe_insert("rotation_euler", frame=frame)
    return arm


def _check_a_bake_holds_the_evaluated_motion_once_constraints_are_muted(handler) -> None:
    """
    Bake constrained motion through a bone parent and delta transforms, then mute the constraints.

    Keys sampled from matrix_basis drop the constraint; keys divided by the parent object's
    matrix alone ignore the bone the prop hangs from and its deltas; and a bone's channels taken
    from parent-times-rest ignore a bone that does not inherit rotation. Each puts the muted
    playback somewhere other than where the evaluated pose was.
    """
    scene = bpy.context.scene
    target = bpy.data.objects.new("BakeTarget", None)
    scene.collection.objects.link(target)
    target.rotation_euler = (0.3, 0.2, 0.1)

    holder = _animated_arm("BakeHolder")
    prop = bpy.data.objects.new("BakeProp", None)
    scene.collection.objects.link(prop)
    prop.parent, prop.parent_type, prop.parent_bone = holder, "BONE", "Hand"
    prop.location = (0.1, 0.2, 0.3)
    prop.delta_location = (0.0, 0.5, 0.0)
    prop.delta_rotation_euler = (0.0, 0.0, 0.4)
    prop.delta_scale = (1.5, 1.5, 1.5)
    prop.empty_display_size = 0.75
    prop_constraint = prop.constraints.new("COPY_ROTATION")
    prop_constraint.target = target
    wanted = {}
    for frame in (1, 5, 10):
        scene.frame_set(frame)
        wanted[frame] = _world(prop)
    baked = handler.bake_evaluated_animation(
        {
            "object_name": prop.name,
            "transforms": ["LOCATION", "ROTATION", "SCALE"],
            "properties": [{"data_path": "empty_display_size"}],
        },
        1,
        10,
        action_name="PropBake",
        confirm_bake=True,
    )
    assert "sample_space" not in baked
    assert any(curve["data_path"] == "empty_display_size" for curve in baked["curves"]), baked["curves"]
    prop_constraint.mute = True
    for frame, matrix in wanted.items():
        scene.frame_set(frame)
        error = _matrix_error(_world(prop), matrix)
        assert error < 1e-4, f"muted bone-parented prop left its evaluated pose by {error} at frame {frame}"

    arm = _animated_arm("BakeArm")
    arm.data.bones["Hand"].use_inherit_rotation = False
    hand_constraint = arm.pose.bones["Hand"].constraints.new("COPY_ROTATION")
    hand_constraint.target = target
    wanted = {}
    for frame in (1, 5, 10):
        scene.frame_set(frame)
        evaluated = arm.evaluated_get(bpy.context.evaluated_depsgraph_get())
        wanted[frame] = evaluated.matrix_world @ evaluated.pose.bones["Hand"].matrix
    handler.bake_evaluated_animation(
        {"object_name": arm.name, "transforms": ["LOCATION", "ROTATION", "SCALE"], "bone_names": ["Root", "Hand"]},
        1,
        10,
        action_name="ArmBake",
        confirm_bake=True,
        confirm_displace_action=True,
    )
    hand_constraint.mute = True
    for frame, matrix in wanted.items():
        scene.frame_set(frame)
        evaluated = arm.evaluated_get(bpy.context.evaluated_depsgraph_get())
        error = _matrix_error(evaluated.matrix_world @ evaluated.pose.bones["Hand"].matrix, matrix)
        assert error < 1e-4, f"muted non-inheriting bone left its evaluated pose by {error} at frame {frame}"

    mesh = bpy.data.meshes.new("BakeTri")
    mesh.from_pydata([(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)], [], [(0, 1, 2)])
    tri = bpy.data.objects.new("BakeTri", mesh)
    scene.collection.objects.link(tri)
    pinned = bpy.data.objects.new("BakeVertexChild", None)
    scene.collection.objects.link(pinned)
    pinned.parent, pinned.parent_type, pinned.parent_vertices = tri, "VERTEX_3", (0, 1, 2)
    try:
        handler.bake_evaluated_animation(
            {"object_name": pinned.name, "transforms": ["LOCATION"]}, 1, 2, action_name="VertexBake", confirm_bake=True
        )
    except ValueError as refusal:
        assert "parented to vertices" in str(refusal), refusal
    else:
        raise AssertionError("a vertex-parented object was baked through a parent matrix nobody can invert")
    assert bpy.data.actions.get("VertexBake") is None


def _check_a_bake_keeps_rotation_continuous_through_180_degrees(handler) -> None:
    """
    Bake a turn through 180 degrees in each rotation mode and read it between two keys.

    A decomposed matrix wraps Euler angles to +-180, picks a quaternion's sign per sample, and
    folds an axis-angle back under 180 by flipping its axis, so neighbouring baked keys can sit on
    opposite branches; the curve between them then spins the long way round and the in-between
    pose faces the opposite way.
    """
    scene = bpy.context.scene
    for mode in ("XYZ", "QUATERNION", "AXIS_ANGLE"):
        turner = bpy.data.objects.new(f"Turner{mode}", None)
        scene.collection.objects.link(turner)
        turner.rotation_mode = mode
        for frame, degrees in ((1, 170.0), (2, 190.0)):
            if mode == "QUATERNION":
                turner.rotation_quaternion = mathutils.Euler((0.0, 0.0, math.radians(degrees))).to_quaternion()
                turner.keyframe_insert("rotation_quaternion", frame=frame)
            elif mode == "AXIS_ANGLE":
                turner.rotation_axis_angle = (math.radians(degrees), 0.0, 0.0, 1.0)
                turner.keyframe_insert("rotation_axis_angle", frame=frame)
            else:
                turner.rotation_euler = (0.0, 0.0, math.radians(degrees))
                turner.keyframe_insert("rotation_euler", frame=frame)
        handler.bake_evaluated_animation(
            {"object_name": turner.name, "transforms": ["ROTATION"]},
            1,
            2,
            action_name=f"TurnBake{mode}",
            confirm_bake=True,
            confirm_displace_action=True,
        )
        scene.frame_set(1, subframe=0.5)
        facing = _world(turner).to_3x3() @ mathutils.Vector((1.0, 0.0, 0.0))
        assert facing.x < -0.99, f"{mode} bake turned the long way: halfway it faces {tuple(facing)}"


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
    bake_target = {
        "object_name": cube.name,
        "transforms": ["LOCATION"],
        "bone_names": [],
        "properties": [],
    }
    # The bake assigns its new action, which would leave the keyed CubeAction driving nothing.
    keyed_motion = cube.animation_data.action
    try:
        handler.bake_evaluated_animation(bake_target, 1, 3, action_name="Cube Evaluated Bake", confirm_bake=True)
    except ValueError as refusal:
        assert "confirm_displace_action=True" in str(refusal), refusal
    else:
        raise AssertionError("a bake displaced keyed motion without confirm_displace_action")
    assert cube.animation_data.action == keyed_motion
    assert bpy.data.actions.get("Cube Evaluated Bake") is None
    baked = handler.bake_evaluated_animation(
        bake_target, 1, 3, action_name="Cube Evaluated Bake", confirm_bake=True, confirm_displace_action=True
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
    _check_a_bake_holds_the_evaluated_motion_once_constraints_are_muted(handler)
    _check_a_bake_keeps_rotation_continuous_through_180_degrees(handler)

    print("ANIMATION_SMOKE_OK")


if __name__ == "__main__":
    main()
