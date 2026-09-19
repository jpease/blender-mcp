"""Typed tools for rigid-body inspection, setup, constraints, and validation."""

import asyncio
import logging
import sys

from collections.abc import Sequence
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from ...app import mcp
from ...connection import get_blender_connection
from ..envelope import envelope_for

logger = logging.getLogger("BlenderMCPServer")

Vector3 = tuple[float, float, float]
Quaternion = tuple[float, float, float, float]
BodyType = Literal["ACTIVE", "PASSIVE"]
CollisionShape = Literal["BOX", "SPHERE", "CAPSULE", "CYLINDER", "CONE", "CONVEX_HULL", "MESH", "COMPOUND"]
MeshSource = Literal["BASE", "DEFORM", "FINAL"]
ConstraintType = Literal["FIXED", "POINT", "HINGE", "SLIDER", "PISTON", "GENERIC", "GENERIC_SPRING", "MOTOR"]


def _connection_call(command: str, params: dict, changed_objects: list[str] | None = None) -> dict:
    try:
        result = get_blender_connection().send_command(command, params)
    except Exception as exc:
        logger.error("Error running %s: %s", command, exc)
        raise ToolError(f"Error running {command}: {exc}") from exc
    return envelope_for(result, changed_objects=changed_objects or ())


def _call(command: str, params: dict, changed_objects: list[str] | None = None) -> dict:
    """Dispatch through the package hook so tests and embedders can replace the transport."""
    package = sys.modules.get(__package__) if __package__ is not None else None
    override = getattr(package, "_call", None) if package is not None else None
    if override is not None and override is not _call:
        return override(command, params, changed_objects)
    return _connection_call(command, params, changed_objects)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class RigidBodyWorldPatch(_StrictModel):
    enabled: bool | None = None
    time_scale: float | None = Field(default=None, ge=0.0, le=100.0)
    substeps_per_frame: int | None = Field(default=None, ge=1, le=32767)
    solver_iterations: int | None = Field(default=None, ge=1, le=1000)
    use_split_impulse: bool | None = None


class RigidBodyCachePatch(_StrictModel):
    frame_start: int | None = Field(default=None, ge=-1_000_000, le=1_000_000)
    frame_end: int | None = Field(default=None, ge=-1_000_000, le=1_000_000)
    frame_step: int | None = Field(default=None, ge=1, le=1000)

    @model_validator(mode="after")
    def validate_range(self) -> "RigidBodyCachePatch":
        if (self.frame_start is None) != (self.frame_end is None):
            raise ValueError("frame_start and frame_end must be supplied together")
        if self.frame_start is not None and self.frame_end is not None and self.frame_start > self.frame_end:
            raise ValueError("frame_start must not exceed frame_end")
        return self


class RigidBodyEffectorWeightsPatch(_StrictModel):
    all: float | None = Field(default=None, ge=0.0, le=1.0)
    gravity: float | None = Field(default=None, ge=0.0, le=1.0)
    force: float | None = Field(default=None, ge=0.0, le=1.0)
    vortex: float | None = Field(default=None, ge=0.0, le=1.0)
    magnetic: float | None = Field(default=None, ge=0.0, le=1.0)
    wind: float | None = Field(default=None, ge=0.0, le=1.0)
    curve_guide: float | None = Field(default=None, ge=0.0, le=1.0)
    texture: float | None = Field(default=None, ge=0.0, le=1.0)
    harmonic: float | None = Field(default=None, ge=0.0, le=1.0)
    charge: float | None = Field(default=None, ge=0.0, le=1.0)
    lennardjones: float | None = Field(default=None, ge=0.0, le=1.0)
    turbulence: float | None = Field(default=None, ge=0.0, le=1.0)
    drag: float | None = Field(default=None, ge=0.0, le=1.0)
    boid: float | None = Field(default=None, ge=0.0, le=1.0)
    smokeflow: float | None = Field(default=None, ge=0.0, le=1.0)
    collection_name: str | None = None
    clear_collection: bool = False

    @model_validator(mode="after")
    def validate_collection(self) -> "RigidBodyEffectorWeightsPatch":
        if self.collection_name is not None and self.clear_collection:
            raise ValueError("collection_name and clear_collection are mutually exclusive")
        return self


