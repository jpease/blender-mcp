"""Rigid-body character proxy construction and armature delivery tools."""

import asyncio

from typing import Annotated, Any, Literal

from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .inspection_and_setup import Vector3, _call, mcp, rigid_body_constraint_adapter


class RagdollBodySpec(BaseModel):
    """Collision shape and mass weighting for one mapped armature bone."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    bone_name: str = Field(min_length=1)
    proxy_name: str | None = None
    shape: Literal["CAPSULE", "BOX", "CONVEX_HULL"] = "CAPSULE"
    radius: float | None = Field(default=None, gt=0.0)
    length_scale: float = Field(default=0.9, gt=0.0, le=2.0)
    mass_weight: float = Field(default=1.0, gt=0.0)
    convex_source_object_name: str | None = None

    @model_validator(mode="after")
    def validate_shape_source(self) -> "RagdollBodySpec":
        if (self.shape == "CONVEX_HULL") != (self.convex_source_object_name is not None):
            raise ValueError("convex_source_object_name is required only for CONVEX_HULL")
        return self


class RagdollJointSpec(BaseModel):
    """An explicit anatomical joint with reviewed rigid-body limits."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    parent_bone_name: str = Field(min_length=1)
    child_bone_name: str = Field(min_length=1)
    # Untyped and validated below, so the whole constraint union stays out of
    # create_ragdoll_rig's advertised schema.
    configuration: dict[str, Any]
    constraint_name: str | None = None
    axis: Vector3 | None = None

    @model_validator(mode="after")
    def validate_joint(self) -> "RagdollJointSpec":
        if self.parent_bone_name == self.child_bone_name:
            raise ValueError("Ragdoll joint endpoints must be distinct")
        if self.axis is not None and sum(component * component for component in self.axis) <= 1e-16:
            raise ValueError("Ragdoll joint axis must be non-zero")
        return self

    @model_validator(mode="after")
    def validate_configuration(self) -> "RagdollJointSpec":
        """
        Validate and normalize the untyped configuration dict against RigidBodyConstraintSpec.

        Returns:
            RagdollJointSpec: This instance, with configuration replaced by its validated form.

        """
        validated = rigid_body_constraint_adapter.validate_python(self.configuration)
        self.configuration = validated.model_dump(exclude_none=True, exclude_unset=True)
        return self


class RagdollBakeMapping(BaseModel):
    """Map one simulated proxy back to one pose bone."""

    model_config = ConfigDict(extra="forbid")
    bone_name: str = Field(min_length=1)
    proxy_object_name: str = Field(min_length=1)


@mcp.tool()
async def create_ragdoll_rig(
    ctx: Context,
    scene_name: str,
    armature_object_name: str,
    rig_name: str,
    bodies: Annotated[list[RagdollBodySpec], Field(min_length=2, max_length=64)],
    joints: Annotated[list[RagdollJointSpec], Field(min_length=1, max_length=128)],
    total_mass: Annotated[float, Field(gt=0.0)],
    proxy_collection_name: str = "Ragdoll Proxies",
    constraint_collection_name: str = "Ragdoll Constraints",
    collision_layers: Annotated[
        list[Annotated[int, Field(ge=1, le=20)]] | None, Field(min_length=1, max_length=20)
    ] = None,
    start_kinematic: bool = True,
    confirm_delete_baked_cache: bool = False,
) -> dict:
    """
    Build reviewed bone proxies and anatomical constraints without altering the source armature.

    Unlike create_rigid_body_proxy_rig (arbitrary mesh objects) or setup_animated_passive_collider
    (a single passive collider), this is specifically for driving simulated ragdoll physics from
    armature_object_name's pose bones: each RagdollBodySpec creates one simulated proxy for a mapped
    bone, and each RagdollJointSpec constrains two mapped bones' proxies together at an anatomical
    joint. start_kinematic=True (default) starts the ragdoll following the armature's existing
    animation until switched dynamic; False lets it fall immediately under gravity from frame 1.
    Requires scene_name to already have a rigid body world. Rejects with
    confirm_delete_baked_cache=False if that world already has a baked simulation cache.
    """
    layers = collision_layers or [4]
    bone_names = [body.bone_name for body in bodies]
    if len(bone_names) != len(set(bone_names)):
        raise ToolError("Ragdoll body bone names must be unique")
    proxy_names = [body.proxy_name for body in bodies if body.proxy_name]
    if len(proxy_names) != len(set(proxy_names)):
        raise ToolError("Explicit ragdoll proxy names must be unique")
    known = set(bone_names)
    pairs = []
    for joint in joints:
        pair = (joint.parent_bone_name, joint.child_bone_name)
        if pair[0] not in known or pair[1] not in known:
            raise ToolError(f"Ragdoll joint references an unmapped bone: {pair}")
        pairs.append(pair)
    if len(pairs) != len(set(pairs)):
        raise ToolError("Ragdoll joint pairs must be unique")
    if len(layers) != len(set(layers)) or any(not 1 <= layer <= 20 for layer in layers):
        raise ToolError("collision_layers must contain unique values in [1, 20]")
    return await asyncio.to_thread(
        _call,
        "create_ragdoll_rig",
        {
            "scene_name": scene_name,
            "armature_object_name": armature_object_name,
            "rig_name": rig_name,
            "bodies": [body.model_dump(exclude_none=True) for body in bodies],
            "joints": [joint.model_dump(exclude_none=True) for joint in joints],
            "total_mass": total_mass,
            "proxy_collection_name": proxy_collection_name,
            "constraint_collection_name": constraint_collection_name,
            "collision_layers": layers,
            "start_kinematic": start_kinematic,
            "confirm_delete_baked_cache": confirm_delete_baked_cache,
        },
        [
            armature_object_name,
            *[body.convex_source_object_name for body in bodies if body.convex_source_object_name],
        ],
    )


