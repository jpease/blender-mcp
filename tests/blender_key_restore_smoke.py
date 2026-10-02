# ruff: file-ignore[module-import-not-at-top-of-file]
"""
Run with Blender 5.1+ to prove a keying call that fails part way hands a reused action its keys back.

Restoring the ID's assignment on error is not a rollback once the action outlives the call: a
REUSE action is the shot's animation, often shared between IDs, and a batch that failed at
its last entry used to leave every earlier entry's keys written into it. Each keyer is driven
here through the socket dispatch into an action that already holds authored keys - a FREE
handle, a LINEAR key, a cycled curve with extrapolation, a second ID's slot - and made to fail
after it has inserted, overwritten and deleted keys; the whole action must read back exactly as
it did before the call. The failure is forced by making a step the handler takes *after* its
first writes raise, which is the shape every real mid-batch failure has.
"""

import contextlib
import sys

from pathlib import Path

import bpy

sys.path.append(str(Path(__file__).resolve().parent))
from smoke_addon import load_addon

addon = load_addon("blender_mcp_key_restore_smoke")

from blender_mcp_key_restore_smoke.handlers import object_animation
from blender_mcp_key_restore_smoke.handlers.character_rigging import posing, reach
from blender_mcp_key_restore_smoke.server_core import BlenderMCPServer

SHARED_ACTION = "Shared_root"
POSE_ACTION = "Rig_performance"


def _run(server: BlenderMCPServer, command: str, **params: object) -> dict:
    """
    Dispatch one command the way the socket server does, and require success.

    Args:
        server: The server under test.
        command: The MCP command type.
        **params: The command's parameters.

    Returns:
        dict: The command's result.

    """
    response = server.execute_command_internal({"type": command, "params": params})
    assert response["status"] == "success", (command, response)
    return response["result"]


def _refused(server: BlenderMCPServer, command: str, **params: object) -> str:
    """
    Dispatch one command that must fail, and return its error message.

    Args:
        server: The server under test.
        command: The MCP command type.
        **params: The command's parameters.

    Returns:
        str: The error message.

    """
    response = server.execute_command_internal({"type": command, "params": params})
    assert response["status"] == "error", (command, response)
    return response["message"]


@contextlib.contextmanager
def _failing_on_call(module, name: str, call: int):
    """
    Make `module.name` raise on its `call`-th invocation, after the earlier ones ran for real.

    Args:
        module: The handler module whose global the keying loop looks up.
        name: The function to wrap.
        call: Which invocation raises, counting from 1.

    """
    original = getattr(module, name)
    calls = []

    def wrapped(*args, **kwargs):
        calls.append(None)
        if len(calls) == call:
            raise RuntimeError(f"forced failure in {name} call {call}")
        return original(*args, **kwargs)

    setattr(module, name, wrapped)
    try:
        yield calls
    finally:
        setattr(module, name, original)


def _action_state(action: bpy.types.Action) -> dict:
    """
    Read everything about an action's keys a restore must put back, slot by slot.

    Args:
        action: The action to read.

    Returns:
        dict: `(slot, data_path, array_index)` to the curve's settings, modifiers and points.

    """
    state = {}
    for layer in action.layers:
        for strip in layer.strips:
            for bag in strip.channelbags:
                for curve in bag.fcurves:
                    state[bag.slot.identifier, curve.data_path, curve.array_index] = (
                        curve.extrapolation,
                        curve.group.name if curve.group else None,
                        tuple(
                            (modifier.type, modifier.mode_before, modifier.mode_after) for modifier in curve.modifiers
                        ),
                        tuple(
                            (
                                tuple(point.co),
                                tuple(point.handle_left),
                                tuple(point.handle_right),
                                point.handle_left_type,
                                point.handle_right_type,
                                point.interpolation,
                                point.easing,
                                point.type,
                                point.select_control_point,
                            )
                            for point in curve.keyframe_points
                        ),
                    )
    return state


