"""Rigid-body simulation sampling and cache lifecycle tools."""

from typing import Annotated, Literal

from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .._dispatch import call_blender
from .inspection_and_setup import Vector3, mcp


class SimulationFrameSelection(BaseModel):
    """A bounded explicit or ranged frame selection."""

    model_config = ConfigDict(extra="forbid")
    frames: list[int] | None = Field(default=None, min_length=1, max_length=100)
    frame_start: int | None = None
    frame_end: int | None = None
    frame_step: int = Field(default=1, ge=1, le=120)

    @model_validator(mode="after")
    def validate_selection(self) -> "SimulationFrameSelection":
        has_explicit = self.frames is not None
        has_range = self.frame_start is not None or self.frame_end is not None
        if has_explicit == has_range:
            raise ValueError("Supply either frames or frame_start/frame_end")
        if has_range and (self.frame_start is None or self.frame_end is None):
            raise ValueError("frame_start and frame_end must be supplied together")
        if self.frame_start is not None and self.frame_end is not None and self.frame_start > self.frame_end:
            raise ValueError("frame_start must not exceed frame_end")
        if self.frames is not None and (
            len(set(self.frames)) != len(self.frames) or self.frames != sorted(self.frames)
        ):
            raise ValueError("frames must be unique and ordered")
        return self


class RigidBodyCacheSettings(BaseModel):
    """Editable PointCache settings supported by the rigid-body world."""

    model_config = ConfigDict(extra="forbid")
    frame_start: int | None = None
    frame_end: int | None = None
    frame_step: int | None = Field(default=None, ge=1, le=1000)
    name: str | None = None
    index: int | None = Field(default=None, ge=0)
    use_disk_cache: bool | None = None
    use_external: bool | None = None
    use_library_path: bool | None = None
    filepath: str | None = None

    @model_validator(mode="after")
    def validate_range(self) -> "RigidBodyCacheSettings":
        if self.frame_start is not None and self.frame_end is not None and self.frame_start > self.frame_end:
            raise ValueError("frame_start must not exceed frame_end")
        return self


@mcp.tool()
async def sample_rigid_body_simulation(
    ctx: Context,
    scene_name: str,
    object_names: Annotated[list[str], Field(min_length=1, max_length=100)],
    frame_selection: SimulationFrameSelection,
    include_velocity: bool = True,
    stationary_speed: Annotated[float, Field(ge=0.0)] = 0.001,
    escape_bounds_min: Vector3 | None = None,
    escape_bounds_max: Vector3 | None = None,
    timeout_seconds: Annotated[float, Field(gt=0.0, le=300.0)] = 30.0,
) -> dict:
    """
    Sequentially evaluate bounded frames and return transforms plus stability diagnostics.

    For each object at each selected frame, reports its transform and, when include_velocity is
    true, linear/angular velocity. An object whose speed drops below stationary_speed is reported
    as settled; supplying escape_bounds_min and escape_bounds_max together (both or neither) flags
    any object whose location leaves that world-space box as having escaped the simulation. This
    only evaluates the current scene state frame-by-frame - it does not bake or write keyframes; use
    bake_rigid_bodies_to_keyframes for that.
    """
    if (escape_bounds_min is None) != (escape_bounds_max is None):
        raise ToolError("escape_bounds_min and escape_bounds_max must be supplied together")
    return await call_blender(
        "sample_rigid_body_simulation",
        {
            "scene_name": scene_name,
            "object_names": object_names,
            "frame_selection": frame_selection.model_dump(exclude_none=True),
            "include_velocity": include_velocity,
            "stationary_speed": stationary_speed,
            "escape_bounds_min": escape_bounds_min,
            "escape_bounds_max": escape_bounds_max,
            "timeout_seconds": timeout_seconds,
        },
        changed_objects=object_names,
    )


@mcp.tool()
async def manage_rigid_body_cache(
    ctx: Context,
    scene_name: str,
    action: Literal["INSPECT", "CONFIGURE", "CALCULATE_TO_FRAME", "BAKE", "BAKE_FROM_CACHE", "FREE"] = "INSPECT",
    settings: RigidBodyCacheSettings | None = None,
    calculate_frame: int | None = None,
    confirm_bake: bool = False,
    confirm_free: bool = False,
    confirm_external_overwrite: bool = False,
    max_frame_steps: Annotated[int, Field(ge=1, le=10_000)] = 250,
) -> dict:
    """
    Inspect, configure, evaluate, bake, or explicitly free scene_name's rigid-body world point cache.

    action selects the operation: INSPECT reports the current cache settings and bake state (no
    settings or calculate_frame accepted); CONFIGURE applies `settings` (required, and only accepted
    for this action) such as frame range, disk/external cache paths, and cache name/index;
    CALCULATE_TO_FRAME evaluates up to calculate_frame (required only for this action) without a
    full bake; BAKE and BAKE_FROM_CACHE compute or continue the full cache and require
    confirm_bake=True; FREE discards the cache and requires confirm_free=True.
    confirm_external_overwrite additionally guards actions that would overwrite an existing external
    cache file. max_frame_steps bounds how many frames a single call may evaluate before returning
    early.
    """
    patch = settings.model_dump(exclude_none=True, exclude_unset=True) if settings else {}
    if action == "CONFIGURE" and not patch:
        raise ToolError("CONFIGURE requires settings")
    if action != "CONFIGURE" and patch:
        raise ToolError(f"{action} does not accept settings")
    if (action == "CALCULATE_TO_FRAME") != (calculate_frame is not None):
        raise ToolError("calculate_frame is required only for CALCULATE_TO_FRAME")
    return await call_blender(
        "manage_rigid_body_cache",
        {
            "scene_name": scene_name,
            "action": action,
            "settings": patch,
            "calculate_frame": calculate_frame,
            "confirm_bake": confirm_bake,
            "confirm_free": confirm_free,
            "confirm_external_overwrite": confirm_external_overwrite,
            "max_frame_steps": max_frame_steps,
        },
    )
