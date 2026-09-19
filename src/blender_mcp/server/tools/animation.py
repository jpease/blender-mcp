"""Typed tools for generic Blender animation data and layered Actions."""

import ast
import asyncio
import re

from typing import Annotated, Literal

from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..app import mcp
from ..connection import get_blender_connection
from .envelope import ok

AnimationTargetType = Literal[
    "OBJECT",
    "SCENE",
    "MATERIAL",
    "WORLD",
    "CAMERA",
    "LIGHT",
    "MESH",
    "CURVE",
    "ARMATURE",
    "SHAPE_KEYS",
    "NODE_GROUP",
]
_DRIVER_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SAFE_EXPRESSION_NODES = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.Name,
    # `ast.walk` yields each Name's `ctx` too, and in `mode="eval"` that is always Load: without
    # it every expression naming a variable - `frame` included - is refused.
    ast.Load,
    ast.Constant,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.UAdd,
    ast.USub,
)


class AnimationTarget(BaseModel):
    """Exact Blender ID datablock that owns animation data."""

    model_config = ConfigDict(extra="forbid")

    type: AnimationTargetType
    name: Annotated[str, Field(min_length=1)]


class KeyframeEdit(BaseModel):
    """One key insertion/update or removal on an RNA property."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    operation: Literal["UPSERT", "REMOVE"] = "UPSERT"
    data_path: Annotated[str, Field(min_length=1, max_length=512)]
    array_index: Annotated[int, Field(ge=-1, le=63)] = -1
    frame: Annotated[float, Field(ge=-1_000_000, le=1_000_000)]
    value: float | list[float] | None = None
    interpolation: Literal["CONSTANT", "LINEAR", "BEZIER"] = "BEZIER"
    group: Annotated[str | None, Field(min_length=1, max_length=128)] = None

    @model_validator(mode="after")
    def validate_operation(self) -> "KeyframeEdit":
        """Require a value only when creating or updating a key."""
        if self.operation == "UPSERT" and self.value is None:
            raise ValueError("UPSERT requires value")
        if self.operation == "REMOVE" and self.value is not None:
            raise ValueError("REMOVE does not accept value")
        return self


class BakePropertyChannel(BaseModel):
    """One scalar or array RNA property sampled from the evaluated target."""

    model_config = ConfigDict(extra="forbid")
    data_path: Annotated[str, Field(min_length=1, max_length=512)]
    array_indices: Annotated[list[int] | None, Field(min_length=1, max_length=64)] = None
    tolerance: Annotated[float, Field(ge=0)] = 0.0


class EvaluatedBakeTarget(BaseModel):
    """One object or armature and the evaluated channels to bake."""

    model_config = ConfigDict(extra="forbid")
    object_name: Annotated[str, Field(min_length=1)]
    transforms: Annotated[list[Literal["LOCATION", "ROTATION", "SCALE"]], Field(max_length=3)] = Field(
        default_factory=list
    )
    bone_names: Annotated[list[str], Field(max_length=10_000)] = Field(default_factory=list)
    properties: Annotated[list[BakePropertyChannel], Field(max_length=1_000)] = Field(default_factory=list)
    space: Literal["LOCAL", "WORLD", "POSE"] = "LOCAL"

    @model_validator(mode="after")
    def require_channels(self) -> "EvaluatedBakeTarget":
        """Require transforms, bones, or explicitly selected properties."""
        if not self.transforms and not self.bone_names and not self.properties:
            raise ValueError("bake target must request transforms, bone_names, or properties")
        if self.bone_names and not self.transforms:
            raise ValueError("bone_names require at least one transform channel")
        return self


class NlaTrackPatch(BaseModel):
    """Validated NLA track-state patch."""

    model_config = ConfigDict(extra="forbid")

    mute: bool | None = None
    solo: bool | None = None
    lock: bool | None = None

    @model_validator(mode="after")
    def require_field(self) -> "NlaTrackPatch":
        """Reject empty patches."""
        if not self.model_fields_set:
            raise ValueError("patch must set at least one field")
        return self


class NlaStripPatch(BaseModel):
    """Validated NLA strip timing and blending patch."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    frame_start: float | None = None
    frame_end: float | None = None
    action_frame_start: float | None = None
    action_frame_end: float | None = None
    blend_type: Literal["REPLACE", "COMBINE", "ADD", "SUBTRACT", "MULTIPLY"] | None = None
    extrapolation: Literal["NOTHING", "HOLD", "HOLD_FORWARD"] | None = None
    influence: Annotated[float | None, Field(ge=0, le=1)] = None
    repeat: Annotated[float | None, Field(gt=0, le=10_000)] = None
    scale: Annotated[float | None, Field(gt=0, le=10_000)] = None
    mute: bool | None = None

    @model_validator(mode="after")
    def validate_patch(self) -> "NlaStripPatch":
        """Reject empty patches and inverted explicitly supplied ranges."""
        if not self.model_fields_set:
            raise ValueError("patch must set at least one field")
        if self.frame_start is not None and self.frame_end is not None and self.frame_end <= self.frame_start:
            raise ValueError("frame_end must be greater than frame_start")
        if (
            self.action_frame_start is not None
            and self.action_frame_end is not None
            and self.action_frame_end <= self.action_frame_start
        ):
            raise ValueError("action_frame_end must be greater than action_frame_start")
        return self


