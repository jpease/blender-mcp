# ruff: file-ignore[multi-line-summary-second-line]
"""Procedural output delivery tools."""

from typing import Literal

from mcp.server.fastmcp import Context

from ...app import mcp
from .._dispatch import call_blender


@mcp.tool()
async def realize_procedural_output(
    ctx: Context,
    object_name: str,
    output_name: str,
    collection_name: str,
    delivery_mode: Literal["REALIZED_MESH", "LIVE_INSTANCE_COPY", "APPLIED_MODIFIER_COPY"] = "REALIZED_MESH",
    modifier_name: str | None = None,
    frame: int | None = None,
    collision_policy: Literal["ERROR", "UNIQUE"] = "ERROR",
    confirm_destructive: bool = False,
) -> dict:
    """Create a named delivery object while preserving the live source object.

    ``REALIZED_MESH`` converts the full evaluated result to a standalone mesh and therefore realizes
    mesh-representable instances. ``LIVE_INSTANCE_COPY`` keeps a copied procedural stack and is not a
    standalone mesh. ``APPLIED_MODIFIER_COPY`` applies one exact Geometry Nodes modifier only on the
    copy and requires confirmation; downstream modifiers remain live. The result reports retained and
    lost attributes/components plus source provenance.
    """
    if delivery_mode == "APPLIED_MODIFIER_COPY":
        if not modifier_name:
            raise ValueError("APPLIED_MODIFIER_COPY requires modifier_name")
        if not confirm_destructive:
            raise ValueError("confirm_destructive=True is required for APPLIED_MODIFIER_COPY")
    return await call_blender(
        "realize_procedural_output",
        {
            "object_name": object_name,
            "output_name": output_name,
            "collection_name": collection_name,
            "delivery_mode": delivery_mode,
            "modifier_name": modifier_name,
            "frame": frame,
            "collision_policy": collision_policy,
            "confirm_destructive": confirm_destructive,
        },
        changed_objects=[output_name],
    )
