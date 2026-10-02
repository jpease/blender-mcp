# ruff: file-ignore[module-import-not-at-top-of-file]
"""Run with Blender 5.1+ to smoke-test generic object transform keyframing."""

import itertools
import math
import sys

from pathlib import Path

import bpy
import mathutils

sys.path.append(str(Path(__file__).resolve().parent))
from smoke_addon import load_addon

package_name = "blender_mcp_object_animation_smoke"
load_addon(package_name)

from blender_mcp_object_animation_smoke.handlers.object_animation import ObjectAnimationHandlersMixin

_action_fcurves = sys.modules[f"{package_name}.handlers.object_animation"]._action_fcurves

_RIG_LOCATION = (10.0, 0.0, 0.0)


def _new_object(name: str, *, location: tuple[float, float, float] = (0.0, 0.0, 0.0)):
    obj = bpy.data.objects.new(name, bpy.data.meshes.new(f"{name}Mesh"))
    obj.location = location
    bpy.context.scene.collection.objects.link(obj)
    return obj


def _fcurve(obj, data_path: str):
    _action, curves = _action_fcurves(obj)
    matches = [curve for curve in curves if curve.data_path == data_path]
    assert matches, f"no fcurve found for {obj.name}:{data_path}"
    return matches[0]


def _point_at(curve, frame: float):
    matches = [point for point in curve.keyframe_points if math.isclose(point.co[0], frame, abs_tol=1e-4)]
    assert matches, f"no keyframe at frame {frame} on {curve.data_path}"
    return matches[0]


def _channel_at(obj, data_path: str, frame: float) -> tuple[float, ...]:
    _action, curves = _action_fcurves(obj)
    by_index = {curve.array_index: curve for curve in curves if curve.data_path == data_path}
    return tuple(by_index[index].evaluate(frame) for index in range(len(by_index)))


def _orientation(value) -> mathutils.Quaternion:
    """Read a keyed rotation_quaternion (four values) or XYZ rotation_euler (three) as an orientation."""
    if len(value) == 4:
        return mathutils.Quaternion(value).normalized()
    return mathutils.Euler(value, "XYZ").to_quaternion()


def _arc(first: mathutils.Quaternion, second: mathutils.Quaternion) -> float:
    """Measure the angle between two orientations, whichever hemisphere either is spelled in."""
    return 2.0 * math.acos(min(1.0, abs(first.normalized().dot(second.normalized()))))


def _yaw_records(obj, path: str, yaws) -> list[dict]:
    records = []
    for frame, degrees in yaws:
        euler = mathutils.Euler((0.0, 0.0, math.radians(degrees)), "XYZ")
        value = tuple(euler.to_quaternion()) if path == "rotation_quaternion" else tuple(euler)
        records.append({"object_name": obj.name, "frame": frame, "space": "WORLD", path: value})
    return records


def _test_world_rotation_keys_take_the_short_way(handler) -> None:
    """
    Key a WORLD yaw across +-180 degrees and read the curve between the keys.

    `matrix_world` decomposes each key onto one branch - w >= 0 for a quaternion, +-180 degrees
    for an Euler - so 170 then 190 degrees came back as two spellings on opposite branches, and
    the curve between two correct poses spun 340 degrees the wrong way.
    """
    yaws = ((1.0, 170.0), (10.0, 190.0), (20.0, 200.0))
    for mode, path in (("QUATERNION", "rotation_quaternion"), ("XYZ", "rotation_euler")):
        obj = _new_object(f"AnimYaw{mode}")
        obj.rotation_mode = mode
        handler.keyframe_object_transform(keyframes=_yaw_records(obj, path, yaws))

        keyed = [_channel_at(obj, path, frame) for frame, _degrees in yaws]
        for (frame, degrees), value in zip(yaws, keyed, strict=True):
            asked = mathutils.Euler((0.0, 0.0, math.radians(degrees)), "XYZ").to_quaternion()
            assert _arc(_orientation(value), asked) < 1e-4, f"{mode} key at {frame} is not the pose asked for"
        for before, after in itertools.pairwise(keyed):
            if mode == "QUATERNION":
                dot = mathutils.Quaternion(before).dot(mathutils.Quaternion(after))
                assert dot >= 0.0, f"adjacent quaternion keys {before} and {after} sit on opposite hemispheres"
            else:
                assert abs(after[2] - before[2]) < math.pi, f"adjacent Euler Z keys {before[2]} and {after[2]} wrap"

        first, second = (_orientation(value) for value in keyed[:2])
        midway = _orientation(_channel_at(obj, path, 5.5))
        detour = _arc(first, midway) + _arc(midway, second) - _arc(first, second)
        assert detour < 1e-3, f"{mode}: the curve leaves the 20-degree arc between its keys by {math.degrees(detour)}"


