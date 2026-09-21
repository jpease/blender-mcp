# MCP tool signatures intentionally expose more than five keyword arguments so
# agents receive precise JSON schemas instead of opaque catch-all dictionaries.
"""Typed tools for managing a cloth's point cache and removing cloth-related components."""

from typing import Annotated, Literal

from mcp.server.fastmcp import Context
from pydantic import Field

from ...app import mcp
from .._dispatch import call_blender
from ._shared import _dump, _StrictModel

CacheAction = Literal["INSPECT", "CONFIGURE", "BAKE", "BAKE_FROM_CACHE", "FREE"]
ClothComponentType = Literal[
    "CLOTH_MODIFIER",
    "COLLISION_MODIFIER",
    "ATTACHMENT_MODIFIER",
    "COLLISION_COLLECTION_MEMBERSHIP",
]


class PointCachePatch(_StrictModel):
    """Writable PointCache configuration fields."""

    frame_start: Annotated[int, Field(ge=0)] | None = None
    frame_end: Annotated[int, Field(ge=0)] | None = None
    frame_step: Annotated[int, Field(ge=1)] | None = None
    name: Annotated[str, Field(min_length=1)] | None = None
    index: Annotated[int, Field(ge=0)] | None = None
    use_disk_cache: bool | None = None
    use_external: bool | None = None
    use_library_path: bool | None = None
    filepath: Annotated[str, Field(min_length=1)] | None = None


@mcp.tool()
async def manage_cloth_cache(
    ctx: Context,
    object_name: str,
    modifier_name: str,
    action: CacheAction = "INSPECT",
    patch: PointCachePatch | None = None,
    confirm_bake: bool = False,
    confirm_free_bake: bool = False,
    confirm_external_overwrite: bool = False,
    max_bake_frames: Annotated[int, Field(ge=1)] = 250,
) -> dict:
    """
    Inspect, configure, bake, mark baked-from-cache, or free one exact Cloth point cache.

    BAKE is synchronous and requires confirmation; requests exceeding ``max_bake_frames`` are
    refused so this command never launches an unbounded job. FREE requires separate confirmation.
    Baking into or freeing an external cache directory containing files also requires explicit
    overwrite/deletion confirmation.
    """
    return await call_blender(
        "manage_cloth_cache",
        {
            "object_name": object_name,
            "modifier_name": modifier_name,
            "action": action,
            "patch": _dump(patch),
            "confirm_bake": confirm_bake,
            "confirm_free_bake": confirm_free_bake,
            "confirm_external_overwrite": confirm_external_overwrite,
            "max_bake_frames": max_bake_frames,
        },
        changed_objects=[] if action == "INSPECT" else [object_name],
    )


@mcp.tool()
async def remove_cloth_components(
    ctx: Context,
    object_name: str,
    component_type: ClothComponentType,
    modifier_name: str | None = None,
    collection_name: str | None = None,
    confirm_baked_removal: bool = False,
    confirm_affected_bakes: bool = False,
) -> dict:
    """
    Remove one exact cloth-related component while retaining unrelated data and cache files.

    Modifier targets require their exact name; collection membership requires its exact collection.
    Vertex groups, meshes, materials, controls, and external cache files are never deleted. Baked
    cloth or affected baked dependencies require the corresponding explicit confirmation flag.
    """
    return await call_blender(
        "remove_cloth_components",
        {
            "object_name": object_name,
            "component_type": component_type,
            "modifier_name": modifier_name,
            "collection_name": collection_name,
            "confirm_baked_removal": confirm_baked_removal,
            "confirm_affected_bakes": confirm_affected_bakes,
        },
        changed_objects=[object_name],
    )
