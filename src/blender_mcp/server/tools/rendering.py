"""Typed tools for scene render configuration, view layers, passes, and rendering."""

import asyncio
import contextlib
import logging
import os
import tempfile

from pathlib import Path
from typing import Annotated, Literal

from mcp.server.fastmcp import Context, Image
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..app import mcp
from ..connection import get_blender_connection
from .envelope import envelope_for, ok

logger = logging.getLogger("BlenderMCPServer")


class RenderSettingsPatch(BaseModel):
    """Validated patch for common scene render settings."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    engine: Literal["BLENDER_EEVEE", "BLENDER_WORKBENCH", "CYCLES"] | None = None
    resolution_x: Annotated[int | None, Field(ge=4, le=65_536)] = None
    resolution_y: Annotated[int | None, Field(ge=4, le=65_536)] = None
    resolution_percentage: Annotated[int | None, Field(ge=1, le=100)] = None
    pixel_aspect_x: Annotated[float | None, Field(gt=0, le=200)] = None
    pixel_aspect_y: Annotated[float | None, Field(gt=0, le=200)] = None
    fps: Annotated[int | None, Field(ge=1, le=960)] = None
    fps_base: Annotated[float | None, Field(gt=0, le=1000)] = None
    frame_start: int | None = None
    frame_end: int | None = None
    frame_step: Annotated[int | None, Field(ge=1)] = None
    film_transparent: bool | None = None
    image_format: Literal["PNG", "JPEG", "OPEN_EXR", "TIFF", "WEBP"] | None = None
    color_mode: Literal["BW", "RGB", "RGBA"] | None = None
    color_depth: Literal["8", "16", "32"] | None = None
    compression: Annotated[int | None, Field(ge=0, le=100)] = None
    quality: Annotated[int | None, Field(ge=0, le=100)] = None
    cycles_samples: Annotated[int | None, Field(ge=1, le=16_384)] = None
    cycles_use_denoising: bool | None = None
    motion_blur: "MotionBlurPatch | None" = None
    film: "FilmPatch | None" = None
    output: "OutputPatch | None" = None
    metadata: "MetadataPatch | None" = None
    multiview: "MultiviewPatch | None" = None
    cycles: "CyclesPatch | None" = None
    eevee: "EeveePatch | None" = None

    @model_validator(mode="after")
    def validate_patch(self) -> "RenderSettingsPatch":
        """Require at least one field and a valid optional frame range."""
        if not self.model_fields_set:
            raise ValueError("patch must set at least one field")
        if self.frame_start is not None and self.frame_end is not None and self.frame_end < self.frame_start:
            raise ValueError("frame_end must be greater than or equal to frame_start")
        return self


class MotionBlurPatch(BaseModel):
    """Engine-independent render motion-blur controls when exposed by Blender RNA."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    enabled: bool | None = None
    shutter: Annotated[float | None, Field(ge=0, le=10)] = None
    position: Literal["START", "CENTER", "END"] | None = None


class FilmPatch(BaseModel):
    """Film/background controls."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    transparent: bool | None = None
    transparent_glass: bool | None = None
    transparent_roughness: Annotated[float | None, Field(ge=0, le=1)] = None


class OutputPatch(BaseModel):
    """Output path and image-format controls; no render is started."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    filepath: str | None = None
    image_format: Literal["PNG", "JPEG", "OPEN_EXR", "OPEN_EXR_MULTILAYER", "TIFF", "WEBP"] | None = None
    color_mode: Literal["BW", "RGB", "RGBA"] | None = None
    color_depth: Literal["8", "16", "32"] | None = None
    compression: Annotated[int | None, Field(ge=0, le=100)] = None
    quality: Annotated[int | None, Field(ge=0, le=100)] = None
    exr_codec: Literal["NONE", "PXR24", "ZIP", "PIZ", "RLE", "ZIPS", "B44", "B44A", "DWAA", "DWAB"] | None = None
    use_file_extension: bool | None = None
    use_overwrite: bool | None = None
    use_placeholder: bool | None = None


class MetadataPatch(BaseModel):
    """Render stamp/metadata controls."""

    model_config = ConfigDict(extra="forbid")
    use_stamp: bool | None = None
    use_stamp_date: bool | None = None
    use_stamp_time: bool | None = None
    use_stamp_render_time: bool | None = None
    use_stamp_frame: bool | None = None
    use_stamp_frame_range: bool | None = None
    use_stamp_camera: bool | None = None
    use_stamp_scene: bool | None = None
    use_stamp_note: bool | None = None
    stamp_note_text: str | None = None


