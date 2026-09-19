"""Agent-facing tools for building new retopology geometry: guides, patches, and boundaries."""

import asyncio
import math

from typing import Annotated, Any, Literal

from mcp.server.fastmcp import Context
from pydantic import Field

from ...app import mcp
from ..envelope import STALE_INDEX_WARNING, ok
from ._shared import _call


@mcp.tool()
async def create_retopology_guides(
    ctx: Context,
    source_object_name: str,
    guides: list[dict[str, Any]],
    collection_name: str = "Retopology Guides",
    projection_offset: float = 0.0,
    max_projection_distance: Annotated[float, Field(gt=0)] | None = None,
) -> dict:
    """
    Create explicit world-space curve guides projected onto an evaluated source.

    Each guide dict must contain `name`, `role`, and exactly one of `points`
    or `source_vertex_indices`; it may contain `cyclic` (default false).
    `points` is an ordered list of world-space XYZ triples. Source indices
    refer to the source's current base mesh and are converted to world space
    before projection. Roles are caller-declared EYE_LOOP, MOUTH_LOOP,
    JOINT_RING, HARD_EDGE, SEAM, DENSITY_TRANSITION, PANEL_BOUNDARY, or CUSTOM;
    the tool never infers anatomy. Every input and projection is validated
    before any Curve object is created. Returns the projected world-space
    points and collision-safe object names.
    """
    result = await asyncio.to_thread(
        _call,
        "create_retopology_guides",
        {
            "source_object_name": source_object_name,
            "guides": guides,
            "collection_name": collection_name,
            "projection_offset": projection_offset,
            "max_projection_distance": max_projection_distance,
        },
    )
    return ok(result, changed_objects=result["created_guide_objects"])


@mcp.tool()
async def create_surface_section(
    ctx: Context,
    source_object_name: str,
    plane_origin: tuple[float, float, float],
    plane_normal: tuple[float, float, float],
    vertex_count: Annotated[int, Field(ge=2)],
    name: str | None = None,
    collection_name: str = "Retopology Guides",
    component_index: Annotated[int, Field(ge=0)] = 0,
    cyclic: bool = True,
    projection_offset: float = 0.0,
) -> dict:
    """
    Create a resampled guide from a world-space plane/source intersection.

    The evaluated source (including live modifiers) is intersected without
    modifying it. Connected sections are sorted by descending world-space
    length; `component_index=0` selects the longest. The selected polyline is
    resampled to exactly `vertex_count` points, reprojected to the evaluated
    source, and stored as a named Curve in `collection_name`. Use `cyclic=True`
    for a closed limb/pipe section and false only when an open intersection is
    intentional. The result lists every discovered component before selection.
    """
    params = {key: value for key, value in locals().items() if key != "ctx"}
    result = await asyncio.to_thread(_call, "create_surface_section", params)
    return ok(result, changed_objects=[result["guide_object"]])


@mcp.tool()
async def set_retopology_features(
    ctx: Context,
    object_name: str,
    edge_indices: list[int] | None = None,
    detect_source_object_name: str | None = None,
    source_dihedral_angle: Annotated[float, Field(ge=0, le=math.pi)] | None = None,
    include_material_boundaries: bool = False,
    guide_object_names: list[str] | None = None,
    guide_distance: Annotated[float, Field(ge=0)] = 0.01,
    apply_detected: bool = False,
    seam: bool | None = None,
    sharp: bool | None = None,
    crease: Annotated[float, Field(ge=0, le=1)] | None = None,
    bevel_weight: Annotated[float, Field(ge=0, le=1)] | None = None,
    expected_revision: str | None = None,
) -> dict:
    """
    Detect and optionally write coherent feature marks on explicit target edges.

    Explicit `edge_indices` are always active. Optional source dihedral,
    material-boundary, and guide proximity rules add *candidates*; candidates
    are only changed when `apply_detected=True`, so detection never silently
    activates every suggestion. Angles are radians in [0, pi], guide distances
    are world-space, and crease/bevel weights are in [0, 1]. `None` leaves a
    mark unchanged; booleans can set or clear seam/sharp. Blender 5.1 edge
    attributes `sharp_edge`, `crease_edge`, and `bevel_weight_edge` are used.
    All indices and `expected_revision` are validated before mutation. This
    changes attributes but not connectivity, so valid element indices remain stable.
    """
    params = {key: value for key, value in locals().items() if key != "ctx"}
    return ok(await asyncio.to_thread(_call, "set_retopology_features", params), changed_objects=[object_name])


