"""Typed tools for camera-rig keyframing and time-based shot effects."""

from typing import Annotated, Literal

from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field, model_validator

from ...app import mcp
from .._dispatch import call_blender
from .._inputs import MAX_FRAME, MIN_FRAME, StrictModel, dump_inputs
from ..key_style import Easing, HandleType, Interpolation
from ._shared import _tool_params

AnimationOwner = Literal["OBJECT", "CAMERA_DATA", "CONSTRAINT", "DOF"]
KeyPolicy = Literal["REPLACE", "INSERT_ONLY"]
FocusPullMode = Literal["DISTANCE", "FOCUS_CONTROL"]
FramingAxis = Literal["HORIZONTAL", "VERTICAL"]


class CameraKeyframe(StrictModel):
    """One allowlisted camera-rig channel value at one frame or seconds offset."""

    object_name: str = Field(min_length=1)
    scene_name: str | None = None
    owner: AnimationOwner = "OBJECT"
    constraint_name: str | None = None
    data_path: str = Field(min_length=1)
    value: float | tuple[float, float, float] | tuple[float, float, float, float]
    frame: int | None = Field(default=None, ge=MIN_FRAME, le=MAX_FRAME)
    at_seconds: float | None = None
    array_index: int | None = Field(default=None, ge=0, le=3)

    @model_validator(mode="after")
    def validate_constraint_owner(self) -> "CameraKeyframe":
        if (self.owner == "CONSTRAINT") != (self.constraint_name is not None):
            raise ValueError("constraint_name is required only for CONSTRAINT keyframes")
        if (self.frame is None) == (self.at_seconds is None):
            raise ValueError("supply exactly one of frame or at_seconds")
        allowed = {
            "OBJECT": {"location", "rotation_euler", "rotation_quaternion", "scale"},
            "CAMERA_DATA": {"lens", "ortho_scale", "shift_x", "shift_y", "clip_start", "clip_end"},
            "DOF": {"focus_distance", "aperture_fstop"},
            "CONSTRAINT": {"influence", "offset_factor"},
        }
        if self.data_path not in allowed[self.owner]:
            raise ValueError(f"data_path '{self.data_path}' is not allowed for {self.owner}")
        return self


@mcp.tool()
async def keyframe_camera_rig(
    ctx: Context,
    keyframes: Annotated[list[CameraKeyframe], Field(min_length=1, max_length=500)],
    policy: KeyPolicy = "REPLACE",
    interpolation: Interpolation = "BEZIER",
    handle_left: HandleType = "AUTO_CLAMPED",
    handle_right: HandleType = "AUTO_CLAMPED",
) -> dict:
    """Set coordinated allowlisted camera-rig channels without touching unrelated keys."""
    payload = dump_inputs(keyframes)
    return await call_blender(
        "keyframe_camera_rig",
        {
            "keyframes": payload,
            "policy": policy,
            "interpolation": interpolation,
            "handle_left": handle_left,
            "handle_right": handle_right,
        },
    )


@mcp.tool()
async def set_camera_interpolation(
    ctx: Context,
    object_name: Annotated[str, Field(min_length=1)],
    owner: Literal["OBJECT", "CAMERA_DATA"],
    data_path: Annotated[str, Field(min_length=1)],
    frame_start: Annotated[int, Field(ge=MIN_FRAME, le=MAX_FRAME)],
    frame_end: Annotated[int, Field(ge=MIN_FRAME, le=MAX_FRAME)],
    array_index: Annotated[int | None, Field(ge=0, le=3)] = None,
    interpolation: Interpolation = "BEZIER",
    handle_left: HandleType = "AUTO_CLAMPED",
    handle_right: HandleType = "AUTO_CLAMPED",
    easing: Easing | None = None,
) -> dict:
    """
    Change interpolation only on one exact channel and inclusive frame interval.

    ``data_path`` must be one of: for ``owner="OBJECT"``, ``location``, ``rotation_euler``,
    ``rotation_quaternion``, or ``scale``; for ``owner="CAMERA_DATA"``, ``lens``, ``ortho_scale``,
    ``shift_x``, ``shift_y``, ``clip_start``, or ``clip_end``. Use ``keyframe_camera_rig`` first to
    author keys on these channels; this tool never creates keyframes itself.
    """
    if frame_start > frame_end:
        raise ToolError("frame_start must be less than or equal to frame_end")
    return await call_blender("set_camera_interpolation", _tool_params(locals()), changed_objects=[object_name])


