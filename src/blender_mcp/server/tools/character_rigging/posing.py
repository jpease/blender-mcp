"""Typed tools for deterministic pose application and pose keyframing."""

from typing import Annotated, Literal

from mcp.server.fastmcp import Context
from pydantic import Field, model_validator

from ...app import mcp
from .._dispatch import call_blender
from ..key_style import Easing, HandleType, Interpolation
from ._shared import _StrictModel

_SignedAxis = Literal["X", "-X", "Y", "-Y", "Z", "-Z"]
# A direction is a direction at any length, so only a vector that is zero to float noise is
# rejected; this is the squared length, so it is the square of a nanometre-scale threshold.
_MINIMUM_SQUARED_LENGTH = 1e-18


class BoneAim(_StrictModel):
    """Point one of a bone's own axes at a world-space point or at another object's origin."""

    target: tuple[float, float, float] | None = None
    target_object: Annotated[str, Field(min_length=1, max_length=63)] | None = None
    track_axis: _SignedAxis
    up_axis: _SignedAxis | None = None
    up_reference: tuple[float, float, float] = (0.0, 0.0, 1.0)

    @model_validator(mode="after")
    def validate_aim(self) -> "BoneAim":
        """
        Reject an aim that names no target, two targets, or a roll that cannot be resolved.

        Returns:
            BoneAim: This model, unchanged.

        Raises:
            ValueError: If neither or both target forms are given, if up_axis repeats the
                tracked axis, or if up_reference is a zero vector.

        """
        if (self.target is None) == (self.target_object is None):
            raise ValueError("Supply exactly one of target or target_object")
        if self.up_axis is not None and self.up_axis.lstrip("-") == self.track_axis.lstrip("-"):
            raise ValueError("up_axis must name a different bone axis than track_axis")
        if sum(value * value for value in self.up_reference) <= _MINIMUM_SQUARED_LENGTH:
            raise ValueError("up_reference must be non-zero")
        return self


class BoneRotation(_StrictModel):
    """A rotation in degrees about a named bone axis or an explicit axis vector."""

    axis: _SignedAxis | tuple[float, float, float]
    degrees: float
    relative: bool = False

    @model_validator(mode="after")
    def validate_axis(self) -> "BoneRotation":
        """
        Reject an axis vector with no direction.

        Returns:
            BoneRotation: This model, unchanged.

        Raises:
            ValueError: If axis is a zero vector.

        """
        if not isinstance(self.axis, str) and sum(value * value for value in self.axis) <= _MINIMUM_SQUARED_LENGTH:
            raise ValueError("axis must be non-zero")
        return self


class BonePose(_StrictModel):
    """One bone transform represented in an explicitly selected coordinate space."""

    bone_name: str = Field(min_length=1, max_length=63)
    matrix: (
        tuple[
            tuple[float, float, float, float],
            tuple[float, float, float, float],
            tuple[float, float, float, float],
            tuple[float, float, float, float],
        ]
        | None
    ) = None
    location: tuple[float, float, float] | None = None
    rotation_euler: tuple[float, float, float] | None = None
    rotation_quaternion: tuple[float, float, float, float] | None = None
    rotation_axis_angle: tuple[float, float, float, float] | None = None
    rotate: BoneRotation | None = None
    aim_at: BoneAim | None = None
    scale: tuple[float, float, float] | None = None
    custom_properties: dict[str, bool | int | float] | None = None

    @model_validator(mode="after")
    def validate_representation(self) -> "BonePose":
        rotations = [
            self.rotation_euler,
            self.rotation_quaternion,
            self.rotation_axis_angle,
            self.rotate,
            self.aim_at,
        ]
        if sum(value is not None for value in rotations) > 1:
            raise ValueError("Supply at most one rotation representation")
        if self.matrix is not None and any(value is not None for value in [self.location, *rotations, self.scale]):
            raise ValueError("matrix is mutually exclusive with location, rotation, and scale")
        if self.matrix is None and not any(
            value is not None for value in [self.location, *rotations, self.scale, self.custom_properties]
        ):
            raise ValueError("A pose entry must change at least one channel")
        if self.rotation_quaternion is not None and sum(value * value for value in self.rotation_quaternion) <= 1e-16:
            raise ValueError("rotation_quaternion must be non-zero")
        if (
            self.rotation_axis_angle is not None
            and sum(value * value for value in self.rotation_axis_angle[1:]) <= 1e-16
        ):
            raise ValueError("rotation_axis_angle axis must be non-zero")
        if self.scale is not None and any(value == 0 for value in self.scale):
            raise ValueError("scale components must be non-zero")
        return self


