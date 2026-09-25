"""
Blender 5.1+ background smoke coverage for the walk-cycle path: planted feet, bent knees, loops.

Each assertion here is tied to a symptom reported from a real first-attempt walk cycle authored
through this MCP - a foot that slid forward while the body moved, knees that stayed straight, and
motion that read as robotic. The unit suite drives these handlers through a fake `bpy`, which can
prove the handler asked Blender for something but never that Blender's IK solver, F-Curve
modifiers and playhead actually produced it.

Run with::

    blender --background --factory-startup --python tests/blender_walk_cycle_smoke.py
"""

# Blender runtime types are dynamic in this executable harness.

import importlib
import math
import sys

from pathlib import Path

import bpy

from mathutils import Vector

sys.path.append(str(Path(__file__).resolve().parent))
from smoke_addon import load_addon

package_name = "blender_mcp_walk_cycle_smoke"
load_addon(package_name)
character_handlers = importlib.import_module(f"{package_name}.handlers.character_rigging")
object_animation = importlib.import_module(f"{package_name}.handlers.object_animation")
animation = importlib.import_module(f"{package_name}.handlers.animation")
scene_handlers = importlib.import_module(f"{package_name}.handlers.scene")

# The plant must hold to well under a millimetre: a foot that moves a millimetre across twelve
# frames is not visible, one that moves a centimetre is the reported skate.
PLANT_TOLERANCE_M = 1e-3
# What Blender's own IK converges to on a bent three-bone chain at 500 iterations.
REACH_TOLERANCE_M = 1e-4
CONTACT_FRAMES = range(1, 13)
SWING_FRAMES = range(13, 25)
CYCLE_TRAVEL_M = 1.0
# The plant sits ahead of the hip's starting position, the way a real contact does: the leg
# reaches forward at contact and is behind the body by toe-off. Placing it under the hip instead
# puts the frame-12 target outside the leg's reach and the solve would report that.
PLANT_POINT = (0.35, -0.12, 0.1)


class WalkCycleSmokeHarness(
    character_handlers.CharacterRiggingHandlersMixin,
    object_animation.ObjectAnimationHandlersMixin,
    animation.AnimationHandlersMixin,
    scene_handlers.SceneHandlersMixin,
):
    """Expose the four handler domains a walk cycle needs, without starting the socket server."""


def build_leg_rig(name):
    """
    Build root -> hips -> thigh.L -> shin.L -> foot.L with a real bend already at the knee.

    The knee bends in the YZ plane about local X, so the hinge the reach applies has one correct
    axis and the rest pose has a direction a pole can be inferred from. A dead-straight leg is
    refused by pole synthesis by design, which is the other half of the reported "knees stay
    straight": there is nothing to infer.
    """
    data = bpy.data.armatures.new(f"{name}Data")
    rig = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(rig)
    bpy.context.view_layer.objects.active = rig
    rig.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    root = data.edit_bones.new("root")
    root.head, root.tail = (0, 0, 0), (0, 0, 0.1)
    hips = data.edit_bones.new("hips")
    hips.head, hips.tail = (0, 0, 1.0), (0, 0, 1.15)
    hips.parent = root
    thigh = data.edit_bones.new("thigh.L")
    thigh.head, thigh.tail = (0.1, 0, 1.0), (0.1, 0.02, 0.55)
    thigh.parent = hips
    shin = data.edit_bones.new("shin.L")
    shin.head, shin.tail = (0.1, 0.02, 0.55), (0.1, 0, 0.1)
    shin.parent, shin.use_connect = thigh, True
    foot = data.edit_bones.new("foot.L")
    foot.head, foot.tail = (0.1, 0, 0.1), (0.1, -0.12, 0.1)
    foot.parent, foot.use_connect = shin, True
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


def key_at(curve, frame):
    """Return the keyframe point at one frame, or None - frames compare as floats."""
    return next((point for point in curve.keyframe_points if abs(point.co[0] - frame) <= 1e-6), None)


def evaluated_world(rig, bone_name):
    """Where a bone's head and tail actually are after the depsgraph has run."""
    evaluated = rig.evaluated_get(bpy.context.evaluated_depsgraph_get())
    bone = evaluated.pose.bones[bone_name]
    matrix = evaluated.matrix_world
    return matrix @ bone.head, matrix @ bone.tail


