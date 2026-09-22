"""Typed tools for aiming, targeting, framing, and constraining cameras."""

from typing import Annotated, Literal

from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from ...app import mcp
from .._dispatch import call_blender
from ._shared import (
    ConstraintSpace,
    FollowForwardAxis,
    LockAxis,
    TrackAxis,
    UpAxis,
    _StrictModel,
    _tool_params,
)

TrackingConstraint = Literal["TRACK_TO", "DAMPED_TRACK", "LOCKED_TRACK"]
FramePolicy = Literal["MOVE_CAMERA", "CHANGE_LENS", "CHANGE_ORTHO_SCALE"]
CameraConstraint = Literal[
    "TRACK_TO",
    "DAMPED_TRACK",
    "LOCKED_TRACK",
    "FOLLOW_PATH",
    "CHILD_OF",
    "COPY_LOCATION",
    "COPY_ROTATION",
    "COPY_TRANSFORMS",
    "LIMIT_LOCATION",
    "LIMIT_ROTATION",
    "LIMIT_SCALE",
]


@mcp.tool()
async def point_camera_at(
    ctx: Context,
    scene_name: str,
    camera_name: str,
    target_object_name: str | None = None,
    target_point: tuple[float, float, float] | None = None,
    subtarget: str | None = None,
    camera_location: tuple[float, float, float] | None = None,
) -> dict:
    """
    Optionally place a camera at a world point, then rotate it once to aim at an object or point.

    Supply exactly one target source. Rotates local -Z toward the target with local Y as up,
    correctly resolving parent space. subtarget aims at the named bone's evaluated world head,
    its posed position at the current frame, not the armature's origin. This is a one-shot
    rotation, not a constraint — use add_camera_constraint for a live tracking relationship.

    camera_location is a world-space point applied before the aim, so place-and-aim is one call
    rather than set_object_transform followed by this tool. It is parent-aware: a camera parented
    to a rig root or a path follower still lands on that world point. It is refused when it
    coincides with the resolved target, because there is then no direction to look along. Use
    frame_camera_on_objects(policy="MOVE_CAMERA") instead when the distance should be solved from
    what must fit in frame rather than stated exactly.
    """
    if (target_object_name is None) == (target_point is None):
        raise ToolError("Supply exactly one of target_object_name or target_point")
    if subtarget is not None and target_object_name is None:
        raise ToolError("subtarget requires target_object_name")
    if camera_location is not None and target_point is not None and tuple(camera_location) == tuple(target_point):
        # The handler rejects this against the resolved target too (a target_object_name only
        # resolves inside Blender), but when both points are literal the round trip buys nothing.
        raise ToolError(f"camera_location {list(camera_location)} is the same point as target_point")
    return await call_blender(
        "point_camera_at",
        {
            "scene_name": scene_name,
            "camera_name": camera_name,
            "target_object_name": target_object_name,
            "target_point": target_point,
            "subtarget": subtarget,
            "camera_location": camera_location,
        },
        changed_objects=[camera_name],
    )


@mcp.tool()
async def create_camera_target(
    ctx: Context,
    scene_name: str,
    collection_name: str,
    name: Annotated[str, Field(min_length=1, max_length=63)],
    location: tuple[float, float, float] | None = None,
    target_object_name: str | None = None,
    use_evaluated_bounds_center: bool = True,
    reuse: bool = False,
    camera_names: list[str] | None = None,
    constraint_type: TrackingConstraint = "DAMPED_TRACK",
) -> dict:
    """
    Create or explicitly reuse a tagged Empty as a camera aim control.

    Supply either a world location or an object whose evaluated bounds center should be used. Reuse
    is opt-in and accepts only an Empty already tagged as a camera target. Named cameras receive a
    live -Z tracking constraint; unrelated constraints remain untouched.
    """
    if (location is None) == (target_object_name is None):
        raise ToolError("Supply exactly one of location or target_object_name")
    return await call_blender(
        "create_camera_target",
        {
            "scene_name": scene_name,
            "collection_name": collection_name,
            "name": name,
            "location": location,
            "target_object_name": target_object_name,
            "use_evaluated_bounds_center": use_evaluated_bounds_center,
            "reuse": reuse,
            "camera_names": camera_names or [],
            "constraint_type": constraint_type,
        },
    )