@mcp.tool()
async def add_support_loops(
    ctx: Context,
    object_name: str,
    edge_indices: list[int],
    width: Annotated[float, Field(gt=0, le=10)],
    side: Literal["BOTH", "LEFT", "RIGHT"] = "BOTH",
    clamp: bool = True,
    corner_policy: Literal["MITER", "CAP_ENDPOINTS"] = "MITER",
    source_object_name: str | None = None,
    projection_offset: float = 0.0,
    subdivision_levels: Annotated[int, Field(ge=0, le=6)] = 2,
    expected_revision: str | None = None,
) -> dict:
    """
    Insert deterministic support loops around selected manifold feature edges.

    The ordered-independent edge IDs must describe non-branching manifold
    chains. `width` is Blender's positive Edge Slide factor (0, 10]. BOTH runs
    equal offsets on both sides; LEFT or RIGHT retains only that signed side.
    `clamp` prevents overshoot and CAP_ENDPOINTS extends around open ends.
    Blender's verified Offset Edge Loops + Edge Slide operator is used in a
    restored Edit-Mode context; cancellation is an error. New vertices are
    optionally projected to the evaluated source. A live Subdivision Surface
    modifier is created or updated, never applied. Returns created element IDs,
    manifold validation, modifier order, and a new topology revision.
    """
    params = {key: value for key, value in locals().items() if key != "ctx"}
    return ok(
        await asyncio.to_thread(_call, "add_support_loops", params),
        changed_objects=[object_name],
        warnings=[STALE_INDEX_WARNING],
    )


@mcp.tool()
async def build_quad_patch(
    ctx: Context,
    object_name: str,
    corners: list[tuple[float, float, float]],
    u_segments: Annotated[int, Field(ge=1, le=1000)],
    v_segments: Annotated[int, Field(ge=1, le=1000)],
    source_object_name: str | None = None,
    coordinate_space: Literal["LOCAL", "WORLD"] = "WORLD",
    interpolation: Literal["BILINEAR", "COONS"] = "BILINEAR",
    boundary_u0: list[tuple[float, float, float]] | None = None,
    boundary_u1: list[tuple[float, float, float]] | None = None,
    boundary_v0: list[tuple[float, float, float]] | None = None,
    boundary_v1: list[tuple[float, float, float]] | None = None,
    projection_offset: float = 0.0,
    expected_revision: str | None = None,
) -> dict:
    """
    Append a regular quad grid defined by four ordered corners or Coons guides.

    Corners are ordered [u0v0, u1v0, u1v1, u0v1]. `u_segments` and
    `v_segments` are face counts, each at least 1. COONS additionally requires
    all four boundary polylines; their endpoints must match the corners and
    opposite guides must not cross. New vertices are optionally projected to
    the evaluated source. Returns every created vertex/edge/face index and a
    fresh topology revision.
    """
    params = {key: value for key, value in locals().items() if key != "ctx"}
    return ok(
        await asyncio.to_thread(_call, "build_quad_patch", params),
        changed_objects=[object_name],
        warnings=[STALE_INDEX_WARNING],
    )


@mcp.tool()
async def extend_boundary(
    ctx: Context,
    object_name: str,
    ordered_boundary_vertex_indices: list[int],
    rows: Annotated[int, Field(ge=1, le=500)] = 1,
    distance: Annotated[float, Field(gt=0)] = 0.1,
    mode: Literal["FIXED_VECTOR", "VERTEX_NORMAL", "GUIDE_DIRECTED", "SURFACE_TANGENT"] = "VERTEX_NORMAL",
    vector: tuple[float, float, float] = (0.0, 0.0, 1.0),
    guide_points: list[tuple[float, float, float]] | None = None,
    source_object_name: str | None = None,
    projection_offset: float = 0.0,
    expected_revision: str | None = None,
) -> dict:
    """
    Grow one or more quad rows from one ordered open manifold boundary.

    The supplied vertices must form one non-branching open boundary in order;
    no implicit sorting or selection is used. FIXED_VECTOR uses `vector` in
    target-local space, VERTEX_NORMAL uses averaged local boundary normals,
    GUIDE_DIRECTED follows a same-length world-space guide polyline, and
    SURFACE_TANGENT removes the source-normal component from `vector`. New
    rows are projected when a source is supplied. Returns all created IDs and
    the new topology revision.
    """
    params = {key: value for key, value in locals().items() if key != "ctx"}
    return ok(
        await asyncio.to_thread(_call, "extend_boundary", params),
        changed_objects=[object_name],
        warnings=[STALE_INDEX_WARNING],
    )


@mcp.tool()
async def fill_boundary_quads(
    ctx: Context,
    object_name: str,
    boundary_edge_indices: list[int],
    span: Annotated[int, Field(ge=1)] = 1,
    offset: int = 0,
    use_interp_simple: bool = False,
    source_object_name: str | None = None,
    projection_offset: float = 0.0,
    expected_revision: str | None = None,
) -> dict:
    """
    Fill a compatible closed hole boundary with quads only.

    `boundary_edge_indices` must be exactly one ordered-compatible closed
    manifold boundary with an even vertex count. `span` and `offset` select
    the grid correspondence, matching Blender's Grid Fill controls. The tool
    rejects any result containing triangles or n-gons and never falls back to
    a generic fill. New vertices may be projected to an evaluated source.
    """
    params = {key: value for key, value in locals().items() if key != "ctx"}
    return ok(
        await asyncio.to_thread(_call, "fill_boundary_quads", params),
        changed_objects=[object_name],
        warnings=[STALE_INDEX_WARNING],
    )
