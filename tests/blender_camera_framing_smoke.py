# ruff: file-ignore[module-import-not-at-top-of-file]
"""
Blender 5.1+ background smoke coverage for framing a camera on objects and on rig bones.

Run with::

    blender --background --factory-startup --python tests/blender_camera_framing_smoke.py
"""

import importlib.util
import math
import sys

from pathlib import Path

import bpy
import mathutils

from bpy_extras.object_utils import world_to_camera_view

addon_path = Path(__file__).resolve().parents[1] / "src" / "blender_mcp" / "bundled" / "addon" / "__init__.py"
package_name = "blender_mcp_camera_framing_smoke"
spec = importlib.util.spec_from_file_location(
    package_name, addon_path, submodule_search_locations=[str(addon_path.parent)]
)
assert spec is not None
addon = importlib.util.module_from_spec(spec)
sys.modules[package_name] = addon
spec.loader.exec_module(addon)

from blender_mcp_camera_framing_smoke.handlers.camera import CameraHandlersMixin

PLAUSIBLE_LENS_MM = (15.0, 60.0)
LENS_TOLERANCE_MM = 1e-6
POSITION_TOLERANCE_M = 1e-5


def _new_object(name: str, data: bpy.types.ID | None = None) -> bpy.types.Object:
    obj = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def _box_mesh(name: str, half_x: float, half_y: float, top_z: float) -> bpy.types.Mesh:
    mesh = bpy.data.meshes.new(name)
    corners = [(x, y, z) for x in (-half_x, half_x) for y in (-half_y, half_y) for z in (0.0, top_z)]
    mesh.from_pydata(corners, [], [(0, 1, 3, 2), (4, 5, 7, 6), (0, 1, 5, 4), (2, 3, 7, 6), (0, 2, 6, 4), (1, 3, 7, 5)])
    return mesh


def _projected(camera_object: bpy.types.Object, point) -> mathutils.Vector:
    """Where a world point lands in the render frame, in Blender's own 0..1 camera coordinates."""
    bpy.context.view_layer.update()
    return world_to_camera_view(bpy.context.scene, camera_object, mathutils.Vector(point))


def _inside_frame(projection: mathutils.Vector) -> bool:
    return 0.0 <= projection.x <= 1.0 and 0.0 <= projection.y <= 1.0 and projection.z > 0.0


def _evaluated_corners(obj: bpy.types.Object) -> list[mathutils.Vector]:
    """Read the object's world-space bound-box corners, as the depsgraph evaluates it."""
    bpy.context.view_layer.update()
    evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    # bpy's stub types a bound_box entry as a float rather than the 3-component array it is,
    # and `Matrix @ Vector` as a Matrix rather than the rotated Vector it returns.
    return [
        evaluated.matrix_world @ mathutils.Vector(corner)  # pyright: ignore[reportArgumentType]
        for corner in evaluated.bound_box
    ]


scene = bpy.context.scene
handler = CameraHandlersMixin()

# A 1.6 x 0.8 x 1.2 m group 2.1 m in front of the camera: two children, as in a shot.
group_mesh = bpy.data.meshes.new("Framing Group Mesh")
corners = [(x, y, z) for x in (-0.8, 0.8) for y in (-0.4, 0.4) for z in (0.0, 1.2)]
faces = [(0, 1, 3, 2), (4, 5, 7, 6), (0, 1, 5, 4), (2, 3, 7, 6), (0, 2, 6, 4), (1, 3, 7, 5)]
group_mesh.from_pydata(corners, [], faces)
group = _new_object("Framing Group", group_mesh)
camera = _new_object("Framing Camera", bpy.data.cameras.new("Framing Camera Data"))
camera.location = (0.0, -2.1, 0.6)
camera.rotation_euler = (1.5708, 0.0, 0.0)
camera.data.lens = 30.0
bpy.context.view_layer.update()

# CHANGE_LENS keeps the camera where it is and solves the focal length that fits the subject.
framed = handler.frame_camera_on_objects(scene.name, camera.name, [group.name], margin=0.12, policy="CHANGE_LENS")
low, high = PLAUSIBLE_LENS_MM
assert low < framed["lens"] < high, f"solved lens {framed['lens']} mm is not a plausible framing"
assert abs(camera.data.lens - framed["lens"]) < LENS_TOLERANCE_MM, "the reported lens is not the camera's lens"

# ---------------------------------------------------------------------------------------------
# Bone targets: a close-up is requested by naming a bone, not by guessing which meshes cover it.
# ---------------------------------------------------------------------------------------------

