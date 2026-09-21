# MCP tool signatures intentionally expose more than five keyword arguments so
# agents receive precise JSON schemas instead of opaque catch-all dictionaries.
"""Typed tools for inspecting cloth systems and adding a Cloth modifier."""

from typing import Annotated, Literal

from mcp.server.fastmcp import Context
from pydantic import Field

from ...app import mcp
from .._dispatch import call_blender
from ._shared import _dump
from .collisions import ClothCollisionPatch
from .material_and_solver import ClothMaterialPatch, ClothSolverPatch, MaterialPreset

ExistingPolicy = Literal["ERROR", "REUSE"]


@mcp.tool()
async def get_cloth_simulation_info(
    ctx: Context,
    scene_name: str,
    collection_name: str | None = None,
    object_limit: Annotated[int, Field(ge=1, le=200)] = 25,
    object_offset: Annotated[int, Field(ge=0)] = 0,
    dependency_limit: Annotated[int, Field(ge=1, le=500)] = 100,
    dependency_offset: Annotated[int, Field(ge=0)] = 0,
) -> dict:
    """
    Inspect cloth systems in one explicit scene or collection without evaluating another frame.

    Use the two independent offsets to continue the object and dependency pages. Eligible-collider
    records distinguish active relationships from in-scope but disabled collision; objects excluded
    by a cloth collision collection are never reported as affecting that cloth.
    """
    return await call_blender(
        "get_cloth_simulation_info",
        {
            "scene_name": scene_name,
            "collection_name": collection_name,
            "object_limit": object_limit,
            "object_offset": object_offset,
            "dependency_limit": dependency_limit,
            "dependency_offset": dependency_offset,
        },
    )


@mcp.tool()
async def get_cloth_object_info(
    ctx: Context,
    object_name: str,
    vertex_group_limit: Annotated[int, Field(ge=1, le=500)] = 50,
    vertex_group_offset: Annotated[int, Field(ge=0)] = 0,
) -> dict:
    """
    Inspect one named cloth or collider before planning a mutation.

    Base mesh statistics and vertex indices are object-local; evaluated counts include the live
    dependency graph. Vertex groups are separately paginated and can change after topology edits.
    """
    return await call_blender(
        "get_cloth_object_info",
        {
            "object_name": object_name,
            "vertex_group_limit": vertex_group_limit,
            "vertex_group_offset": vertex_group_offset,
        },
    )


@mcp.tool()
async def add_cloth_simulation(
    ctx: Context,
    object_name: str,
    modifier_name: str = "Cloth",
    modifier_index: int | None = None,
    existing_policy: ExistingPolicy = "ERROR",
    cache_frame_start: Annotated[int, Field(ge=0)] = 1,
    cache_frame_end: Annotated[int, Field(ge=0)] = 250,
    collision_collection_name: str | None = None,
    preset: MaterialPreset | None = None,
    material: ClothMaterialPatch | None = None,
    solver: ClothSolverPatch | None = None,
    collisions: ClothCollisionPatch | None = None,
) -> dict:
    """
    Add a named live Cloth modifier to an explicit nonempty mesh, without baking or applying it.

    ``existing_policy='ERROR'`` is the safe default. ``REUSE`` targets only a same-name Cloth
    modifier and still refuses baked caches. All supplied patches and the cache range are validated,
    including that ``cache_frame_end`` is not before ``cache_frame_start``; failure removes a newly
    created modifier or restores the reused modifier's touched properties.
    """
    return await call_blender(
        "add_cloth_simulation",
        {
            "object_name": object_name,
            "modifier_name": modifier_name,
            "modifier_index": modifier_index,
            "existing_policy": existing_policy,
            "cache_frame_start": cache_frame_start,
            "cache_frame_end": cache_frame_end,
            "collision_collection_name": collision_collection_name,
            "preset": preset,
            "material": _dump(material),
            "solver": _dump(solver),
            "collisions": _dump(collisions),
        },
        changed_objects=[object_name],
    )