def _author(curve: bpy.types.FCurve) -> None:
    """Give a curve the hand edits an artist leaves: a FREE handle, a LINEAR key, a cycle."""
    points = curve.keyframe_points
    points[0].interpolation = "LINEAR"
    points[-1].handle_left_type = "FREE"
    points[-1].handle_left = (points[-1].co[0] - 3.0, points[-1].co[1] + 2.0)
    curve.extrapolation = "LINEAR"
    curve.modifiers.new("CYCLES").mode_after = "REPEAT_OFFSET"


def _slot_curves(rig: bpy.types.Object, data_path: str) -> list[bpy.types.FCurve]:
    animation_data = rig.animation_data
    bag = animation_data.action.layers[0].strips[0].channelbag(animation_data.action_slot)
    return [curve for curve in bag.fcurves if curve.data_path == data_path]


def _check_object_keyer(server: BlenderMCPServer) -> None:
    """Fail a batch at its third record, and require the shared action back exactly as both objects keyed it."""
    hero = bpy.data.objects.new("Hero", None)
    prop = bpy.data.objects.new("Prop", None)
    for obj in (hero, prop):
        bpy.context.scene.collection.objects.link(obj)
    _run(
        server,
        "keyframe_object_transform",
        keyframes=[
            {"object_name": "Hero", "frame": 1.0, "space": "LOCAL", "location": [0.0, 0.0, 0.0]},
            {"object_name": "Hero", "frame": 24.0, "space": "LOCAL", "location": [4.0, 1.0, 0.0]},
        ],
        action_name=SHARED_ACTION,
        action_policy="CREATE",
    )
    # A second ID's slot in the same action: the keys a failed Hero batch must never touch.
    bpy.data.actions[SHARED_ACTION].slots.new(id_type="OBJECT", name="Prop")
    _run(
        server,
        "keyframe_object_transform",
        keyframes=[{"object_name": "Prop", "frame": 6.0, "space": "LOCAL", "location": [0.0, 3.0, 0.0]}],
        action_name=SHARED_ACTION,
        action_policy="REUSE",
        action_slot_identifier="OBProp",
    )
    for curve in _slot_curves(hero, "location"):
        _author(curve)
    action = bpy.data.actions[SHARED_ACTION]
    slot = hero.animation_data.action_slot
    before = _action_state(action)
    assert len({key[0] for key in before}) == 2, before

    # Records 1 and 2 insert a key, overwrite the frame-24 key and create rotation curves; the
    # fourth key styled - record 3's scale, already inserted - raises.
    with _failing_on_call(object_animation, "_style_inserted_keys", 4):
        message = _refused(
            server,
            "keyframe_object_transform",
            keyframes=[
                {"object_name": "Hero", "frame": 12.0, "space": "LOCAL", "location": [2.0, 2.0, 2.0]},
                {
                    "object_name": "Hero",
                    "frame": 24.0,
                    "space": "LOCAL",
                    "location": [9.0, 9.0, 9.0],
                    "rotation_euler": [0.5, 0.0, 0.0],
                },
                {"object_name": "Hero", "frame": 30.0, "space": "LOCAL", "scale": [2.0, 2.0, 2.0]},
            ],
            action_name=SHARED_ACTION,
            action_policy="REUSE",
            action_slot_identifier="OBHero",
        )
    assert "forced failure" in message, message
    assert _action_state(action) == before, "keyframe_object_transform left keys in the reused action"
    assert hero.animation_data.action == action and hero.animation_data.action_slot == slot


def _build_rig() -> bpy.types.Object:
    data = bpy.data.armatures.new("RigData")
    rig = bpy.data.objects.new("Rig", data)
    bpy.context.scene.collection.objects.link(rig)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode="EDIT")
    spine = data.edit_bones.new("spine")
    spine.head, spine.tail = (0, 0, 0), (0, 0, 0.4)
    head = data.edit_bones.new("head")
    head.head, head.tail = (0, 0, 0.4), (0, 0, 0.6)
    head.parent, head.use_connect = spine, True
    bpy.ops.object.mode_set(mode="OBJECT")
    return rig


