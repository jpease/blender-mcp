"""Regression coverage for rigid-body debris, proxy, character, export, and analysis tools."""

import asyncio
import sys

import pytest

from conftest import load_addon
from pydantic import ValidationError

from blender_mcp.server.tools import _dispatch, rigid_body

EXTENDED_COMMANDS = {
    "create_rigid_body_debris_field",
    "create_rigid_body_proxy_rig",
    "create_ragdoll_rig",
    "bake_ragdoll_to_armature",
    "export_rigid_body_animation",
    "analyze_rigid_body_performance",
}


def _run(function, **kwargs):
    return asyncio.run(function(ctx=None, **kwargs))


def test_extended_commands_are_registered_and_dispatched(monkeypatch) -> None:
    assert all(callable(getattr(rigid_body, name)) for name in EXTENDED_COMMANDS)
    assert set(rigid_body.mcp._tool_manager._tools) >= EXTENDED_COMMANDS

    addon, _bpy = load_addon(monkeypatch, data={})
    server = addon.BlenderMCPServer()
    assert set(server._build_command_handlers()) >= EXTENDED_COMMANDS


def test_debris_region_and_transform_ranges_are_strict() -> None:
    with pytest.raises(ValidationError, match="minimum and maximum"):
        rigid_body.DebrisRegion(shape="BOX")
    with pytest.raises(ValidationError, match="less than maximum"):
        rigid_body.DebrisRegion(shape="BOX", minimum=(1, 0, 0), maximum=(0, 1, 1))
    with pytest.raises(ValidationError, match="uniform_scale_min"):
        rigid_body.DebrisTransformRange(uniform_scale_min=2.0, uniform_scale_max=1.0)


def test_proxy_and_ragdoll_specs_reject_ambiguous_mappings() -> None:
    with pytest.raises(ValidationError, match="required only"):
        rigid_body.RigidBodyProxyMapping(
            render_object_name="Hero",
            approximation="BOX",
            low_resolution_source_name="Low",
        )
    with pytest.raises(ValidationError, match="required only"):
        rigid_body.RagdollBodySpec(bone_name="upper_arm.L", shape="CONVEX_HULL")
    with pytest.raises(ValidationError, match="distinct"):
        rigid_body.RagdollJointSpec(
            parent_bone_name="spine",
            child_bone_name="spine",
            configuration={"type": "POINT"},
        )


def test_debris_payload_preserves_seed_and_explicit_sources(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        _dispatch,
        "send_command",
        lambda command, params=None: calls.append((command, params)) or {"ok": True},
    )
    result = _run(
        rigid_body.create_rigid_body_debris_field,
        scene_name="Scene",
        field_name="Impact",
        sources=[rigid_body.DebrisSourceSpec(object_name="Shard", weight=2.0)],
        count=12,
        seed=42,
        region=rigid_body.DebrisRegion(shape="SPHERE", center=(0, 0, 0), radius=2.0),
        density=400.0,
    )

    command, params = calls[0]
    assert command == "create_rigid_body_debris_field"
    assert params["seed"] == 42
    assert params["count"] == 12
    assert result["changed_objects"] == ["Shard"]


def test_ragdoll_payload_keeps_reviewed_constraint_limits(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        _dispatch,
        "send_command",
        lambda command, params=None: calls.append((command, params)) or {"ok": True},
    )
    joint = rigid_body.RagdollJointSpec(
        parent_bone_name="spine",
        child_bone_name="head",
        configuration={
            "type": "HINGE",
            "angular_z": {"use_limit": True, "lower": -0.5, "upper": 0.5},
        },
    )
    result = _run(
        rigid_body.create_ragdoll_rig,
        scene_name="Scene",
        armature_object_name="Rig",
        rig_name="Hero Ragdoll",
        bodies=[
            rigid_body.RagdollBodySpec(bone_name="spine"),
            rigid_body.RagdollBodySpec(bone_name="head"),
        ],
        joints=[joint],
        total_mass=75.0,
    )

    command, params = calls[0]
    assert command == "create_ragdoll_rig"
    assert params["joints"][0]["configuration"]["angular_z"]["lower"] == pytest.approx(-0.5)
    assert result["changed_objects"] == ["Rig"]


def test_export_coordinate_contract_is_explicit() -> None:
    with pytest.raises(Exception, match="Y_UP_RIGHT_HANDED"):
        _run(
            rigid_body.export_rigid_body_animation,
            scene_name="Scene",
            object_names=["Baked"],
            filepath="/tmp/baked.glb",
            format="GLTF",
            frame_start=1,
            frame_end=10,
        )


def test_structural_performance_analysis_uses_read_only_dispatch(monkeypatch) -> None:
    addon, _bpy = load_addon(monkeypatch, data={})
    server = addon.BlenderMCPServer()
    monkeypatch.setattr(server, "analyze_rigid_body_performance", lambda **_kwargs: {"findings": []})

    result = server.execute_command_internal(
        {
            "type": "analyze_rigid_body_performance",
            "params": {"scene_name": "Scene", "object_names": ["Body"], "sample_frames": []},
        }
    )

    assert result["result"]["findings"] == []


def test_new_implementation_names_are_purpose_based(monkeypatch) -> None:
    module_names = (
        "debris",
        "proxy_rigs",
        "ragdolls",
        "exporting",
        "performance",
    )
    assert all("phase" not in name for name in module_names)
    addon, _bpy = load_addon(monkeypatch, data={})
    handler = sys.modules[f"{addon.__name__}.handlers.rigid_body"]
    added_classes = [name for name in vars(handler) if name.startswith("RigidBody")]
    assert all("phase" not in name.lower() for name in added_classes)
