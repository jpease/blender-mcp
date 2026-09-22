"""
Blender 5.1+ background smoke coverage for what `set_action_cycle` reports about a cycle.

A test agent authored a walk through this MCP and shipped a broken shot: the legs cycled at
stride pitch, the arms - whose curves had picked up a later gesture's keys - repeated a 161-frame
extent instead, and a `cycles_after=6` stopped the whole thing dead at frame 169, after which the
raw curve's constant extrapolation snapped the root and froze it. Blender did exactly what it was
asked every time. Nothing in the reply said what period each curve would repeat at, or where a
finite count stops, so nothing caught it.

A later rehearsal hit the structural half of the same trap: a curve cycled at 20 frames, three
explicit strides keyed onto it afterwards, and `period_frames: 60.0` read back from a reply that
still said success. The period is the curve's own key extent, so no parameter fixes that - what
a caller can have is a refusal. This script proves `expected_period_frames` names the offending
curve and its measured extent and creates nothing, that a forward loop is no longer also an
unrequested backward one, and that a restricted range confines a cycle to part of a shot.

The period comes from `keyframe_points` and the stop frame from Blender's own Cycles evaluation,
neither of which the unit suite's fake `bpy` can produce: this script measures both against the
real API.

Run with::

    blender --background --factory-startup --python tests/blender_cycle_period_smoke.py
"""

# Blender runtime types are dynamic in this executable harness.

import importlib
import sys
import types

from pathlib import Path

import bpy

addon_path = Path(__file__).resolve().parents[1] / "src" / "blender_mcp" / "bundled" / "addon"
package_name = "blender_mcp_cycle_period_smoke"
addon = types.ModuleType(package_name)
addon.__path__ = [str(addon_path)]
addon.ADDON_ID = package_name
sys.modules[package_name] = addon
character_handlers = importlib.import_module(f"{package_name}.handlers.character_rigging")
animation = importlib.import_module(f"{package_name}.handlers.animation")

ACTION = "CyclePeriodTest"
# The legs of the failing shot: keyed across one stride, so they repeat a 24-frame extent.
LEG_KEYS = ((1.0, 0.0), (25.0, 0.5))
# The arms: the same cycle plus a gesture keyed at 162 long after the modifier went on, which
# is all it takes to turn a 24-frame loop into one 161-frame interpolation.
ARM_KEYS = ((1.0, 0.0), (162.0, 0.5))
LEG_PERIOD = LEG_KEYS[-1][0] - LEG_KEYS[0][0]
ARM_PERIOD = ARM_KEYS[-1][0] - ARM_KEYS[0][0]
CYCLES_AFTER = 6
LEG_PATH = 'pose.bones["leg"].location'
ARM_PATH = 'pose.bones["arm"].location'

# The second finding, reproduced below from frame numbers the rehearsal actually used: a curve
# cycled at 20 frames, three explicit strides keyed onto it afterwards, and a reply that still
# read as success while `period_frames` had become 59.
GUARD_ACTION = "CyclePeriodGuardTest"
# Frame 21 is frame 1 of the next stride, so this extent is exactly one 20-frame cycle.
GUARD_KEYS = ((1.0, 0.0), (21.0, 0.4))
GUARD_PERIOD = GUARD_KEYS[-1][0] - GUARD_KEYS[0][0]
GUARD_DELTA = GUARD_KEYS[-1][1] - GUARD_KEYS[0][1]
GESTURE_FRAME = 60.0
CYCLED_PATH = 'pose.bones["cycled"].location'
OTHER_PATH = 'pose.bones["other"].location'
# Where a cycle confined to part of a shot applies: three whole strides after the keyed one.
RANGE_START = GUARD_KEYS[-1][0]
RANGE_END = RANGE_START + 3 * GUARD_PERIOD


class CyclePeriodSmokeHarness(
    character_handlers.CharacterRiggingHandlersMixin,
    animation.AnimationHandlersMixin,
):
    """The two handler domains this script drives, without starting the socket server."""


def build_rig(name, bone_names):
    """Build a rig with one independent bone per named channel, side by side along X."""
    data = bpy.data.armatures.new(f"{name}Data")
    rig = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(rig)
    bpy.context.view_layer.objects.active = rig
    rig.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    for index, bone_name in enumerate(bone_names):
        bone = data.edit_bones.new(bone_name)
        bone.head = (0.3 * index, 0.0, 0.0)
        bone.tail = (0.3 * index, 0.0, 0.4)
    bpy.ops.object.mode_set(mode="OBJECT")
    rig.select_set(False)
    bpy.context.view_layer.update()
    return rig


