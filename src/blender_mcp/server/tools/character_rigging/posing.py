"""Typed tools for deterministic pose application and pose keyframing."""

import asyncio

from typing import Annotated, Literal

from mcp.server.fastmcp import Context
from pydantic import Field, model_validator

from ...app import mcp
from ._shared import _call, _StrictModel


class BonePose(_StrictModel):
    """One bone transform represented in an explicitly selected coordinate space."""

    bone_name: str = Field(min_length=1, max_length=63)
    matrix: (
        tuple[
            tuple[float, float, float, float],
            tuple[float, float, float, float],
            tuple[float, float, float, float],
            tuple[float, float, float, float],
        ]
        | None
    ) = None
    location: tuple[float, float, float] | None = None
    rotation_euler: tuple[float, float, float] | None = None
    rotation_quaternion: tuple[float, float, float, float] | None = None
    rotation_axis_angle: tuple[float, float, float, float] | None = None
    scale: tuple[float, float, float] | None = None
    custom_properties: dict[str, bool | int | float] | None = None

    @model_validator(mode="after")
    def validate_representation(self) -> "BonePose":
        rotations = [self.rotation_euler, self.rotation_quaternion, self.rotation_axis_angle]
        if sum(value is not None for value in rotations) > 1:
            raise ValueError("Supply at most one rotation representation")
        if self.matrix is not None and any(value is not None for value in [self.location, *rotations, self.scale]):
            raise ValueError("matrix is mutually exclusive with location, rotation, and scale")
        if self.matrix is None and not any(
            value is not None for value in [self.location, *rotations, self.scale, self.custom_properties]
        ):
            raise ValueError("A pose entry must change at least one channel")
        if self.rotation_quaternion is not None and sum(value * value for value in self.rotation_quaternion) <= 1e-16:
            raise ValueError("rotation_quaternion must be non-zero")
        if (
            self.rotation_axis_angle is not None
            and sum(value * value for value in self.rotation_axis_angle[1:]) <= 1e-16
        ):
            raise ValueError("rotation_axis_angle axis must be non-zero")
        if self.scale is not None and any(value == 0 for value in self.scale):
            raise ValueError("scale components must be non-zero")
        return self


@mcp.tool()
async def list_character_bones(
    ctx: Context,
    armature_object_name: str,
    limit: Annotated[int, Field(ge=1, le=200)] = 100,
    offset: Annotated[int, Field(ge=0, le=99_999)] = 0,
) -> dict:
    """
    List a rig's bone names, parents, and deform flags so a pose can name real bones.

    No coordinates or transforms are returned; read get_character_rig_info for those.

    Args:
        ctx: MCP request context.
        armature_object_name: An existing object of type ARMATURE; any other object is an error.
            Rest-bone edits still open in Edit Mode are flushed before reading.
        limit: Bones per page. A production rig carries a few hundred bones, so one page rarely
            covers a whole rig.
        offset: Where to resume. Pass the previous reply's next_offset while truncated is true.

    Returns:
        armature_object, and bones with items (name, parent - null for a root - and deform,
        whether the bone deforms a bound mesh), total, offset, limit, truncated, and next_offset
        (null on the last page). Items follow armature bone order, which lists a parent before
        its children.

    """
    return await asyncio.to_thread(
        _call,
        "list_character_bones",
        {
            "armature_object_name": armature_object_name,
            "limit": limit,
            "offset": offset,
        },
    )


@mcp.tool()
async def set_character_pose(
    ctx: Context,
    armature_object_name: str,
    poses: Annotated[list[BonePose], Field(min_length=1, max_length=500)],
    space: Literal["LOCAL", "LOCAL_WITH_PARENT", "POSE", "WORLD"] = "LOCAL",
    reset_unspecified: bool = False,
    confirm_reset_unspecified: bool = False,
    detail: bool = False,
) -> dict:
    """
    Apply explicit bone transforms without inserting animation keys.

    Each pose's bone_name must name an existing pose bone on armature_object_name. Only fields
    explicitly set on a pose entry are sent and changed; bones not named in poses are left as-is
    unless reset_unspecified=True (which requires confirm_reset_unspecified) resets every other
    pose bone to rest. Use keyframe_character_pose instead to record this as animation.

    Args:
        detail: Also report each bone's pre-call pose matrix, and report both matrices at
            Blender's own precision instead of rounded to six decimal places.

    Returns:
        armature_object, space, changed_bones naming every posed bone, and bones with one
        record per posed bone (bone, the channels the call set - a custom property appears as
        its data path - and after_pose_matrix, the armature-space matrix it ended on). A long
        pose shortens bones to fit the reply budget; changed_bones stays complete.

    """
    if reset_unspecified and not confirm_reset_unspecified:
        raise ValueError("confirm_reset_unspecified=True is required to reset unspecified pose bones")
    return await asyncio.to_thread(
        _call,
        "set_character_pose",
        {
            "armature_object_name": armature_object_name,
            "poses": [pose.model_dump(exclude_none=True, exclude_unset=True) for pose in poses],
            "space": space,
            "reset_unspecified": reset_unspecified,
            "confirm_reset_unspecified": confirm_reset_unspecified,
            "detail": detail,
        },
        [armature_object_name],
    )


@mcp.tool()
async def keyframe_character_pose(
    ctx: Context,
    armature_object_name: str,
    action_name: Annotated[str, Field(min_length=1, max_length=63)],
    frame: float,
    poses: Annotated[list[BonePose], Field(min_length=1, max_length=500)],
    space: Literal["LOCAL", "LOCAL_WITH_PARENT", "POSE", "WORLD"] = "LOCAL",
    keying_policy: Literal["INSERT", "REPLACE", "REMOVE"] = "INSERT",
    interpolation: Literal["CONSTANT", "LINEAR", "BEZIER"] = "BEZIER",
    action_policy: Literal["CREATE", "REUSE"] = "CREATE",
    action_slot_identifier: str | None = None,
    detail: bool = False,
) -> dict:
    """
    Apply a pose and insert, replace, or remove exact keys in a named action.

    action_policy="CREATE" (default) requires action_name to not already exist; "REUSE" requires
    it to already exist, and is the only policy keying_policy="REMOVE" accepts. Each pose's
    bone_name must name an existing pose bone on armature_object_name. action_slot_identifier
    selects which of the action's animation slots to key by its `identifier`; it is only required
    when the action already has multiple candidate slots and none is unambiguously suitable
    (inspect the action's slots before assuming this can be omitted). The pose itself is restored
    once the keys are written, so the action holds it and the rig does not.

    Args:
        detail: Also report, as "bones", the pose each bone was keyed at - its pre-call and
            keyed armature-space matrices, at Blender's own precision.

    Returns:
        armature_object, action, action_slot, keying_policy, changed_bones naming every posed
        bone, changed_keys with one entry per keyed channel (bone, data_path, frame), and
        interpolation_updates. A long pose shortens changed_keys to fit the reply budget;
        changed_bones stays complete.

    """
    if keying_policy == "REMOVE" and action_policy != "REUSE":
        raise ValueError("Removing keys requires action_policy='REUSE'")
    return await asyncio.to_thread(
        _call,
        "keyframe_character_pose",
        {
            "armature_object_name": armature_object_name,
            "action_name": action_name,
            "frame": frame,
            "poses": [pose.model_dump(exclude_none=True, exclude_unset=True) for pose in poses],
            "space": space,
            "keying_policy": keying_policy,
            "interpolation": interpolation,
            "action_policy": action_policy,
            "action_slot_identifier": action_slot_identifier,
            "detail": detail,
        },
        [armature_object_name],
    )
