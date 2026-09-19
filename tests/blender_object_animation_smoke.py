# ruff: file-ignore[module-import-not-at-top-of-file]
"""Run with Blender 5.1+ to smoke-test generic object transform keyframing."""

import importlib.util
import math
import sys

from pathlib import Path

import bpy

addon_path = Path(__file__).resolve().parents[1] / "src" / "blender_mcp" / "bundled" / "addon" / "__init__.py"
package_name = "blender_mcp_object_animation_smoke"
spec = importlib.util.spec_from_file_location(
    package_name,
    addon_path,
    submodule_search_locations=[str(addon_path.parent)],
)
assert spec is not None
addon = importlib.util.module_from_spec(spec)
sys.modules[package_name] = addon
spec.loader.exec_module(addon)

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

    print("OBJECT_ANIMATION_SMOKE_OK")


if __name__ == "__main__":
    main()