def action_fcurves(action) -> list:
    """Every F-Curve in a legacy or layered action, whichever shape this Blender used."""
    curves: list = list(getattr(action, "fcurves", ()))
    for layer in getattr(action, "layers", ()):
        for strip in getattr(layer, "strips", ()):
            for bag in getattr(strip, "channelbags", ()):
                curves.extend(bag.fcurves)
    return curves


def curve_for(action, data_path, array_index):
    """Find one exact channel, or fail saying which channels the action does hold."""
    for curve in action_fcurves(action):
        if curve.data_path == data_path and curve.array_index == array_index:
            return curve
    paths = sorted({curve.data_path for curve in action_fcurves(action)})
    raise AssertionError(f"no curve for {data_path}[{array_index}]; action holds {paths}")


def record_for(reply, data_path, array_index=0):
    """One per-curve record out of a cycle reply."""
    for record in reply["modifiers"]:
        if record["data_path"] == data_path and record["array_index"] == array_index:
            return record
    raise AssertionError(f"no record for {data_path}[{array_index}] in {reply['modifiers']}")


def warning_containing(reply, fragment):
    """Return the one warning carrying a fragment, or fail printing every warning there was."""
    for warning in reply["warnings"]:
        if fragment in warning:
            return warning
    raise AssertionError(f"no warning containing {fragment!r}; got {reply['warnings']}")


handler = CyclePeriodSmokeHarness()
rig = build_rig("CycleRig", ("leg", "arm"))
target = {"type": "OBJECT", "name": rig.name}

for bone, keys in (("leg", LEG_KEYS), ("arm", ARM_KEYS)):
    for frame, offset in keys:
        handler.keyframe_character_pose(rig.name, ACTION, frame, [{"bone_name": bone, "location": (offset, 0.0, 0.0)}])

action = bpy.data.actions[ACTION]

# --- item 1: the reply names the period each curve will really repeat at -----------------------
cycled = handler.set_action_cycle(target, ACTION)

leg_record = record_for(cycled, LEG_PATH)
arm_record = record_for(cycled, ARM_PATH)
assert leg_record["first_key_frame"] == LEG_KEYS[0][0], leg_record
assert leg_record["last_key_frame"] == LEG_KEYS[-1][0], leg_record
assert leg_record["period_frames"] == LEG_PERIOD, leg_record
assert arm_record["period_frames"] == ARM_PERIOD, arm_record
# Every selected channel reports a period, and each is one of the two extents that were keyed.
assert all(record["period_frames"] in {LEG_PERIOD, ARM_PERIOD} for record in cycled["modifiers"]), cycled["modifiers"]

disagreement = warning_containing(cycled, "do not share one cycle period")
assert f"{LEG_PERIOD:g} frames" in disagreement, disagreement
assert f"{ARM_PERIOD:g} frames" in disagreement, disagreement
assert LEG_PATH in disagreement and ARM_PATH in disagreement, disagreement

# The period is Blender's, not arithmetic this script agreed with itself about: the leg curve
# repeats exactly what the reply said it would.
leg_curve = curve_for(action, LEG_PATH, 0)
cycled_here = leg_curve.evaluate(5.0 + LEG_PERIOD)
expected_offset = leg_curve.evaluate(5.0) + (LEG_KEYS[-1][1] - LEG_KEYS[0][1])
assert abs(cycled_here - expected_offset) < 1e-5, (
    f"the reported {LEG_PERIOD:g}-frame period is not the one Blender repeats: frame {5.0 + LEG_PERIOD} "
    f"evaluates to {cycled_here:.6f}, not {expected_offset:.6f}"
)

# --- item 2: a finite count names the frame it stops at, and what governs past it --------------
bounded = handler.set_action_cycle(target, ACTION, cycles_after=CYCLES_AFTER, data_path_prefix=LEG_PATH)

bounded_record = record_for(bounded, LEG_PATH)
stop_frame = LEG_KEYS[-1][0] + CYCLES_AFTER * LEG_PERIOD
assert bounded_record["repeat_end_frame"] == stop_frame, bounded_record
assert "repeat_start_frame" not in bounded_record, "cycles_before=0 is unlimited and bounds nothing"
finite = warning_containing(bounded, f"cycles_after={CYCLES_AFTER}")
assert f"frame {stop_frame:g}" in finite, finite
assert "extrapolation" in finite, finite