class DriverVariable(BaseModel):
    """One safe driver input sourced from a property or object transform."""

    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(min_length=1, max_length=64)]
    type: Literal["SINGLE_PROP", "TRANSFORMS"]
    target: AnimationTarget
    data_path: Annotated[str | None, Field(min_length=1, max_length=512)] = None
    bone_target: str | None = None
    transform_type: (
        Literal[
            "LOC_X",
            "LOC_Y",
            "LOC_Z",
            "ROT_X",
            "ROT_Y",
            "ROT_Z",
            "ROT_W",
            "SCALE_X",
            "SCALE_Y",
            "SCALE_Z",
            "SCALE_AVG",
        ]
        | None
    ) = None
    transform_space: Literal["WORLD_SPACE", "TRANSFORM_SPACE", "LOCAL_SPACE"] = "WORLD_SPACE"

    @model_validator(mode="after")
    def validate_source(self) -> "DriverVariable":
        """Validate names and fields required by each driver-variable kind."""
        if not _DRIVER_NAME_RE.fullmatch(self.name) or self.name == "frame":
            raise ValueError("name must be a Python identifier other than 'frame'")
        if self.type == "SINGLE_PROP" and self.data_path is None:
            raise ValueError("SINGLE_PROP requires data_path")
        if self.type == "TRANSFORMS":
            if self.target.type != "OBJECT" or self.transform_type is None:
                raise ValueError("TRANSFORMS requires an OBJECT target and transform_type")
            if self.data_path is not None:
                raise ValueError("TRANSFORMS does not accept data_path")
        return self


def _validate_safe_expression(expression: str, variable_names: set[str]) -> None:
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ValueError("expression must be valid arithmetic syntax") from exc
    for node in ast.walk(tree):
        if not isinstance(node, _SAFE_EXPRESSION_NODES):
            raise ValueError("expression may contain only arithmetic, numeric constants, variables, and frame")
        if isinstance(node, ast.Name) and node.id not in variable_names | {"frame"}:
            raise ValueError(f"expression references undeclared variable: {node.id}")
        if isinstance(node, ast.Constant) and (
            isinstance(node.value, bool) or not isinstance(node.value, (int, float))
        ):
            raise ValueError("expression constants must be numeric")


async def _call(command: str, params: dict, *, changed_resources: list[str] | None = None) -> dict:
    result = await asyncio.to_thread(get_blender_connection().send_command, command, params)
    resources = changed_resources or []
    if isinstance(result, dict):
        result = dict(result)
        resources = result.pop("changed_resources", resources)
    return ok(result, changed_resources=resources)


