"""Typed tools for keyframing curated cloth-related RNA properties."""

import asyncio

from typing import Annotated, Literal

from mcp.server.fastmcp import Context
from pydantic import Field

from ...app import mcp
from ._shared import _call, _StrictModel

AnimationOwner = Literal[
    "CLOTH_SETTINGS",
    "EFFECTOR_WEIGHTS",
    "FIELD_SETTINGS",
    "COLLIDER_SETTINGS",
    "SHAPE_KEY",
    "MODIFIER",
    "OBJECT",
]
KeyframePolicy = Literal["INSERT_ONLY", "REPLACE_EXISTING"]
Interpolation = Literal["CONSTANT", "LINEAR", "BEZIER"]


class ClothAnimationKeyframe(_StrictModel):
    """One curated RNA property value at an exact frame."""

    owner: AnimationOwner
    property_name: Annotated[str, Field(min_length=1)]
    value: bool | int | float | tuple[float, float, float] | tuple[float, float, float, float]
    frame: float
    target_name: Annotated[str, Field(min_length=1)] | None = None
    array_index: int = Field(default=-1, ge=-1, le=3)
    interpolation: Interpolation = "BEZIER"


@mcp.tool()
async def animate_cloth_parameters(
    ctx: Context,
    object_name: str,
    keyframes: Annotated[list[ClothAnimationKeyframe], Field(min_length=1)],
    cloth_modifier_name: str | None = None,
    policy: KeyframePolicy = "INSERT_ONLY",
) -> dict:
    """
    Insert exact keyframes on curated cloth-related RNA owners without touching unrelated curves.

    ``target_name`` selects a shape key or modifier for those owner kinds. INSERT_ONLY rejects an
    existing key at the same property/index/frame; REPLACE_EXISTING updates only that exact key.
    FIELD_SETTINGS currently permits force-field strength. Raw vertex-group membership is
    intentionally not animatable through this tool.
    """
    return await asyncio.to_thread(
        _call,
        "animate_cloth_parameters",
        {
            "object_name": object_name,
            "cloth_modifier_name": cloth_modifier_name,
            "keyframes": [item.model_dump() for item in keyframes],
            "policy": policy,
        },
        [object_name],
    )
