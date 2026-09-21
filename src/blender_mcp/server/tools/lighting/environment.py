"""Typed MCP tools for world background, HDRI, and procedural-sky lighting."""

from pathlib import Path
from typing import Annotated, Literal

from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field, model_validator

from ...app import mcp
from .._dispatch import call_blender
from ._shared import StrictLightingInput, TargetEngine, dump_input


class ProceduralSkySettings(StrictLightingInput):
    """Physical controls supported by Blender's Sky Texture node."""

    sky_type: Literal["MULTIPLE_SCATTERING", "SINGLE_SCATTERING", "PREETHAM", "HOSEK_WILKIE"] = "MULTIPLE_SCATTERING"
    sun_elevation: float = Field(default=0.7853981633974483, ge=-1.5707963267948966, le=1.5707963267948966)
    sun_rotation: float = Field(default=0.0, ge=-6.283185307179586, le=6.283185307179586)
    altitude: float = Field(default=0.0, ge=0, le=100000)
    air_density: float = Field(default=1.0, ge=0, le=10)
    dust_density: float = Field(default=1.0, ge=0, le=10)
    ozone_density: float = Field(default=1.0, ge=0, le=10)
    sun_size: float = Field(default=0.00918043, gt=0, le=1.5707963267948966)
    sun_intensity: float = Field(default=1.0, ge=0, le=1000)
    sun_disc: bool = True
    background_strength: float = Field(default=1.0, ge=0)

    @model_validator(mode="after")
    def validate_model_controls(self) -> "ProceduralSkySettings":
        # These defaults are exactly representable and every value here is parsed
        # from the request rather than computed, so "differs from its default" is
        # an exact question.
        if self.sky_type not in {"MULTIPLE_SCATTERING", "SINGLE_SCATTERING"} and (
            # ruff: ignore[float-equality-comparison]
            self.altitude != 0.0 or self.air_density != 1.0 or self.dust_density != 1.0 or self.ozone_density != 1.0
        ):
            raise ValueError("altitude and air/dust/ozone density require a scattering sky model")
        return self


@mcp.tool()
async def configure_world_background(
    ctx: Context,
    scene_name: str,
    color: tuple[float, float, float] | None = None,
    strength: Annotated[float | None, Field(ge=0)] = None,
    transparent_film: bool | None = None,
    world_name: str | None = None,
    create_world: bool = False,
) -> dict:
    """
    Create, assign, or patch a managed simple World background.

    This operates on the named scene's assigned world, never the first global World datablock. A
    missing world is created only when ``create_world`` is true and ``world_name`` is provided.
    Managed Background/World Output nodes are reused without clearing unrelated user nodes.
    ``transparent_film`` changes camera visibility, not the amount of world illumination.
    """
    if color is None and strength is None and transparent_film is None:
        raise ToolError("Provide color, strength, or transparent_film")
    if color is not None and any(channel < 0 or channel > 1 for channel in color):
        raise ToolError("color channels must be in [0, 1]")
    if create_world and not world_name:
        raise ToolError("world_name is required when create_world is true")
    return await call_blender(
        "configure_world_background",
        {
            "scene_name": scene_name,
            "color": color,
            "strength": strength,
            "transparent_film": transparent_film,
            "world_name": world_name,
            "create_world": create_world,
        },
    )


@mcp.tool()
async def configure_hdri_environment(
    ctx: Context,
    scene_name: str,
    image_path: str,
    strength: Annotated[float, Field(ge=0)] = 1.0,
    rotation: float = 0.0,
    projection: Literal["EQUIRECTANGULAR", "MIRROR_BALL"] = "EQUIRECTANGULAR",
    replacement_policy: Literal["REPLACE_MANAGED", "ERROR_IF_MANAGED"] = "REPLACE_MANAGED",
    world_name: str | None = None,
    create_world: bool = False,
    transparent_film: bool | None = None,
) -> dict:
    """
    Configure a persistent HDR/EXR environment without clearing user-authored world nodes.

    ``image_path`` must be an existing absolute .hdr or .exr file that will remain available after
    this call. The tool reuses Blender image datablocks, builds a tagged Texture Coordinate → Mapping
    → Environment → Background → World Output chain, and reports the active OCIO color space rather
    than forcing Non-Color. Rotation is radians around world Z. The replacement policy applies only
    to the MCP-managed world surface chain.
    """
    path = Path(image_path)
    if not path.is_absolute():
        raise ToolError("image_path must be absolute")
    if path.suffix.lower() not in {".hdr", ".exr"}:
        raise ToolError("image_path must use .hdr or .exr")
    if create_world and not world_name:
        raise ToolError("world_name is required when create_world is true")
    return await call_blender(
        "configure_hdri_environment",
        {
            "scene_name": scene_name,
            "image_path": str(path),
            "strength": strength,
            "rotation": rotation,
            "projection": projection,
            "replacement_policy": replacement_policy,
            "world_name": world_name,
            "create_world": create_world,
            "transparent_film": transparent_film,
        },
    )


@mcp.tool()
async def configure_procedural_sky(
    ctx: Context,
    scene_name: str,
    settings: ProceduralSkySettings,
    target_engine: TargetEngine = "BOTH",
    sync_sun: bool = False,
    sun_name: str | None = None,
    sun_collection_name: str = "Lighting",
    sun_energy: Annotated[float, Field(ge=0)] = 1.0,
    world_name: str | None = None,
    create_world: bool = False,
) -> dict:
    """
    Configure a physical sky and optionally synchronize a Sun light.

    The managed Sky → Background → World Output chain preserves unrelated nodes. Blender documents
    the sky sun disc as Cycles-only; for ``BOTH`` or ``EEVEE``, use ``sync_sun`` with a stable
    ``sun_name`` to create or update an EEVEE-compatible direct-light source. The response warns
    when both the sky disc and Sun could double direct illumination.
    """
    if sync_sun and not sun_name:
        raise ToolError("sun_name is required when sync_sun is true")
    if create_world and not world_name:
        raise ToolError("world_name is required when create_world is true")
    return await call_blender(
        "configure_procedural_sky",
        {
            "scene_name": scene_name,
            "settings": dump_input(settings),
            "target_engine": target_engine,
            "sync_sun": sync_sun,
            "sun_name": sun_name,
            "sun_collection_name": sun_collection_name,
            "sun_energy": sun_energy,
            "world_name": world_name,
            "create_world": create_world,
        },
    )