def _test_world_rotation_keeps_a_deliberate_turn(handler) -> None:
    """Accumulate a turn of under 180 degrees per key past a full revolution instead of folding it back."""
    obj = _new_object("AnimSpin")
    yaws = ((1.0, 0.0), (10.0, 170.0), (20.0, 340.0), (30.0, 510.0))
    handler.keyframe_object_transform(keyframes=_yaw_records(obj, "rotation_euler", yaws))
    keyed = [math.degrees(_channel_at(obj, "rotation_euler", frame)[2]) for frame, _degrees in yaws]
    for (_frame, degrees), value in zip(yaws, keyed, strict=True):
        assert math.isclose(value, degrees, abs_tol=1e-3), f"WORLD turn keyed as {keyed}, not {[d for _f, d in yaws]}"


def _test_local_rotation_is_keyed_verbatim(handler) -> None:
    """LOCAL writes the caller's own spelling, so a single step past 180 degrees stays the step asked for."""
    euler_obj = _new_object("AnimLocalEuler")
    handler.keyframe_object_transform(
        keyframes=[
            {"object_name": euler_obj.name, "frame": 1.0, "space": "LOCAL", "rotation_euler": (0.0, 0.0, 0.0)},
            {"object_name": euler_obj.name, "frame": 10.0, "space": "LOCAL", "rotation_euler": (0.0, 0.0, 4.7)},
        ]
    )
    assert math.isclose(_channel_at(euler_obj, "rotation_euler", 10.0)[2], 4.7, abs_tol=1e-6)

    quat_obj = _new_object("AnimLocalQuat")
    quat_obj.rotation_mode = "QUATERNION"
    far_side = tuple(-component for component in mathutils.Euler((0.0, 0.0, 0.5)).to_quaternion())
    handler.keyframe_object_transform(
        keyframes=[
            {"object_name": quat_obj.name, "frame": 1.0, "space": "LOCAL", "rotation_quaternion": (1.0, 0.0, 0.0, 0.0)},
            {"object_name": quat_obj.name, "frame": 10.0, "space": "LOCAL", "rotation_quaternion": far_side},
        ]
    )
    keyed = _channel_at(quat_obj, "rotation_quaternion", 10.0)
    assert all(math.isclose(a, b, abs_tol=1e-6) for a, b in zip(keyed, far_side, strict=True)), keyed


def _test_world_and_local_space(handler, cube, rig, child) -> None:
    """WORLD space on an unparented object; WORLD space through a parent chain; LOCAL space direct sets."""
    handler.keyframe_object_transform(
        keyframes=[{"object_name": "AnimCube", "frame": 1, "space": "WORLD", "location": (2.0, 0.0, 5.0)}]
    )
    assert all(math.isclose(a, b, abs_tol=1e-6) for a, b in zip(cube.location, (2.0, 0.0, 5.0), strict=True))

    bpy.context.view_layer.update()
    handler.keyframe_object_transform(
        keyframes=[{"object_name": "AnimChild", "frame": 1, "space": "WORLD", "location": (15.0, 3.0, 0.0)}]
    )
    world = child.matrix_world.translation
    assert all(math.isclose(a, b, abs_tol=1e-4) for a, b in zip(world, (15.0, 3.0, 0.0), strict=True))
    assert math.isclose(child.location[0], 5.0, abs_tol=1e-4), "child.location should be parent-relative"
    assert all(math.isclose(a, b, abs_tol=1e-6) for a, b in zip(rig.location, _RIG_LOCATION, strict=True)), (
        "the parent itself should be untouched by keying its child"
    )
    # A WORLD key solves through the parent as it sits at the key's frame, not where the playhead
    # is: the parent travels 0 -> 10 over frames 1-10, so solved at frame 1 the child would land
    # at x=30 instead of x=20. The playhead, subframe included, goes back where it was.
    bpy.context.scene.frame_set(1)
    rig.location = (0.0, 0.0, 0.0)
    rig.keyframe_insert(data_path="location", frame=1)
    rig.location = (10.0, 0.0, 0.0)
    rig.keyframe_insert(data_path="location", frame=10)
    bpy.context.scene.frame_set(1, subframe=0.25)
    handler.keyframe_object_transform(
        keyframes=[{"object_name": "AnimChild", "frame": 10, "space": "WORLD", "location": (20.0, 3.0, 0.0)}]
    )
    playhead = (bpy.context.scene.frame_current, bpy.context.scene.frame_subframe)
    assert playhead == (1, 0.25), f"the playhead was left at {playhead}"
    bpy.context.scene.frame_set(10)
    world_10 = child.evaluated_get(bpy.context.evaluated_depsgraph_get()).matrix_world.translation
    assert math.isclose(world_10[0], 20.0, abs_tol=1e-4), f"a WORLD key at frame 10 plays at x={world_10[0]}"
    bpy.context.scene.frame_set(1)

    handler.keyframe_object_transform(
        keyframes=[
            {
                "object_name": "AnimCube",
                "frame": 10,
                "space": "LOCAL",
                "location": (1.0, 1.0, 1.0),
                "rotation_euler": (0.0, 0.0, math.radians(45.0)),
                "scale": (2.0, 2.0, 2.0),
            }
        ]
    )
    assert all(math.isclose(a, b, abs_tol=1e-6) for a, b in zip(cube.location, (1.0, 1.0, 1.0), strict=True))
    assert math.isclose(cube.rotation_euler[2], math.radians(45.0), abs_tol=1e-6)
    assert all(math.isclose(a, b, abs_tol=1e-6) for a, b in zip(cube.scale, (2.0, 2.0, 2.0), strict=True))