class BoneFrameTarget(_StrictModel):
    """One posed bone whose head-tail segment must fit in frame."""

    object_name: Annotated[str, Field(min_length=1, max_length=63)]
    bone_name: Annotated[str, Field(min_length=1, max_length=63)]
    radius_m: Annotated[float, Field(ge=0)] = 0.0


@mcp.tool()
async def frame_camera_on_objects(
    ctx: Context,
    scene_name: str,
    camera_name: str,
    object_names: list[str] | None = None,
    bone_targets: Annotated[list[BoneFrameTarget], Field(max_length=64)] | None = None,
    margin: Annotated[float, Field(ge=0, lt=0.9)] = 0.1,
    policy: FramePolicy = "MOVE_CAMERA",
    aim_at_center: bool = True,
) -> dict:
    """
    Fit explicit evaluated objects and posed bones in a camera without viewport operators.

    ``MOVE_CAMERA`` preserves perspective optics, ``CHANGE_LENS`` preserves camera position, and
    ``CHANGE_ORTHO_SCALE`` is required for orthographic scale changes. Modifier-evaluated world
    bounds, target point, solved distance or optical value, and limiting frame axis are returned.
    The margin is the fractional inset on each side of the render frame.

    Supply ``object_names``, ``bone_targets``, or both; both together frame their union. A bone
    target contributes its head-tail segment alone, read from the evaluated armature in world
    space at the current frame, so constraints and animation are respected. That segment is a
    line with no thickness, so ``radius_m`` pads it on every axis and is how the geometry
    *around* a bone is included: ``{"object_name": "my_rig", "bone_name": "thigh.L",
    "radius_m": 0.12}`` frames that bone plus 12 cm of limb. Naming a bone is how a region of a
    rig is framed without guessing which meshes cover it; the reply echoes each resolved
    ``head_world`` and ``tail_world``.
    """
    object_names = object_names or []
    bone_targets = bone_targets or []
    if not object_names and not bone_targets:
        raise ToolError("Supply at least one of object_names or bone_targets; both were empty")
    seen: set[tuple[str, str]] = set()
    for target in bone_targets:
        key = (target.object_name, target.bone_name)
        if key in seen:
            raise ToolError(f"bone_targets must not repeat bone '{target.bone_name}' on '{target.object_name}'")
        seen.add(key)
    return await call_blender(
        "frame_camera_on_objects",
        {
            "scene_name": scene_name,
            "camera_name": camera_name,
            "object_names": object_names,
            "bone_targets": [target.model_dump() for target in bone_targets],
            "margin": margin,
            "policy": policy,
            "aim_at_center": aim_at_center,
        },
        changed_objects=[camera_name],
    )


@mcp.tool()
async def add_camera_constraint(
    ctx: Context,
    scene_name: str,
    owner_name: str,
    constraint_name: Annotated[str, Field(min_length=1)],
    constraint_type: CameraConstraint,
    target_name: str | None = None,
    subtarget: str | None = None,
    influence: Annotated[float, Field(ge=0, le=1)] = 1.0,
    owner_space: ConstraintSpace = "WORLD",
    target_space: ConstraintSpace = "WORLD",
    stack_index: Annotated[int, Field(ge=-1)] = -1,
    preserve_transform: bool = True,
    track_axis: TrackAxis = "TRACK_NEGATIVE_Z",
    up_axis: UpAxis = "UP_Y",
    lock_axis: LockAxis = "LOCK_Y",
    forward_axis: FollowForwardAxis = "FORWARD_X",
    use_curve_follow: bool = True,
    use_fixed_location: bool = True,
    offset_factor: Annotated[float, Field(ge=0, le=1)] = 0.0,
    use_x: bool = True,
    use_y: bool = True,
    use_z: bool = True,
    invert_x: bool = False,
    invert_y: bool = False,
    invert_z: bool = False,
    minimum: tuple[float, float, float] | None = None,
    maximum: tuple[float, float, float] | None = None,
) -> dict:
    """Add or update one curated, typed camera-rig constraint with a stable name."""
    targeted = constraint_type not in {"LIMIT_LOCATION", "LIMIT_ROTATION", "LIMIT_SCALE"}
    if targeted != (target_name is not None):
        raise ToolError(
            "This constraint type requires target_name" if targeted else "Limit constraints do not use target_name"
        )
    if constraint_type.startswith("LIMIT_") and minimum is None and maximum is None:
        raise ToolError("Limit constraints require minimum and/or maximum")
    return await call_blender("add_camera_constraint", _tool_params(locals()), changed_objects=[owner_name])