# Blender agrees about where it stops: the last repeat still lands on the cycled value at the
# reported frame, and one frame past it the modifier is out and the raw curve holds its last key
# - the snap and freeze the shot ended on.
last_repeat = leg_curve.evaluate(stop_frame)
past_repeat = leg_curve.evaluate(stop_frame + 1.0)
expected_last = LEG_KEYS[-1][1] + CYCLES_AFTER * (LEG_KEYS[-1][1] - LEG_KEYS[0][1])
assert abs(last_repeat - expected_last) < 1e-5, (
    f"frame {stop_frame:g} was reported as the last repeat but evaluates to {last_repeat:.6f}, not {expected_last:.6f}"
)
assert abs(past_repeat - LEG_KEYS[-1][1]) < 1e-5, (
    f"past frame {stop_frame:g} the raw curve's extrapolation should hold {LEG_KEYS[-1][1]:.6f}, got {past_repeat:.6f}"
)

# --- item 3: a key landing outside a cycle the curve already carries says what it changed ------
stretch_frame = 400.0
stretched = handler.keyframe_character_pose(
    rig.name, ACTION, stretch_frame, [{"bone_name": "leg", "location": (0.9, 0.0, 0.0)}], action_policy="REUSE"
)
extension = warning_containing(stretched, "already")
assert "Bone 'leg'" in extension, extension
assert f"frame {stretch_frame:g}" in extension, extension
assert f"instead of {LEG_PERIOD:g}" in extension, extension
assert len(stretched["warnings"]) == 1, "one warning per bone, not one per channel"
assert any(abs(point.co[0] - stretch_frame) < 1e-6 for point in leg_curve.keyframe_points), (
    "the key was refused rather than warned about; extending a cycle on purpose is legitimate"
)

inside = handler.keyframe_character_pose(
    rig.name, ACTION, 12.0, [{"bone_name": "arm", "location": (0.2, 0.0, 0.0)}], action_policy="REUSE"
)
assert inside["warnings"] == [], f"a key inside the cycle warned anyway: {inside['warnings']}"

# --- item 4: a forward loop is not also a backward one ----------------------------------------
guard_rig = build_rig("CycleGuardRig", ("cycled", "other"))
guard_target = {"type": "OBJECT", "name": guard_rig.name}
# Keyed through the generic writer, one channel per bone: this finding is about what an
# F-Curve repeats, so the fewer channels in the slot the sharper every claim below is.
handler.edit_keyframes(
    guard_target,
    [
        {"data_path": f'pose.bones["{bone}"].location', "array_index": 0, "frame": frame, "value": offset}
        for bone in ("cycled", "other")
        for frame, offset in GUARD_KEYS
    ],
    action_name=GUARD_ACTION,
)
guard_action = bpy.data.actions[GUARD_ACTION]
guard_curve = curve_for(guard_action, CYCLED_PATH, 0)

authored = handler.set_action_cycle(
    guard_target, GUARD_ACTION, expected_period_frames=GUARD_PERIOD, data_path_prefix=CYCLED_PATH
)
authored_record = record_for(authored, CYCLED_PATH)
assert authored_record["period_frames"] == GUARD_PERIOD, authored_record
assert authored_record["mode_before"] == "NONE", authored_record
assert authored_record["mode_after"] == "REPEAT_OFFSET", authored_record

# Forward it loops, and Blender accumulates the stride: frame 21 + one period is one delta on.
forward = guard_curve.evaluate(GUARD_KEYS[-1][0] + GUARD_PERIOD)
assert abs(forward - (GUARD_KEYS[-1][1] + GUARD_DELTA)) < 1e-5, (
    f"frame {GUARD_KEYS[-1][0] + GUARD_PERIOD:g} should evaluate {GUARD_KEYS[-1][1] + GUARD_DELTA:.6f} one cycle "
    f"on, got {forward:.6f}"
)
# Backward it does nothing, because nobody asked: the raw curve's constant extrapolation holds
# the first key. Under the old REPEAT_OFFSET default this frame evaluated one delta the other
# way, walking the character backwards out of the set before the shot began.
backward = guard_curve.evaluate(GUARD_KEYS[0][0] - GUARD_PERIOD)
assert abs(backward - GUARD_KEYS[0][1]) < 1e-5, (
    f"mode_before defaults to NONE, so frame {GUARD_KEYS[0][0] - GUARD_PERIOD:g} should hold "
    f"{GUARD_KEYS[0][1]:.6f}; got {backward:.6f}, which is an unrequested backward cycle"
)

