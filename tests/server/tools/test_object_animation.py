# ruff: file-ignore[yoda-conditions]
"""Regression coverage for generic object transform keyframing tools."""

import asyncio
import types

import pytest

from pydantic import ValidationError
from test_mutation_transaction import _TRACKED_COLLECTIONS, FakeCollection, FakeMatrix, _load_addon

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
    addon, _bpy = _load_addon(monkeypatch, data={})
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


def test_keyframe_object_transform_rolls_back_partial_channel_failure(monkeypatch) -> None:
    """A record's later channel refusing keyframe_insert must undo that record's earlier inserts."""
    data = {name: FakeCollection() for name in _TRACKED_COLLECTIONS}
    addon, bpy = _load_addon(monkeypatch, data=data)
    obj = _KeyedObject("Cube", fails_on="scale")
    bpy.data.objects["Cube"] = obj

    server = addon.BlenderMCPServer()
    response = server.execute_command_internal(
        {
            "type": "keyframe_object_transform",
            "params": {
                "keyframes": [
                    {
                        "object_name": "Cube",
                        "frame": 5.0,
                        "space": "LOCAL",
                        "location": [1.0, 2.0, 3.0],
                        "scale": [2.0, 2.0, 2.0],
                    }
                ],
                "policy": "REPLACE_EXISTING",
                "interpolation": "BEZIER",
                "handle_left": "AUTO_CLAMPED",
                "handle_right": "AUTO_CLAMPED",
            },
        }
    )

    assert response["status"] == "error"
    assert "Cube:scale" in response["message"]
    assert obj.inserted == [("location", 5.0), ("scale", 5.0)]
    assert obj.deleted == [("location", 5.0)]


def test_keyframe_object_transform_refuses_one_action_for_several_objects(monkeypatch) -> None:
    """An object holds one action, so a batch naming two would leave one of them keyed elsewhere."""
    data = {name: FakeCollection() for name in _TRACKED_COLLECTIONS}
    addon, bpy = _load_addon(monkeypatch, data=data)
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


class _FakeKey:
    """One keyframe point; the cycle scan reads the frame it sits on, nothing else."""

    def __init__(self, frame) -> None:
        self.co = (float(frame), 0.0)


class _CycledCurve:
    """An F-Curve already carrying a Cycles modifier over the frames it was keyed across."""

    def __init__(self, data_path, frames, *, cyclic=True) -> None:
        self.data_path = data_path
        self.array_index = 1
        self.keyframe_points = [_FakeKey(frame) for frame in frames]
        self.modifiers = [types.SimpleNamespace(type="CYCLES")] if cyclic else []


def _cycling_rig(monkeypatch, curves):
    """
    Build an object whose assigned action holds `curves`, and the server to key it through.

    Args:
        monkeypatch: pytest's monkeypatch fixture.
        curves: The F-Curves the object's action carries before this call keys anything.

    Returns:
        tuple: The server and the keyed object.

    """
    data = {name: FakeCollection() for name in _TRACKED_COLLECTIONS}
    addon, bpy = _load_addon(monkeypatch, data=data)
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