def _turn(bone: str, degrees: float) -> list[dict]:
    return [{"bone_name": bone, "rotate": {"axis": "X", "degrees": degrees}}]


def _check_pose_keyer(server: BlenderMCPServer, rig: bpy.types.Object) -> None:
    """REPLACE deletes a lone cycled key, Blender deletes its curve, and the failure rebuilds it."""
    _run(
        server,
        "keyframe_character_pose",
        armature_object_name=rig.name,
        action_name=POSE_ACTION,
        keys=[
            {"frame": 1.0, "poses": _turn("head", 0.0) + _turn("spine", 10.0)},
            {"frame": 24.0, "poses": _turn("head", 30.0)},
        ],
        action_policy="CREATE",
    )
    for curve in _slot_curves(rig, 'pose.bones["head"].rotation_quaternion'):
        _author(curve)
    for curve in _slot_curves(rig, 'pose.bones["spine"].rotation_quaternion'):
        curve.extrapolation = "LINEAR"
        curve.modifiers.new("CYCLES")
    action = bpy.data.actions[POSE_ACTION]
    before = _action_state(action)

    with _failing_on_call(posing, "_style_written_keys", 2):
        message = _refused(
            server,
            "keyframe_character_pose",
            armature_object_name=rig.name,
            action_name=POSE_ACTION,
            keys=[
                {"frame": 1.0, "poses": _turn("spine", -20.0)},
                {"frame": 12.0, "poses": _turn("head", 15.0)},
            ],
            keying_policy="REPLACE",
            action_policy="REUSE",
        )
    assert "forced failure" in message, message
    assert _action_state(action) == before, "keyframe_character_pose left keys in the reused action"


def _check_reach_keyer(server: BlenderMCPServer, rig: bpy.types.Object) -> None:
    """Fail a two-frame reach on its second frame, and require the first frame's keys gone again."""
    action = bpy.data.actions[POSE_ACTION]
    before = _action_state(action)
    with _failing_on_call(reach, "_style_written_keys", 2):
        message = _refused(
            server,
            "keyframe_bone_reach",
            armature_object_name=rig.name,
            action_name=POSE_ACTION,
            reaches=[
                {
                    "tip_bone": "head",
                    "chain_length": 2,
                    "pole_target_point": [0.0, 1.0, 0.3],
                    "keys": [
                        {"frame": 1.0, "target_point": [0.0, 0.2, 0.5]},
                        {"frame": 24.0, "target_point": [0.0, -0.2, 0.5]},
                    ],
                }
            ],
            action_policy="REUSE",
        )
    assert "forced failure" in message, message
    assert _action_state(action) == before, "keyframe_bone_reach left keys in the reused action"


def _check_edit_keyframes(server: BlenderMCPServer, rig: bpy.types.Object) -> None:
    """Fail an edit batch on a group name Blender refuses, and require the earlier edits undone."""
    action = bpy.data.actions[POSE_ACTION]
    before = _action_state(action)
    message = _refused(
        server,
        "edit_keyframes",
        target={"type": "OBJECT", "name": rig.name},
        edits=[
            {"data_path": 'pose.bones["head"].rotation_quaternion', "array_index": 1, "frame": 24, "value": 0.9},
            {"operation": "REMOVE", "data_path": 'pose.bones["spine"].rotation_quaternion', "frame": 1},
            {"data_path": 'pose.bones["head"].location', "array_index": 0, "frame": 5, "value": 1.0, "group": 5},
        ],
        action_name=POSE_ACTION,
    )
    assert "group_name" in message or "str" in message, message
    assert _action_state(action) == before, "edit_keyframes left keys in the action"


def main() -> None:
    """Drive every keyer into a pre-keyed action and make it fail after its first writes."""
    bpy.ops.wm.read_factory_settings(use_empty=True)
    server = BlenderMCPServer()
    _check_object_keyer(server)
    rig = _build_rig()
    _check_pose_keyer(server, rig)
    _check_reach_keyer(server, rig)
    _check_edit_keyframes(server, rig)
    print("KEY_RESTORE_SMOKE_OK")


if __name__ == "__main__":
    main()
