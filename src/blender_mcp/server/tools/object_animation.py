"""Typed tool for generic object transform keyframing (location/rotation/scale, local or world space)."""

import asyncio

from typing import Annotated, Literal

from mcp.server.fastmcp import Context
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..app import mcp
from ..connection import get_blender_connection
from .envelope import envelope_for
from .key_style import HandleType, Interpolation

_MAX_FRAME = 1_048_574


class ObjectTransformKeyframe(BaseModel):
    """One object's location/rotation/scale key at a single frame or seconds offset."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    object_name: Annotated[str, Field(min_length=1)]
    scene_name: Annotated[str | None, Field(min_length=1)] = None
    frame: Annotated[float | None, Field(ge=-_MAX_FRAME, le=_MAX_FRAME)] = None
    at_seconds: float | None = None
    space: Literal["LOCAL", "WORLD"] = "WORLD"
    location: tuple[float, float, float] | None = None
    rotation_euler: tuple[float, float, float] | None = None
    rotation_quaternion: tuple[float, float, float, float] | None = None
    scale: tuple[float, float, float] | None = None

    @model_validator(mode="after")
    def validate_record(self) -> "ObjectTransformKeyframe":
        """Require exactly one time reference, at most one rotation channel, and at least one keyed channel."""
        if (self.frame is None) == (self.at_seconds is None):
            raise ValueError("supply exactly one of frame or at_seconds")
        if self.rotation_euler is not None and self.rotation_quaternion is not None:
            raise ValueError("supply rotation_euler or rotation_quaternion, not both")
        if not any(
            value is not None for value in (self.location, self.rotation_euler, self.rotation_quaternion, self.scale)
        ):
            raise ValueError("supply at least one of location, rotation_euler, rotation_quaternion, or scale")
        return self


async def _call(command: str, params: dict, *, changed_resources: list[str] | None = None) -> dict:
    result = await asyncio.to_thread(get_blender_connection().send_command, command, params)
    return envelope_for(result, changed_resources=changed_resources or ())


@mcp.tool()
async def keyframe_object_transform(
    ctx: Context,
    keyframes: Annotated[list[ObjectTransformKeyframe], Field(min_length=1, max_length=500)],
    policy: Literal["INSERT_ONLY", "REPLACE_EXISTING"] = "REPLACE_EXISTING",
    interpolation: Interpolation = "BEZIER",
    handle_left: HandleType = "AUTO_CLAMPED",
    handle_right: HandleType = "AUTO_CLAMPED",
    action_name: Annotated[str, Field(min_length=1, max_length=63)] | None = None,
    action_policy: Literal["ENSURE", "CREATE", "REUSE"] = "ENSURE",
    action_slot_identifier: str | None = None,
    confirm_displace_action: bool = False,
) -> dict:
    """
    Keyframe one or more objects' location/rotation/scale, in local or world space, at a frame or seconds offset.

    Combine every channel for one object at one frame into a single record (location, rotation, and/or scale
    together) rather than separate records - each (object_name, frame) pair may appear only once per call.
    WORLD space solves the requested location/rotation/scale through the object's current parent chain by
    assigning matrix_world directly (Blender resolves the parent inverse), then keys the resulting local
    values, so a child of an animated rig can be keyed at an absolute world pose without solving parenting
    yourself; omitted channels keep their current world value. LOCAL space sets the given channels directly.
    Rotation must match the object's current rotation_mode - rotation_quaternion when rotation_mode is
    QUATERNION, otherwise rotation_euler; AXIS_ANGLE objects are rejected (use edit_keyframes instead).
    rotation_mode itself is never changed. Convert seconds to a frame via at_seconds using the target scene's
    fps and frame_start (see get_scene_physics_info / configure_scene_physics) instead of supplying frame.

    Without action_name the keys land in whatever action already drives each object, so which action holds
    the shot's motion depends on call order. Name one and they are written there: action_policy="ENSURE"
    (default) creates it when missing and reuses it when present, "CREATE" requires it to be new, "REUSE"
    requires it to exist, and action_slot_identifier picks the slot when the action carries several. An
    object holds one action, so a batch naming action_name may name only one object, and displacing a
    different action that already holds keys is refused unless confirm_displace_action=True: those keys
    would stop driving anything and Blender drops an unreferenced action at save. Give a character's root
    motion (here) and its pose (keyframe_character_pose) the same action_name and both play back together.
    """
    if action_name is not None and len({record.object_name for record in keyframes}) > 1:
        raise ValueError(
            f"action_name='{action_name}' names one action, but this batch keys several objects: an object "
            "holds one action, so call keyframe_object_transform once per object"
        )
    return await _call(
        "keyframe_object_transform",
        {
            "keyframes": [record.model_dump(exclude_none=True) for record in keyframes],
            "policy": policy,
            "interpolation": interpolation,
            "handle_left": handle_left,
            "handle_right": handle_right,
            "action_name": action_name,
            "action_policy": action_policy,
            "action_slot_identifier": action_slot_identifier,
            "confirm_displace_action": confirm_displace_action,
        },
        changed_resources=list(dict.fromkeys(record.object_name for record in keyframes)),
    )