@mcp.tool()
async def inspect_animation(
    ctx: Context,
    target: AnimationTarget,
    offset: Annotated[int, Field(ge=0)] = 0,
    limit: Annotated[int, Field(ge=1, le=1000)] = 200,
) -> dict:
    """Inspect an ID's active Action, layered slots, keyframes, drivers, and NLA strips with pagination."""
    return await _call(
        "inspect_animation",
        {"target": target.model_dump(), "offset": offset, "limit": limit},
    )


@mcp.tool()
async def manage_animation_action(
    ctx: Context,
    target: AnimationTarget,
    action: Literal["CREATE", "ASSIGN", "DUPLICATE", "UNASSIGN"],
    action_name: Annotated[str | None, Field(min_length=1)] = None,
    source_action_name: Annotated[str | None, Field(min_length=1)] = None,
    replace_active: bool = False,
) -> dict:
    """Create, assign, duplicate, or unassign a Blender 5.1+ layered Action on one exact ID."""
    if action in {"CREATE", "ASSIGN", "DUPLICATE"} and action_name is None:
        raise ToolError(f"{action} requires action_name")
    if action == "DUPLICATE" and source_action_name is None:
        raise ToolError("DUPLICATE requires source_action_name")
    if action != "DUPLICATE" and source_action_name is not None:
        raise ToolError("source_action_name is only valid for DUPLICATE")
    return await _call(
        "manage_animation_action",
        {
            "target": target.model_dump(),
            "action": action,
            "action_name": action_name,
            "source_action_name": source_action_name,
            "replace_active": replace_active,
        },
        changed_resources=[target.name, action_name] if action_name else [target.name],
    )


@mcp.tool()
async def edit_keyframes(
    ctx: Context,
    target: AnimationTarget,
    edits: Annotated[list[KeyframeEdit], Field(min_length=1, max_length=1000)],
    action_name: Annotated[str | None, Field(min_length=1)] = None,
    replace_active_action: bool = False,
    allow_shared_action: bool = False,
) -> dict:
    """Batch-upsert or remove validated property keyframes in one layered Action without changing current values."""
    return await _call(
        "edit_keyframes",
        {
            "target": target.model_dump(),
            "edits": [edit.model_dump() for edit in edits],
            "action_name": action_name,
            "replace_active_action": replace_active_action,
            "allow_shared_action": allow_shared_action,
        },
        changed_resources=[target.name, action_name] if action_name else [target.name],
    )


