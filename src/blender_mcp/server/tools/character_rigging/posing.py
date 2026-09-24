"""Typed tools for deterministic pose application and pose keyframing."""

from typing import Annotated, Literal

from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field, model_validator

from ...app import mcp
from .._dispatch import call_blender
from .._inputs import StrictModel, dump_inputs
from ..key_style import Easing, HandleType, Interpolation

_SignedAxis = Literal["X", "-X", "Y", "-Y", "Z", "-Z"]
# A direction is a direction at any length, so only a vector that is zero to float noise is
# rejected; this is the squared length, so it is the square of a nanometre-scale threshold.
_MINIMUM_SQUARED_LENGTH = 1e-18


class BoneAim(StrictModel):
    """
    Point one of a bone's own axes at a world point, at an object, or at a bone on that object.

    track_axis and up_axis name the bone's own axes. Read them from
    list_character_bones(rest_axes=True): aim_axis_for_world's entry for the world direction the
    bone should point along is track_axis, and up_axis is that reply's up_axis (the "+Z" entry)
    unless track_axis already took that axis, in which case name another entry. The reply's
    length_axis is the axis along the bone - "Y" on every bone Blender builds - and is almost
    never the axis that should look anywhere. target_point, target_object_name and up_reference are
    world-space; target_object_name alone aims at the object's origin, which on a character rig is
    the floor under it, so name target_bone_name to aim at a bone on it.
    """

    target_point: tuple[float, float, float] | None = None
    target_object_name: Annotated[str, Field(min_length=1, max_length=63)] | None = None
    target_bone_name: Annotated[str, Field(min_length=1, max_length=63)] | None = None
    target_bone_position: Literal["HEAD", "TAIL", "CENTER"] = "HEAD"
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
            ValueError: If neither or both target forms are given, if target_bone_name names no
                object to find it on, if a position is named without a bone, if up_axis repeats
                the tracked axis, or if up_reference is a zero vector.

        """
        if (self.target_point is None) == (self.target_object_name is None):
            raise ValueError("Supply exactly one of target_point or target_object_name")
        if self.target_bone_name is not None and self.target_object_name is None:
            raise ValueError("target_bone_name names a bone on target_object_name, so target_object_name is required")
        if self.target_bone_name is None and "target_bone_position" in self.model_fields_set:
            raise ValueError("target_bone_position names a point on target_bone_name, which this aim does not name")
        if self.up_axis is not None and self.up_axis.lstrip("-") == self.track_axis.lstrip("-"):
            raise ValueError("up_axis must name a different bone axis than track_axis")
        if sum(value * value for value in self.up_reference) <= _MINIMUM_SQUARED_LENGTH:
            raise ValueError("up_reference must be non-zero")
        return self


class BoneRotation(StrictModel):
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


class BonePose(StrictModel):
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


# Three opt-in sections is this tool's ceiling: rest axes, custom properties and deformed
# meshes. Each is a different question about the same armature and each is off by default, so
# the quiet reply stays the cheap one - but the schema is read whole by every session that
# mounts it, and a fourth section would cost every caller bytes for a question most of them
# are not asking. Put the next one in its own tool, as sample_deformed_geometry is.
@mcp.tool()
async def list_character_bones(
    ctx: Context,
    armature_object_name: str,
    limit: Annotated[int, Field(ge=1, le=200)] = 100,
    offset: Annotated[int, Field(ge=0, le=99_999)] = 0,
    rest_axes: bool = False,
    bone_names: Annotated[list[str] | None, Field(min_length=1, max_length=200)] = None,
    custom_properties: bool = False,
    property_offset: Annotated[int, Field(ge=0, le=99_999)] = 0,
    deformed_meshes: bool = False,
    mesh_offset: Annotated[int, Field(ge=0, le=99_999)] = 0,
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
        rest_axes: Also report each bone's rest axes, and name the bone axis that already points
            along each world direction. Which way a bone's own X, Y and Z point is rig-specific
            and not guessable from its name, and the letters an aim takes cannot be derived from
            the nine numbers alone. It costs about 375 wire bytes a bone, measured, which the
            reply budget spends as roughly 12 bones a page instead of 57, so leave it off unless
            choosing an axis - and name bone_names when you do.
        bone_names: Report only these exact bones. Name the bones you intend to pose and read
            their rest axes in one call, instead of paging a whole rig to reach three of them -
            a 187-bone rig costs sixteen calls with rest_axes and one with this. A name the
            armature does not have is an error, never a silent omission.
        custom_properties: Also report each bone's pose-bone custom properties - the sliders a
            rig carries, and the exact names set_character_pose and keyframe_character_pose
            take in their own custom_properties. A face rig commonly puts hundreds on one
            control bone, so name that bone in bone_names rather than paging the rig with this
            on.
        property_offset: Where to resume inside each reported bone's property list, when a bone
            holds more than one page of them. Pass the item's custom_property_next_offset.
        deformed_meshes: Also name the meshes this rig deforms. Independent of the bone page: a
            bone's deform flag says it deforms something, never what. This is the set
            sample_deformed_geometry measures; frame_camera_on_objects frames the members of it
            the render shows and names the rest in its excluded_objects.
        mesh_offset: Where to resume in that list; pass its next_offset while truncated.

    Returns:
        armature_object, and bones with items (name, parent - null for a root - and deform,
        whether the bone deforms a bound mesh), total, offset, limit, truncated, and next_offset
        (null on the last page). With rest_axes, each item also carries rest_axes (nine numbers:
        the bone's own X axis, then Y, then Z, each a unit direction in armature space),
        aim_axis_for_world (one signed bone-axis letter per world direction - "+X" "-X" "+Y"
        "-Y" "+Z" "-Z" - naming the bone's own axis that points most nearly that way at rest,
        measured through the rig's object matrix, null only where the rig's object scale
        collapses the axes), and up_axis, which is that table's "+Z" entry. Choose
        aim_at.track_axis by looking up the world direction the bone should point along and
        passing the letter through unchanged; the reply's own length_axis is the axis along the
        bone, one letter for every bone Blender builds, and is almost never the one that should
        look anywhere. up_axis must name a different axis than track_axis, so where the two
        collide take the roll from another entry of the same table. Items follow armature bone
        order, a parent before its children.

        With custom_properties, each item also carries custom_properties (name, value, and
        min/max where the property defines a slider range), custom_property_count (how many the
        bone holds in total) and custom_property_next_offset (null once the page reaches the
        end). Values are read from the pose bone, so on a library override they are the
        override's, and a bare write to one does not survive a reload - key it instead.

        With deformed_meshes, the reply also carries deformed_meshes: items (object, binding -
        MODIFIER, PARENT or BOTH - and modifier_enabled, false where a hidden Armature modifier
        is why a bound mesh does not move), total, offset, limit, truncated and next_offset.
        Meshes outside the scene are not listed.

    """
    return await call_blender(
        "list_character_bones",
        {
            "armature_object_name": armature_object_name,
            "limit": limit,
            "offset": offset,
            "rest_axes": rest_axes,
            "bone_names": bone_names,
            "custom_properties": custom_properties,
            "property_offset": property_offset,
            "deformed_meshes": deformed_meshes,
            "mesh_offset": mesh_offset,
        },
    )