class RigidBodySettingsPatch(_StrictModel):
    type: BodyType | None = None
    enabled: bool | None = None
    kinematic: bool | None = None
    collision_shape: CollisionShape | None = None
    mesh_source: MeshSource | None = None
    use_deform: bool | None = None
    mass: float | None = Field(default=None, ge=0.001)
    use_margin: bool | None = None
    collision_margin: float | None = Field(default=None, ge=0.0, le=1.0)
    friction: float | None = Field(default=None, ge=0.0)
    restitution: float | None = Field(default=None, ge=0.0)
    linear_damping: float | None = Field(default=None, ge=0.0, le=1.0)
    angular_damping: float | None = Field(default=None, ge=0.0, le=1.0)
    use_deactivation: bool | None = None
    use_start_deactivated: bool | None = None
    deactivate_linear_velocity: float | None = Field(default=None, ge=0.0)
    deactivate_angular_velocity: float | None = Field(default=None, ge=0.0)


class RigidBodyTarget(_StrictModel):
    object_name: str = Field(min_length=1)
    settings: RigidBodySettingsPatch


class RigidBodyMassTarget(_StrictModel):
    object_name: str = Field(min_length=1)
    mass: float | None = Field(default=None, ge=0.001)
    density: float | None = Field(default=None, gt=0.0)

    @model_validator(mode="after")
    def validate_source(self) -> "RigidBodyMassTarget":
        if (self.mass is None) == (self.density is None):
            raise ValueError("Supply exactly one of mass or density")
        return self


LayerProfile = Literal["ENVIRONMENT", "HERO", "DEBRIS", "RAGDOLL"]


class RigidBodyLayerTarget(_StrictModel):
    object_name: str = Field(min_length=1)
    layers: list[Annotated[int, Field(ge=1, le=20)]] | None = Field(default=None, max_length=20)
    profile: LayerProfile | None = None

    @model_validator(mode="after")
    def validate_layer_source(self) -> "RigidBodyLayerTarget":
        if (self.layers is None) == (self.profile is None):
            raise ValueError("Supply exactly one of layers or profile")
        if self.layers is not None and len(set(self.layers)) != len(self.layers):
            raise ValueError("layers must not contain duplicates")
        return self


class ConstraintCommon(_StrictModel):
    type: ConstraintType
    enabled: bool | None = None
    disable_collisions: bool | None = None
    use_breaking: bool | None = None
    breaking_threshold: float | None = Field(default=None, ge=0.0)
    use_override_solver_iterations: bool | None = None
    solver_iterations: int | None = Field(default=None, ge=1, le=1000)


class LimitAxis(_StrictModel):
    use_limit: bool | None = None
    lower: float | None = None
    upper: float | None = None

    @model_validator(mode="after")
    def validate_limits(self) -> "LimitAxis":
        if self.lower is not None and self.upper is not None and self.lower > self.upper:
            raise ValueError("lower must not exceed upper")
        return self


class SpringAxis(LimitAxis):
    use_spring: bool | None = None
    stiffness: float | None = Field(default=None, ge=0.0)
    damping: float | None = Field(default=None, ge=0.0)


class MotorAxis(_StrictModel):
    enabled: bool | None = None
    target_velocity: float | None = None
    max_impulse: float | None = Field(default=None, ge=0.0)


class FixedConstraint(ConstraintCommon):
    type: Literal["FIXED"]


class PointConstraint(ConstraintCommon):
    type: Literal["POINT"]


class HingeConstraint(ConstraintCommon):
    type: Literal["HINGE"]
    angular_z: LimitAxis | None = None


class SliderConstraint(ConstraintCommon):
    type: Literal["SLIDER"]
    linear_x: LimitAxis | None = None


class PistonConstraint(ConstraintCommon):
    type: Literal["PISTON"]
    linear_x: LimitAxis | None = None
    angular_x: LimitAxis | None = None


class GenericConstraint(ConstraintCommon):
    type: Literal["GENERIC"]
    linear_x: LimitAxis | None = None
    linear_y: LimitAxis | None = None
    linear_z: LimitAxis | None = None
    angular_x: LimitAxis | None = None
    angular_y: LimitAxis | None = None
    angular_z: LimitAxis | None = None


