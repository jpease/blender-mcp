"""Regression coverage for advanced rigid-body workflows."""

import asyncio
import sys
import types

import pytest

from conftest import load_addon
from pydantic import ValidationError

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
    addon, _bpy = load_addon(monkeypatch, data={})
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


class _EndClampingPointCache:
    """A rigid-body world's PointCache whose `frame_end` Blender clamps to 300, silently, as RNA does."""

    def __init__(self) -> None:
        frame = types.SimpleNamespace(type="INT", is_readonly=False, hard_min=-1_048_574, hard_max=1_048_574)
        self.bl_rna = types.SimpleNamespace(properties={"frame_start": frame, "frame_end": frame})
        self.frame_start, self.frame_end, self.frame_step = 1, 250, 1
        self.name, self.index, self.filepath = "", -1, ""
        self.use_disk_cache = self.use_external = self.use_library_path = False
        self.is_baked = self.is_baking = False

    def __setattr__(self, name, value) -> None:
        if name == "frame_end":
            value = min(value, 300)
        object.__setattr__(self, name, value)


def test_manage_rigid_body_cache_refuses_and_undoes_a_frame_range_blender_did_not_keep(monkeypatch) -> None:
    """A range that reads back different is the client's request refused, not a fault in the add-on."""
    addon, _bpy = load_addon(monkeypatch, data={})
    simulation = sys.modules[f"{addon.__name__}.handlers.rigid_body.simulation"]
    cache = _EndClampingPointCache()
    scene = types.SimpleNamespace(name="Scene", rigidbody_world=types.SimpleNamespace(point_cache=cache))
    monkeypatch.setattr(simulation, "_scene", lambda _name: scene)

    with pytest.raises(ValueError, match=r"\[1, 400\]"):
        simulation.RigidBodySimulationHandlers().manage_rigid_body_cache(
            "Scene", action="CONFIGURE", settings={"frame_start": 1, "frame_end": 400}
        )

    assert (cache.frame_start, cache.frame_end) == (1, 250)


def _counted_objects(objects, by_type):
    """Build the counted page of more than ten `objects`: exact totals and the first ten names."""
    return {
        "total": len(objects),
        "by_type": by_type,
        "limit": 10,
        "returned_count": 10,
        "truncated": True,
        "names": [obj.name for obj in objects[:10]],
    }


def test_a_cache_calculation_counts_its_bodies_and_names_none_of_them_as_changed(monkeypatch) -> None:
    """57 bodies is a count, a by-type split and ten names; the caller acts next on the scene."""
    addon, _bpy = load_addon(monkeypatch, data={})
    simulation = sys.modules[f"{addon.__name__}.handlers.rigid_body.simulation"]
    bodies = [
        types.SimpleNamespace(
            name=f"Shard {index:02d}", type="EMPTY" if index % 8 == 3 else "MESH", rigid_body=object()
        )
        for index in range(57)
    ]
    ground = types.SimpleNamespace(name="Ground Plate", type="MESH", rigid_body=None)
    cache = _EndClampingPointCache()
    cache.frame_end = 10
    scene = types.SimpleNamespace(
        name="Scene",
        rigidbody_world=types.SimpleNamespace(point_cache=cache),
        objects=[ground, *bodies],
        frame_current=1,
        frame_subframe=0.0,
        frame_set=lambda _frame, subframe=0.0: None,
    )
    monkeypatch.setattr(simulation, "_scene", lambda _name: scene)
    monkeypatch.setattr(simulation, "_view_layer_for", lambda *_args: types.SimpleNamespace(update=lambda: None))

    reply = simulation.RigidBodySimulationHandlers().manage_rigid_body_cache(
        "Scene", action="CALCULATE_TO_FRAME", calculate_frame=10
    )

    assert reply["changed_objects"] == []
    assert reply["changed_resources"] == ["Scene"]
    assert reply["simulated_objects"] == _counted_objects(bodies, {"EMPTY": 7, "MESH": 50})


class _TaggedHelper(types.SimpleNamespace):
    """A scene object carrying the rigid-body ownership tags `remove_rigid_body_components` reads."""

    def get(self, key, default=None):
        return self.tags.get(key, default)


def test_removing_a_rigs_helpers_counts_them_and_names_none_as_a_next_target(monkeypatch) -> None:
    """A rig's 57 helpers are gone: `removed` counts them; nothing is left for changed_objects to name."""
    removed = []
    addon, _bpy = load_addon(
        monkeypatch,
        data={"objects": types.SimpleNamespace(remove=lambda obj, do_unlink: removed.append(obj.name))},
    )
    lifecycle = sys.modules[f"{addon.__name__}.handlers.rigid_body.lifecycle"]
    owned = {"blendermcp_rigid_body_role": "PROXY", "blendermcp_rigid_body_rig_id": "debris"}
    helpers = [
        _TaggedHelper(name=f"Debris Proxy {index:02d}", type="EMPTY" if index % 3 else "MESH", tags=owned)
        for index in range(57)
    ]
    other_rig = _TaggedHelper(name="Bridge Proxy", type="MESH", tags={**owned, "blendermcp_rigid_body_rig_id": "b"})
    scene = types.SimpleNamespace(name="Scene", rigidbody_world=None, objects=[other_rig, *helpers])
    monkeypatch.setattr(lifecycle, "_scene", lambda _name: scene)

    reply = lifecycle.RigidBodyLifecycleHandlers().remove_rigid_body_components(
        "Scene", "TAGGED_HELPERS", rig_id="debris", confirm_destructive=True
    )

    assert removed == [helper.name for helper in helpers]
    assert reply["changed_objects"] == []
    assert reply["removed"] == _counted_objects(helpers, {"EMPTY": 38, "MESH": 19})


def test_force_field_model_rejects_unknown_and_nonfinite_values() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        rigid_body.RigidBodyForceField(object_name="Wind", field_type="WIND", arbitrary=True)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        rigid_body.RigidBodyForceField(object_name="Wind", field_type="WIND", strength=float("inf"))


def test_package_and_handler_class_names_are_purpose_based(monkeypatch) -> None:
    assert rigid_body.__name__.endswith(".rigid_body")
    addon, _bpy = load_addon(monkeypatch, data={})
    handler = sys.modules[f"{addon.__name__}.handlers.rigid_body"]
    assert hasattr(handler, "RigidBodyHandlersMixin")
    assert not any("phase" in name.lower() for name in vars(handler) if isinstance(name, str))