# A 12 m wall, so that anything but a tight framing on the bone leaves its far end in shot.
wall = _new_object("Framing Wall", _box_mesh("Framing Wall Mesh", 6.0, 0.1, 3.0))
WALL_FAR_CORNER = (-6.0, 0.1, 3.0)

rig_data = bpy.data.armatures.new("Framing Rig Data")
rig = _new_object("Framing Rig", rig_data)
rig.location = (4.0, 0.0, 0.0)
bpy.context.view_layer.objects.active = rig
bpy.ops.object.mode_set(mode="EDIT")
segment = rig_data.edit_bones.new("segment")
segment.head = (0.0, 0.0, 1.50)
segment.tail = (0.0, 0.0, 1.75)
probe = rig_data.edit_bones.new("probe")
probe.head = (0.0, 0.0, 0.0)
probe.tail = (0.0, 0.0, 0.4)
bpy.ops.object.mode_set(mode="OBJECT")
bpy.context.view_layer.update()

SEGMENT_HEAD_WORLD = (4.0, 0.0, 1.50)
SEGMENT_TAIL_WORLD = (4.0, 0.0, 1.75)
BONE_RADIUS_M = 0.05

bone_camera = _new_object("Bone Camera", bpy.data.cameras.new("Bone Camera Data"))
bone_camera.location = (4.0, -3.0, 1.6)
bone_camera.rotation_euler = (1.5708, 0.0, 0.0)
bone_camera.data.lens = 50.0
bpy.context.view_layer.update()

close_up = handler.frame_camera_on_objects(
    scene.name,
    bone_camera.name,
    bone_targets=[{"object_name": rig.name, "bone_name": "segment", "radius_m": BONE_RADIUS_M}],
    margin=0.05,
)

assert close_up["objects"] == [], "a bone-only framing must not claim to have framed any object"
record = close_up["bone_targets"][0]
assert record["object_name"] == rig.name and record["bone_name"] == "segment"
assert record["radius_m"] == BONE_RADIUS_M
for reported, expected in ((record["head_world"], SEGMENT_HEAD_WORLD), (record["tail_world"], SEGMENT_TAIL_WORLD)):
    assert all(abs(a - b) < POSITION_TOLERANCE_M for a, b in zip(reported, expected, strict=True)), (
        f"reported bone point {reported} is not the evaluated world point {expected}"
    )

# The bone is framed: both ends, and the radius that padded them, land inside the render frame.
for label, point in (
    ("head", SEGMENT_HEAD_WORLD),
    ("tail", SEGMENT_TAIL_WORLD),
    ("padded head", (SEGMENT_HEAD_WORLD[0] + BONE_RADIUS_M, 0.0, SEGMENT_HEAD_WORLD[2])),
    ("padded tail", (SEGMENT_TAIL_WORLD[0] - BONE_RADIUS_M, 0.0, SEGMENT_TAIL_WORLD[2])),
):
    projection = _projected(bone_camera, point)
    assert _inside_frame(projection), f"the bone's {label} projects outside the render frame at {list(projection)}"

# ...and the framing is a close-up, not the whole wall the bone stands against.
far = _projected(bone_camera, WALL_FAR_CORNER)
assert not _inside_frame(far), f"the wall's far corner is still in shot at {list(far)}; the framing is not tight"

# Object and bone targets together frame their union, so the wall returns to shot.
union = handler.frame_camera_on_objects(
    scene.name,
    bone_camera.name,
    [wall.name],
    bone_targets=[{"object_name": rig.name, "bone_name": "segment", "radius_m": BONE_RADIUS_M}],
    margin=0.05,
)
assert union["objects"] == [wall.name] and len(union["bone_targets"]) == 1
for label, point in (("far corner", WALL_FAR_CORNER), ("bone tail", SEGMENT_TAIL_WORLD)):
    projection = _projected(bone_camera, point)
    assert _inside_frame(projection), f"the union framing dropped the {label} at {list(projection)}"

# ---------------------------------------------------------------------------------------------
# Armature names: a whole-body frame is one rig name, not a hand-enumerated list of its meshes.
# ---------------------------------------------------------------------------------------------

CHARACTER_ORIGIN_X = -10.0

character_rig_data = bpy.data.armatures.new("Character Rig Data")
character_rig = _new_object("Character Rig", character_rig_data)
character_rig.location = (CHARACTER_ORIGIN_X, 0.0, 0.0)
bpy.context.view_layer.objects.active = character_rig
bpy.ops.object.mode_set(mode="EDIT")
spine = character_rig_data.edit_bones.new("spine")
spine.head = (0.0, 0.0, 0.90)
spine.tail = (0.0, 0.0, 1.70)
bpy.ops.object.mode_set(mode="OBJECT")
bpy.context.view_layer.update()

