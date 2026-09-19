"""Typed tools for building reusable camera rigs and rig-level transform utilities."""

import asyncio

from typing import Annotated, Literal

from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from ...app import mcp
from ._shared import FollowForwardAxis, UpAxis, _call, _dump, _StrictModel, _tool_params

SplineType = Literal["BEZIER", "NURBS"]
DataPolicy = Literal["COPY", "LINK"]
AnimationPolicy = Literal["COPY", "LINK", "NONE"]
ExternalTargetPolicy = Literal["SHARE", "REJECT"]
MatchPolicy = Literal["TRANSFORM_ONLY", "OPTICS_ONLY", "FULL"]


class WorldTransform(_StrictModel):
    """Complete world transform using a [w, x, y, z] quaternion."""

    location: tuple[float, float, float]
    rotation_quaternion: tuple[float, float, float, float]
    scale: tuple[float, float, float] = (1.0, 1.0, 1.0)


@mcp.tool()
async def create_orbit_camera_rig(
    ctx: Context,
    scene_name: str,
    collection_name: str,
    rig_name: Annotated[str, Field(min_length=1)],
    pivot: tuple[float, float, float],
    radius: Annotated[float, Field(gt=0)],
    azimuth: float = 0.0,
    elevation: float = 0.0,
    roll: float = 0.0,
    lens: Annotated[float, Field(gt=0)] = 50.0,
    target_height: float = 0.0,
) -> dict:
    """
    Build a new editable orbit rig from standard Empty, Camera, and Damped Track objects.

    Angles are radians. The root Z rotation is azimuth; the boom offset encodes radius/elevation;
    camera roll remains available on the camera control. All members are tagged with one rig UUID,
    role, schema version, and owner so agents can inspect or duplicate them safely.
    """
    return await asyncio.to_thread(_call, "create_orbit_camera_rig", _tool_params(locals()))


@mcp.tool()
async def create_dolly_camera_rig(
    ctx: Context,
    scene_name: str,
    collection_name: str,
    rig_name: Annotated[str, Field(min_length=1)],
    location: tuple[float, float, float],
    rail_direction: tuple[float, float, float] = (0.0, 1.0, 0.0),
    yaw: float = 0.0,
    camera_height: Annotated[float, Field(ge=0)] = 1.5,
    target_distance: Annotated[float, Field(gt=0)] = 10.0,
    lens: Annotated[float, Field(gt=0)] = 50.0,
    create_target: bool = True,
) -> dict:
    """
    Build a conventional dolly rig with root, camera-height control, camera, and optional aim target.

    ``rail_direction`` is a non-zero local direction exposed as rig metadata for animation planning;
    translate/yaw the root for the dolly move and animate the child control for height/pitch/roll.
    """
    return await asyncio.to_thread(_call, "create_dolly_camera_rig", _tool_params(locals()))


@mcp.tool()
async def create_crane_camera_rig(
    ctx: Context,
    scene_name: str,
    collection_name: str,
    rig_name: Annotated[str, Field(min_length=1)],
    location: tuple[float, float, float],
    base_height: Annotated[float, Field(ge=0)] = 1.0,
    arm_length: Annotated[float, Field(gt=0)] = 5.0,
    elevation: float = 0.0,
    pan: float = 0.0,
    tilt: float = 0.0,
    roll: float = 0.0,
    lens: Annotated[float, Field(gt=0)] = 50.0,
    create_target: bool = True,
) -> dict:
    """
    Build an editable crane hierarchy with base, arm pivot, boom, head, camera, and optional target.

    Angles are radians and remain independently animatable on standard object transforms. The boom
    length is its local X offset; no opaque driver or optional add-on dependency is introduced.
    """
    return await asyncio.to_thread(_call, "create_crane_camera_rig", _tool_params(locals()))


@mcp.tool()
async def create_camera_path_rig(
    ctx: Context,
    scene_name: str,
    collection_name: str,
    rig_name: Annotated[str, Field(min_length=1)],
    camera_name: str,
    curve_object_name: str | None = None,
    path_points: list[tuple[float, float, float]] | None = None,
    spline_type: SplineType = "BEZIER",
    forward_axis: FollowForwardAxis = "TRACK_NEGATIVE_Z",
    up_axis: UpAxis = "UP_Y",
    use_curve_follow: bool = True,
    start_frame: Annotated[int | None, Field(ge=-1_048_574, le=1_048_574)] = None,
    end_frame: Annotated[int | None, Field(ge=-1_048_574, le=1_048_574)] = None,
    target_object_name: str | None = None,
) -> dict:
    """
    Attach an existing camera to a new rig root following one explicit curve.

    Supply either an existing curve name or at least two world-space points for a new Bézier/NURBS
    path. Optional start/end frames key the constraint's fixed-position ``offset_factor`` from 0 to
    1 without touching curve path animation. The camera's pre-rig world transform is preserved.
    """
    if (curve_object_name is None) == (path_points is None):
        raise ToolError("Supply exactly one of curve_object_name or path_points")
    if path_points is not None and len(path_points) < 2:
        raise ToolError("path_points must contain at least two points")
    if (start_frame is None) != (end_frame is None):
        raise ToolError("start_frame and end_frame must be supplied together")
    if start_frame is not None and end_frame is not None and start_frame >= end_frame:
        raise ToolError("start_frame must be less than end_frame")
    return await asyncio.to_thread(_call, "create_camera_path_rig", _tool_params(locals()))


@mcp.tool()
async def match_camera_transform(
    ctx: Context,
    destination_name: str,
    policy: MatchPolicy = "TRANSFORM_ONLY",
    source_object_name: str | None = None,
    world_transform: WorldTransform | None = None,
) -> dict:
    """Match a destination in world space and optionally copy explicit camera optical fields."""
    if (source_object_name is None) == (world_transform is None):
        raise ToolError("Supply exactly one source_object_name or world_transform")
    if policy != "TRANSFORM_ONLY" and source_object_name is None:
        raise ToolError("Optics matching requires a source camera object")
    return await asyncio.to_thread(
        _call,
        "match_camera_transform",
        {
            "destination_name": destination_name,
            "policy": policy,
            "source_object_name": source_object_name,
            "world_transform": _dump(world_transform),
        },
        [destination_name],
    )


@mcp.tool()
async def duplicate_camera_rig(
    ctx: Context,
    scene_name: str,
    source_root_name: str,
    collection_name: str,
    new_rig_name: Annotated[str, Field(min_length=1)],
    camera_data_policy: DataPolicy = "COPY",
    path_data_policy: DataPolicy = "COPY",
    animation_policy: AnimationPolicy = "COPY",
    external_target_policy: ExternalTargetPolicy = "SHARE",
) -> dict:
    """Duplicate one tagged rig and explicitly control datablock, action, and external-target sharing."""
    return await asyncio.to_thread(_call, "duplicate_camera_rig", _tool_params(locals()))