@mcp.tool()
async def create_focus_pull(
    ctx: Context,
    scene_name: str,
    camera_name: str,
    start_frame: Annotated[int | None, Field(ge=MIN_FRAME, le=MAX_FRAME)] = None,
    end_frame: Annotated[int | None, Field(ge=MIN_FRAME, le=MAX_FRAME)] = None,
    start_at_seconds: float | None = None,
    end_at_seconds: float | None = None,
    start_subject_name: str | None = None,
    start_point: tuple[float, float, float] | None = None,
    end_subject_name: str | None = None,
    end_point: tuple[float, float, float] | None = None,
    mode: FocusPullMode = "DISTANCE",
    interpolation: Interpolation = "BEZIER",
    focus_control_name: str = "MCP Focus Pull",
    collection_name: str = "MCP Camera Controls",
) -> dict:
    """
    Animate camera-space focus distance or a dedicated live focus control between two subjects.

    Both modes enable the camera's depth of field, unlike ``configure_camera_dof``, which leaves that
    switch to the caller. ``DISTANCE`` keys the camera's own focus distance, clears any focus object,
    and creates nothing - ``focus_control_name`` and ``collection_name`` are unused, and no object is
    added to the scene. ``FOCUS_CONTROL`` creates one tagged Empty under ``collection_name``, keys its
    location, and focuses the camera on it, so an artist can re-aim the pull afterwards.
    """
    if (start_frame is None) == (start_at_seconds is None):
        raise ToolError("supply exactly one of start_frame or start_at_seconds")
    if (end_frame is None) == (end_at_seconds is None):
        raise ToolError("supply exactly one of end_frame or end_at_seconds")
    if (start_subject_name is None) == (start_point is None):
        raise ToolError("Supply exactly one start subject or start point")
    if (end_subject_name is None) == (end_point is None):
        raise ToolError("Supply exactly one end subject or end point")
    return await call_blender("create_focus_pull", _tool_params(locals()), changed_objects=[camera_name])


@mcp.tool()
async def create_dolly_zoom(
    ctx: Context,
    scene_name: str,
    camera_name: str,
    movement_object_name: str,
    start_frame: Annotated[int | None, Field(ge=MIN_FRAME, le=MAX_FRAME)] = None,
    end_frame: Annotated[int | None, Field(ge=MIN_FRAME, le=MAX_FRAME)] = None,
    start_at_seconds: float | None = None,
    end_at_seconds: float | None = None,
    start_distance: Annotated[float, Field(gt=0)] = 0,
    end_distance: Annotated[float, Field(gt=0)] = 0,
    subject_object_name: str | None = None,
    subject_point: tuple[float, float, float] | None = None,
    subject_reference_size: Annotated[float, Field(gt=0)] = 1.0,
    start_lens: Annotated[float | None, Field(gt=0)] = None,
    framing_axis: FramingAxis = "VERTICAL",
    interpolation: Interpolation = "LINEAR",
) -> dict:
    """Animate a lens/distance pair that approximately preserves an explicit subject reference size."""
    if (start_frame is None) == (start_at_seconds is None):
        raise ToolError("supply exactly one of start_frame or start_at_seconds")
    if (end_frame is None) == (end_at_seconds is None):
        raise ToolError("supply exactly one of end_frame or end_at_seconds")
    if start_distance == 0 or end_distance == 0:
        raise ToolError("start_distance and end_distance must be provided and positive")
    if (subject_object_name is None) == (subject_point is None):
        raise ToolError("Supply exactly one subject_object_name or subject_point")
    return await call_blender(
        "create_dolly_zoom", _tool_params(locals()), changed_objects=[camera_name, movement_object_name]
    )


@mcp.tool()
async def add_camera_shake(
    ctx: Context,
    scene_name: str,
    camera_name: str,
    collection_name: str,
    control_name: str,
    frame_start: Annotated[int | None, Field(ge=MIN_FRAME, le=MAX_FRAME)] = None,
    frame_end: Annotated[int | None, Field(ge=MIN_FRAME, le=MAX_FRAME)] = None,
    frame_start_at_seconds: float | None = None,
    frame_end_at_seconds: float | None = None,
    translation_strength: tuple[float, float, float] = (0.02, 0.02, 0.01),
    rotation_strength: tuple[float, float, float] = (0.01, 0.01, 0.02),
    noise_scale: Annotated[float, Field(gt=0)] = 12.0,
    phase: float = 0.0,
    depth: Annotated[int, Field(ge=0, le=8)] = 1,
    influence: Annotated[float, Field(ge=0, le=1)] = 1.0,
) -> dict:
    """Add deterministic procedural shake on a new parent control, preserving authored camera curves."""
    if (frame_start is None) == (frame_start_at_seconds is None):
        raise ToolError("supply exactly one of frame_start or frame_start_at_seconds")
    if (frame_end is None) == (frame_end_at_seconds is None):
        raise ToolError("supply exactly one of frame_end or frame_end_at_seconds")
    if not any(translation_strength) and not any(rotation_strength):
        raise ToolError("At least one shake strength component must be non-zero")
    return await call_blender("add_camera_shake", _tool_params(locals()), changed_objects=[camera_name])