class GenericSpringConstraint(ConstraintCommon):
    type: Literal["GENERIC_SPRING"]
    spring_type: Literal["SPRING1", "SPRING2"] | None = None
    linear_x: SpringAxis | None = None
    linear_y: SpringAxis | None = None
    linear_z: SpringAxis | None = None
    angular_x: SpringAxis | None = None
    angular_y: SpringAxis | None = None
    angular_z: SpringAxis | None = None


class MotorConstraint(ConstraintCommon):
    type: Literal["MOTOR"]
    linear_motor: MotorAxis | None = None
    angular_motor: MotorAxis | None = None


RigidBodyConstraintSpec = Annotated[
    FixedConstraint
    | PointConstraint
    | HingeConstraint
    | SliderConstraint
    | PistonConstraint
    | GenericConstraint
    | GenericSpringConstraint
    | MotorConstraint,
    Field(discriminator="type"),
]

# Validated where `configuration` dicts arrive (create_rigid_body_constraint,
# configure_rigid_body_constraint, and the network/chain/ragdoll builders elsewhere in this
# package) rather than declared directly as a tool parameter type: exposing
# RigidBodyConstraintSpec that way would serialize this whole 8-variant discriminated union
# into every one of those tools' advertised JSON schemas on every client connection.
rigid_body_constraint_adapter = TypeAdapter(RigidBodyConstraintSpec)


class ConstraintTransform(_StrictModel):
    """World-space constraint transform; quaternion order is [w, x, y, z]."""

    location: Vector3
    rotation_quaternion: Quaternion | None = None
    axis: Vector3 | None = None

    @model_validator(mode="after")
    def validate_orientation(self) -> "ConstraintTransform":
        if self.rotation_quaternion is not None and self.axis is not None:
            raise ValueError("rotation_quaternion and axis are mutually exclusive")
        if self.rotation_quaternion is not None and sum(v * v for v in self.rotation_quaternion) <= 1e-16:
            raise ValueError("rotation_quaternion must be non-zero")
        if self.axis is not None and sum(v * v for v in self.axis) <= 1e-16:
            raise ValueError("axis must be non-zero")
        return self


def _dump(model: BaseModel | None) -> dict | None:
    return model.model_dump(exclude_none=True, exclude_unset=True) if model is not None else None


def _models(items: Sequence[BaseModel]) -> list[dict]:
    return [item.model_dump(exclude_none=True, exclude_unset=True) for item in items]


@mcp.tool()
async def get_rigid_body_scene_info(
    ctx: Context,
    scene_name: Annotated[str, Field(min_length=1)],
    member_limit: Annotated[int, Field(ge=1, le=500)] = 100,
    member_offset: Annotated[int, Field(ge=0, le=99_999)] = 0,
    constraint_limit: Annotated[int, Field(ge=1, le=500)] = 100,
    constraint_offset: Annotated[int, Field(ge=0, le=99_999)] = 0,
) -> dict:
    """Inspect one scene's rigid-body world and paginated membership without evaluating another frame."""
    return await asyncio.to_thread(
        _call,
        "get_rigid_body_scene_info",
        {
            "scene_name": scene_name,
            "member_limit": member_limit,
            "member_offset": member_offset,
            "constraint_limit": constraint_limit,
            "constraint_offset": constraint_offset,
        },
    )


@mcp.tool()
async def get_rigid_body_object_info(
    ctx: Context,
    object_names: Annotated[list[str], Field(min_length=1, max_length=100)],
) -> dict:
    """Inspect complete rigid-body, transform, animation, bounds, and geometry state for explicit objects."""
    return await asyncio.to_thread(_call, "get_rigid_body_object_info", {"object_names": object_names})