@mcp.tool()
async def list_character_bones(
    ctx: Context,
    armature_object_name: str,
    limit: Annotated[int, Field(ge=1, le=200)] = 100,
    offset: Annotated[int, Field(ge=0, le=99_999)] = 0,
    rest_axes: bool = False,
    bone_names: Annotated[list[str] | None, Field(min_length=1, max_length=200)] = None,
) -> dict:
    """
    List a rig's bone names, parents, and deform flags so a pose can name real bones.

    No coordinates are returned unless rest_axes is set; read get_character_rig_info for
    positions, lengths and rolls.

    Args:
        ctx: MCP request context.
        armature_object_name: An existing object of type ARMATURE; any other object is an error.
            Rest-bone edits still open in Edit Mode are flushed before reading.
        limit: Bones per page. A production rig carries a few hundred bones, so one page rarely
            covers a whole rig.
        offset: Where to resume. Pass the previous reply's next_offset while truncated is true.
        rest_axes: Also report each bone's rest axes, the datum an axis has to be chosen from:
            which way a bone's local X, Y and Z point is rig-specific and not guessable from
            its name. It costs about 180 wire bytes a bone, which the reply budget spends as
            roughly 22 bones a page instead of 57, so leave it off unless choosing an axis.
        bone_names: Report only these exact bones. Name the bones you intend to pose and read
            their rest axes in one call, instead of paging a whole rig to reach three of them -
            a 187-bone rig costs six calls with rest_axes and one with this. A name the
            armature does not have is an error, never a silent omission.

    Returns:
        armature_object, and bones with items (name, parent - null for a root - and deform,
        whether the bone deforms a bound mesh), total, offset, limit, truncated, and next_offset
        (null on the last page). With rest_axes, each item also carries rest_axes: nine numbers,
        the bone's rest X axis, then Y, then Z, each a unit direction in armature space. Items
        follow armature bone order, which lists a parent before its children.

    """
    return await call_blender(
        "list_character_bones",
        {
            "armature_object_name": armature_object_name,
            "limit": limit,
            "offset": offset,
            "rest_axes": rest_axes,
            "bone_names": bone_names,
        },
    )


@mcp.tool()
async def set_character_pose(
    ctx: Context,
    armature_object_name: str,
    poses: Annotated[list[BonePose], Field(min_length=1, max_length=500)],
    space: Literal["LOCAL", "LOCAL_WITH_PARENT", "POSE", "WORLD"] = "LOCAL",
    reset_unspecified: bool = False,
    confirm_reset_unspecified: bool = False,
    detail: bool = False,
) -> dict:
    """
    Apply explicit bone transforms without inserting animation keys.

    Each pose's bone_name must name an existing pose bone on armature_object_name. Only fields
    explicitly set on a pose entry are sent and changed; bones not named in poses are left as-is
    unless reset_unspecified=True (which requires confirm_reset_unspecified) resets every other
    pose bone to rest. Use keyframe_character_pose instead to record this as animation.

    A pose entry carries at most one rotation: rotation_euler, rotation_quaternion,
    rotation_axis_angle, rotate, or aim_at. LOCAL is the bone's own channel delta from rest and
    composes with its parent; LOCAL_WITH_PARENT, POSE and WORLD are absolute, so writing one
    replaces what the parent contributed. Parents are posed first, and an absolute child target
    is resolved against the parent this call already wrote. aim_at is always world-space.

    Args:
        ctx: MCP request context.
        poses: The bones to change, each with at most one rotation.
            rotate turns the bone degrees about axis - a signed bone axis name such as "-Y", or
            a vector in space - replacing its rotation unless relative=True composes with what
            is there. It is sugar: rotation_axis_angle says the same thing in radians.
            aim_at points the bone's track_axis at target (a world point) or target_object's
            origin. track_axis has no default, because a bone's length axis is rarely the one
            that "looks" anywhere; read the axes from list_character_bones(rest_axes=True).
            up_axis leans that bone axis toward up_reference (a world direction, default +Z)
            and so fixes the roll. Without up_axis the aim takes the shortest arc and keeps the
            roll the bone already holds, which depends on the pose it started from, and a swing
            past 150 degrees is refused because that roll is then arbitrary.
        detail: Also report each bone's pre-call pose matrix, and report both matrices at
            Blender's own precision instead of rounded to six decimal places.

    Returns:
        armature_object, space, changed_bones naming every posed bone, and bones with one
        record per posed bone (bone, the channels the call set - a custom property appears as
        its data path - and after_pose_matrix, the armature-space matrix it ended on). A long
        pose shortens bones to fit the reply budget; changed_bones stays complete.

    """
    if reset_unspecified and not confirm_reset_unspecified:
        raise ValueError("confirm_reset_unspecified=True is required to reset unspecified pose bones")
    return await call_blender(
        "set_character_pose",
        {
            "armature_object_name": armature_object_name,
            "poses": [pose.model_dump(exclude_none=True, exclude_unset=True) for pose in poses],
            "space": space,
            "reset_unspecified": reset_unspecified,
            "confirm_reset_unspecified": confirm_reset_unspecified,
            "detail": detail,
        },
        changed_objects=[armature_object_name],
    )


