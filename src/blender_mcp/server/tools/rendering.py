"""Typed tools for scene render configuration, view layers, passes, and rendering."""

import asyncio
import contextlib
import logging
import os
import tempfile
import time

from pathlib import Path
from typing import Annotated, Literal

from mcp.server.fastmcp import Context, Image
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..app import mcp
from ..connection import get_blender_connection
from .envelope import envelope_for, ok

logger = logging.getLogger("BlenderMCPServer")

# Matches the addon's own render_scene(mode="ANIMATION") progress-array cap
# (bundled/addon/handlers/rendering.py), so an orchestrated run's detail=True reply truncates
# "progress" at the same length a single-call ANIMATION already does.
_PROGRESS_ENTRY_LIMIT = 1000


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


async def _send(command: str, params: dict) -> dict:
    return await asyncio.to_thread(get_blender_connection().send_command, command, params)


async def _call(command: str, params: dict, *, changed_resources: list[str] | None = None) -> dict:
    result = await _send(command, params)
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


def _aggregate_animation_summary(
    *,
    scene_name: str,
    output: str,
    frame_current: int,
    render_slot_policy: str,
    persisted: bool,
    verify_passes: bool,
    frame_replies: list[dict],
    frame_count_planned: int,
    cancelled: bool,
    cancellation_reason: str | None,
    duration_seconds: float,
    detail: bool,
) -> dict:
    """
    Combine N per-frame STILL replies into the exact summary shape a single ANIMATION call returns today.

    frame_replies is one detail=True render_scene(mode="STILL") reply per completed frame, in
    render order. passes/pass_verification come from the LAST completed frame, matching the
    single-call path reading Render Result once, after its whole loop. An empty frame_replies
    (cancelled before any frame rendered) reports first_file/last_file as None, bytes_written
    as 0, and passes as empty - honest about there being no render to report on, rather than the
    single-call path's own quirk of reading whatever Render Result happened to predate the call.

    Args:
        scene_name: Scene the animation rendered against.
        output: The animation's resolved absolute output template (plan_render_animation's
            "output").
        frame_current: The scene's frame_current before this animation started.
        render_slot_policy: The caller's original request - echoed back, not each per-frame
            call's own effective value.
        persisted: Whether the caller's template was actually written back onto the scene.
        verify_passes: Whether an empty passes list on the last completed frame must raise.
        frame_replies: Completed per-frame STILL replies, in render order.
        frame_count_planned: Number of frames plan_render_animation resolved.
        cancelled: Whether max_duration_seconds stopped the run early.
        cancellation_reason: Human-readable reason when cancelled, else None.
        duration_seconds: Wall-clock time the whole orchestrated run took.
        detail: Whether to add the per-frame "files"/"progress" arrays.

    Returns:
        The same summary shape render_scene(mode="ANIMATION") returns for a single call.

    Raises:
        RuntimeError: If verify_passes is set and the last completed frame captured no passes.

    """
    last = frame_replies[-1] if frame_replies else None
    passes = last["passes"] if last else []
    pass_verification = last["pass_verification"] if last else None
    if verify_passes and not passes:
        raise RuntimeError("Render completed but no enabled passes could be verified")
    files = [reply["files"][0] for reply in frame_replies]
    progress = [
        {
            "frame": entry["frame"],
            "completed": index + 1,
            "total": frame_count_planned,
            "fraction": (index + 1) / frame_count_planned,
        }
        for index, entry in enumerate(files)
        if index < _PROGRESS_ENTRY_LIMIT
    ]
    summary = {
        "scene": scene_name,
        "mode": "ANIMATION",
        "filepath": output,
        "frame": frame_current,
        "frame_count": len(frame_replies),
        "operator_result": last["operator_result"] if last else ["FINISHED"],
        "settings_restored": not persisted,
        "status": "CANCELLED" if cancelled else "COMPLETED",
        "cancelled": cancelled,
        "cancellation_reason": cancellation_reason,
        "duration_seconds": duration_seconds,
        "render_slot_policy": render_slot_policy,
        "output_persisted": persisted,
        "first_file": files[0]["path"] if files else None,
        "last_file": files[-1]["path"] if files else None,
        "bytes_written": sum(entry["bytes"] or 0 for entry in files),
        "passes": passes,
        "pass_verification": pass_verification,
    }
    if not detail:
        return summary
    return {
        **summary,
        "files": files,
        "progress": progress,
        "progress_truncated": len(files) > len(progress),
    }