@mcp.tool()
async def get_rigid_body_constraint_info(
    ctx: Context,
    scene_name: Annotated[str, Field(min_length=1)],
    constraint_object_names: Annotated[list[str], Field(max_length=500)] | None = None,
    limit: Annotated[int, Field(ge=1, le=500)] = 100,
    offset: Annotated[int, Field(ge=0, le=99_999)] = 0,
) -> dict:
    """Inspect typed rigid-body constraints, endpoints, local axes, and world membership."""
    return await asyncio.to_thread(
        _call,
        "get_rigid_body_constraint_info",
        {
            "scene_name": scene_name,
            "constraint_object_names": constraint_object_names,
            "limit": limit,
            "offset": offset,
        },
    )


@mcp.tool()
async def configure_rigid_body_world(
    ctx: Context,
    scene_name: Annotated[str, Field(min_length=1)],
    body_collection_name: Annotated[str, Field(min_length=1)] = "RigidBodyWorld",
    constraint_collection_name: Annotated[str, Field(min_length=1)] = "RigidBodyConstraints",
    world: RigidBodyWorldPatch | None = None,
    gravity: Vector3 | None = None,
    use_gravity: bool | None = None,
    cache: RigidBodyCachePatch | None = None,
    effector_weights: RigidBodyEffectorWeightsPatch | None = None,
    confirm_reassign_populated_collections: bool = False,
    confirm_delete_baked_cache: bool = False,
) -> dict:
    """Create or patch one scene world; populated collection reassignment and bake deletion require confirmation."""
    return await asyncio.to_thread(
        _call,
        "configure_rigid_body_world",
        {
            "scene_name": scene_name,
            "body_collection_name": body_collection_name,
            "constraint_collection_name": constraint_collection_name,
            "world": _dump(world) or {},
            "gravity": gravity,
            "use_gravity": use_gravity,
            "cache": _dump(cache) or {},
            "effector_weights": _dump(effector_weights) or {},
            "confirm_reassign_populated_collections": confirm_reassign_populated_collections,
            "confirm_delete_baked_cache": confirm_delete_baked_cache,
        },
    )


@mcp.tool()
async def add_rigid_bodies(
    ctx: Context,
    scene_name: Annotated[str, Field(min_length=1)],
    object_names: Annotated[list[str], Field(min_length=1, max_length=500)],
    body_type: BodyType,
    settings: RigidBodySettingsPatch | None = None,
    source_settings_object_name: Annotated[str | None, Field(min_length=1)] = None,
    world_collection_name: Annotated[str | None, Field(min_length=1)] = None,
    existing_policy: Literal["ERROR", "REUSE"] = "ERROR",
    confirm_delete_baked_cache: bool = False,
) -> dict:
    """
    Atomically add rigid bodies to validated mesh objects and preserve their existing collection links.

    existing_policy="ERROR" (default) rejects any object that already has a rigid body; "REUSE" patches
    it in place instead. To modify bodies you know already exist, prefer configure_rigid_bodies.
    """
    if settings is not None and settings.type is not None and settings.type != body_type:
        raise ToolError("settings.type must match body_type")
    return await asyncio.to_thread(
        _call,
        "add_rigid_bodies",
        {
            "scene_name": scene_name,
            "object_names": object_names,
            "body_type": body_type,
            "settings": _dump(settings) or {},
            "source_settings_object_name": source_settings_object_name,
            "world_collection_name": world_collection_name,
            "existing_policy": existing_policy,
            "confirm_delete_baked_cache": confirm_delete_baked_cache,
        },
        object_names,
    )


@mcp.tool()
async def configure_rigid_bodies(
    ctx: Context,
    scene_name: Annotated[str, Field(min_length=1)],
    targets: Annotated[list[RigidBodyTarget], Field(min_length=1, max_length=500)],
    confirm_delete_baked_cache: bool = False,
) -> dict:
    """
    Atomically patch allowlisted rigid-body properties after validating the complete batch.

    Every target object must already have a rigid body; use add_rigid_bodies first for objects that
    do not.
    """
    if any(not target.settings.model_fields_set for target in targets):
        raise ToolError("Every target settings patch must contain at least one property")
    return await asyncio.to_thread(
        _call,
        "configure_rigid_bodies",
        {
            "scene_name": scene_name,
            "targets": _models(targets),
            "confirm_delete_baked_cache": confirm_delete_baked_cache,
        },
        [target.object_name for target in targets],
    )


