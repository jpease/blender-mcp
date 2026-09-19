# MCP camera tools intentionally use explicit keyword arguments so agents receive
# a precise schema instead of an unsafe generic RNA property bag.
"""Typed tools for camera object lifecycle: creation, optics/display configuration, and DOF."""

import asyncio

from typing import Annotated, Literal

from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field, model_validator

from ...app import mcp
from ._shared import _call, _dump, _StrictModel

Projection = Literal["PERSP", "ORTHO", "PANO"]
SensorFit = Literal["AUTO", "HORIZONTAL", "VERTICAL"]


class CameraOpticsPatch(_StrictModel):
    """Allowlisted Blender 5.1 camera projection and optical fields."""

    projection: Projection | None = None
    lens: float | None = Field(default=None, gt=0)
    ortho_scale: float | None = Field(default=None, gt=0)
    sensor_width: float | None = Field(default=None, gt=0)
    sensor_height: float | None = Field(default=None, gt=0)
    sensor_fit: SensorFit | None = None
    shift_x: float | None = None
    shift_y: float | None = None
    clip_start: float | None = Field(default=None, gt=0)
    clip_end: float | None = Field(default=None, gt=0)
    panorama_type: str | None = None

    @model_validator(mode="after")
    def validate_clip_order(self) -> "CameraOpticsPatch":
        if self.clip_start is not None and self.clip_end is not None and self.clip_start >= self.clip_end:
            raise ValueError("clip_start must be less than clip_end")
        return self


class CameraDisplayPatch(_StrictModel):
    """Allowlisted Blender 5.1 camera viewport and composition-guide fields."""

    passepartout_alpha: float | None = Field(default=None, ge=0, le=1)
    show_passepartout: bool | None = None
    show_safe_areas: bool | None = None
    show_name: bool | None = None
    show_limits: bool | None = None
    show_mist: bool | None = None
    show_composition_center: bool | None = None
    show_composition_center_diagonal: bool | None = None
    show_composition_golden: bool | None = None
    show_composition_golden_tria_a: bool | None = None
    show_composition_golden_tria_b: bool | None = None
    show_composition_harmony_tri_a: bool | None = None
    show_composition_harmony_tri_b: bool | None = None
    show_composition_thirds: bool | None = None


class CameraDofPatch(_StrictModel):
    """Photographic depth-of-field settings; focus intent is supplied separately."""

    use_dof: bool | None = None
    aperture_fstop: float | None = Field(default=None, gt=0)
    aperture_blades: int | None = Field(default=None, ge=0, le=16)
    aperture_rotation: float | None = None
    aperture_ratio: float | None = Field(default=None, gt=0)


@mcp.tool()
async def create_camera(
    ctx: Context,
    scene_name: str,
    collection_name: str,
    name: Annotated[str, Field(min_length=1, max_length=63)],
    projection: Projection = "PERSP",
    location: tuple[float, float, float] = (0.0, 0.0, 0.0),
    rotation_euler: tuple[float, float, float] | None = None,
    rotation_quaternion: tuple[float, float, float, float] | None = None,
    look_at_object_name: str | None = None,
    look_at_point: tuple[float, float, float] | None = None,
    optics: CameraOpticsPatch | None = None,
    make_active: bool = False,
) -> dict:
    """
    Create a collision-safe camera in an explicit scene collection.

    Coordinates are world-space; Euler angles are XYZ radians and quaternions are [w, x, y, z].
    Supply at most one of Euler rotation, quaternion rotation, look-at object, or look-at point.
    The camera is not selected and does not become the scene camera unless ``make_active`` is true.
    Panoramic settings are capability-checked against the running Blender build.
    """
    orientations = [rotation_euler, rotation_quaternion, look_at_object_name, look_at_point]
    if sum(value is not None for value in orientations) > 1:
        raise ToolError("Supply only one orientation source: Euler, quaternion, look-at object, or look-at point")
    if optics is not None and optics.projection is not None and optics.projection != projection:
        raise ToolError("projection conflicts with optics.projection; supply projection in only one place")
    return await asyncio.to_thread(
        _call,
        "create_camera",
        {
            "scene_name": scene_name,
            "collection_name": collection_name,
            "name": name,
            "projection": projection,
            "location": location,
            "rotation_euler": rotation_euler,
            "rotation_quaternion": rotation_quaternion,
            "look_at_object_name": look_at_object_name,
            "look_at_point": look_at_point,
            "optics": _dump(optics),
            "make_active": make_active,
        },
    )