@mcp.tool()
async def bake_ragdoll_to_armature(
    ctx: Context,
    scene_name: str,
    armature_object_name: str,
    mappings: Annotated[list[RagdollBakeMapping], Field(min_length=1, max_length=64)],
    frame_start: Annotated[int, Field(ge=-1_000_000, le=1_000_000)],
    frame_end: Annotated[int, Field(ge=-1_000_000, le=1_000_000)],
    frame_step: Annotated[int, Field(ge=1, le=120)] = 1,
    action_name: str = "Ragdoll Bake",
    blend_in_frames: Annotated[int, Field(ge=0, le=1000)] = 0,
    blend_out_frames: Annotated[int, Field(ge=0, le=1000)] = 0,
    reduce_keys: bool = False,
    position_tolerance: Annotated[float, Field(ge=0.0)] = 0.001,
    angular_tolerance_radians: Annotated[float, Field(ge=0.0, le=3.141592653589793)] = 0.001,
    confirm_overwrite_action: bool = False,
) -> dict:
    """
    Bake proxy world motion to pose-bone quaternion channels in a new preserved animation layer.

    Writes to a new action named action_name rather than modifying the armature's currently
    assigned action; reusing an existing action_name requires confirm_overwrite_action=True.
    blend_in_frames/blend_out_frames ease into and out of the baked range from the armature's prior
    pose so the ragdoll's start/end don't pop. reduce_keys removes redundant keyframes within
    position_tolerance/angular_tolerance_radians after baking.
    """
    if frame_start > frame_end:
        raise ToolError("frame_start must not exceed frame_end")
    bone_names = [mapping.bone_name for mapping in mappings]
    proxy_names = [mapping.proxy_object_name for mapping in mappings]
    if len(bone_names) != len(set(bone_names)) or len(proxy_names) != len(set(proxy_names)):
        raise ToolError("Bone and proxy mappings must each be unique")
    return await asyncio.to_thread(
        _call,
        "bake_ragdoll_to_armature",
        {
            "scene_name": scene_name,
            "armature_object_name": armature_object_name,
            "mappings": [mapping.model_dump() for mapping in mappings],
            "frame_start": frame_start,
            "frame_end": frame_end,
            "frame_step": frame_step,
            "action_name": action_name,
            "blend_in_frames": blend_in_frames,
            "blend_out_frames": blend_out_frames,
            "reduce_keys": reduce_keys,
            "position_tolerance": position_tolerance,
            "angular_tolerance_radians": angular_tolerance_radians,
            "confirm_overwrite_action": confirm_overwrite_action,
        },
        [armature_object_name, *proxy_names],
    )