@mcp.tool()
async def set_rigid_body_mass(
    ctx: Context,
    scene_name: Annotated[str, Field(min_length=1)],
    assignments: Annotated[list[RigidBodyMassTarget], Field(min_length=1, max_length=500)],
    target_total_mass: Annotated[float | None, Field(gt=0.0)] = None,
    confirm_delete_baked_cache: bool = False,
) -> dict:
    """
    Set direct mass or derive it from density and evaluated closed-mesh world volume.

    Each assignment must already have a rigid body and supplies exactly one of mass or density
    (density-derived mass depends on a closed/manifold evaluated mesh). target_total_mass, when given,
    additionally normalizes the batch's resulting masses to sum to that total.
    """
    return await asyncio.to_thread(
        _call,
        "set_rigid_body_mass",
        {
            "scene_name": scene_name,
            "assignments": _models(assignments),
            "target_total_mass": target_total_mass,
            "confirm_delete_baked_cache": confirm_delete_baked_cache,
        },
        [item.object_name for item in assignments],
    )


@mcp.tool()
async def set_rigid_body_collision_layers(
    ctx: Context,
    scene_name: Annotated[str, Field(min_length=1)],
    targets: Annotated[list[RigidBodyLayerTarget], Field(min_length=1, max_length=500)],
    policy: Literal["REPLACE", "ADD", "REMOVE"] = "REPLACE",
    confirm_delete_baked_cache: bool = False,
) -> dict:
    """Set the 20 Bullet collision flags using explicit 1-based layers or stable named profiles."""
    return await asyncio.to_thread(
        _call,
        "set_rigid_body_collision_layers",
        {
            "scene_name": scene_name,
            "targets": _models(targets),
            "policy": policy,
            "confirm_delete_baked_cache": confirm_delete_baked_cache,
        },
        [target.object_name for target in targets],
    )


@mcp.tool()
async def create_rigid_body_collision_proxy(
    ctx: Context,
    scene_name: Annotated[str, Field(min_length=1)],
    source_object_name: Annotated[str, Field(min_length=1)],
    proxy_name: Annotated[str, Field(min_length=1)],
    collection_name: Annotated[str, Field(min_length=1)],
    approximation: Literal["BOX", "SPHERE", "CAPSULE", "CYLINDER", "CONVEX_HULL", "LOW_RES_SOURCE"],
    body_type: BodyType,
    low_resolution_source_name: Annotated[str | None, Field(min_length=1)] = None,
    drive_render_object: Literal["NONE", "PARENT", "COPY_TRANSFORMS"] = "NONE",
    hide_from_render: bool = True,
    settings: RigidBodySettingsPatch | None = None,
    confirm_delete_baked_cache: bool = False,
) -> dict:
    """Create a non-destructive collision proxy from evaluated geometry and optionally drive the render object."""
    if approximation == "LOW_RES_SOURCE" and not low_resolution_source_name:
        raise ToolError("low_resolution_source_name is required for LOW_RES_SOURCE")
    if approximation != "LOW_RES_SOURCE" and low_resolution_source_name:
        raise ToolError("low_resolution_source_name is only valid for LOW_RES_SOURCE")
    if body_type == "ACTIVE" and drive_render_object == "NONE":
        raise ToolError("Active proxies must drive the render object via PARENT or COPY_TRANSFORMS")
    if settings is not None and settings.type is not None and settings.type != body_type:
        raise ToolError("settings.type must match body_type")
    expected_shape = "CONVEX_HULL" if approximation in {"CONVEX_HULL", "LOW_RES_SOURCE"} else approximation
    if settings is not None and settings.collision_shape is not None and settings.collision_shape != expected_shape:
        raise ToolError("settings.collision_shape must match the selected proxy approximation")
    return await asyncio.to_thread(
        _call,
        "create_rigid_body_collision_proxy",
        {
            "scene_name": scene_name,
            "source_object_name": source_object_name,
            "proxy_name": proxy_name,
            "collection_name": collection_name,
            "approximation": approximation,
            "body_type": body_type,
            "low_resolution_source_name": low_resolution_source_name,
            "drive_render_object": drive_render_object,
            "hide_from_render": hide_from_render,
            "settings": _dump(settings) or {},
            "confirm_delete_baked_cache": confirm_delete_baked_cache,
        },
        [source_object_name, proxy_name],
    )


