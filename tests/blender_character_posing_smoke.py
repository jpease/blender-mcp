"""
Blender 5.1+ background smoke coverage for pose resolution, aims, and pose keyframing.

Everything here is measured against the real API rather than the unit suite's fake `bpy`:
whether an aim actually lands on its target, whether a child posed in an absolute space plays
back where it was posed, and whether the action the handler authored is still in the file after
a save, a reset, and a reopen. The unit tests can only prove the geometry the handler asks for.

Run with::

    blender --background --factory-startup --python tests/blender_character_posing_smoke.py
"""

# Blender runtime types are dynamic in this executable harness.

import importlib
import json
import math
import shutil
import sys
import tempfile

from pathlib import Path

import bpy

from mathutils import Vector

sys.path.append(str(Path(__file__).resolve().parent))
from smoke_addon import load_addon

package_name = "blender_mcp_character_posing_smoke"
load_addon(package_name)
character_handlers = importlib.import_module(f"{package_name}.handlers.character_rigging")
CharacterRiggingHandlersMixin = character_handlers.CharacterRiggingHandlersMixin

AIM_TOLERANCE_DEGREES = 1e-6
# A metre of rig is 1e7 float32 ulps wide, so a tenth of a micrometre is the floor for a
# position that has been through a matrix inverse and back.
POSITION_TOLERANCE = 1e-6
AXIS_ROUNDING = 5e-4
# Blender's IK stops at its own convergence threshold rather than at float precision, so a
# solved reach lands micrometres from its target where a matrix round-trip lands nanometres
# from it. Measured at 3.3e-5 m for the head/neck/spine chain below, Blender 5.2.2, 500
# iterations; POSITION_TOLERANCE is what the exact-arithmetic checks use and stays at 1e-6.
# This is the precision this shot asks for and is passed to the handler as `tolerance_m`:
# the handler judges convergence, and the assertions below read the flag it reported rather
# than re-deciding here what "close enough" means.
REACH_TOLERANCE_M = 1e-4


class CharacterPosingSmokeHarness(CharacterRiggingHandlersMixin):
    """Expose character-rigging handlers without starting the socket server."""


def build_rig(name, location, turn):
    """
    Build a three-bone spine/neck/head chain on a rig that is neither at the origin nor aligned.

    The head bone runs along +X so its length axis is not the direction it "looks", which is the
    trap CHAR1_head_jnt sets: armature space and world space, and bone axes and rig axes, all
    differ, so an aim resolved in the wrong space cannot pass by coincidence.
    """
    data = bpy.data.armatures.new(f"{name}Data")
    rig = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(rig)
    bpy.context.view_layer.objects.active = rig
    rig.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    spine = data.edit_bones.new("spine")
    spine.head, spine.tail = (0, 0, 0), (0, 0, 0.4)
    neck = data.edit_bones.new("neck")
    neck.head, neck.tail = (0, 0, 0.4), (0, 0, 0.55)
    neck.parent, neck.use_connect = spine, True
    head = data.edit_bones.new("head")
    head.head, head.tail = (0, 0, 0.55), (0.03, 0, 0.55)
    head.parent = neck
    bpy.ops.object.mode_set(mode="OBJECT")
    rig.select_set(False)
    rig.location = location
    rig.rotation_euler = (0.0, 0.0, turn)
    bpy.context.view_layer.update()
    return rig


def rest_pose(rig):
    for pose_bone in rig.pose.bones:
        pose_bone.matrix_basis.identity()
    bpy.context.view_layer.update()


def world_matrix(rig, bone_name):
    return rig.matrix_world @ rig.pose.bones[bone_name].matrix


def aim_error_degrees(rig, bone_name, track_axis, target):
    """Angle between the bone's tracked axis and the direction from its head to the target."""
    index = {"X": 0, "Y": 1, "Z": 2}[track_axis.lstrip("-")]
    sign = -1.0 if track_axis.startswith("-") else 1.0
    matrix = world_matrix(rig, bone_name)
    axis = (matrix.to_3x3().col[index] * sign).normalized()
    direction = (Vector(target) - matrix.translation).normalized()
    return math.degrees(axis.angle(direction, 0.0))


def action_fcurves(action) -> list:
    """Every F-Curve in a legacy or layered action, whichever shape it has."""
    curves: list = list(getattr(action, "fcurves", ()))
    for layer in getattr(action, "layers", ()):
        for strip in getattr(layer, "strips", ()):
            for channelbag in getattr(strip, "channelbags", ()):
                curves.extend(channelbag.fcurves)
    return curves


def refuses(call, fragment):
    try:
        call()
    except ValueError as error:
        assert fragment in str(error), f"expected {fragment!r} in {error}"
        return
    raise AssertionError(f"expected a refusal mentioning {fragment!r}")


handler = CharacterPosingSmokeHarness()
scratch = Path(tempfile.mkdtemp(prefix="posing_smoke."))
rig = build_rig("SmokeRig", (0.3, 0.0, 0.0), 0.55)
camera = bpy.data.objects.new("SmokeCam", None)
camera.location = (0.6, -4.7, 0.95)
bpy.context.scene.collection.objects.link(camera)
bpy.context.view_layer.update()
CAMERA_WORLD = tuple(camera.matrix_world.translation)

# --- 1. An aim lands on its target, and leaves position and scale alone -----------------------

aimed = handler.set_character_pose(
    rig.name,
    [
        {
            "bone_name": "head",
            "aim_at": {"target_object_name": camera.name, "track_axis": "Z", "up_axis": "X", "up_reference": (0, 0, 1)},
        }
    ],
)
object_error = aim_error_degrees(rig, "head", "Z", CAMERA_WORLD)
basis = rig.pose.bones["head"].matrix_basis
stranded = basis.translation.length
scale_drift = max(abs(value - 1.0) for value in basis.to_scale())
assert aimed["bones"][0]["channels"] == ["aim_at"]
assert object_error < AIM_TOLERANCE_DEGREES, f"aim at an object missed by {object_error} degrees"
assert stranded < POSITION_TOLERANCE, f"a rotation-only aim moved the bone {stranded} m"
assert scale_drift < 1e-9, f"a rotation-only aim scaled the bone by {scale_drift}"

rest_pose(rig)
handler.set_character_pose(
    rig.name,
    [{"bone_name": "head", "aim_at": {"target_point": (0.0, -2.0, 0.75), "track_axis": "Z", "up_axis": "X"}}],
)
point_error = aim_error_degrees(rig, "head", "Z", (0.0, -2.0, 0.75))
assert point_error < AIM_TOLERANCE_DEGREES, f"aim at a world point missed by {point_error} degrees"

# The up axis leans toward the reference as far as a perpendicular axis can.
up_alignment = world_matrix(rig, "head").to_3x3().col[0].normalized().dot(Vector((0, 0, 1)))
assert up_alignment > 0.0, "up_axis leaned away from up_reference"

