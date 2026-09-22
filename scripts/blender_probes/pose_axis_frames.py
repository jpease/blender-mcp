"""
Measure whether `list_character_bones(rest_axes=True)` and `aim_at` mean the same thing by "X".

A demo runbook read a head bone's reported rest axes, concluded `up_axis: "-X"`, and got a head
tilted about 90 degrees; `up_axis: "Y"` was what actually worked on that rig. The proposed
mechanism was that the two surfaces report axes in different frames. This probe settles it by
measurement rather than by reading, on a synthetic bone built to carry exactly the rest axes that
rig reported: X = (0, 0, -1), Y = (1, 0, 0), Z = (0, -1, 0) in armature space.

It calls the shipped handlers, not raw `bpy`, and prints three things per case: where the bone's
tracked axis ended up relative to the target, where the axis `up_axis` named ended up relative to
world up, and where the axis that was up *at rest* ended up relative to world up. The last is the
one that decides whether the bone looks upright, and it is the one an `up_axis` choice is really
about.

Run with::

    blender --background --factory-startup --python scripts/blender_probes/pose_axis_frames.py
"""

# Blender runtime types are dynamic in this executable harness.

import importlib
import math
import sys
import types

from pathlib import Path

import bpy

from mathutils import Vector

addon_path = Path(__file__).resolve().parents[2] / "src" / "blender_mcp" / "bundled" / "addon"
package_name = "blender_mcp_pose_axis_probe"
addon = types.ModuleType(package_name)
addon.__path__ = [str(addon_path)]
addon.ADDON_ID = package_name
sys.modules[package_name] = addon
character_handlers = importlib.import_module(f"{package_name}.handlers.character_rigging")


class Harness(character_handlers.CharacterRiggingHandlersMixin):
    """Expose character-rigging handlers without starting the socket server."""


AXIS_INDEX = {"X": 0, "Y": 1, "Z": 2}
WORLD_UP = Vector((0.0, 0.0, 1.0))
# The rest axes the runbook recorded for CHAR1_head_jnt, as columns X, Y, Z in armature space.
WANTED_REST_AXES = ((0.0, 0.0, -1.0), (1.0, 0.0, 0.0), (0.0, -1.0, 0.0))
CHILD = "CHAR1_headEnd_jnt"


def build_rig(name: str) -> "bpy.types.Object":
    """
    Build a rig carrying WANTED_REST_AXES, plus a child bone rolled differently from it.

    Args:
        name: Object name for the rig; its armature datablock takes the same name with `Data`.

    Returns:
        bpy.types.Object: The linked armature object, back in Object Mode.

    """
    data = bpy.data.armatures.new(f"{name}Data")
    rig = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(rig)
    bpy.context.view_layer.objects.active = rig
    rig.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    bone = data.edit_bones.new("CHAR1_head_jnt")
    # Length along armature +X makes the bone's own Y armature +X; the roll then turns its own
    # X onto armature -Z, which is the rest relationship the runbook was reading.
    bone.head, bone.tail = (0.0, 0.0, 1.0), (0.12, 0.0, 1.0)
    bone.roll = math.pi / 2.0
    # A child whose own rest basis is nothing like its parent's, so "the bone's axes" and "the
    # parent's axes" cannot be confused for each other in what LOCAL_WITH_PARENT reports.
    child = data.edit_bones.new(CHILD)
    child.head, child.tail = (0.12, 0.0, 1.0), (0.12, 0.15, 1.05)
    child.parent, child.use_connect = bone, True
    child.roll = 0.6
    bpy.ops.object.mode_set(mode="OBJECT")
    rig.select_set(False)
    bpy.context.view_layer.update()
    return rig


def signed_axis_world(rig: "bpy.types.Object", bone_name: str, signed: str) -> Vector:
    """
    Point where a signed bone axis such as `-X` points in world space.

    Args:
        rig: The armature object the bone belongs to.
        bone_name: The pose bone to read.
        signed: A signed axis name: one of X, -X, Y, -Y, Z, -Z.

    Returns:
        Vector: That axis as a unit world direction.

    """
    sign = -1.0 if signed.startswith("-") else 1.0
    matrix = (rig.matrix_world @ rig.pose.bones[bone_name].matrix).to_3x3()
    index = AXIS_INDEX[signed.lstrip("-")]
    # Read the column off the rows: `Matrix.col[i]` is typed `None` by the mathutils stubs.
    return (Vector((matrix[0][index], matrix[1][index], matrix[2][index])) * sign).normalized()