async def _render_animation_orchestrated(
    ctx: Context,
    *,
    scene_name: str,
    filepath: str | None,
    view_layer_name: str | None,
    max_animation_frames: int,
    confirm_overwrite: bool,
    confirm_frame_range: bool,
    render_slot_policy: str,
    verify_outputs: bool,
    verify_passes: bool,
    max_duration_seconds: float | None,
    persist_output: bool,
    detail: bool,
) -> dict:
    """
    Drive mode="ANIMATION" as N independent STILL calls to the existing addon render_scene command.

    Reports real per-frame progress and lands real MCP cancellation between frames - instead of
    one blocking call Blender's main-thread timer cannot interrupt or report out of until every
    frame is done.

    render_slot_policy is honoured only on the first per-frame call (a NEW_SLOT would
    otherwise be created once per frame); every later frame is forced to USE_ACTIVE so all
    frames land in the slot the first call chose. persist_output is never forwarded to a
    per-frame call (render_scene refuses persist_output on a STILL); when the whole run
    completes uncancelled, the caller's template is written back with one extra
    configure_render_settings call instead, matching what the single-call path does directly.
    """
    plan = await _send(
        "plan_render_animation",
        {
            "scene_name": scene_name,
            "filepath": filepath,
            "max_animation_frames": max_animation_frames,
            "confirm_frame_range": confirm_frame_range,
        },
    )
    frames = plan["frames"]
    started = time.monotonic()
    replies: list[dict] = []
    cancelled = False
    cancellation_reason: str | None = None
    for index, entry in enumerate(frames):
        if max_duration_seconds is not None and time.monotonic() - started >= max_duration_seconds:
            cancelled = True
            cancellation_reason = "max_duration_seconds exceeded"
            break
        reply = await _send(
            "render_scene",
            {
                "scene_name": scene_name,
                "filepath": entry["path"],
                "mode": "STILL",
                "view_layer_name": view_layer_name,
                "frame": entry["frame"],
                "confirm_render": True,
                "confirm_overwrite": confirm_overwrite,
                "render_slot_policy": render_slot_policy if index == 0 else "USE_ACTIVE",
                "verify_outputs": verify_outputs,
                "verify_passes": False,
                "persist_output": False,
                "detail": True,
            },
        )
        replies.append(reply)
        await ctx.report_progress(
            index + 1, len(frames), message=f"Rendered frame {entry['frame']} ({index + 1}/{len(frames)})"
        )

    completed = len(replies) == len(frames)
    persisted = bool(persist_output) and not cancelled and completed
    if persisted:
        await _send(
            "configure_render_settings",
            {"scene_name": scene_name, "patch": {"output": {"filepath": plan["requested_filepath"]}}},
        )

    summary = _aggregate_animation_summary(
        scene_name=scene_name,
        output=plan["output"],
        frame_current=plan["frame_current"],
        render_slot_policy=render_slot_policy,
        persisted=persisted,
        verify_passes=verify_passes,
        frame_replies=replies,
        frame_count_planned=len(frames),
        cancelled=cancelled,
        cancellation_reason=cancellation_reason,
        duration_seconds=time.monotonic() - started,
        detail=detail,
    )
    return envelope_for(summary, changed_resources=[scene_name])


@mcp.tool()
async def render_scene(
    ctx: Context,
    scene_name: str,
    filepath: Annotated[str | None, Field(min_length=1)] = None,
    mode: Literal["STILL", "ANIMATION"] = "STILL",
    view_layer_name: str | None = None,
    frame: int | None = None,
    max_animation_frames: Annotated[int, Field(ge=1, le=10_000)] = 250,
    confirm_render: bool = False,
    confirm_overwrite: bool = False,
    confirm_frame_range: bool = False,
    render_slot_policy: Literal["USE_ACTIVE", "NEW_SLOT", "REPLACE_ACTIVE"] = "USE_ACTIVE",
    verify_outputs: bool = True,
    verify_passes: bool = True,
    max_duration_seconds: Annotated[float | None, Field(gt=0, le=604_800)] = None,
    persist_output: bool = False,
    detail: bool = False,
    orchestrate_animation: bool = True,
) -> dict:
    """
    Render a still or bounded animation to an explicit path after confirmation.

    This writes the actual rendered frame(s) to disk but returns only metadata: the first and
    last written path, the total bytes, and per-frame status. get_viewport_screenshot captures
    the live viewport, not this render, so it is not a substitute for looking at the output. To
    see this render's pixels, call inspect_render_output(output_path=result["last_file"])
    afterward - or omit output_path there to read the in-memory Render Result directly.
    detail=true adds the per-frame "files" and "progress" arrays.

    Omit filepath to render to the scene's own output path (configure_render_settings
    output.filepath); persist_output=true stores an ANIMATION's template on the scene so a
    re-render needs no arguments. An ANIMATION over Blender's untouched 1-250 default range is
    refused until the range is set or confirm_frame_range=true.

    filepath is never a directory: Blender appends the frame number to the path as given,
    so a trailing slash writes files beside the folder instead of inside it and is refused.
    A STILL takes a full filename ending in the scene's image-format extension
    ("<dir>/sh010.png"); an ANIMATION takes a per-frame prefix ("<dir>/sh010_") or an
    explicit template ("<dir>/sh010_####.png") and is refused a plain "<dir>/sh010.png",
    which Blender would write as "sh010.png0001.png".

    An ANIMATION (orchestrate_animation=true, the default) renders as independent per-frame
    calls instead of one call Blender cannot be interrupted out of or report progress from
    until every frame is done: this reports real progress after each frame and responds to a
    client cancellation between frames. Set orchestrate_animation=false for the single
    blocking legacy call instead - the reply shape is identical either way.
    """
    if not confirm_render:
        raise ToolError("confirm_render=True is required")
    if mode == "ANIMATION" and frame is not None:
        raise ToolError("frame is only valid for STILL renders")
    if mode == "ANIMATION" and orchestrate_animation:
        return await _render_animation_orchestrated(
            ctx,
            scene_name=scene_name,
            filepath=filepath,
            view_layer_name=view_layer_name,
            max_animation_frames=max_animation_frames,
            confirm_overwrite=confirm_overwrite,
            confirm_frame_range=confirm_frame_range,
            render_slot_policy=render_slot_policy,
            verify_outputs=verify_outputs,
            verify_passes=verify_passes,
            max_duration_seconds=max_duration_seconds,
            persist_output=persist_output,
            detail=detail,
        )
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
            "confirm_frame_range": confirm_frame_range,
            "render_slot_policy": render_slot_policy,
            "verify_outputs": verify_outputs,
            "verify_passes": verify_passes,
            "max_duration_seconds": max_duration_seconds,
            "persist_output": persist_output,
            "detail": detail,
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
    real render output: an explicit output_path (typically render_scene's returned "last_file",
    or one of its detail=true "files" paths - read-only, never modified) or, when omitted, the
    in-memory "Render Result" datablock. Render Result only ever reflects the most recently
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
