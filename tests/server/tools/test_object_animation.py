# ruff: file-ignore[yoda-conditions]
"""Regression coverage for generic object transform keyframing tools."""

import asyncio

import pytest

from pydantic import ValidationError
from test_mutation_transaction import _TRACKED_COLLECTIONS, FakeCollection, FakeMatrix, _load_addon

from blender_mcp.server.tools import object_animation

OBJECT_ANIMATION_COMMANDS = {"keyframe_object_transform"}


class _RollbackObject:
    """Fake object whose keyframe_insert fails on a later channel in the same record."""

    def __init__(self, name, *, fails_on) -> None:
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
    assert "keyframe_object_transform" not in server._READ_ONLY_COMMANDS


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
        object_animation.ObjectTransformKeyframe(object_name="Cube", frame=1, location=(0, 0, 0), unknown=True)
    with pytest.raises(ValidationError):
        object_animation.ObjectTransformKeyframe(object_name="Cube", frame=2_000_000, location=(0, 0, 0))

    record = object_animation.ObjectTransformKeyframe(
        object_name="Cube", frame=12.0, space="LOCAL", location=(1.0, 2.0, 3.0)
    )
    assert record.frame == pytest.approx(12.0)


def test_keyframe_object_transform_serializes_records(monkeypatch) -> None:
    connection = _Connection()
    monkeypatch.setattr(object_animation, "get_blender_connection", lambda: connection)

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
    obj = _RollbackObject("Cube", fails_on="scale")
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
