"""Read-only end-to-end PBR asset validation."""

from typing import Literal

from mcp.server.fastmcp import Context
from pydantic import Field

from ...app import mcp
from .._dispatch import call_blender


@mcp.tool()
async def validate_pbr_asset(
    ctx: Context,
    object_names: list[str] | None = None,
    material_names: list[str] | None = None,
    profile: Literal["BLENDER_CYCLES", "BLENDER_EEVEE", "BLENDER_BOTH"] = "BLENDER_BOTH",
    overlap_pair_limit: int = Field(default=100, ge=0, le=1000),
) -> dict:
    """
    Return evidence-backed PBR readiness findings without changing the scene.

    Scope by objects, materials, or both. Findings cover slots, output paths, images, colorspaces,
    normal conversion, alpha, UVs, dirty storage, and engine-specific displacement/transmission risk.
    Each finding includes severity, evidence, and remediation; truncation is disclosed.
    """
    params = {k: v for k, v in locals().items() if k != "ctx"}
    return await call_blender("validate_pbr_asset", params)