def _test_at_seconds_conversion(handler, cube) -> None:
    """at_seconds must convert through the scene's fps/frame_start, matching configure_scene_physics's own math."""
    handler.keyframe_object_transform(
        keyframes=[{"object_name": "AnimCube", "at_seconds": 5.0, "space": "LOCAL", "location": (9.0, 9.0, 9.0)}]
    )
    _point_at(_fcurve(cube, "location"), 121.0)


def _test_rotation_mode_enforcement(handler, quat_obj) -> None:
    try:
        handler.keyframe_object_transform(
            keyframes=[{"object_name": "AnimQuatObj", "frame": 1, "rotation_euler": (0.0, 0.0, 0.0)}]
        )
    except ValueError as exc:
        assert "rotation_quaternion" in str(exc)
    else:
        raise AssertionError("rotation_euler on a QUATERNION object should have been rejected")

    handler.keyframe_object_transform(
        keyframes=[{"object_name": "AnimQuatObj", "frame": 1, "rotation_quaternion": (1.0, 0.0, 0.0, 0.0)}]
    )
    assert quat_obj.animation_data is not None

    try:
        handler.keyframe_object_transform(
            keyframes=[{"object_name": "AnimAxisObj", "frame": 1, "rotation_euler": (0.0, 0.0, 0.0)}]
        )
    except ValueError as exc:
        assert "edit_keyframes" in str(exc)
    else:
        raise AssertionError("rotation_euler on an AXIS_ANGLE object should have been rejected")

    try:
        handler.keyframe_object_transform(
            keyframes=[{"object_name": "AnimAxisObj", "frame": 1, "rotation_quaternion": (1.0, 0.0, 0.0, 0.0)}]
        )
    except ValueError as exc:
        assert "rotation_euler" in str(exc)
    else:
        raise AssertionError("rotation_quaternion on an AXIS_ANGLE object should have been rejected")


def _test_insert_only_and_replace_existing(handler, cube) -> None:
    handler.keyframe_object_transform(
        keyframes=[{"object_name": "AnimCube", "frame": 50, "space": "LOCAL", "location": (3.0, 3.0, 3.0)}],
        policy="INSERT_ONLY",
    )
    try:
        handler.keyframe_object_transform(
            keyframes=[{"object_name": "AnimCube", "frame": 50, "space": "LOCAL", "location": (4.0, 4.0, 4.0)}],
            policy="INSERT_ONLY",
        )
    except ValueError as exc:
        assert "already exists" in str(exc)
    else:
        raise AssertionError("INSERT_ONLY should reject a duplicate key at the same frame")
    assert math.isclose(cube.location[0], 3.0, abs_tol=1e-6), "rejected INSERT_ONLY must leave state untouched"

    handler.keyframe_object_transform(
        keyframes=[{"object_name": "AnimCube", "frame": 50, "space": "LOCAL", "location": (4.0, 4.0, 4.0)}],
        policy="REPLACE_EXISTING",
    )
    assert math.isclose(_point_at(_fcurve(cube, "location"), 50.0).co[1], 4.0, abs_tol=1e-6)


def _test_interpolation_and_handle_styling(handler, cube) -> None:
    handler.keyframe_object_transform(
        keyframes=[{"object_name": "AnimCube", "frame": 70, "space": "LOCAL", "location": (0.0, 0.0, 0.0)}],
        interpolation="LINEAR",
    )
    assert _point_at(_fcurve(cube, "location"), 70.0).interpolation == "LINEAR"

    handler.keyframe_object_transform(
        keyframes=[{"object_name": "AnimCube", "frame": 80, "space": "LOCAL", "location": (0.0, 0.0, 0.0)}],
        interpolation="BEZIER",
        handle_left="VECTOR",
        handle_right="VECTOR",
    )
    styled = _point_at(_fcurve(cube, "location"), 80.0)
    assert styled.handle_left_type == "VECTOR"
    assert styled.handle_right_type == "VECTOR"


