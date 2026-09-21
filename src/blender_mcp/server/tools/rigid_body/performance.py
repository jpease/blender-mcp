"""Bounded rigid-body stability and performance analysis tools."""

from typing import Annotated

from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from .._dispatch import call_blender
from .inspection_and_setup import mcp


@mcp.tool()
async def analyze_rigid_body_performance(
    ctx: Context,
    scene_name: str,
    object_names: Annotated[list[str], Field(min_length=1, max_length=500)],
    sample_frames: Annotated[list[int] | None, Field(max_length=20)] = None,
    maximum_pair_checks: Annotated[int, Field(ge=1, le=10_000)] = 512,
    triangle_warning_threshold: Annotated[int, Field(ge=100, le=10_000_000)] = 50_000,
    timeout_seconds: Annotated[float, Field(gt=0.0, le=120.0)] = 20.0,
) -> dict:
    """
    Report structural costs and optional whole-frame evaluation timing without inventing Bullet profiler data.

    Always reports structural costs for object_names: triangle counts (flagged against
    triangle_warning_threshold) and an estimated collision-pair count (bounded by
    maximum_pair_checks). When sample_frames is given, additionally times how long evaluating the
    scene at each of those frames actually takes, bounded by timeout_seconds; omit sample_frames to
    skip that timing pass. This never fabricates Bullet (Blender's rigid-body physics engine)
    internal profiler numbers that aren't available through the API.
    """
    frames = sample_frames or []
    if len(object_names) != len(set(object_names)):
        raise ToolError("object_names must be unique")
    if frames != sorted(set(frames)):
        raise ToolError("sample_frames must be unique and ordered")
    return await call_blender(
        "analyze_rigid_body_performance",
        {
            "scene_name": scene_name,
            "object_names": object_names,
            "sample_frames": frames,
            "maximum_pair_checks": maximum_pair_checks,
            "triangle_warning_threshold": triangle_warning_threshold,
            "timeout_seconds": timeout_seconds,
        },
        changed_objects=object_names,
    )