@mcp.tool()
async def create_rigid_body_constraint(
    ctx: Context,
    scene_name: Annotated[str, Field(min_length=1)],
    name: Annotated[str, Field(min_length=1)],
    object1_name: Annotated[str, Field(min_length=1)],
    object2_name: Annotated[str, Field(min_length=1)],
    transform: ConstraintTransform,
    configuration: dict[str, Any],
    collection_name: Annotated[str | None, Field(min_length=1)] = None,
    confirm_delete_baked_cache: bool = False,
) -> dict:
    """
    Create a typed rigid-body constraint at an explicit world transform between two bodies.

    configuration is a discriminated-union object keyed by "type": FIXED, POINT, HINGE, SLIDER,
    PISTON, GENERIC, GENERIC_SPRING, or MOTOR - each with its own additional fields, validated
    against Blender's real constraint schema before this call reaches Blender.
    """
    validated_configuration = rigid_body_constraint_adapter.validate_python(configuration)
    return await asyncio.to_thread(
        _call,
        "create_rigid_body_constraint",
        {
            "scene_name": scene_name,
            "name": name,
            "object1_name": object1_name,
            "object2_name": object2_name,
            "transform": transform.model_dump(exclude_none=True),
            "configuration": validated_configuration.model_dump(exclude_none=True, exclude_unset=True),
            "collection_name": collection_name,
            "confirm_delete_baked_cache": confirm_delete_baked_cache,
        },
        [object1_name, object2_name, name],
    )


@mcp.tool()
async def configure_rigid_body_constraint(
    ctx: Context,
    scene_name: Annotated[str, Field(min_length=1)],
    constraint_object_name: Annotated[str, Field(min_length=1)],
    configuration: dict[str, Any],
    object1_name: Annotated[str | None, Field(min_length=1)] = None,
    object2_name: Annotated[str | None, Field(min_length=1)] = None,
    confirm_delete_baked_cache: bool = False,
) -> dict:
    """
    Patch a constraint through a type-specific schema and optionally replace validated endpoints.

    configuration is a discriminated-union object keyed by "type": FIXED, POINT, HINGE, SLIDER,
    PISTON, GENERIC, GENERIC_SPRING, or MOTOR - each with its own additional fields, validated
    against Blender's real constraint schema before this call reaches Blender.
    """
    validated_configuration = rigid_body_constraint_adapter.validate_python(configuration)
    if len(validated_configuration.model_fields_set) == 1 and object1_name is None and object2_name is None:
        raise ToolError("Provide at least one constraint setting or endpoint change")
    return await asyncio.to_thread(
        _call,
        "configure_rigid_body_constraint",
        {
            "scene_name": scene_name,
            "constraint_object_name": constraint_object_name,
            "configuration": validated_configuration.model_dump(exclude_none=True, exclude_unset=True),
            "object1_name": object1_name,
            "object2_name": object2_name,
            "confirm_delete_baked_cache": confirm_delete_baked_cache,
        },
        [name for name in [constraint_object_name, object1_name, object2_name] if name],
    )


@mcp.tool()
async def validate_rigid_body_setup(
    ctx: Context,
    scene_name: Annotated[str, Field(min_length=1)],
    object_names: Annotated[list[str], Field(max_length=500)] | None = None,
    max_findings: Annotated[int, Field(ge=1, le=1000)] = 200,
    collision_pair_limit: Annotated[int, Field(ge=1, le=256)] = 64,
    evaluated_triangle_limit: Annotated[int, Field(ge=1000, le=1_000_000)] = 250_000,
) -> dict:
    """Run a bounded, non-mutating rigid-body preflight and report evidence-based findings."""
    return await asyncio.to_thread(
        _call,
        "validate_rigid_body_setup",
        {
            "scene_name": scene_name,
            "object_names": object_names,
            "max_findings": max_findings,
            "collision_pair_limit": collision_pair_limit,
            "evaluated_triangle_limit": evaluated_triangle_limit,
        },
    )
