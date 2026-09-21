"""Agent-facing discovery, graph inspection, evaluation, and validation tools."""

from typing import Annotated, Literal

from mcp.server.fastmcp import Context
from pydantic import Field

from ...app import mcp
from .._dispatch import call_blender


@mcp.tool()
async def list_procedural_systems(
    ctx: Context,
    limit: Annotated[int, Field(ge=1, le=200)] = 50,
    offset: Annotated[int, Field(ge=0)] = 0,
    include_orphans: bool = True,
) -> dict:
    """
    Inventory reusable Geometry Nodes groups and every object modifier that uses them.

    Use this before creating or attaching a procedural system. Results include sharing,
    asset/library state, execution-role flags, interface summaries, and MCP ownership tags.
    Continue with ``next_offset`` while ``truncated`` is true.
    """
    return await call_blender(
        "list_procedural_systems", {"limit": limit, "offset": offset, "include_orphans": include_orphans}
    )


@mcp.tool()
async def get_geometry_node_graph(
    ctx: Context,
    node_group_name: str,
    sections: list[Literal["IDENTITY", "INTERFACE", "NODES", "LINKS", "MODIFIERS", "WARNINGS"]] | None = None,
    limit: Annotated[int, Field(ge=1, le=500)] = 100,
    offset: Annotated[int, Field(ge=0)] = 0,
) -> dict:
    """
    Inspect one Geometry Nodes graph with stable socket identifiers and modifier overrides.

    Request only the sections needed for the next edit. Node and socket display names are
    descriptive only; use returned identifiers and indices when preparing a graph patch.
    """
    return await call_blender(
        "get_geometry_node_graph",
        {"node_group_name": node_group_name, "sections": sections, "limit": limit, "offset": offset},
    )


@mcp.tool()
async def get_geometry_node_type_info(
    ctx: Context,
    bl_idname: str | None = None,
    search: str | None = None,
    category: str | None = None,
    limit: Annotated[int, Field(ge=1, le=200)] = 50,
    offset: Annotated[int, Field(ge=0)] = 0,
) -> dict:
    """
    Discover Geometry Nodes node types supported by the connected Blender runtime.

    Supply ``bl_idname`` for an exact schema or ``search``/``category`` to browse. Dynamic
    sockets are reported from a disposable runtime node, so use this instead of remembered
    socket layouts from another Blender version.
    """
    if bl_idname is None and search is None and category is None:
        search = ""
    return await call_blender(
        "get_geometry_node_type_info",
        {
            "bl_idname": bl_idname,
            "search": search,
            "category": category,
            "limit": limit,
            "offset": offset,
        },
    )


@mcp.tool()
async def evaluate_procedural_geometry(
    ctx: Context,
    object_name: str,
    frame: int | None = None,
    instance_limit: Annotated[int, Field(ge=1, le=5000)] = 500,
) -> dict:
    """
    Inspect the evaluated result of an object's live procedural stack without applying it.

    Returns world-space bounds, mesh counts, materials, named attributes, component limits,
    and a bounded dependency-graph instance summary at the requested frame.
    """
    return await call_blender(
        "evaluate_procedural_geometry", {"object_name": object_name, "frame": frame, "instance_limit": instance_limit}
    )


@mcp.tool()
async def validate_geometry_node_graph(
    ctx: Context,
    node_group_name: str,
    object_names: list[str] | None = None,
    topology_warning_threshold: Annotated[int, Field(ge=1)] = 1_000_000,
) -> dict:
    """
    Check a procedural graph and its live users for production risks without changing data.

    Findings have INFO, WARNING, or ERROR severity and identify the affected group, node,
    socket, modifier, or object plus a concrete remediation. A clean evaluation is not claimed
    as artistic correctness.
    """
    return await call_blender(
        "validate_geometry_node_graph",
        {
            "node_group_name": node_group_name,
            "object_names": object_names,
            "topology_warning_threshold": topology_warning_threshold,
        },
    )