@mcp.tool()
async def bake_evaluated_animation(
    ctx: Context,
    target: EvaluatedBakeTarget,
    frame_start: int,
    frame_end: int,
    frame_step: Annotated[int, Field(ge=1, le=10_000)] = 1,
    action_name: Annotated[str, Field(min_length=1, max_length=128)] = "Evaluated Bake",
    interpolation: Literal["CONSTANT", "LINEAR", "BEZIER"] = "LINEAR",
    transform_tolerance: Annotated[float, Field(ge=0)] = 0.0,
    confirm_bake: bool = False,
) -> dict:
    """Bake dependency-graph object/bone transforms and selected properties into a new Action."""
    if frame_end < frame_start:
        raise ToolError("frame_end must be greater than or equal to frame_start")
    if ((frame_end - frame_start) // frame_step) + 1 > 100_000:
        raise ToolError("Bake range exceeds the 100000-sample safety limit")
    if not confirm_bake:
        raise ToolError("confirm_bake=True is required")
    return await _call(
        "bake_evaluated_animation",
        {
            "target": target.model_dump(),
            "frame_start": frame_start,
            "frame_end": frame_end,
            "frame_step": frame_step,
            "action_name": action_name,
            "interpolation": interpolation,
            "transform_tolerance": transform_tolerance,
            "confirm_bake": confirm_bake,
        },
        changed_resources=[target.object_name, action_name],
    )


@mcp.tool()
async def manage_nla_tracks(
    ctx: Context,
    target: AnimationTarget,
    action: Literal["CREATE_TRACK", "ADD_STRIP", "PATCH_TRACK", "PATCH_STRIP", "REMOVE_STRIP", "REMOVE_TRACK"],
    track_name: Annotated[str, Field(min_length=1)],
    strip_name: Annotated[str | None, Field(min_length=1)] = None,
    action_name: Annotated[str | None, Field(min_length=1)] = None,
    frame_start: float | None = None,
    track_patch: NlaTrackPatch | None = None,
    strip_patch: NlaStripPatch | None = None,
    confirm_remove: bool = False,
) -> dict:
    """Create, patch, or explicitly remove NLA tracks and strips that reference existing layered Actions."""
    if action == "ADD_STRIP" and (strip_name is None or action_name is None or frame_start is None):
        raise ToolError("ADD_STRIP requires strip_name, action_name, and frame_start")
    if action in {"PATCH_STRIP", "REMOVE_STRIP"} and strip_name is None:
        raise ToolError(f"{action} requires strip_name")
    if action == "PATCH_TRACK" and track_patch is None:
        raise ToolError("PATCH_TRACK requires track_patch")
    if action == "PATCH_STRIP" and strip_patch is None:
        raise ToolError("PATCH_STRIP requires strip_patch")
    if action.startswith("REMOVE") and not confirm_remove:
        raise ToolError("confirm_remove=True is required for removal")
    return await _call(
        "manage_nla_tracks",
        {
            "target": target.model_dump(),
            "action": action,
            "track_name": track_name,
            "strip_name": strip_name,
            "action_name": action_name,
            "frame_start": frame_start,
            "track_patch": track_patch.model_dump(exclude_none=True) if track_patch else None,
            "strip_patch": strip_patch.model_dump(exclude_none=True) if strip_patch else None,
            "confirm_remove": confirm_remove,
        },
        changed_resources=[target.name, track_name],
    )


@mcp.tool()
async def manage_animation_driver(
    ctx: Context,
    target: AnimationTarget,
    action: Literal["ADD", "PATCH", "REMOVE"],
    data_path: Annotated[str, Field(min_length=1, max_length=512)],
    array_index: Annotated[int, Field(ge=-1, le=63)] = -1,
    driver_type: Literal["AVERAGE", "SUM", "MIN", "MAX", "SCRIPTED"] | None = None,
    expression: Annotated[str | None, Field(min_length=1, max_length=256)] = None,
    variables: Annotated[list[DriverVariable] | None, Field(max_length=64)] = None,
    mute: bool | None = None,
    confirm_remove: bool = False,
) -> dict:
    """Add, patch, or remove one driver; scripted expressions permit safe arithmetic only, never Python calls."""
    if action == "ADD" and driver_type is None:
        raise ToolError("ADD requires driver_type")
    if action == "REMOVE" and not confirm_remove:
        raise ToolError("confirm_remove=True is required for REMOVE")
    if action == "REMOVE" and any(value is not None for value in (driver_type, expression, variables, mute)):
        raise ToolError("REMOVE does not accept driver settings")
    if expression is not None:
        if driver_type not in {None, "SCRIPTED"}:
            raise ToolError("expression is valid only for a SCRIPTED driver")
        _validate_safe_expression(expression, {variable.name for variable in variables or []})
    return await _call(
        "manage_animation_driver",
        {
            "target": target.model_dump(),
            "action": action,
            "data_path": data_path,
            "array_index": array_index,
            "driver_type": driver_type,
            "expression": expression,
            "variables": [variable.model_dump() for variable in variables] if variables is not None else None,
            "mute": mute,
            "confirm_remove": confirm_remove,
        },
        changed_resources=[target.name],
    )
