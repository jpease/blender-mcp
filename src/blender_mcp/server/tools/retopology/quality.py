"""Agent-facing tools for measuring and validating retopology quality."""

from typing import Annotated

from mcp.server.fastmcp import Context
from pydantic import Field

from ...app import mcp
from ..envelope import ok
from ._shared import RetopologyProfile, _call


@mcp.tool()
async def analyze_surface_conformity(
    ctx: Context,
    object_name: str,
    source_object_name: str,
    sample_vertices: bool = True,
    sample_edge_midpoints: bool = False,
    sample_face_centroids: bool = False,
    max_distance: Annotated[float, Field(gt=0)] | None = None,
    worst_limit: Annotated[int, Field(ge=1, le=1000)] = 20,
    create_heat_map: bool = False,
    attribute_name: str = "retopology_distance",
) -> dict:
    """
    Measure world-space distance from a target to an evaluated source.

    The source BVH includes its live modifiers. Results include mean, RMS,
    p50/p90/p95/p99, maximum, signed offsets where the nearest triangle normal
    gives a reliable orientation, missed samples, and the worst element IDs.
    Enable edge/face samples when sparse target vertices could hide poor
    conformity. A POINT/FLOAT heat-map attribute is created only when
    `create_heat_map=True`; its values always represent vertex samples.
    """
    result = _call(
        "analyze_surface_conformity",
        {
            "object_name": object_name,
            "source_object_name": source_object_name,
            "sample_vertices": sample_vertices,
            "sample_edge_midpoints": sample_edge_midpoints,
            "sample_face_centroids": sample_face_centroids,
            "max_distance": max_distance,
            "worst_limit": worst_limit,
            "create_heat_map": create_heat_map,
            "attribute_name": attribute_name,
        },
    )
    return ok(result, changed_objects=[object_name] if create_heat_map else [])


@mcp.tool()
async def validate_retopology(
    ctx: Context,
    object_name: str,
    profile: RetopologyProfile = "CHARACTER",
    source_object_name: str | None = None,
    thresholds: dict[str, float] | None = None,
    check_self_intersections: bool = True,
    check_uv_overlap: bool = True,
    check_skin_weights: bool = True,
    issue_limit: Annotated[int, Field(ge=1, le=2000)] = 100,
) -> dict:
    """
    Return a production pass/warn/fail report for a retopology profile.

    Profiles select documented thresholds rather than enforcing a blanket
    all-quads rule. Checks cover manifoldness and allowed boundaries, doubles,
    degenerates, winding, self-intersections, face aspect and density changes,
    poles, live symmetry, source conformity, UV presence/overlap, skin-weight
    normalization, and modifier readiness/order. `thresholds` may override
    named numeric profile values returned in the report. Mesh.validate() is
    run only on a temporary copy. Long issue lists are capped by `issue_limit`.
    The overall status is FAIL when any fail-severity check fails, WARN when
    only warnings remain, otherwise PASS.
    """
    params = {key: value for key, value in locals().items() if key != "ctx"}
    return ok(_call("validate_retopology", params))


@mcp.tool()
async def test_deformation(
    ctx: Context,
    object_name: str,
    frames: Annotated[list[int], Field(min_length=1)],
    reference_frame: int | None = None,
    source_object_name: str | None = None,
    joint_vertex_groups: list[str] | None = None,
    stretch_warning_ratio: Annotated[float, Field(gt=0)] = 1.25,
    area_warning_ratio: Annotated[float, Field(gt=0)] = 1.5,
    check_self_intersections: bool = True,
    issue_limit: Annotated[int, Field(ge=1, le=2000)] = 100,
) -> dict:
    """
    Evaluate deformation quality at explicit animation frames without editing it.

    The current scene frame is restored in `finally` and no keyframes are
    inserted. Evaluated meshes include the live modifier/armature stack.
    Every requested frame is compared with `reference_frame` (or the current
    frame): edge stretch/compression ratios, face-area ratios, signed volume
    change, flipped face normals, and non-adjacent self-intersections are
    reported. `joint_vertex_groups` restricts detailed worst-element lists to
    caller-declared joint regions while whole-mesh summaries remain available.
    When `source_object_name` is supplied, world-space conformity to that
    evaluated animated source is also measured for joint vertices (or every
    vertex when no joint groups are supplied). This inspection changes no datablocks.
    """
    params = {key: value for key, value in locals().items() if key != "ctx"}
    return ok(_call("test_deformation", params))
