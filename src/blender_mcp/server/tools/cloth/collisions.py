# MCP tool signatures intentionally expose more than five keyword arguments so
# agents receive precise JSON schemas instead of opaque catch-all dictionaries.
"""Typed tools for cloth-side collisions and collider registration."""

import asyncio

from typing import Annotated, Literal

from mcp.server.fastmcp import Context
from pydantic import Field

from ...app import mcp
from ._shared import _call, _dump, _StrictModel

ExistingPolicy = Literal["ERROR", "REUSE"]


class ClothCollisionPatch(_StrictModel):
    """Allowlisted cloth-side object and self-collision controls."""

    use_collision: bool | None = None
    collision_quality: Annotated[int, Field(ge=1)] | None = None
    distance_min: Annotated[float, Field(ge=0)] | None = None
    impulse_clamp: Annotated[float, Field(ge=0)] | None = None
    damping: Annotated[float, Field(ge=0)] | None = None
    friction: Annotated[float, Field(ge=0)] | None = None
    collection_name: Annotated[str, Field(min_length=1)] | None = None
    clear_collection: bool = False
    vertex_group_object_collisions: Annotated[str, Field(min_length=1)] | None = None
    use_self_collision: bool | None = None
    self_distance_min: Annotated[float, Field(ge=0)] | None = None
    self_friction: Annotated[float, Field(ge=0)] | None = None
    self_impulse_clamp: Annotated[float, Field(ge=0)] | None = None
    vertex_group_self_collisions: Annotated[str, Field(min_length=1)] | None = None


class ClothColliderPatch(_StrictModel):
    """CollisionSettings fields documented as cloth-relevant in Blender 5.1."""

    use: bool | None = None
    thickness_outer: Annotated[float, Field(ge=0)] | None = None
    cloth_friction: Annotated[float, Field(ge=0)] | None = None
    damping: Annotated[float, Field(ge=0)] | None = None
    use_culling: bool | None = None
    use_normal: bool | None = None


class ClothColliderRegistration(_StrictModel):
    """Register one collider through an explicit cloth and collection relationship."""

    cloth_object_name: Annotated[str, Field(min_length=1)]
    cloth_modifier_name: Annotated[str, Field(min_length=1)]
    collection_name: Annotated[str, Field(min_length=1)]


@mcp.tool()
async def add_cloth_collider(
    ctx: Context,
    object_name: str,
    modifier_name: str = "Collision",
    existing_policy: ExistingPolicy = "ERROR",
    settings: ClothColliderPatch | None = None,
    registrations: list[ClothColliderRegistration] | None = None,
) -> dict:
    """
    Add or explicitly reuse Collision physics on a named mesh or curve.

    Each registration links the collider into an existing collection and assigns that collection as
    the named cloth setup's collision scope; existing object collection links are preserved. Baked
    affected cloth caches are rejected, and request-created modifiers/links are rolled back on error.
    """
    return await asyncio.to_thread(
        _call,
        "add_cloth_collider",
        {
            "object_name": object_name,
            "modifier_name": modifier_name,
            "existing_policy": existing_policy,
            "settings": _dump(settings),
            "registrations": [item.model_dump() for item in registrations or []],
        },
        [object_name],
    )
