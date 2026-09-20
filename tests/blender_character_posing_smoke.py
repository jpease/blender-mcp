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
import types

from pathlib import Path

import bpy

from mathutils import Vector

addon_path = Path(__file__).resolve().parents[1] / "src" / "blender_mcp" / "bundled" / "addon"
package_name = "blender_mcp_character_posing_smoke"
addon = types.ModuleType(package_name)
addon.__path__ = [str(addon_path)]
addon.ADDON_ID = package_name
sys.modules[package_name] = addon
character_handlers = importlib.import_module(f"{package_name}.handlers.character_rigging")
CharacterRiggingHandlersMixin = character_handlers.CharacterRiggingHandlersMixin

AIM_TOLERANCE_DEGREES = 1e-6
# A metre of rig is 1e7 float32 ulps wide, so a tenth of a micrometre is the floor for a
# position that has been through a matrix inverse and back.
POSITION_TOLERANCE = 1e-6
AXIS_ROUNDING = 5e-4


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
            "aim_at": {"target_object": camera.name, "track_axis": "Z", "up_axis": "X", "up_reference": (0, 0, 1)},
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
    [{"bone_name": "head", "aim_at": {"target": (0.0, -2.0, 0.75), "track_axis": "Z", "up_axis": "X"}}],
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
        rig.name, [{"bone_name": "head", "aim_at": {"target": tuple(head_world), "track_axis": "Z"}}]
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
                    "target": tuple(head_world + Vector((0, 0, 1))),
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
        [{"bone_name": "head", "aim_at": {"target": (0, -2, 0.75), "track_axis": "Z", "up_axis": "-Z"}}],
    ),
    "different bone axis than track_axis",
)
refuses(
    lambda: handler.set_character_pose(
        rig.name,
        [
            {
                "bone_name": "head",
                "aim_at": {"target": (0, -2, 0.75), "track_axis": "Z", "up_axis": "X", "up_reference": (0, 0, 0)},
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
        rig.name, [{"bone_name": "head", "aim_at": {"target": behind, "track_axis": "Z"}}]
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

# Authoring a second action moves the rig onto it, and the reply says what it displaced.
displaced = handler.keyframe_character_pose(
    rig.name, "SMOKE_displacer", 1.0, [{"bone_name": "spine", "rotation_euler": (0.0, 0.0, 0.0)}]
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
# Nine rounded numbers, one per line at the reply's indentation: about 180 bytes a bone. Twice
# that would mean the rounding or the flat shape had been lost.
assert 100 < per_bone < 260, f"a rest-axis row costs {per_bone} bytes"

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
        [{"bone_name": "head", "aim_at": {"target": target, "track_axis": "Z", "up_axis": "X"}}],
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
    "aim_at": {"target_object": camera.name, "track_axis": "Z", "up_axis": "X", "up_reference": (0, 0, 1)},
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
            "aim_at": {"target": (-3.0, 1.0, 0.9), "track_axis": "Z", "up_axis": "X", "up_reference": (0, 0, 1)},
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
        [{"bone_name": "head", "aim_at": {"target_object": camera.name, "track_axis": "Z"}}],
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

print(f"aim error: object {object_error:.12f} deg, world point {point_error:.12f} deg")
print(f"keyed playback error: frame 1 {first_frame_error:.12f} deg, frame 24 {playback_error:.12f} deg")
print("absolute-space divergence (m): " + ", ".join(f"{space} {value:.3e}" for space, value in divergence.items()))
print(
    f"reopened action users {reopened_action.users}, curves {len(action_fcurves(reopened_action))}, drift {drift:.3e}"
)
shutil.rmtree(scratch, ignore_errors=True)
print("CHARACTER_POSING_SMOKE_OK")