@mcp.tool()
async def probe_bone_axis(
    ctx: Context,
    armature_object_name: str,
    bone_name: Annotated[str, Field(min_length=1, max_length=63)],
    axes: Annotated[list[_SignedAxis], Field(min_length=1, max_length=6)],
    degrees: Annotated[float, Field(ge=-180.0, le=180.0)] = 15.0,
    space: Literal["LOCAL", "LOCAL_WITH_PARENT", "POSE", "WORLD"] = "LOCAL",
    witness_bone_name: Annotated[str, Field(min_length=1, max_length=63)] | None = None,
    witness_bone_position: Literal["HEAD", "TAIL", "CENTER"] = "TAIL",
    reference_directions: Annotated[dict[str, tuple[float, float, float]], Field(max_length=6)] | None = None,
) -> dict:
    """
    Turn a bone about each named axis and report where that actually carried a witness bone.

    Which local axis swings a limb, and which sign of a roll turns a palm outward, is not
    derivable from ``list_character_bones(rest_axes=True)``: those nine numbers are a rest
    reading of the bone's matrix, they name directions rather than rotations, and they carry no
    witness and therefore no lever arm. Constraints, drivers and IK can also null or invert a
    channel without appearing in them at all. This measures it instead - it applies the turn,
    reads the witness where the rig actually put it, and hands the pose straight back, so the
    rig is exactly as it was found whether the probe succeeds or raises.

    One call answers a whole basis: pass ``axes=["X", "Z", "-Z"]`` rather than probing three
    times. Each axis is turned from the same starting pose, so the entries are comparable - the
    one with the largest ``travel_m`` is the axis that swings this bone, and a length axis
    answers near zero because a roll carries nothing that sits on it.

    Args:
        ctx: MCP request context.
        armature_object_name: An existing object of type ARMATURE with pose_position='POSE'.
        bone_name: The bone to turn. Nothing else on the rig is touched.
        axes: The signed axes to try, one entry per probe, named twice is refused. They mean
            exactly what ``set_character_pose``'s ``rotate.axis`` means in the same ``space``,
            so an answer here transfers to a pose call unchanged.
        degrees: How far to turn the bone for the measurement, signed. Fifteen degrees is large
            enough to stand clear of float noise on a finger bone and small enough that a
            constrained rig does not hit a limit part way through; reverse the sign to see a
            component's sign reverse with it. A turn under half a degree is refused, because
            its travel would not stand clear of float noise.
        space: The space the axis letters are read in. Under LOCAL and LOCAL_WITH_PARENT a
            letter is the bone's own rest axis; under POSE it is the armature's and under WORLD
            the scene's, which is why the same letter answers differently in each.
        witness_bone_name: The bone whose travel is measured. Omitted, the probe picks this
            bone's farthest descendant - the hand at the end of an arm rather than the shoulder
            beside it - and falls back to the probed bone when it has no descendants. The reply
            always names which bone was used and how it was chosen.
        witness_bone_position: Which end of the witness bone is followed.
        reference_directions: World-space directions to decompose the travel against, by
            caller-chosen name, such as ``{"camera_right": [1, 0, 0], "up": [0, 0, 1]}``. Each
            answers a signed number of metres, which is what settles which way round to roll a
            wrist rather than merely how far it moves. An entry that names no direction, or one
            that is not three finite numbers, is refused by name.

    Returns:
        armature_object, bone, space, the degrees applied, bone_length_m for scale, and
        witness_bone with witness_bone_position and witness_bone_source ("explicit",
        "farthest_descendant", or "probed_bone" when the bone carries nothing else). axes then
        carries one entry per probed axis, in the order asked: axis, degrees,
        witness_before_world and witness_after_world (the witness point in world space before
        and after that turn), travel_world (the difference), travel_m (its length), and, where
        directions were named, reference_components_m mapping each name to the signed metres the
        witness moved along it.

    """
    return await call_blender(
        "probe_bone_axis",
        {
            "armature_object_name": armature_object_name,
            "bone_name": bone_name,
            "axes": axes,
            "degrees": degrees,
            "space": space,
            "witness_bone_name": witness_bone_name,
            "witness_bone_position": witness_bone_position,
            "reference_directions": reference_directions,
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
            rotate turns the bone degrees about axis - a signed axis name such as "-Y", or a
            vector - replacing its rotation unless relative=True composes with what is there.
            Its letters name axes of space: under LOCAL and LOCAL_WITH_PARENT that is the
            bone's own rest basis, under POSE the armature's, under WORLD the scene's. It is
            sugar: rotation_axis_angle says the same thing in radians.
            aim_at points the bone's track_axis at target_point (a world point), at target_object_name's
            origin, or at target_bone_name's HEAD, TAIL or CENTER when target_object_name is an armature
            carrying that bone, and leans up_axis toward up_reference (a world direction,
            default +Z) to fix the roll. Both letters name the bone's own axes whatever space
            is: take track_axis from list_character_bones(rest_axes=True)'s aim_axis_for_world
            entry for the world direction the bone should point along, and up_axis from that
            same table's "+Z" entry unless track_axis already took that axis, in which case
            name a different entry. Do not pass length_axis: it is the axis along the bone,
            "Y" on every bone, and an upright bone reports "Y" for up as well, so the pair is
            refused. track_axis has no default. Without up_axis the aim takes the shortest arc
            and keeps the roll the bone already holds, which depends on the pose it started
            from, and a swing past 150 degrees is refused because that roll is then arbitrary.
        detail: Also report each bone's pre-call pose matrix, and report both matrices at
            Blender's own precision instead of rounded to six decimal places.

    Returns:
        armature_object, space, changed_bones naming every posed bone, bones with one record
        per posed bone (bone, the channels the call set - a custom property appears as its data
        path - and after_pose_matrix, the armature-space matrix it ended on), and warnings -
        which say, in particular, when a custom property was written straight onto a library
        override, where it reads back correctly now and is the library's value again after save
        and reopen. A long pose shortens bones to fit the reply budget; changed_bones and
        warnings stay complete.

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


class PoseKeyframe(StrictModel):
    """One frame of a batched pose keying call: the frame, and the bones posed at it."""

    frame: float
    poses: Annotated[list[BonePose], Field(min_length=1, max_length=500)]


# What one batched call may apply across every frame it names. 250 frames of 500 bones would be
# 125,000 bone writes and as many view-layer updates behind one request; the handler refuses the
# same total, because the socket is the boundary an unvalidated caller reaches.
_MAX_BATCHED_POSE_ENTRIES = 2000


def _validate_pose_key_batch(keys: list[PoseKeyframe]) -> None:
    """
    Reject a batch that names a frame twice or carries more poses than one call may apply.

    Args:
        keys: The requested frames.

    Raises:
        ToolError: Naming the repeated frames, or the total and the ceiling it passed.

    """
    frames = [key.frame for key in keys]
    repeated = sorted({value for value in frames if frames.count(value) > 1})
    if repeated:
        raise ToolError(f"keys names the same frame more than once: {repeated}")
    total = sum(len(key.poses) for key in keys)
    if total > _MAX_BATCHED_POSE_ENTRIES:
        raise ToolError(
            f"keys carries {total} pose entries across {len(keys)} frames, more than the "
            f"{_MAX_BATCHED_POSE_ENTRIES} one call may apply; split the frame range across calls"
        )


@mcp.tool()
async def keyframe_character_pose(
    ctx: Context,
    armature_object_name: str,
    action_name: Annotated[str, Field(min_length=1, max_length=63)],
    frame: float | None = None,
    poses: Annotated[list[BonePose], Field(min_length=1, max_length=500)] | None = None,
    keys: Annotated[list[PoseKeyframe], Field(min_length=1, max_length=250)] | None = None,
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

    Key one frame with frame and poses, or a whole stride in one call with keys - exactly one of
    the two forms, never both. A batched call is not a loop over the single-frame one only in
    round trips: the frames are validated together before the first key is written, the action
    is assigned once, and the playhead is placed on each frame in ascending order before that
    frame's poses are resolved, so an aim_at reads its target where the target is at that frame
    and an absolute-space pose is built on the root motion the action already holds there.

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
    same call at two frames would key two different rolls. A world-space aim is evaluated at the
    frame being keyed, not at the frame the playhead happened to be on: target_object_name and
    target_bone_name are read where they are at that frame, so aiming at an animated character keys
    a look that follows it. A keyed aim is also re-spelled to interpolate the short way from the
    previous key in this action: the quaternion sign is flipped when it would take the long
    route, and an Euler triple is made compatible with the previous key.

    Args:
        ctx: MCP request context.
        frame: The single-frame form's frame; fractional frames are keyed as subframes. Given
            together with poses, and never alongside keys.
        poses: The bones to key at frame; see set_character_pose for the channels.
        keys: The batched form: one entry per frame, each with its own frame and poses, up to
            2000 pose entries in total. Frames must be unique and may arrive in any order; they
            are keyed ascending. A thirteen-key stride is one call, not thirteen.
        detail: Also report, as "bones", the pose each bone was keyed at - its pre-call and
            keyed armature-space matrices, at Blender's own precision. Under keys each record
            also names its frame.

    Returns:
        armature_object, action, action_slot, assigned_action (the action now driving the rig),
        unassigned_action when a different action was displaced, keying_policy, changed_bones
        naming every bone any frame posed (once, complete), changed_keys with one entry per keyed
        channel (bone, data_path, frame), interpolation_updates, keyed_frames (ascending) and
        warnings. A long call shortens changed_keys to fit the reply budget; changed_bones,
        keyed_frames and warnings stay complete.

    Raises:
        ToolError: If neither or both call forms are supplied, if keys repeats a frame, or if it
            carries more pose entries than one call may apply.

    """
    if keys is None:
        if frame is None or poses is None:
            raise ToolError(
                "keyframe_character_pose takes either frame with poses (one frame) or keys (several frames, "
                "each with its own poses); neither form was supplied in full"
            )
    elif frame is not None or poses is not None:
        raise ToolError(
            "keyframe_character_pose takes either frame with poses (one frame) or keys (several frames, each "
            "with its own poses), not both"
        )
    else:
        _validate_pose_key_batch(keys)
    return await call_blender(
        "keyframe_character_pose",
        {
            "armature_object_name": armature_object_name,
            "action_name": action_name,
            "frame": frame,
            "poses": None
            if poses is None
            else [pose.model_dump(exclude_none=True, exclude_unset=True) for pose in poses],
            "keys": None
            if keys is None
            else [
                {
                    "frame": key.frame,
                    "poses": [pose.model_dump(exclude_none=True, exclude_unset=True) for pose in key.poses],
                }
                for key in keys
            ],
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


class ReachHinge(StrictModel):
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


class _ReachChain(StrictModel):
    """
    The chain, pole and solver settings every reach carries, whatever it does with them.

    solve_bone_reach poses one chain now and keyframe_bone_reach solves the same chain at many
    frames; everything except where the tail has to be is identical between them, the pole rule
    included. Two copies of it had to agree by hand.
    """

    tip_bone: str = Field(min_length=1, max_length=63)
    chain_length: Annotated[int, Field(ge=1, le=32)] | None = None
    pole_target_point: tuple[float, float, float] | None = None
    pole_target_object_name: Annotated[str, Field(min_length=1, max_length=63)] | None = None
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
        if self.pole_target_point is not None and self.pole_target_object_name is not None:
            raise ValueError("Supply at most one of pole_target_point or pole_target_object_name")
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

    target_point: tuple[float, float, float] | None = None
    target_object_name: Annotated[str, Field(min_length=1, max_length=63)] | None = None

    @model_validator(mode="after")
    def validate_target(self) -> "BoneReach":
        """
        Reject a reach that names no target, or two.

        Returns:
            BoneReach: This model, unchanged.

        Raises:
            ValueError: If neither or both target forms are given.

        """
        if (self.target_point is None) == (self.target_object_name is None):
            raise ValueError("Supply exactly one of target_point or target_object_name")
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

    pole_target_point/pole_target_object_name omitted: synthesized from the chain's REST pose - the
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
            "reaches": dump_inputs(reaches),
            "tolerance_m": tolerance_m,
            "detail": detail,
        },
        changed_objects=[armature_object_name],
    )


class ReachKey(StrictModel):
    """One frame of one reach: where the tip bone's tail must be at that frame."""

    frame: float
    target_point: tuple[float, float, float] | None = None
    target_object_name: Annotated[str, Field(min_length=1, max_length=63)] | None = None

    @model_validator(mode="after")
    def validate_key(self) -> "ReachKey":
        """
        Reject a key that names no target or two targets.

        Returns:
            ReachKey: This model, unchanged.

        Raises:
            ValueError: If neither or both target forms are given.

        """
        if (self.target_point is None) == (self.target_object_name is None):
            raise ValueError("Supply exactly one of target_point or target_object_name")
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
            "reaches": dump_inputs(reaches),
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


@mcp.tool()
async def sample_deformed_geometry(
    ctx: Context,
    mesh_object_name: Annotated[str, Field(min_length=1, max_length=63)],
    frame: int | None = None,
    space: Literal["WORLD", "LOCAL"] = "WORLD",
    vertex_indices: Annotated[list[int], Field(min_length=1, max_length=1000)] | None = None,
    vertex_limit: Annotated[int, Field(ge=1, le=1000)] = 50,
    vertex_offset: Annotated[int, Field(ge=0, le=99_999_999)] = 0,
) -> dict:
    """
    Read a mesh's deformed surface - the shape the armature, shape keys and drivers produce.

    The only evaluated readback in the rigging surface, and so the only way to prove a pose
    moved anything: get_mesh_data and get_skinning_info both describe the base mesh at rest and
    answer identically before and after a pose. Use it after set_character_pose,
    keyframe_bone_reach, transfer_skin_weights or a shape-key change.

    Indices are the EVALUATED mesh's own. They are base-mesh indices only while the reply's
    index_correspondence is BASE_MESH; a Subdivision, Mirror or Array renumbers the result and
    displacement then cannot be measured per vertex. Hide that modifier in the viewport to
    compare vertex for vertex, or read inspect_evaluated_geometry's bounds and counts.

    Args:
        ctx: MCP request context.
        mesh_object_name: An existing MESH object - the deformed mesh, not the armature.
        frame: Evaluate here, putting the playhead back afterwards. Omit for the current frame.
        space: WORLD transforms positions and normals by the evaluated object's matrix; LOCAL
            returns that object's own coordinates.
        vertex_indices: Exact evaluated vertices, in this order, instead of paging the mesh.
        vertex_limit: Vertices per page, small by default: each record carries a position and
            a normal against the reply's byte budget.
        vertex_offset: First vertex of the page.

    Returns:
        object, frame, evaluated_deformation_included (always true - get_skinning_info's is
        always false), coordinate_space, evaluated_counts, base_counts, index_correspondence,
        world_bounds, displacement (under BASE_MESH only: maximum_m, mean_m, moved_vertices
        over an evenly spread bounded sample, with complete saying whether it covered the whole
        mesh), and a vertices page of {index, co, normal, displacement_m}.

    """
    return await call_blender(
        "sample_deformed_geometry",
        {
            "mesh_object_name": mesh_object_name,
            "frame": frame,
            "space": space,
            "vertex_indices": vertex_indices,
            "vertex_limit": vertex_limit,
            "vertex_offset": vertex_offset,
        },
    )
