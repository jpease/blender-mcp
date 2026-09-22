"""Typed tools for generic Blender animation data and layered Actions."""

import ast
import re

from typing import Annotated, Literal

from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..app import mcp
from ._dispatch import call_blender
from .key_style import Easing, HandleType, Interpolation

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
# `modifiers` is a page the envelope shortens, and this tool takes no offset, so a cut page is
# gone rather than resumable - the generic shortening notice can only say "narrow the scope",
# which is not a move unless the reader already knows which parameter narrows. Named here and
# passed into the reply on an unscoped call so it is present while the page is measured.
_UNSCOPED_CYCLE_WARNING = (
    "Every curve in the slot was cycled; a shortened modifiers page carries no offset to resume from, so "
    're-run with data_path_prefix - \'pose.bones["thigh.L"]\' for one limb, "location" for the root alone - '
    "to see the records it dropped."
)
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
    interpolation: Interpolation = "BEZIER"
    handle_left: HandleType = "AUTO_CLAMPED"
    handle_right: HandleType = "AUTO_CLAMPED"
    easing: Easing | None = None
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


@mcp.tool()
async def inspect_animation(
    ctx: Context,
    target: AnimationTarget,
    offset: Annotated[int, Field(ge=0)] = 0,
    limit: Annotated[int, Field(ge=1, le=1000)] = 200,
) -> dict:
    """Inspect an ID's active Action, layered slots, keyframes, drivers, and NLA strips with pagination."""
    return await call_blender(
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
    return await call_blender(
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
    return await call_blender(
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
async def set_action_cycle(
    ctx: Context,
    target: AnimationTarget,
    action_name: Annotated[str, Field(min_length=1, max_length=128)],
    operation: Literal["SET", "REMOVE"] = "SET",
    mode_before: Literal["NONE", "REPEAT", "REPEAT_OFFSET", "MIRROR"] = "NONE",
    mode_after: Literal["NONE", "REPEAT", "REPEAT_OFFSET", "MIRROR"] = "REPEAT_OFFSET",
    cycles_before: Annotated[int, Field(ge=0, le=10_000)] = 0,
    cycles_after: Annotated[int, Field(ge=0, le=10_000)] = 0,
    expected_period_frames: Annotated[float, Field(gt=0)] | None = None,
    frame_start: float | None = None,
    frame_end: float | None = None,
    blend_in: Annotated[float, Field(ge=0)] = 0.0,
    blend_out: Annotated[float, Field(ge=0)] = 0.0,
    data_path_prefix: Annotated[str, Field(min_length=1, max_length=512)] | None = None,
    action_slot_identifier: str | None = None,
) -> dict:
    """
    Make an action's curves repeat outside their keyed range, which is what turns 24 keyed frames into a walk.

    REPEAT_OFFSET adds the curve's start-to-end delta to each repeat, so a root that travelled
    one metre across the cycle keeps travelling: repeat two starts where repeat one ended.
    REPEAT restarts from the first key every cycle, which teleports a travelling character back
    to the origin; use it for curves that return to where they began, and for the rotations of a
    cycle authored in place. mode_after defaults to REPEAT_OFFSET because a forward loop is what
    is nearly always being asked for; mode_before defaults to NONE because extrapolating the
    same cycle backwards forever is not free - it fills every frame before the first key with
    motion nobody requested, and on a travelling root it walks the character backwards out of
    the set. Ask for it explicitly.

    The period is structural and not a parameter: a Cycles modifier repeats its own curve's
    first-to-last key extent. Keying anything on a cycled curve outside the intended cycle
    therefore changes what that curve repeats - one gesture key at frame 60 turns a 20-frame
    loop into a 59-frame one, on that curve alone. Pass expected_period_frames to be refused
    instead of finding out at playback. To cycle part of a shot and still animate the same bone
    elsewhere in it, the supported route is an NLA strip with repeat (manage_nla_tracks), which
    loops a bounded slice of the action, not this tool.

    Args:
        ctx: MCP request context.
        target: The ID whose action is made cyclic.
        action_name: The action to modify. An action of that name must exist.
        operation: SET adds or updates the Cycles modifier; REMOVE deletes it, leaving a
            curve that has none untouched. REMOVE accepts none of the arguments below that
            describe a cycle, since it creates none.
        mode_before: Extrapolation before the first key.
        mode_after: Extrapolation after the last key.
        cycles_before: How many repeats before the range; 0 is unlimited.
        cycles_after: How many repeats after the range; 0 is unlimited.
        expected_period_frames: The period every selected curve must already measure, in
            frames. Any curve whose own key extent differs, or that has no extent at all, is
            refused by name before a single modifier is created or changed.
        frame_start: First frame the modifier applies on, given together with frame_end. Bounds
            WHERE the modifier applies; it does not change the period.
        frame_end: Last frame the modifier applies on, later than frame_start.
        blend_in: Frames over which the modifier fades in at frame_start.
        blend_out: Frames over which it fades out at frame_end.
        data_path_prefix: Only touch curves whose data_path starts with this - e.g.
            'pose.bones["thigh.L"]' for one limb, or "location" for root travel alone.
            Omitted, every curve in the slot is made cyclic.
        action_slot_identifier: Which of the action's slots to modify, when several qualify.

    Returns:
        action, action_slot, operation, curve_count, warnings, and modifiers - per curve:
        data_path, array_index, mode_before, mode_after, first_key_frame, last_key_frame,
        period_frames (what that curve repeats: its own key extent, null under two keys),
        restricted_range (frame_start/frame_end/blend_in/blend_out, only when one was
        given), and, for a finite count, repeat_end_frame/repeat_start_frame - where
        repetition stops and the curve's own extrapolation takes over, by default holding
        the end key: a snap, then a freeze. modifiers shortens to fit the reply budget.

    """
    if (frame_start is None) != (frame_end is None):
        raise ToolError("frame_start and frame_end must be given together")
    if frame_start is not None and frame_end is not None and frame_end <= frame_start:
        raise ToolError(f"frame_end must be greater than frame_start; got {frame_start} to {frame_end}")
    if operation == "REMOVE":
        # Accepting and ignoring them would report a success that did none of what was asked.
        unusable = [
            name
            for name, given in (
                ("expected_period_frames", expected_period_frames is not None),
                ("frame_start", frame_start is not None),
                ("blend_in", bool(blend_in)),
                ("blend_out", bool(blend_out)),
            )
            if given
        ]
        if unusable:
            raise ToolError(f"REMOVE deletes the Cycles modifier and cannot apply {', '.join(unusable)}")
    return await call_blender(
        "set_action_cycle",
        {
            "target": target.model_dump(),
            "action_name": action_name,
            "operation": operation,
            "mode_before": mode_before,
            "mode_after": mode_after,
            "cycles_before": cycles_before,
            "cycles_after": cycles_after,
            "expected_period_frames": expected_period_frames,
            "frame_start": frame_start,
            "frame_end": frame_end,
            "blend_in": blend_in,
            "blend_out": blend_out,
            "data_path_prefix": data_path_prefix,
            "action_slot_identifier": action_slot_identifier,
        },
        changed_resources=[action_name],
        warnings=[] if data_path_prefix else [_UNSCOPED_CYCLE_WARNING],
    )


@mcp.tool()
async def bake_evaluated_animation(
    ctx: Context,
    target: EvaluatedBakeTarget,
    frame_start: int,
    frame_end: int,
    frame_step: Annotated[int, Field(ge=1, le=10_000)] = 1,
    action_name: Annotated[str, Field(min_length=1, max_length=128)] = "Evaluated Bake",
    interpolation: Interpolation = "LINEAR",
    handle_left: HandleType = "AUTO_CLAMPED",
    handle_right: HandleType = "AUTO_CLAMPED",
    easing: Easing | None = None,
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
    return await call_blender(
        "bake_evaluated_animation",
        {
            "target": target.model_dump(),
            "frame_start": frame_start,
            "frame_end": frame_end,
            "frame_step": frame_step,
            "action_name": action_name,
            "interpolation": interpolation,
            "handle_left": handle_left,
            "handle_right": handle_right,
            "easing": easing,
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
    return await call_blender(
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
    return await call_blender(
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