rest_pose(rig)
head_world = world_matrix(rig, "head").translation
refuses(
    lambda: handler.set_character_pose(
        rig.name, [{"bone_name": "head", "aim_at": {"target_point": tuple(head_world), "track_axis": "Z"}}]
    ),
    "at the head of 'head'",
)
refuses(
    lambda: handler.set_character_pose(
        rig.name,
        [
            {
                "bone_name": "head",
                "aim_at": {
                    "target_point": tuple(head_world + Vector((0, 0, 1))),
                    "track_axis": "Z",
                    "up_axis": "X",
                    "up_reference": (0, 0, 1),
                },
            }
        ],
    ),
    "parallel to the aim direction",
)
refuses(
    lambda: handler.set_character_pose(
        rig.name,
        [{"bone_name": "head", "aim_at": {"target_point": (0, -2, 0.75), "track_axis": "Z", "up_axis": "-Z"}}],
    ),
    "different bone axis than track_axis",
)
refuses(
    lambda: handler.set_character_pose(
        rig.name,
        [
            {
                "bone_name": "head",
                "aim_at": {"target_point": (0, -2, 0.75), "track_axis": "Z", "up_axis": "X", "up_reference": (0, 0, 0)},
            }
        ],
    ),
    "is a zero vector",
)
# Straight behind the axis being tracked: the half turn whose roll a minimal arc cannot define.
tracked = world_matrix(rig, "head").to_3x3().col[2].normalized()
behind = tuple(world_matrix(rig, "head").translation - tracked * 2.0)
refuses(
    lambda: handler.set_character_pose(
        rig.name, [{"bone_name": "head", "aim_at": {"target_point": behind, "track_axis": "Z"}}]
    ),
    "without an up reference",
)
assert rig.pose.bones["head"].matrix_basis.translation.length < POSITION_TOLERANCE, "a refused aim left a pose behind"

# --- 2. A parent and child posed in an absolute space play back where they were posed ----------

rest_pose(rig)
REST_HEAD_ROTATION = tuple(rig.pose.bones["head"].matrix.to_quaternion())
PARENT_CHILD = [
    {"bone_name": "spine", "rotation_euler": (0.0, 0.4, 0.0)},
    {"bone_name": "head", "rotation_quaternion": REST_HEAD_ROTATION},
]
divergence = {}
for space in ("POSE", "WORLD", "LOCAL_WITH_PARENT"):
    rest_pose(rig)
    if rig.animation_data is not None:
        rig.animation_data.action = None
    poses = list(PARENT_CHILD)
    if space == "WORLD":
        poses = [
            {"bone_name": "spine", "rotation_euler": (0.0, 0.4, 0.0)},
            {"bone_name": "head", "rotation_quaternion": tuple(world_matrix(rig, "head").to_quaternion())},
        ]
    handler.set_character_pose(rig.name, poses, space=space)
    posed = world_matrix(rig, "head").copy()
    stranded = rig.pose.bones["head"].matrix_basis.translation.length

    rest_pose(rig)
    action_name = f"SMOKE_{space.lower()}"
    handler.keyframe_character_pose(rig.name, action_name, 1.0, poses, space=space)
    bpy.context.scene.frame_set(1)
    played = world_matrix(rig, "head")

    divergence[space] = (posed.translation - played.translation).length
    assert stranded < POSITION_TOLERANCE, f"{space}: a rotation-only child kept {stranded} m of compensation"
    assert divergence[space] < POSITION_TOLERANCE, (
        f"{space}: playback is {divergence[space]} m from the pose that was set"
    )

# Authoring a second action moves the rig onto it, which is destructive once the first holds
# keys - it stops driving the rig and the next save drops it - so the call has to confirm that
# on purpose, and the reply says what it displaced.
displaced = handler.keyframe_character_pose(
    rig.name,
    "SMOKE_displacer",
    1.0,
    [{"bone_name": "spine", "rotation_euler": (0.0, 0.0, 0.0)}],
    confirm_displace_action=True,
)
assert displaced["unassigned_action"] == "SMOKE_local_with_parent"
assert displaced["assigned_action"] == "SMOKE_displacer"
assert rig.animation_data.action.name == "SMOKE_displacer"

# --- 3. rotate reproduces rotation_axis_angle, in degrees --------------------------------------

rest_pose(rig)
rig.animation_data.action = None
handler.set_character_pose(rig.name, [{"bone_name": "head", "rotation_axis_angle": (1.05, 0.0, -1.0, 0.0)}])
by_radians = rig.pose.bones["head"].matrix.copy()
rest_pose(rig)
handler.set_character_pose(rig.name, [{"bone_name": "head", "rotate": {"axis": "-Y", "degrees": math.degrees(1.05)}}])
by_degrees = rig.pose.bones["head"].matrix.copy()
sugar_error = math.degrees(by_radians.to_quaternion().rotation_difference(by_degrees.to_quaternion()).angle)
assert sugar_error < 1e-9, f"rotate and rotation_axis_angle differ by {sugar_error} degrees"

handler.set_character_pose(
    rig.name, [{"bone_name": "head", "rotate": {"axis": "-Y", "degrees": math.degrees(1.05), "relative": True}}]
)
composed = rig.pose.bones["head"].matrix_basis.to_quaternion().angle
assert abs(composed - 2.1) < 1e-6, f"relative rotate composed to {composed} rad, not 2.1"

# --- 4. Rest axes, and what they cost ----------------------------------------------------------

rest_pose(rig)
plain_page = handler.list_character_bones(rig.name)
axis_page = handler.list_character_bones(rig.name, rest_axes=True)
assert "rest_axes" not in plain_page["bones"]["items"][0]
for item in axis_page["bones"]["items"]:
    reported = item["rest_axes"]
    columns = rig.data.bones[item["name"]].matrix_local.to_3x3()
    expected = [value for index in range(3) for value in columns.col[index]]
    assert max(abs(a - b) for a, b in zip(reported, expected, strict=True)) <= AXIS_ROUNDING
    assert abs(Vector(reported[0:3]).length - 1.0) < AXIS_ROUNDING
    # Full float precision would nearly double the page again for digits that are encoding noise.
    assert all(value == round(value, 3) for value in reported), "rest axes were reported unrounded"

wide = build_rig("WideRig", (0.0, 0.0, 0.0), 0.0)
bpy.context.view_layer.objects.active = wide
wide.select_set(True)
bpy.ops.object.mode_set(mode="EDIT")
for index in range(184):
    extra = wide.data.edit_bones.new(f"CHAR1_filler_{index:03d}_jnt")
    extra.head, extra.tail = (0.01 * index, 0, 0.55), (0.01 * index, 0.02, 0.6)
    extra.parent = wide.data.edit_bones["head"]
