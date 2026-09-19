"""UV map lifecycle, seams, unwrap, optimization, and audit tools."""

import asyncio

from typing import Literal

from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from ...app import mcp
from ._shared import call_blender


@mcp.tool()
async def manage_uv_maps(
    ctx: Context,
    object_name: str,
    action: Literal["LIST", "CREATE", "DUPLICATE", "RENAME", "ACTIVATE", "SET_RENDER", "REMOVE"],
    uv_map_name: str | None = None,
    new_name: str | None = None,
    source_uv_map_name: str | None = None,
    confirm: bool = False,
) -> dict:
    """
    List or perform one explicit UV-map lifecycle action on a named mesh.

    CREATE never replaces; DUPLICATE copies loop UVs; RENAME preserves references; ACTIVATE and
    SET_RENDER are distinct. REMOVE requires `confirm=True` and reports material nodes referencing it.
    """
    if action != "LIST" and uv_map_name is None and action not in {"CREATE", "DUPLICATE"}:
        raise ToolError("uv_map_name is required for this action")
    params = {k: v for k, v in locals().items() if k != "ctx"}
    return await asyncio.to_thread(call_blender, "manage_uv_maps", params, [object_name] if action != "LIST" else [])


@mcp.tool()
async def set_uv_seams(
    ctx: Context,
    object_name: str,
    action: Literal["MARK", "CLEAR"],
    edge_indices: list[int] | None = None,
    rule: Literal["BOUNDARY", "SHARP", "ANGLE"] | None = None,
    angle_threshold: float | None = Field(default=None, gt=0, le=3.141592653589793),
) -> dict:
    """
    Mark or clear seams by explicit edge indices or one deterministic topology rule.

    Supply exactly one of `edge_indices` and `rule`; ANGLE requires radians in `angle_threshold`.
    The response returns the exact changed edge indices. Topology indices remain valid.
    """
    if (edge_indices is None) == (rule is None):
        raise ToolError("Supply exactly one of edge_indices or rule")
    if rule == "ANGLE" and angle_threshold is None:
        raise ToolError("angle_threshold is required for ANGLE")
    params = {k: v for k, v in locals().items() if k != "ctx"}
    return await asyncio.to_thread(call_blender, "set_uv_seams", params, [object_name])


@mcp.tool()
async def unwrap_uvs(
    ctx: Context,
    object_name: str,
    uv_map_name: str,
    method: Literal["ANGLE_BASED", "CONFORMAL", "MINIMUM_STRETCH"] = "ANGLE_BASED",
    face_indices: list[int] | None = None,
    create_if_missing: bool = True,
    margin: float = Field(default=0.001, ge=0, le=1),
) -> dict:
    """
    Seam-unwrap all or explicit faces into a named UV map with full context restoration.

    Face indices are base-mesh indices and are validated before mode changes. This tool unwraps
    only; call `optimize_uv_layout` for density normalization, relaxation, and packing.
    """
    params = {k: v for k, v in locals().items() if k != "ctx"}
    return await asyncio.to_thread(call_blender, "unwrap_uvs", params, [object_name])


@mcp.tool()
async def optimize_uv_layout(
    ctx: Context,
    object_name: str,
    uv_map_name: str,
    face_indices: list[int] | None = None,
    average_island_scale: bool = True,
    minimize_stretch_iterations: int = Field(default=10, ge=0, le=1000),
    pack_islands: bool = True,
    rotate: bool = True,
    scale: bool = True,
    margin_method: Literal["SCALED", "ADD", "FRACTION"] = "SCALED",
    margin: float = Field(default=0.001, ge=0, le=1),
    udim_source: Literal["CLOSEST_UDIM", "ACTIVE_UDIM", "ORIGINAL_AABB"] = "CLOSEST_UDIM",
) -> dict:
    """
    Normalize, relax, and pack selected or all UV faces using checked Blender operators.

    Every enabled stage must return FINISHED. Pinned UV behavior follows Blender's operator contract;
    the result reports executed stages and updated layout measurements.
    """
    params = {k: v for k, v in locals().items() if k != "ctx"}
    return await asyncio.to_thread(call_blender, "optimize_uv_layout", params, [object_name])


@mcp.tool()
async def inspect_uv_layout(
    ctx: Context,
    object_name: str,
    uv_map_name: str | None = None,
    overlap_pair_limit: int = Field(default=100, ge=0, le=1000),
) -> dict:
    """
    Audit UV islands, bounds, degeneracy, overlap, orientation, stretch, and density read-only.

    Coordinates are raw UV space and density is UV units per world unit. Overlap testing is bounded;
    check `overlap_truncated` before treating the reported pair list as exhaustive.
    """
    params = {k: v for k, v in locals().items() if k != "ctx"}
    return await asyncio.to_thread(call_blender, "inspect_uv_layout", params)
