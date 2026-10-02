# ruff: file-ignore[yoda-conditions]
"""Regression coverage for generic object transform keyframing tools."""

import asyncio
import types

import pytest

from conftest import load_addon
from datablock_doubles import (
    NO_RNA_SETTINGS,
    TRACKED_COLLECTIONS,
    FakeCollection,
    FakeDatablock,
    FakeFCurveFields,
    FakeKeyframeFields,
    FakeMatrix,
)
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import ValidationError

from blender_mcp.server.tools import _dispatch, object_animation

OBJECT_ANIMATION_COMMANDS = {"keyframe_object_transform"}


class _KeyedObject:
    """Fake object recording every key inserted on it; `fails_on` makes one channel refuse insertion."""

    def __init__(self, name, *, fails_on=None) -> None:
        self.name = name
        self.data = None
        self.matrix_basis = FakeMatrix()
        self.parent = None
        self.matrix_parent_inverse = FakeMatrix()
        self.material_slots = []
        self.modifiers = []
        self.rotation_mode = "XYZ"
        self.location = (0.0, 0.0, 0.0)
        self.rotation_euler = (0.0, 0.0, 0.0)
        self.scale = (1.0, 1.0, 1.0)
        self.animation_data = None
        self._fails_on = fails_on
        self.inserted = []
        self.deleted = []

    def keyframe_insert(self, data_path, frame):
        self.inserted.append((data_path, frame))
        return data_path != self._fails_on

    def keyframe_delete(self, data_path, frame):
        self.deleted.append((data_path, frame))
        return True


class _Connection:
    def __init__(self) -> None:
        self.calls = []

    def send_command(self, command, params):
        self.calls.append((command, params))
        return {"changed_resources": [record["object_name"] for record in params["keyframes"]]}


def test_object_animation_tools_are_registered_and_dispatched(monkeypatch) -> None:
    addon, _bpy = load_addon(monkeypatch, data={})
    server = addon.BlenderMCPServer()

    assert OBJECT_ANIMATION_COMMANDS <= set(object_animation.mcp._tool_manager._tools)
    assert OBJECT_ANIMATION_COMMANDS <= set(server._build_command_handlers())
    assert not server.command_spec("keyframe_object_transform").read_only


def test_object_transform_keyframe_is_strict_and_bounded() -> None:
    with pytest.raises(ValidationError, match="exactly one of frame or at_seconds"):
        object_animation.ObjectTransformKeyframe(object_name="Cube", frame=1, at_seconds=1.0, location=(0, 0, 0))
    with pytest.raises(ValidationError, match="exactly one of frame or at_seconds"):
        object_animation.ObjectTransformKeyframe(object_name="Cube", location=(0, 0, 0))
    with pytest.raises(ValidationError, match="not both"):
        object_animation.ObjectTransformKeyframe(
            object_name="Cube", frame=1, rotation_euler=(0, 0, 0), rotation_quaternion=(1, 0, 0, 0)
        )
    with pytest.raises(ValidationError, match="at least one"):
        object_animation.ObjectTransformKeyframe(object_name="Cube", frame=1)
    with pytest.raises(ValidationError):
        object_animation.ObjectTransformKeyframe(object_name="Cube", frame=1, location=(0, 0, 0), unknown=True)  # pyright: ignore[reportCallIssue]
    with pytest.raises(ValidationError):
        object_animation.ObjectTransformKeyframe(object_name="Cube", frame=2_000_000, location=(0, 0, 0))

    record = object_animation.ObjectTransformKeyframe(
        object_name="Cube", frame=12.0, space="LOCAL", location=(1.0, 2.0, 3.0)
    )
    assert record.frame == pytest.approx(12.0)


