# ruff: file-ignore[yoda-conditions]
"""Regression coverage for scene-wide unit/gravity/sync-mode tools."""

import asyncio

import pytest

from pydantic import ValidationError
from test_mutation_transaction import _load_addon

from blender_mcp.server.tools import _dispatch, scene_physics

SCENE_PHYSICS_COMMANDS = {
    "get_scene_physics_info",
    "configure_scene_physics",
}


class _Connection:
    def __init__(self) -> None:
        self.calls = []

    def send_command(self, command, params):
        self.calls.append((command, params))
        return {"changed_resources": [params.get("scene_name", "Scene")]}


def test_scene_physics_tools_are_registered_and_dispatched(monkeypatch) -> None:
    addon, _bpy = _load_addon(monkeypatch, data={})
    server = addon.BlenderMCPServer()

    assert SCENE_PHYSICS_COMMANDS <= set(scene_physics.mcp._tool_manager._tools)
    assert SCENE_PHYSICS_COMMANDS <= set(server._build_command_handlers())
    assert server.command_spec("get_scene_physics_info").read_only
    assert not server.command_spec("configure_scene_physics").read_only


def test_scene_physics_patch_is_strict_and_bounded() -> None:
    with pytest.raises(ValidationError, match="at least one field"):
        scene_physics.ScenePhysicsPatch()
    with pytest.raises(ValidationError):
        scene_physics.ScenePhysicsPatch(scale_length=0.0)
    with pytest.raises(ValidationError):
        scene_physics.ScenePhysicsPatch(scale_length=1000.0)
    with pytest.raises(ValidationError):
        scene_physics.ScenePhysicsPatch(system="METERS")  # pyright: ignore[reportArgumentType] - rejection is the assertion
    with pytest.raises(ValidationError):
        scene_physics.ScenePhysicsPatch(sync_mode="PLAY_EVERY_FRAME")  # pyright: ignore[reportArgumentType] - rejection is the assertion
    with pytest.raises(ValidationError):
        scene_physics.ScenePhysicsPatch(unknown=True)  # pyright: ignore[reportCallIssue] - rejection is the assertion

    patch = scene_physics.ScenePhysicsPatch(system="METRIC", scale_length=0.5, sync_mode="NONE")
    assert patch.scale_length == pytest.approx(0.5)


def test_configure_scene_physics_serializes_patch(monkeypatch) -> None:
    connection = _Connection()
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    result = asyncio.run(
        scene_physics.configure_scene_physics(
            ctx=None,
            scene_name="Scene",
            patch=scene_physics.ScenePhysicsPatch(
                system="METRIC",
                gravity=(0.0, 0.0, -1.62),
                use_gravity=True,
            ),
        )
    )

    command, params = connection.calls[0]
    assert command == "configure_scene_physics"
    assert params["scene_name"] == "Scene"
    assert params["patch"] == {
        "system": "METRIC",
        "gravity": (0.0, 0.0, -1.62),
        "use_gravity": True,
    }
    assert result["changed_resources"] == ["Scene"]


def test_get_scene_physics_info_forwards_convert_seconds(monkeypatch) -> None:
    connection = _Connection()
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    asyncio.run(scene_physics.get_scene_physics_info(ctx=None, scene_name="Scene", convert_seconds=[0.0, 5.0]))

    command, params = connection.calls[0]
    assert command == "get_scene_physics_info"
    assert params == {"scene_name": "Scene", "convert_seconds": [0.0, 5.0]}