def degrees_between(first: Vector, second: Vector) -> float:
    """
    Measure the angle between two directions.

    Args:
        first: One direction, of any length.
        second: The other.

    Returns:
        float: The angle in degrees. Every direction this probe measures comes from a
        normalisation, so a zero-length one would be a bug rather than a case to fall back for.

    """
    # The half-angle form, not `acos` of the dot product: near 0 and 180 degrees the dot is
    # 1 to within float round-off and `acos` turns that round-off into hundredths of a degree,
    # which is exactly the range this probe reports in. `Vector.angle` is stable the same way,
    # but the mathutils stubs type it `float | object`.
    first, second = first.normalized(), second.normalized()
    return math.degrees(2.0 * math.atan2((first - second).length, (first + second).length))


def rest_pose(rig: "bpy.types.Object") -> None:
    """
    Clear every pose channel, so the next aim starts from the rest relationship, not a pose.

    Args:
        rig: The armature object to reset.

    """
    for pose_bone in rig.pose.bones:
        pose_bone.matrix_basis.identity()
    bpy.context.view_layer.update()


handler = Harness()
rig = build_rig("ProbeRig")
BONE = "CHAR1_head_jnt"

print("=== BLENDER ===", bpy.app.version_string)

# --- 1. What list_character_bones(rest_axes=True) says ----------------------------------------

reported = handler.list_character_bones(rig.name, rest_axes=True)["bones"]["items"][0]["rest_axes"]
print("rest_axes:", reported)
for offset, letter in ((0, "X"), (3, "Y"), (6, "Z")):
    print(f"  {letter} = {tuple(reported[offset : offset + 3])}  wanted {WANTED_REST_AXES[offset // 3]}")
drift = max(abs(a - b) for a, b in zip(reported, [v for column in WANTED_REST_AXES for v in column], strict=True))
print(f"  max drift from the runbook's numbers: {drift:.6f}")

# Which signed bone axis is nearest world up at rest, and which runs along the bone's length.
rest_matrix = (rig.matrix_world @ rig.data.bones[BONE].matrix_local).to_3x3()
for signed in ("X", "-X", "Y", "-Y", "Z", "-Z"):
    sign = -1.0 if signed.startswith("-") else 1.0
    axis = (rest_matrix.col[AXIS_INDEX[signed.lstrip("-")]] * sign).normalized()
    print(
        f"  rest {signed:>2} -> world {tuple(round(v, 3) for v in axis)}, "
        f"{degrees_between(axis, WORLD_UP):6.2f} deg from +Z"
    )

# --- 2. Aim the same bone twice, changing only up_axis -----------------------------------------
#
# track_axis has to name a letter that is neither of the two up_axis candidates, or one of the
# two calls is refused for repeating the tracked axis. Z is the only such letter.

REST_UP = "-X"  # what the reported rest axes say points up
TRACK = "Z"
rest_pose(rig)
head_world = (rig.matrix_world @ rig.pose.bones[BONE].matrix).translation.copy()
rest_track = signed_axis_world(rig, BONE, TRACK)

for label, target in (
    ("target along the rest track axis (a no-op aim)", head_world + rest_track * 2.0),
    ("target 40 deg off the rest track axis", head_world + (rest_track * 2.0 + WORLD_UP * 0.0 + Vector((1.68, 0, 0)))),
):
    print(f"\ntrack_axis={TRACK!r}, {label}, target={tuple(round(v, 3) for v in target)}")
    for up_axis in (REST_UP, "Y"):
        rest_pose(rig)
        handler.set_character_pose(
            rig.name,
            [{"bone_name": BONE, "aim_at": {"target_point": tuple(target), "track_axis": TRACK, "up_axis": up_axis}}],
        )
        tracked = signed_axis_world(rig, BONE, TRACK)
        to_target = (target - (rig.matrix_world @ rig.pose.bones[BONE].matrix).translation).normalized()
        named_up = signed_axis_world(rig, BONE, up_axis)
        rest_up_now = signed_axis_world(rig, BONE, REST_UP)
        print(
            f"  up_axis={up_axis!r:>4}: aim error {degrees_between(tracked, to_target):7.4f} deg | "
            f"the axis up_axis named is {degrees_between(named_up, WORLD_UP):7.3f} deg from world +Z | "
            f"the bone's rest-up axis ({REST_UP}) is {degrees_between(rest_up_now, WORLD_UP):7.3f} deg from world +Z"
        )