bpy.ops.object.mode_set(mode="OBJECT")
wide.select_set(False)
plain_bytes = len(json.dumps(handler.list_character_bones(wide.name, limit=200), indent=2))
axis_bytes = len(json.dumps(handler.list_character_bones(wide.name, limit=200, rest_axes=True), indent=2))
bone_count = len(wide.data.bones)
per_bone = (axis_bytes - plain_bytes) / bone_count
print(
    f"rest_axes cost on {bone_count} bones: {plain_bytes} -> {axis_bytes} bytes "
    f"(+{axis_bytes - plain_bytes}, x{axis_bytes / plain_bytes:.2f}, {per_bone:.0f} per bone)"
)
# Nine rounded numbers one per line at the reply's indentation, the six-entry
# aim_axis_for_world table, and the up_axis they resolve to: about 375 bytes a bone, measured.
# Half of that would mean a bone had stopped saying which axis to aim with; twice it would mean
# the rounding or the flat shape had been lost.
assert 300 < per_bone < 420, f"a rest-axis row costs {per_bone} bytes"

# Naming the bones is the difference between six paginated calls and one. Measured on this
# 187-bone rig: an unfiltered rest_axes walk needs `ceil(total / page)` calls before the three
# bones a pose names are all in hand, and the filter needs one reply of a few hundred bytes.
wanted = ["CHAR1_filler_010_jnt", "CHAR1_filler_120_jnt", "head"]
named = handler.list_character_bones(wide.name, rest_axes=True, bone_names=wanted)
assert named["bones"]["total"] == len(wanted), named["bones"]["total"]
assert named["bones"]["truncated"] is False
assert {item["name"] for item in named["bones"]["items"]} == set(wanted)
assert all("rest_axes" in item for item in named["bones"]["items"])
named_bytes = len(json.dumps(named, indent=2))
assert named_bytes < axis_bytes / 10, f"a three-bone reply cost {named_bytes} of {axis_bytes} bytes"
print(f"named three bones of {bone_count}: {named_bytes} bytes in 1 call, vs {axis_bytes} paged")

# Armature order, so paging a filtered list behaves exactly like paging an unfiltered one.
order = [bone.name for bone in wide.data.bones if bone.name in set(wanted)]
assert [item["name"] for item in named["bones"]["items"]] == order, order

# A name the rig does not have is refused, never quietly dropped.
try:
    handler.list_character_bones(wide.name, bone_names=["head", "CHAR1_no_such_jnt"])
except ValueError as error:
    assert "CHAR1_no_such_jnt" in str(error), str(error)
else:
    raise AssertionError("an unknown bone name was accepted")

# --- 5. A keyed aim on an Euler bone stays on one branch -------------------------------------

# A derived Euler triple has infinitely many spellings, and the one nearest the previous key is
# the only one that does not render as a spin between the two frames. Done on its own rig so it
# cannot disturb the action the persistence check below depends on.
euler_rig = build_rig("EulerRig", (-1.2, 0.4, 0.0), -0.3)
euler_head = euler_rig.pose.bones["head"]
euler_head.rotation_mode = "XYZ"
LEFT, RIGHT = (-2.0, -1.0, 0.7), (2.4, 1.6, 0.7)
for frame, target in ((1.0, LEFT), (25.0, RIGHT)):
    handler.keyframe_character_pose(
        euler_rig.name,
        "SMOKE_euler",
        frame,
        [{"bone_name": "head", "aim_at": {"target_point": target, "track_axis": "Z", "up_axis": "X"}}],
        space="LOCAL",
        action_policy="CREATE" if frame <= 1.0 else "REUSE",
    )
euler_keys = {}
for curve in action_fcurves(bpy.data.actions["SMOKE_euler"]):
    assert curve.data_path == 'pose.bones["head"].rotation_euler'
    for point in curve.keyframe_points:
        euler_keys.setdefault(point.co[0], [0.0] * 3)[curve.array_index] = point.co[1]
euler_jump = max(abs(a - b) for a, b in zip(euler_keys[1.0], euler_keys[25.0], strict=True))
bpy.context.scene.frame_set(13)
midway = (euler_rig.matrix_world @ euler_head.matrix).to_3x3().col[2].normalized()
between = (
    (Vector(LEFT) - (euler_rig.matrix_world @ euler_head.matrix).translation).normalized()
    + (Vector(RIGHT) - (euler_rig.matrix_world @ euler_head.matrix).translation).normalized()
).normalized()
sweep = math.degrees(midway.angle(between, 0.0))
print(f"euler aim: worst adjacent component jump {math.degrees(euler_jump):.2f} deg, midpoint {sweep:.2f} deg off")
# A branch flip shows up twice over: as a component jump of a whole turn or more, and as a
# midpoint that has swung away from the arc between the two targets instead of along it.
assert math.degrees(euler_jump) < 180.0, f"a keyed Euler aim jumped {math.degrees(euler_jump)} degrees"
assert sweep < 30.0, f"the interpolated Euler aim swung {sweep} degrees off the arc between the targets"

# --- 6. A rotation-only aim keys exactly four readable curves, and the rig keeps them ---------

rest_pose(rig)
rig.animation_data.action = None
AIM_SPEC = {
    "bone_name": "head",
    "aim_at": {"target_object_name": camera.name, "track_axis": "Z", "up_axis": "X", "up_reference": (0, 0, 1)},
}
first = handler.keyframe_character_pose(rig.name, "SMOKE_motion", 1.0, [AIM_SPEC], space="LOCAL")
motion = bpy.data.actions["SMOKE_motion"]
curves = action_fcurves(motion)
assert len(curves) == 4, f"a rotation-only aim keyed {len(curves)} curves, not 4"
assert {curve.data_path for curve in curves} == {'pose.bones["head"].rotation_quaternion'}
assert {curve.array_index for curve in curves} == {0, 1, 2, 3}
assert {curve.group.name for curve in curves} == {"head"}
assert first["assigned_action"] == "SMOKE_motion"
assert "unassigned_action" not in first, "an unassigned rig reported a displacement"
assert [entry["data_path"] for entry in first["changed_keys"]] == ["rotation_quaternion"]

second = handler.keyframe_character_pose(
    rig.name,
    "SMOKE_motion",
    24.0,
    [
        {
            "bone_name": "head",
            "aim_at": {"target_point": (-3.0, 1.0, 0.9), "track_axis": "Z", "up_axis": "X", "up_reference": (0, 0, 1)},
        }
    ],
    space="LOCAL",
    action_policy="REUSE",
)
assert "unassigned_action" not in second, "reusing the assigned action reported a displacement"
keyed_quaternions: dict[float, list[float]] = {}
for curve in action_fcurves(motion):
    for point in curve.keyframe_points:
        keyed_quaternions.setdefault(point.co[0], [0.0] * 4)[curve.array_index] = point.co[1]
assert set(keyed_quaternions) == {1.0, 24.0}
short_way = sum(a * b for a, b in zip(keyed_quaternions[1.0], keyed_quaternions[24.0], strict=True))
assert short_way >= 0.0, f"adjacent aim keys interpolate the long way round (dot {short_way})"

