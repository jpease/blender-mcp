# MCP tool signatures intentionally expose more than five keyword arguments so
# agents receive precise JSON schemas instead of opaque catch-all dictionaries.
"""Typed tool for adding a reversible render-only modifier stack after Cloth."""

from typing import Annotated, Literal

from mcp.server.fastmcp import Context
from pydantic import Field

from ...app import mcp
from .._dispatch import call_blender
from .._inputs import StrictModel, dump_input
from .inspection_and_setup import ExistingPolicy


class CorrectiveSmoothPatch(StrictModel):
    """Allowlisted post-cloth Corrective Smooth controls."""

    factor: float | None = None
    iterations: Annotated[int, Field(ge=0)] | None = None
    scale: Annotated[float, Field(gt=0)] | None = None
    rest_source: Literal["ORCO", "BIND"] | None = None
    smooth_type: Literal["SIMPLE", "LENGTH_WEIGHTED"] | None = None
    use_only_smooth: bool | None = None
    use_pin_boundary: bool | None = None
    vertex_group: Annotated[str, Field(min_length=1)] | None = None


class ClothSubdivisionPatch(StrictModel):
    """Allowlisted post-cloth Subdivision Surface controls."""

    levels: Annotated[int, Field(ge=0, le=6)] | None = None
    render_levels: Annotated[int, Field(ge=0, le=6)] | None = None
    quality: int | None = None
    subdivision_type: Literal["CATMULL_CLARK", "SIMPLE"] | None = None
    uv_smooth: (
        Literal[
            "NONE",
            "PRESERVE_CORNERS",
            "PRESERVE_CORNERS_AND_JUNCTIONS",
            "PRESERVE_CORNERS_JUNCTIONS_AND_CONCAVE",
            "PRESERVE_BOUNDARIES",
            "SMOOTH_ALL",
        ]
        | None
    ) = None
    use_creases: bool | None = None


class ClothSolidifyPatch(StrictModel):
    """Allowlisted post-cloth Solidify controls."""

    thickness: float | None = None
    offset: Annotated[float, Field(ge=-1, le=1)] | None = None
    material_offset: int | None = None
    material_offset_rim: int | None = None
    use_even_offset: bool | None = None
    use_quality_normals: bool | None = None
    use_rim: bool | None = None


class ClothWeightedNormalPatch(StrictModel):
    """Allowlisted Blender 5.1 Weighted Normal controls."""

    weight: Annotated[int, Field(ge=1, le=100)] | None = None
    mode: Literal["FACE_AREA", "CORNER_ANGLE", "FACE_AREA_WITH_ANGLE"] | None = None
    thresh: Annotated[float, Field(ge=0)] | None = None
    keep_sharp: bool | None = None
    use_face_influence: bool | None = None


@mcp.tool()
async def prepare_cloth_render_surface(
    ctx: Context,
    object_name: str,
    cloth_modifier_name: str,
    corrective_smooth: CorrectiveSmoothPatch | None = None,
    subdivision: ClothSubdivisionPatch | None = None,
    solidify: ClothSolidifyPatch | None = None,
    weighted_normal: ClothWeightedNormalPatch | None = None,
    corrective_smooth_name: str = "Cloth Corrective Smooth",
    subdivision_name: str = "Cloth Render Subdivision",
    solidify_name: str = "Cloth Render Thickness",
    weighted_normal_name: str = "Cloth Weighted Normal",
    existing_policy: ExistingPolicy = "ERROR",
    rest_frame: Annotated[int, Field(ge=0)] = 1,
) -> dict:
    """
    Add or update a reversible render-only modifier stack after Cloth.

    Requested modifiers are ordered Corrective Smooth, Subdivision, Solidify, then Weighted Normal.
    Nothing is applied, source geometry/materials/UVs are retained, and evaluated cost evidence is
    returned. Existing modifiers are reused only when explicitly requested.
    """
    return await call_blender(
        "prepare_cloth_render_surface",
        {
            "object_name": object_name,
            "cloth_modifier_name": cloth_modifier_name,
            "corrective_smooth": dump_input(corrective_smooth),
            "subdivision": dump_input(subdivision),
            "solidify": dump_input(solidify),
            "weighted_normal": dump_input(weighted_normal),
            "corrective_smooth_name": corrective_smooth_name,
            "subdivision_name": subdivision_name,
            "solidify_name": solidify_name,
            "weighted_normal_name": weighted_normal_name,
            "existing_policy": existing_policy,
            "rest_frame": rest_frame,
        },
        changed_objects=[object_name],
    )