@mcp.tool()
async def keyframe_character_pose(
    ctx: Context,
    armature_object_name: str,
    action_name: Annotated[str, Field(min_length=1, max_length=63)],
    frame: float,
    poses: Annotated[list[BonePose], Field(min_length=1, max_length=500)],
    space: Literal["LOCAL", "LOCAL_WITH_PARENT", "POSE", "WORLD"] = "LOCAL",
    keying_policy: Literal["INSERT", "REPLACE", "REMOVE"] = "INSERT",
    interpolation: Interpolation = "BEZIER",
    handle_left: HandleType = "AUTO_CLAMPED",
    handle_right: HandleType = "AUTO_CLAMPED",
    easing: Easing | None = None,
    action_policy: Literal["ENSURE", "CREATE", "REUSE"] = "ENSURE",
    confirm_displace_action: bool = False,
    action_slot_identifier: str | None = None,
    detail: bool = False,
) -> dict:
    """
    Apply a pose and insert, replace, or remove exact keys in a named action.

    action_policy="ENSURE" (default) keys into action_name whether or not it already exists;
    "CREATE" requires it to be new, "REUSE" requires it to exist, and keying_policy="REMOVE"
    needs an action that is already there (REUSE, or ENSURE finding one). A rig holds one
    action, so keying a pose into a second one stops the first driving the rig and Blender
    drops an unreferenced action at save: when another action already holds keys, the call is
    refused unless confirm_displace_action=True. Root motion keyed by keyframe_object_transform
    lives in exactly such an action - pass both it and the pose the same action_name and they
    play back together. Each pose's bone_name must name an existing pose bone on
    armature_object_name. action_slot_identifier selects which of the action's animation slots
    to key by its `identifier`; it is only required when the action already has multiple
    candidate slots and none is unambiguously suitable (inspect the action's slots before
    assuming this can be omitted).

    The call leaves the rig driven by the action it keyed, which is what makes the animation
    part of the file: an action nothing references has no users and Blender drops it at save.
    assigned_action says what drives the rig afterwards, unassigned_action names a different
    action this call displaced, and the pose itself is restored, so the bones then show what
    the action says at the current frame.

    Pose entries take the same channels as set_character_pose, with one added rule: an aim_at
    must supply up_axis. A shortest-arc aim keeps whatever roll the bone already holds, so the
    same call at two frames would key two different rolls. A keyed aim is also re-spelled to
    interpolate the short way from the previous key in this action: the quaternion sign is
    flipped when it would take the long route, and an Euler triple is made compatible with the
    previous key.

    Args:
        ctx: MCP request context.
        frame: Fractional frames are keyed as subframes.
        poses: The bones to key; see set_character_pose for the channels.
        detail: Also report, as "bones", the pose each bone was keyed at - its pre-call and
            keyed armature-space matrices, at Blender's own precision.

    Returns:
        armature_object, action, action_slot, assigned_action (the action now driving the rig),
        unassigned_action when a different action was displaced, keying_policy, changed_bones
        naming every posed bone, changed_keys with one entry per keyed channel (bone, data_path,
        frame), and interpolation_updates. A long pose shortens changed_keys to fit the reply
        budget; changed_bones stays complete.

    """
    return await call_blender(
        "keyframe_character_pose",
        {
            "armature_object_name": armature_object_name,
            "action_name": action_name,
            "frame": frame,
            "poses": [pose.model_dump(exclude_none=True, exclude_unset=True) for pose in poses],
            "space": space,
            "keying_policy": keying_policy,
            "interpolation": interpolation,
            "handle_left": handle_left,
            "handle_right": handle_right,
            "easing": easing,
            "action_policy": action_policy,
            "confirm_displace_action": confirm_displace_action,
            "action_slot_identifier": action_slot_identifier,
            "detail": detail,
        },
        changed_objects=[armature_object_name],
    )


