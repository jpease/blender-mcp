"""
Blender 5.1+ background smoke coverage for what `set_action_cycle` reports about a cycle.

A test agent authored a walk through this MCP and shipped a broken shot: the legs cycled at
stride pitch, the arms - whose curves had picked up a later gesture's keys - repeated a 161-frame
extent instead, and a `cycles_after=6` stopped the whole thing dead at frame 169, after which the
raw curve's constant extrapolation snapped the root and froze it. Blender did exactly what it was
asked every time. Nothing in the reply said what period each curve would repeat at, or where a
finite count stops, so nothing caught it.

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


class CyclePeriodSmokeHarness(
    character_handlers.CharacterRiggingHandlersMixin,
    animation.AnimationHandlersMixin,
):
    """The two handler domains this script drives, without starting the socket server."""


def build_two_bone_rig(name):
    """Build a rig with one bone per period, so one call can cycle two disagreeing extents."""
    data = bpy.data.armatures.new(f"{name}Data")
    rig = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(rig)
    bpy.context.view_layer.objects.active = rig
    rig.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    leg = data.edit_bones.new("leg")
    leg.head, leg.tail = (0.0, 0.0, 0.0), (0.0, 0.0, 0.4)
    arm = data.edit_bones.new("arm")
    arm.head, arm.tail = (0.3, 0.0, 0.0), (0.3, 0.0, 0.4)
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
rig = build_two_bone_rig("CycleRig")
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

print(
    f"periods over {cycled['curve_count']} curves: leg {leg_record['period_frames']:g}, "
    f"arm {arm_record['period_frames']:g}"
)
print(f"disagreement: {disagreement}")
print(f"finite: {finite}")
print(f"stop frame {stop_frame:g} evaluates {last_repeat:.4f}, frame {stop_frame + 1:g} evaluates {past_repeat:.4f}")
print(f"extension: {extension}")
print("CYCLE_PERIOD_SMOKE_OK")