@mcp.tool()
async def configure_camera(
    ctx: Context,
    camera_name: str,
    optics: CameraOpticsPatch | None = None,
    display: CameraDisplayPatch | None = None,
) -> dict:
    """
    Patch only the supplied optical, clipping, and viewport fields on one camera.

    This does not change render resolution because the render gate belongs to the scene. The result
    reports old and new values. Use ``configure_camera_dof`` for focus and aperture controls.
    """
    optics_payload = _dump(optics)
    display_payload = _dump(display)
    if not optics_payload and not display_payload:
        raise ToolError("Provide at least one optics or display field to change")
    return await asyncio.to_thread(
        _call,
        "configure_camera",
        {"camera_name": camera_name, "optics": optics_payload, "display": display_payload},
        [camera_name],
    )


@mcp.tool()
async def set_scene_camera(
    ctx: Context,
    scene_name: str,
    camera_name: str,
    marker_name: str | None = None,
    marker_frame: Annotated[int | None, Field(ge=-1_048_574, le=1_048_574)] = None,
    replace_marker: bool = False,
) -> dict:
    """
    Set the scene camera and optionally bind it to one exact timeline marker.

    ``marker_name`` and ``marker_frame`` must be supplied together. Existing marker bindings are
    preserved unless ``replace_marker`` is true; this avoids silently changing editorial cuts.
    """
    if (marker_name is None) != (marker_frame is None):
        raise ToolError("marker_name and marker_frame must be supplied together")
    return await asyncio.to_thread(
        _call,
        "set_scene_camera",
        {
            "scene_name": scene_name,
            "camera_name": camera_name,
            "marker_name": marker_name,
            "marker_frame": marker_frame,
            "replace_marker": replace_marker,
        },
        [camera_name],
    )


@mcp.tool()
async def configure_camera_dof(
    ctx: Context,
    scene_name: str,
    camera_name: str,
    patch: CameraDofPatch,
    focus_object_name: str | None = None,
    focus_distance: Annotated[float | None, Field(gt=0)] = None,
    focus_point: tuple[float, float, float] | None = None,
    focus_target_name: str | None = None,
    focus_collection_name: str = "MCP Camera Controls",
    reuse_focus_target: bool = False,
) -> dict:
    """
    Configure photographic depth of field without changing camera aim.

    Supply at most one focus intent: an existing object, a positive distance, or a world-space point.
    A point creates (or explicitly reuses) a tagged focus Empty. Object focus and aim targets remain
    separate dependencies. The visible result still depends on the render engine and sampling.
    """
    if sum(value is not None for value in (focus_object_name, focus_distance, focus_point)) > 1:
        raise ToolError("Supply at most one focus intent: focus object, focus distance, or focus point")
    if focus_point is not None and not focus_target_name:
        raise ToolError("focus_target_name is required when focus_point is supplied")
    patch_payload = _dump(patch)
    if not patch_payload and focus_object_name is None and focus_distance is None and focus_point is None:
        raise ToolError("Provide at least one depth-of-field or focus change")
    return await asyncio.to_thread(
        _call,
        "configure_camera_dof",
        {
            "scene_name": scene_name,
            "camera_name": camera_name,
            "patch": patch_payload,
            "focus_object_name": focus_object_name,
            "focus_distance": focus_distance,
            "focus_point": focus_point,
            "focus_target_name": focus_target_name,
            "focus_collection_name": focus_collection_name,
            "reuse_focus_target": reuse_focus_target,
        },
        [camera_name],
    )