# --- item 5: a later key redefines the period, and expected_period_frames refuses it ----------
# The gesture the rehearsal keyed long after the cycle went on. Nothing refuses it, and
# nothing should: extending a cycle on purpose is legitimate. It just is not a cycle any more.
handler.edit_keyframes(
    guard_target,
    [{"data_path": CYCLED_PATH, "array_index": 0, "frame": GESTURE_FRAME, "value": 0.9}],
    action_name=GUARD_ACTION,
)
measured = record_for(handler.set_action_cycle(guard_target, GUARD_ACTION, data_path_prefix=CYCLED_PATH), CYCLED_PATH)
assert measured["last_key_frame"] == GESTURE_FRAME, measured
assert measured["period_frames"] == GESTURE_FRAME - GUARD_KEYS[0][0], (
    f"one gesture key at frame {GESTURE_FRAME:g} must redefine the period this curve repeats: {measured}"
)

other_curve = curve_for(guard_action, OTHER_PATH, 0)
assert not any(modifier.type == "CYCLES" for modifier in other_curve.modifiers), (
    "the 'other' bone has never been cycled; the refusal below must find it that way"
)
try:
    handler.set_action_cycle(guard_target, GUARD_ACTION, expected_period_frames=GUARD_PERIOD)
except ValueError as failure:
    refusal = str(failure)
else:
    raise AssertionError(
        f"a curve now measuring {measured['period_frames']:g} frames was cycled at "
        f"expected_period_frames={GUARD_PERIOD:g} without complaint"
    )
assert f"expected_period_frames={GUARD_PERIOD:g}" in refusal, refusal
assert f"keys {GUARD_KEYS[0][0]:g}-{GESTURE_FRAME:g}" in refusal, refusal
assert f"a {GESTURE_FRAME - GUARD_KEYS[0][0]:g}-frame extent" in refusal, refusal
assert "NLA strip" in refusal, refusal
# Validate the whole selection, then mutate: the refusal named the third curve, so the first
# two - the 'other' bone's, which did match - must still carry no modifier.
assert not any(modifier.type == "CYCLES" for modifier in other_curve.modifiers), (
    "a refused call left a Cycles modifier on a curve it was never allowed to cycle"
)

# --- item 6: a restricted range bounds where the cycle applies, not what it repeats -----------
ranged = handler.set_action_cycle(
    guard_target,
    GUARD_ACTION,
    expected_period_frames=GUARD_PERIOD,
    frame_start=RANGE_START,
    frame_end=RANGE_END,
    data_path_prefix=OTHER_PATH,
)
ranged_record = record_for(ranged, OTHER_PATH)
assert ranged_record["restricted_range"] == {
    "frame_start": RANGE_START,
    "frame_end": RANGE_END,
    "blend_in": 0.0,
    "blend_out": 0.0,
}, ranged_record
assert ranged_record["period_frames"] == GUARD_PERIOD, (
    f"a restricted range bounds where the modifier applies and changes no period: {ranged_record}"
)
ranged_modifier = next(modifier for modifier in other_curve.modifiers if modifier.type == "CYCLES")
assert ranged_modifier.use_restricted_range, "frame_start/frame_end must switch the restriction on"
assert (ranged_modifier.frame_start, ranged_modifier.frame_end) == (RANGE_START, RANGE_END), (
    f"the window collapsed on the way in: {ranged_modifier.frame_start:g} to {ranged_modifier.frame_end:g}"
)

# Inside the window Blender cycles and accumulates; one frame past it the modifier is out and
# the raw curve holds its last key. That boundary is what confines a loop to part of a shot.
inside_range = other_curve.evaluate(RANGE_END)
outside_range = other_curve.evaluate(RANGE_END + 1.0)
expected_inside = GUARD_KEYS[-1][1] + 3 * GUARD_DELTA
assert abs(inside_range - expected_inside) < 1e-5, (
    f"frame {RANGE_END:g} is the last frame of a three-stride window and should evaluate "
    f"{expected_inside:.6f}; got {inside_range:.6f}"
)
assert abs(outside_range - GUARD_KEYS[-1][1]) < 1e-5, (
    f"frame {RANGE_END + 1:g} is outside the window, so the raw curve should hold "
    f"{GUARD_KEYS[-1][1]:.6f}; got {outside_range:.6f}"
)