class ReachHinge(_StrictModel):
    """
    A temporary IK hinge applied to one chain bone for the duration of the solve.

    A knee and an elbow bend on one axis only, and an IK solver with three free axes will
    happily invert one to reach a target half a frame sooner. The limit is applied to the
    pose bone, read by the solve, and restored afterwards, so the rig is left exactly as it
    arrived - the same contract the temporary IK constraint already follows.
    """

    bone_name: str = Field(min_length=1, max_length=63)
    axis: Literal["X", "Y", "Z"]
    min_degrees: float = Field(ge=-180.0, le=180.0)
    max_degrees: float = Field(ge=-180.0, le=180.0)

    @model_validator(mode="after")
    def validate_hinge(self) -> "ReachHinge":
        """
        Reject an inverted limit interval.

        Returns:
            ReachHinge: This model, unchanged.

        Raises:
            ValueError: If min_degrees is greater than max_degrees.

        """
        if self.min_degrees > self.max_degrees:
            raise ValueError("min_degrees must not exceed max_degrees")
        return self


class _ReachChain(_StrictModel):
    """
    The chain, pole and solver settings every reach carries, whatever it does with them.

    solve_bone_reach poses one chain now and keyframe_bone_reach solves the same chain at many
    frames; everything except where the tail has to be is identical between them, the pole rule
    included. Two copies of it had to agree by hand.
    """

    tip_bone: str = Field(min_length=1, max_length=63)
    chain_length: Annotated[int, Field(ge=1, le=32)] | None = None
    pole_target: tuple[float, float, float] | None = None
    pole_target_object: Annotated[str, Field(min_length=1, max_length=63)] | None = None
    pole_angle_degrees: float = 0.0
    use_stretch: bool = False
    iterations: Annotated[int, Field(ge=1, le=1000)] = 500
    hinge: ReachHinge | None = None

    @model_validator(mode="after")
    def validate_pole(self) -> "_ReachChain":
        """
        Reject a reach that names two pole targets.

        Returns:
            _ReachChain: This model, unchanged.

        Raises:
            ValueError: If both pole forms are given.

        """
        if self.pole_target is not None and self.pole_target_object is not None:
            raise ValueError("Supply at most one of pole_target or pole_target_object")
        return self


class BoneReach(_ReachChain):
    """
    Bend an unbranched ancestor chain so tip_bone's TAIL reaches a world point.

    tip_bone is driven by a temporary Blender IK constraint, evaluated, captured, then
    removed - never left live on the rig - and the captured pose is applied through the same
    matrix+space="POSE" mechanism set_character_pose uses for an explicit matrix entry. Pick
    tip_bone as the bone whose POSITION must be exact; pose anything distal to it (e.g. a
    hand's grip shape) separately with set_character_pose's rotate/aim_at. For a handshake,
    that is usually a wrist-class bone, not the hand/fingertip bone - the hand's own
    orientation is posed separately.
    """

    target: tuple[float, float, float] | None = None
    target_object: Annotated[str, Field(min_length=1, max_length=63)] | None = None

    @model_validator(mode="after")
    def validate_target(self) -> "BoneReach":
        """
        Reject a reach that names no target, or two.

        Returns:
            BoneReach: This model, unchanged.

        Raises:
            ValueError: If neither or both target forms are given.

        """
        if (self.target is None) == (self.target_object is None):
            raise ValueError("Supply exactly one of target or target_object")
        return self