def swing_target(frame):
    """
    Carry the foot forward one cycle's travel across the swing, folding the knee as it passes.

    The lift is 0.35 m, not a token clearance. A foot dragged forward barely above the floor
    stays at nearly full leg extension the whole way, and the solver reaches it by bending the
    knee BACKWARDS - measured at -31 degrees at the apex on this rig. Lifting the foot shortens
    the hip-to-target distance to roughly 55% of the chain's reach, which is only solvable with
    the knee folded the way a knee folds.
    """
    progress = (frame - SWING_FRAMES.start) / (SWING_FRAMES.stop - 1 - SWING_FRAMES.start)
    return (
        PLANT_POINT[0] + CYCLE_TRAVEL_M * progress,
        PLANT_POINT[1],
        PLANT_POINT[2] + 0.35 * math.sin(math.pi * progress),
    )


handler = WalkCycleSmokeHarness()
scene = bpy.context.scene
scene.frame_start, scene.frame_end = 1, 25
rig = build_leg_rig("Walker")

# --- the body travels first, because every reach is solved against the body at that frame ------
# Frame 6 is keyed on the same straight line as 1 and 25, so the motion is unchanged by its
# presence; it is there for the interpolation-bleed check further down.
root_keys = [
    {"object_name": rig.name, "frame": 1.0, "location": [0.0, 0.0, 0.0]},
    {"object_name": rig.name, "frame": 6.0, "location": [CYCLE_TRAVEL_M * 5 / 24, 0.0, 0.0]},
    {"object_name": rig.name, "frame": 25.0, "location": [CYCLE_TRAVEL_M, 0.0, 0.0]},
]
handler.keyframe_object_transform(root_keys, interpolation="LINEAR", action_name="WalkTest")

# --- one call plants one foot for twelve frames and swings it for twelve more ------------------
keys = [{"frame": float(frame), "target_point": list(PLANT_POINT)} for frame in CONTACT_FRAMES]
keys += [{"frame": float(frame), "target_point": list(swing_target(frame))} for frame in SWING_FRAMES]
reply = handler.keyframe_bone_reach(
    rig.name,
    "WalkTest",
    [
        {
            "tip_bone": "foot.L",
            "keys": keys,
            # Explicit, because the unbranched run above foot.L reaches the root: letting IK
            # move the hips would undo the travel just keyed into the same action.
            "chain_length": 3,
            "pole_target_point": [0.1, 1.0, 0.55],
            # A knee folds one way only, and which way is rig-specific: on this rig the fold is
            # NEGATIVE about the shin's local X, measured against real Blender rather than
            # assumed. The 0 ceiling is the anti-inversion guard - the joint may straighten to
            # rest and no further. An inverted range (0..150) stalls eight of the swing frames
            # against the limit, which is the tool reporting an impossible request rather than
            # keying a broken leg.
            "hinge": {"bone_name": "shin.L", "axis": "X", "min_degrees": -150.0, "max_degrees": 0.0},
        }
    ],
    tolerance_m=REACH_TOLERANCE_M,
    interpolation="BEZIER",
    handle_left="VECTOR",
    handle_right="VECTOR",
    easing="EASE_IN_OUT",
)

assert reply["action"] == "WalkTest", reply["action"]
assert reply["assigned_action"] == "WalkTest", "the rig must be left driven by the action just keyed"
assert reply["keyed_frames"] == [float(frame) for frame in range(1, 25)], reply["keyed_frames"]
assert set(reply["changed_bones"]) == {"foot.L", "shin.L", "thigh.L"}, reply["changed_bones"]
solved = reply["reaches"][0]
assert solved["chain_length"] == 3, solved
assert solved["chain_length_source"] == "explicit", solved
assert solved["pole_source"] == "explicit", solved
missed = [record for record in solved["keys"] if not record["converged"]]
assert not missed, f"{len(missed)} frame(s) did not converge: {missed[:2]}; warnings {reply['warnings']}"

