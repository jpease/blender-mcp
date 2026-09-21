"""Rigid-body component removal tools."""

from typing import Annotated, Literal

from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from .._dispatch import call_blender
from .inspection_and_setup import mcp


@mcp.tool()
async def remove_rigid_body_components(
    ctx: Context,
    scene_name: str,
    component_type: Literal["BODY_SETTINGS", "CONSTRAINT_SETTINGS", "TAGGED_HELPERS", "WORLD"],
    object_names: Annotated[list[str] | None, Field(max_length=500)] = None,
    rig_id: Annotated[str | None, Field(min_length=1)] = None,
    confirm_destructive: bool = False,
) -> dict:
    """
    Remove precisely scoped physics components without deleting ordinary mesh objects.

    component_type selects what's removed: BODY_SETTINGS clears rigid-body settings from
    object_names (required); CONSTRAINT_SETTINGS clears rigid-body constraint settings from
    object_names (required); TAGGED_HELPERS deletes helper objects created by other rigid-body
    tools (proxies, colliders, constraint empties) selected by rig_id and/or object_names (at least
    one required); WORLD removes scene_name's entire rigid body world and accepts neither
    object_names nor rig_id. TAGGED_HELPERS and WORLD delete objects outright rather than just
    clearing settings, so both require confirm_destructive=True.
    """
    names = object_names or []
    if component_type in {"BODY_SETTINGS", "CONSTRAINT_SETTINGS"} and not names:
        raise ToolError(f"{component_type} requires object_names")
    if component_type == "TAGGED_HELPERS" and not (names or rig_id):
        raise ToolError("TAGGED_HELPERS requires object_names or rig_id")
    if component_type == "WORLD" and (names or rig_id):
        raise ToolError("WORLD does not accept object_names or rig_id")
    if component_type in {"TAGGED_HELPERS", "WORLD"} and not confirm_destructive:
        raise ToolError(f"{component_type} requires confirm_destructive=True")
    return await call_blender(
        "remove_rigid_body_components",
        {
            "scene_name": scene_name,
            "component_type": component_type,
            "object_names": names,
            "rig_id": rig_id,
            "confirm_destructive": confirm_destructive,
        },
        changed_objects=names,
    )
