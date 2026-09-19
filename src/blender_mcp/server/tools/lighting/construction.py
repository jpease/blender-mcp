"""Typed MCP tools for creating, configuring, aiming, and linking lights."""

import asyncio

from typing import Annotated, Literal

from mcp.server.fastmcp import Context, Image
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field, model_validator

from ...app import mcp
from ._shared import LightType, StrictLightingInput, StudioLightingMood, call_blender, dump_input
from .rendering import render_lighting_preview


class LightSettings(StrictLightingInput):
    """Allowlisted shared and type-specific settings for a Blender light datablock."""

    energy: float = Field(default=1000.0, gt=0)
    exposure: float = Field(default=0.0, ge=-32, le=32)
    normalize: bool = True
    color: tuple[float, float, float] | None = None
    use_temperature: bool = False
    temperature: float | None = Field(default=None, ge=800, le=20000)
    use_shadow: bool = True
    diffuse_factor: float = Field(default=1.0, ge=0)
    specular_factor: float = Field(default=1.0, ge=0)
    transmission_factor: float = Field(default=1.0, ge=0)
    volume_factor: float = Field(default=1.0, ge=0)
    use_custom_distance: bool = False
    cutoff_distance: float | None = Field(default=None, gt=0)
    shape: Literal["SQUARE", "RECTANGLE", "DISK", "ELLIPSE"] | None = None
    size: float | None = Field(default=None, gt=0)
    size_y: float | None = Field(default=None, gt=0)
    spread: float | None = Field(default=None, ge=0, le=3.141592653589793)
    shadow_soft_size: float | None = Field(default=None, ge=0)
    use_soft_falloff: bool | None = None
    spot_size: float | None = Field(default=None, ge=0.017453292519943295, le=3.141592653589793)
    spot_blend: float | None = Field(default=None, ge=0, le=1)
    show_cone: bool | None = None
    angle: float | None = Field(default=None, ge=0, le=3.141592653589793)

    @model_validator(mode="after")
    def validate_color_and_cutoff(self) -> "LightSettings":
        if self.color is not None and any(channel < 0 or channel > 1 for channel in self.color):
            raise ValueError("color channels must be in [0, 1]")
        if self.use_temperature and self.temperature is None:
            raise ValueError("temperature is required when use_temperature is true")
        if self.use_custom_distance and self.cutoff_distance is None:
            raise ValueError("cutoff_distance is required when use_custom_distance is true")
        return self


class LightPatch(StrictLightingInput):
    """Allowlisted partial update for one existing light; omitted fields remain unchanged."""

    energy: float | None = Field(default=None, gt=0)
    exposure: float | None = Field(default=None, ge=-32, le=32)
    normalize: bool | None = None
    color: tuple[float, float, float] | None = None
    use_temperature: bool | None = None
    temperature: float | None = Field(default=None, ge=800, le=20000)
    use_shadow: bool | None = None
    diffuse_factor: float | None = Field(default=None, ge=0)
    specular_factor: float | None = Field(default=None, ge=0)
    transmission_factor: float | None = Field(default=None, ge=0)
    volume_factor: float | None = Field(default=None, ge=0)
    use_custom_distance: bool | None = None
    cutoff_distance: float | None = Field(default=None, gt=0)
    shape: Literal["SQUARE", "RECTANGLE", "DISK", "ELLIPSE"] | None = None
    size: float | None = Field(default=None, gt=0)
    size_y: float | None = Field(default=None, gt=0)
    spread: float | None = Field(default=None, ge=0, le=3.141592653589793)
    shadow_soft_size: float | None = Field(default=None, ge=0)
    use_soft_falloff: bool | None = None
    spot_size: float | None = Field(default=None, ge=0.017453292519943295, le=3.141592653589793)
    spot_blend: float | None = Field(default=None, ge=0, le=1)
    show_cone: bool | None = None
    angle: float | None = Field(default=None, ge=0, le=3.141592653589793)

    @model_validator(mode="after")
    def validate_color(self) -> "LightPatch":
        if self.color is not None and any(channel < 0 or channel > 1 for channel in self.color):
            raise ValueError("color channels must be in [0, 1]")
        return self


@mcp.tool()
async def create_light(
    ctx: Context,
    scene_name: str,
    collection_name: str,
    name: str,
    light_type: LightType,
    location: tuple[float, float, float],
    rotation_euler: tuple[float, float, float] = (0.0, 0.0, 0.0),
    settings: LightSettings | None = None,
) -> dict:
    """
    Create one collision-safe Point, Spot, Area, or Sun light.

    Location and XYZ Euler rotation are world-space. The light is linked only to the named scene
    collection and does not disturb selection. Use type-specific settings only with their matching
    light type. Energy, exposure, normalization, dimensions, and scene unit scale are returned so
    unlike light types are not mistaken for directly comparable power values.
    """
    payload = settings or LightSettings()
    return await asyncio.to_thread(
        call_blender,
        "create_light",
        {
            "scene_name": scene_name,
            "collection_name": collection_name,
            "name": name,
            "light_type": light_type,
            "location": location,
            "rotation_euler": rotation_euler,
            "settings": dump_input(payload),
        },
    )


@mcp.tool()
async def configure_light(ctx: Context, light_name: str, patch: LightPatch) -> dict:
    """
    Patch only supplied settings on one existing light.

    The tool rejects settings that do not apply to the light's actual type and returns before/after
    values. If the light datablock is shared, every object user is reported as changed. It never
    accepts a free-form RNA property name and does not change transforms or nodes.
    """
    payload = dump_input(patch)
    if not payload:
        raise ToolError("Provide at least one light setting to change")
    return await asyncio.to_thread(
        call_blender,
        "configure_light",
        {"light_name": light_name, "patch": payload},
        [light_name],
    )


