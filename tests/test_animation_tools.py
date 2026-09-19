# ruff: file-ignore[yoda-conditions]
"""Schema, registration, and forwarding tests for generic animation tools."""

import asyncio

import pytest

from mcp.server.fastmcp.exceptions import ToolError
from pydantic import ValidationError
from test_mutation_transaction import _load_addon

from blender_mcp.server.tools import animation

ANIMATION_COMMANDS = {
    "inspect_animation",
    "manage_animation_action",
    "edit_keyframes",
    "bake_evaluated_animation",
    "manage_nla_tracks",
}


class _Connection:
    def __init__(self) -> None:
        self.calls = []

    def send_command(self, command, params):
        self.calls.append((command, params))
        target = params["target"]
        return {"changed_resources": [target.get("name", target.get("object_name"))]}


def test_animation_tools_are_registered_and_dispatched(monkeypatch) -> None:
    addon, _bpy = _load_addon(monkeypatch, data={})
    server = addon.BlenderMCPServer()

    assert ANIMATION_COMMANDS <= set(animation.mcp._tool_manager._tools)
    assert ANIMATION_COMMANDS <= set(server._build_command_handlers())
    assert "inspect_animation" in server._READ_ONLY_COMMANDS
    assert "edit_keyframes" not in server._READ_ONLY_COMMANDS


def test_keyframe_edit_requires_operation_appropriate_value() -> None:
    with pytest.raises(ValidationError, match="UPSERT requires value"):
        animation.KeyframeEdit(data_path="location", frame=1)
    with pytest.raises(ValidationError, match="REMOVE does not accept value"):
        animation.KeyframeEdit(operation="REMOVE", data_path="location", frame=1, value=2)
    with pytest.raises(ValidationError):
        animation.KeyframeEdit(data_path="location", frame=1, value=float("nan"))


def test_action_tool_validates_conditional_arguments(monkeypatch) -> None:
    connection = _Connection()
    monkeypatch.setattr(animation, "get_blender_connection", lambda: connection)
    target = animation.AnimationTarget(type="OBJECT", name="Cube")

    with pytest.raises(ToolError, match="requires action_name"):
        asyncio.run(animation.manage_animation_action(ctx=None, target=target, action="CREATE"))
    with pytest.raises(ToolError, match="source_action_name"):
        asyncio.run(
            animation.manage_animation_action(
                ctx=None,
                target=target,
                action="DUPLICATE",
                action_name="Copy",
            )
        )
    assert connection.calls == []


def test_edit_keyframes_serializes_batch(monkeypatch) -> None:
    connection = _Connection()
    monkeypatch.setattr(animation, "get_blender_connection", lambda: connection)
    target = animation.AnimationTarget(type="OBJECT", name="Cube")

    result = asyncio.run(
        animation.edit_keyframes(
            ctx=None,
            target=target,
            action_name="Cube Motion",
            edits=[
                animation.KeyframeEdit(data_path="location", frame=1, value=[0, 0, 0]),
                animation.KeyframeEdit(data_path="location", array_index=0, frame=20, value=4),
            ],
        )
    )

    command, params = connection.calls[0]
    assert command == "edit_keyframes"
    assert params["target"] == {"type": "OBJECT", "name": "Cube"}
    assert len(params["edits"]) == 2
    assert result["changed_resources"] == ["Cube"]


def test_bake_evaluated_animation_requires_confirmation_and_serializes(monkeypatch) -> None:
    connection = _Connection()
    monkeypatch.setattr(animation, "get_blender_connection", lambda: connection)
    target = animation.EvaluatedBakeTarget(object_name="Camera", transforms=["LOCATION", "ROTATION"])

    with pytest.raises(ToolError, match="confirm_bake"):
        asyncio.run(
            animation.bake_evaluated_animation(ctx=None, target=target, frame_start=1, frame_end=10, confirm_bake=False)
        )
    asyncio.run(
        animation.bake_evaluated_animation(
            ctx=None,
            target=target,
            frame_start=1,
            frame_end=10,
            action_name="Camera Bake",
            confirm_bake=True,
        )
    )
    assert connection.calls[-1][0] == "bake_evaluated_animation"
    assert connection.calls[-1][1]["target"]["object_name"] == "Camera"


def test_nla_tool_requires_operation_specific_inputs(monkeypatch) -> None:
    connection = _Connection()
    monkeypatch.setattr(animation, "get_blender_connection", lambda: connection)
    target = animation.AnimationTarget(type="OBJECT", name="Cube")

    with pytest.raises(ToolError, match="requires strip_name"):
        asyncio.run(
            animation.manage_nla_tracks(
                ctx=None,
                target=target,
                action="PATCH_STRIP",
                track_name="Motion",
                strip_patch=animation.NlaStripPatch(influence=0.5),
            )
        )
    with pytest.raises(ToolError, match="confirm_remove"):
        asyncio.run(
            animation.manage_nla_tracks(
                ctx=None,
                target=target,
                action="REMOVE_TRACK",
                track_name="Motion",
            )
        )
    assert connection.calls == []


def test_a_driver_expression_may_name_frame_and_its_declared_variables(monkeypatch) -> None:
    """
    Every useful driver expression names something: `frame`, or a variable the call declares.

    `ast.walk` yields each `Name`'s `ctx` node as well as the `Name`, so an allowlist without
    `ast.Load` rejects every expression that is not a bare arithmetic constant.
    """
    addon, _bpy = _load_addon(monkeypatch, data={})
    handlers = addon.handlers.animation

    assert handlers._safe_expression("frame * 0.5", set()) == "frame * 0.5"
    assert handlers._safe_expression("-(offset + frame) / 24", {"offset"}) == "-(offset + frame) / 24"


def test_a_driver_expression_still_refuses_undeclared_names_and_calls(monkeypatch) -> None:
    """The allowlist must keep refusing what it existed to refuse: calls, attributes, other names."""
    addon, _bpy = _load_addon(monkeypatch, data={})
    handlers = addon.handlers.animation

    with pytest.raises(ValueError, match="undeclared variable: speed"):
        handlers._safe_expression("speed * 2", set())
    with pytest.raises(ValueError, match="only arithmetic"):
        handlers._safe_expression("__import__('os').system('ls')", set())
    with pytest.raises(ValueError, match="only arithmetic"):
        handlers._safe_expression("frame.real", set())
    with pytest.raises(ValueError, match="only arithmetic"):
        handlers._safe_expression("abs(frame)", set())


def test_a_scripted_driver_reaches_blender_with_its_frame_expression(monkeypatch) -> None:
    """The server validates the expression before dispatch, so its allowlist gates the whole tool."""
    connection = _Connection()
    monkeypatch.setattr(animation, "get_blender_connection", lambda: connection)
    target = animation.AnimationTarget(type="OBJECT", name="Cube")

    asyncio.run(
        animation.manage_animation_driver(
            ctx=None,
            target=target,
            action="ADD",
            data_path="location",
            array_index=2,
            driver_type="SCRIPTED",
            expression="frame * 0.5",
        )
    )

    assert connection.calls[-1][0] == "manage_animation_driver"
    # `_validate_safe_expression` raises ValueError; FastMCP turns it into the client's tool error.
    with pytest.raises(ValueError, match="undeclared variable: speed"):
        asyncio.run(
            animation.manage_animation_driver(
                ctx=None,
                target=target,
                action="ADD",
                data_path="location",
                array_index=2,
                driver_type="SCRIPTED",
                expression="speed * 2",
            )
        )
