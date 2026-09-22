"""
Blender 5.1+ background smoke coverage for camera handlers.

Run with::

    blender --background --factory-startup --python tests/blender_camera_smoke.py
"""

import importlib.util
import sys

from pathlib import Path

import bpy
import mathutils

addon_path = Path(__file__).resolve().parents[1] / "src" / "blender_mcp" / "bundled" / "addon" / "__init__.py"
package_name = "blender_mcp_camera_smoke"
spec = importlib.util.spec_from_file_location(
    package_name, addon_path, submodule_search_locations=[str(addon_path.parent)]
)
assert spec is not None
addon = importlib.util.module_from_spec(spec)
sys.modules[package_name] = addon
spec.loader.exec_module(addon)

from blender_mcp_camera_smoke.handlers.camera import (  # ruff: ignore[module-import-not-at-top-of-file]
    CameraHandlersMixin,
)
from blender_mcp_camera_smoke.handlers.camera._shared import _tag  # ruff: ignore[module-import-not-at-top-of-file]


def _new_object(name, data=None):
    obj = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(obj)
    return obj


scene = bpy.context.scene
handler = CameraHandlersMixin()
root = _new_object("Smoke Rig Root")
camera = _new_object("Smoke Camera", bpy.data.cameras.new("Smoke Camera Data"))
target = _new_object("Smoke Target")
destination = _new_object("Smoke Match", bpy.data.cameras.new("Smoke Match Data"))
rig_id = "smoke-rig"
_tag(root, rig_id, "root")
_tag(camera, rig_id, "camera")
camera.parent = root
camera.location = (0.0, -8.0, 2.0)
camera.rotation_mode = "QUATERNION"
camera.rotation_quaternion = (target.location - camera.matrix_world.translation).to_track_quat("-Z", "Y")
scene.camera = camera

key_result = handler.keyframe_camera_rig(
    [
        {"object_name": camera.name, "owner": "CAMERA_DATA", "data_path": "lens", "value": 40.0, "frame": 1},
        {"object_name": camera.name, "owner": "CAMERA_DATA", "data_path": "lens", "value": 80.0, "frame": 20},
    ],
    interpolation="LINEAR",
)
assert len(key_result["keyframes"]) == 2
interpolation_result = handler.set_camera_interpolation(
    camera.name, "CAMERA_DATA", "lens", 1, 20, interpolation="CONSTANT"
)
assert len(interpolation_result["changed_keys"]) == 2

scene.frame_set(7)
focus_result = handler.create_focus_pull(
    scene.name,
    camera.name,
    1,
    20,
    start_point=(0.0, 0.0, 0.0),
    end_point=(0.0, 2.0, 0.0),
)
assert scene.frame_current == 7
assert focus_result["mode"] == "DISTANCE"
# The documented DISTANCE contract: keys the camera's own focus distance, enables DOF, and adds
# no object. A rehearsal read the tool as adding a focus control in every mode and went looking
# for one that is never built here.
assert focus_result["focus_control"] is None
assert not [obj for obj in bpy.data.objects if obj.get("mcp_camera_role") == "focus_pull"]
assert camera.data.dof.use_dof is True, "create_focus_pull owns the switch"
assert camera.data.dof.focus_object is None

# The other half of that ownership split: configure_camera_dof leaves the switch alone, so a
# focus set while it is off renders sharp. Silence there is what made the two tools look alike.
camera.data.dof.use_dof = False
dof_result = handler.configure_camera_dof(scene.name, camera.name, {}, focus_distance=4.0)
assert camera.data.dof.use_dof is False, "configure_camera_dof must not enable DOF behind the caller"
assert any("use_dof" in warning for warning in dof_result["warnings"]), dof_result["warnings"]
enabled = handler.configure_camera_dof(scene.name, camera.name, {"use_dof": True}, focus_distance=4.0)
assert camera.data.dof.use_dof is True
assert not any("use_dof is off" in warning for warning in enabled["warnings"]), enabled["warnings"]

# Keywords, not positions: `start_at_seconds`/`end_at_seconds` were added between
# the frame and distance parameters, so a positional call passes distances as times.
dolly_result = handler.create_dolly_zoom(
    scene.name,
    camera.name,
    root.name,
    start_frame=1,
    end_frame=20,
    start_distance=8.0,
    end_distance=16.0,
    subject_point=(0.0, 0.0, 0.0),
    subject_reference_size=2.0,
    start_lens=80.0,
)
assert abs(dolly_result["solutions"][1]["lens"] - 160.0) < 1e-6
assert (
    abs(
        dolly_result["solutions"][0]["projected_frame_fraction"]
        - dolly_result["solutions"][1]["projected_frame_fraction"]
    )
    < 1e-6
)
assert scene.frame_current == 7

marker_result = handler.create_camera_markers(
    scene.name,
    "CREATE",
    [{"name": "Smoke Shot", "frame": 1, "camera_name": camera.name}],
)
assert marker_result["camera_cuts"][0]["camera"] == camera.name
# A marker sitting on frame_start claims no frame retroactively, so it warns about nothing.
assert marker_result["warnings"] == []
assert handler.create_camera_markers(scene.name, "LIST")["changed_objects"] == []
moved = handler.create_camera_markers(scene.name, "UPDATE", [{"name": "Smoke Shot", "frame": 5}])
assert scene.timeline_markers["Smoke Shot"].frame == 5
assert len(moved["warnings"]) == 1
assert f"frames {scene.frame_start}-4 render through '{camera.name}'" in moved["warnings"][0]

# The binding that warning describes, measured rather than remembered: frame 1 holds no marker of
# its own, and Blender still resolves it to the frame-5 marker's camera instead of the scene's.
previous_frame = scene.frame_current
scene.camera = destination
scene.frame_set(1)
assert scene.camera.name == camera.name
scene.frame_set(previous_frame)
scene.camera = camera

