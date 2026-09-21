"""Regression coverage for the Phase 0 rigid-body surface."""

import asyncio
import sys

import pytest

from pydantic import ValidationError
from test_mutation_transaction import _load_addon

from blender_mcp.server.tools import _dispatch, rigid_body


def _run(function, **kwargs):
    return asyncio.run(function(ctx=None, **kwargs))


def test_all_twelve_phase_zero_rigid_body_commands_are_registered() -> None:
    names = {
        "get_rigid_body_scene_info",
        "get_rigid_body_object_info",
        "get_rigid_body_constraint_info",
        "configure_rigid_body_world",
        "add_rigid_bodies",
        "configure_rigid_bodies",
        "set_rigid_body_mass",
        "set_rigid_body_collision_layers",
        "create_rigid_body_collision_proxy",
        "create_rigid_body_constraint",
        "configure_rigid_body_constraint",
        "validate_rigid_body_setup",
    }

    assert all(callable(getattr(rigid_body, name)) for name in names)
    assert set(rigid_body.mcp._tool_manager._tools) >= names


def test_models_reject_unknown_nonfinite_and_conflicting_values() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        rigid_body.RigidBodySettingsPatch(arbitrary_rna=True)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        rigid_body.RigidBodySettingsPatch(mass=float("inf"))
    with pytest.raises(ValidationError, match="exactly one"):
        rigid_body.RigidBodyMassTarget(object_name="Box", mass=1.0, density=1000.0)
    with pytest.raises(ValidationError, match="mutually exclusive"):
        rigid_body.ConstraintTransform(
            location=(0, 0, 0),
            axis=(0, 0, 1),
            rotation_quaternion=(1, 0, 0, 0),
        )
    with pytest.raises(ValidationError, match="lower must not exceed upper"):
        rigid_body.LimitAxis(lower=1.0, upper=-1.0)


def test_constraint_schema_rejects_incompatible_fields() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        rigid_body.HingeConstraint(type="HINGE", linear_x={"use_limit": True})  # type: ignore[call-arg]
    motor = rigid_body.MotorConstraint(
        type="MOTOR",
        angular_motor=rigid_body.MotorAxis(enabled=True, target_velocity=2.0, max_impulse=10.0),
    )
    assert motor.model_dump(exclude_none=True)["angular_motor"]["max_impulse"] == pytest.approx(10.0)


def test_add_bodies_serializes_only_typed_settings(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        _dispatch,
        "send_command",
        lambda command, params=None: calls.append((command, params)) or {"ok": True},
    )

    result = _run(
        rigid_body.add_rigid_bodies,
        scene_name="Scene",
        object_names=["Crate", "Barrel"],
        body_type="ACTIVE",
        settings=rigid_body.RigidBodySettingsPatch(mass=5.0, collision_shape="CONVEX_HULL"),
    )

    assert result["changed_objects"] == ["Crate", "Barrel"]
    assert calls == [
        (
            "add_rigid_bodies",
            {
                "scene_name": "Scene",
                "object_names": ["Crate", "Barrel"],
                "body_type": "ACTIVE",
                "settings": {"mass": 5.0, "collision_shape": "CONVEX_HULL"},
                "source_settings_object_name": None,
                "world_collection_name": None,
                "existing_policy": "ERROR",
                "confirm_delete_baked_cache": False,
            },
        )
    ]


def test_constraint_discriminator_and_local_axis_payload(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        _dispatch,
        "send_command",
        lambda command, params=None: calls.append((command, params)) or {"ok": True},
    )

    _run(
        rigid_body.create_rigid_body_constraint,
        scene_name="Scene",
        name="Door Hinge",
        object1_name="Door",
        object2_name="Frame",
        transform=rigid_body.ConstraintTransform(location=(0, 0, 1), axis=(0, 0, 1)),
        configuration=rigid_body.HingeConstraint(
            type="HINGE",
            disable_collisions=True,
            angular_z=rigid_body.LimitAxis(use_limit=True, lower=-1.0, upper=1.0),
        ),
    )

    payload = calls[0][1]
    assert payload["transform"]["axis"] == (0.0, 0.0, 1.0)
    assert payload["configuration"]["type"] == "HINGE"
    assert payload["configuration"]["angular_z"] == {"use_limit": True, "lower": -1.0, "upper": 1.0}


