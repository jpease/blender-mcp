# MCP tool signatures intentionally expose more than five keyword arguments so
# agents receive precise JSON schemas instead of opaque catch-all dictionaries.
"""Typed tools for cloth resource estimation, validation, sampling, and performance analysis."""

import asyncio

from typing import Annotated

from mcp.server.fastmcp import Context
from pydantic import Field

from ...app import mcp
from ._shared import _call


@mcp.tool()
async def estimate_cloth_resources(
    ctx: Context,
    scene_name: str,
    collection_name: str | None = None,
    cloth_object_names: list[str] | None = None,
    object_limit: Annotated[int, Field(ge=1, le=200)] = 25,
    object_offset: Annotated[int, Field(ge=0)] = 0,
) -> dict:
    """
    Estimate bounded relative CPU, memory, cache, and contact pressure for scoped cloth objects.

    Indices are deterministic heuristics for comparing setups and choosing preview/final settings;
    they are not byte counts or bake-duration promises. Runtime PointCache facts are reported apart.
    """
    return await asyncio.to_thread(
        _call,
        "estimate_cloth_resources",
        {
            "scene_name": scene_name,
            "collection_name": collection_name,
            "cloth_object_names": cloth_object_names,
            "object_limit": object_limit,
            "object_offset": object_offset,
        },
    )


@mcp.tool()
async def validate_cloth_setup(
    ctx: Context,
    scene_name: str,
    collection_name: str | None = None,
    cloth_object_names: list[str] | None = None,
    max_findings: Annotated[int, Field(ge=1, le=2000)] = 200,
    collision_pair_limit: Annotated[int, Field(ge=1)] = 64,
    evaluated_triangle_limit: Annotated[int, Field(ge=1)] = 250_000,
) -> dict:
    """
    Run a bounded, non-mutating structural preflight over scoped cloth systems.

    Findings include severity, evidence, affected property/object/frame, and remediation. Continue
    with narrower scopes when ``truncated`` or the collision-pair limit is reported. Passing this
    check does not replace representative evaluated-frame review in Blender.
    """
    return await asyncio.to_thread(
        _call,
        "validate_cloth_setup",
        {
            "scene_name": scene_name,
            "collection_name": collection_name,
            "cloth_object_names": cloth_object_names,
            "max_findings": max_findings,
            "collision_pair_limit": collision_pair_limit,
            "evaluated_triangle_limit": evaluated_triangle_limit,
        },
    )


@mcp.tool()
async def sample_cloth_simulation(
    ctx: Context,
    object_name: str,
    modifier_name: str,
    frames: Annotated[list[int], Field(min_length=1)],
    vertex_sample_limit: Annotated[int, Field(ge=1)] = 10_000,
    collider_sample_limit: Annotated[int, Field(ge=0)] = 16,
    timeout_seconds: Annotated[float, Field(gt=0)] = 30.0,
) -> dict:
    """
    Evaluate bounded frames and measure the cloth without baking it.

    Sampling can populate or invalidate Blender's in-memory point cache and is therefore mutating.
    The original frame is restored in ``finally``. Returned penetration evidence is heuristic and
    representative-frame review remains necessary.
    """
    return await asyncio.to_thread(
        _call,
        "sample_cloth_simulation",
        {
            "object_name": object_name,
            "modifier_name": modifier_name,
            "frames": frames,
            "vertex_sample_limit": vertex_sample_limit,
            "collider_sample_limit": collider_sample_limit,
            "timeout_seconds": timeout_seconds,
        },
        [object_name],
    )


@mcp.tool()
async def analyze_cloth_performance(
    ctx: Context,
    object_name: str,
    modifier_name: str,
    frames: Annotated[list[int], Field(min_length=1)],
    warm_repeats: Annotated[int, Field(ge=0)] = 2,
    max_total_evaluations: Annotated[int, Field(ge=1)] = 60,
    include_short_bake: bool = False,
    confirm_short_bake: bool = False,
    short_bake_frame_start: Annotated[int, Field(ge=0)] | None = None,
    short_bake_frame_end: Annotated[int, Field(ge=0)] | None = None,
) -> dict:
    """
    Profile bounded first-pass/warm frame evaluation and optional isolated short baking.

    The optional bake runs on a temporary object with an independent in-memory cache and requires
    confirmation. Source caches are never freed or overwritten. Timings are measurements for this
    run only and are returned separately from structural cost evidence.
    """
    return await asyncio.to_thread(
        _call,
        "analyze_cloth_performance",
        {
            "object_name": object_name,
            "modifier_name": modifier_name,
            "frames": frames,
            "warm_repeats": warm_repeats,
            "max_total_evaluations": max_total_evaluations,
            "include_short_bake": include_short_bake,
            "confirm_short_bake": confirm_short_bake,
            "short_bake_frame_start": short_bake_frame_start,
            "short_bake_frame_end": short_bake_frame_end,
        },
        [object_name],
    )
