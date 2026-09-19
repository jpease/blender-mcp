"""Typed tool for generic object transform keyframing (location/rotation/scale, local or world space)."""

import asyncio

from typing import Annotated, Literal

from mcp.server.fastmcp import Context
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..app import mcp
from ..connection import get_blender_connection
from .envelope import ok

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
    resources = changed_resources or []
    if isinstance(result, dict):
        result = dict(result)
        resources = result.pop("changed_resources", resources)
    return ok(result, changed_resources=resources)


@mcp.tool()
async def keyframe_object_transform(
    ctx: Context,
    keyframes: Annotated[list[ObjectTransformKeyframe], Field(min_length=1, max_length=500)],
    policy: Literal["INSERT_ONLY", "REPLACE_EXISTING"] = "REPLACE_EXISTING",
    interpolation: Literal["CONSTANT", "LINEAR", "BEZIER"] = "BEZIER",
    handle_left: Literal["FREE", "ALIGNED", "VECTOR", "AUTO", "AUTO_CLAMPED"] = "AUTO_CLAMPED",
    handle_right: Literal["FREE", "ALIGNED", "VECTOR", "AUTO", "AUTO_CLAMPED"] = "AUTO_CLAMPED",
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
    """
    return await _call(
        "keyframe_object_transform",
        {
            "keyframes": [record.model_dump(exclude_none=True) for record in keyframes],
            "policy": policy,
            "interpolation": interpolation,
            "handle_left": handle_left,
            "handle_right": handle_right,
        },
        changed_resources=list(dict.fromkeys(record.object_name for record in keyframes)),
    )