def _test_batch_validation(handler) -> None:
    """Duplicate destinations within one batch call must be rejected before any mutation; multi-object batches work."""
    try:
        handler.keyframe_object_transform(
            keyframes=[
                {"object_name": "AnimCube", "frame": 90, "space": "LOCAL", "location": (1.0, 0.0, 0.0)},
                {"object_name": "AnimCube", "frame": 90, "space": "LOCAL", "scale": (2.0, 2.0, 2.0)},
            ]
        )
    except ValueError as exc:
        assert "Duplicate keyframe destination" in str(exc)
    else:
        raise AssertionError("Two records targeting the same object+frame should have been rejected")

    result = handler.keyframe_object_transform(
        keyframes=[
            {"object_name": "AnimCube", "frame": 100, "space": "LOCAL", "location": (0.0, 0.0, 0.0)},
            {"object_name": "AnimRig", "frame": 100, "space": "LOCAL", "location": (0.0, 0.0, 0.0)},
        ]
    )
    assert set(result["changed_objects"]) == {"AnimCube", "AnimRig"}
    assert result["policy"] == "REPLACE_EXISTING"


def _test_a_key_outside_a_travelling_cycle_is_reported(handler, root) -> None:
    """
    Report the root-teleport defect in the tool that keys roots.

    A stride's travel is keyed over a handful of frames and cycled with REPEAT_OFFSET; the
    arrival is then keyed long after it. Blender redefines the period to the curve's new key
    extent and says nothing, so the walk stops repeating and every repeat carries the wrong
    distance. `keyframe_character_pose` has warned about this for pose bones; this is the same
    warning on the channel that moves the whole character.
    """
    for frame, distance in ((1.0, 0.0), (17.0, 2.1)):
        handler.keyframe_object_transform(
            keyframes=[{"object_name": root.name, "frame": frame, "space": "LOCAL", "location": (0.0, distance, 0.0)}],
            action_name="RootTravel",
        )
    curve = _fcurve(root, "location")
    modifier = curve.modifiers.new(type="CYCLES")
    modifier.mode_after = "REPEAT_OFFSET"

    quiet = handler.keyframe_object_transform(
        keyframes=[{"object_name": root.name, "frame": 9.0, "space": "LOCAL", "location": (0.0, 1.0, 0.0)}],
        action_name="RootTravel",
    )
    stretched = handler.keyframe_object_transform(
        keyframes=[{"object_name": root.name, "frame": 199.0, "space": "LOCAL", "location": (0.0, 9.0, 0.0)}],
        action_name="RootTravel",
    )

    assert quiet["warnings"] == [], f"a key inside the cycle warned anyway: {quiet['warnings']}"
    warning = next(text for text in stretched["warnings"] if "cycles over" in text)
    assert f"'{root.name}'.location is keyed at frame 199" in warning, warning
    assert "period becomes 198 frames instead of 16" in warning, warning
    # Warned, not refused: the key landed, and Blender now repeats the period the notice named.
    assert any(math.isclose(point.co[0], 199.0, abs_tol=1e-4) for point in curve.keyframe_points), warning
    print(f"cycle extension: {warning}")


def main() -> None:
    """Exercise LOCAL/WORLD keying, rotation-mode enforcement, at_seconds, policies, and interpolation styling."""
    handler = ObjectAnimationHandlersMixin()
    scene = bpy.context.scene
    scene.render.fps = 24
    scene.render.fps_base = 1.0
    scene.frame_start = 1

    cube = _new_object("AnimCube")
    rig = _new_object("AnimRig", location=_RIG_LOCATION)
    child = _new_object("AnimChild")
    child.parent = rig
    quat_obj = _new_object("AnimQuatObj")
    quat_obj.rotation_mode = "QUATERNION"
    axis_obj = _new_object("AnimAxisObj")
    axis_obj.rotation_mode = "AXIS_ANGLE"

    _test_world_and_local_space(handler, cube, rig, child)
    _test_at_seconds_conversion(handler, cube)
    _test_rotation_mode_enforcement(handler, quat_obj)
    _test_insert_only_and_replace_existing(handler, cube)
    _test_interpolation_and_handle_styling(handler, cube)
    _test_batch_validation(handler)
    _test_world_rotation_keys_take_the_short_way(handler)
    _test_world_rotation_keeps_a_deliberate_turn(handler)
    _test_local_rotation_is_keyed_verbatim(handler)
    _test_a_key_outside_a_travelling_cycle_is_reported(handler, _new_object("AnimRoot"))

    print("OBJECT_ANIMATION_SMOKE_OK")


if __name__ == "__main__":
    main()
