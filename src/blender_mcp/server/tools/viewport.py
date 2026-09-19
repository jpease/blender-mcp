"""Scene/object introspection and viewport screenshot tools."""

import asyncio
import logging
import os
import tempfile

from typing import Annotated, Literal

from mcp.server.fastmcp import Context, Image
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from ..app import mcp
from ..connection import get_blender_connection
from .envelope import ok

logger = logging.getLogger("BlenderMCPServer")


@mcp.tool()
async def list_scene_objects(
    ctx: Context,
    limit: Annotated[int, Field(ge=1, le=200)] = 25,
    offset: Annotated[int, Field(ge=0)] = 0,
) -> dict:
    """
    Inspect the current Blender scene and page through its objects.

    Args:
        ctx: MCP request context.
        limit: Maximum number of objects to return in this page (default 25, capped at 200).
        offset: Index of the first object to return, for paging through a scene with more objects than fit in one page.

    Returns:
        "name" (scene name), active object, selection, mode, unit settings, "materials_count", and "objects"
        (stable name-sorted records with local location, parent, collections, selection and visibility),
        "object_count" (the scene's true total),
        "offset"/"limit" (the effective page bounds used), "returned_count" (length of this page), "truncated"
        (True if more objects remain), and "next_offset" (pass as offset to fetch the next page while truncated
        is True).

    Raises:
        ToolError: If the operation cannot be completed.

    """
    try:
        blender = await asyncio.to_thread(get_blender_connection)
        result = await asyncio.to_thread(blender.send_command, "list_scene_objects", {"limit": limit, "offset": offset})
        return ok(result)
    except Exception as e:
        logger.error(f"Error getting scene info from Blender: {e}")
        raise ToolError(f"Error getting scene info: {e}") from e


ViewportOverlay = Literal["CAVITY", "WIREFRAMES", "FACE_ORIENTATION"]


@mcp.tool()
async def set_viewport_overlay(ctx: Context, toggle: ViewportOverlay, enabled: bool) -> dict:
    """
    Set a native Blender viewport overlay to an explicit on/off state.

    A true idempotent setter backed by Blender's own viewport overlay properties -
    calling it again with the same enabled value is a no-op.

    Args:
        ctx: MCP request context.
        toggle: One of CAVITY, WIREFRAMES, FACE_ORIENTATION.
        enabled: Desired on/off state.

    Returns:
        "toggle" (the resolved overlay name) and "enabled" (the state it was set to).

    Raises:
        ToolError: If the operation cannot be completed.

    """
    try:
        blender = await asyncio.to_thread(get_blender_connection)
        result = await asyncio.to_thread(
            blender.send_command, "set_viewport_overlay", {"toggle": toggle, "enabled": enabled}
        )
        return ok(result)
    except Exception as e:
        logger.error(f"Error toggling viewport overlay: {e}")
        raise ToolError(f"Error toggling viewport overlay: {e}") from e


@mcp.tool()
async def get_object_info(
    ctx: Context,
    object_name: str,
    sections: list[
        Literal[
            "GEOMETRY",
            "ATTRIBUTES",
            "VOLUME_GRIDS",
            "GREASE_PENCIL",
            "PARTICLES",
            "SOFT_BODY",
            "DYNAMIC_PAINT",
        ]
    ]
    | None = None,
    limit: Annotated[int, Field(ge=1, le=1000)] = 100,
    offset: Annotated[int, Field(ge=0)] = 0,
) -> dict:
    """
    Inspect an object's transform, type, materials, modifiers, and summary geometry data.

    "location"/"rotation"/"scale" are the object's local (parent-relative) transform; "world_bounding_box" (mesh
    objects only) is the world-space AABB - the two live in different spaces and aren't directly comparable for a
    parented or transformed object. "rotation_mode" names how to read "rotation": one of the six Euler orders means
    "[x, y, z]" radians in that order; "QUATERNION" means "[w, x, y, z]"; "AXIS_ANGLE" means "[angle, x, y, z]".

    Args:
        ctx: MCP request context.
        object_name: Object name to inspect. For meshes, this returns only vertex/edge/polygon counts (base-mesh,
            pre-modifier, same as `get_mesh_data`); use `get_mesh_data` for element coordinates, normals, indices, or
            selection state before calling index-based editing tools - and again afterward, since those tools change
            topology and invalidate prior indices.

    Returns:
        "name", "type", "library" (linked-from library name, null when local), "is_override" (an override was
        resolved, not the linked original), "data_name", "location"/"rotation"/"scale" (local transform - see the
        Note above for reading "rotation" against "rotation_mode"), "matrix_world", world-aligned
        "dimensions", parent and collection membership, selection/visibility flags, "materials" (assigned material
        names), "modifiers" (each as {"name", "type", "show_viewport", "show_render"}), and for mesh objects,
        "world_bounding_box" (world-space AABB) and "mesh" ({"vertices", "edges", "polygons"} base-mesh counts).

    Raises:
        ToolError: If the operation cannot be completed.

    """
    try:
        blender = await asyncio.to_thread(get_blender_connection)
        result = await asyncio.to_thread(
            blender.send_command,
            "get_object_info",
            {"name": object_name, "sections": sections, "limit": limit, "offset": offset},
        )
        return ok(result)
    except Exception as e:
        logger.error(f"Error getting object info from Blender: {e}")
        raise ToolError(f"Error getting object info: {e}") from e