class MultiviewPatch(BaseModel):
    """Stereo/multiview output controls."""

    model_config = ConfigDict(extra="forbid")
    enabled: bool | None = None
    views_format: Literal["INDIVIDUAL", "STEREO_3D"] | None = None
    stereo_3d_format: Literal["ANAGLYPH", "INTERLACE", "TIMESEQUENTIAL", "SIDEBYSIDE", "TOPBOTTOM"] | None = None


class CyclesPatch(BaseModel):
    """Cycles-only sampling and denoising controls."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    samples: Annotated[int | None, Field(ge=1, le=1_000_000)] = None
    preview_samples: Annotated[int | None, Field(ge=1, le=1_000_000)] = None
    use_adaptive_sampling: bool | None = None
    adaptive_threshold: Annotated[float | None, Field(gt=0, le=1)] = None
    time_limit: Annotated[float | None, Field(ge=0, le=604_800)] = None
    use_denoising: bool | None = None
    denoiser: Literal["OPENIMAGEDENOISE", "OPTIX"] | None = None


class EeveeRayTracingPatch(BaseModel):
    """
    EEVEE screen-trace controls, Blender 5.2's `scene.eevee.ray_tracing_options` (RaytraceEEVEE).

    Separate from EeveePatch because Blender keeps them on a nested struct, not on `scene.eevee`.
    """

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    resolution_scale: Literal["1", "2", "4", "8", "16"] | None = None
    screen_trace_quality: Annotated[float | None, Field(ge=0, le=1)] = None
    screen_trace_thickness: Annotated[float | None, Field(gt=0, le=10_000)] = None
    trace_max_roughness: Annotated[float | None, Field(ge=0, le=1)] = None
    use_denoise: bool | None = None


class EeveePatch(BaseModel):
    """
    EEVEE-only sampling and ray-tracing controls, resolved against Blender 5.x RNA at runtime.

    Ray tracing is off by default in EEVEE, and clear glass renders black without it, so
    `use_raytracing` is the switch a scene with windows needs before anything else here matters.
    """

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    taa_samples: Annotated[int | None, Field(ge=1, le=1_000_000)] = None
    taa_render_samples: Annotated[int | None, Field(ge=1, le=1_000_000)] = None
    use_shadows: bool | None = None
    use_raytracing: bool | None = None
    ray_tracing_method: Literal["PROBE", "SCREEN"] | None = None
    ray_tracing: EeveeRayTracingPatch | None = None


RenderSettingsPatch.model_rebuild()


class ViewLayerPatch(BaseModel):
    """Validated view-layer visibility and render-pass patch."""

    model_config = ConfigDict(extra="forbid")

    use: bool | None = None
    use_sky: bool | None = None
    use_solid: bool | None = None
    use_strand: bool | None = None
    material_override: str | None = None
    world_override: str | None = None
    use_pass_combined: bool | None = None
    use_pass_z: bool | None = None
    use_pass_mist: bool | None = None
    use_pass_normal: bool | None = None
    use_pass_position: bool | None = None
    use_pass_vector: bool | None = None
    use_pass_uv: bool | None = None
    use_pass_object_index: bool | None = None
    use_pass_material_index: bool | None = None
    use_pass_cryptomatte_object: bool | None = None
    use_pass_cryptomatte_material: bool | None = None
    use_pass_cryptomatte_asset: bool | None = None
    pass_cryptomatte_depth: Annotated[int | None, Field(ge=2, le=16, multiple_of=2)] = None

    @model_validator(mode="after")
    def require_field(self) -> "ViewLayerPatch":
        """Reject empty patches."""
        if not self.model_fields_set:
            raise ValueError("patch must set at least one field")
        return self


async def _call(command: str, params: dict, *, changed_resources: list[str] | None = None) -> dict:
    result = await asyncio.to_thread(get_blender_connection().send_command, command, params)
    return envelope_for(result, changed_resources=changed_resources or ())


@mcp.tool()
async def inspect_render_setup(
    ctx: Context,
    scene_name: str | None = None,
    graph_sections: list[Literal["NODES", "LINKS", "DEPENDENCIES"]] | None = None,
    limit: Annotated[int, Field(ge=1, le=1000)] = 100,
    offset: Annotated[int, Field(ge=0)] = 0,
) -> dict:
    """Inspect render engine, output, color, camera, view layers, passes, and compositor state."""
    return await _call(
        "inspect_render_setup",
        {"scene_name": scene_name, "graph_sections": graph_sections, "limit": limit, "offset": offset},
    )


@mcp.tool()
async def configure_render_settings(
    ctx: Context, scene_name: str, patch: RenderSettingsPatch, detail: bool = False
) -> dict:
    """
    Patch validated scene render settings without rendering or writing a file.

    The reply names the scene, lists the property paths the patch wrote ("changed", dotted for
    nested patches such as "output.image_format") and maps each to its resulting value.

    Args:
        ctx: MCP request context.
        scene_name: Exact name of the scene to patch.
        patch: Strict typed settings patch; omitted fields remain unchanged.
        detail: Also return the whole render state before and after the patch as "before" and
            "after" - engine, camera, resolution, frame range, film, output, engine sampling,
            metadata, multiview, every view layer, compositor - instead of the patched paths.

    """
    return await _call(
        "configure_render_settings",
        {"scene_name": scene_name, "patch": patch.model_dump(exclude_none=True), "detail": detail},
        changed_resources=[scene_name],
    )


@mcp.tool()
async def manage_view_layers(
    ctx: Context,
    scene_name: str,
    action: Literal["CREATE", "PATCH", "REMOVE"],
    view_layer_name: str,
    patch: ViewLayerPatch | None = None,
    confirm_remove: bool = False,
) -> dict:
    """Create, patch, or explicitly remove one view layer and its render-pass settings."""
    if action == "PATCH" and patch is None:
        raise ToolError("PATCH requires patch")
    if action == "REMOVE" and patch is not None:
        raise ToolError("REMOVE does not accept patch")
    if action == "REMOVE" and not confirm_remove:
        raise ToolError("confirm_remove=True is required for REMOVE")
    return await _call(
        "manage_view_layers",
        {
            "scene_name": scene_name,
            "action": action,
            "view_layer_name": view_layer_name,
            "patch": patch.model_dump(exclude_none=True) if patch else None,
            "confirm_remove": confirm_remove,
        },
        changed_resources=[view_layer_name],
    )


@mcp.tool()
async def render_scene(
    ctx: Context,
    scene_name: str,
    filepath: Annotated[str, Field(min_length=1)],
    mode: Literal["STILL", "ANIMATION"] = "STILL",
    view_layer_name: str | None = None,
    frame: int | None = None,
    max_animation_frames: Annotated[int, Field(ge=1, le=10_000)] = 250,
    confirm_render: bool = False,
    confirm_overwrite: bool = False,
    render_slot_policy: Literal["USE_ACTIVE", "NEW_SLOT", "REPLACE_ACTIVE"] = "USE_ACTIVE",
    verify_outputs: bool = True,
    verify_passes: bool = True,
    max_duration_seconds: Annotated[float | None, Field(gt=0, le=604_800)] = None,
) -> dict:
    """
    Render a still or bounded animation to an explicit path after confirmation.

    This writes the actual rendered frame(s) to disk but returns only metadata (written
    path, byte size, per-frame status), not pixel content. get_viewport_screenshot
    captures the live viewport, not this render, so it is not a substitute for looking
    at the output. To actually see this render's pixels, call
    inspect_render_output(output_path=<one of this result's "files" paths>) afterward -
    or omit output_path there to read the in-memory Render Result directly.

    filepath is never a directory: Blender appends the frame number to the path as given,
    so a trailing slash writes files beside the folder instead of inside it and is refused.
    A STILL takes a full filename ending in the scene's image-format extension
    ("<dir>/sh010.png"); an ANIMATION takes a per-frame prefix ("<dir>/sh010_") or an
    explicit template ("<dir>/sh010_####.png") and is refused a plain "<dir>/sh010.png",
    which Blender would write as "sh010.png0001.png".
    """
    if not confirm_render:
        raise ToolError("confirm_render=True is required")
    return await _call(
        "render_scene",
        {
            "scene_name": scene_name,
            "filepath": filepath,
            "mode": mode,
            "view_layer_name": view_layer_name,
            "frame": frame,
            "max_animation_frames": max_animation_frames,
            "confirm_render": confirm_render,
            "confirm_overwrite": confirm_overwrite,
            "render_slot_policy": render_slot_policy,
            "verify_outputs": verify_outputs,
            "verify_passes": verify_passes,
            "max_duration_seconds": max_duration_seconds,
        },
    )


def _render_output_metadata(result: dict) -> dict:
    """
    Build the metadata dict for a rendered-frame inspection result, alongside its Image content item.

    Args:
        result: The raw dict returned by the Blender-side handler.

    Returns:
        dict: "width", "height", "native_width", "native_height", "source" ("output_path" or "render_result"),
        "source_path", and "frame".

    """
    return {
        "width": result.get("width"),
        "height": result.get("height"),
        "native_width": result.get("native_width"),
        "native_height": result.get("native_height"),
        "source": result.get("source"),
        "source_path": result.get("source_path"),
        "frame": result.get("frame"),
    }


def _read_render_output(output_path: str | None, frame: int | None, max_size: int) -> list[Image | dict]:
    """
    Copy one rendered frame out of Blender and read the copy back.

    Every step blocks - a socket round-trip, then a temporary file written by Blender, read here,
    and deleted - so the whole of it lives in one function for `inspect_render_output` to hand to a
    single `asyncio.to_thread`.

    Args:
        output_path: Exact path to an existing rendered file, or None to read the in-memory
            Render Result.
        frame: Frame number the Render Result must currently hold; only checked without
            `output_path`.
        max_size: Maximum pixel length of the returned image's largest dimension.

    Returns:
        list[Image | dict]: The rendered frame, then the envelope carrying its metadata.

    Raises:
        Exception: If Blender wrote no copy, or the command itself failed.

    """
    temp_path = None
    try:
        descriptor, temp_path = tempfile.mkstemp(prefix="blender_mcp_render_output_", suffix=".png")
        os.close(descriptor)
        result = get_blender_connection().send_command(
            "inspect_render_output",
            {
                "filepath": temp_path,
                "output_path": output_path,
                "frame": frame,
                "max_size": max_size,
                "format": "png",
            },
        )
        if not os.path.exists(temp_path):
            raise Exception("Rendered-frame copy was not created")
        return [Image(data=Path(temp_path).read_bytes(), format="png"), ok(_render_output_metadata(result))]
    except Exception as e:
        logger.error(f"Error inspecting render output: {e!s}")
        raise Exception(f"Render output inspection failed: {e!s}") from e
    finally:
        if temp_path:
            # Blender may never have written the copy, and the client is owed that failure rather
            # than a cleanup error on top of it.
            with contextlib.suppress(FileNotFoundError):
                os.remove(temp_path)


@mcp.tool(structured_output=False)
async def inspect_render_output(
    ctx: Context,
    output_path: Annotated[str | None, Field(min_length=1)] = None,
    frame: int | None = None,
    max_size: Annotated[int, Field(ge=16, le=4096)] = 1000,
) -> list[Image | dict]:
    """
    Return a previously rendered frame's actual pixels for visual inspection.

    Unlike get_viewport_screenshot, which captures the live viewport and never matches
    final render output (different engine, lighting, and color management), this reads
    real render output: an explicit output_path (typically one of render_scene's
    returned "files" paths - read-only, never modified) or, when omitted, the in-memory
    "Render Result" datablock. Render Result only ever reflects the most recently
    rendered frame, so an animation's earlier frames are only reachable by passing
    their own written output_path.

    Unlike most other tools, this returns two content items instead of one dict: the
    rendered image itself, followed by an ok() envelope carrying its metadata - read
    both.

    Args:
        ctx: MCP request context.
        output_path: Exact path to an existing rendered file on disk. Takes precedence
            over frame; the file is read but never modified.
        frame: Frame number the in-memory Render Result must currently hold; only
            checked when output_path is omitted. A mismatch raises rather than
            silently returning a different frame's pixels.
        max_size: Maximum pixel length of the returned image's largest dimension;
            defaults to 1000.

    Returns:
        [Image, dict]: the rendered frame, then an envelope whose data has "width",
        "height", "native_width", "native_height", "source", "source_path", "frame".
        With output_path, "frame" is read back out of the filename Blender wrote and is
        null when that filename carries no unambiguous frame number - so a narration of
        "here is frame 24" is only warranted when it is not null.

    Raises:
        Exception: If the operation cannot be completed.

    """
    return await asyncio.to_thread(_read_render_output, output_path, frame, max_size)
