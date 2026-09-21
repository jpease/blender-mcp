"""Non-destructive rigid-body transform animation delivery tools."""

from typing import Annotated, Literal

from mcp.server.fastmcp import Context
from pydantic import Field

from .._dispatch import call_blender
from .inspection_and_setup import mcp


@mcp.tool()
async def bake_rigid_bodies_to_keyframes(
    ctx: Context,
    scene_name: str,
    object_names: Annotated[list[str], Field(min_length=1, max_length=100)],
    frame_start: Annotated[int, Field(ge=0, le=300_000)],
    frame_end: Annotated[int, Field(ge=1, le=300_000)],
    frame_step: Annotated[int, Field(ge=1, le=120)] = 1,
    output_mode: Literal["DUPLICATES", "SOURCE"] = "DUPLICATES",
    output_collection_name: str = "Rigid Body Bakes",
    action_name_prefix: str = "Rigid Body Bake",
    key_scale: bool = False,
    confirm_overwrite_animation: bool = False,
) -> dict:
    """
    Record evaluated rigid-body world transforms into new actions, preserving simulation sources by default.

    output_mode="DUPLICATES" (default) creates new copy objects in output_collection_name with the
    baked action, leaving object_names and their simulation untouched - non-destructive.
    output_mode="SOURCE" instead bakes directly onto object_names' own action, replacing their
    live rigid-body simulation with keyframes; this requires confirm_overwrite_animation=True since
    it discards the ability to re-simulate those objects from their prior state. Use
    sample_rigid_body_simulation first if you only need to inspect transforms without baking.
    """
    if frame_start > frame_end:
        raise ValueError("frame_start must not exceed frame_end")
    return await call_blender(
        "bake_rigid_bodies_to_keyframes",
        {
            "scene_name": scene_name,
            "object_names": object_names,
            "frame_start": frame_start,
            "frame_end": frame_end,
            "frame_step": frame_step,
            "output_mode": output_mode,
            "output_collection_name": output_collection_name,
            "action_name_prefix": action_name_prefix,
            "key_scale": key_scale,
            "confirm_overwrite_animation": confirm_overwrite_animation,
        },
        changed_objects=object_names,
    )
