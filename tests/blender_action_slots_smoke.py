# ruff: file-ignore[module-import-not-at-top-of-file]
"""
Run with Blender 5.1+ to prove root motion and a pose can share one action and one slot.

This is the script the defect it guards would have failed. An agent keyed a character's root
motion with `keyframe_object_transform` and its pose with `keyframe_character_pose`, and the
second call moved the rig onto its own action: the location curves were still in the file,
still perfectly correct, and driving nothing at all, so the characters stood still. No unit
test sees that, because the fake `bpy` has no depsgraph to evaluate and no slotted action to
put the two kinds of curve in the same channelbag - only real Blender answers whether the one
assigned action holds both, and whether the object then actually moves.

Run with::

    blender --background --factory-startup --python tests/blender_action_slots_smoke.py
"""

import importlib.util
import math
import sys

from pathlib import Path

import bpy

addon_path = Path(__file__).resolve().parents[1] / "src" / "blender_mcp" / "bundled" / "addon" / "__init__.py"
package_name = "blender_mcp_action_slots_smoke"
spec = importlib.util.spec_from_file_location(
    package_name,
    addon_path,
    submodule_search_locations=[str(addon_path.parent)],
)
assert spec is not None
addon = importlib.util.module_from_spec(spec)
sys.modules[package_name] = addon
spec.loader.exec_module(addon)

from blender_mcp_action_slots_smoke.handlers.character_rigging import CharacterRiggingHandlersMixin
from blender_mcp_action_slots_smoke.handlers.object_animation import ObjectAnimationHandlersMixin

action_fcurve_collections = sys.modules[f"{package_name}.handlers.action_assignment"].action_fcurve_collections

SHOT_ACTION = "SMOKE_sh010_motion"
POSE_ONLY_ACTION = "SMOKE_sh010_pose"
START_FRAME = 1
END_FRAME = 24
# Far enough that a rig that did not move cannot pass by float noise, and a plain walk-in.
START_LOCATION = (0.0, 0.0, 0.0)
END_LOCATION = (4.0, 0.0, 0.0)
BONE_TURN_DEGREES = 35.0


class ActionSlotsSmokeHarness(ObjectAnimationHandlersMixin, CharacterRiggingHandlersMixin):
    """Expose both keying handlers on one object, without starting the socket server."""


def build_rig(name: str):
    """
    Build the smallest rig that has both an object transform and a pose: one bone.

    Args:
        name: Object name; its armature data takes the same name plus `Data`.

    Returns:
        bpy.types.Object: The armature object, left in Object Mode.

    """
    data = bpy.data.armatures.new(f"{name}Data")
    rig = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(rig)
    bpy.context.view_layer.objects.active = rig
    rig.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    bone = data.edit_bones.new("spine")
    bone.head, bone.tail = (0, 0, 0), (0, 0, 0.4)
    bpy.ops.object.mode_set(mode="OBJECT")
    rig.select_set(False)
    bpy.context.view_layer.update()
    return rig


def assigned_curves(obj) -> list:
    """
    Every F-Curve the assigned action holds in the slot this object is keyed through.

    Args:
        obj: The keyed object.

    Returns:
        list: Its F-Curves, which is the measurement the whole script turns on - a curve in
        another slot would evaluate for another ID and drive nothing here.

    """
    animation = obj.animation_data
    slot = animation.action_slot
    return [curve for collection in action_fcurve_collections(animation.action, slot) for curve in collection]


def refuses(call, fragment: str) -> None:
    """
    Require a call to refuse, naming what it refused over.

    Args:
        call: The zero-argument call expected to raise.
        fragment: Text the message must carry.

    Raises:
        AssertionError: If the call succeeded or refused for another reason.

    """
    try:
        call()
    except ValueError as error:
        assert fragment in str(error), f"expected {fragment!r} in {error}"
        return
    raise AssertionError(f"expected a refusal mentioning {fragment!r}")


def key_root_motion(handler, rig) -> None:
    """
    Key the rig's OBJECT location at both frames, into the shot's named action.

    Args:
        handler: The smoke harness.
        rig: The armature object.

    Raises:
        AssertionError: If the reply does not name the action and slot the keys landed in.

    """
    reply = handler.keyframe_object_transform(
        keyframes=[
            {"object_name": rig.name, "frame": START_FRAME, "space": "LOCAL", "location": START_LOCATION},
            {"object_name": rig.name, "frame": END_FRAME, "space": "LOCAL", "location": END_LOCATION},
        ],
        action_name=SHOT_ACTION,
    )
    assert reply["actions"] == [SHOT_ACTION], reply["actions"]
    # The slot is the half of the answer a bare action name cannot give: two IDs can share one
    # action, and only the slot says which of them these curves drive.
    assert reply["action_slot"] is not None, reply
    assert rig.animation_data.action.name == SHOT_ACTION


def key_pose(handler, rig) -> None:
    """
    Key the rig's POSE at both frames, into the same action the root motion is in.

    Args:
        handler: The smoke harness.
        rig: The armature object.

    Raises:
        AssertionError: If keying the pose displaced the action holding the root motion.

    """
    for frame, degrees in ((START_FRAME, 0.0), (END_FRAME, BONE_TURN_DEGREES)):
        reply = handler.keyframe_character_pose(
            rig.name,
            SHOT_ACTION,
            float(frame),
            [{"bone_name": "spine", "rotate": {"axis": "Z", "degrees": degrees}}],
            space="LOCAL",
        )
        assert reply["assigned_action"] == SHOT_ACTION, reply
        assert "unassigned_action" not in reply, reply


