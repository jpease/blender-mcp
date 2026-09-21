"""Cycles-backed texture baking exposed with explicit sources and output."""

from typing import Literal

from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from ...app import mcp
from .._dispatch import call_blender
from ._shared import absolute_path


@mcp.tool()
async def bake_texture_map(
    ctx: Context,
    object_name: str,
    map_type: Literal[
        "NORMAL",
        "AO",
        "COMBINED",
        "DIFFUSE",
        "GLOSSY",
        "ROUGHNESS",
        "EMISSION",
        "POSITION",
        "SHADOW",
        "UV",
        "BASE_COLOR",
        "METALLIC",
        "OPACITY",
    ],
    output_path: str,
    high_poly_object_names: list[str] | None = None,
    width: int = Field(default=2048, ge=1, le=16384),
    height: int = Field(default=2048, ge=1, le=16384),
    uv_map_name: str | None = None,
    cage_object_name: str | None = None,
    cage_extrusion: float = Field(default=0, ge=0),
    max_ray_distance: float = Field(default=0, ge=0),
    margin: int = Field(default=16, ge=0, le=1024),
    normal_space: Literal["TANGENT", "OBJECT"] = "TANGENT",
    normal_swizzle: tuple[
        Literal["POS_X", "NEG_X", "POS_Y", "NEG_Y", "POS_Z", "NEG_Z"],
        Literal["POS_X", "NEG_X", "POS_Y", "NEG_Y", "POS_Z", "NEG_Z"],
        Literal["POS_X", "NEG_X", "POS_Y", "NEG_Y", "POS_Z", "NEG_Z"],
    ] = ("POS_X", "POS_Y", "POS_Z"),
    target_engine: Literal["CYCLES", "EEVEE", "BLENDER_EEVEE_NEXT"] = "CYCLES",
    overwrite: bool = False,
    confirm: bool = False,
) -> dict:
    """
    Bake one native or semantic map atomically through Cycles to an explicit file.

    `confirm=True` is required because baking is expensive and writes a file. Omit high-poly sources
    for same-object baking. BASE_COLOR, METALLIC, and OPACITY are temporarily routed through
    emission without modifying original materials. Blender context and render engine are restored.
    """
    if not confirm:
        raise ToolError("Set confirm=True to run the bake")
    params = {k: v for k, v in locals().items() if k != "ctx"}
    params["output_path"] = absolute_path(output_path, "output_path")
    return await call_blender("bake_texture_map", params, changed_objects=[object_name])