# The aim is still exact when the action plays it back, not just when it is applied.
bpy.context.scene.frame_set(24)
playback_error = aim_error_degrees(rig, "head", "Z", (-3.0, 1.0, 0.9))
assert playback_error < AIM_TOLERANCE_DEGREES, f"keyed aim plays back {playback_error} degrees off"
bpy.context.scene.frame_set(1)
first_frame_error = aim_error_degrees(rig, "head", "Z", CAMERA_WORLD)
assert first_frame_error < AIM_TOLERANCE_DEGREES, f"first aim key plays back {first_frame_error} degrees off"

refuses(
    lambda: handler.keyframe_character_pose(
        rig.name,
        "SMOKE_motion",
        36.0,
        [{"bone_name": "head", "aim_at": {"target_object_name": camera.name, "track_axis": "Z"}}],
        action_policy="REUSE",
    ),
    "requires up_axis and up_reference when keying",
)


# --- 7. The keyed action survives a save, a reset, and a reopen --------------------------------

bpy.context.scene.frame_set(24)
expected_world = [list(row) for row in world_matrix(rig, "head")]
blend_path = scratch / "posing_smoke.blend"
bpy.ops.wm.save_as_mainfile(filepath=str(blend_path))
bpy.ops.wm.read_factory_settings(use_empty=True)
assert not bpy.data.actions, "the reset did not clear the session"
bpy.ops.wm.open_mainfile(filepath=str(blend_path))

reopened_action = bpy.data.actions.get("SMOKE_motion")
assert reopened_action is not None, "the action the handler authored was dropped at save"
assert reopened_action.users >= 1, f"the reopened action has {reopened_action.users} users"
reopened_rig = bpy.data.objects["SmokeRig"]
assert reopened_rig.animation_data.action.name == "SMOKE_motion"
assert reopened_rig.animation_data.action_slot is not None
assert len(action_fcurves(reopened_action)) == 4
bpy.context.scene.frame_set(24)
reopened_world = [list(row) for row in (reopened_rig.matrix_world @ reopened_rig.pose.bones["head"].matrix)]
drift = max(
    abs(a - b)
    for row_a, row_b in zip(reopened_world, expected_world, strict=True)
    for a, b in zip(row_a, row_b, strict=True)
)
assert drift < POSITION_TOLERANCE, f"the reopened rig evaluates {drift} away from what was saved"
reopened_error = aim_error_degrees(reopened_rig, "head", "Z", (-3.0, 1.0, 0.9))
assert reopened_error < AIM_TOLERANCE_DEGREES, f"the reopened rig aims {reopened_error} degrees off"


# --- 8. solve_bone_reach bends a chain so its tip's tail reaches a world point -----------------

reopened_rig.animation_data.action = None
rest_pose(reopened_rig)
root_world = reopened_rig.matrix_world @ reopened_rig.pose.bones["spine"].head
# spine (0.4 m) + neck (0.15 m) + head (0.03 m) of reach, and the target sits 0.39 m out:
# comfortably inside it, and off the rig's straight +Z rest axis. tip_bone is "head", not
# "neck": spine and neck are colinear (see build_rig), and Blender's IK cannot bend a chain
# whose rest pose is dead straight - given the same target it only straightens and aims that
# chain, missing by 0.159 m however many iterations it is given (measured, Blender 5.2.2).
# The head bone turns 90 degrees at neck's tail, which is the rest bend the solver needs.
target_point = root_world + reopened_rig.matrix_world.to_3x3() @ Vector((0.25, 0.0, 0.3))
reach_target = tuple(target_point)
reach_pole = tuple(root_world + reopened_rig.matrix_world.to_3x3() @ Vector((0.5, 0.0, 0.0)))
reach = handler.solve_bone_reach(
    reopened_rig.name,
    [{"tip_bone": "head", "target_point": reach_target, "pole_target_point": reach_pole}],
    tolerance_m=REACH_TOLERANCE_M,
)
solved = reach["reaches"][0]
reach_error = solved["achieved_error_m"]
assert reach["tolerance_m"] == REACH_TOLERANCE_M, f"the reply echoed tolerance_m {reach['tolerance_m']}"
assert solved["converged"] is True, f"solve_bone_reach reported no convergence, off by {reach_error} m"
assert solved["out_of_reach"] is False, "a target inside the chain's reach was reported out of reach"
assert solved["target_distance_m"] < solved["chain_reach_m"], (
    f"target {solved['target_distance_m']} m out, chain reaches {solved['chain_reach_m']} m"
)
assert reach["warnings"] == [], f"a converged reach warned anyway: {reach['warnings']}"
assert solved["chain_length_source"] == "resolved"
assert solved["chain_bones"] == ["head", "neck", "spine"]
assert solved["pole_source"] == "explicit"
assert list(reopened_rig.pose.bones["head"].constraints) == [], "a temporary IK constraint was left on head"
assert list(reopened_rig.pose.bones["neck"].constraints) == [], "a temporary IK constraint was left on neck"
assert list(reopened_rig.pose.bones["spine"].constraints) == [], "a temporary IK constraint was left on spine"
assert not any(obj.name.startswith("__solve_bone_reach__") for obj in bpy.data.objects), (
    "a scratch Empty was left behind"
)

# achieved_error_m is read while the constraint is still live. What the caller keeps is the
# captured pose reapplied after it was removed, so measure the rig itself.
bpy.context.view_layer.update()
applied_error = (reopened_rig.matrix_world @ reopened_rig.pose.bones["head"].tail - target_point).length
assert applied_error < reach["tolerance_m"], f"the reapplied pose sits {applied_error} m from the target"

# Omitting the pole synthesizes one from the chain's rest bend, and it has to solve just as well.
rest_pose(reopened_rig)
synthesized = handler.solve_bone_reach(
    reopened_rig.name, [{"tip_bone": "head", "target_point": reach_target}], tolerance_m=REACH_TOLERANCE_M
)
assert synthesized["reaches"][0]["pole_source"] == "resolved"
synthesized_error = synthesized["reaches"][0]["achieved_error_m"]
assert synthesized["reaches"][0]["converged"] is True, (
    f"the synthesized pole missed its target by {synthesized_error} m"
)

# A target no pose of this chain can reach is reported as out of reach, not as a near miss:
# retrying it with more iterations or a different pole would never close the gap.
rest_pose(reopened_rig)
unreachable_point = root_world + reopened_rig.matrix_world.to_3x3() @ Vector((5.0, 0.0, 0.0))
unreachable = handler.solve_bone_reach(
    reopened_rig.name,
    [{"tip_bone": "head", "target_point": tuple(unreachable_point), "pole_target_point": reach_pole}],
    tolerance_m=REACH_TOLERANCE_M,
)
missed = unreachable["reaches"][0]
assert missed["converged"] is False, "a 5 m target was reported as converged"
assert missed["out_of_reach"] is True, (
    f"target {missed['target_distance_m']} m out was not flagged against a {missed['chain_reach_m']} m chain"
)
assert any("out of reach" in warning for warning in unreachable["warnings"]), unreachable["warnings"]
assert any("head" in warning for warning in unreachable["warnings"]), unreachable["warnings"]

