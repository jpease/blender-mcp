"""Rigid-body animation interchange tools."""

from typing import Annotated, Literal

from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from .._dispatch import call_blender
from .inspection_and_setup import mcp


@mcp.tool()
async def export_rigid_body_animation(
    ctx: Context,
    scene_name: str,
    object_names: Annotated[list[str], Field(min_length=1, max_length=100)],
    filepath: Annotated[str, Field(min_length=1)],
    format: Literal["JSON", "ALEMBIC", "USD", "GLTF", "FBX"],
    frame_start: Annotated[int, Field(ge=-1_000_000, le=1_000_000)],
    frame_end: Annotated[int, Field(ge=-1_000_000, le=1_000_000)],
    frame_step: Annotated[int, Field(ge=1, le=120)] = 1,
    coordinate_convention: Literal["BLENDER_Z_UP", "Y_UP_RIGHT_HANDED"] = "BLENDER_Z_UP",
    unit_scale: Annotated[float, Field(gt=0.0, le=10_000.0)] = 1.0,
    confirm_overwrite: bool = False,
) -> dict:
    """
    Export explicit objects and bounded animation without altering the simulation source.

    coordinate_convention is constrained per format: GLTF requires Y_UP_RIGHT_HANDED and ALEMBIC
    currently requires BLENDER_Z_UP; JSON, USD, and FBX accept either. Rejects with
    confirm_overwrite=False if filepath already exists.
    """
    if frame_start > frame_end:
        raise ToolError("frame_start must not exceed frame_end")
    if not filepath.strip():
        raise ToolError("filepath must be non-empty")
    if len(object_names) != len(set(object_names)):
        raise ToolError("object_names must be unique")
    if format == "GLTF" and coordinate_convention != "Y_UP_RIGHT_HANDED":
        raise ToolError("GLTF uses Y_UP_RIGHT_HANDED coordinates")
    if format == "ALEMBIC" and coordinate_convention != "BLENDER_Z_UP":
        raise ToolError("ALEMBIC export currently preserves Blender's Z-up convention")
    return await call_blender(
        "export_rigid_body_animation",
        {
            "scene_name": scene_name,
            "object_names": object_names,
            "filepath": filepath,
            "format": format,
            "frame_start": frame_start,
            "frame_end": frame_end,
            "frame_step": frame_step,
            "coordinate_convention": coordinate_convention,
            "unit_scale": unit_scale,
            "confirm_overwrite": confirm_overwrite,
        },
        changed_objects=object_names,
    )
