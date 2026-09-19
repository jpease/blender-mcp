# ruff: file-ignore[multi-line-summary-second-line]
"""Typed tool for measuring what a baked liquid shot actually produced against fill/spill targets."""

import asyncio

from typing import Annotated, Literal

from mcp.server.fastmcp import Context
from pydantic import Field

from ...app import mcp
from ._shared import _call

OverflowPolicy = Literal["ALLOW", "FORBID"]


@mcp.tool()
async def validate_liquid_result(
    ctx: Context,
    domain_object_name: str,
    modifier_name: str,
    frames: Annotated[list[int], Field(min_length=1, max_length=32)],
    volume_object_names: list[str] | None = None,
    sample_resolution: Annotated[int, Field(ge=4, le=32)] = 16,
    target_fill_fraction: Annotated[float, Field(ge=0.0, le=1.0)] | None = None,
    deadline_frame: int | None = None,
    overflow_policy: OverflowPolicy = "ALLOW",
    timeout_seconds: Annotated[float, Field(gt=0.0, le=300.0)] = 30.0,
    max_preroll_frames: Annotated[int, Field(ge=1, le=10_000)] = 250,
) -> dict:
    """Measure a baked liquid shot's fill/spill/wall-penetration volumes against caller-supplied targets.

    Where sample_liquid_simulation and validate_liquid_setup check that a shot is set up correctly
    before or during baking, this checks what the bake actually produced: per requested frame, it
    grid-samples the evaluated liquid mesh against each container's validation volumes (the interior
    and, when present, spill-catch boxes created by setup_liquid_shot(create_validation_volumes=True))
    to estimate fill volume, fill fraction, spill volume, wall-penetration volume, and volume that
    escaped the container entirely, plus the liquid mesh's connected-component count and manifoldness.

    Omitting volume_object_names auto-discovers every CONTAINER_VOLUME/SPILL_VOLUME tagged with this
    domain's shot id; pass it explicitly for validation volumes built outside setup_liquid_shot.

    Frame stepping follows sample_liquid_simulation's semantics exactly: REPLAY domains are stepped
    one frame at a time from cache_frame_start (bounded by max_preroll_frames), MODULAR/ALL domains
    are jumped to directly but must fall within the already-baked cache range.

    overflow_policy=FORBID turns any measured spill or escaped volume into an ERROR finding.
    target_fill_fraction with deadline_frame turns a fill fraction still below target at or after
    that frame into an ERROR finding. ``passed`` is false whenever any ERROR finding was produced.
    """
    return await asyncio.to_thread(
        _call,
        "validate_liquid_result",
        {
            "domain_object_name": domain_object_name,
            "modifier_name": modifier_name,
            "frames": frames,
            "volume_object_names": volume_object_names,
            "sample_resolution": sample_resolution,
            "target_fill_fraction": target_fill_fraction,
            "deadline_frame": deadline_frame,
            "overflow_policy": overflow_policy,
            "timeout_seconds": timeout_seconds,
            "max_preroll_frames": max_preroll_frames,
        },
    )
