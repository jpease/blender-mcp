"""Read-only tools for inspecting and validating camera rigs."""

from typing import Annotated

from mcp.server.fastmcp import Context
from pydantic import Field

from ...app import mcp
from .._dispatch import call_blender


@mcp.tool()
async def get_camera_rig_info(
    ctx: Context,
    scene_name: str,
    object_name: str,
    descendant_depth: Annotated[int, Field(ge=0, le=12)] = 4,
    child_limit: Annotated[int, Field(ge=1, le=200)] = 50,
    child_offset: Annotated[int, Field(ge=0, le=1999)] = 0,
    animation_limit: Annotated[int, Field(ge=1, le=500)] = 100,
    animation_offset: Annotated[int, Field(ge=0, le=4999)] = 0,
) -> dict:
    """
    Inspect one camera or rig root before editing it.

    The result labels local and world transforms separately and includes camera optics, DOF,
    constraints, drivers, actions, render gate, active-camera state, camera markers, rig metadata,
    and a bounded descendant page. Continue pages with the returned next offsets. This tool never
    evaluates another frame and never changes the scene.
    """
    return await call_blender(
        "get_camera_rig_info",
        {
            "scene_name": scene_name,
            "object_name": object_name,
            "descendant_depth": descendant_depth,
            "child_limit": child_limit,
            "child_offset": child_offset,
            "animation_limit": animation_limit,
            "animation_offset": animation_offset,
        },
    )


@mcp.tool()
async def validate_camera_rig(
    ctx: Context,
    scene_name: str,
    object_names: Annotated[list[str] | None, Field(max_length=500)] = None,
    sample_frames: Annotated[list[int] | None, Field(max_length=24)] = None,
) -> dict:
    """Read-only structural validation of explicit or scene camera rigs at bounded sample frames."""
    return await call_blender(
        "validate_camera_rig", {"scene_name": scene_name, "object_names": object_names, "sample_frames": sample_frames}
    )
