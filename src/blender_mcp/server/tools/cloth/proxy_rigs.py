# MCP tool signatures intentionally expose more than five keyword arguments so
# agents receive precise JSON schemas instead of opaque catch-all dictionaries.
"""Typed tool for binding a low-resolution cloth proxy to a render mesh."""

import asyncio

from typing import Annotated, Literal

from mcp.server.fastmcp import Context
from pydantic import Field

from ...app import mcp
from ._shared import _call
from .inspection_and_setup import ExistingPolicy

ProxySourcePolicy = Literal["EXISTING", "DUPLICATE_RENDER", "DECIMATE_RENDER"]
ProxyBindType = Literal["SURFACE_DEFORM", "MESH_DEFORM"]


@mcp.tool()
async def create_cloth_proxy_rig(
    ctx: Context,
    render_object_name: str,
    proxy_object_name: str,
    proxy_source_policy: ProxySourcePolicy = "EXISTING",
    bind_type: ProxyBindType = "SURFACE_DEFORM",
    cloth_modifier_name: str = "Cloth",
    bind_modifier_name: str = "Cloth Proxy Bind",
    existing_policy: ExistingPolicy = "ERROR",
    allow_topology_change: bool = False,
    decimate_ratio: Annotated[float, Field(gt=0, le=1)] = 0.25,
    vertex_group_name: str | None = None,
    surface_deform_falloff: Annotated[float, Field(gt=0)] = 4.0,
    mesh_deform_precision: Annotated[int, Field(ge=1)] = 5,
    rest_frame: Annotated[int, Field(ge=0)] = 1,
    validation_frames: list[int] | None = None,
) -> dict:
    """
    Create a live low-resolution cloth proxy relationship for one render mesh.

    EXISTING uses an explicit proxy object. DUPLICATE_RENDER preserves topology; DECIMATE_RENDER
    creates a new proxy and is accepted only with ``allow_topology_change=True``. The render mesh
    receives a bound Surface Deform or Mesh Deform modifier and is never destructively converted.
    """
    return await asyncio.to_thread(
        _call,
        "create_cloth_proxy_rig",
        {
            "render_object_name": render_object_name,
            "proxy_object_name": proxy_object_name,
            "proxy_source_policy": proxy_source_policy,
            "bind_type": bind_type,
            "cloth_modifier_name": cloth_modifier_name,
            "bind_modifier_name": bind_modifier_name,
            "existing_policy": existing_policy,
            "allow_topology_change": allow_topology_change,
            "decimate_ratio": decimate_ratio,
            "vertex_group_name": vertex_group_name,
            "surface_deform_falloff": surface_deform_falloff,
            "mesh_deform_precision": mesh_deform_precision,
            "rest_frame": rest_frame,
            "validation_frames": validation_frames or [],
        },
        [render_object_name],
    )
