# ruff: file-ignore[multi-line-summary-second-line, unused-async]
"""Agent-facing specialized retopology accelerators: quadriflow, primitive fitting, surface deform, LODs."""

from typing import Annotated, Any, Literal

from mcp.server.fastmcp import Context
from pydantic import Field

from ...app import mcp
from ..envelope import ok
from ._shared import RetopologyProfile, _call

QuadriFlowMode = Literal["RATIO", "EDGE", "FACES"]
PrimitiveType = Literal["PLANE", "CYLINDER", "CONE", "SPHERE"]
SurfaceDeformAction = Literal["BIND", "UNBIND"]


@mcp.tool()
async def generate_quadriflow_draft(
    ctx: Context,
    source_object_name: str,
    name: str | None = None,
    collection_name: str = "Retopology Drafts",
    mode: QuadriFlowMode = "FACES",
    target_faces: Annotated[int, Field(ge=1)] = 4000,
    target_ratio: Annotated[float, Field(gt=0, le=1)] = 1.0,
    target_edge_length: Annotated[float, Field(gt=0)] = 0.1,
    use_mesh_symmetry: bool = False,
    preserve_sharp: bool = False,
    preserve_boundary: bool = False,
    preserve_attributes: bool = True,
    smooth_normals: bool = True,
    seed: Annotated[int, Field(ge=0)] = 0,
    validation_profile: RetopologyProfile = "CHARACTER",
) -> dict:
    """Create a QuadriFlow draft from a source's evaluated surface.

    The source object and its live modifiers are never changed. A standalone
    evaluated copy is placed in `collection_name`, then Blender 5.1's
    QuadriFlow operator is run with exactly one active sizing mode: FACES,
    RATIO, or EDGE. Edge length is measured in the candidate's local space.
    The deterministic non-negative `seed` is forwarded to the solver.

    Blender documents that QuadriFlow rebuilds the mesh and can lose data
    layers, so the result compares UV, color, generic attribute, vertex-group,
    and material layers before/after and runs `validate_retopology`. The output
    is always identified as a draft, never as deformation-ready topology.
    """
    params = {key: value for key, value in locals().items() if key != "ctx"}
    result = _call("generate_quadriflow_draft", params)
    return ok(result, changed_objects=[result["name"]])


@mcp.tool()
async def fit_surface_primitive(
    ctx: Context,
    source_object_name: str,
    primitive: PrimitiveType,
    source_vertex_indices: list[int],
    expected_source_revision: str,
    name: str | None = None,
    collection_name: str = "Retopology",
    u_segments: Annotated[int, Field(ge=3, le=1000)] = 16,
    v_segments: Annotated[int, Field(ge=1, le=1000)] = 4,
    project_to_source: bool = True,
    projection_offset: float = 0.0,
    max_fit_residual: Annotated[float, Field(ge=0)] | None = None,
    axis_hint_world: tuple[float, float, float] | None = None,
) -> dict:
    """Fit a deterministic quad primitive to explicit source samples.

    Source indices address the base mesh and require a fresh
    `expected_source_revision` from `inspect_retopology`. Fitting happens in world space:
    PLANE uses PCA, CYLINDER/CONE compare all PCA axes and reject ambiguous
    axes, and SPHERE uses an algebraic least-squares fit. Supply a nonzero
    world-space `axis_hint_world` to disambiguate CYLINDER/CONE regions whose
    PCA axes fit similarly. The generated object
    retains the source transform while storing local coordinates. PLANE uses a
    U/V grid, CYLINDER and CONE use U around and V along their axis, and SPHERE
    uses a closed all-quad cube-sphere with U subdivisions per cube face
    (`v_segments` is validated but not used for SPHERE).

    `max_fit_residual`, when set, is an absolute world-space limit on the
    largest sample residual. Projection uses nearest points on the evaluated
    source after fitting. The result includes fit parameters, mean/RMS/max
    residuals, projection misses, counts, and topology revision.
    """
    params = {key: value for key, value in locals().items() if key != "ctx"}
    result = _call("fit_surface_primitive", params)
    return ok(result, changed_objects=[result["name"]])