def test_keyframe_object_transform_serializes_records(monkeypatch) -> None:
    connection = _Connection()
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    result = asyncio.run(
        object_animation.keyframe_object_transform(
            ctx=None,
            keyframes=[
                object_animation.ObjectTransformKeyframe(
                    object_name="Cube",
                    frame=1.0,
                    space="WORLD",
                    location=(1.0, 2.0, 3.0),
                    rotation_euler=(0.0, 0.0, 0.0),
                ),
                object_animation.ObjectTransformKeyframe(object_name="Empty", at_seconds=2.0, scale=(1.5, 1.5, 1.5)),
            ],
            policy="INSERT_ONLY",
        )
    )

    command, params = connection.calls[0]
    assert command == "keyframe_object_transform"
    assert params["policy"] == "INSERT_ONLY"
    assert params["keyframes"] == [
        {
            "object_name": "Cube",
            "frame": 1.0,
            "space": "WORLD",
            "location": (1.0, 2.0, 3.0),
            "rotation_euler": (0.0, 0.0, 0.0),
        },
        {"object_name": "Empty", "at_seconds": 2.0, "space": "WORLD", "scale": (1.5, 1.5, 1.5)},
    ]
    assert result["changed_resources"] == ["Cube", "Empty"]


class _Points(list):
    """`fcurve.keyframe_points`: the two mutators the failure path rebuilds a curve with."""

    def add(self, count) -> None:
        self.extend(_FakeKey(0.0) for _ in range(count))


class _Curve(FakeFCurveFields):
    def __init__(self, data_path, array_index, keys=()) -> None:
        self.data_path = data_path
        self.array_index = array_index
        self.keyframe_points = _Points(_FakeKey(frame, value) for frame, value in keys)
        self.modifiers = []

    def keys(self):
        return [tuple(point.co) for point in self.keyframe_points]


class _Curves(list):
    def new(self, data_path, index=0, group_name=""):
        curve = _Curve(data_path, index)
        self.append(curve)
        return curve


class _WritingObject(_KeyedObject):
    """An object whose keys land in its assigned action's curves, as Blender's do."""

    def __init__(self, name, *, fails_on=None, fails_at=None) -> None:
        super().__init__(name, fails_on=fails_on)
        self._fails_at = fails_at

    def animation_data_create(self):
        if self.animation_data is None:
            self.animation_data = types.SimpleNamespace(action=None, action_slot=None, action_suitable_slots=())
        return self.animation_data

    def keyframe_insert(self, data_path, frame):
        self.inserted.append((data_path, frame))
        if data_path == self._fails_on and frame == self._fails_at:
            return False
        curves = self.animation_data.action.fcurves
        for index, value in enumerate(getattr(self, data_path)):
            curve = next((c for c in curves if (c.data_path, c.array_index) == (data_path, index)), None)
            if curve is None:
                curve = curves.new(data_path, index=index)
            existing = next((point for point in curve.keyframe_points if point.co[0] == frame), None)
            if existing is None:
                curve.keyframe_points.append(_FakeKey(frame, value))
            else:
                existing.co = (frame, value)
        return True


def test_a_batch_failing_part_way_hands_a_reused_action_back_its_keys(monkeypatch) -> None:
    """
    The action outlives the call, so restoring the assignment alone is not a rollback.

    A REUSE action is the shot's animation - other objects and NLA strips may play it - and a
    batch refused at its third record had already overwritten one existing key, inserted
    another and created a whole rotation channel in it, behind an error saying nothing changed.
    """
    data = {name: FakeCollection() for name in TRACKED_COLLECTIONS}
    addon, bpy = load_addon(monkeypatch, data=data)
    root = FakeDatablock("Hero_root")
    original = {index: [(1.0, 0.0), (24.0, 5.0 + index)] for index in range(3)}
    root.fcurves = _Curves(_Curve("location", index, keys) for index, keys in original.items())
    bpy.data.actions["Hero_root"] = root
    hero = _WritingObject("Hero", fails_on="scale", fails_at=30.0)
    hero.animation_data_create().action = root
    bpy.data.objects["Hero"] = hero

    response = addon.BlenderMCPServer().execute_command_internal(
        {
            "type": "keyframe_object_transform",
            "params": {
                "keyframes": [
                    {"object_name": "Hero", "frame": 12.0, "space": "LOCAL", "location": [1.0, 1.0, 1.0]},
                    {
                        "object_name": "Hero",
                        "frame": 24.0,
                        "space": "LOCAL",
                        "location": [9.0, 9.0, 9.0],
                        "rotation_euler": [0.5, 0.0, 0.0],
                    },
                    {"object_name": "Hero", "frame": 30.0, "space": "LOCAL", "scale": [2.0, 2.0, 2.0]},
                ],
                "action_name": "Hero_root",
                "action_policy": "REUSE",
            },
        }
    )

    assert response["status"] == "error"
    assert "Hero:scale" in response["message"]
    # The two records before the refusal really did write into the action...
    assert ("rotation_euler", 24.0) in hero.inserted
    # ...and the action holds exactly the keys it arrived with, and no curve it did not.
    assert [(curve.data_path, curve.array_index) for curve in root.fcurves] == [("location", i) for i in range(3)]
    assert {curve.array_index: curve.keys() for curve in root.fcurves} == original
    assert hero.animation_data.action is root