@mcp.tool()
async def get_mesh_data(
    ctx: Context,
    object_name: str,
    element_type: Literal["vertices", "edges", "faces", "loops"] = "vertices",
    limit: Annotated[int, Field(ge=1, le=1000)] = 100,
    offset: Annotated[int, Field(ge=0)] = 0,
    selected_only: bool = False,
) -> dict:
    """
    Paginated inspection of a mesh's topology: vertices, edges, faces, or loops.

    Use this to discover valid indices (with coordinates, normals, and selection state)
    before calling index-based mesh tools such as mesh_extrude, mesh_inset, mesh_bevel,
    mesh_bridge, or mesh_subdivide. Call it again after any topology-changing edit
    (extrude, inset, bevel, bridge, subdivide, symmetrize, boolean, remesh, or an applied
    modifier) before reusing indices - those operations rebuild the mesh's vertex/edge/face
    arrays, so previously fetched indices are no longer guaranteed to refer to the same
    elements.

    Coordinates and normals come from the object's base mesh in local (object-space)
    coordinates - modifiers are not evaluated. To get world-space positions, transform by
    the object's `matrix_world` (see `get_object_info`).

    Args:
        ctx: MCP request context.
        object_name: Name of the mesh object to inspect.
        element_type: One of "vertices", "edges", "faces", "loops". Each element in the result includes its "index"
            plus type-specific fields: vertices have "co" and "normal"; edges have their two "vertices" indices;
            faces have their "vertices" indices, "normal", and "material_index"; loops have "vertex_index",
            "edge_index", and "face_index". Vertices/edges/faces also include a "select" flag.
        limit: Maximum number of elements to return in this page (default 100, capped at 1000).
        offset: Index of the first element to return, for paging through a large mesh.
        selected_only: If True, only return elements currently selected in Edit Mode (not supported for
            element_type="loops", which has no selection state of its own). The result includes "total" (elements
            matching the current filter), "total_unfiltered", "returned_count", "truncated", and "next_offset" -
            when "truncated" is true, call again with offset=next_offset to see the rest.

    Returns:
        "name" (object name), "element_type", "total", "total_unfiltered", "offset"/"limit" (effective page
        bounds used), "returned_count", "truncated", "next_offset", and "elements" (this page's list of
        per-element dicts, shaped per element_type as described in the element_type Args entry above).

    Raises:
        ToolError: If the operation cannot be completed.

    """
    try:
        blender = await asyncio.to_thread(get_blender_connection)
        result = await asyncio.to_thread(
            blender.send_command,
            "get_mesh_data",
            {
                "object_name": object_name,
                "element_type": element_type,
                "limit": limit,
                "offset": offset,
                "selected_only": selected_only,
            },
        )
        return ok(result)
    except Exception as e:
        logger.error(f"Error getting mesh data from Blender: {e}")
        raise ToolError(f"Error getting mesh data: {e}") from e


def _screenshot_metadata(result: dict) -> dict:
    """
    Build the metadata dict for a viewport screenshot result, alongside its Image content item.

    Args:
        result: The raw dict returned by the Blender-side screenshot handler.

    Returns:
        dict: "width", "height", "method" ("offscreen" or "window_grab", indicating how the capture was taken).

    """
    return {
        "width": result.get("width"),
        "height": result.get("height"),
        "method": result.get("method"),
    }


@mcp.tool(structured_output=False)
async def get_viewport_screenshot(
    ctx: Context, max_size: Annotated[int, Field(ge=16, le=4096)] = 1000
) -> list[Image | dict]:
    """
    Capture the current Blender 3D viewport as an image for visual inspection.

    Unlike other tools, this returns two content items instead of one dict: the
    screenshot image itself, followed by an ok() envelope carrying its metadata - read
    both.

    Args:
        ctx: MCP request context.
        max_size: Maximum pixel length of the image's largest dimension; defaults to 1000.

    Returns:
        [Image, dict]: the screenshot, then an envelope whose data has "width", "height", "method".

    Raises:
        Exception: If the operation cannot be completed.

    """
    temp_path = None
    try:
        blender = await asyncio.to_thread(get_blender_connection)

        descriptor, temp_path = tempfile.mkstemp(prefix="blender_mcp_viewport_", suffix=".png")
        os.close(descriptor)

        result = await asyncio.to_thread(
            blender.send_command,
            "get_viewport_screenshot",
            {"max_size": max_size, "filepath": temp_path, "format": "png"},
        )

        if "error" in result:
            raise Exception(result["error"])

        if not os.path.exists(temp_path):
            raise Exception("Screenshot file was not created")

        # Read the file
        with open(temp_path, "rb") as f:
            image_bytes = f.read()

        return [Image(data=image_bytes, format="png"), ok(_screenshot_metadata(result))]

    except Exception as e:
        logger.error(f"Error capturing screenshot: {e!s}")
        raise Exception(f"Screenshot failed: {e!s}") from e
    finally:
        if temp_path:
            try:
                os.remove(temp_path)
            except FileNotFoundError:
                pass
