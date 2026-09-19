"""Stable named-attribute and procedural-instance management tools."""

import asyncio

from typing import Any, Literal

from mcp.server.fastmcp import Context

from ...app import mcp
from ._shared import call_geometry_nodes


@mcp.tool()
async def manage_named_attributes(
    ctx: Context,
    object_name: str,
    action: Literal["LIST", "CREATE", "SET", "RENAME", "CONVERT", "REMOVE"],
    attribute_name: str | None = None,
    new_name: str | None = None,
    data_type: Literal["FLOAT", "INT", "FLOAT_VECTOR", "FLOAT_COLOR", "BYTE_COLOR", "BOOLEAN", "STRING"] | None = None,
    domain: Literal["POINT", "EDGE", "FACE", "CORNER", "CURVE", "INSTANCE"] | None = None,
    values: list[Any] | None = None,
    confirm_destructive: bool = False,
) -> dict:
    """
    Inspect or change persistent named geometry attributes used across procedural graphs.

    ``LIST`` is read-only. ``REMOVE`` and ``CONVERT`` require confirmation and report known node-group
    consumers. Values are bounded and validated against the selected data type, domain, and element count.
    """
    if action in {"REMOVE", "CONVERT"} and not confirm_destructive:
        raise ValueError(f"confirm_destructive=True is required for {action}")
    return await asyncio.to_thread(
        call_geometry_nodes,
        "manage_named_attributes",
        {
            "object_name": object_name,
            "action": action,
            "attribute_name": attribute_name,
            "new_name": new_name,
            "data_type": data_type,
            "domain": domain,
            "values": values,
            "confirm_destructive": confirm_destructive,
        },
        changed_objects=[] if action == "LIST" else [object_name],
    )


@mcp.tool()
async def manage_procedural_instances(
    ctx: Context,
    node_group_name: str,
    source_type: Literal["OBJECT", "COLLECTION"] | None = None,
    source_name: str | None = None,
    pick_instance: bool | None = None,
    rotation: tuple[float, float, float] | None = None,
    scale: tuple[float, float, float] | None = None,
    translation: tuple[float, float, float] | None = None,
    realize_instances: bool | None = None,
) -> dict:
    """
    Inspect or update a tagged scatter/array instance system through stable builder roles.

    The result identifies source dependencies, estimated instance count, nesting depth, and whether
    downstream nodes force realization. Omitted settings remain unchanged.
    """
    return await asyncio.to_thread(
        call_geometry_nodes,
        "manage_procedural_instances",
        {
            "node_group_name": node_group_name,
            "source_type": source_type,
            "source_name": source_name,
            "pick_instance": pick_instance,
            "rotation": rotation,
            "scale": scale,
            "translation": translation,
            "realize_instances": realize_instances,
        },
        changed_resources=[node_group_name]
        if any(
            value is not None
            for value in [source_type, source_name, pick_instance, rotation, scale, translation, realize_instances]
        )
        else [],
    )