def test_active_proxy_requires_an_explicit_driver() -> None:
    with pytest.raises(Exception, match="Active proxies must drive"):
        _run(
            rigid_body.create_rigid_body_collision_proxy,
            scene_name="Scene",
            source_object_name="Hero",
            proxy_name="Hero Proxy",
            collection_name="Physics Proxies",
            approximation="BOX",
            body_type="ACTIVE",
        )


def test_conflicting_body_and_proxy_settings_are_rejected() -> None:
    with pytest.raises(Exception, match=r"settings\.type must match"):
        _run(
            rigid_body.add_rigid_bodies,
            scene_name="Scene",
            object_names=["Crate"],
            body_type="ACTIVE",
            settings=rigid_body.RigidBodySettingsPatch(type="PASSIVE"),
        )
    with pytest.raises(Exception, match="collision_shape must match"):
        _run(
            rigid_body.create_rigid_body_collision_proxy,
            scene_name="Scene",
            source_object_name="Hero",
            proxy_name="Hero Proxy",
            collection_name="Physics Proxies",
            approximation="BOX",
            body_type="PASSIVE",
            settings=rigid_body.RigidBodySettingsPatch(collision_shape="SPHERE"),
        )


def test_empty_configuration_patches_are_rejected() -> None:
    with pytest.raises(Exception, match="settings patch"):
        _run(
            rigid_body.configure_rigid_bodies,
            scene_name="Scene",
            targets=[rigid_body.RigidBodyTarget(object_name="Crate", settings=rigid_body.RigidBodySettingsPatch())],
        )
    with pytest.raises(Exception, match="constraint setting"):
        _run(
            rigid_body.configure_rigid_body_constraint,
            scene_name="Scene",
            constraint_object_name="Hinge",
            configuration=rigid_body.HingeConstraint(type="HINGE"),
        )


def test_rigid_body_dispatch_and_read_only_contract(monkeypatch) -> None:
    addon, _bpy = _load_addon(monkeypatch, data={})
    server = addon.BlenderMCPServer()
    handlers = server._build_command_handlers()
    names = {
        "get_rigid_body_scene_info",
        "get_rigid_body_object_info",
        "get_rigid_body_constraint_info",
        "configure_rigid_body_world",
        "add_rigid_bodies",
        "configure_rigid_bodies",
        "set_rigid_body_mass",
        "set_rigid_body_collision_layers",
        "create_rigid_body_collision_proxy",
        "create_rigid_body_constraint",
        "configure_rigid_body_constraint",
        "validate_rigid_body_setup",
    }
    read_only = {
        "get_rigid_body_scene_info",
        "get_rigid_body_object_info",
        "get_rigid_body_constraint_info",
        "validate_rigid_body_setup",
    }

    assert set(handlers) >= names
    assert all(server.command_spec(name).read_only for name in read_only)
    assert not {name for name in names - read_only if server.command_spec(name).read_only}


def test_layer_profile_map_is_stable_and_one_based(monkeypatch) -> None:
    addon, _bpy = _load_addon(monkeypatch, data={})
    handler = sys.modules[f"{addon.__name__}.handlers.rigid_body"]

    assert {
        "ENVIRONMENT": {1},
        "HERO": {2},
        "DEBRIS": {3},
        "RAGDOLL": {4},
    } == handler._LAYER_PROFILES


def test_constraint_axis_mapping_matches_blender_conventions(monkeypatch) -> None:
    addon, _bpy = _load_addon(monkeypatch, data={})
    handler = sys.modules[f"{addon.__name__}.handlers.rigid_body"]

    assert handler._constraint_axis_fields("angular_z", {"use_limit": True, "lower": -0.5, "upper": 0.5}, False) == {
        "use_limit_ang_z": True,
        "limit_ang_z_lower": -0.5,
        "limit_ang_z_upper": 0.5,
    }
    assert handler._active_degrees_of_freedom("HINGE") == ["ANGULAR_Z"]
    assert handler._active_degrees_of_freedom("SLIDER") == ["LINEAR_X"]