# --- item 7: and the reply says so, before a render has to ------------------------------------
# The evaluation above is the whole defect a later rehearsal shipped: it bounded a travelling
# root cycle at frame 141, read the world position at 150, and got the value one raw period
# ends on - the character back on his starting mark, having followed every documented rule.
# `frame_start`/`frame_end` read as a scoping convenience and behave as a cliff, so the call
# that sets one now says what governs past it.
bounded_range = warning_containing(ranged, f"frame_end={RANGE_END:g}")
assert "the curve evaluates from its own keys alone" in bounded_range, bounded_range
assert "REPEAT_OFFSET's accumulated travel is not part of that hold" in bounded_range, bounded_range
assert "no key out there" in bounded_range, bounded_range
# mode_before is NONE, so the start bound extrapolates nothing and warns about nothing.
assert not any(warning.startswith("frame_start=") for warning in ranged["warnings"]), ranged["warnings"]

# --- item 8: a pose-bone cycle addressed at the armature datablock names the object ----------
# Blender keys `pose.bones[...]` under the armature *object's* slot, so an ARMATURE target is
# always wrong here and the old refusal named the datablock without naming the remedy. Thirteen
# refused calls in one rehearsal.
armature_target = {"type": "ARMATURE", "name": guard_rig.data.name}
try:
    handler.set_action_cycle(armature_target, GUARD_ACTION, data_path_prefix=CYCLED_PATH)
except ValueError as failure:
    scoped_refusal = str(failure)
else:
    raise AssertionError("a pose-bone prefix on an ARMATURE datablock target was accepted")
assert "live on the armature object" in scoped_refusal, scoped_refusal
assert '"type": "OBJECT"' in scoped_refusal, scoped_refusal

try:
    handler.set_action_cycle(armature_target, GUARD_ACTION)
except ValueError as failure:
    slot_refusal = str(failure)
else:
    raise AssertionError("an ARMATURE datablock resolved a slot that holds pose-bone curves")
assert f'retry with target={{"type": "OBJECT", "name": "{guard_rig.name}"}}' in slot_refusal, slot_refusal


# Moving the window forward past where it already was is what a second pass over a shot does.
# An F-Modifier keeps frame_start <= frame_end by dragging whichever bound was not assigned,
# so both have to be written: this is where a call that wrote only one would land wrong.
moved_start, moved_end = RANGE_END + GUARD_PERIOD, RANGE_END + 4 * GUARD_PERIOD
handler.set_action_cycle(
    guard_target, GUARD_ACTION, frame_start=moved_start, frame_end=moved_end, data_path_prefix=OTHER_PATH
)
assert (ranged_modifier.frame_start, ranged_modifier.frame_end) == (moved_start, moved_end), (
    f"re-ranging forward collapsed the window to {ranged_modifier.frame_start:g}-"
    f"{ranged_modifier.frame_end:g}, not {moved_start:g}-{moved_end:g}"
)

# And omitting it widens back to the whole timeline rather than keeping the last window.
handler.set_action_cycle(guard_target, GUARD_ACTION, data_path_prefix=OTHER_PATH)
assert not ranged_modifier.use_restricted_range, "a re-run without a window must clear the old one"

print(
    f"periods over {cycled['curve_count']} curves: leg {leg_record['period_frames']:g}, "
    f"arm {arm_record['period_frames']:g}"
)
print(f"disagreement: {disagreement}")
print(f"finite: {finite}")
print(f"stop frame {stop_frame:g} evaluates {last_repeat:.4f}, frame {stop_frame + 1:g} evaluates {past_repeat:.4f}")
print(f"extension: {extension}")
print(f"forward one cycle: {forward:.4f}; backward with mode_before=NONE: {backward:.4f}")
print(f"period after the frame {GESTURE_FRAME:g} key: {measured['period_frames']:g}")
print(f"refusal: {refusal}")
print(
    f"window {RANGE_START:g}-{RANGE_END:g}: frame {RANGE_END:g} evaluates {inside_range:.4f}, "
    f"frame {RANGE_END + 1:g} evaluates {outside_range:.4f}"
)
print(f"bounded range: {bounded_range}")
print(f"pose-bone prefix on an armature datablock: {scoped_refusal}")
print(f"unscoped armature target: {slot_refusal}")
print("CYCLE_PERIOD_SMOKE_OK")