def test_keyframe_object_transform_refuses_one_action_for_several_objects(monkeypatch) -> None:
    """An object holds one action, so a batch naming two would leave one of them keyed elsewhere."""
    data = {name: FakeCollection() for name in TRACKED_COLLECTIONS}
    addon, bpy = load_addon(monkeypatch, data=data)
    hero = _KeyedObject("Hero")
    prop = _KeyedObject("Prop")
    bpy.data.objects["Hero"] = hero
    bpy.data.objects["Prop"] = prop

    server = addon.BlenderMCPServer()
    response = server.execute_command_internal(
        {
            "type": "keyframe_object_transform",
            "params": {
                "keyframes": [
                    {"object_name": "Hero", "frame": 1.0, "space": "LOCAL", "location": [0.0, 0.0, 0.0]},
                    {"object_name": "Prop", "frame": 1.0, "space": "LOCAL", "location": [1.0, 0.0, 0.0]},
                ],
                "action_name": "SH030_motion",
            },
        }
    )

    assert response["status"] == "error"
    assert "once per object" in response["message"]
    # Refused before anything was keyed, and without leaving the action it would have made.
    assert hero.inserted == []
    assert prop.inserted == []
    assert bpy.data.actions.get("SH030_motion") is None


class _FakeKey(FakeKeyframeFields):
    """One keyframe point at a frame, holding a value."""

    def __init__(self, frame, value=0.0) -> None:
        self.co = (float(frame), float(value))


class _CycledCurve(FakeFCurveFields):
    """An F-Curve already carrying a Cycles modifier over the frames it was keyed across."""

    def __init__(self, data_path, frames, *, cyclic=True) -> None:
        self.data_path = data_path
        self.array_index = 1
        self.keyframe_points = [_FakeKey(frame) for frame in frames]
        self.modifiers = [types.SimpleNamespace(type="CYCLES", bl_rna=NO_RNA_SETTINGS)] if cyclic else []


def _cycling_rig(monkeypatch, curves):
    """
    Build an object whose assigned action holds `curves`, and the server to key it through.

    Args:
        monkeypatch: pytest's monkeypatch fixture.
        curves: The F-Curves the object's action carries before this call keys anything.

    Returns:
        tuple: The server and the keyed object.

    """
    data = {name: FakeCollection() for name in TRACKED_COLLECTIONS}
    addon, bpy = load_addon(monkeypatch, data=data)
    rig = _KeyedObject("CHAR1_rig")
    action = types.SimpleNamespace(name="CHAR1_sh030_performance", fcurves=list(curves))
    rig.animation_data = types.SimpleNamespace(
        action=action, action_slot=types.SimpleNamespace(identifier="OBCHAR1_rig", handle=3)
    )
    bpy.data.objects["CHAR1_rig"] = rig
    return addon.BlenderMCPServer(), rig


def _key_at(server, frame, channel="location", value=(0.0, 2.1, 0.0)):
    return server.execute_command_internal(
        {
            "type": "keyframe_object_transform",
            "params": {
                "keyframes": [{"object_name": "CHAR1_rig", "frame": frame, "space": "LOCAL", channel: list(value)}]
            },
        }
    )


def test_a_key_outside_a_travelling_cycle_reports_the_period_it_redefines(monkeypatch) -> None:
    """
    `keyframe_character_pose` has warned about this since the walk whose arms drifted.

    The object path carried the same trap silently, and it is the worse one: the channel it
    redefines is the root's own `location`, so a key landing outside the stride's extent moves
    the whole character rather than one limb. A rehearsal stretched a 16-frame travelling cycle
    to 199 frames this way and only saw it in a render.
    """
    server, rig = _cycling_rig(monkeypatch, [_CycledCurve("location", (1.0, 17.0))])

    response = _key_at(server, 199.0)

    assert response["status"] == "success", response
    warning = next(text for text in response["result"]["warnings"] if "cycles over" in text)
    assert "'CHAR1_rig'.location is keyed at frame 199" in warning
    assert "frames 1-17" in warning
    assert "period becomes 198 frames instead of 16" in warning
    # It warns and keys: extending a cycle deliberately is legitimate authoring.
    assert rig.inserted == [("location", 199.0)]


