"""Scene/object introspection and viewport screenshot tools."""

import asyncio

from typing import Annotated, Literal

from mcp.server.fastmcp import Context, Image
from pydantic import Field, model_validator

from ..app import mcp
from ._dispatch import call_blender, send_command
from ._inputs import StrictModel, dump_input
from .image_capture import capture_png


@mcp.tool()
async def list_scene_objects(
    ctx: Context,
    limit: Annotated[int, Field(ge=1, le=200)] = 25,
    offset: Annotated[int, Field(ge=0)] = 0,
    search: Annotated[str | None, Field(min_length=1)] = None,
) -> dict:
    """
    Inspect the current Blender scene and page through its objects.

    Args:
        ctx: MCP request context.
        limit: Maximum number of objects to return in this page (default 25, capped at 200).
        offset: Index of the first object to return, for paging through a scene with more objects than fit in one page.
        search: Only objects whose name contains this, case-insensitively ("char1_" finds each CHAR1_).
            Paging then runs over the matches.

    Returns:
        "name" (scene name), active object, "selected_count", mode, unit settings, "materials_count", and
        "objects" (stable name-sorted records with local location, parent, collections, selection and
        visibility), "object_count" (the scene's true total), "matched_count" (how many match search; the
        total paging runs over), "search" (echoed, or None), "offset"/"limit" (the effective page bounds used),
        "returned_count" (length of this page), "truncated" (True if more matches remain), and "next_offset"
        (pass as offset to fetch the next page while truncated is True).

    Raises:
        ToolError: If the operation cannot be completed.

    """
    return await call_blender("list_scene_objects", {"limit": limit, "offset": offset, "search": search})


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
    return await call_blender("set_viewport_overlay", {"toggle": toggle, "enabled": enabled})


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
    return await call_blender(
        "get_object_info", {"name": object_name, "sections": sections, "limit": limit, "offset": offset}
    )


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
    coordinates - modifiers are not evaluated, so this reads the same before and after a pose
    or a simulation. For world space, transform by `matrix_world` (see `get_object_info`); for
    the deformed result, use sample_deformed_geometry (per-vertex) or
    inspect_evaluated_geometry (bounds and counts).

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
    return await call_blender(
        "get_mesh_data",
        {
            "object_name": object_name,
            "element_type": element_type,
            "limit": limit,
            "offset": offset,
            "selected_only": selected_only,
        },
    )


# Named so the two RUF069-sensitive float comparisons below compare against a constant
# rather than a bare literal, and so lens_mm's default and its camera_object conflict check
# cannot drift apart.
_DEFAULT_LENS_MM: float = 50.0

# Mirrors _look_quaternion's own <= 1e-16 length_squared guard (camera/_shared.py) - this
# check runs server-side, before any addon round trip, on the raw eye/target_point tuples only
# (target_object_name's world position is not known here; _look_quaternion re-checks it live).
_DEGENERATE_LENGTH_SQUARED: float = 1e-16


class ViewSpec(StrictModel):
    """
    An ad hoc camera view for get_viewport_screenshot, independent of the live viewport.

    Supply exactly one source of view: camera_object (an existing camera already in the
    scene, using that camera's own lens) or eye with exactly one of target_point/
    target_object_name (a one-off look-at built from a world point and a camera position,
    never added to the scene).

    The look-at is world-Z-up, as `_look_quaternion` (the production aim used by
    create_camera's target_point) builds it. There is no `up` field: a roll this model
    accepted but that math ignores would be a lie in the schema.
    """

    camera_object: Annotated[str, Field(min_length=1, max_length=63)] | None = None
    eye: tuple[float, float, float] | None = None
    target_point: tuple[float, float, float] | None = None
    target_object_name: Annotated[str, Field(min_length=1, max_length=63)] | None = None
    lens_mm: Annotated[float, Field(gt=0.0)] = _DEFAULT_LENS_MM

    @model_validator(mode="after")
    def _validate_view(self) -> "ViewSpec":
        ad_hoc = self.eye is not None or self.target_point is not None or self.target_object_name is not None
        if (self.camera_object is not None) == ad_hoc:
            raise ValueError("Supply exactly one of camera_object or eye (with target_point or target_object_name)")
        if ad_hoc:
            if self.eye is None:
                raise ValueError("eye is required when target_point or target_object_name is given")
            if (self.target_point is None) == (self.target_object_name is None):
                raise ValueError("Supply exactly one of target_point or target_object_name")
            if self.target_point is not None:
                dx, dy, dz = (t - e for t, e in zip(self.target_point, self.eye, strict=True))
                if dx * dx + dy * dy + dz * dz <= _DEGENERATE_LENGTH_SQUARED:
                    raise ValueError("eye and target_point cannot occupy the same point")
        if self.camera_object is not None and self.lens_mm != _DEFAULT_LENS_MM:
            raise ValueError("lens_mm has no effect when camera_object is given - it uses that camera's own lens")
        return self


ShadingOverride = Literal["SOLID", "MATERIAL"]


def _screenshot_metadata(result: dict) -> dict:
    """
    Build the metadata dict for a viewport screenshot result, alongside its Image content item.

    Args:
        result: The raw dict returned by the Blender-side screenshot handler.

    Returns:
        dict: "width", "height", "method" ("offscreen" or "window_grab", indicating how the
        capture was taken), "view_source" ("live_viewport", "camera_object", or "eye_target" -
        which source actually produced this capture), "shading_mode" (the space.shading.type
        actually used, whether or not shading_override was given).

    """
    return {
        "width": result.get("width"),
        "height": result.get("height"),
        "method": result.get("method"),
        "view_source": result.get("view_source"),
        "shading_mode": result.get("shading_mode"),
    }


@mcp.tool(structured_output=False)
async def get_viewport_screenshot(
    ctx: Context,
    max_size: Annotated[int, Field(ge=16, le=4096)] = 1000,
    view: ViewSpec | None = None,
    shading_override: ShadingOverride | None = None,
) -> list[Image | dict]:
    """
    Capture the current Blender 3D viewport as an image for visual inspection.

    Unlike other tools, this returns two content items instead of one dict: the
    screenshot image itself, followed by an ok() envelope carrying its metadata - read
    both.

    Args:
        ctx: MCP request context.
        max_size: Maximum pixel length of the image's largest dimension; defaults to 1000.
        view: An ad hoc camera view (see ViewSpec) to capture from instead of the live
            viewport's current navigation. Omit to capture exactly what the viewport is
            showing right now (unchanged default behavior).
        shading_override: Force SOLID or MATERIAL viewport shading for just this capture,
            then restore whatever shading the live viewport had. Both are cheap single-pass
            rasterization; RENDERED (real render-engine cost) is not offered here.

    Returns:
        [Image, dict]: the screenshot, then an envelope whose data has "width", "height",
        "method", "view_source", "shading_mode".

    Raises:
        ToolError: If Blender refused the capture, the round trip failed, or no image came back.

    """
    return await asyncio.to_thread(
        capture_png,
        send_command,
        "get_viewport_screenshot",
        {
            "max_size": max_size,
            "view": dump_input(view),
            "shading_override": shading_override,
        },
        prefix="blender_mcp_viewport_",
        metadata=_screenshot_metadata,
        missing_file="Screenshot file was not created",
    )