@mcp.tool()
async def aim_light(
    ctx: Context,
    scene_name: str,
    light_name: str,
    target_point: tuple[float, float, float] | None = None,
    target_object_name: str | None = None,
    target_bone_name: str | None = None,
    bounds_position: Literal["CENTER", "TOP", "BOTTOM"] = "CENTER",
    method: Literal["STATIC_ROTATION", "TRACK_TO", "DAMPED_TRACK"] = "STATIC_ROTATION",
    constraint_name: str = "MCP Light Aim",
    helper_name: str | None = None,
    helper_collection_name: str = "Lighting Helpers",
) -> dict:
    """
    Aim a Spot, Area, Sun, or Point light at one explicit target.

    Supply exactly one target point or object. A bone requires its armature object. Object bounds can
    target center/top/bottom after evaluated transforms. Blender lights emit along local -Z. Static
    aiming writes a parent-safe world rotation; live TRACK_TO/DAMPED_TRACK aiming retains a named
    constraint. A world point or evaluated bounds target needs ``helper_name`` for live aiming and
    creates a tagged Empty in the helpers collection.
    """
    if (target_point is None) == (target_object_name is None):
        raise ToolError("Supply exactly one of target_point or target_object_name")
    if target_bone_name is not None and target_object_name is None:
        raise ToolError("target_bone_name requires target_object_name")
    if method != "STATIC_ROTATION" and (target_point is not None or bounds_position != "CENTER") and not helper_name:
        raise ToolError("helper_name is required for a live point or evaluated-bounds target")
    return await asyncio.to_thread(
        call_blender,
        "aim_light",
        {
            "scene_name": scene_name,
            "light_name": light_name,
            "target_point": target_point,
            "target_object_name": target_object_name,
            "target_bone_name": target_bone_name,
            "bounds_position": bounds_position,
            "method": method,
            "constraint_name": constraint_name,
            "helper_name": helper_name,
            "helper_collection_name": helper_collection_name,
        },
        [light_name],
    )


@mcp.tool()
async def configure_light_linking(
    ctx: Context,
    scene_name: str,
    light_name: str,
    receiver_collection_name: str | None = None,
    blocker_collection_name: str | None = None,
    clear_receivers: bool = False,
    clear_blockers: bool = False,
) -> dict:
    """
    Set or clear the collections that receive or block one light.

    Collections must already exist and be linked to the scene; this tool never moves objects or
    creates hidden membership changes. Omitted sides remain unchanged. The result expands the full
    effective collection membership and reports engine-support caveats.
    """
    if receiver_collection_name is not None and clear_receivers:
        raise ToolError("Choose receiver_collection_name or clear_receivers, not both")
    if blocker_collection_name is not None and clear_blockers:
        raise ToolError("Choose blocker_collection_name or clear_blockers, not both")
    if (
        receiver_collection_name is None
        and blocker_collection_name is None
        and not clear_receivers
        and not clear_blockers
    ):
        raise ToolError("Request at least one receiver or blocker change")
    return await asyncio.to_thread(
        call_blender,
        "configure_light_linking",
        {
            "scene_name": scene_name,
            "light_name": light_name,
            "receiver_collection_name": receiver_collection_name,
            "blocker_collection_name": blocker_collection_name,
            "clear_receivers": clear_receivers,
            "clear_blockers": clear_blockers,
        },
        [light_name],
    )


@mcp.tool(structured_output=False)
async def create_studio_lighting(
    ctx: Context,
    scene_name: str,
    target_object_name: str,
    camera_name: str,
    frame: int,
    mood: StudioLightingMood = "SOFT",
    key_ratio: Annotated[float | None, Field(gt=0)] = None,
    rig_name: str | None = None,
    collection_name: str = "Studio Lighting",
    preview_engine: Literal["CYCLES", "EEVEE"] = "EEVEE",
    preview_width: Annotated[int, Field(ge=16, le=1024)] = 512,
    preview_height: Annotated[int, Field(ge=16, le=1024)] = 512,
    preview_samples: Annotated[int, Field(ge=1, le=1024)] = 32,
    preview_output_path: str | None = None,
    confirm_overwrite: bool = False,
    confirm_long_render: bool = False,
) -> list[Image | dict]:
    """
    Build a preset key/fill/rim AREA-light rig around a target and render a preview.

    A one-call preset layer over create_light + aim_light: ratio, softbox sizing, and placement
    conventions for the requested mood are encoded here instead of left to the caller. The rig is
    sized off the target's evaluated bounding box and aimed using the given camera as the front
    reference axis, which is also the camera used for the mandatory render_lighting_preview call.
    Returns the rig-creation result (names and roles of the created lights), the preview image(s),
    and the preview's own result envelope, in that order.
    """
    rig_result = await asyncio.to_thread(
        call_blender,
        "create_studio_lighting",
        {
            "scene_name": scene_name,
            "target_object_name": target_object_name,
            "camera_name": camera_name,
            "mood": mood,
            "key_ratio": key_ratio,
            "rig_name": rig_name,
            "collection_name": collection_name,
        },
    )
    preview = await render_lighting_preview(
        ctx,
        scene_name=scene_name,
        camera_name=camera_name,
        frame=frame,
        target_engine=preview_engine,
        width=preview_width,
        height=preview_height,
        samples=preview_samples,
        output_path=preview_output_path,
        confirm_overwrite=confirm_overwrite,
        confirm_long_render=confirm_long_render,
    )
    return [rig_result, *preview]
