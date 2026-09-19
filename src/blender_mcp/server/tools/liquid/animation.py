"""Typed tools for keying liquid flow settings over time."""

import asyncio

from typing import Annotated, Literal

from mcp.server.fastmcp import Context
from pydantic import Field, model_validator

from ...app import mcp
from ._shared import _call, _StrictModel

Interpolation = Literal["CONSTANT", "LINEAR", "BEZIER"]
AnimationPolicy = Literal["INSERT_ONLY", "REPLACE_EXISTING"]


class LiquidFlowKeyframe(_StrictModel):
    frame: float = Field(ge=-1_000_000.0, le=1_000_000.0)
    use_inflow: bool | None = None
    use_initial_velocity: bool | None = None
    velocity_factor: float | None = None
    velocity_normal: float | None = None
    velocity_random: float | None = Field(default=None, ge=0.0)
    interpolation: Interpolation = "CONSTANT"

    @model_validator(mode="after")
    def require_value(self) -> "LiquidFlowKeyframe":
        values = self.model_dump(exclude={"frame", "interpolation"}, exclude_none=True)
        if len(values) != 1:
            raise ValueError("Each record must key exactly one flow property")
        return self


@mcp.tool()
async def animate_liquid_flow(
    ctx: Context,
    object_name: str,
    modifier_name: str,
    domain_object_name: str,
    keyframes: Annotated[list[LiquidFlowKeyframe], Field(min_length=1)],
    policy: AnimationPolicy = "INSERT_ONLY",
    subframes: Annotated[int, Field(ge=0, le=200)] | None = None,
) -> dict:
    """Key liquid flow settings with explicit merge policy and per-key interpolation."""
    return await asyncio.to_thread(
        _call,
        "animate_liquid_flow",
        {
            "object_name": object_name,
            "modifier_name": modifier_name,
            "domain_object_name": domain_object_name,
            "keyframes": [item.model_dump(exclude_none=True) for item in keyframes],
            "policy": policy,
            "subframes": subframes,
        },
        [object_name, domain_object_name],
    )
