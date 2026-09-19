"""Higher-level modeling tools built on top of mesh modifiers/operations."""

import asyncio
import logging

from typing import Annotated, Literal

from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from ..app import mcp
from ..connection import get_blender_connection
from .envelope import STALE_INDEX_WARNING, ok

logger = logging.getLogger("BlenderMCPServer")

Axis = Literal["X", "Y", "Z"]
Space = Literal["LOCAL", "WORLD"]


@mcp.tool()
async def copy_object_transform(
    ctx: Context,
    object_name: str,
    reference_object_name: str,
    match_location: bool = True,
    match_rotation: bool = True,
    match_scale: bool = True,
    space: Space = "WORLD",
) -> dict:
    """
    Align an object's transform to another object's transform in the scene.

    Args:
        ctx: MCP request context.
        object_name: Name of the object to move/rotate/scale.
        reference_object_name: Name of the object whose transform to copy.
        match_location: Copy the reference object's location.
        match_rotation: Copy the reference object's rotation.
        match_scale: Copy the reference object's scale.
        space: "WORLD" (default) matches visually even across differently-parented objects, and correctly handles
            quaternion/axis-angle rotation modes, by decomposing/recomposing world matrices. "LOCAL" copies the raw
            local location/rotation/scale properties instead.

    Returns:
        the object's name; "location"/"rotation"/"scale" (obj's local properties after the match, rotation in
        obj's own rotation_mode representation - a 3-float Euler triple, a 4-float [w,x,y,z] quaternion, or a
        4-float [angle,x,y,z] axis-angle); "rotation_mode"; and "world_location"/"world_rotation_quaternion"/
        "world_scale" (the world-space equivalents, rotation always as a quaternion).

    Raises:
        ToolError: If the operation cannot be completed.

    """
    try:
        blender = await asyncio.to_thread(get_blender_connection)
        result = await asyncio.to_thread(
            blender.send_command,
            "copy_object_transform",
            {
                "object_name": object_name,
                "reference_object_name": reference_object_name,
                "match_location": match_location,
                "match_rotation": match_rotation,
                "match_scale": match_scale,
                "space": space,
            },
        )
        return ok(result, changed_objects=[object_name])
    except Exception as e:
        logger.error(f"Error matching reference transform: {e}")
        raise ToolError(f"Error matching reference transform: {e}") from e


@mcp.tool()
async def add_radial_array_modifier(
    ctx: Context,
    object_name: str,
    count: Annotated[int, Field(ge=2, le=10000)] = 6,
    axis: Axis = "Z",
    apply: bool = False,
    pivot_object_name: str | None = None,
    pivot_location: tuple[float, float, float] | None = None,
    radius: Annotated[float | None, Field(gt=0)] = None,
) -> dict:
    """
    Duplicate an object radially around a pivot, evenly spaced about an axis.

    The array's visible spread is the distance between the object and the pivot - if
    the mesh is centered on its own origin, every rotated copy lands on top of the
    original. Provide exactly one of pivot_object_name, pivot_location, or radius to
    set that distance; omitting all three raises an error instead of silently
    producing overlapping copies.

    Args:
        ctx: MCP request context.
        object_name: Name of the mesh object to array.
        count: Number of copies around the circle (including the original). Must be at least 2.
        axis: One of X, Y, Z — the axis to rotate around.
        apply: If True, bake the modifier into the mesh and remove the helper empty. If False (default), leave both
            live.
        pivot_object_name: Name of an existing object whose world location is used as the pivot.
        pivot_location: [x, y, z] world location to use as the pivot.
        radius: Distance to auto-place the pivot from the object, perpendicular to axis. For a parented object, the
            pivot is offset from its world-space location, not its local one.

    Note: when apply=True, this changes topology - indices returned by an earlier get_mesh_data call are no
    longer valid afterward; call get_mesh_data again before further index-based edits. When apply=False, the
    base mesh (and its indices) are untouched.

    Returns:
        the object's name, whether the modifier was applied, base vertex/edge/polygon counts, and (when apply=False)
        an "evaluated" count, "modifier" name, and world-space "bounds" reflecting the live modifier's effect.

    Raises:
        ToolError: If the operation cannot be completed.

    """
    try:
        blender = await asyncio.to_thread(get_blender_connection)
        result = await asyncio.to_thread(
            blender.send_command,
            "add_radial_array_modifier",
            {
                "object_name": object_name,
                "count": count,
                "axis": axis,
                "apply": apply,
                "pivot_object_name": pivot_object_name,
                "pivot_location": list(pivot_location) if pivot_location else None,
                "radius": radius,
            },
        )
        warnings = [STALE_INDEX_WARNING] if apply else None
        return ok(result, changed_objects=[object_name], warnings=warnings)
    except Exception as e:
        logger.error(f"Error creating radial array: {e}")
        raise ToolError(f"Error creating radial array: {e}") from e


@mcp.tool()
async def sync_data_name(ctx: Context, object_names: Annotated[list[str], Field(min_length=1, max_length=500)]) -> dict:
    """
    Sync each object's data-block name to match its object name.

    Args:
        ctx: MCP request context.
        object_names: Names of the objects to sync.

    Returns:
        "names": the object names that were synced (data-block renamed to match object name).

    Raises:
        ToolError: If the operation cannot be completed.

    """
    try:
        blender = await asyncio.to_thread(get_blender_connection)
        result = await asyncio.to_thread(blender.send_command, "sync_data_name", {"object_names": object_names})
        changed = result.get("names", object_names) if isinstance(result, dict) else object_names
        return ok(result, changed_objects=changed)
    except Exception as e:
        logger.error(f"Error syncing data-block names: {e}")
        raise ToolError(f"Error syncing data-block names: {e}") from e