# The body is bound by an ARMATURE modifier and weighted to the bone; the coat is bound by
# ARMATURE parenting. Both are the shape the camera sees and neither is the shape of the bone:
# the spine is a 0.8 m line with no width, while the character it deforms stands 1.8 m tall and
# is 2.2 m across, exactly as a real rig's bones sit inside the silhouette rather than spanning it.
character_body = _new_object("Character Body", _box_mesh("Character Body Mesh", 0.45, 0.25, 1.8))
character_body.location = (CHARACTER_ORIGIN_X, 0.0, 0.0)
spine_group = character_body.vertex_groups.new(name="spine")
# bpy's stub types VertexGroup.add's index argument as a bpy_prop_array; it takes any int sequence.
spine_group.add([vertex.index for vertex in character_body.data.vertices], 1.0, "REPLACE")  # pyright: ignore[reportArgumentType]
body_skin = character_body.modifiers.new(name="Armature", type="ARMATURE")
body_skin.object = character_rig

character_coat = _new_object("Character Coat", _box_mesh("Character Coat Mesh", 1.1, 0.3, 1.2))
character_coat.parent = character_rig
character_coat.parent_type = "ARMATURE"
character_coat.location = (0.0, 0.0, 0.3)

# An unbound prop standing with the character: an expansion must frame the rig, not the scene.
bystander = _new_object("Character Bystander", _box_mesh("Character Bystander Mesh", 0.4, 0.4, 2.4))
bystander.location = (CHARACTER_ORIGIN_X + 6.0, 0.0, 0.0)
bpy.context.view_layer.update()

# Two cameras in the same place, so the two framings below start from identical state.
character_camera = _new_object("Character Camera", bpy.data.cameras.new("Character Camera Data"))
character_camera.location = (CHARACTER_ORIGIN_X, -5.0, 0.9)
character_camera.rotation_euler = (1.5708, 0.0, 0.0)
character_camera.data.lens = 50.0
armature_only_camera = _new_object("Armature Only Camera", bpy.data.cameras.new("Armature Only Camera Data"))
armature_only_camera.location = (CHARACTER_ORIGIN_X, -5.0, 0.9)
armature_only_camera.rotation_euler = (1.5708, 0.0, 0.0)
armature_only_camera.data.lens = 50.0
bpy.context.view_layer.update()

whole_character = handler.frame_camera_on_objects(
    scene.name,
    character_camera.name,
    armature_names=[character_rig.name],
    margin=0.05,
    policy="CHANGE_LENS",
)

assert whole_character["objects"] == [], "an armature-only framing must not claim to have framed a named object"
assert whole_character["armature_meshes"] == {character_rig.name: [character_body.name, character_coat.name]}, (
    f"the rig resolved to {whole_character['armature_meshes']}, not to both of its deformed meshes"
)

# Every corner of every deformed mesh, evaluated, is in shot: the silhouette is what was framed.
for mesh_object in (character_body, character_coat):
    for corner in _evaluated_corners(mesh_object):
        projection = _projected(character_camera, corner)
        assert _inside_frame(projection), (
            f"{mesh_object.name} has an evaluated corner outside the render frame at {list(projection)}"
        )

# ...and the bystander it does not deform stayed out of it.
bystander_near_corner = min(_evaluated_corners(bystander), key=lambda corner: corner.x)
assert not _inside_frame(_projected(character_camera, bystander_near_corner)), (
    "the expansion swept in a mesh the armature does not deform"
)

# Framing the armature object itself is not the same request, which is the point of the argument:
# its bounds are its bones, so it solves a longer lens and leaves the character's own coat out.
armature_only = handler.frame_camera_on_objects(
    scene.name,
    armature_only_camera.name,
    [character_rig.name],
    margin=0.05,
    policy="CHANGE_LENS",
)
assert whole_character["lens"] < armature_only["lens"], (
    f"framing the rig's meshes solved {whole_character['lens']} mm, no wider than the armature's own "
    f"{armature_only['lens']} mm"
)
coat_far_corner = max(_evaluated_corners(character_coat), key=lambda corner: corner.x)
assert not _inside_frame(_projected(armature_only_camera, coat_far_corner)), (
    "framing the armature object alone already contained the coat, so the comparison proves nothing"
)

# CHANGE_ORTHO_SCALE is the only policy an orthographic camera can be framed with.
ortho_camera = _new_object("Ortho Camera", bpy.data.cameras.new("Ortho Camera Data"))
ortho_camera.data.type = "ORTHO"
ortho_camera.location = (0.0, -4.0, 0.6)
ortho_camera.rotation_euler = (1.5708, 0.0, 0.0)
bpy.context.view_layer.update()

