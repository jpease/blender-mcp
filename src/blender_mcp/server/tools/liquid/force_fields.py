"""Typed tools for scoping force fields to a liquid domain."""

import asyncio

from typing import Annotated, Literal

from mcp.server.fastmcp import Context
from pydantic import Field, model_validator

from ...app import mcp
from ._shared import _call, _dump, _StrictModel

FieldType = Literal["FORCE", "WIND", "VORTEX", "TURBULENCE", "DRAG"]
FieldShape = Literal["POINT", "LINE", "PLANE", "SURFACE", "POINTS"]
FalloffType = Literal["CONE", "SPHERE", "TUBE"]


class EffectorWeightsPatch(_StrictModel):
    all: float | None = Field(default=None, ge=-200.0, le=200.0)
    gravity: float | None = Field(default=None, ge=-200.0, le=200.0)
    force: float | None = Field(default=None, ge=-200.0, le=200.0)
    vortex: float | None = Field(default=None, ge=-200.0, le=200.0)
    magnetic: float | None = Field(default=None, ge=-200.0, le=200.0)
    wind: float | None = Field(default=None, ge=-200.0, le=200.0)
    curve_guide: float | None = Field(default=None, ge=-200.0, le=200.0)
    texture: float | None = Field(default=None, ge=-200.0, le=200.0)
    harmonic: float | None = Field(default=None, ge=-200.0, le=200.0)
    charge: float | None = Field(default=None, ge=-200.0, le=200.0)
    lennardjones: float | None = Field(default=None, ge=-200.0, le=200.0)
    boid: float | None = Field(default=None, ge=-200.0, le=200.0)
    turbulence: float | None = Field(default=None, ge=-200.0, le=200.0)
    drag: float | None = Field(default=None, ge=-200.0, le=200.0)
    smokeflow: float | None = Field(default=None, ge=-200.0, le=200.0)


class LiquidForceFieldSpec(_StrictModel):
    object_name: str
    field_type: FieldType
    create_if_missing: bool = False
    location: tuple[float, float, float] = (0.0, 0.0, 0.0)
    rotation_euler: tuple[float, float, float] = (0.0, 0.0, 0.0)
    strength: float = 0.0
    shape: FieldShape = "POINT"
    falloff_type: FalloffType = "SPHERE"
    noise: float = Field(default=0.0, ge=0.0, le=10.0)
    seed: int = Field(default=1, ge=1, le=128)
    use_min_distance: bool = False
    distance_min: float = Field(default=0.0, ge=0.0)
    use_max_distance: bool = False
    distance_max: float = Field(default=0.0, ge=0.0)

    @model_validator(mode="after")
    def validate_distances(self) -> "LiquidForceFieldSpec":
        if self.use_max_distance and self.use_min_distance and self.distance_max < self.distance_min:
            raise ValueError("distance_max must be >= distance_min")
        return self


@mcp.tool()
async def configure_liquid_force_fields(
    ctx: Context,
    scene_name: str,
    domain_object_name: str,
    modifier_name: str,
    fields: Annotated[list[LiquidForceFieldSpec], Field(min_length=1)],
    force_collection_name: str,
    create_collection: bool = False,
    weights: EffectorWeightsPatch | None = None,
) -> dict:
    """Create or configure bounded force fields and scope their influence to one liquid domain."""
    return await asyncio.to_thread(
        _call,
        "configure_liquid_force_fields",
        {
            "scene_name": scene_name,
            "domain_object_name": domain_object_name,
            "modifier_name": modifier_name,
            "fields": [item.model_dump() for item in fields],
            "force_collection_name": force_collection_name,
            "create_collection": create_collection,
            "weights": _dump(weights),
        },
        [domain_object_name, *[item.object_name for item in fields]],
    )