@mcp.tool()
async def solve_bone_reach(
    ctx: Context,
    armature_object_name: str,
    reaches: Annotated[list[BoneReach], Field(min_length=1, max_length=8)],
    tolerance_m: Annotated[float, Field(gt=0)] = 1e-4,
    detail: bool = False,
) -> dict:
    """
    Bend one or more unbranched bone chains so each tip_bone's tail reaches a world point.

    chain_length omitted: resolved to the longest unbranched ancestor run above tip_bone -
    every bone up to (not including) the first ancestor with more than one child, or a root.
    Always reported back, whether resolved or supplied explicitly.

    pole_target/pole_target_object omitted: synthesized from the chain's REST pose - the
    perpendicular offset of the chain's middle joint from the straight line between the
    chain's root-most head and tip_bone's rest tail. Refused when the rest pose is straight
    (no natural bend to infer a pole from); supply one explicitly then.

    Two reaches in the same call may not claim the same bone - solving it to two different
    targets is ambiguous, so it is refused rather than letting the later reach silently win.

    Every reach that misses tolerance_m also raises one warning saying whether the target was
    beyond the chain's reach or the solve stalled short of a reachable target.

    Args:
        ctx: MCP request context.
        armature_object_name: An existing object of type ARMATURE with pose_position='POSE'.
        reaches: One to eight independent chains to solve, applied together as one pose.
        tolerance_m: How close tip_bone's tail must land to the target, in metres, for the
            reach to report converged=True. The default 0.1 mm suits a character contact
            (a handshake, a hand on a prop); widen it for a gesture nobody measures, tighten
            it when two rigs must share an exact point.
        detail: Also report each bone's pre-call pose matrix, and report both matrices at
            Blender's own precision instead of rounded to six decimal places.

    Returns:
        armature_object, the tolerance_m the solve was judged against, changed_bones naming
        every bone any reach posed, and reaches with one entry per requested reach: tip_bone,
        chain_bones (tip first), chain_length, chain_length_source ("explicit" or
        "resolved"), pole_source ("explicit" or "resolved"), target_world, head_world,
        tail_world, achieved_error_m (the distance between tail_world and the target after
        the solve), converged (achieved_error_m within tolerance_m), chain_reach_m (the
        chain's maximum straight-line extension), target_distance_m (from the chain root
        bone's head to the target), out_of_reach (target_distance_m beyond chain_reach_m -
        no pose of this chain can reach that point), and bones, the same per-bone records
        set_character_pose returns for this chain.

    """
    return await call_blender(
        "solve_bone_reach",
        {
            "armature_object_name": armature_object_name,
            "reaches": [reach.model_dump(exclude_none=True) for reach in reaches],
            "tolerance_m": tolerance_m,
            "detail": detail,
        },
        changed_objects=[armature_object_name],
    )


class ReachKey(_StrictModel):
    """One frame of one reach: where the tip bone's tail must be at that frame."""

    frame: float
    target: tuple[float, float, float] | None = None
    target_object: Annotated[str, Field(min_length=1, max_length=63)] | None = None

    @model_validator(mode="after")
    def validate_key(self) -> "ReachKey":
        """
        Reject a key that names no target or two targets.

        Returns:
            ReachKey: This model, unchanged.

        Raises:
            ValueError: If neither or both target forms are given.

        """
        if (self.target is None) == (self.target_object is None):
            raise ValueError("Supply exactly one of target or target_object")
        return self


class KeyedBoneReach(_ReachChain):
    """One chain solved and keyed at several frames."""

    keys: Annotated[list[ReachKey], Field(min_length=1, max_length=250)]

    @model_validator(mode="after")
    def validate_frames(self) -> "KeyedBoneReach":
        """
        Reject the same frame keyed twice in one reach.

        Returns:
            KeyedBoneReach: This model, unchanged.

        Raises:
            ValueError: If two keys name the same frame.

        """
        frames = [key.frame for key in self.keys]
        if len(set(frames)) != len(frames):
            raise ValueError("Each frame may appear at most once in one reach's keys")
        return self


