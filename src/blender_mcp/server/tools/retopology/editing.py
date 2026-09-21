"""Agent-facing tools for editing existing retopology geometry: projection, edge flow, symmetry."""

from typing import Annotated, Literal

from mcp.server.fastmcp import Context
from pydantic import Field

from ...app import mcp
from .._dispatch import call_blender
from ..envelope import STALE_INDEX_WARNING

ProjectionMethod = Literal["NEAREST", "RAYCAST"]
RerouteAction = Literal["CONNECT", "ROTATE_DIAGONAL", "COLLAPSE", "DISSOLVE", "SPLIT"]


@mcp.tool()
async def configure_surface_projection(
    ctx: Context,
    object_name: str,
    target_object_name: str,
    modifier_name: str = "RetopologyProjection",
    wrap_method: Literal[
        "NEAREST_SURFACEPOINT", "PROJECT", "NEAREST_VERTEX", "TARGET_PROJECT"
    ] = "NEAREST_SURFACEPOINT",
    wrap_mode: Literal["ON_SURFACE", "INSIDE", "OUTSIDE", "OUTSIDE_SURFACE", "ABOVE_SURFACE"] = "ON_SURFACE",
    offset: float = 0.0,
    project_limit: Annotated[float, Field(ge=0)] = 0.0,
    project_axes: tuple[bool, bool, bool] = (False, False, True),
    positive_direction: bool = True,
    negative_direction: bool = True,
    cull_face: Literal["OFF", "FRONT", "BACK"] = "OFF",
    invert_cull: bool = False,
    auxiliary_target_name: str | None = None,
    vertex_group: str = "",
    invert_vertex_group: bool = False,
    apply: bool = False,
) -> dict:
    """
    Idempotently create or update one named live Shrinkwrap relationship.

    Recalling with the same `modifier_name` updates that modifier instead of
    stacking another. PROJECT uses the selected local projection axes and
    direction flags; `project_limit=0` means unlimited. A non-empty
    `vertex_group` must already exist. The exact post-call modifier order is
    returned. The modifier stays live unless `apply=True`, which bakes its
    evaluated result into the base mesh and invalidates prior indices.
    """
    params = {key: value for key, value in locals().items() if key != "ctx"}
    warnings = [STALE_INDEX_WARNING] if apply else []
    return await call_blender("configure_surface_projection", params, changed_objects=[object_name], warnings=warnings)


@mcp.tool()
async def project_mesh_elements(
    ctx: Context,
    object_name: str,
    source_object_name: str,
    vertex_indices: list[int] | None = None,
    vertex_group: str | None = None,
    method: ProjectionMethod = "NEAREST",
    direction: tuple[float, float, float] = (0.0, 0.0, -1.0),
    direction_space: Literal["LOCAL", "WORLD"] = "WORLD",
    offset: float = 0.0,
    max_distance: Annotated[float, Field(gt=0)] | None = None,
    positive_direction: bool = True,
    negative_direction: bool = False,
    backface_policy: Literal["ALLOW", "CULL"] = "ALLOW",
    preserve_boundary: bool = False,
    preserve_symmetry_axis: Literal["NONE", "X", "Y", "Z"] = "NONE",
    symmetry_tolerance: Annotated[float, Field(ge=0)] = 0.0001,
    expected_revision: str | None = None,
) -> dict:
    """
    Project explicit target vertices onto an evaluated source without snapping.

    Supply exactly one of `vertex_indices` or `vertex_group`. NEAREST uses the
    closest BVH point. RAYCAST tries the requested positive and/or negative
    direction and chooses the nearest acceptable hit. Direction vectors are
    interpreted in `direction_space`; positions and distances are always
    processed in world space, then written back to target-local coordinates.
    All indices and revision are validated before mutation. Boundary or
    symmetry-plane vertices can be retained. Failed vertex IDs are reported.
    """
    params = {key: value for key, value in locals().items() if key != "ctx"}
    return await call_blender("project_mesh_elements", params, changed_objects=[object_name])