# This rig's spine/neck rest chain is dead straight; omitting pole_target has no bend to infer.
rest_pose(reopened_rig)
refuses(
    lambda: handler.solve_bone_reach(reopened_rig.name, [{"tip_bone": "neck", "target_point": reach_target}]),
    "rest pose is straight",
)

# Two reaches naming an overlapping chain in one call are refused, not silently resolved.
refuses(
    lambda: handler.solve_bone_reach(
        reopened_rig.name,
        [
            {"tip_bone": "head", "target_point": reach_target, "pole_target_point": reach_pole},
            {"tip_bone": "spine", "target_point": reach_target},
        ],
    ),
    "claimed by more than one reach",
)

# A refused reach cleans up after itself too: the target's scratch Empty is created before the
# pole is resolved, and the IK constraint before the chain is read.
assert not any(obj.name.startswith("__solve_bone_reach__") for obj in bpy.data.objects), (
    "a refused reach left a scratch Empty behind"
)
assert list(reopened_rig.pose.bones["head"].constraints) == [], "a refused reach left an IK constraint on head"


# --- 9. An aim can name a bone on another rig, not just that rig's origin ---------------------
#
# "Have A look at B" aimed at B's object origin, which on a character sits on the floor, so two
# characters told to look at each other looked at each other's feet. The bone is 0.55 m above
# that origin and 1.9 m away, so the two answers are about 16 degrees apart: enough that a
# reply claiming success while aiming at the origin cannot pass this.

rest_pose(reopened_rig)
reopened_rig.animation_data.action = None
partner = build_rig("PartnerRig", (2.0, 1.0, 0.0), 0.0)
rest_pose(partner)
partner_head_world = tuple(world_matrix(partner, "head").translation)
partner_origin_world = tuple(partner.matrix_world.translation)
handler.set_character_pose(
    reopened_rig.name,
    [
        {
            "bone_name": "head",
            "aim_at": {
                "target_object_name": partner.name,
                "target_bone_name": "head",
                "track_axis": "Z",
                "up_axis": "X",
                "up_reference": (0, 0, 1),
            },
        }
    ],
)
bone_aim_error = aim_error_degrees(reopened_rig, "head", "Z", partner_head_world)
origin_aim_error = aim_error_degrees(reopened_rig, "head", "Z", partner_origin_world)
print(f"bone target: {bone_aim_error:.6f} deg off the bone, {origin_aim_error:.6f} deg off the rig's origin")
assert bone_aim_error < 0.5, f"an aim at a bone missed it by {bone_aim_error} degrees"
assert origin_aim_error > 5.0, (
    f"aiming at the bone and at the rig's origin differ by only {origin_aim_error} degrees, so this proves nothing"
)

# TAIL and CENTER name the other two points on the same bone, and land on them.
partner_tail_world = tuple(world_matrix(partner, "head") @ Vector((0.0, partner.pose.bones["head"].length, 0.0)))
for position, expected in (("TAIL", partner_tail_world), ("HEAD", partner_head_world)):
    rest_pose(reopened_rig)
    handler.set_character_pose(
        reopened_rig.name,
        [
            {
                "bone_name": "head",
                "aim_at": {
                    "target_object_name": partner.name,
                    "target_bone_name": "head",
                    "target_bone_position": position,
                    "track_axis": "Z",
                    "up_axis": "X",
                },
            }
        ],
    )
    placed = aim_error_degrees(reopened_rig, "head", "Z", expected)
    assert placed < 0.5, f"target_bone_position={position} missed its point by {placed} degrees"

# The rig reopened from disk in section 7, so the scene's earlier Empty is gone with it.
plain_target = bpy.data.objects.new("PlainTarget", None)
bpy.context.scene.collection.objects.link(plain_target)
bpy.context.view_layer.update()
refuses(
    lambda: handler.set_character_pose(
        reopened_rig.name,
        [
            {
                "bone_name": "head",
                "aim_at": {
                    "target_object_name": plain_target.name,
                    "target_bone_name": "head",
                    "track_axis": "Z",
                    "up_axis": "X",
                },
            }
        ],
    ),
    "needs an armature to live on",
)
refuses(
    lambda: handler.set_character_pose(
        reopened_rig.name,
        [
            {
                "bone_name": "head",
                "aim_at": {
                    "target_object_name": partner.name,
                    "target_bone_name": "jaw",
                    "track_axis": "Z",
                    "up_axis": "X",
                },
            }
        ],
    ),
    "aim_at.target_bone_name not found on 'PartnerRig': jaw",
)

# --- 10. One batched call keys a whole stride, each frame solved where that frame is ----------
#
# A thirteen-key stride was thirteen round trips, and every one of them solved its aim against
# whatever frame the playhead happened to be on. One call keys them all, placing the playhead on
# each frame first: the target below moves 6 m across the stride, so an aim evaluated once would
# be tens of degrees off on four of the five frames.

rest_pose(reopened_rig)
batch_rig = build_rig("BatchRig", (0.0, -1.0, 0.0), 0.2)
mover = bpy.data.objects.new("MovingTarget", None)
bpy.context.scene.collection.objects.link(mover)
BATCH_FRAMES = (1.0, 6.0, 11.0, 16.0, 21.0)
for index, frame in enumerate(BATCH_FRAMES):
    mover.location = (3.0 - 1.5 * index, -3.0, 0.55)
    mover.keyframe_insert(data_path="location", frame=frame)
bpy.context.view_layer.update()


def mover_world(frame):
    """Where the animated target sits at one frame, read from the scene rather than assumed."""
    bpy.context.scene.frame_set(int(frame))
    return tuple(mover.matrix_world.translation)


MOVER_TRACK = {frame: mover_world(frame) for frame in BATCH_FRAMES}
bpy.context.scene.frame_set(7)
playhead_before = bpy.context.scene.frame_current


def head_basis(rig):
    """Read one bone's own channel values out as plain numbers, so a comparison is by value."""
    return [list(row) for row in rig.pose.bones["head"].matrix_basis]


def worst_difference(left, right):
    return max(abs(a - b) for row_a, row_b in zip(left, right, strict=True) for a, b in zip(row_a, row_b, strict=True))


batched = handler.keyframe_character_pose(
    batch_rig.name,
    "SMOKE_batch",
    keys=[
        {
            "frame": frame,
            "poses": [
                {
                    "bone_name": "head",
                    "aim_at": {
                        "target_object_name": mover.name,
                        "track_axis": "Z",
                        "up_axis": "X",
                        "up_reference": (0, 0, 1),
                    },
                }
            ],
        }
        # Out of order on purpose: the handler keys them ascending, and says so.
        for frame in (11.0, 1.0, 21.0, 6.0, 16.0)
    ],
    action_policy="CREATE",
)