@mcp.tool()
async def keyframe_bone_reach(
    ctx: Context,
    armature_object_name: str,
    action_name: Annotated[str, Field(min_length=1, max_length=63)],
    reaches: Annotated[list[KeyedBoneReach], Field(min_length=1, max_length=8)],
    tolerance_m: Annotated[float, Field(gt=0)] = 1e-4,
    keying_policy: Literal["INSERT", "REPLACE"] = "REPLACE",
    interpolation: Interpolation = "BEZIER",
    handle_left: HandleType = "AUTO_CLAMPED",
    handle_right: HandleType = "AUTO_CLAMPED",
    easing: Easing | None = None,
    action_policy: Literal["ENSURE", "CREATE", "REUSE"] = "ENSURE",
    confirm_displace_action: bool = False,
    action_slot_identifier: str | None = None,
    detail: bool = False,
) -> dict:
    """
    Solve an IK reach at many frames and key every one of them in a single call.

    This is how a foot is planted. Give the same world target at each contact frame and the
    foot stays on that point while the hips travel over it, because every frame is solved
    against the parents' evaluated pose at that frame. Repeating an FK rotation instead
    slides the foot, and rotating a thigh never bends a knee.

    Two preconditions, neither guessable:

    1. Body and root motion must ALREADY be keyed in action_name before this call. Each
       frame is solved after the playhead moves there, so the chain's parents - hips, root -
       hold whatever the action says at that frame. Key the travel with
       keyframe_object_transform(action_name=<same name>) and the body with
       keyframe_character_pose(action_name=<same name>) first.
    2. A planted foot is the same target repeated across the contact frames. A target that
       moves during contact is a foot that slides; interpolation cannot fix that.

    keying_policy defaults to REPLACE here, unlike keyframe_character_pose, because a
    multi-frame solve is normally re-run after the body pose changes. REMOVE is not offered:
    there is nothing to solve when removing keys - use
    keyframe_character_pose(keying_policy="REMOVE").

    Args:
        ctx: MCP request context.
        armature_object_name: An existing object of type ARMATURE with pose_position='POSE'.
        action_name: The action every key lands in - the same one holding the root motion.
        reaches: One to eight chains, each with its own frames. Two reaches may not claim the
            same bone. Each reach takes the same chain_length/pole/iterations fields as
            solve_bone_reach, plus hinge: a temporary one-axis IK limit (e.g. the shin,
            axis="X", 0 to 150 degrees) that stops a knee or elbow inverting during the
            solve. The limit is removed again before the call returns.
        tolerance_m: How close the tail must land to count as converged, per frame.
        keying_policy: REPLACE overwrites a frame's existing keys; INSERT adds to them.
        handle_left: Left Bézier handle type, applied only under interpolation="BEZIER".
        handle_right: Right Bézier handle type, applied only under interpolation="BEZIER".
        easing: Easing direction, meaningful for the SINE..ELASTIC interpolations.
        action_policy: ENSURE keys into action_name whether or not it exists; CREATE
            requires it to be new, REUSE requires it to exist.
        confirm_displace_action: Required to displace a different action that holds keys.
        action_slot_identifier: Which of the action's slots to key, when several qualify.
        interpolation: Applied to every key this call writes. A contact key usually wants
            handle_left/handle_right="VECTOR" so the foot does not ease through the floor.
        detail: Also report each bone's pre-call pose matrix at Blender's own precision.

    Returns:
        armature_object, action, action_slot, assigned_action, unassigned_action (only when
        one was displaced), tolerance_m, keying_policy, changed_bones (complete),
        keyed_frames, changed_keys, and reaches with one record per requested reach:
        tip_bone, chain_bones, chain_length, chain_length_source, pole_source, and keys -
        per frame the frame, target_world, tail_world, achieved_error_m, converged,
        chain_reach_m, target_distance_m and out_of_reach. Check converged on every frame
        before moving on; each miss also raises a warning naming its frame.

    """
    return await call_blender(
        "keyframe_bone_reach",
        {
            "armature_object_name": armature_object_name,
            "action_name": action_name,
            "reaches": [reach.model_dump(exclude_none=True) for reach in reaches],
            "tolerance_m": tolerance_m,
            "keying_policy": keying_policy,
            "interpolation": interpolation,
            "handle_left": handle_left,
            "handle_right": handle_right,
            "easing": easing,
            "action_policy": action_policy,
            "confirm_displace_action": confirm_displace_action,
            "action_slot_identifier": action_slot_identifier,
            "detail": detail,
        },
        changed_objects=[armature_object_name],
    )
