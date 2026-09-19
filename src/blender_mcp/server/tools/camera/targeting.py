"""Typed tools for aiming, targeting, framing, and constraining cameras."""

import asyncio

from typing import Annotated, Literal

from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from ...app import mcp
from ._shared import ConstraintSpace, FollowForwardAxis, LockAxis, TrackAxis, UpAxis, _call, _tool_params

TrackingConstraint = Literal["TRACK_TO", "DAMPED_TRACK", "LOCKED_TRACK"]
FramePolicy = Literal["MOVE_CAMERA", "CHANGE_LENS", "CHANGE_ORTHO_SCALE"]
CameraConstraint = Literal[
    "TRACK_TO",
    "DAMPED_TRACK",
    "LOCKED_TRACK",
    "FOLLOW_PATH",
    "CHILD_OF",
    "COPY_LOCATION",
    "COPY_ROTATION",
    "COPY_TRANSFORMS",
    "LIMIT_LOCATION",
    "LIMIT_ROTATION",
    "LIMIT_SCALE",
]


@mcp.tool()
async def point_camera_at(
    ctx: Context,
    scene_name: str,
    camera_name: str,
    target_object_name: str | None = None,
    target_point: tuple[float, float, float] | None = None,
    subtarget: str | None = None,
) -> dict:
    """
    Rotate a camera once to aim at an object or world-space point.

    Supply exactly one target source. Rotates local -Z toward the target with local Y as up,
    correctly resolving parent space. This is a one-shot rotation, not a constraint — use
    add_camera_constraint for a live tracking relationship.
    """
    if (target_object_name is None) == (target_point is None):
        raise ToolError("Supply exactly one of target_object_name or target_point")
    if subtarget is not None and target_object_name is None:
        raise ToolError("subtarget requires target_object_name")
    return await asyncio.to_thread(
        _call,
        "point_camera_at",
        {
            "scene_name": scene_name,
            "camera_name": camera_name,
            "target_object_name": target_object_name,
            "target_point": target_point,
            "subtarget": subtarget,
        },
        [camera_name],
    )


@mcp.tool()
async def create_camera_target(
    ctx: Context,
    scene_name: str,
    collection_name: str,
    name: Annotated[str, Field(min_length=1, max_length=63)],
    location: tuple[float, float, float] | None = None,
    target_object_name: str | None = None,
    use_evaluated_bounds_center: bool = True,
    reuse: bool = False,
    camera_names: list[str] | None = None,
    constraint_type: TrackingConstraint = "DAMPED_TRACK",
) -> dict:
    """
    Create or explicitly reuse a tagged Empty as a camera aim control.

    Supply either a world location or an object whose evaluated bounds center should be used. Reuse
    is opt-in and accepts only an Empty already tagged as a camera target. Named cameras receive a
    live -Z tracking constraint; unrelated constraints remain untouched.
    """
    if (location is None) == (target_object_name is None):
        raise ToolError("Supply exactly one of location or target_object_name")
    return await asyncio.to_thread(
        _call,
        "create_camera_target",
        {
            "scene_name": scene_name,
            "collection_name": collection_name,
            "name": name,
            "location": location,
            "target_object_name": target_object_name,
            "use_evaluated_bounds_center": use_evaluated_bounds_center,
            "reuse": reuse,
            "camera_names": camera_names or [],
            "constraint_type": constraint_type,
        },
    )


@mcp.tool()
async def frame_camera_on_objects(
    ctx: Context,
    scene_name: str,
    camera_name: str,
    object_names: list[str],
    margin: Annotated[float, Field(ge=0, lt=0.9)] = 0.1,
    policy: FramePolicy = "MOVE_CAMERA",
    aim_at_center: bool = True,
) -> dict:
    """
    Fit explicit evaluated objects in a camera without viewport operators.

    ``MOVE_CAMERA`` preserves perspective optics, ``CHANGE_LENS`` preserves camera position, and
    ``CHANGE_ORTHO_SCALE`` is required for orthographic scale changes. Modifier-evaluated world
    bounds, target point, solved distance or optical value, and limiting frame axis are returned.
    The margin is the fractional inset on each side of the render frame.
    """
    if not object_names:
        raise ToolError("object_names must not be empty")
    return await asyncio.to_thread(
        _call,
        "frame_camera_on_objects",
        {
            "scene_name": scene_name,
            "camera_name": camera_name,
            "object_names": object_names,
            "margin": margin,
            "policy": policy,
            "aim_at_center": aim_at_center,
        },
        [camera_name],
    )


@mcp.tool()
async def add_camera_constraint(
    ctx: Context,
    scene_name: str,
    owner_name: str,
    constraint_name: Annotated[str, Field(min_length=1)],
    constraint_type: CameraConstraint,
    target_name: str | None = None,
    subtarget: str | None = None,
    influence: Annotated[float, Field(ge=0, le=1)] = 1.0,
    owner_space: ConstraintSpace = "WORLD",
    target_space: ConstraintSpace = "WORLD",
    stack_index: Annotated[int, Field(ge=-1)] = -1,
    preserve_transform: bool = True,
    track_axis: TrackAxis = "TRACK_NEGATIVE_Z",
    up_axis: UpAxis = "UP_Y",
    lock_axis: LockAxis = "LOCK_Y",
    forward_axis: FollowForwardAxis = "FORWARD_X",
    use_curve_follow: bool = True,
    use_fixed_location: bool = True,
    offset_factor: Annotated[float, Field(ge=0, le=1)] = 0.0,
    use_x: bool = True,
    use_y: bool = True,
    use_z: bool = True,
    invert_x: bool = False,
    invert_y: bool = False,
    invert_z: bool = False,
    minimum: tuple[float, float, float] | None = None,
    maximum: tuple[float, float, float] | None = None,
) -> dict:
    """Add or update one curated, typed camera-rig constraint with a stable name."""
    targeted = constraint_type not in {"LIMIT_LOCATION", "LIMIT_ROTATION", "LIMIT_SCALE"}
    if targeted != (target_name is not None):
        raise ToolError(
            "This constraint type requires target_name" if targeted else "Limit constraints do not use target_name"
        )
    if constraint_type.startswith("LIMIT_") and minimum is None and maximum is None:
        raise ToolError("Limit constraints require minimum and/or maximum")
    return await asyncio.to_thread(_call, "add_camera_constraint", _tool_params(locals()), [owner_name])