def test_a_key_inside_the_cycle_or_on_an_uncycled_channel_stays_quiet(monkeypatch) -> None:
    """The notice has to be rare enough to read: neither of these redefines anything."""
    server, _rig = _cycling_rig(
        monkeypatch, [_CycledCurve("location", (1.0, 17.0)), _CycledCurve("scale", (1.0, 17.0), cyclic=False)]
    )

    inside = _key_at(server, 9.0)
    uncycled = _key_at(server, 199.0, channel="scale", value=(1.0, 1.0, 1.0))

    assert inside["result"]["warnings"] == []
    assert uncycled["result"]["warnings"] == []


class _BorrowedObject(_KeyedObject):
    """A keyed object whose animation data comes and goes as Blender's does, recording the action each key hit."""

    def __init__(self, name) -> None:
        super().__init__(name)
        self.keyed_into = []

    def animation_data_create(self):
        if self.animation_data is None:
            self.animation_data = types.SimpleNamespace(action=None, action_slot=None, action_suitable_slots=())
        return self.animation_data

    def animation_data_clear(self) -> None:
        self.animation_data = None

    def keyframe_insert(self, data_path, frame):
        self.keyed_into.append(self.animation_data.action.name)
        return super().keyframe_insert(data_path, frame)


def _key_clip(server, location):
    return server.keyframe_object_transform(
        keyframes=[{"object_name": "Hero", "frame": 12.0, "space": "LOCAL", "location": list(location)}],
        action_name="Hero_wave",
        assign_action=False,
    )


def test_an_unassigned_clip_hands_the_object_back_its_action_and_transform(monkeypatch) -> None:
    """The clip's values drive nothing once the object's own action is back, so neither may stick."""
    addon, bpy = load_addon(monkeypatch, data={name: FakeCollection() for name in TRACKED_COLLECTIONS})
    hero = _BorrowedObject("Hero")
    hero.location = (1.0, 2.0, 3.0)
    root_motion = types.SimpleNamespace(name="Hero_root", fcurves=[_CycledCurve("location", (1.0, 24.0))])
    root_slot = types.SimpleNamespace(identifier="OBHero", handle=1)
    hero.animation_data_create()
    hero.animation_data.action, hero.animation_data.action_slot = root_motion, root_slot
    bpy.data.objects["Hero"] = hero

    reply = _key_clip(addon.BlenderMCPServer(), (9.0, 9.0, 9.0))

    # Keyed into the clip, without the displacement guard refusing the keys root motion holds.
    assert hero.keyed_into == ["Hero_wave"]
    assert hero.animation_data.action is root_motion
    assert hero.animation_data.action_slot is root_slot
    assert hero.location == (1.0, 2.0, 3.0)
    assert (reply["actions"], reply["assigned_action"]) == (["Hero_wave"], "Hero_root")


def test_an_unassigned_clip_leaves_an_object_with_no_animation_data_without_any(monkeypatch) -> None:
    addon, bpy = load_addon(monkeypatch, data={name: FakeCollection() for name in TRACKED_COLLECTIONS})
    hero = _BorrowedObject("Hero")
    bpy.data.objects["Hero"] = hero

    reply = _key_clip(addon.BlenderMCPServer(), (9.0, 9.0, 9.0))

    assert hero.keyed_into == ["Hero_wave"]
    assert hero.animation_data is None
    assert reply["assigned_action"] is None


def test_an_unassigned_clip_without_a_name_is_refused_before_the_socket(monkeypatch) -> None:
    """There is no clip to key when no action is named: the keys would land in the active action."""
    connection = _Connection()
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)
    record = object_animation.ObjectTransformKeyframe(object_name="Hero", frame=1.0, location=(0.0, 0.0, 0.0))

    with pytest.raises(ToolError, match="assign_action=False requires action_name"):
        asyncio.run(object_animation.keyframe_object_transform(ctx=None, keyframes=[record], assign_action=False))

    assert connection.calls == []
