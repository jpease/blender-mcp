# ruff: file-ignore[multi-line-summary-second-line]
"""Typed tools for evaluating and caching liquid simulations."""

from typing import Annotated, Literal

from mcp.server.fastmcp import Context
from pydantic import Field, model_validator

from ...app import mcp
from .._dispatch import call_blender
from ._shared import _dump, _StrictModel
from .inspection_and_setup import FluidDomainType, FluidSolverPatch
from .mesh_and_materials import CacheMeshFormat

CacheAction = Literal[
    "STATUS",
    "CONFIGURE",
    "BAKE_DATA",
    "BAKE_GUIDES",
    "BAKE_MESH",
    "BAKE_PARTICLES",
    "BAKE_ALL",
    "START_BAKE",
    "RESUME",
    "CANCEL",
    "PAUSE",
    "FREE_DATA",
    "FREE_GUIDES",
    "FREE_MESH",
    "FREE_PARTICLES",
    "FREE_ALL",
]
BakeStage = Literal["DATA", "GUIDES", "MESH", "PARTICLES", "ALL"]
LiquidCacheType = Literal["REPLAY", "MODULAR", "FINAL", "ALL"]
FluidBakeStage = Literal["DATA", "GUIDES", "MESH", "PARTICLES", "NOISE", "ALL"]
FluidCacheAction = Literal[
    "STATUS",
    "CONFIGURE",
    "BAKE_DATA",
    "BAKE_GUIDES",
    "BAKE_MESH",
    "BAKE_PARTICLES",
    "BAKE_NOISE",
    "BAKE_ALL",
    "START_BAKE",
    "RESUME",
    "CANCEL",
    "PAUSE",
    "FREE_DATA",
    "FREE_GUIDES",
    "FREE_MESH",
    "FREE_PARTICLES",
    "FREE_NOISE",
    "FREE_ALL",
]


class LiquidCachePatch(_StrictModel):
    cache_directory: str | None = None
    cache_type: LiquidCacheType | None = None
    cache_data_format: Literal["UNI", "OPENVDB", "RAW"] | None = None
    cache_mesh_format: CacheMeshFormat | None = None
    cache_particle_format: Literal["UNI", "OPENVDB", "RAW"] | None = None
    cache_frame_start: int | None = None
    cache_frame_end: int | None = None
    cache_frame_offset: int | None = None
    cache_resumable: bool | None = None
    openvdb_cache_compress_type: Literal["ZIP", "BLOSC", "NONE"] | None = None
    # Blender exposes this as a dynamic enum whose static RNA metadata is unreliable; the writable
    # identifiers verified against Blender 5.2.1 are the strings "8", "16" and "32" (bit depth).
    openvdb_data_depth: Literal["8", "16", "32"] | None = None

    @model_validator(mode="after")
    def validate_range(self) -> "LiquidCachePatch":
        if (
            self.cache_frame_start is not None
            and self.cache_frame_end is not None
            and self.cache_frame_start > self.cache_frame_end
        ):
            raise ValueError("cache_frame_start must be <= cache_frame_end")
        return self


@mcp.tool()
async def sample_liquid_simulation(
    ctx: Context,
    domain_object_name: str,
    modifier_name: str,
    frames: Annotated[list[int], Field(min_length=1, max_length=32)],
    timeout_seconds: Annotated[float, Field(gt=0.0, le=300.0)] = 30.0,
    boundary_tolerance_cells: Annotated[float, Field(ge=0.0, le=10.0)] = 1.0,
    max_preroll_frames: Annotated[int, Field(ge=1, le=10_000)] = 250,
) -> dict:
    """Evaluate up to 32 cached/replay frames and return numerical mesh, particle, and bounds evidence.

    For a REPLAY domain, Blender only evaluates a frame correctly when every prior frame from
    cache_frame_start was played in order, so this steps scene.frame_set one frame at a time from
    cache_frame_start through the highest requested frame (only requested frames are sampled and
    returned); it rejects a request whose cache_frame_start-to-max(frames) span exceeds
    max_preroll_frames, and rejects any frame before cache_frame_start. For a MODULAR/ALL domain,
    frames are jumped to directly but must fall within the already-baked cache range, or the call
    fails instead of silently returning stale or empty geometry.
    """
    return await call_blender(
        "sample_liquid_simulation",
        {
            "domain_object_name": domain_object_name,
            "modifier_name": modifier_name,
            "frames": frames,
            "timeout_seconds": timeout_seconds,
            "boundary_tolerance_cells": boundary_tolerance_cells,
            "max_preroll_frames": max_preroll_frames,
        },
        changed_objects=[domain_object_name],
    )


