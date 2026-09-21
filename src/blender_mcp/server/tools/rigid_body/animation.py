"""Authored-animation and rigid-body handoff tools."""

from typing import Annotated, Literal

from mcp.server.fastmcp import Context
from pydantic import Field

from .._dispatch import call_blender
from .inspection_and_setup import Vector3, mcp


@mcp.tool()
async def animate_rigid_body_release(
    ctx: Context,
    scene_name: str,
    object_name: str,
    transition: Literal["RELEASE", "CAPTURE"],
    frame: Annotated[int, Field(ge=-1_000_000, le=1_000_000)],
    pre_roll_frames: Annotated[int, Field(ge=1, le=120)] = 1,
    linear_velocity: Vector3 | None = None,
    angular_velocity: Vector3 | None = None,
    action_name: str | None = None,
    overwrite_existing_action: bool = False,
    confirm_delete_baked_cache: bool = False,
) -> dict:
    """
    Key a deterministic handoff between authored keyframe animation and rigid-body simulation.

    RELEASE inserts a keyframe on object_name's authored transform at `frame`, then lets rigid-body
    simulation drive it from that pose (and linear_velocity/angular_velocity, if given) onward.
    CAPTURE does the reverse: it bakes object_name's simulated transform at `frame` into an authored
    keyframe so animation takes back over from that point. object_name must already have rigid body
    physics enabled in scene_name's rigid body world. Rejects with confirm_delete_baked_cache=False if
    that world already has a baked simulation cache, since inserting a release/capture keyframe
    invalidates it.
    """
    return await call_blender(
        "animate_rigid_body_release",
        {
            "scene_name": scene_name,
            "object_name": object_name,
            "transition": transition,
            "frame": frame,
            "pre_roll_frames": pre_roll_frames,
            "linear_velocity": linear_velocity,
            "angular_velocity": angular_velocity,
            "action_name": action_name,
            "overwrite_existing_action": overwrite_existing_action,
            "confirm_delete_baked_cache": confirm_delete_baked_cache,
        },
        changed_objects=[object_name],
    )
