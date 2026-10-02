"""
Blender 5.1+ background smoke coverage for `sample_evaluated_range`.

The unit suite proves the handler's refusals and paging against a fake `bpy`; only the real
dependency graph can prove the numbers it reports are the performance Blender evaluates. A cube
keyed to sink through a static slab must read zero penetration until it touches and then a
count and depth that grow; a keyed bone's tail must match `matrix_world @ pose_bone.tail`
recomputed here at each frame; and the playhead must come back to the exact frame and subframe
it left. Everything is built from scratch in the factory-startup scene.

Run with::

    blender --background --factory-startup --python tests/blender_evaluated_range_smoke.py
"""

# Blender runtime types are dynamic in this executable harness.

import importlib
import math
import sys

from pathlib import Path

import bpy

from mathutils import Quaternion

sys.path.append(str(Path(__file__).resolve().parent))
from smoke_addon import load_addon

package_name = "blender_mcp_evaluated_range_smoke"
load_addon(package_name)
CharacterRiggingHandlersMixin = importlib.import_module(
    f"{package_name}.handlers.character_rigging"
).CharacterRiggingHandlersMixin

# The reply rounds to a tenth of a millimetre, so half of that bounds an honest rounding error.
ROUNDING_TOLERANCE = 0.5e-4 + 1e-7
FIRST_FRAME = 1
LAST_FRAME = 21
PARKED_FRAME = 5
PARKED_SUBFRAME = 0.5


class EvaluatedRangeSmokeHarness(CharacterRiggingHandlersMixin):
    """Expose character-rigging handlers without starting the socket server."""


def box(name, minimum, maximum):
    """
    Build a closed, outward-facing box mesh object between two world corners.

    Args:
        name: Object name.
        minimum: The box's lowest corner.
        maximum: The box's highest corner.

    Returns:
        The mesh object, linked into the scene at the origin.

    """
    (x0, y0, z0), (x1, y1, z1) = minimum, maximum
    vertices = [
        (x0, y0, z0),
        (x1, y0, z0),
        (x1, y1, z0),
        (x0, y1, z0),
        (x0, y0, z1),
        (x1, y0, z1),
        (x1, y1, z1),
        (x0, y1, z1),
    ]
    faces = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
    mesh = bpy.data.meshes.new(f"{name}Data")
    mesh.from_pydata(vertices, [], faces)
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def build_rig(name, location):
    """
    Build a two-bone chain whose upper bone is keyed to swing a quarter turn about X.

    Args:
        name: Object name for the armature.
        location: Where to stand it, off the origin so world and armature space differ.

    Returns:
        The armature object.

    """
    data = bpy.data.armatures.new(f"{name}Data")
    rig = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(rig)
    bpy.context.view_layer.objects.active = rig
    rig.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    lower = data.edit_bones.new("lower")
    lower.head, lower.tail = (0.0, 0.0, 0.0), (0.0, 0.0, 1.0)
    upper = data.edit_bones.new("upper")
    upper.head, upper.tail = (0.0, 0.0, 1.0), (0.0, 0.0, 2.0)
    upper.parent, upper.use_connect = lower, True
    bpy.ops.object.mode_set(mode="OBJECT")
    rig.select_set(False)
    rig.location = location
    pose_bone = rig.pose.bones["upper"]
    pose_bone.rotation_mode = "QUATERNION"
    pose_bone.rotation_quaternion = Quaternion((1.0, 0.0, 0.0, 0.0))
    pose_bone.keyframe_insert("rotation_quaternion", frame=FIRST_FRAME)
    pose_bone.rotation_quaternion = Quaternion((1.0, 0.0, 0.0), math.radians(90.0))
    pose_bone.keyframe_insert("rotation_quaternion", frame=LAST_FRAME)
    return rig


scene = bpy.context.scene
handler = EvaluatedRangeSmokeHarness()

# A slab a metre thick whose top is the floor at z = 0, and a unit cube keyed to sink from
# resting 1 m above it to half its height below it.
floor = box("SmokeFloor", (-2.0, -2.0, -1.0), (2.0, 2.0, 0.0))
cube = box("SmokeCube", (-0.5, -0.5, -0.5), (0.5, 0.5, 0.5))
cube.location = (0.0, 0.0, 1.5)
cube.keyframe_insert("location", frame=FIRST_FRAME)
cube.location = (0.0, 0.0, 0.0)
cube.keyframe_insert("location", frame=LAST_FRAME)
# An open surface: a lone quad. Parity means nothing against it, and the reply must say so.
sheet = bpy.data.objects.new("SmokeSheet", bpy.data.meshes.new("SmokeSheetData"))
sheet.data.from_pydata([(-1.0, -1.0, 0.2), (1.0, -1.0, 0.2), (1.0, 1.0, 0.2), (-1.0, 1.0, 0.2)], [], [(0, 1, 2, 3)])
scene.collection.objects.link(sheet)
rig = build_rig("SmokeRig", (1.5, -0.5, 0.25))
bpy.context.view_layer.update()

scene.frame_set(PARKED_FRAME, subframe=PARKED_SUBFRAME)
meshes_before = len(bpy.data.meshes)
objects_before = len(bpy.data.objects)

