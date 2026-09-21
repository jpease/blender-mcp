# ruff: file-ignore[multi-line-summary-second-line]
"""Bounded procedural-system performance analysis tool."""

from typing import Annotated

from mcp.server.fastmcp import Context
from pydantic import Field

from ...app import mcp
from .._dispatch import call_blender

MAX_SAMPLE_FRAMES = 8


@mcp.tool()
async def analyze_procedural_performance(
    ctx: Context,
    object_name: str,
    modifier_name: str,
    frames: Annotated[list[int], Field(min_length=1, max_length=MAX_SAMPLE_FRAMES)],
    repetitions: Annotated[int, Field(ge=1, le=5)] = 1,
    time_limit_seconds: Annotated[float, Field(gt=0, le=300)] = 30.0,
    instance_limit: Annotated[int, Field(ge=1, le=100_000)] = 10_000,
    topology_warning_threshold: Annotated[int, Field(ge=1)] = 1_000_000,
) -> dict:
    """Measure bounded whole-system evaluation and flag common Geometry Nodes cost risks.

    Supply 1-8 explicit frames. Timings cover dependency-graph evaluation and mesh extraction, not
    individual nodes. Heuristics separately identify early realization, dense volume/subdivision or
    distribution nodes, Boolean fan-in, high repeat counts, nested groups, unverified simulation
    caches, and heavy branches disconnected from the active output. The current frame is restored.
    """
    if not frames or len(frames) > MAX_SAMPLE_FRAMES or len(set(frames)) != len(frames):
        raise ValueError(f"frames must contain 1-{MAX_SAMPLE_FRAMES} unique frame numbers")
    return await call_blender(
        "analyze_procedural_performance",
        {
            "object_name": object_name,
            "modifier_name": modifier_name,
            "frames": frames,
            "repetitions": repetitions,
            "time_limit_seconds": time_limit_seconds,
            "instance_limit": instance_limit,
            "topology_warning_threshold": topology_warning_threshold,
        },
    )
