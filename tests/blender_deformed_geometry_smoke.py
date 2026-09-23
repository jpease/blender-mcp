"""
Blender 5.1+ background smoke coverage for `sample_deformed_geometry`.

The unit suite proves the handler's bookkeeping against a fake `bpy`; only the real
dependency graph can prove the number it reports is the surface an armature actually
produced. Everything here is built from scratch in the factory-startup scene - an armature,
a strip of vertices, weights assigned by hand - so nothing is read from, saved to, or
compared against any existing .blend file.

What it falsifies: reading `obj.data` instead of `evaluated_get(...).to_mesh()` leaves the
posed frame reporting zero displacement, which is exactly the blind spot this tool closes.

Run with::

    blender --background --factory-startup --python tests/blender_deformed_geometry_smoke.py
"""

# Blender runtime types are dynamic in this executable harness.

import importlib
import math
import sys
import types

from pathlib import Path

import bpy

from mathutils import Quaternion, Vector

addon_path = Path(__file__).resolve().parents[1] / "src" / "blender_mcp" / "bundled" / "addon"
package_name = "blender_mcp_deformed_geometry_smoke"
addon = types.ModuleType(package_name)
addon.__path__ = [str(addon_path)]
addon.ADDON_ID = package_name
sys.modules[package_name] = addon
CharacterRiggingHandlersMixin = importlib.import_module(
    f"{package_name}.handlers.character_rigging"
).CharacterRiggingHandlersMixin

# A metre of rig is ~1e7 float32 ulps wide, so a tenth of a micrometre is the floor for a
# position that has been through the depsgraph and a matrix round trip.
POSITION_TOLERANCE = 1e-6
REST_FRAME = 1
POSED_FRAME = 20
BEND_DEGREES = 40.0


class DeformedGeometrySmokeHarness(CharacterRiggingHandlersMixin):
    """Expose character-rigging handlers without starting the socket server."""


