"""Typed tools for armature foundations, skinning, constraints, and rig validation."""

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
from ..envelope import ok

logger = logging.getLogger("BlenderMCPServer")

Vector3 = tuple[float, float, float]
Quaternion = tuple[float, float, float, float]
ConstraintSpace = Literal["WORLD", "CUSTOM", "POSE", "LOCAL_WITH_PARENT", "LOCAL"]
ExistingPolicy = Literal["ERROR", "UPDATE"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class RigWorldTransform(_StrictModel):
    """An explicit world transform; quaternion order is [w, x, y, z]."""

    location: Vector3 = (0.0, 0.0, 0.0)
    rotation_quaternion: Quaternion = (1.0, 0.0, 0.0, 0.0)
    scale: Vector3 = (1.0, 1.0, 1.0)

    @model_validator(mode="after")
    def validate_transform(self) -> "RigWorldTransform":
        if sum(value * value for value in self.rotation_quaternion) <= 1e-16:
            raise ValueError("rotation_quaternion must be non-zero")
        if any(value == 0 for value in self.scale):
            raise ValueError("scale components must be non-zero")
        return self


class ArmatureDisplaySettings(_StrictModel):
    pose_position: Literal["POSE", "REST"] = "POSE"
    display_type: Literal["OCTAHEDRAL", "STICK", "BBONE", "ENVELOPE", "WIRE"] = "OCTAHEDRAL"
    show_axes: bool = False
    show_names: bool = True
    show_in_front: bool = True
    axes_position: float = Field(default=0.0, ge=0.0, le=1.0)
    relation_line_position: Literal["TAIL", "HEAD"] = "TAIL"
    show_bone_custom_shapes: bool = True
    show_bone_colors: bool = True


class InitialBone(_StrictModel):
    """One rest bone in armature-local space."""

    name: str = Field(min_length=1, max_length=63)
    head: Vector3
    tail: Vector3
    roll: float = 0.0
    parent: str | None = None
    use_connect: bool = False
    use_deform: bool = True
    inherit_scale: Literal["FULL", "FIX_SHEAR", "ALIGNED", "AVERAGE", "NONE", "NONE_LEGACY"] = "FULL"
    envelope_distance: float = Field(default=0.25, ge=0.0)
    envelope_weight: float = Field(default=1.0, ge=0.0)
    head_radius: float = Field(default=0.1, ge=0.0)
    tail_radius: float = Field(default=0.05, ge=0.0)
    collections: list[str] = Field(default_factory=list, max_length=64)


class CreateBoneOperation(InitialBone):
    operation: Literal["CREATE"] = "CREATE"


class RenameBoneOperation(_StrictModel):
    operation: Literal["RENAME"] = "RENAME"
    bone_name: str = Field(min_length=1, max_length=63)
    new_name: str = Field(min_length=1, max_length=63)
    reference_policy: Literal["UPDATE", "ERROR"]


class UpdateBoneOperation(_StrictModel):
    operation: Literal["UPDATE"] = "UPDATE"
    bone_name: str = Field(min_length=1, max_length=63)
    head: Vector3 | None = None
    tail: Vector3 | None = None
    roll: float | None = None
    align_roll_vector: Vector3 | None = None
    align_orientation_bone: str | None = None
    parent: str | None = None
    clear_parent: bool = False
    use_connect: bool | None = None
    use_deform: bool | None = None
    inherit_scale: Literal["FULL", "FIX_SHEAR", "ALIGNED", "AVERAGE", "NONE", "NONE_LEGACY"] | None = None
    envelope_distance: float | None = Field(default=None, ge=0.0)
    envelope_weight: float | None = Field(default=None, ge=0.0)
    head_radius: float | None = Field(default=None, ge=0.0)
    tail_radius: float | None = Field(default=None, ge=0.0)

    @model_validator(mode="after")
    def validate_parent_and_alignment(self) -> "UpdateBoneOperation":
        if self.parent is not None and self.clear_parent:
            raise ValueError("parent and clear_parent are mutually exclusive")
        if self.align_roll_vector is not None and self.align_orientation_bone is not None:
            raise ValueError("align_roll_vector and align_orientation_bone are mutually exclusive")
        return self


class DeleteBoneOperation(_StrictModel):
    operation: Literal["DELETE"] = "DELETE"
    bone_name: str = Field(min_length=1, max_length=63)
    reference_policy: Literal["ERROR", "REMOVE_REFERENCES"]


BoneOperation = Annotated[
    CreateBoneOperation | RenameBoneOperation | UpdateBoneOperation | DeleteBoneOperation,
    Field(discriminator="operation"),
]


class CollectionCreate(_StrictModel):
    operation: Literal["CREATE"] = "CREATE"
    name: str = Field(min_length=1, max_length=63)
    parent: str | None = None
    is_visible: bool = True
    is_solo: bool = False
    existing_policy: ExistingPolicy = "ERROR"


class CollectionRename(_StrictModel):
    operation: Literal["RENAME"] = "RENAME"
    name: str = Field(min_length=1, max_length=63)
    new_name: str = Field(min_length=1, max_length=63)


class CollectionConfigure(_StrictModel):
    operation: Literal["CONFIGURE"] = "CONFIGURE"
    name: str = Field(min_length=1, max_length=63)
    parent: str | None = None
    clear_parent: bool = False
    is_visible: bool | None = None
    is_solo: bool | None = None
    position: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_parent(self) -> "CollectionConfigure":
        if self.parent is not None and self.clear_parent:
            raise ValueError("parent and clear_parent are mutually exclusive")
        return self


class CollectionAssign(_StrictModel):
    operation: Literal["ASSIGN"] = "ASSIGN"
    name: str = Field(min_length=1, max_length=63)
    bone_names: Annotated[list[str], Field(min_length=1, max_length=500)]
    replace_memberships: bool = False
    confirm_destructive: bool = False

    @model_validator(mode="after")
    def validate_replacement(self) -> "CollectionAssign":
        if self.replace_memberships and not self.confirm_destructive:
            raise ValueError("confirm_destructive=True is required when replace_memberships=True")
        return self


class CollectionUnassign(_StrictModel):
    operation: Literal["UNASSIGN"] = "UNASSIGN"
    name: str = Field(min_length=1, max_length=63)
    bone_names: Annotated[list[str], Field(min_length=1, max_length=500)]
    confirm_destructive: bool = False

    @model_validator(mode="after")
    def validate_removal(self) -> "CollectionUnassign":
        if not self.confirm_destructive:
            raise ValueError("confirm_destructive=True is required to unassign bone memberships")
        return self


class CollectionRemove(_StrictModel):
    operation: Literal["REMOVE"] = "REMOVE"
    name: str = Field(min_length=1, max_length=63)
    confirm_destructive: bool = False

    @model_validator(mode="after")
    def validate_removal(self) -> "CollectionRemove":
        if not self.confirm_destructive:
            raise ValueError("confirm_destructive=True is required to remove a bone collection")
        return self


CollectionOperation = Annotated[
    CollectionCreate
    | CollectionRename
    | CollectionConfigure
    | CollectionAssign
    | CollectionUnassign
    | CollectionRemove,
    Field(discriminator="operation"),
]


class BoneBehaviorPatch(_StrictModel):
    bone_name: str = Field(min_length=1, max_length=63)
    use_deform: bool | None = None
    use_inherit_rotation: bool | None = None
    inherit_scale: Literal["FULL", "FIX_SHEAR", "ALIGNED", "AVERAGE", "NONE", "NONE_LEGACY"] | None = None
    use_local_location: bool | None = None
    use_relative_parent: bool | None = None
    use_envelope_multiply: bool | None = None
    envelope_distance: float | None = Field(default=None, ge=0.0)
    envelope_weight: float | None = Field(default=None, ge=0.0)
    head_radius: float | None = Field(default=None, ge=0.0)
    tail_radius: float | None = Field(default=None, ge=0.0)


class PoseBoneBehaviorPatch(_StrictModel):
    bone_name: str = Field(min_length=1, max_length=63)
    rotation_mode: Literal["QUATERNION", "XYZ", "XZY", "YXZ", "YZX", "ZXY", "ZYX", "AXIS_ANGLE"] | None = None
    lock_location: tuple[bool, bool, bool] | None = None
    lock_rotation: tuple[bool, bool, bool] | None = None
    lock_rotation_w: bool | None = None
    lock_rotations_4d: bool | None = None
    lock_scale: tuple[bool, bool, bool] | None = None
    lock_ik_x: bool | None = None
    lock_ik_y: bool | None = None
    lock_ik_z: bool | None = None
    use_ik_limit_x: bool | None = None
    use_ik_limit_y: bool | None = None
    use_ik_limit_z: bool | None = None
    ik_min_x: float | None = None
    ik_max_x: float | None = None
    ik_min_y: float | None = None
    ik_max_y: float | None = None
    ik_min_z: float | None = None
    ik_max_z: float | None = None
    ik_stiffness_x: float | None = Field(default=None, ge=0.0, le=0.99)
    ik_stiffness_y: float | None = Field(default=None, ge=0.0, le=0.99)
    ik_stiffness_z: float | None = Field(default=None, ge=0.0, le=0.99)
    ik_stretch: float | None = Field(default=None, ge=0.0)
    custom_properties: dict[str, bool | int | float | str] | None = None

    @model_validator(mode="after")
    def validate_ik_limits(self) -> "PoseBoneBehaviorPatch":
        for axis in "xyz":
            low = getattr(self, f"ik_min_{axis}")
            high = getattr(self, f"ik_max_{axis}")
            if low is not None and high is not None and low > high:
                raise ValueError(f"ik_min_{axis} must not exceed ik_max_{axis}")
        return self


class SkinWeightAssignment(_StrictModel):
    mesh_object_name: str = Field(min_length=1)
    group_name: str = Field(min_length=1, max_length=63)
    vertex_indices: Annotated[list[int], Field(min_length=1, max_length=100_000)]
    weight: float = Field(ge=0.0, le=1.0)
    mode: Literal["REPLACE", "ADD", "SUBTRACT"] = "REPLACE"
    create_missing_group: bool = False

    @model_validator(mode="after")
    def validate_indices(self) -> "SkinWeightAssignment":
        if any(index < 0 for index in self.vertex_indices):
            raise ValueError("vertex indices must be non-negative")
        if len(set(self.vertex_indices)) != len(self.vertex_indices):
            raise ValueError("vertex indices must be unique within an assignment")
        return self


class NormalizedVertexWeights(_StrictModel):
    mesh_object_name: str = Field(min_length=1)
    vertex_index: int = Field(ge=0)
    weights: dict[str, float] = Field(min_length=1, max_length=256)
    create_missing_groups: bool = False

    @model_validator(mode="after")
    def validate_weights(self) -> "NormalizedVertexWeights":
        if any(not name for name in self.weights):
            raise ValueError("weight group names must be non-empty")
        if any(weight < 0 or weight > 1 for weight in self.weights.values()):
            raise ValueError("normalized weights must be in [0, 1]")
        if abs(sum(self.weights.values()) - 1.0) > 1e-6:
            raise ValueError("normalized vertex weights must sum to 1")
        return self


class ConstraintBase(_StrictModel):
    name: str = Field(min_length=1, max_length=63)
    target_object_name: str | None = None
    subtarget: str | None = None
    influence: float = Field(default=1.0, ge=0.0, le=1.0)
    owner_space: ConstraintSpace = "WORLD"
    target_space: ConstraintSpace = "WORLD"
    stack_index: int | None = Field(default=None, ge=0)
    existing_policy: ExistingPolicy = "ERROR"


class IKConstraintSpec(ConstraintBase):
    type: Literal["IK"] = "IK"
    pole_target_object_name: str | None = None
    pole_subtarget: str | None = None
    pole_angle: float = 0.0
    chain_count: int = Field(default=0, ge=0, le=255)
    iterations: int = Field(default=500, ge=0, le=10_000)
    use_tail: bool = True
    use_stretch: bool = True
    weight: float = Field(default=1.0, ge=0.0, le=1.0)
    orient_weight: float = Field(default=1.0, ge=0.0, le=1.0)


class SplineIKConstraintSpec(ConstraintBase):
    type: Literal["SPLINE_IK"] = "SPLINE_IK"
    chain_count: int = Field(default=0, ge=0, le=255)
    use_even_divisions: bool = False
    use_chain_offset: bool = False
    y_scale_mode: Literal["NONE", "FIT_CURVE", "BONE_ORIGINAL"] = "FIT_CURVE"
    xz_scale_mode: Literal["NONE", "BONE_ORIGINAL", "INVERSE_PRESERVE", "VOLUME_PRESERVE"] = "NONE"
    use_original_scale: bool = False
    bulge: float = 1.0

    @model_validator(mode="after")
    def validate_curve_target(self) -> "SplineIKConstraintSpec":
        if self.subtarget is not None:
            raise ValueError("SPLINE_IK targets a curve object and does not accept subtarget")
        return self


class CopyTransformsConstraintSpec(ConstraintBase):
    type: Literal["COPY_TRANSFORMS"] = "COPY_TRANSFORMS"
    mix_mode: Literal["REPLACE", "BEFORE_FULL", "BEFORE", "BEFORE_SPLIT", "AFTER_FULL", "AFTER", "AFTER_SPLIT"] = (
        "REPLACE"
    )
    remove_target_shear: bool = False
    head_tail: float = Field(default=0.0, ge=0.0, le=1.0)


class CopyLocationConstraintSpec(ConstraintBase):
    type: Literal["COPY_LOCATION"] = "COPY_LOCATION"
    use_x: bool = True
    use_y: bool = True
    use_z: bool = True
    invert_x: bool = False
    invert_y: bool = False
    invert_z: bool = False
    use_offset: bool = False
    head_tail: float = Field(default=0.0, ge=0.0, le=1.0)


class CopyRotationConstraintSpec(ConstraintBase):
    type: Literal["COPY_ROTATION"] = "COPY_ROTATION"
    use_x: bool = True
    use_y: bool = True
    use_z: bool = True
    invert_x: bool = False
    invert_y: bool = False
    invert_z: bool = False
    mix_mode: Literal["REPLACE", "OFFSET", "ADD", "BEFORE", "AFTER"] = "REPLACE"
    euler_order: Literal["AUTO", "XYZ", "XZY", "YXZ", "YZX", "ZXY", "ZYX"] = "AUTO"


class CopyScaleConstraintSpec(ConstraintBase):
    type: Literal["COPY_SCALE"] = "COPY_SCALE"
    use_x: bool = True
    use_y: bool = True
    use_z: bool = True
    power: float = 1.0
    use_make_uniform: bool = False
    use_offset: bool = False
    use_add: bool = False


class ChildOfConstraintSpec(ConstraintBase):
    type: Literal["CHILD_OF"] = "CHILD_OF"
    preserve_pose: bool = False
    use_location_x: bool = True
    use_location_y: bool = True
    use_location_z: bool = True
    use_rotation_x: bool = True
    use_rotation_y: bool = True
    use_rotation_z: bool = True
    use_scale_x: bool = True
    use_scale_y: bool = True
    use_scale_z: bool = True


class DampedTrackConstraintSpec(ConstraintBase):
    type: Literal["DAMPED_TRACK"] = "DAMPED_TRACK"
    track_axis: Literal["TRACK_X", "TRACK_Y", "TRACK_Z", "TRACK_NEGATIVE_X", "TRACK_NEGATIVE_Y", "TRACK_NEGATIVE_Z"] = (
        "TRACK_Y"
    )
    head_tail: float = Field(default=0.0, ge=0.0, le=1.0)


class TrackToConstraintSpec(DampedTrackConstraintSpec):
    type: Literal["TRACK_TO"] = "TRACK_TO"
    up_axis: Literal["UP_X", "UP_Y", "UP_Z"] = "UP_Z"


class StretchToConstraintSpec(ConstraintBase):
    type: Literal["STRETCH_TO"] = "STRETCH_TO"
    head_tail: float = Field(default=0.0, ge=0.0, le=1.0)
    volume: Literal["VOLUME_XZX", "VOLUME_X", "VOLUME_Z", "NO_VOLUME"] = "VOLUME_XZX"
    keep_axis: Literal["PLANE_X", "PLANE_Z", "SWING_Y"] = "PLANE_X"
    rest_length: float = Field(default=0.0, ge=0.0)
    bulge: float = Field(default=1.0, ge=0.0)


class LimitConstraintSpec(ConstraintBase):
    type: Literal["LIMIT_LOCATION", "LIMIT_ROTATION", "LIMIT_SCALE"]
    use_x: bool = False
    use_y: bool = False
    use_z: bool = False
    min_x: float = 0.0
    max_x: float = 0.0
    min_y: float = 0.0
    max_y: float = 0.0
    min_z: float = 0.0
    max_z: float = 0.0
    use_transform_limit: bool = False

    @model_validator(mode="after")
    def validate_limits(self) -> "LimitConstraintSpec":
        if self.target_object_name is not None or self.subtarget is not None:
            raise ValueError(f"{self.type} does not accept a target")
        for axis in "xyz":
            if getattr(self, f"use_{axis}") and getattr(self, f"min_{axis}") > getattr(self, f"max_{axis}"):
                raise ValueError(f"min_{axis} must not exceed max_{axis}")
        return self


class TransformConstraintSpec(ConstraintBase):
    type: Literal["TRANSFORM"] = "TRANSFORM"
    map_from: Literal["LOCATION", "ROTATION", "SCALE"] = "LOCATION"
    map_to: Literal["LOCATION", "ROTATION", "SCALE"] = "LOCATION"
    map_to_x_from: Literal["X", "Y", "Z"] = "X"
    map_to_y_from: Literal["X", "Y", "Z"] = "Y"
    map_to_z_from: Literal["X", "Y", "Z"] = "Z"
    from_min: Vector3 = (0.0, 0.0, 0.0)
    from_max: Vector3 = (1.0, 1.0, 1.0)
    to_min: Vector3 = (0.0, 0.0, 0.0)
    to_max: Vector3 = (1.0, 1.0, 1.0)
    use_motion_extrapolate: bool = False


class ActionConstraintSpec(ConstraintBase):
    type: Literal["ACTION"] = "ACTION"
    action_name: str = Field(min_length=1)
    action_slot_identifier: str | None = Field(default=None, min_length=1)
    transform_channel: Literal[
        "LOCATION_X",
        "LOCATION_Y",
        "LOCATION_Z",
        "ROTATION_X",
        "ROTATION_Y",
        "ROTATION_Z",
        "SCALE_X",
        "SCALE_Y",
        "SCALE_Z",
    ] = "ROTATION_X"
    frame_start: int
    frame_end: int
    min: float = 0.0
    max: float = 1.0
    mix_mode: Literal["REPLACE", "BEFORE_FULL", "BEFORE", "BEFORE_SPLIT", "AFTER_FULL", "AFTER", "AFTER_SPLIT"] = (
        "AFTER_FULL"
    )

    @model_validator(mode="after")
    def validate_frames(self) -> "ActionConstraintSpec":
        if self.frame_start >= self.frame_end:
            raise ValueError("frame_start must be less than frame_end")
        if self.min >= self.max:
            raise ValueError("min must be less than max")
        return self


PoseConstraintSpec = Annotated[
    IKConstraintSpec
    | SplineIKConstraintSpec
    | CopyTransformsConstraintSpec
    | CopyLocationConstraintSpec
    | CopyRotationConstraintSpec
    | CopyScaleConstraintSpec
    | ChildOfConstraintSpec
    | DampedTrackConstraintSpec
    | TrackToConstraintSpec
    | StretchToConstraintSpec
    | LimitConstraintSpec
    | TransformConstraintSpec
    | ActionConstraintSpec,
    Field(discriminator="type"),
]

# Validated inside add_pose_bone_constraint rather than declared as that tool's parameter
# type: exposing PoseConstraintSpec directly would serialize this whole 13-variant
# discriminated union into that one tool's advertised JSON schema on every client
# connection, at a cost of several thousand tokens for detail most calls never touch.
_pose_constraint_adapter = TypeAdapter(PoseConstraintSpec)


def _models(items: Sequence[BaseModel]) -> list[dict]:
    return [item.model_dump(exclude_none=True) for item in items]


def _call(command: str, params: dict, changed_objects: list[str] | None = None) -> dict:
    package = sys.modules.get(__package__) if __package__ is not None else None
    package_call = getattr(package, "_call", None) if package is not None else None
    if package_call is not None and package_call is not _call:
        return package_call(command, params, changed_objects)
    try:
        result = get_blender_connection().send_command(command, params)
        changed = result.get("changed_objects", changed_objects or []) if isinstance(result, dict) else changed_objects
        resources = result.get("changed_resources", []) if isinstance(result, dict) else []
        if isinstance(result, dict):
            result = {
                key: value for key, value in result.items() if key not in {"changed_objects", "changed_resources"}
            }
        return ok(result, changed_objects=changed or [], changed_resources=resources)
    except Exception as exc:
        logger.error("Error running %s: %s", command, exc)
        raise ToolError(f"Error running {command}: {exc}") from exc


@mcp.tool()
async def get_character_rig_info(
    ctx: Context,
    armature_object_name: str,
    bone_limit: Annotated[int, Field(ge=1, le=500)] = 100,
    bone_offset: Annotated[int, Field(ge=0, le=99_999)] = 0,
    dependency_limit: Annotated[int, Field(ge=1, le=500)] = 100,
    dependency_offset: Annotated[int, Field(ge=0, le=99_999)] = 0,
    include_custom_properties: bool = True,
) -> dict:
    """
    Inspect a rig without changing it.

    Rest coordinates are armature-local; pose records include armature-space and world-space matrices.
    """
    return await asyncio.to_thread(
        _call,
        "get_character_rig_info",
        {
            "armature_object_name": armature_object_name,
            "bone_limit": bone_limit,
            "bone_offset": bone_offset,
            "dependency_limit": dependency_limit,
            "dependency_offset": dependency_offset,
            "include_custom_properties": include_custom_properties,
        },
    )


@mcp.tool()
async def get_skinning_info(
    ctx: Context,
    armature_object_name: str,
    mesh_object_names: Annotated[list[str], Field(min_length=1, max_length=200)] | None = None,
    influence_limit: Annotated[int, Field(ge=1, le=64)] = 4,
    normalization_tolerance: Annotated[float, Field(ge=0.0, le=1.0)] = 1e-4,
    weight_epsilon: Annotated[float, Field(ge=0.0, le=1.0)] = 1e-6,
    membership_limit: Annotated[int, Field(ge=1, le=2_000)] = 500,
    membership_offset: Annotated[int, Field(ge=0, le=9_999_999)] = 0,
) -> dict:
    """Inspect base-mesh vertex groups and weight quality without evaluating or changing pose/frame state."""
    return await asyncio.to_thread(
        _call,
        "get_skinning_info",
        {
            "armature_object_name": armature_object_name,
            "mesh_object_names": mesh_object_names,
            "influence_limit": influence_limit,
            "normalization_tolerance": normalization_tolerance,
            "weight_epsilon": weight_epsilon,
            "membership_limit": membership_limit,
            "membership_offset": membership_offset,
        },
    )


@mcp.tool()
async def create_armature(
    ctx: Context,
    name: Annotated[str, Field(min_length=1, max_length=63)],
    collection_name: Annotated[str, Field(min_length=1, max_length=63)],
    bones: Annotated[list[InitialBone], Field(max_length=1_000)] | None = None,
    world_transform: RigWorldTransform | None = None,
    display: ArmatureDisplaySettings | None = None,
) -> dict:
    """
    Create a collision-safe armature and one fully prevalidated initial hierarchy.

    collection_name is looked up by name and reused if it already exists in the scene, or
    created if it does not - it is never required to pre-exist.
    """
    return await asyncio.to_thread(
        _call,
        "create_armature",
        {
            "name": name,
            "collection_name": collection_name,
            "bones": _models(bones or []),
            "world_transform": (world_transform or RigWorldTransform()).model_dump(),
            "display": (display or ArmatureDisplaySettings()).model_dump(),
        },
        [name],
    )


@mcp.tool()
async def patch_armature_bones(
    ctx: Context,
    armature_object_name: str,
    operations: Annotated[list[BoneOperation], Field(min_length=1, max_length=1_000)],
    confirm_animated_rest_changes: bool = False,
) -> dict:
    """Atomically patch rest bones after validating the complete resulting hierarchy and dependency policy."""
    return await asyncio.to_thread(
        _call,
        "patch_armature_bones",
        {
            "armature_object_name": armature_object_name,
            "operations": _models(operations),
            "confirm_animated_rest_changes": confirm_animated_rest_changes,
        },
        [armature_object_name],
    )


@mcp.tool()
async def mirror_armature_bones(
    ctx: Context,
    armature_object_name: str,
    bone_names: Annotated[list[str], Field(min_length=1, max_length=500)],
    axis: Literal["X", "Y", "Z"] = "X",
    source_token: str = ".L",
    target_token: str = ".R",
    mirror_constraints: bool = False,
) -> dict:
    """Mirror explicit rest bones in armature space with deterministic name and hierarchy remapping."""
    return await asyncio.to_thread(
        _call,
        "mirror_armature_bones",
        {
            "armature_object_name": armature_object_name,
            "bone_names": bone_names,
            "axis": axis,
            "source_token": source_token,
            "target_token": target_token,
            "mirror_constraints": mirror_constraints,
        },
        [armature_object_name],
    )


@mcp.tool()
async def manage_bone_collections(
    ctx: Context,
    armature_object_name: str,
    operations: Annotated[list[CollectionOperation], Field(min_length=1, max_length=500)],
) -> dict:
    """Batch-manage Blender 5.1 bone collections while preserving multi-collection membership by default."""
    return await asyncio.to_thread(
        _call,
        "manage_bone_collections",
        {"armature_object_name": armature_object_name, "operations": _models(operations)},
        [armature_object_name],
    )


@mcp.tool()
async def configure_armature_bones(
    ctx: Context,
    armature_object_name: str,
    bone_patches: Annotated[list[BoneBehaviorPatch], Field(max_length=1_000)] | None = None,
    pose_bone_patches: Annotated[list[PoseBoneBehaviorPatch], Field(max_length=1_000)] | None = None,
) -> dict:
    """Patch allowlisted non-geometric Bone and PoseBone settings after complete preflight validation."""
    if not bone_patches and not pose_bone_patches:
        raise ToolError("At least one bone or pose-bone patch is required")
    return await asyncio.to_thread(
        _call,
        "configure_armature_bones",
        {
            "armature_object_name": armature_object_name,
            "bone_patches": _models(bone_patches or []),
            "pose_bone_patches": _models(pose_bone_patches or []),
        },
        [armature_object_name],
    )


@mcp.tool()
async def bind_mesh_to_armature(
    ctx: Context,
    armature_object_name: str,
    mesh_object_names: Annotated[list[str], Field(min_length=1, max_length=200)],
    method: Literal["EMPTY_GROUPS", "AUTOMATIC", "ENVELOPES", "EXISTING_WEIGHTS"] = "EMPTY_GROUPS",
    modifier_name: str = "Armature",
    existing_modifier_policy: Literal["ERROR", "REUSE"] = "REUSE",
    parent_meshes: bool = False,
    preserve_volume: bool = False,
    modifier_index: int | None = Field(default=None, ge=0),
    replacement_policy: Literal["PRESERVE", "REPLACE"] = "PRESERVE",
    confirm_replace_weights: bool = False,
) -> dict:
    """
    Bind explicit meshes using groups, automatic weights, envelopes, or existing weights with rollback on failure.

    armature_object_name must already reference an object of type ARMATURE.
    """
    if replacement_policy == "REPLACE" and not confirm_replace_weights:
        raise ToolError("confirm_replace_weights=True is required when replacement_policy='REPLACE'")
    return await asyncio.to_thread(
        _call,
        "bind_mesh_to_armature",
        {
            "armature_object_name": armature_object_name,
            "mesh_object_names": mesh_object_names,
            "method": method,
            "modifier_name": modifier_name,
            "existing_modifier_policy": existing_modifier_policy,
            "parent_meshes": parent_meshes,
            "preserve_volume": preserve_volume,
            "modifier_index": modifier_index,
            "replacement_policy": replacement_policy,
            "confirm_replace_weights": confirm_replace_weights,
        },
        [armature_object_name, *mesh_object_names],
    )


@mcp.tool()
async def set_skin_weights(
    ctx: Context,
    assignments: Annotated[list[SkinWeightAssignment], Field(max_length=2_000)] | None = None,
    normalized_vertices: Annotated[list[NormalizedVertexWeights], Field(max_length=10_000)] | None = None,
) -> dict:
    """Set deterministic base-mesh weights; topology indices must be refreshed after topology edits."""
    if not assignments and not normalized_vertices:
        raise ToolError("At least one assignment or normalized vertex payload is required")
    changed = sorted({item.mesh_object_name for item in [*(assignments or []), *(normalized_vertices or [])]})
    return await asyncio.to_thread(
        _call,
        "set_skin_weights",
        {"assignments": _models(assignments or []), "normalized_vertices": _models(normalized_vertices or [])},
        changed,
    )


@mcp.tool()
async def clean_skin_weights(
    ctx: Context,
    mesh_object_name: str,
    armature_object_name: str | None = None,
    vertex_indices: Annotated[list[int], Field(min_length=1, max_length=100_000)] | None = None,
    threshold: Annotated[float, Field(ge=0.0, le=1.0)] = 1e-4,
    influence_limit: Annotated[int | None, Field(ge=1, le=64)] = 4,
    normalize: Literal["NONE", "ALL", "DEFORM"] = "DEFORM",
    protected_group_names: Annotated[list[str], Field(min_length=1, max_length=500)] | None = None,
    remove_orphan_groups: bool = False,
    confirm_remove_orphan_groups: bool = False,
) -> dict:
    """Precompute and apply stable weight cleanup while preserving locked and protected groups."""
    if remove_orphan_groups and not confirm_remove_orphan_groups:
        raise ToolError("confirm_remove_orphan_groups=True is required to remove orphan groups")
    return await asyncio.to_thread(
        _call,
        "clean_skin_weights",
        {
            "mesh_object_name": mesh_object_name,
            "armature_object_name": armature_object_name,
            "vertex_indices": vertex_indices,
            "threshold": threshold,
            "influence_limit": influence_limit,
            "normalize": normalize,
            "protected_group_names": protected_group_names,
            "remove_orphan_groups": remove_orphan_groups,
            "confirm_remove_orphan_groups": confirm_remove_orphan_groups,
        },
        [mesh_object_name],
    )


@mcp.tool()
async def add_pose_bone_constraint(
    ctx: Context,
    armature_object_name: str,
    bone_name: str,
    constraint: dict[str, Any],
) -> dict:
    """
    Create or update one typed pose-bone constraint after validating targets and dependency cycles.

    bone_name must already exist as a pose bone on armature_object_name; this tool cannot create bones.
    constraint is a discriminated-union object keyed by "type": IK, SPLINE_IK, COPY_TRANSFORMS,
    COPY_LOCATION, COPY_ROTATION, COPY_SCALE, CHILD_OF, DAMPED_TRACK, TRACK_TO, STRETCH_TO,
    LIMIT_LOCATION, LIMIT_ROTATION, LIMIT_SCALE, TRANSFORM, or ACTION - each with its own additional
    fields, validated against Blender's real constraint schema before this call reaches Blender.
    """
    validated = _pose_constraint_adapter.validate_python(constraint)
    return await asyncio.to_thread(
        _call,
        "add_pose_bone_constraint",
        {
            "armature_object_name": armature_object_name,
            "bone_name": bone_name,
            "constraint": validated.model_dump(exclude_none=True),
        },
        [armature_object_name],
    )


@mcp.tool()
async def validate_character_rig(
    ctx: Context,
    armature_object_names: Annotated[list[str], Field(min_length=1, max_length=200)] | None = None,
    mesh_object_names: Annotated[list[str], Field(min_length=1, max_length=200)] | None = None,
    frames: Annotated[list[int], Field(max_length=50)] | None = None,
    influence_limit: Annotated[int, Field(ge=1, le=64)] = 4,
    normalization_tolerance: Annotated[float, Field(ge=0.0, le=1.0)] = 1e-4,
    issue_limit: Annotated[int, Field(ge=1, le=2_000)] = 500,
    issue_offset: Annotated[int, Field(ge=0, le=99_999)] = 0,
) -> dict:
    """Run a bounded, non-mutating structural preflight; this does not certify artistic deformation quality."""
    return await asyncio.to_thread(
        _call,
        "validate_character_rig",
        {
            "armature_object_names": armature_object_names,
            "mesh_object_names": mesh_object_names,
            "frames": frames,
            "influence_limit": influence_limit,
            "normalization_tolerance": normalization_tolerance,
            "issue_limit": issue_limit,
            "issue_offset": issue_offset,
        },
    )