# The hinge is not decoration. Re-solve the same swing with the knee clamped to nearly straight
# and the frames that need a folded knee must stall: a swing is only reachable with the joint
# bent. If this ever stops stalling, the limit is no longer reaching Blender's solver, and
# nothing is stopping a knee inverting either.
clamped = handler.keyframe_bone_reach(
    rig.name,
    "WalkTest",
    [
        {
            "tip_bone": "foot.L",
            "keys": [{"frame": float(frame), "target_point": list(swing_target(frame))} for frame in SWING_FRAMES],
            "chain_length": 3,
            "pole_target_point": [0.1, 1.0, 0.55],
            "hinge": {"bone_name": "shin.L", "axis": "X", "min_degrees": -5.0, "max_degrees": 0.0},
        }
    ],
    tolerance_m=REACH_TOLERANCE_M,
)
stalled = [record["frame"] for record in clamped["reaches"][0]["keys"] if not record["converged"]]
assert stalled, "clamping the knee to nearly straight changed nothing, so the hinge never reached the solver"
assert clamped["warnings"], "a stalled frame must raise a warning naming it"
assert any("Frame" in warning for warning in clamped["warnings"]), clamped["warnings"]

# Put the converged solve back, since the clamped one overwrote the swing frames.
handler.keyframe_bone_reach(
    rig.name,
    "WalkTest",
    [
        {
            "tip_bone": "foot.L",
            "keys": keys,
            "chain_length": 3,
            "pole_target_point": [0.1, 1.0, 0.55],
            "hinge": {"bone_name": "shin.L", "axis": "X", "min_degrees": -150.0, "max_degrees": 0.0},
        }
    ],
    tolerance_m=REACH_TOLERANCE_M,
    interpolation="BEZIER",
    handle_left="VECTOR",
    handle_right="VECTOR",
    easing="EASE_IN_OUT",
)

# --- symptom 1: the planted foot must not slide while the body travels over it -----------------
plant = Vector(PLANT_POINT)
deviations = {}
for frame in CONTACT_FRAMES:
    handler.set_scene_frame(frame)
    _head, tail = evaluated_world(rig, "foot.L")
    deviations[frame] = (tail - plant).length
worst_frame = max(deviations, key=lambda frame: deviations[frame])
assert deviations[worst_frame] < PLANT_TOLERANCE_M, (
    f"the planted foot moved {deviations[worst_frame]:.6f} m at frame {worst_frame}, "
    f"over the {PLANT_TOLERANCE_M} m allowance"
)

handler.set_scene_frame(1)
start_x = rig.evaluated_get(bpy.context.evaluated_depsgraph_get()).matrix_world.translation.x
handler.set_scene_frame(12)
end_x = rig.evaluated_get(bpy.context.evaluated_depsgraph_get()).matrix_world.translation.x
travelled = end_x - start_x
assert travelled > 0.3, f"the body only travelled {travelled:.4f} m, so a still foot proves nothing"

# --- symptom 2: the knee has to actually bend -------------------------------------------------
# Measured at the swing apex, not mid-stance. A leg in contact with the ground is nearly
# straight and should be; the reported "knees stay straight" is about the swing, where a leg
# that does not fold is a leg being dragged through the floor.


def knee_interior_degrees(frame):
    """Return the angle hip-knee-ankle in world space: 180 degrees is a straight leg."""
    handler.set_scene_frame(frame)
    hip, knee = evaluated_world(rig, "thigh.L")
    _knee_head, ankle = evaluated_world(rig, "shin.L")
    return math.degrees((hip - knee).angle(ankle - knee))


apex_degrees = knee_interior_degrees(18)
stance_degrees = knee_interior_degrees(6)
assert apex_degrees < 140.0, f"the knee did not fold through the swing: {apex_degrees:.2f} degrees at the apex"
assert stance_degrees < 175.0, f"the knee locked dead straight in stance: {stance_degrees:.2f} degrees at frame 6"
assert apex_degrees < stance_degrees - 20.0, (
    f"the knee barely moved between stance ({stance_degrees:.2f}) and swing ({apex_degrees:.2f})"
)

# --- symptom 3: the keys carry the shape the call asked for -----------------------------------
walk_action = bpy.data.actions["WalkTest"]
shaped = 0
for array_index in range(4):
    curve = curve_for(walk_action, 'pose.bones["shin.L"].rotation_quaternion', array_index)
    point = key_at(curve, 6.0)
    assert point is not None, f"no shin.L rotation key at frame 6 on index {array_index}"
    assert point.interpolation == "BEZIER", point.interpolation
    assert point.handle_left_type == "VECTOR", point.handle_left_type
    assert point.handle_right_type == "VECTOR", point.handle_right_type
    assert point.easing == "EASE_IN_OUT", point.easing
    shaped += 1

