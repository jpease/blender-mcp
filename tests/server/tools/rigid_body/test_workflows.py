"""Regression coverage for advanced rigid-body workflows."""

import asyncio
import sys

import pytest

from pydantic import ValidationError
from test_mutation_transaction import _load_addon

from blender_mcp.server.tools import _dispatch, rigid_body

WORKFLOW_COMMANDS = {
    "remove_rigid_body_components",
    "animate_rigid_body_release",
    "create_compound_rigid_body",
    "create_rigid_body_constraint_network",
    "prepare_fracture_rigid_bodies",
    "create_rigid_body_chain",
    "setup_animated_passive_collider",
    "configure_rigid_body_force_fields",
    "sample_rigid_body_simulation",
    "manage_rigid_body_cache",
    "bake_rigid_bodies_to_keyframes",
}


def _run(function, **kwargs):
    return asyncio.run(function(ctx=None, **kwargs))


def test_all_advanced_rigid_body_commands_are_registered() -> None:
    assert all(callable(getattr(rigid_body, name)) for name in WORKFLOW_COMMANDS)
    assert set(rigid_body.mcp._tool_manager._tools) >= WORKFLOW_COMMANDS


def test_blender_dispatch_exposes_every_workflow_command(monkeypatch) -> None:
    addon, _bpy = _load_addon(monkeypatch, data={})
    server = addon.BlenderMCPServer()

    assert set(server._build_command_handlers()) >= WORKFLOW_COMMANDS
    assert not server.command_spec("sample_rigid_body_simulation").read_only
    assert server._run_handler.__self__ is server


def test_frame_selection_requires_one_ordered_source() -> None:
    with pytest.raises(ValidationError, match="either frames"):
        rigid_body.SimulationFrameSelection()
    with pytest.raises(ValidationError, match="unique and ordered"):
        rigid_body.SimulationFrameSelection(frames=[2, 1])
    with pytest.raises(ValidationError, match="supplied together"):
        rigid_body.SimulationFrameSelection(frame_start=1)
    assert rigid_body.SimulationFrameSelection(frame_start=1, frame_end=5, frame_step=2).frame_step == 2


def test_constraint_edges_reject_self_links() -> None:
    with pytest.raises(ValidationError, match="distinct"):
        rigid_body.ConstraintEdge(object1_name="Shard", object2_name="Shard")


def test_destructive_removal_requires_confirmation() -> None:
    with pytest.raises(Exception, match="confirm_destructive"):
        _run(
            rigid_body.remove_rigid_body_components,
            scene_name="Scene",
            component_type="WORLD",
        )


def test_constraint_network_payload_is_typed(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        _dispatch,
        "send_command",
        lambda command, params=None: calls.append((command, params)) or {"ok": True},
    )
    configuration = rigid_body.HingeConstraint(
        type="HINGE",
        angular_z=rigid_body.LimitAxis(use_limit=True, lower=-0.5, upper=0.5),
    )
    result = _run(
        rigid_body.create_rigid_body_constraint_network,
        scene_name="Scene",
        network_name="Bridge",
        body_names=["A", "B"],
        configuration=configuration,
        edges=[rigid_body.ConstraintEdge(object1_name="A", object2_name="B")],
    )

    command, params = calls[0]
    assert command == "create_rigid_body_constraint_network"
    assert params["configuration"]["type"] == "HINGE"
    assert params["edges"] == [{"object1_name": "A", "object2_name": "B"}]
    assert result["changed_objects"] == ["A", "B"]


def test_cache_action_boundaries_are_explicit() -> None:
    with pytest.raises(Exception, match="requires settings"):
        _run(rigid_body.manage_rigid_body_cache, scene_name="Scene", action="CONFIGURE")
    with pytest.raises(Exception, match="only for CALCULATE_TO_FRAME"):
        _run(rigid_body.manage_rigid_body_cache, scene_name="Scene", action="INSPECT", calculate_frame=10)


def test_force_field_model_rejects_unknown_and_nonfinite_values() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        rigid_body.RigidBodyForceField(object_name="Wind", field_type="WIND", arbitrary=True)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        rigid_body.RigidBodyForceField(object_name="Wind", field_type="WIND", strength=float("inf"))


def test_package_and_handler_class_names_are_purpose_based(monkeypatch) -> None:
    assert rigid_body.__name__.endswith(".rigid_body")
    addon, _bpy = _load_addon(monkeypatch, data={})
    handler = sys.modules[f"{addon.__name__}.handlers.rigid_body"]
    assert hasattr(handler, "RigidBodyHandlersMixin")
    assert not any("phase" in name.lower() for name in vars(handler) if isinstance(name, str))