@mcp.tool()
async def bind_surface_deformation(
    ctx: Context,
    object_name: str,
    action: SurfaceDeformAction,
    target_object_name: str | None = None,
    modifier_name: str = "RetopologySurfaceDeform",
    falloff: Annotated[float, Field(ge=2, le=16)] = 4.0,
    strength: Annotated[float, Field(ge=-100, le=100)] = 1.0,
    vertex_group: str | None = None,
    invert_vertex_group: bool = False,
    sparse_bind: bool = False,
    duplicate_tolerance: Annotated[float, Field(ge=0)] = 1e-6,
) -> dict:
    """Bind or unbind a mesh through a live Surface Deform modifier.

    BIND requires a distinct mesh `target_object_name`. Before creating or
    changing the modifier, the evaluated target is checked for every condition
    Blender 5.1 documents as preventing binding: edges with more than two
    faces, concave faces, overlapping vertices, and collinear face edges.
    `duplicate_tolerance` is measured in world space. Falloff must be in
    [2, 16], strength in [-100, 100], and a named vertex group must already
    exist. Sparse bind records only vertices with nonzero group weights at bind
    time, so later group additions require rebinding.

    UNBIND is idempotent and ignores target/configuration values. Both actions
    preserve the caller's active object, selection, and mode, verify the
    operator result and final `is_bound` state, and keep the modifier live.
    """
    params = {key: value for key, value in locals().items() if key != "ctx"}
    result = _call("bind_surface_deformation", params)
    changed = [object_name] if result.get("changed", True) else []
    return ok(result, changed_objects=changed)


@mcp.tool()
async def generate_retopology_lods(
    ctx: Context,
    object_name: str,
    levels: list[dict[str, Any]],
    profile: RetopologyProfile = "GAME",
    collection_name: str = "Retopology LODs",
    source_object_name: str | None = None,
    reproject: bool = False,
    projection_offset: float = 0.0,
    transfer_data_types: list[
        Literal[
            "VERTEX_GROUPS",
            "UVS",
            "COLOR_ATTRIBUTES",
            "CUSTOM_NORMALS",
            "SEAMS",
            "CREASES",
            "BEVEL_WEIGHTS",
            "SHARP_EDGES",
            "SMOOTH_SHADING",
            "MATERIAL_INDICES",
        ]
    ]
    | None = None,
    confirm: bool = False,
) -> dict:
    """Generate materialized, validated LOD meshes from an approved master.

    This operation requires `confirm=True` because it applies Decimate or
    QuadriFlow to newly created derivatives. The master and its modifier stack
    remain untouched; each LOD begins as an evaluated data-layer-preserving
    copy. `levels` is a list of objects with a strictly decreasing `ratio` in
    (0, 1), optional collision-safe `name`, and `method` DECIMATE (default) or
    QUADRIFLOW. DECIMATE levels may set `use_symmetry`, `symmetry_axis`,
    `vertex_group`, `vertex_group_factor`, and `invert_vertex_group`;
    QUADRIFLOW levels may set `seed`, `preserve_sharp`,
    `preserve_boundary`, `preserve_attributes`, and `smooth_normals`.

    Ratios are always relative to the evaluated master's polygon count.
    Reprojection requires an explicit `source_object_name` and moves vertices
    to nearest points on that evaluated surface. Requested production data is
    then transferred and materialized. Every level is checked with the chosen
    CHARACTER/HARD_SURFACE/VFX/GAME validation profile and returned with its
    actual counts, revision, projection misses, and validation report.
    """
    params = {key: value for key, value in locals().items() if key != "ctx"}
    result = _call("generate_retopology_lods", params)
    return ok(result, changed_objects=result["created_objects"])
