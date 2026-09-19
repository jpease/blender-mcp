"""Typed tools for creating liquid effector and domain guides."""

import asyncio

from typing import Annotated, Literal

from mcp.server.fastmcp import Context
from pydantic import Field

from ...app import mcp
from ._shared import _call
from .inspection_and_setup import ExistingPolicy, GuideMode

GuideSource = Literal["EFFECTOR", "DOMAIN"]


@mcp.tool()
async def create_liquid_guide(
    ctx: Context,
    domain_object_name: str,
    domain_modifier_name: str,
    guide_object_name: str,
    source: GuideSource = "EFFECTOR",
    guide_modifier_name: str = "Liquid Guide",
    existing_policy: ExistingPolicy = "ERROR",
    guide_mode: GuideMode = "OVERRIDE",
    velocity_factor: float = 1.0,
    guide_parent_domain_object_name: str | None = None,
    guide_collection_name: str | None = None,
    cache_frame_start: int | None = None,
    cache_frame_end: int | None = None,
    guide_alpha: Annotated[float, Field(ge=1.0, le=100.0)] | None = None,
    guide_beta: Annotated[int, Field(ge=1, le=50)] | None = None,
    guide_vel_factor: Annotated[float, Field(ge=0.0, le=100.0)] | None = None,
) -> dict:
    """Create an effector guide or connect one liquid domain as another domain's guide source."""
    return await asyncio.to_thread(
        _call,
        "create_liquid_guide",
        {
            "domain_object_name": domain_object_name,
            "domain_modifier_name": domain_modifier_name,
            "guide_object_name": guide_object_name,
            "source": source,
            "guide_modifier_name": guide_modifier_name,
            "existing_policy": existing_policy,
            "guide_mode": guide_mode,
            "velocity_factor": velocity_factor,
            "guide_parent_domain_object_name": guide_parent_domain_object_name,
            "guide_collection_name": guide_collection_name,
            "cache_frame_start": cache_frame_start,
            "cache_frame_end": cache_frame_end,
            "guide_alpha": guide_alpha,
            "guide_beta": guide_beta,
            "guide_vel_factor": guide_vel_factor,
        },
        [name for name in [domain_object_name, guide_object_name, guide_parent_domain_object_name] if name],
    )