orthographic = handler.frame_camera_on_objects(
    scene.name, ortho_camera.name, [group.name], margin=0.1, policy="CHANGE_ORTHO_SCALE"
)
assert 1.6 <= orthographic["ortho_scale"] <= 3.0, f"solved ortho scale {orthographic['ortho_scale']} is not snug"
assert abs(ortho_camera.data.ortho_scale - orthographic["ortho_scale"]) < LENS_TOLERANCE_MM
for corner in group.bound_box:
    # bpy's stub types a bound_box entry as a float rather than the 3-component array it is.
    projection = _projected(ortho_camera, group.matrix_world @ mathutils.Vector(corner))  # pyright: ignore[reportArgumentType]
    assert _inside_frame(projection), f"an orthographic framing left a corner at {list(projection)}"

# ---------------------------------------------------------------------------------------------
# Refusals leave the camera exactly as it was.
# ---------------------------------------------------------------------------------------------

before_matrix = bone_camera.matrix_world.copy()
before_lens = bone_camera.data.lens
refusals = [
    ("no subject of any kind", {}),
    ("a bone target on a mesh", {"bone_targets": [{"object_name": wall.name, "bone_name": "segment"}]}),
    ("a bone the armature lacks", {"bone_targets": [{"object_name": rig.name, "bone_name": "absent"}]}),
    ("a missing object", {"bone_targets": [{"object_name": "Nothing Here", "bone_name": "segment"}]}),
    ("an armature that deforms no mesh", {"armature_names": [rig.name]}),
    ("an armature name on a mesh", {"armature_names": [wall.name]}),
    ("the same armature twice", {"armature_names": [character_rig.name, character_rig.name]}),
]
for label, kwargs in refusals:
    try:
        handler.frame_camera_on_objects(scene.name, bone_camera.name, **kwargs)
    except ValueError:
        pass
    else:
        raise AssertionError(f"framing accepted {label}")
    assert bone_camera.matrix_world == before_matrix, f"the camera moved while refusing {label}"
    assert bone_camera.data.lens == before_lens, f"the camera's lens changed while refusing {label}"

# A refusal raised after the solve has begun unwinds through the same restore.
away_camera = _new_object("Away Camera", bpy.data.cameras.new("Away Camera Data"))
away_camera.location = (0.0, -5.0, 0.6)
away_camera.rotation_euler = (1.5708, 0.0, math.pi)
away_camera.data.lens = 35.0
bpy.context.view_layer.update()
away_matrix = away_camera.matrix_world.copy()
try:
    handler.frame_camera_on_objects(
        scene.name, away_camera.name, [group.name], policy="CHANGE_LENS", aim_at_center=False
    )
except ValueError:
    pass
else:
    raise AssertionError("framing accepted a subject entirely behind the camera")
assert away_camera.matrix_world == away_matrix, "a mid-solve refusal left the camera moved"
assert abs(away_camera.data.lens - 35.0) < LENS_TOLERANCE_MM, "a mid-solve refusal left the lens changed"

# ---------------------------------------------------------------------------------------------
# point_camera_at(subtarget=...) aims at the bone's evaluated world head, not the rig's origin.
# ---------------------------------------------------------------------------------------------

anchor = _new_object("Probe Anchor")
anchor.location = (1.0, 2.0, 3.0)
pinned = rig.pose.bones["probe"].constraints.new("COPY_LOCATION")
pinned.target = anchor
bpy.context.view_layer.update()

aimed = handler.point_camera_at(
    scene.name,
    away_camera.name,
    target_object_name=rig.name,
    subtarget="probe",
    camera_location=(1.0, -2.0, 3.0),
)
assert all(abs(a - b) < POSITION_TOLERANCE_M for a, b in zip(aimed["target_point"], (1.0, 2.0, 3.0), strict=True)), (
    f"subtarget aimed at {aimed['target_point']}, not the constrained bone head [1.0, 2.0, 3.0]"
)
aim_direction = (mathutils.Vector((1.0, 2.0, 3.0)) - away_camera.matrix_world.translation).normalized()
camera_forward = (away_camera.matrix_world.to_3x3() @ mathutils.Vector((0.0, 0.0, -1.0))).normalized()
# bpy's stub types `Matrix @ Vector` as a Matrix; at runtime it is the rotated Vector.
assert aim_direction.dot(camera_forward) > 1.0 - 1e-6, "the camera was not rotated onto the bone it reported"  # pyright: ignore[reportArgumentType]

print("CAMERA_FRAMING_SMOKE_OK")
