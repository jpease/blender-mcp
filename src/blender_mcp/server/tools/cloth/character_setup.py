# MCP tool signatures intentionally expose more than five keyword arguments so
# agents receive precise JSON schemas instead of opaque catch-all dictionaries.
"""Typed tool for assembling a non-destructive character cloth garment stack."""

import asyncio

from typing import Annotated

from mcp.server.fastmcp import Context
from pydantic import Field

from ...app import mcp
from ._shared import _call, _dump
from .collisions import ClothColliderPatch, ClothCollisionPatch
from .inspection_and_setup import ExistingPolicy
from .material_and_solver import ClothMaterialPatch, ClothSolverPatch


@mcp.tool()
async def create_character_cloth_setup(
    ctx: Context,
    garment_object_name: str,
    armature_object_name: str,
    body_collider_object_names: list[str],
    pin_group_name: str,
    collision_collection_name: str,
    cloth_modifier_name: str = "Cloth",
    armature_modifier_name: str = "Cloth Armature",
    collider_modifier_name: str = "Cloth Collision",
    subdivision_modifier_name: str = "Cloth Subdivision",
    solidify_modifier_name: str = "Cloth Solidify",
    existing_policy: ExistingPolicy = "ERROR",
    material: ClothMaterialPatch | None = None,
    solver: ClothSolverPatch | None = None,
    collisions: ClothCollisionPatch | None = None,
    collider_settings: ClothColliderPatch | None = None,
    add_subdivision: bool = False,
    subdivision_levels: Annotated[int, Field(ge=0, le=6)] = 1,
    add_solidify: bool = False,
    solidify_thickness: float = 0.002,
    rest_frame: Annotated[int, Field(ge=0)] = 1,
    cache_frame_start: Annotated[int, Field(ge=0)] = 1,
    cache_frame_end: Annotated[int, Field(ge=0)] = 250,
) -> dict:
    """
    Assemble a non-destructive garment, armature, collider, and finishing stack.

    All assets, the pin group, and collision collection must be explicitly named. No weights,
    collision proxies, or anatomy are inferred. Deformation is placed before Cloth and optional
    render-only Subdivision/Solidify modifiers after it; no modifier is applied. ``cache_frame_end``
    must not precede ``cache_frame_start``.
    """
    return await asyncio.to_thread(
        _call,
        "create_character_cloth_setup",
        {
            "garment_object_name": garment_object_name,
            "armature_object_name": armature_object_name,
            "body_collider_object_names": body_collider_object_names,
            "pin_group_name": pin_group_name,
            "collision_collection_name": collision_collection_name,
            "cloth_modifier_name": cloth_modifier_name,
            "armature_modifier_name": armature_modifier_name,
            "collider_modifier_name": collider_modifier_name,
            "subdivision_modifier_name": subdivision_modifier_name,
            "solidify_modifier_name": solidify_modifier_name,
            "existing_policy": existing_policy,
            "material": _dump(material),
            "solver": _dump(solver),
            "collisions": _dump(collisions),
            "collider_settings": _dump(collider_settings),
            "add_subdivision": add_subdivision,
            "subdivision_levels": subdivision_levels,
            "add_solidify": add_solidify,
            "solidify_thickness": solidify_thickness,
            "rest_frame": rest_frame,
            "cache_frame_start": cache_frame_start,
            "cache_frame_end": cache_frame_end,
        },
        [garment_object_name, armature_object_name, *body_collider_object_names],
    )