reply = handler.sample_evaluated_range(
    frame_start=FIRST_FRAME,
    frame_end=LAST_FRAME,
    bone_points=[{"armature_object_name": "SmokeRig", "bone_name": "upper"}],
    mesh_metrics={"object_names": ["SmokeCube"], "against_object_names": ["SmokeFloor"]},
    limit=LAST_FRAME,
)

assert scene.frame_current == PARKED_FRAME, f"the playhead was left on {scene.frame_current}"
assert math.isclose(scene.frame_subframe, PARKED_SUBFRAME, abs_tol=1e-6), scene.frame_subframe
assert reply["timeline_restored"]["frame"] == PARKED_FRAME
assert len(bpy.data.meshes) == meshes_before, "an evaluated mesh was left behind"
assert len(bpy.data.objects) == objects_before
assert reply["inside_test"] == "RAY_PARITY"
assert "warnings" not in reply, reply.get("warnings")
items = reply["samples"]["items"]
assert [item["frame"] for item in items] == list(range(FIRST_FRAME, LAST_FRAME + 1))
assert reply["samples"]["truncated"] is False

# Recompute each frame independently, straight from Blender, after the sampler is done.
expected = {}
for frame in range(FIRST_FRAME, LAST_FRAME + 1):
    scene.frame_set(frame)
    pose_bone = rig.pose.bones["upper"]
    expected[frame] = {
        "tail": rig.matrix_world @ pose_bone.tail,
        "head": rig.matrix_world @ pose_bone.head,
        "bottom": cube.matrix_world.translation.z - 0.5,
    }
scene.frame_set(PARKED_FRAME, subframe=PARKED_SUBFRAME)

previous_depth = 0.0
previous_tail = None
first_contact = None
for item in items:
    frame = item["frame"]
    bone = item["bones"][0]
    for got, want in ((bone["tail_world"], expected[frame]["tail"]), (bone["head_world"], expected[frame]["head"])):
        assert all(abs(got[axis] - want[axis]) <= ROUNDING_TOLERANCE for axis in range(3)), (frame, got, list(want))
    if previous_tail is not None and frame > FIRST_FRAME:
        assert bone["tail_world"] != previous_tail, f"the tail did not move into frame {frame}"
    previous_tail = bone["tail_world"]

    mesh = item["meshes"][0]
    bottom = expected[frame]["bottom"]
    assert mesh["object_name"] == "SmokeCube", mesh
    assert abs(mesh["world_bounds"]["minimum"][2] - bottom) <= ROUNDING_TOLERANCE, (frame, mesh, bottom)
    penetration = mesh["penetration"][0]
    assert penetration["against_object_name"] == "SmokeFloor"
    if bottom >= -1e-5:
        assert penetration == {"against_object_name": "SmokeFloor", "inside_vertices": 0, "max_depth_m": 0.0}, (
            frame,
            penetration,
        )
    else:
        first_contact = first_contact or frame
        # The four bottom corners, each as deep below the floor's top as the cube's bottom face.
        assert penetration["inside_vertices"] == 4, (frame, penetration)
        assert abs(penetration["max_depth_m"] + bottom) <= ROUNDING_TOLERANCE, (frame, penetration, bottom)
        assert penetration["max_depth_m"] > previous_depth, (frame, penetration, previous_depth)
        previous_depth = penetration["max_depth_m"]
assert first_contact is not None and first_contact > FIRST_FRAME, first_contact
assert math.isclose(previous_depth, 0.5, abs_tol=ROUNDING_TOLERANCE), previous_depth

# A page is the frames it returns: the second page of three resumes where the first stopped.
paged = handler.sample_evaluated_range(
    frames=[1, 11, 21], bone_points=[{"armature_object_name": "SmokeRig", "bone_name": "upper"}], limit=2, offset=2
)
assert [item["frame"] for item in paged["samples"]["items"]] == [21]
assert paged["samples"]["truncated"] is False and paged["samples"]["next_offset"] is None
assert scene.frame_current == PARKED_FRAME

# An open against surface is measured but named as unreliable.
opened = handler.sample_evaluated_range(
    frames=[1], mesh_metrics={"object_names": ["SmokeCube"], "against_object_names": ["SmokeSheet"]}
)
assert any("SmokeSheet has open edges" in warning for warning in opened["warnings"]), opened

# Refusals land before the playhead moves.
for arguments, message in (
    ({"frames": [1], "frame_start": 1, "frame_end": 2}, "exactly one of"),
    ({"frame_start": 1, "frame_end": 1000}, "Raise frame_step to at least 4"),
    ({"frames": [1], "bone_points": [{"armature_object_name": "SmokeRig", "bone_name": "missing"}]}, "missing"),
    ({"frames": [1], "mesh_metrics": {"object_names": ["SmokeRig"]}}, "not a mesh"),
):
    try:
        handler.sample_evaluated_range(**arguments)
    except ValueError as error:
        assert message in str(error), str(error)
    else:
        raise AssertionError(f"{arguments} should be refused")
    assert scene.frame_current == PARKED_FRAME
    assert math.isclose(scene.frame_subframe, PARKED_SUBFRAME, abs_tol=1e-6)

print(f"first contact at frame {first_contact}, deepest {previous_depth} m at frame {LAST_FRAME}")
print(f"tail at frame {LAST_FRAME}: {items[-1]['bones'][0]['tail_world']}")
print("EVALUATED_RANGE_SMOKE_OK")