assert batched["keyed_frames"] == list(BATCH_FRAMES), batched["keyed_frames"]
assert batched["changed_bones"] == ["head"], batched["changed_bones"]
assert len(batched["changed_keys"]) == len(BATCH_FRAMES), batched["changed_keys"]
assert batched["interpolation_updates"] == 4 * len(BATCH_FRAMES), batched["interpolation_updates"]
assert bpy.context.scene.frame_current == playhead_before, "the batched call left the playhead where it stopped"
# The rig is handed back to the action it just authored, not left holding the last frame it
# solved: the playhead is on 7, so the pose the call leaves behind is the one the action
# interpolates there - which is nothing like the pose keyed at 21.
after_call = head_basis(batch_rig)
bpy.context.scene.frame_set(playhead_before)
bpy.context.view_layer.update()
assert worst_difference(after_call, head_basis(batch_rig)) < POSITION_TOLERANCE, (
    "the batched call left a pose the action does not evaluate to at the restored playhead"
)
bpy.context.scene.frame_set(int(BATCH_FRAMES[-1]))
bpy.context.view_layer.update()
assert worst_difference(after_call, head_basis(batch_rig)) > 0.05, (
    "the rig was left holding the last frame this call solved"
)
bpy.context.scene.frame_set(playhead_before)

batch_keys: dict[float, list[float]] = {}
for curve in action_fcurves(bpy.data.actions["SMOKE_batch"]):
    for point in curve.keyframe_points:
        batch_keys.setdefault(point.co[0], [0.0] * 4)[curve.array_index] = point.co[1]
assert set(batch_keys) == set(BATCH_FRAMES), sorted(batch_keys)
distinct = {tuple(round(value, 6) for value in values) for values in batch_keys.values()}
assert len(distinct) == len(BATCH_FRAMES), f"a moving target keyed only {len(distinct)} distinct poses"

batch_errors = []
for frame, target in MOVER_TRACK.items():
    bpy.context.scene.frame_set(int(frame))
    batch_errors.append(aim_error_degrees(batch_rig, "head", "Z", target))
worst_batch_error = max(batch_errors)
print(f"batched aim: worst frame off by {worst_batch_error:.9f} deg over {len(BATCH_FRAMES)} frames in 1 call")
assert worst_batch_error < AIM_TOLERANCE_DEGREES, (
    f"a batched frame was keyed aiming {worst_batch_error} degrees off, so the aim was not solved at that frame"
)

refuses(
    lambda: handler.keyframe_character_pose(batch_rig.name, "SMOKE_batch", 4.0, [], action_policy="REUSE"),
    "requires both frame and poses",
)
refuses(
    lambda: handler.keyframe_character_pose(
        batch_rig.name,
        "SMOKE_batch",
        4.0,
        [{"bone_name": "head", "rotate": {"axis": "Z", "degrees": 5.0}}],
        keys=[{"frame": 5.0, "poses": [{"bone_name": "head", "rotate": {"axis": "Z", "degrees": 5.0}}]}],
        action_policy="REUSE",
    ),
    "exactly one of frame with poses",
)

# --- 11. The reply names the bone axis for each world direction, and an aim takes it ----------
#
# `length_axis` is `Y` on every bone Blender builds, and an upright bone's `up_axis` is `Y` too,
# so the instruction to pass both through named one axis twice and was refused. The table below
# is what a caller reads instead: one letter per world direction, already in aim_at's vocabulary.

rest_pose(batch_rig)
bpy.context.scene.frame_set(1)
axis_record = handler.list_character_bones(batch_rig.name, rest_axes=True, bone_names=["head"])["bones"]["items"][0]
aim_axes = axis_record["aim_axis_for_world"]
print(f"aim_axis_for_world for 'head': {aim_axes}, up_axis={axis_record['up_axis']!r}")
assert set(aim_axes) == {"+X", "-X", "+Y", "-Y", "+Z", "-Z"}
assert aim_axes["+Z"] == axis_record["up_axis"], "up_axis and the +Z entry disagree"

# A horizontal direction whose letter is free of the up axis: that pair is what an aim takes.
WORLD_VECTORS = {"+X": (1.0, 0.0, 0.0), "-X": (-1.0, 0.0, 0.0), "+Y": (0.0, 1.0, 0.0), "-Y": (0.0, -1.0, 0.0)}
up_axis = axis_record["up_axis"]
usable = [
    direction
    for direction in WORLD_VECTORS
    if aim_axes[direction] is not None and aim_axes[direction].lstrip("-") != up_axis.lstrip("-")
]
assert usable, f"no world direction gave a track_axis free of up_axis={up_axis!r}: {aim_axes}"
for direction in usable:
    rest_pose(batch_rig)
    bone_head = world_matrix(batch_rig, "head").translation
    point = tuple(bone_head + Vector(WORLD_VECTORS[direction]) * 2.0)
    handler.set_character_pose(
        batch_rig.name,
        [
            {
                "bone_name": "head",
                "aim_at": {"target_point": point, "track_axis": aim_axes[direction], "up_axis": up_axis},
            }
        ],
    )
    tracked_error = aim_error_degrees(batch_rig, "head", aim_axes[direction], point)
    index = {"X": 0, "Y": 1, "Z": 2}[up_axis.lstrip("-")]
    sign = -1.0 if up_axis.startswith("-") else 1.0
    # bpy's stub types a matrix column as None rather than the Vector it is at runtime.
    up_error = math.degrees(
        (Vector(world_matrix(batch_rig, "head").to_3x3().col[index]) * sign)  # pyright: ignore[reportArgumentType]
        .normalized()
        .angle(Vector((0, 0, 1)), 0.0)
    )
    print(f"{direction}: track_axis={aim_axes[direction]!r} up_axis={up_axis!r} aim {tracked_error:.9f} deg")
    assert tracked_error < 0.5, f"{direction}: the reply's own letter missed by {tracked_error} degrees"
    assert up_error < 0.5, f"{direction}: the reply's up_axis left the bone {up_error} degrees off upright"

# The rest-axes letters are the ones a caller has; the refusal for naming one twice says which
# two are left and where each points, because the bone's own axes are not otherwise readable.
rest_pose(batch_rig)
refuses(
    lambda: handler.set_character_pose(
        batch_rig.name,
        [{"bone_name": "head", "aim_at": {"target_point": (0.0, -3.0, 0.55), "track_axis": "Y", "up_axis": "Y"}}],
    ),
    "The bone's other axes at rest:",
)

# --- A roll warns only when the rig really carries nothing off the axis it turns about -------
#
# The failure this covers is silent in every other channel: the call succeeds, the keys land,
# the pose matrix changes, and the joint does not bend. The trap on the other side is worse -
# the same arithmetic on a head bone is the intended head turn, and a notice that fired there
# taught an agent that these warnings were noise. Measured here against the real API: what
# moves, in world space, is what decides which of the two this is.


def tail_world(rig, bone_name):
    return rig.matrix_world @ rig.pose.bones[bone_name].tail.copy()


def bone_head_world(rig, bone_name):
    return rig.matrix_world @ rig.pose.bones[bone_name].head.copy()


def twist_notices(reply):
    return [warning for warning in reply["warnings"] if "length axis" in warning]


