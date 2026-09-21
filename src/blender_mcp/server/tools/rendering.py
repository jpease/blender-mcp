"""Typed tools for scene render configuration, view layers, passes, and rendering."""

import asyncio
import time

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated, Literal

from mcp.server.fastmcp import Context, Image
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..app import mcp
from ._dispatch import call_blender, send_blender_command, send_command
from .envelope import envelope_for
from .image_capture import capture_png

# How many per-frame progress records a detail=True ANIMATION reply carries. Its twin is
# `_PROGRESS_ENTRY_LIMIT` in `src/blender_mcp/bundled/addon/handlers/rendering.py`, which caps
# the single-call path at the same length: one animation must truncate identically however it
# was driven. Change one and change the other.
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


@mcp.tool()
async def inspect_render_setup(
    ctx: Context,
    scene_name: str | None = None,
    graph_sections: list[Literal["NODES", "LINKS", "DEPENDENCIES"]] | None = None,
    limit: Annotated[int, Field(ge=1, le=1000)] = 100,
    offset: Annotated[int, Field(ge=0)] = 0,
) -> dict:
    """Inspect render engine, output, color, camera, view layers, passes, and compositor state."""
    return await call_blender(
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
    return await call_blender(
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
    return await call_blender(
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


@dataclass(frozen=True, slots=True)
class _RenderRequest:
    """
    One render_scene call's arguments, so the same request is spelled exactly once.

    The tool signature, the single-call payload and each orchestrated frame's payload all
    describe the same render. Holding them as one value object is what stops a parameter added
    to the signature from reaching only two of the three.
    """

    scene_name: str
    filepath: str | None
    mode: str
    view_layer_name: str | None
    frame: int | None
    max_animation_frames: int
    confirm_render: bool
    confirm_overwrite: bool
    confirm_frame_range: bool
    render_slot_policy: str
    verify_outputs: bool
    verify_passes: bool
    max_duration_seconds: float | None
    persist_output: bool
    detail: bool

    def payload(self) -> dict:
        """
        Build the params the addon's own render_scene command takes.

        Returns:
            dict: Every field of this request, under the addon's parameter names.

        """
        return {
            "scene_name": self.scene_name,
            "filepath": self.filepath,
            "mode": self.mode,
            "view_layer_name": self.view_layer_name,
            "frame": self.frame,
            "max_animation_frames": self.max_animation_frames,
            "confirm_render": self.confirm_render,
            "confirm_overwrite": self.confirm_overwrite,
            "confirm_frame_range": self.confirm_frame_range,
            "render_slot_policy": self.render_slot_policy,
            "verify_outputs": self.verify_outputs,
            "verify_passes": self.verify_passes,
            "max_duration_seconds": self.max_duration_seconds,
            "persist_output": self.persist_output,
            "detail": self.detail,
        }

    def frame_payload(self, entry: dict, *, first: bool) -> dict:
        """
        Build the params for one frame of an orchestrated ANIMATION.

        Args:
            entry: One plan_render_animation frame record: its "frame" number and "path".
            first: Whether this is the run's first frame, which alone chooses the render slot.

        Returns:
            dict: A STILL render of that one frame, to that one path.

        """
        # A NEW_SLOT is created once per call, so honouring the caller's policy on every frame
        # would scatter one animation across N slots. The first frame chooses; the rest follow.
        render_slot_policy = self.render_slot_policy if first else "USE_ACTIVE"
        return {
            "scene_name": self.scene_name,
            "filepath": entry["path"],
            "mode": "STILL",
            "view_layer_name": self.view_layer_name,
            "frame": entry["frame"],
            "confirm_overwrite": self.confirm_overwrite,
            "render_slot_policy": render_slot_policy,
            "verify_outputs": self.verify_outputs,
            # The four the orchestrator owns outright rather than forwards: the run as a whole
            # is already confirmed; passes are verified once over the last frame and the output
            # template written back once after it, both by the orchestrator; and every frame
            # must come back detailed whatever the caller asked for, because its "files" record
            # is what the summary counts, sums and reports first_file/last_file from.
            "confirm_render": True,
            "verify_passes": False,
            "persist_output": False,
            "detail": True,
        }


@dataclass(frozen=True, slots=True)
class _AnimationOutcome:
    """
    How one orchestrated run ended.

    Carries the request it ran so the summary is built from two values - the plan and the
    outcome - rather than from a dozen loose keywords the caller has to keep in step.
    """

    request: _RenderRequest
    cancelled: bool
    cancellation_reason: str | None
    duration_seconds: float
    persisted: bool


def _animation_summary(
    *,
    scene_name: str,
    mode: str,
    output: str,
    frame: int,
    files: list[dict],
    frame_total: int,
    operator_result: list[str],
    persisted: bool,
    cancelled: bool,
    cancellation_reason: str | None,
    duration_seconds: float,
    render_slot_policy: str,
    passes: list,
    pass_verification: str | None,
    detail: bool,
) -> dict:
    """
    Build a render reply out of the frames that were actually written.

    This is the one place the shape is spelled on this side of the socket. Its twin is
    `_animation_summary` in `src/blender_mcp/bundled/addon/handlers/rendering.py`, which builds
    the same keys from its own render loop; the add-on cannot import this package, so the shape
    is stated twice and the two must be changed together.

    Args:
        scene_name: Name of the scene that rendered.
        mode: "STILL" or "ANIMATION".
        output: The resolved absolute output path or per-frame template.
        frame: The frame the reply reports as current.
        files: One record per written frame, in render order: "frame", "path", "bytes".
        frame_total: How many frames the run planned, which is what "fraction" divides by.
        operator_result: The last rendered frame's sorted operator result, or ["FINISHED"]
            when no frame rendered.
        persisted: Whether the caller's output template was written back onto the scene.
        cancelled: Whether the run stopped before every planned frame.
        cancellation_reason: Why it stopped, or None.
        duration_seconds: Wall-clock time the run took.
        render_slot_policy: The caller's requested slot policy, echoed back.
        passes: Render passes read after the run.
        pass_verification: How `passes` was established.
        detail: Whether to add the per-frame "files"/"progress" arrays.

    Returns:
        dict: The render reply, with "files"/"progress"/"progress_truncated" only when detail.

    """
    progress = [
        {
            "frame": entry["frame"],
            "completed": index + 1,
            "total": frame_total,
            "fraction": (index + 1) / frame_total,
        }
        for index, entry in enumerate(files[:_PROGRESS_ENTRY_LIMIT])
    ]
    summary = {
        "scene": scene_name,
        "mode": mode,
        "filepath": output,
        "frame": frame,
        "frame_count": len(files),
        "operator_result": operator_result,
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
    return {**summary, "files": files, "progress": progress, "progress_truncated": len(files) > len(progress)}


def _aggregate_animation_summary(plan: dict, replies: list[dict], outcome: _AnimationOutcome) -> dict:
    """
    Combine N per-frame STILL replies into the exact summary shape a single ANIMATION call returns.

    passes/pass_verification come from the LAST completed frame, matching the single-call path
    reading Render Result once, after its whole loop. An empty `replies` (cancelled before any
    frame rendered) reports first_file/last_file as None, bytes_written as 0, and passes as
    empty - honest about there being no render to report on, rather than the single-call path's
    own quirk of reading whatever Render Result happened to predate the call.

    Args:
        plan: The plan_render_animation reply this run was driven from.
        replies: Completed per-frame detail=True STILL replies, in render order.
        outcome: How the run ended, and the request it ran.

    Returns:
        dict: The same summary shape render_scene(mode="ANIMATION") returns for a single call.

    Raises:
        RuntimeError: If verify_passes was set and the last completed frame captured no passes.

    """
    request = outcome.request
    last = replies[-1] if replies else None
    passes = last["passes"] if last else []
    pass_verification = last["pass_verification"] if last else None
    if request.verify_passes and not passes:
        raise RuntimeError("Render completed but no enabled passes could be verified")
    return _animation_summary(
        scene_name=request.scene_name,
        mode="ANIMATION",
        output=plan["output"],
        frame=plan["frame_current"],
        files=[reply["files"][0] for reply in replies],
        frame_total=len(plan["frames"]),
        # The last completed frame's operator result, ["FINISHED"] when none completed: the
        # same value the add-on's twin reports, whose `result` is its last render's and stays
        # the initial {"FINISHED"} when its loop never ran.
        operator_result=last["operator_result"] if last else ["FINISHED"],
        persisted=outcome.persisted,
        cancelled=outcome.cancelled,
        cancellation_reason=outcome.cancellation_reason,
        duration_seconds=outcome.duration_seconds,
        render_slot_policy=request.render_slot_policy,
        passes=passes,
        pass_verification=pass_verification,
        detail=request.detail,
    )


async def _iter_rendered_frames(request: _RenderRequest, frames: list[dict], started: float) -> AsyncIterator[dict]:
    """
    Render each planned frame as its own STILL call, ending early when the deadline trips.

    Owning the deadline here is what lets the caller read cancellation off the arithmetic -
    fewer replies than frames - instead of carrying a flag and a reason through the loop.

    Args:
        request: The animation request every frame is narrowed from.
        frames: plan_render_animation's frame records, in render order.
        started: The monotonic clock reading the whole run is timed from.

    Yields:
        dict: One detail=True STILL reply per rendered frame.

    """
    for index, entry in enumerate(frames):
        if request.max_duration_seconds is not None and time.monotonic() - started >= request.max_duration_seconds:
            return
        yield await send_blender_command("render_scene", request.frame_payload(entry, first=index == 0))


async def _render_animation_orchestrated(ctx: Context, request: _RenderRequest) -> dict:
    """
    Drive mode="ANIMATION" as N independent STILL calls to the existing addon render_scene command.

    Reports real per-frame progress and lands real MCP cancellation between frames - instead of
    one blocking call Blender's main-thread timer cannot interrupt or report out of until every
    frame is done. Which parts of the request each frame keeps is `_RenderRequest.frame_payload`;
    persist_output is one of the parts it drops, because render_scene refuses it on a STILL, so
    a run that completed uncancelled writes the caller's template back with one extra
    configure_render_settings call instead - what the single-call path does directly.

    Args:
        ctx: MCP request context, reported to after every frame.
        request: The caller's render request.

    Returns:
        dict: An envelope over the same summary a single ANIMATION call returns.

    """
    plan = await send_blender_command(
        "plan_render_animation",
        {
            "scene_name": request.scene_name,
            "filepath": request.filepath,
            "max_animation_frames": request.max_animation_frames,
            "confirm_frame_range": request.confirm_frame_range,
        },
    )
    frames = plan["frames"]
    started = time.monotonic()
    replies: list[dict] = []
    async for reply in _iter_rendered_frames(request, frames, started):
        replies.append(reply)
        await ctx.report_progress(
            len(replies), len(frames), message=f"Rendered frame {reply['frame']} ({len(replies)}/{len(frames)})"
        )

    cancelled = len(replies) < len(frames)
    persisted = bool(request.persist_output) and not cancelled
    if persisted:
        await send_blender_command(
            "configure_render_settings",
            {"scene_name": request.scene_name, "patch": {"output": {"filepath": plan["requested_filepath"]}}},
        )
    outcome = _AnimationOutcome(
        request=request,
        cancelled=cancelled,
        cancellation_reason="max_duration_seconds exceeded" if cancelled else None,
        duration_seconds=time.monotonic() - started,
        persisted=persisted,
    )
    return envelope_for(_aggregate_animation_summary(plan, replies, outcome), changed_resources=[request.scene_name])


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
    request = _RenderRequest(
        scene_name=scene_name,
        filepath=filepath,
        mode=mode,
        view_layer_name=view_layer_name,
        frame=frame,
        max_animation_frames=max_animation_frames,
        confirm_render=confirm_render,
        confirm_overwrite=confirm_overwrite,
        confirm_frame_range=confirm_frame_range,
        render_slot_policy=render_slot_policy,
        verify_outputs=verify_outputs,
        verify_passes=verify_passes,
        max_duration_seconds=max_duration_seconds,
        persist_output=persist_output,
        detail=detail,
    )
    if mode == "ANIMATION" and orchestrate_animation:
        return await _render_animation_orchestrated(ctx, request)
    return await call_blender("render_scene", request.payload())


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
    return await asyncio.to_thread(
        capture_png,
        send_command,
        "inspect_render_output",
        {"output_path": output_path, "frame": frame, "max_size": max_size},
        prefix="blender_mcp_render_output_",
        metadata=_render_output_metadata,
        failure="Render output inspection failed",
        missing_file="Rendered-frame copy was not created",
    )
