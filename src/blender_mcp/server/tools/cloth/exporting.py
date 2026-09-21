# MCP tool signatures intentionally expose more than five keyword arguments so
# agents receive precise JSON schemas instead of opaque catch-all dictionaries.
"""Typed tool for exporting cloth objects to Alembic or USD."""

from typing import Annotated, Literal

from mcp.server.fastmcp import Context
from pydantic import Field

from ...app import mcp
from .._dispatch import call_blender

ClothExportFormat = Literal["ALEMBIC", "USD"]
ClothExportSpace = Literal["WORLD", "LOCAL"]
ClothExportUnits = Literal["SCENE", "METERS", "CENTIMETERS", "MILLIMETERS"]
ClothExportAxis = Literal["X", "Y", "Z", "NEGATIVE_X", "NEGATIVE_Y", "NEGATIVE_Z"]
ClothTopologyPolicy = Literal["REQUIRE_STABLE", "ALLOW_VARYING"]
ClothEvaluationPolicy = Literal["REQUIRE_BAKED", "EVALUATE"]


@mcp.tool()
async def export_cloth_simulation(
    ctx: Context,
    scene_name: str,
    filepath: str,
    file_format: ClothExportFormat,
    object_names: Annotated[list[str], Field(min_length=1)],
    frame_start: Annotated[int, Field(ge=0)],
    frame_end: Annotated[int, Field(ge=0)],
    frame_step: Annotated[int, Field(ge=1)],
    coordinate_space: ClothExportSpace,
    units: ClothExportUnits,
    forward_axis: ClothExportAxis,
    up_axis: ClothExportAxis,
    topology_policy: ClothTopologyPolicy,
    evaluation_policy: ClothEvaluationPolicy,
    include_uvs: bool = True,
    include_normals: bool = True,
    include_vertex_colors: bool = True,
    include_materials: bool = True,
    overwrite: bool = False,
    max_frames: Annotated[int, Field(ge=1)] = 500,
) -> dict:
    """
    Export exact cloth objects to Alembic or USD using Blender 5.1's native exporter.

    The path, range, transforms, units, axes, topology policy, attributes, and overwrite boundary
    are explicit. REQUIRE_BAKED rejects unbaked Cloth modifiers; EVALUATE may populate in-memory
    caches. Alembic supports only unit frame steps and Blender's fixed -Z/Y export orientation.
    """
    return await call_blender(
        "export_cloth_simulation",
        {
            "scene_name": scene_name,
            "filepath": filepath,
            "file_format": file_format,
            "object_names": object_names,
            "frame_start": frame_start,
            "frame_end": frame_end,
            "frame_step": frame_step,
            "coordinate_space": coordinate_space,
            "units": units,
            "forward_axis": forward_axis,
            "up_axis": up_axis,
            "topology_policy": topology_policy,
            "evaluation_policy": evaluation_policy,
            "include_uvs": include_uvs,
            "include_normals": include_normals,
            "include_vertex_colors": include_vertex_colors,
            "include_materials": include_materials,
            "overwrite": overwrite,
            "max_frames": max_frames,
        },
        changed_objects=object_names,
    )
