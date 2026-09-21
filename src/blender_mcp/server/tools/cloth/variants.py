# MCP tool signatures intentionally expose more than five keyword arguments so
# agents receive precise JSON schemas instead of opaque catch-all dictionaries.
"""Typed tool for duplicating a cloth setup with explicit sharing policies."""

from typing import Annotated, Literal

from mcp.server.fastmcp import Context
from pydantic import Field

from ...app import mcp
from .._dispatch import call_blender

VariantDataPolicy = Literal["COPY", "SHARE"]
VariantDependencyPolicy = Literal["DUPLICATE", "SHARE"]
RenderSurfacePolicy = Literal["DUPLICATE", "OMIT"]


@mcp.tool()
async def duplicate_cloth_setup_variant(
    ctx: Context,
    source_object_name: str,
    variant_object_name: str,
    variant_collection_name: str,
    name_suffix: Annotated[str, Field(min_length=1)],
    mesh_data_policy: VariantDataPolicy,
    material_policy: VariantDataPolicy,
    animation_policy: VariantDataPolicy,
    collider_policy: VariantDependencyPolicy,
    force_field_policy: VariantDependencyPolicy,
    render_surface_policy: RenderSurfacePolicy,
    cache_directory: str | None = None,
) -> dict:
    """
    Duplicate a cloth setup with explicit sharing policies and independent point caches.

    Vertex groups copy with the object. Shape keys follow ``mesh_data_policy``; material slots and
    actions follow their own policies. Collision/effector dependencies and render surfaces are
    discovered from the source setup, then either shared or duplicated as explicitly requested.
    """
    return await call_blender(
        "duplicate_cloth_setup_variant",
        {
            "source_object_name": source_object_name,
            "variant_object_name": variant_object_name,
            "variant_collection_name": variant_collection_name,
            "name_suffix": name_suffix,
            "mesh_data_policy": mesh_data_policy,
            "material_policy": material_policy,
            "animation_policy": animation_policy,
            "collider_policy": collider_policy,
            "force_field_policy": force_field_policy,
            "render_surface_policy": render_surface_policy,
            "cache_directory": cache_directory,
        },
        changed_objects=[source_object_name],
    )