@mcp.tool()
async def manage_liquid_cache(
    ctx: Context,
    domain_object_name: str,
    modifier_name: str,
    action: CacheAction = "STATUS",
    patch: LiquidCachePatch | None = None,
    stage: BakeStage | None = None,
    confirm_bake: bool = False,
    confirm_free: bool = False,
    confirm_external_path: bool = False,
    confirm_external_overwrite: bool = False,
    max_bake_frames: Annotated[int, Field(ge=1, le=10_000)] = 250,
    max_existing_cache_bytes: Annotated[int, Field(ge=0)] = 10_000_000_000,
) -> dict:
    """Inspect, configure, bake, pause/resume, cancel, or explicitly free exact Mantaflow cache stages.

    BAKE_DATA/BAKE_GUIDES/BAKE_MESH/BAKE_PARTICLES/BAKE_ALL always run synchronously on the calling
    thread (Blender's default operator-calling convention), blocking until that stage finishes.

    START_BAKE (requires `stage`) instead dispatches the matching bake as a non-blocking Blender job
    (INVOKE_DEFAULT under an explicit window/area/region) whenever this Blender process has a real GUI
    window, returning almost immediately with a `job_id` to poll via STATUS; poll until the stage's
    has_cache_baked_* flag is true before sampling or chaining another bake action. Under `--background`
    Blender (no window manager) START_BAKE transparently falls back to the same synchronous behavior as
    BAKE_*, and a warning says so.

    RESUME (requires `stage`) continues a paused MODULAR+cache_resumable bake from its pause frame; it
    is only valid when that stage is actually paused (not currently baking, not already fully baked,
    with a nonzero cache_frame_pause for that stage — Blender leaves cache_frame_pause set to the final
    frame after a normal completed bake too, so both checks are required to detect a real pause) and
    behaves like START_BAKE otherwise (dispatched as a job when a GUI window is available). Bake All
    cannot be paused or resumed.

    PAUSE now requires cache_type=MODULAR with cache_resumable=True in addition to a stage currently
    baking; Blender does not support pausing a Bake All run.

    CONFIGURE's patch.openvdb_cache_compress_type and patch.openvdb_data_depth ("8"/"16"/"32" bits of
    float precision) only affect OpenVDB-formatted stages, so they are rejected unless at least one of
    cache_data_format/cache_mesh_format/cache_particle_format is (or is being set to) OPENVDB.

    CANCEL (requires `stage`) has no scripted equivalent to Blender's interactive Esc-to-abort for a
    running bake job, so it raises a clear error while that stage is actively baking; once the stage is
    not baking, CANCEL degrades to freeing that stage's cache (same confirm_free/confirm_external_overwrite
    gates as FREE_*).
    """
    return await call_blender(
        "manage_liquid_cache",
        {
            "domain_object_name": domain_object_name,
            "modifier_name": modifier_name,
            "action": action,
            "patch": _dump(patch),
            "stage": stage,
            "confirm_bake": confirm_bake,
            "confirm_free": confirm_free,
            "confirm_external_path": confirm_external_path,
            "confirm_external_overwrite": confirm_external_overwrite,
            "max_bake_frames": max_bake_frames,
            "max_existing_cache_bytes": max_existing_cache_bytes,
        },
        changed_objects=[domain_object_name],
    )


@mcp.tool()
async def configure_fluid_solver(
    ctx: Context,
    domain_type: FluidDomainType,
    domain_object_name: str,
    modifier_name: str,
    patch: FluidSolverPatch,
) -> dict:
    """Patch validated common or domain-specific solver settings without touching omitted fields."""
    return await call_blender(
        "configure_fluid_solver",
        {
            "domain_type": domain_type,
            "domain_object_name": domain_object_name,
            "modifier_name": modifier_name,
            "patch": _dump(patch),
        },
        changed_objects=[domain_object_name],
    )


@mcp.tool()
async def manage_fluid_cache(
    ctx: Context,
    domain_type: FluidDomainType,
    domain_object_name: str,
    modifier_name: str,
    action: FluidCacheAction = "STATUS",
    patch: LiquidCachePatch | None = None,
    stage: FluidBakeStage | None = None,
    confirm_bake: bool = False,
    confirm_free: bool = False,
    confirm_external_path: bool = False,
    confirm_external_overwrite: bool = False,
    max_bake_frames: Annotated[int, Field(ge=1, le=10_000)] = 250,
) -> dict:
    """Manage a normalized Mantaflow cache lifecycle for LIQUID or GAS domains."""
    return await call_blender(
        "manage_fluid_cache",
        {
            "domain_type": domain_type,
            "domain_object_name": domain_object_name,
            "modifier_name": modifier_name,
            "action": action,
            "patch": _dump(patch),
            "stage": stage,
            "confirm_bake": confirm_bake,
            "confirm_free": confirm_free,
            "confirm_external_path": confirm_external_path,
            "confirm_external_overwrite": confirm_external_overwrite,
            "max_bake_frames": max_bake_frames,
        },
        changed_objects=[domain_object_name],
    )
