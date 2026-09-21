"""Deterministic rigid-body debris generation tools."""

from typing import Annotated, Literal

from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .._dispatch import call_blender
from .inspection_and_setup import RigidBodySettingsPatch, Vector3, mcp


class DebrisSourceSpec(BaseModel):
    """A reusable mesh source and its relative selection weight."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    object_name: str = Field(min_length=1)
    weight: float = Field(default=1.0, gt=0.0)


class DebrisRegion(BaseModel):
    """A bounded world-space region used to place debris."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    shape: Literal["BOX", "SPHERE", "COLLECTION_BOUNDS"]
    minimum: Vector3 | None = None
    maximum: Vector3 | None = None
    center: Vector3 | None = None
    radius: float | None = Field(default=None, gt=0.0)
    collection_name: str | None = None

    @model_validator(mode="after")
    def validate_shape_fields(self) -> "DebrisRegion":
        if self.shape == "BOX":
            if self.minimum is None or self.maximum is None:
                raise ValueError("BOX regions require minimum and maximum")
            if any(low >= high for low, high in zip(self.minimum, self.maximum, strict=True)):
                raise ValueError("Every BOX minimum component must be less than maximum")
        elif self.shape == "SPHERE":
            if self.center is None or self.radius is None:
                raise ValueError("SPHERE regions require center and radius")
        elif not self.collection_name:
            raise ValueError("COLLECTION_BOUNDS regions require collection_name")
        allowed = {
            "BOX": {"minimum", "maximum"},
            "SPHERE": {"center", "radius"},
            "COLLECTION_BOUNDS": {"collection_name"},
        }[self.shape]
        supplied = {
            name
            for name in ("minimum", "maximum", "center", "radius", "collection_name")
            if getattr(self, name) is not None
        }
        if supplied - allowed:
            raise ValueError(f"{self.shape} regions do not accept {sorted(supplied - allowed)}")
        return self


class DebrisTransformRange(BaseModel):
    """Validated random rotation and uniform-scale bounds."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    rotation_min_radians: Vector3 = (0.0, 0.0, 0.0)
    rotation_max_radians: Vector3 = (0.0, 0.0, 0.0)
    uniform_scale_min: float = Field(default=1.0, gt=0.0)
    uniform_scale_max: float = Field(default=1.0, gt=0.0)

    @model_validator(mode="after")
    def validate_ranges(self) -> "DebrisTransformRange":
        if any(low > high for low, high in zip(self.rotation_min_radians, self.rotation_max_radians, strict=True)):
            raise ValueError("rotation_min_radians must not exceed rotation_max_radians")
        if self.uniform_scale_min > self.uniform_scale_max:
            raise ValueError("uniform_scale_min must not exceed uniform_scale_max")
        return self


@mcp.tool()
async def create_rigid_body_debris_field(
    ctx: Context,
    scene_name: str,
    field_name: str,
    sources: Annotated[list[DebrisSourceSpec], Field(min_length=1, max_length=32)],
    count: Annotated[int, Field(ge=1, le=500)],
    seed: int,
    region: DebrisRegion,
    density: Annotated[float, Field(gt=0.0)],
    transform_range: DebrisTransformRange | None = None,
    collection_name: str = "Rigid Body Debris",
    collision_shape: Literal["BOX", "SPHERE", "CAPSULE", "CYLINDER", "CONE", "CONVEX_HULL"] = "CONVEX_HULL",
    collision_layers: Annotated[
        list[Annotated[int, Field(ge=1, le=20)]] | None, Field(min_length=1, max_length=20)
    ] = None,
    start_deactivated: bool = True,
    settings: RigidBodySettingsPatch | None = None,
    confirm_delete_baked_cache: bool = False,
) -> dict:
    """
    Create deterministic linked-mesh debris objects with bounded count and placement.

    Every created debris object links (shares) its mesh data with the DebrisSourceSpec it was
    sampled from rather than copying it - editing that source object's mesh afterward changes the
    appearance of every debris instance sampled from it. `count` objects are sampled from `sources`
    weighted by each source's `weight`, then placed within `region` (a BOX, SPHERE, or
    COLLECTION_BOUNDS volume) using `seed` for deterministic, repeatable placement. Requires
    scene_name to already have a rigid body world set up. Rejects with confirm_delete_baked_cache=False
    if that world already has a baked simulation cache.
    """
    layers = collision_layers or [3]
    source_names = [source.object_name for source in sources]
    if len(source_names) != len(set(source_names)):
        raise ToolError("Debris source object names must be unique")
    if len(layers) != len(set(layers)) or any(not 1 <= layer <= 20 for layer in layers):
        raise ToolError("collision_layers must contain unique values in [1, 20]")
    if settings is not None and settings.mass is not None:
        raise ToolError("settings.mass is incompatible with density-derived debris mass")
    if settings is not None and settings.type not in {None, "ACTIVE"}:
        raise ToolError("Debris settings.type must be ACTIVE when supplied")
    payload = settings.model_dump(exclude_none=True, exclude_unset=True) if settings else {}
    return await call_blender(
        "create_rigid_body_debris_field",
        {
            "scene_name": scene_name,
            "field_name": field_name,
            "sources": [source.model_dump() for source in sources],
            "count": count,
            "seed": seed,
            "region": region.model_dump(exclude_none=True),
            "density": density,
            "transform_range": (transform_range or DebrisTransformRange()).model_dump(),
            "collection_name": collection_name,
            "collision_shape": collision_shape,
            "collision_layers": layers,
            "start_deactivated": start_deactivated,
            "settings": payload,
            "confirm_delete_baked_cache": confirm_delete_baked_cache,
        },
        changed_objects=source_names,
    )