# --- the step-2 regression: styling one bone's key must not restyle the root's -----------------
# Both live in this one action at frame 6, which is exactly what the tool docstrings ask for.
root_point = key_at(curve_for(walk_action, "location", 0), 6.0)
assert root_point is not None, "the root's own frame-6 key went missing"
assert root_point.interpolation == "LINEAR", (
    f"keying a bone at frame 6 restyled the root's key at frame 6 to {root_point.interpolation}"
)

# --- symptom 4: it has to loop, and the travel has to accumulate -------------------------------
handler.set_action_cycle(
    {"type": "OBJECT", "name": rig.name},
    "WalkTest",
    data_path_prefix="location",
    mode_before="REPEAT_OFFSET",
    mode_after="REPEAT_OFFSET",
)
handler.set_action_cycle(
    {"type": "OBJECT", "name": rig.name},
    "WalkTest",
    data_path_prefix="pose.bones",
    mode_before="REPEAT",
    mode_after="REPEAT",
)

root_curve = curve_for(walk_action, "location", 0)
root_period = root_curve.keyframe_points[-1].co[0] - root_curve.keyframe_points[0].co[0]
offset_error = abs(root_curve.evaluate(3.0 + root_period) - (root_curve.evaluate(3.0) + CYCLE_TRAVEL_M))
assert offset_error < 1e-5, (
    f"REPEAT_OFFSET did not carry the travel forward: frame {3.0 + root_period} is off by {offset_error:.8f} m"
)

leg_curve = curve_for(walk_action, 'pose.bones["shin.L"].rotation_quaternion', 1)
leg_period = leg_curve.keyframe_points[-1].co[0] - leg_curve.keyframe_points[0].co[0]
repeat_error = abs(leg_curve.evaluate(3.0 + leg_period) - leg_curve.evaluate(3.0))
assert repeat_error < 1e-5, f"REPEAT did not repeat the leg: frame {3.0 + leg_period} differs by {repeat_error:.8f}"

removed = handler.set_action_cycle(
    {"type": "OBJECT", "name": rig.name},
    "WalkTest",
    "REMOVE",
    data_path_prefix="location",
)
assert removed["curve_count"] == 3, removed["curve_count"]
assert not any(modifier.type == "CYCLES" for modifier in root_curve.modifiers), "REMOVE left the modifier behind"

# --- symptom 5: the playhead moves, which is what makes any of the above reviewable ------------
moved = handler.set_scene_frame(17)
assert scene.frame_current == 17, scene.frame_current
assert moved["frame"] == 17, moved
assert moved["frame_start"] == 1 and moved["frame_end"] == 25, moved
assert moved["fps"] == scene.render.fps / (scene.render.fps_base or 1.0), moved

# --- the hinge and the solve leave nothing behind on the rig ------------------------------------
assert list(rig.pose.bones["foot.L"].constraints) == [], "a solved reach left an IK constraint on foot.L"
assert not any(obj.name.startswith("__solve_bone_reach__") for obj in bpy.data.objects), (
    "a solved reach left a scratch Empty in the file"
)
shin_bone = rig.pose.bones["shin.L"]
assert shin_bone.use_ik_limit_x is False, "the temporary hinge was not removed from shin.L"
assert shin_bone.lock_ik_y is False and shin_bone.lock_ik_z is False, "the hinge left its axis locks on shin.L"

worst_error = max(record["achieved_error_m"] for record in solved["keys"])
print(
    f"plant: worst deviation {deviations[worst_frame]:.9f} m at frame {worst_frame} "
    f"while the body travelled {travelled:.4f} m"
)
print(f"knee interior angle: stance (frame 6) {stance_degrees:.2f} deg, swing apex (frame 18) {apex_degrees:.2f} deg")
print(f"reach: worst achieved_error_m {worst_error:.9f} over {len(solved['keys'])} solved frames")
print(f"shaped keys checked: {shaped}; root key kept {root_point.interpolation}")
print(f"cycle: offset error {offset_error:.9f} m over period {root_period:g}, repeat error {repeat_error:.9f}")
print("WALK_CYCLE_SMOKE_OK")
