"""Typed tools for scene-wide unit, gravity, and playback-sync configuration."""

from typing import Annotated, Literal

from mcp.server.fastmcp import Context
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..app import mcp
from ._dispatch import call_blender

UnitSystem = Literal["NONE", "METRIC", "IMPERIAL"]
SyncMode = Literal["NONE", "FRAME_DROP", "AUDIO_SYNC"]


class ScenePhysicsPatch(BaseModel):
    """Validated patch for scene-wide unit, gravity, and playback-sync settings."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    system: UnitSystem | None = None
    scale_length: Annotated[float | None, Field(ge=0.001, le=100.0)] = None
    gravity: tuple[float, float, float] | None = None
    use_gravity: bool | None = None
    sync_mode: SyncMode | None = None

    @model_validator(mode="after")
    def require_field(self) -> "ScenePhysicsPatch":
        """Reject empty patches."""
        if not self.model_fields_set:
            raise ValueError("patch must set at least one field")
        return self


@mcp.tool()
async def get_scene_physics_info(
    ctx: Context,
    scene_name: str | None = None,
    convert_seconds: Annotated[list[float], Field(max_length=32)] | None = None,
) -> dict:
    """
    Inspect scene unit system/scale, gravity, and playback sync; optionally convert seconds to frames.

    `convert_seconds` (up to 32 values) is converted using the scene's current fps/fps_base and
    frame_start (frame = frame_start + seconds * fps) - a read-only convenience for callers that need
    to key events (e.g. a liquid flow's enable/disable frame) at an explicit time rather than a frame
    number. It does not itself change fps; use configure_render_settings for that.
    """
    return await call_blender(
        "get_scene_physics_info",
        {"scene_name": scene_name, "convert_seconds": convert_seconds},
    )


@mcp.tool()
async def configure_scene_physics(ctx: Context, scene_name: str, patch: ScenePhysicsPatch) -> dict:
    """
    Patch scene-wide unit system/scale, gravity, and playback-sync mode.

    `sync_mode="NONE"` is Blender's "Play Every Frame" timeline sync option, which the Fluid manual
    requires for correctly scrubbing a REPLAY-cached liquid simulation in the interactive timeline;
    sample_liquid_simulation does not depend on this setting since it steps frames sequentially itself
    regardless of sync_mode. Changing scale_length or the unit system does not rescale existing
    geometry - it only changes how new values are interpreted - so a warning is returned when mesh
    objects with non-1.0 scale already exist in the scene.
    """
    return await call_blender(
        "configure_scene_physics",
        {"scene_name": scene_name, "patch": patch.model_dump(exclude_none=True)},
        changed_resources=[scene_name],
    )
