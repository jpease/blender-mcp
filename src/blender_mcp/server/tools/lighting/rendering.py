"""Engine quality, color-management, and bounded lighting-preview MCP tools."""

import os
import tempfile

from pathlib import Path
from typing import Annotated, Literal

from mcp.server.fastmcp import Context, Image
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from ...app import mcp
from .._dispatch import call_blender
from .._inputs import StrictModel, dump_input


class CyclesLightingQuality(StrictModel):
    """Allowlisted Cycles sampling and light-path quality controls."""

    samples: int | None = Field(default=None, ge=1, le=16384)
    use_adaptive_sampling: bool | None = None
    adaptive_threshold: float | None = Field(default=None, ge=0, le=1)
    use_denoising: bool | None = None
    light_sampling_threshold: float | None = Field(default=None, ge=0, le=1)
    sample_clamp_direct: float | None = Field(default=None, ge=0)
    sample_clamp_indirect: float | None = Field(default=None, ge=0)
    max_bounces: int | None = Field(default=None, ge=0, le=1024)
    diffuse_bounces: int | None = Field(default=None, ge=0, le=1024)
    glossy_bounces: int | None = Field(default=None, ge=0, le=1024)
    transmission_bounces: int | None = Field(default=None, ge=0, le=1024)
    transparent_max_bounces: int | None = Field(default=None, ge=0, le=1024)
    volume_bounces: int | None = Field(default=None, ge=0, le=1024)
    device: Literal["CPU", "GPU"] | None = None


class EeveeLightingQuality(StrictModel):
    """Allowlisted EEVEE lighting, shadow, ray-tracing, GI, and volume controls."""

    render_samples: int | None = Field(default=None, ge=1, le=4096)
    light_threshold: float | None = Field(default=None, ge=0)
    shadow_pool_size: Literal["16", "32", "64", "128", "256", "512", "1024", "1536", "2048"] | None = None
    shadow_resolution_scale: float | None = Field(default=None, gt=0, le=1)
    shadow_ray_count: int | None = Field(default=None, ge=1, le=4)
    shadow_step_count: int | None = Field(default=None, ge=1, le=16)
    use_raytracing: bool | None = None
    ray_tracing_method: Literal["PROBE", "SCREEN"] | None = None
    use_fast_gi: bool | None = None
    volumetric_tile_size: Literal["1", "2", "4", "8", "16"] | None = None
    volumetric_samples: int | None = Field(default=None, ge=1, le=256)
    volumetric_ray_depth: int | None = Field(default=None, ge=1, le=16)


@mcp.tool()
async def configure_lighting_quality(
    ctx: Context,
    scene_name: str,
    target_engine: Literal["CYCLES", "EEVEE", "BOTH"],
    preset: Literal["PREVIEW", "BALANCED", "FINAL"] | None = None,
    cycles: CyclesLightingQuality | None = None,
    eevee: EeveeLightingQuality | None = None,
    detail: bool = False,
) -> dict:
    """
    Patch only render settings that affect lighting quality and cost.

    Choose an explicit preset or supply engine-specific patches. ``BOTH`` requires settings for
    both engines unless a preset is supplied. The tool never changes output size, path, color
    management, camera, or light energy; runtime RNA checks prevent unsupported settings from
    being silently ignored. The reply lists the engine-qualified property paths it wrote
    ("changed", e.g. "eevee.render_samples") and maps each to its resulting value, so a preset's
    expansion and any clamping are visible without a second call.

    Args:
        ctx: MCP request context.
        scene_name: Exact name of the scene whose engine quality is patched.
        target_engine: Which engine's settings the call may touch.
        preset: Named quality expansion applied first; explicit engine fields override it.
        cycles: Allowlisted Cycles sampling and light-path fields.
        eevee: Allowlisted EEVEE sampling, shadow, ray-tracing, GI, and volume fields.
        detail: Also return the whole allowlisted Cycles and EEVEE quality state before and after
            the patch as "before" and "after" instead of the patched paths.

    """
    cycles_payload = dump_input(cycles)
    eevee_payload = dump_input(eevee)
    if preset is None and not cycles_payload and not eevee_payload:
        raise ToolError("Provide a preset or at least one engine quality setting")
    if target_engine == "CYCLES" and eevee_payload:
        raise ToolError("EEVEE settings do not apply to target_engine='CYCLES'")
    if target_engine == "EEVEE" and cycles_payload:
        raise ToolError("Cycles settings do not apply to target_engine='EEVEE'")
    if target_engine == "BOTH" and preset is None and (not cycles_payload or not eevee_payload):
        raise ToolError("target_engine='BOTH' requires both cycles and eevee patches, or a preset")
    return await call_blender(
        "configure_lighting_quality",
        {
            "scene_name": scene_name,
            "target_engine": target_engine,
            "preset": preset,
            "cycles": cycles_payload,
            "eevee": eevee_payload,
            "detail": detail,
        },
    )