match_result = handler.match_camera_transform(destination.name, "FULL", source_object_name=camera.name)
assert match_result["destination"] == destination.name
assert destination.data.lens == camera.data.lens

duplicate_result = handler.duplicate_camera_rig(
    scene.name,
    root.name,
    "Smoke Rigs",
    "Smoke Duplicate",
)
assert duplicate_result["rig_id"] != rig_id
assert len(duplicate_result["members"]) == 2

constraint_result = handler.add_camera_constraint(
    scene.name,
    destination.name,
    "Smoke Copy Location",
    "COPY_LOCATION",
    target_name=target.name,
)
assert constraint_result["constraint"]["type"] == "COPY_LOCATION"

target.location = (3.0, 4.0, 5.0)
point_result = handler.point_camera_at(scene.name, camera.name, target_object_name=target.name)
expected_rotation = (target.location - camera.matrix_world.translation).to_track_quat("-Z", "Y")
assert all(abs(a - b) < 1e-6 for a, b in zip(camera.rotation_quaternion, expected_rotation, strict=True))
assert point_result["target_object"] == target.name

# ---------------------------------------------------------------------------------------------
# create_camera(target_bone_name=...) aims at the bone's evaluated world head. A character rig's
# object origin is the floor under the character, so an object-only aim frames the top of a head.
# ---------------------------------------------------------------------------------------------

hero_rig_data = bpy.data.armatures.new("Smoke Hero Rig Data")
hero_rig = _new_object("Smoke Hero Rig", hero_rig_data)
hero_rig.location = (6.0, 0.0, 0.0)
bpy.context.view_layer.objects.active = hero_rig
bpy.ops.object.mode_set(mode="EDIT")
head_bone = hero_rig_data.edit_bones.new("head")
head_bone.head = (0.0, 0.0, 1.60)
head_bone.tail = (0.0, 0.0, 1.85)
bpy.ops.object.mode_set(mode="OBJECT")
# Constrained rather than merely posed: the aim has to read the depsgraph-evaluated bone, and a
# constraint is what moves it away from the rest position its edit bone was built at.
head_anchor = _new_object("Smoke Head Anchor")
head_anchor.location = (6.0, 0.0, 1.70)
pinned_head = hero_rig.pose.bones["head"].constraints.new("COPY_LOCATION")
pinned_head.target = head_anchor
bpy.context.view_layer.update()

HEAD_WORLD = mathutils.Vector((6.0, 0.0, 1.70))
face_result = handler.create_camera(
    scene.name,
    "Smoke Rigs",
    "Smoke Face Cam",
    location=(6.0, -3.0, 1.70),
    target_object_name=hero_rig.name,
    target_bone_name="head",
)
face_camera = bpy.data.objects[face_result["object"]]
face_direction = (HEAD_WORLD - face_camera.matrix_world.translation).normalized()
# bpy's stub types `Matrix @ Vector` as a Matrix; at runtime it is the rotated Vector.
face_forward = (face_camera.matrix_world.to_3x3() @ mathutils.Vector((0.0, 0.0, -1.0))).normalized()
assert face_direction.dot(face_forward) > 1.0 - 1e-6, (  # pyright: ignore[reportArgumentType]
    "create_camera(target_bone_name=...) did not aim at the constrained bone head"
)

# The same call without the bone aims 1.7 m lower, at the rig's origin on the floor.
floor_result = handler.create_camera(
    scene.name, "Smoke Rigs", "Smoke Floor Cam", location=(6.0, -3.0, 1.70), target_object_name=hero_rig.name
)
floor_camera = bpy.data.objects[floor_result["object"]]
floor_forward = (floor_camera.matrix_world.to_3x3() @ mathutils.Vector((0.0, 0.0, -1.0))).normalized()
assert floor_forward.z < -0.4, "the object-only aim did not point down at the rig's floor origin"  # pyright: ignore[reportAttributeAccessIssue]

# A refusal after the datablocks exist removes both of them: no orphan object, no orphan data.
try:
    handler.create_camera(scene.name, "Smoke Rigs", "Smoke Orphan Cam", optics={"panorama_type": "EQUIRECTANGULAR"})
except ValueError:
    pass
else:
    raise AssertionError("create_camera accepted panorama_type on a PERSP projection")
assert bpy.data.objects.get("Smoke Orphan Cam") is None, "a refused creation left the camera object behind"
assert bpy.data.cameras.get("Smoke Orphan Cam Data") is None, "a refused creation left the camera data behind"

gate_result = handler.configure_camera_render_gate(
    scene.name,
    camera.name,
    render={"resolution_x": 1920, "resolution_y": 1080},
    border={"use_border": True, "min_x": 0.1, "max_x": 0.9},
    guides={"show_composition_thirds": True},
)
assert gate_result["new"]["render"]["resolution_x"] == 1920

camera_world = camera.matrix_world.copy()
shake_result = handler.add_camera_shake(scene.name, camera.name, "Smoke Rigs", "Smoke Shake", 1, 20)
shake = bpy.data.objects[shake_result["control"]]
assert camera.parent is shake
assert shake.parent is root
assert all(
    abs(camera.matrix_world[row][column] - camera_world[row][column]) < 1e-5 for row in range(4) for column in range(4)
)

validation = handler.validate_camera_rig(scene.name, [root.name, camera.name], [1, 20])
assert validation["sampled_frames"] == [1, 20]
assert scene.frame_current == 7
assert "visual correctness was not inferred" in validation["verification"]

handler.create_camera_markers(scene.name, "REMOVE", [{"name": "Smoke Shot"}])
assert scene.timeline_markers.get("Smoke Shot") is None

print("CAMERA_SMOKE_OK")