ROLL_DEGREES = 60.0
rest_pose(batch_rig)
rest_neck_tail = tail_world(batch_rig, "neck")
rest_child_head = bone_head_world(batch_rig, "head")
rest_child_tail = tail_world(batch_rig, "head")
rest_head_tail = rest_child_tail

# The neck's child `head` runs out along +X from the neck's tail, so a roll of the neck leaves
# the neck's tail and the child's head exactly put and still swings the child's far end.
carrier = handler.set_character_pose(
    batch_rig.name, [{"bone_name": "neck", "rotate": {"axis": "-Y", "degrees": ROLL_DEGREES}}]
)
roll_tail_travel = (tail_world(batch_rig, "neck") - rest_neck_tail).length
roll_child_head_travel = (bone_head_world(batch_rig, "head") - rest_child_head).length
roll_child_tail_travel = (tail_world(batch_rig, "head") - rest_child_tail).length
rest_pose(batch_rig)
bent = handler.set_character_pose(
    batch_rig.name, [{"bone_name": "neck", "rotate": {"axis": "X", "degrees": ROLL_DEGREES}}]
)
bend_tail_travel = (tail_world(batch_rig, "neck") - rest_neck_tail).length
rest_pose(batch_rig)

# `head` is the chain's leaf: nothing hangs off its length axis and no mesh is bound to this
# rig, so its roll really does move everything this call can reach by nothing at all.
barren = handler.set_character_pose(
    batch_rig.name, [{"bone_name": "head", "rotate": {"axis": "Y", "degrees": ROLL_DEGREES}}]
)
barren_tail_travel = (tail_world(batch_rig, "head") - rest_head_tail).length
rest_pose(batch_rig)

assert roll_tail_travel < POSITION_TOLERANCE, f"a length-axis roll moved the tail {roll_tail_travel} m"
assert roll_child_head_travel < POSITION_TOLERANCE, (
    f"a length-axis roll moved the child's head {roll_child_head_travel} m"
)
assert roll_child_tail_travel > 0.5 * batch_rig.pose.bones["head"].length, (
    f"the child's far end barely moved ({roll_child_tail_travel} m); this roll carries nothing and proves nothing"
)
assert bend_tail_travel > 0.1 * batch_rig.pose.bones["neck"].length, (
    f"a perpendicular rotation barely moved the tail ({bend_tail_travel} m); the comparison proves nothing"
)
assert barren_tail_travel < POSITION_TOLERANCE, f"the leaf bone's own roll moved its tail {barren_tail_travel} m"

assert twist_notices(carrier) == [], (
    f"a roll that swung the child bone {roll_child_tail_travel:.4f} m was called inert: {carrier['warnings']}"
)
assert twist_notices(bent) == [], f"a rotation that bends the joint was called a twist: {bent['warnings']}"
assert len(twist_notices(barren)) == 1, f"the roll that moved nothing was not reported: {barren['warnings']}"
barren_notice = twist_notices(barren)[0]
assert "head" in barren_notice
assert "moves the furthest thing measured by 0 m" in barren_notice, barren_notice
assert "no mesh bound to this armature carries a vertex group named after it" in barren_notice, barren_notice

# --- The probe measures which axis swings a bone, because no rest reading can say -------------
#
# `rest_axes` names directions at rest: it carries no witness, so it cannot say how far anything
# travels, and a constraint or a driver can null a channel without appearing in it at all.
# Measured here against the real API - the trial turn is applied, the witness is read where
# Blender put it, and the pose has to come back to the channel values it arrived on.

PROBE_DEGREES = 20.0
WORLD_REFERENCES = {"world_x": [1.0, 0.0, 0.0], "world_y": [0.0, 1.0, 0.0], "world_z": [0.0, 0.0, 1.0]}
rest_pose(batch_rig)
bpy.context.scene.frame_set(1)
probe_before = [list(row) for row in batch_rig.pose.bones["neck"].matrix_basis]
probed = handler.probe_bone_axis(
    batch_rig.name, "neck", ["X", "Y", "Z"], degrees=PROBE_DEGREES, reference_directions=WORLD_REFERENCES
)
probe_after = [list(row) for row in batch_rig.pose.bones["neck"].matrix_basis]
probe_travel = {record["axis"]: record["travel_m"] for record in probed["axes"]}
forward = next(record for record in probed["axes"] if record["axis"] == "X")["reference_components_m"]
strongest = max(forward, key=lambda name: abs(forward[name]))
backward = handler.probe_bone_axis(
    batch_rig.name, "neck", ["X"], degrees=-PROBE_DEGREES, reference_directions=WORLD_REFERENCES
)["axes"][0]["reference_components_m"]

assert probed["witness_bone"] == "head", probed
assert probed["witness_bone_source"] == "farthest_descendant", probed
assert probed["witness_bone_position"] == "TAIL", probed
assert probe_travel["X"] > 3.0 * probe_travel["Y"], (
    f"the swinging axis must out-travel the length axis by more than noise: {probe_travel}"
)
assert probe_travel["Z"] > 3.0 * probe_travel["Y"], f"only one axis swung the witness: {probe_travel}"
assert abs(forward[strongest]) > 0.01, f"no named direction caught the travel: {forward}"
# A turn and its reverse carry the witness through mirrored chords rather than opposite vectors,
# so the contract is the sign - which is the half of the answer "how far" cannot give.
assert forward[strongest] * backward[strongest] < 0.0, (
    f"reversing the turn did not reverse the {strongest} component: {forward[strongest]} vs {backward[strongest]}"
)
for row_before, row_after in zip(probe_before, probe_after, strict=True):
    for value_before, value_after in zip(row_before, row_after, strict=True):
        assert abs(value_before - value_after) < POSITION_TOLERANCE, (
            f"the probe left the bone posed: {probe_before} became {probe_after}"
        )

# --- A rotate on a QUATERNION bone moves skinned geometry exactly as the Euler channel does ---
#
# A rehearsal measured a jaw bone moving skin through raw `rotation_euler` and not moving it at
# all through `set_character_pose`'s `rotate`, on a bone in QUATERNION mode - which would mean
# `rotate` composes differently depending on a bone's rotation mode. Channel values cannot
# settle that (the two channels are different channels by construction), so this measures the
# only thing an audience sees: where the deformed vertices end up, read off the depsgraph.


def skin_to_bone(rig, bone_name, name):
    """Bind a small box to one bone at full weight, so its vertices report that bone's motion."""
    mesh = bpy.data.meshes.new(f"{name}Mesh")
    head = rig.pose.bones[bone_name].head.copy()
    corners = [(x, y, z) for x in (-0.05, 0.05) for y in (-0.05, 0.05) for z in (0.0, 0.12)]
    mesh.from_pydata([tuple(head + Vector(corner)) for corner in corners], [], [])
    mesh.update()
    skin = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(skin)
    # The vertices were built from the bone's armature-space head, so the object has to stand in
    # the rig's own frame for them to land on the bone in world space. Without it the skin sits
    # metres from the bone it is weighted to, and anything measuring a radius off that axis -
    # the roll notice does - measures the gap between the two frames instead.
    skin.matrix_world = rig.matrix_world.copy()
    indices = [vertex.index for vertex in mesh.vertices]
    skin.vertex_groups.new(name=bone_name).add(indices, 1.0, "REPLACE")  # pyright: ignore[reportArgumentType]
    skin.modifiers.new("Armature", "ARMATURE").object = rig
    bpy.context.view_layer.update()
    return skin