def build_rig(name, location):
    """
    Build a two-bone chain: a lower bone at rest and an upper bone that will be posed.

    Args:
        name: Object name for the armature.
        location: Where to stand the rig, deliberately off the world origin so a world-space
            answer cannot pass by coinciding with the local one.

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
    bpy.context.view_layer.update()
    return rig


# One column of vertices: the lower half rides `lower` and stays put, the upper half rides
# `upper` and swings with it. Fully weighted either way, so the deformed position of every
# vertex is one bone matrix applied to one rest position - arithmetic this script can redo.
REST_COORDINATES = [
    (0.0, 0.0, 0.0),
    (0.0, 0.0, 0.5),
    (0.0, 0.0, 1.0),
    (0.0, 0.0, 1.5),
    (0.0, 0.0, 2.0),
]
UPPER_VERTICES = (3, 4)


def build_skin(name, rig):
    """
    Build the strip and bind it to the rig with explicit, fully normalized weights.

    Automatic weights are deliberately avoided: a hand-assigned weight of 1.0 makes the
    expected deformed position exact rather than approximately whatever Blender solved.

    Args:
        name: Object name for the mesh.
        rig: The armature object to bind to.

    Returns:
        The mesh object.

    """
    mesh = bpy.data.meshes.new(f"{name}Data")
    mesh.from_pydata(
        list(REST_COORDINATES),
        [(index, index + 1) for index in range(len(REST_COORDINATES) - 1)],
        [],
    )
    mesh.update()
    body = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(body)
    body.location = (0.25, 0.0, 0.0)
    lower_group = body.vertex_groups.new(name="lower")
    upper_group = body.vertex_groups.new(name="upper")
    for index in range(len(REST_COORDINATES)):
        group = upper_group if index in UPPER_VERTICES else lower_group
        group.add([index], 1.0, "REPLACE")  # pyright: ignore[reportArgumentType]
    modifier = body.modifiers.new(name="Armature", type="ARMATURE")
    modifier.object = rig
    bpy.context.view_layer.update()
    return body


def expected_world_position(rig, body, bone_name, vertex_index):
    """
    Recompute one fully weighted vertex's deformed world position from the bone matrices.

    This is Blender's own skinning formula for a single influence of weight 1:
    the bone's posed matrix times the inverse of its rest matrix, in armature space.

    Args:
        rig: The armature object.
        body: The deformed mesh object.
        bone_name: The single bone weighting this vertex.
        vertex_index: Index into the base mesh.

    Returns:
        Vector: Where the vertex should land, in world space.

    """
    pose_bone = rig.pose.bones[bone_name]
    skin = pose_bone.matrix @ pose_bone.bone.matrix_local.inverted()
    rest_armature_space = rig.matrix_world.inverted() @ (body.matrix_world @ Vector(REST_COORDINATES[vertex_index]))
    return rig.matrix_world @ (skin @ rest_armature_space)


handler = DeformedGeometrySmokeHarness()
rig = build_rig("SmokeRig", (1.5, -0.5, 0.0))
body = build_skin("SmokeBody", rig)
scene = bpy.context.scene
scene.frame_set(REST_FRAME)

base_before = [tuple(vertex.co) for vertex in body.data.vertices]
mesh_datablocks_before = len(bpy.data.meshes)

at_rest = handler.sample_deformed_geometry("SmokeBody")
assert at_rest["index_correspondence"] == "BASE_MESH", at_rest["index_correspondence"]
assert at_rest["evaluated_deformation_included"] is True
assert at_rest["displacement"]["measured"] is True
assert at_rest["displacement"]["moved_vertices"] == 0, at_rest["displacement"]
assert at_rest["displacement"]["maximum_m"] < POSITION_TOLERANCE, at_rest["displacement"]

# Key a rest pose and a bent pose, so the playhead - not this script - decides the shape.
upper = rig.pose.bones["upper"]
upper.rotation_mode = "QUATERNION"
upper.rotation_quaternion = Quaternion((1.0, 0.0, 0.0, 0.0))
upper.keyframe_insert("rotation_quaternion", frame=REST_FRAME)
upper.rotation_quaternion = Quaternion((1.0, 0.0, 0.0), math.radians(BEND_DEGREES))
upper.keyframe_insert("rotation_quaternion", frame=POSED_FRAME)
scene.frame_set(REST_FRAME)
bpy.context.view_layer.update()

posed = handler.sample_deformed_geometry("SmokeBody", frame=POSED_FRAME)

assert scene.frame_current == REST_FRAME, f"the playhead was left on {scene.frame_current}"
assert posed["frame"] == POSED_FRAME, posed["frame"]
assert posed["displacement"]["measured"] is True
assert posed["displacement"]["moved_vertices"] == len(UPPER_VERTICES), posed["displacement"]
bend = posed["displacement"]["maximum_m"]
assert bend > 0.5, f"a {BEND_DEGREES} degree bend moved the tip only {bend:.4f} m"

# The base mesh is untouched, which is why every other reader in this domain answers
# identically before and after the pose - and why this tool had to exist.
assert [tuple(vertex.co) for vertex in body.data.vertices] == base_before
assert len(bpy.data.meshes) == mesh_datablocks_before, "an evaluated mesh was left in bpy.data"

scene.frame_set(POSED_FRAME)
bpy.context.view_layer.update()
for record in posed["vertices"]["items"]:
    index = record["index"]
    bone = "upper" if index in UPPER_VERTICES else "lower"
    expected = expected_world_position(rig, body, bone, index)
    reported = Vector(record["co"])
    assert (reported - expected).length < POSITION_TOLERANCE, (
        f"vertex {index} reported {tuple(reported)}, skinning puts it at {tuple(expected)}"
    )
    rest_world = body.matrix_world @ Vector(REST_COORDINATES[index])
    assert abs(record["displacement_m"] - (expected - rest_world).length) < POSITION_TOLERANCE, record
scene.frame_set(REST_FRAME)

local = handler.sample_deformed_geometry("SmokeBody", frame=POSED_FRAME, space="LOCAL")
assert local["coordinate_space"] == "EVALUATED_OBJECT_LOCAL"
tip_world = Vector(posed["vertices"]["items"][4]["co"])
tip_local = Vector(local["vertices"]["items"][4]["co"])
assert ((body.matrix_world @ tip_local) - tip_world).length < POSITION_TOLERANCE, "LOCAL is not the object's own space"

# A generative modifier renumbers the evaluated mesh, so per-vertex correspondence is gone.
# Reported as such rather than silently pairing index i with a base vertex it is not.
mirror = body.modifiers.new(name="Mirror", type="MIRROR")
mirror.use_axis = (False, True, False)
bpy.context.view_layer.update()
mirrored = handler.sample_deformed_geometry("SmokeBody", frame=POSED_FRAME)
assert mirrored["index_correspondence"] == "EVALUATED_ONLY", mirrored["index_correspondence"]
assert mirrored["displacement"] == {
    "measured": False,
    "reason": "EVALUATED_VERTEX_COUNT_DIFFERS_FROM_BASE",
}, mirrored["displacement"]
assert mirrored["evaluated_counts"]["vertices"] > mirrored["base_counts"]["vertices"]
assert "displacement_m" not in mirrored["vertices"]["items"][0]
body.modifiers.remove(mirror)
bpy.context.view_layer.update()

# An armature with no surface of its own is refused by name rather than answered emptily.
try:
    handler.sample_deformed_geometry("SmokeRig")
except ValueError as error:
    assert "not a mesh" in str(error), str(error)
else:
    raise AssertionError("sampling an armature should be refused")

# Which meshes this rig deforms - the prerequisite for naming one above, and previously
# obtainable in a posing session only by moving a camera to frame them. A prop parented to the
# rig for transport rides it without being skinned to it, and must not be in the answer.
prop = bpy.data.objects.new("SmokeProp", bpy.data.meshes.new("SmokePropData"))
bpy.context.scene.collection.objects.link(prop)
prop.parent = rig
bpy.context.view_layer.update()

bound = handler.list_character_bones("SmokeRig", deformed_meshes=True)["deformed_meshes"]

assert [item["object"] for item in bound["items"]] == ["SmokeBody"], bound
assert bound["items"][0] == {"object": "SmokeBody", "binding": "MODIFIER", "modifier_enabled": True}, bound
assert (bound["total"], bound["truncated"]) == (1, False), bound
assert "deformed_meshes" not in handler.list_character_bones("SmokeRig"), "the section must be opt-in"
# A mesh disabled in the viewport still binds; the flag says why it would not move.
body.modifiers["Armature"].show_viewport = False
bpy.context.view_layer.update()
disabled = handler.list_character_bones("SmokeRig", deformed_meshes=True)["deformed_meshes"]
assert disabled["items"][0]["modifier_enabled"] is False, disabled
body.modifiers["Armature"].show_viewport = True

print(f"rest max displacement {at_rest['displacement']['maximum_m']:.3e} m")
print(f"posed max displacement {bend:.4f} m over {posed['displacement']['sampled_vertices']} sampled vertices")
print(
    f"mirrored evaluated vertices {mirrored['evaluated_counts']['vertices']}"
    f" vs base {mirrored['base_counts']['vertices']}"
)
print("DEFORMED_GEOMETRY_SMOKE_OK")