@mcp.tool()
async def reroute_topology(
    ctx: Context,
    object_name: str,
    action: RerouteAction,
    vertex_indices: list[int] | None = None,
    edge_indices: list[int] | None = None,
    cuts: Annotated[int, Field(ge=1, le=1000)] = 1,
    expected_revision: str | None = None,
) -> dict:
    """
    Perform one bounded local edge-flow correction.

    CONNECT needs two vertices, ROTATE_DIAGONAL one interior edge shared by
    two triangles, COLLAPSE one or more edges, DISSOLVE one or more edges, and
    SPLIT one or more edges plus `cuts`. Inputs are prevalidated on a copied
    BMesh; results that introduce non-manifold edges, duplicate faces, or new
    unintended boundaries are rejected before the real mesh is changed.
    Returns created/removed element IDs where stable plus a new revision.
    """
    params = {key: value for key, value in locals().items() if key != "ctx"}
    return await call_blender("reroute_topology", params, changed_objects=[object_name], warnings=[STALE_INDEX_WARNING])


@mcp.tool()
async def relax_topology(
    ctx: Context,
    object_name: str,
    vertex_indices: list[int],
    iterations: Annotated[int, Field(ge=1, le=1000)] = 3,
    factor: Annotated[float, Field(ge=0, le=1)] = 0.5,
    lock_boundary: bool = True,
    lock_vertex_group: str | None = None,
    source_object_name: str | None = None,
    projection_offset: float = 0.0,
    expected_revision: str | None = None,
) -> dict:
    """
    Tangentially smooth a bounded patch while retaining its source shape.

    Each iteration computes adjacency-based Laplacian movement, removes its
    component along the current vertex normal, applies `factor` in [0, 1],
    and optionally reprojects to the evaluated source. Boundary vertices and
    vertices with non-zero weight in `lock_vertex_group` can be fixed. This
    native implementation does not require LoopTools and does not change
    topology, so indices remain valid when the revision matches.
    """
    params = {key: value for key, value in locals().items() if key != "ctx"}
    return await call_blender("relax_topology", params, changed_objects=[object_name])


@mcp.tool()
async def redistribute_edge_loop(
    ctx: Context,
    object_name: str,
    loop_vertex_indices: list[int],
    closed: bool = False,
    preserve_endpoints: bool = True,
    corner_vertex_indices: list[int] | None = None,
    source_object_name: str | None = None,
    projection_offset: float = 0.0,
    expected_revision: str | None = None,
) -> dict:
    """
    Evenly redistribute an explicitly ordered open or closed edge loop.

    The supplied order must follow existing edges exactly. Positions are
    resampled by cumulative target-local arc length, independently between
    protected corners. Open endpoints remain fixed by default. Optional
    source reprojection keeps the redistributed loop on the evaluated shape.
    This changes positions only, not topology.
    """
    params = {key: value for key, value in locals().items() if key != "ctx"}
    return await call_blender("redistribute_edge_loop", params, changed_objects=[object_name])


@mcp.tool()
async def configure_retopology_symmetry(
    ctx: Context,
    object_name: str,
    axis: Literal["X", "Y", "Z"] = "X",
    mirror_object_name: str | None = None,
    source_side: Literal["POSITIVE", "NEGATIVE"] = "POSITIVE",
    bisect: bool = False,
    clipping: bool = True,
    merge: bool = True,
    merge_tolerance: Annotated[float, Field(ge=0)] = 0.001,
    mirror_vertex_groups: bool = True,
    validate_seam: bool = True,
    symmetry_tolerance: Annotated[float, Field(ge=0)] = 0.001,
    modifier_name: str = "RetopologyMirror",
) -> dict:
    """
    Idempotently configure and validate a live retopology Mirror modifier.

    The plane is the target's local origin unless `mirror_object_name` is
    supplied. `source_side` controls which half survives when `bisect=True`.
    Clipping and merge remain live; the modifier is never applied by this
    tool. KD-tree matching reports unmatched mirrored base vertices and seam
    vertices outside `symmetry_tolerance`, so an agent can repair center-seam
    damage before continuing. The exact modifier order is returned.
    """
    params = {key: value for key, value in locals().items() if key != "ctx"}
    return await call_blender("configure_retopology_symmetry", params, changed_objects=[object_name])