def check_one_action_holds_both(rig) -> tuple[int, int]:
    """
    Measure that the object's and the pose's curves share one assigned action and one slot.

    Args:
        rig: The armature object, already keyed by both handlers.

    Returns:
        tuple: How many location curves and how many pose-bone curves that slot holds.

    Raises:
        AssertionError: If either kind is missing, or a second action was authored.

    """
    assert rig.animation_data.action.name == SHOT_ACTION
    paths = [curve.data_path for curve in assigned_curves(rig)]
    location = [path for path in paths if path == "location"]
    pose = [path for path in paths if path.startswith('pose.bones["spine"]')]
    assert len(location) == 3, f"the assigned slot holds {len(location)} location curves, not 3: {paths}"
    assert pose, f"the assigned slot holds no pose-bone curves: {paths}"
    smoke_actions = sorted(action.name for action in bpy.data.actions if action.name.startswith("SMOKE_"))
    assert smoke_actions == [SHOT_ACTION], f"the pose was keyed into a second action: {smoke_actions}"
    return len(location), len(pose)


def check_the_rig_actually_moves(rig) -> float:
    """
    Evaluate the scene at both frames and measure what the file would play back.

    Args:
        rig: The armature object.

    Returns:
        float: How far the rig travelled between the two frames, in metres.

    Raises:
        AssertionError: If the rig stood still, or the pose did not play back with it.

    """
    scene = bpy.context.scene
    scene.frame_set(START_FRAME)
    bpy.context.view_layer.update()
    start = rig.matrix_world.translation.copy()
    start_pose = rig.pose.bones["spine"].matrix_basis.copy()

    scene.frame_set(END_FRAME)
    bpy.context.view_layer.update()
    travelled = (rig.matrix_world.translation - start).length
    start_rotation = start_pose.to_quaternion()
    end_rotation = rig.pose.bones["spine"].matrix_basis.to_quaternion()
    turned = math.degrees(start_rotation.rotation_difference(end_rotation).angle)

    assert travelled > 1.0, f"the rig travelled {travelled} m between the keyed frames"
    assert turned > 1.0, f"the bone turned {turned} degrees between the keyed frames"
    return travelled


def check_displacement_is_refused(handler, rig) -> None:
    """
    Keying a second action over one that holds keys is refused, and leaves nothing behind.

    Args:
        handler: The smoke harness.
        rig: The armature object, driven by the action holding the shot's motion.

    Raises:
        AssertionError: If the call was allowed, or a refusal left a stray action in the file.

    """
    pose = [{"bone_name": "spine", "rotate": {"axis": "Z", "degrees": 10.0}}]
    refuses(lambda: handler.keyframe_character_pose(rig.name, POSE_ONLY_ACTION, 36.0, pose), SHOT_ACTION)
    assert bpy.data.actions.get(POSE_ONLY_ACTION) is None, "a refused call left the action it would have made"
    assert rig.animation_data.action.name == SHOT_ACTION, "a refused call still moved the rig off its action"

    # The confirmation is the escape hatch, and it has to work: the reply names what it took
    # the rig off, which is the only record the agent gets that the earlier keys stopped playing.
    confirmed = handler.keyframe_character_pose(rig.name, POSE_ONLY_ACTION, 36.0, pose, confirm_displace_action=True)
    assert confirmed["assigned_action"] == POSE_ONLY_ACTION, confirmed
    assert confirmed["unassigned_action"] == SHOT_ACTION, confirmed


def check_one_action_per_object(handler, rig) -> None:
    """
    Require a batch naming one action for two objects to be refused rather than resolved.

    Args:
        handler: The smoke harness.
        rig: The armature object.

    Raises:
        AssertionError: If the batch was accepted.

    """
    prop = bpy.data.objects.new("SmokeProp", None)
    bpy.context.scene.collection.objects.link(prop)
    refuses(
        lambda: handler.keyframe_object_transform(
            keyframes=[
                {"object_name": rig.name, "frame": 48, "space": "LOCAL", "location": (1.0, 0.0, 0.0)},
                {"object_name": prop.name, "frame": 48, "space": "LOCAL", "location": (2.0, 0.0, 0.0)},
            ],
            action_name="SMOKE_sh010_shared",
        ),
        "once per object",
    )
    assert bpy.data.actions.get("SMOKE_sh010_shared") is None
    assert prop.animation_data is None, "a refused batch keyed the second object anyway"


def main() -> None:
    """Key one rig's root motion and its pose into one named action, and measure what plays back."""
    handler = ActionSlotsSmokeHarness()
    scene = bpy.context.scene
    scene.frame_start, scene.frame_end = START_FRAME, END_FRAME

    rig = build_rig("SmokeRig")
    key_root_motion(handler, rig)
    key_pose(handler, rig)
    location_curves, pose_curves = check_one_action_holds_both(rig)
    travelled = check_the_rig_actually_moves(rig)
    check_one_action_per_object(handler, rig)
    check_displacement_is_refused(handler, rig)

    print(
        f"one action, one slot: {location_curves} location curves and {pose_curves} pose-bone curves; "
        f"the rig travelled {travelled:.6f} m between frames {START_FRAME} and {END_FRAME}"
    )
    print("ACTION_SLOTS_SMOKE_OK")


if __name__ == "__main__":
    main()
