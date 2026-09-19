"""Geometry Nodes tool execution and asset publication tools."""

import asyncio

from typing import Any, Literal

from mcp.server.fastmcp import Context

from ...app import mcp
from ._shared import call_geometry_nodes


@mcp.tool()
async def run_geometry_nodes_tool(
    ctx: Context,
    node_group_name: str,
    object_names: list[str],
    mode: Literal["OBJECT", "EDIT", "SCULPT", "PAINT"] = "OBJECT",
    vertex_indices: list[int] | None = None,
    edge_indices: list[int] | None = None,
    face_indices: list[int] | None = None,
    inputs: dict[str, Any] | None = None,
    confirm_destructive: bool = False,
) -> dict:
    """
    Run an existing local Geometry Nodes tool on explicit objects and element selections.

    This changes base geometry and therefore requires confirmation. The group must be marked as a
    tool and expose a valid custom operator identifier; object applicability and every index are
    validated before invocation. Refresh topology indices after success.
    """
    if not confirm_destructive:
        raise ValueError("confirm_destructive=True is required to run a Geometry Nodes tool")
    return await asyncio.to_thread(
        call_geometry_nodes,
        "run_geometry_nodes_tool",
        {
            "node_group_name": node_group_name,
            "object_names": object_names,
            "mode": mode,
            "vertex_indices": vertex_indices,
            "edge_indices": edge_indices,
            "face_indices": face_indices,
            "inputs": inputs or {},
            "confirm_destructive": confirm_destructive,
        },
        changed_objects=object_names,
    )


@mcp.tool()
async def publish_procedural_asset(
    ctx: Context,
    node_group_name: str,
    description: str | None = None,
    author: str | None = None,
    tags: list[str] | None = None,
    catalog_id: str | None = None,
    fake_user: bool = True,
    operator_idname: str | None = None,
) -> dict:
    """
    Mark a validated local node group as a reusable asset in the open Blender file.

    This does not save, overwrite, or export a .blend file. For tool groups, ``operator_idname`` is
    the Blender operator identifier used by run_geometry_nodes_tool and must be globally unique.
    """
    return await asyncio.to_thread(
        call_geometry_nodes,
        "publish_procedural_asset",
        {
            "node_group_name": node_group_name,
            "description": description,
            "author": author,
            "tags": tags or [],
            "catalog_id": catalog_id,
            "fake_user": fake_user,
            "operator_idname": operator_idname,
        },
        changed_resources=[node_group_name],
    )