@mcp.tool()
async def configure_color_management(
    ctx: Context,
    scene_name: str,
    view_transform: str | None = None,
    look: str | None = None,
    exposure: Annotated[float | None, Field(ge=-32, le=32)] = None,
    gamma: Annotated[float | None, Field(gt=0, le=5)] = None,
) -> dict:
    """
    Set a reproducible display transform for lighting evaluation.

    View and look names are validated against the active OCIO configuration. Exposure is measured
    in stops; the result includes its ``2 ** exposure`` multiplier. Light energy is never adjusted
    to compensate, and omitted fields remain unchanged.
    """
    if view_transform is None and look is None and exposure is None and gamma is None:
        raise ToolError("Provide at least one color-management setting")
    return await call_blender(
        "configure_color_management",
        {
            "scene_name": scene_name,
            "view_transform": view_transform,
            "look": look,
            "exposure": exposure,
            "gamma": gamma,
        },
    )


def _preview_paths(engine: str, output_path: str | None, cycles_output_path: str | None, eevee_output_path: str | None):
    """Resolve explicit output paths or create unique temporary PNG paths."""
    if engine != "BOTH" and (cycles_output_path or eevee_output_path):
        raise ToolError("Use output_path for a single-engine preview")
    if engine == "BOTH" and output_path:
        raise ToolError("Use cycles_output_path and eevee_output_path for a BOTH preview")
    requested = {
        "CYCLES": cycles_output_path if engine == "BOTH" else output_path,
        "EEVEE": eevee_output_path if engine == "BOTH" else output_path,
    }
    resolved = {}
    temporary = set()
    for item in ["CYCLES", "EEVEE"] if engine == "BOTH" else [engine]:
        path = requested[item]
        if path is not None:
            parsed = Path(path)
            if not parsed.is_absolute() or parsed.suffix.lower() != ".png":
                raise ToolError(f"{item} output path must be an absolute .png path")
            resolved[item] = str(parsed)
        else:
            descriptor, path = tempfile.mkstemp(prefix=f"blender_lighting_{item.lower()}_", suffix=".png")
            os.close(descriptor)
            os.unlink(path)
            resolved[item] = path
            temporary.add(path)
    if len(set(resolved.values())) != len(resolved):
        raise ToolError("Each preview engine requires a distinct output path")
    return resolved, temporary


@mcp.tool(structured_output=False)
async def render_lighting_preview(
    ctx: Context,
    scene_name: str,
    camera_name: str,
    frame: int,
    target_engine: Literal["CYCLES", "EEVEE", "BOTH"],
    width: Annotated[int, Field(ge=16, le=1024)] = 512,
    height: Annotated[int, Field(ge=16, le=1024)] = 512,
    samples: Annotated[int, Field(ge=1, le=1024)] = 32,
    output_path: str | None = None,
    cycles_output_path: str | None = None,
    eevee_output_path: str | None = None,
    confirm_overwrite: bool = False,
    confirm_long_render: bool = False,
) -> list[Image | dict]:
    """
    Render a bounded still or matched Cycles/EEVEE lighting comparison.

    The same camera, frame, dimensions, world, lights, exposure, and view transform are held for a
    ``BOTH`` comparison; only the engine and requested sample count differ. Cycles above 64 samples
    requires ``confirm_long_render``. Existing files are overwritten only with
    ``confirm_overwrite``. Without output paths, PNGs are returned inline and temporary files are
    removed. The final content item is the normal result envelope; inspect it along with the images.
    Its ``matched_state`` names every light the frame was rendered under rather than echoing their
    state; call ``inspect_lighting_setup`` for that.
    """
    if target_engine in {"CYCLES", "BOTH"} and samples > 64 and not confirm_long_render:
        raise ToolError("Cycles previews above 64 samples require confirm_long_render=true")
    paths, temporary = _preview_paths(target_engine, output_path, cycles_output_path, eevee_output_path)
    try:
        result = await call_blender(
            "render_lighting_preview",
            {
                "scene_name": scene_name,
                "camera_name": camera_name,
                "frame": frame,
                "target_engine": target_engine,
                "width": width,
                "height": height,
                "samples": samples,
                "output_paths": paths,
                "confirm_overwrite": confirm_overwrite,
            },
        )
        content: list[Image | dict] = []
        for engine in ["CYCLES", "EEVEE"] if target_engine == "BOTH" else [target_engine]:
            path = paths[engine]
            if path in temporary:
                if not os.path.exists(path):
                    raise ToolError(f"{engine} preview file was not created")
                with open(path, "rb") as handle:
                    content.append(Image(data=handle.read(), format="png"))
        content.append(result)
        return content
    finally:
        for path in temporary:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
