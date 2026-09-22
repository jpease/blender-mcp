"""Typed tools for editorial camera markers and the scene render gate."""

from typing import Annotated, Literal

from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field, model_validator

from ...app import mcp
from .._dispatch import call_blender
from .._inputs import StrictModel, dump_input, dump_inputs

MarkerAction = Literal["LIST", "CREATE", "UPDATE", "REMOVE"]


class MarkerEdit(StrictModel):
    """One exact marker edit; fields are interpreted by the requested action."""

    name: str = Field(min_length=1)
    frame: int | None = Field(default=None, ge=-1_048_574, le=1_048_574)
    camera_name: str | None = None


class RenderGatePatch(StrictModel):
    """Scene render-gate fields: output resolution, its percentage, and pixel aspect."""

    resolution_x: int | None = Field(default=None, ge=4, le=65_536)
    resolution_y: int | None = Field(default=None, ge=4, le=65_536)
    resolution_percentage: int | None = Field(default=None, ge=1, le=100)
    pixel_aspect_x: float | None = Field(default=None, gt=0, le=200)
    pixel_aspect_y: float | None = Field(default=None, gt=0, le=200)


class RenderBorderPatch(StrictModel):
    """Normalized render-region bounds and whether the region crops the output."""

    use_border: bool | None = None
    use_crop_to_border: bool | None = None
    min_x: float | None = Field(default=None, ge=0, le=1)
    max_x: float | None = Field(default=None, ge=0, le=1)
    min_y: float | None = Field(default=None, ge=0, le=1)
    max_y: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def validate_supplied_bounds(self) -> "RenderBorderPatch":
        if self.min_x is not None and self.max_x is not None and self.min_x >= self.max_x:
            raise ValueError("min_x must be less than max_x")
        if self.min_y is not None and self.max_y is not None and self.min_y >= self.max_y:
            raise ValueError("min_y must be less than max_y")
        return self


class SafeAreasPatch(StrictModel):
    """Normalized title/action safe-area margins."""

    title: tuple[float, float] | None = None
    action: tuple[float, float] | None = None
    title_center: tuple[float, float] | None = None
    action_center: tuple[float, float] | None = None

    @model_validator(mode="after")
    def validate_ranges(self) -> "SafeAreasPatch":
        for field in ("title", "action", "title_center", "action_center"):
            value = getattr(self, field)
            if value is not None and any(component < 0 or component > 1 for component in value):
                raise ValueError(f"{field} components must be between 0 and 1")
        return self


class CameraGuidesPatch(StrictModel):
    """Camera guide fields relevant to shot framing."""

    show_safe_areas: bool | None = None
    show_composition_center: bool | None = None
    show_composition_center_diagonal: bool | None = None
    show_composition_golden: bool | None = None
    show_composition_golden_tria_a: bool | None = None
    show_composition_golden_tria_b: bool | None = None
    show_composition_harmony_tri_a: bool | None = None
    show_composition_harmony_tri_b: bool | None = None
    show_composition_thirds: bool | None = None


@mcp.tool()
async def create_camera_markers(
    ctx: Context,
    scene_name: str,
    action: MarkerAction,
    markers: Annotated[list[MarkerEdit] | None, Field(max_length=200)] = None,
    replace_existing: bool = False,
) -> dict:
    """List or batch-create/update/remove exact camera-cut markers after full preflight."""
    if action != "LIST" and not markers:
        raise ToolError("markers must not be empty for a mutating action")
    if action == "LIST" and markers:
        raise ToolError("LIST does not accept marker edits")
    payload = dump_inputs(markers or [])
    return await call_blender(
        "create_camera_markers",
        {"scene_name": scene_name, "action": action, "markers": payload, "replace_existing": replace_existing},
    )


@mcp.tool()
async def configure_camera_render_gate(
    ctx: Context,
    scene_name: str,
    camera_name: str | None = None,
    render: RenderGatePatch | None = None,
    border: RenderBorderPatch | None = None,
    safe_areas: SafeAreasPatch | None = None,
    guides: CameraGuidesPatch | None = None,
) -> dict:
    """Patch the scene render gate and optional camera guides, reporting each old and new value."""
    payloads = [dump_input(render), dump_input(border), dump_input(safe_areas), dump_input(guides)]
    if not any(payloads):
        raise ToolError("Provide at least one render-gate field to change")
    if payloads[3] and camera_name is None:
        raise ToolError("camera_name is required when guides are supplied")
    return await call_blender(
        "configure_camera_render_gate",
        {
            "scene_name": scene_name,
            "camera_name": camera_name,
            "render": payloads[0],
            "border": payloads[1],
            "safe_areas": payloads[2],
            "guides": payloads[3],
        },
        changed_objects=[camera_name] if camera_name else [],
    )
