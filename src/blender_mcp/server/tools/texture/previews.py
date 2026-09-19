"""Deterministic isolated material preview rendering."""

import asyncio
import os
import tempfile

from typing import Literal

from mcp.server.fastmcp import Context, Image
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from ...app import mcp
from ._shared import TargetEngine, absolute_path, call_blender


@mcp.tool(structured_output=False)
async def render_pbr_material_preview(
    ctx: Context,
    material_name: str,
    target_engine: TargetEngine = "BLENDER_EEVEE_NEXT",
    geometry: Literal["SPHERE", "PLANE", "ROUNDED_CUBE"] = "SPHERE",
    resolution: int = Field(default=512, ge=64, le=2048),
    samples: int = Field(default=64, ge=1, le=4096),
    transparent_background: bool = False,
    cycles_output_path: str | None = None,
    eevee_output_path: str | None = None,
    confirm_cycles: bool = False,
) -> list[Image | dict]:
    """
    Render a controlled studio preview in Eevee, Cycles, or both using identical staging.

    Cycles requires `confirm_cycles=True`. Output paths are optional but must be absolute and distinct.
    The result records engine, device, samples, color management, and approximation warnings. The
    temporary scene and datablocks are removed in `finally` and the user's active scene is restored.
    """
    if target_engine in {"CYCLES", "BOTH"} and not confirm_cycles:
        raise ToolError("Set confirm_cycles=True for a Cycles preview")
    paths = [path for path in (cycles_output_path, eevee_output_path) if path]
    if len(paths) != len(set(paths)):
        raise ToolError("Cycles and Eevee output paths must be distinct")
    requested = {
        "CYCLES": cycles_output_path,
        "EEVEE": eevee_output_path,
        "BLENDER_EEVEE_NEXT": eevee_output_path,
    }
    engines = [target_engine] if target_engine != "BOTH" else ["CYCLES", "BLENDER_EEVEE_NEXT"]
    output_paths, temporary = {}, set()
    for engine in engines:
        path = requested[engine]
        if path:
            output_paths[engine] = absolute_path(path, f"{engine} output path")
        else:
            descriptor, path = tempfile.mkstemp(prefix=f"blender_pbr_{engine.lower()}_", suffix=".png")
            os.close(descriptor)
            os.unlink(path)
            output_paths[engine] = path
            temporary.add(path)
    try:
        result = await asyncio.to_thread(
            call_blender,
            "render_pbr_material_preview",
            {
                "material_name": material_name,
                "target_engine": target_engine,
                "geometry": geometry,
                "resolution": resolution,
                "samples": samples,
                "transparent_background": transparent_background,
                "output_paths": output_paths,
            },
        )
        content: list[Image | dict] = []
        for engine in engines:
            path = output_paths[engine]
            if path in temporary:
                if not os.path.isfile(path):
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