def deformed_points(skin):
    """World-space vertex positions after the armature modifier has actually run."""
    evaluated = skin.evaluated_get(bpy.context.evaluated_depsgraph_get())
    mesh = evaluated.to_mesh()
    try:
        return [evaluated.matrix_world @ vertex.co.copy() for vertex in mesh.vertices]
    finally:
        evaluated.to_mesh_clear()


def travel(before, after):
    return max((a - b).length for b, a in zip(before, after, strict=True))


JAW_DEGREES = 25.0
jaw_rig = build_rig("SmokeJawRig", (-0.4, 0.2, 0.0), -0.9)
jaw_skin = skin_to_bone(jaw_rig, "head", "SmokeJawSkin")

rotate_by_mode = {}
euler_by_mode = {}
for rotation_mode in ("QUATERNION", "XYZ"):
    jaw_rig.pose.bones["head"].rotation_mode = rotation_mode
    rest_pose(jaw_rig)
    rest_points = deformed_points(jaw_skin)
    handler.set_character_pose(jaw_rig.name, [{"bone_name": "head", "rotate": {"axis": "X", "degrees": JAW_DEGREES}}])
    rotate_by_mode[rotation_mode] = deformed_points(jaw_skin)
    rest_pose(jaw_rig)
    handler.set_character_pose(
        jaw_rig.name, [{"bone_name": "head", "rotation_euler": (math.radians(JAW_DEGREES), 0.0, 0.0)}]
    )
    euler_by_mode[rotation_mode] = deformed_points(jaw_skin)
    rest_pose(jaw_rig)

    moved = travel(rest_points, rotate_by_mode[rotation_mode])
    assert moved > 0.01, f"rotate moved {rotation_mode} skin {moved:.3e} m; a zero comparison proves nothing"
    disagreement = travel(euler_by_mode[rotation_mode], rotate_by_mode[rotation_mode])
    assert disagreement < POSITION_TOLERANCE, (
        f"on a {rotation_mode} bone, rotate and rotation_euler put skin {disagreement:.3e} m apart"
    )

# The two modes must also agree with each other: `rotate` is stated in the call's space, so a
# bone's storage format is not allowed to change where the geometry lands.
across_modes = travel(rotate_by_mode["QUATERNION"], rotate_by_mode["XYZ"])
assert across_modes < POSITION_TOLERANCE, f"rotation_mode changed where rotate put the skin, by {across_modes:.3e} m"

# --- A roll of a skinned leaf bone is judged on the skin, not on the bone's empty tail -------
#
# `head` carries no child bone, so the rest hierarchy alone says a roll about its length moves
# nothing. The audience disagrees: the box above is weighted to it at full weight and sits up to
# 13 cm off that axis, so the roll carries it. Judging this one from the hierarchy is exactly
# how the notice came to fire on correct poses.
SKIN_ROLL_DEGREES = 45.0
jaw_rig.pose.bones["head"].rotation_mode = "QUATERNION"
rest_pose(jaw_rig)
skin_rest_points = deformed_points(jaw_skin)
skinned_roll = handler.set_character_pose(
    jaw_rig.name, [{"bone_name": "head", "rotate": {"axis": "Y", "degrees": SKIN_ROLL_DEGREES}}]
)
skin_roll_travel = travel(skin_rest_points, deformed_points(jaw_skin))
jaw_tail_roll_travel = (tail_world(jaw_rig, "head") - (jaw_rig.matrix_world @ Vector((0.03, 0.0, 0.55)))).length
rest_pose(jaw_rig)

assert skin_roll_travel > 0.05, f"the roll moved the skin {skin_roll_travel:.3e} m; the comparison proves nothing"
assert jaw_tail_roll_travel < POSITION_TOLERANCE, (
    f"the rolled bone's own tail moved {jaw_tail_roll_travel:.3e} m, so this is not a pure roll"
)
assert twist_notices(skinned_roll) == [], (
    f"a roll that carried skin {skin_roll_travel:.3f} m was called inert: {skinned_roll['warnings']}"
)
jaw_travel = travel(euler_by_mode["QUATERNION"], rotate_by_mode["QUATERNION"])
print(
    f"length-axis roll of 'neck': its tail travelled {roll_tail_travel:.3e} m and the child's head "
    f"{roll_child_head_travel:.3e} m, but the child's far end {roll_child_tail_travel:.4f} m, so "
    f"{len(twist_notices(carrier))} notices; the leaf 'head' rolled its own tail {barren_tail_travel:.3e} m "
    f"for {len(twist_notices(barren))}; the same angle about X travelled {bend_tail_travel:.6f} m with "
    f"{len(twist_notices(bent))}"
)
print(
    f"skinned roll of {SKIN_ROLL_DEGREES} deg: tail travelled {jaw_tail_roll_travel:.3e} m and the skin "
    f"{skin_roll_travel:.4f} m, so {len(twist_notices(skinned_roll))} notices"
)
print(
    f"probe_bone_axis on 'neck' at {PROBE_DEGREES} deg, witness {probed['witness_bone']!r} "
    f"({probed['witness_bone_source']}): travel_m "
    + ", ".join(f"{a} {t:.4f}" for a, t in probe_travel.items())
    + f"; {strongest} component {forward[strongest]:+.4f} reverses to {backward[strongest]:+.4f}"
)
print(
    f"rotate vs rotation_euler at {JAW_DEGREES} deg on a QUATERNION bone: skin agrees to "
    f"{jaw_travel:.3e} m; across rotation modes {across_modes:.3e} m"
)
print(
    f"solve_bone_reach: achieved_error_m {reach_error:.9f}, reapplied {applied_error:.9f}, "
    f"chain_reach_m {solved['chain_reach_m']:.6f}, target_distance_m {solved['target_distance_m']:.6f}, "
    f"out-of-reach target missed by {missed['achieved_error_m']:.6f}"
)

print(f"aim error: object {object_error:.12f} deg, world point {point_error:.12f} deg")
print(f"keyed playback error: frame 1 {first_frame_error:.12f} deg, frame 24 {playback_error:.12f} deg")
print("absolute-space divergence (m): " + ", ".join(f"{space} {value:.3e}" for space, value in divergence.items()))
print(
    f"reopened action users {reopened_action.users}, curves {len(action_fcurves(reopened_action))}, drift {drift:.3e}"
)
shutil.rmtree(scratch, ignore_errors=True)
print("CHARACTER_POSING_SMOKE_OK")
