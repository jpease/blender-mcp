"""Typed tools for weight transfer and B-Bone deformation controls."""

from typing import Annotated, Literal

from mcp.server.fastmcp import Context
from pydantic import Field, model_validator

from ...app import mcp
from .._dispatch import call_blender
from ._shared import _StrictModel

VertexMapping = Literal[
    "TOPOLOGY",
    "NEAREST",
    "EDGE_NEAREST",
    "EDGEINTERP_NEAREST",
    "POLY_NEAREST",
    "POLYINTERP_NEAREST",
    "POLYINTERP_VNORPROJ",
]


class BendyBonePatch(_StrictModel):
    """Allowlisted rest and pose settings for one Blender B-Bone."""

    bone_name: str = Field(min_length=1, max_length=63)
    segments: int | None = Field(default=None, ge=1, le=32)
    display_x: float | None = Field(default=None, gt=0)
    display_z: float | None = Field(default=None, gt=0)
    mapping_mode: Literal["STRAIGHT", "CURVED"] | None = None
    handle_type_start: Literal["AUTO", "ABSOLUTE", "RELATIVE", "TANGENT"] | None = None
    handle_type_end: Literal["AUTO", "ABSOLUTE", "RELATIVE", "TANGENT"] | None = None
    custom_handle_start: str | None = Field(default=None, min_length=1, max_length=63)
    custom_handle_end: str | None = Field(default=None, min_length=1, max_length=63)
    ease_in: float | None = None
    ease_out: float | None = None
    curve_in_x: float | None = None
    curve_in_z: float | None = None
    curve_out_x: float | None = None
    curve_out_z: float | None = None
    roll_in: float | None = None
    roll_out: float | None = None
    scale_in: tuple[float, float, float] | None = None
    scale_out: tuple[float, float, float] | None = None
    use_scale_easing: bool | None = None
    use_endroll_as_inroll: bool | None = None
    handle_use_ease_start: bool | None = None
    handle_use_ease_end: bool | None = None
    handle_use_scale_start: tuple[bool, bool, bool] | None = None
    handle_use_scale_end: tuple[bool, bool, bool] | None = None

    @model_validator(mode="after")
    def validate_handles(self) -> "BendyBonePatch":
        if self.custom_handle_start and self.handle_type_start in {None, "AUTO"}:
            raise ValueError("custom_handle_start requires a non-AUTO handle_type_start")
        if self.custom_handle_end and self.handle_type_end in {None, "AUTO"}:
            raise ValueError("custom_handle_end requires a non-AUTO handle_type_end")
        return self


@mcp.tool()
async def transfer_skin_weights(
    ctx: Context,
    source_mesh_name: str,
    target_mesh_name: str,
    modifier_name: Annotated[str, Field(min_length=1, max_length=63)] = "Rig Weight Transfer",
    mapping: VertexMapping = "POLYINTERP_NEAREST",
    source_groups: Literal["ALL", "DEFORM"] = "DEFORM",
    mix_mode: Literal["REPLACE", "ABOVE_THRESHOLD", "BELOW_THRESHOLD", "MIX", "ADD", "SUB", "MUL"] = "REPLACE",
    mix_factor: Annotated[float, Field(ge=0, le=1)] = 1.0,
    use_object_transform: bool = True,
    max_distance: Annotated[float | None, Field(gt=0)] = None,
    destination_policy: Literal["ERROR", "UPDATE"] = "ERROR",
    commit: bool = False,
    confirm_commit: bool = False,
    normalize: bool = False,
) -> dict:
    """
    Transfer vertex-group weights with a live Data Transfer modifier by default.

    source_mesh_name and target_mesh_name must be existing, distinct mesh objects. modifier_name
    identifies the Data Transfer modifier on target_mesh_name: destination_policy="ERROR" (default)
    rejects the call if a modifier with that name already exists, while "UPDATE" reconfigures it in
    place. ``commit`` applies the modifier and is irreversible at the Blender data level, so it
    requires ``confirm_commit``. Mapping identifiers are Blender 5.1 RNA enum values.
    """
    if source_mesh_name == target_mesh_name:
        raise ValueError("source_mesh_name and target_mesh_name must differ")
    if commit and not confirm_commit:
        raise ValueError("confirm_commit=True is required to apply transferred weights")
    return await call_blender(
        "transfer_skin_weights",
        {
            "source_mesh_name": source_mesh_name,
            "target_mesh_name": target_mesh_name,
            "modifier_name": modifier_name,
            "mapping": mapping,
            "source_groups": source_groups,
            "mix_mode": mix_mode,
            "mix_factor": mix_factor,
            "use_object_transform": use_object_transform,
            "max_distance": max_distance,
            "destination_policy": destination_policy,
            "commit": commit,
            "confirm_commit": confirm_commit,
            "normalize": normalize,
        },
        changed_objects=[target_mesh_name],
    )


@mcp.tool()
async def configure_bendy_bones(
    ctx: Context,
    armature_object_name: str,
    patches: Annotated[list[BendyBonePatch], Field(min_length=1, max_length=500)],
) -> dict:
    """
    Configure validated B-Bone display, curvature, scale, roll, and custom handles.

    Each patch's bone_name must name an existing bone on armature_object_name. Only fields
    explicitly set on a patch are sent and changed; omitted fields (including ones explicitly set
    to None) leave that setting untouched rather than clearing it. custom_handle_start/end, when
    given, must name an existing bone and require a non-AUTO handle_type_start/end.
    """
    return await call_blender(
        "configure_bendy_bones",
        {
            "armature_object_name": armature_object_name,
            "patches": [patch.model_dump(exclude_none=True, exclude_unset=True) for patch in patches],
        },
        changed_objects=[armature_object_name],
    )