# --- 3. The same question with the bone's length axis tracked ----------------------------------
#
# The natural head aim: point the bone's length (Y, per the reported rest axes) at a target.
# up_axis "Y" is refused there for repeating the tracked axis, so the candidates are -X and Z.

rest_pose(rig)
head_world = (rig.matrix_world @ rig.pose.bones[BONE].matrix).translation.copy()
target = head_world + Vector((2.0, 0.0, 0.0))
print(f"\ntrack_axis='Y' (the bone's length), target={tuple(round(v, 3) for v in target)}")
for up_axis in (REST_UP, "Z", "-Z"):
    rest_pose(rig)
    handler.set_character_pose(
        rig.name,
        [{"bone_name": BONE, "aim_at": {"target_point": tuple(target), "track_axis": "Y", "up_axis": up_axis}}],
    )
    rest_up_now = signed_axis_world(rig, BONE, REST_UP)
    print(
        f"  up_axis={up_axis!r:>4}: the bone's rest-up axis ({REST_UP}) is "
        f"{degrees_between(rest_up_now, WORLD_UP):7.3f} deg from world +Z"
    )

# --- 4. Which frame `rotate.axis` letters are in, per pose space -------------------------------
#
# `aim_at`'s letters always name the bone's own axes. `rotate`'s are documented as "the call's
# pose space", which is a different claim; measure what each space actually turns "Z" into.
# `relative=True` composes with what is there, so the delta from rest is a pure turn about
# whatever axis the letter resolved to - the plain set-to form replaces the orientation, and its
# delta from rest says nothing about the axis.

# The rig is turned, so armature space and world space differ and the two readings separate.
turned_rig = build_rig("ProbeRigTurned")
turned_rig.rotation_euler = (0.5, 0.0, 0.7)
turned_rig.location = (0.4, -0.2, 0.0)
bpy.context.view_layer.update()
for bone_name, label in ((BONE, "a root bone"), (CHILD, "a parented bone")):
    print(f"\nrotate.axis='Z', 30 degrees, relative, on {label} of a rig tilted 0.5/0.0/0.7 rad:")
    rest_pose(turned_rig)
    rest_world = (turned_rig.matrix_world @ turned_rig.pose.bones[bone_name].matrix).to_3x3().normalized()
    bone_rest_z = rest_world.col[2].normalized()
    parent = turned_rig.pose.bones[bone_name].parent
    parent_rest_z = None
    if parent is not None:
        parent_rest_z = (turned_rig.matrix_world @ parent.matrix).to_3x3().normalized().col[2].normalized()
    armature_z = turned_rig.matrix_world.to_3x3().col[2].normalized()
    print(f"  the bone's own rest Z in world: {tuple(round(v, 3) for v in bone_rest_z)}")
    if parent_rest_z is not None:
        print(f"  its parent's rest Z in world:   {tuple(round(v, 3) for v in parent_rest_z)}")
    print(f"  armature +Z in world:           {tuple(round(v, 3) for v in armature_z)}")
    for space in ("LOCAL", "LOCAL_WITH_PARENT", "POSE", "WORLD"):
        rest_pose(turned_rig)
        handler.set_character_pose(
            turned_rig.name,
            [{"bone_name": bone_name, "rotate": {"axis": "Z", "degrees": 30.0, "relative": True}}],
            space=space,
        )
        posed = (turned_rig.matrix_world @ turned_rig.pose.bones[bone_name].matrix).to_3x3().normalized()
        delta = (posed @ rest_world.inverted()).to_quaternion()
        axis = delta.axis.normalized()
        against = f"{degrees_between(axis, bone_rest_z):6.2f} from own rest Z"
        if parent_rest_z is not None:
            against += f" | {degrees_between(axis, parent_rest_z):6.2f} from parent's rest Z"
        print(
            f"  space={space:<18}: turned {math.degrees(delta.angle):6.2f} deg about world "
            f"{tuple(round(v, 3) for v in axis)} | {against} | "
            f"{degrees_between(axis, armature_z):6.2f} from armature +Z"
        )

print("\ndone")
