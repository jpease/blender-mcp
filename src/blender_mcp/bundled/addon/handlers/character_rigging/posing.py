"""
Blender handlers for deterministic pose application and keyframing.

Two rules shape this module. A bone's target is resolved inside the depth-sorted write loop,
not before it, because a pose-, world- or rest-space target for a child is only meaningful
against a parent this same call has already written. And a call that authors animation leaves
the action it authored assigned to the rig: an action nobody references carries zero users and
Blender drops it at save, so restoring a previous assignment would throw the work away - which
is why the restore is conditional on the call having raised, and why it is stated once, in the
`restored_*` context managers below, rather than in a `finally` block per entry point.
"""

import contextlib
import math
import uuid

from typing import NamedTuple

import bpy
import mathutils

from ...helpers import paginate, sync_from_editmode
from ..action_assignment import action_fcurve_collections, assign_named_action
from ..key_style import KeyStyle, style_point
from .foundation import (
    _MAX_BONE_PAGE,
    _armature_object,
    _bone_path_token,
    _finite,
    _matrix_list,
    _override_property_warning,
    _required_name,
    _selected_bones,
    _validate_limit_offset,
    _vector,
    _vector_tuple,
)

_POSE_SPACES = {"LOCAL", "LOCAL_WITH_PARENT", "POSE", "WORLD"}
# LOCAL is the only pose space that is relative to the parent: it is `pose_bone.matrix_basis`,
# the channel delta from rest. Every other space states an absolute placement in the armature
# (or the scene), so a child's target has to be built after its parent has moved.
_PARENT_RELATIVE_SPACE = "LOCAL"

_AXIS_INDEX = {"X": 0, "Y": 1, "Z": 2}
_SIGNED_AXIS_SIGNS = {"X": 1.0, "-X": -1.0, "Y": 1.0, "-Y": -1.0, "Z": 1.0, "-Z": -1.0}
# The six world directions an aim can want a bone axis to point along, in the spelling
# `list_character_bones(rest_axes=True)` keys `aim_axis_for_world` by. Signed, because "which
# way along X" is half the question a caller is asking.
_WORLD_DIRECTIONS = {
    "+X": (1.0, 0.0, 0.0),
    "-X": (-1.0, 0.0, 0.0),
    "+Y": (0.0, 1.0, 0.0),
    "-Y": (0.0, -1.0, 0.0),
    "+Z": (0.0, 0.0, 1.0),
    "-Z": (0.0, 0.0, -1.0),
}
# Where on a target bone an aim may point.
_BONE_POSITIONS = ("HEAD", "TAIL", "CENTER")
# Which two axes cross into the third, so a basis built from two chosen axes stays right-handed
# (determinant +1) and the bone is oriented rather than mirrored.
_RIGHT_HANDED = {"X": ("Y", "Z"), "Y": ("Z", "X"), "Z": ("X", "Y")}
_DEFAULT_UP_REFERENCE = (0.0, 0.0, 1.0)
# Bones on these rigs are 5-32 mm long, so a target within a micrometre of the head names no
# direction at all; below this the normalisation is noise.
_AIM_MIN_DISTANCE = 1e-6
_AIM_MIN_LENGTH = 1e-9
# What is left of the up reference after the aim direction is projected out of it. Under this
# the two are parallel and the roll about the aim is undefined rather than merely ill-conditioned.
_AIM_MIN_RESIDUAL = 1e-4
# A minimal-arc aim keeps whatever roll the bone already holds, and past roughly this swing the
# roll it keeps is arbitrary: a 180-degree aim of an upright head lands it upside down. Refuse
# instead, and say which fields make the result well defined.
_AIM_MAX_MINIMAL_ARC = 150.0
_SINGULAR_TOLERANCE = 1e-12
# Rotation channels by the number of F-Curves they occupy, used to read a previous key back.
_ROTATION_CHANNEL_WIDTH = {"rotation_quaternion": 4, "rotation_axis_angle": 4, "rotation_euler": 3}
# Frames compare as floats; a key at 12 and a request at 12.0000001 are the same key.
_FRAME_TOLERANCE = 1e-6
# One call may pose 500 bones, and the envelope lifts warnings whole rather than paging them,
# so the per-bone cycle notices are named up to this many and then counted.
_MAX_CYCLE_WARNINGS = 4


def _signed_axis(name, label):
    """
    Split an axis name such as `-Z` into the axis it names and its sign.

    Args:
        name: One of `X`, `-X`, `Y`, `-Y`, `Z`, `-Z`.
        label: What to call the field in an error message.

    Returns:
        tuple: The bare axis letter, and `1.0` or `-1.0`.

    Raises:
        ValueError: If `name` does not spell one of the six signed axes.

    """
    sign = _SIGNED_AXIS_SIGNS.get(name) if isinstance(name, str) else None
    if sign is None:
        raise ValueError(f"{label} must be one of X, -X, Y, -Y, Z, -Z; got {name!r}")
    return name.lstrip("-"), sign


def _alignment(column, direction):
    """
    Cosine between one unit axis and one unit direction.

    Every axis-naming answer in this module is this number maximised over some set: which bone
    axis stands up at rest, which bone axis points along a world direction, which world
    direction a bone axis holds. Computing it once is what keeps those answers consistent -
    `up_axis` is `aim_axis_for_world["+Z"]` by construction rather than by coincidence.

    Args:
        column: A unit vector.
        direction: A unit direction, as a 3-sequence.

    Returns:
        float: Their dot product.

    """
    return sum(column[axis] * value for axis, value in enumerate(direction))


def _rest_axis_units(armature, bone):
    """
    Each of one bone's own rest axes as a unit direction in world space.

    The rig's object matrix is part of the answer: `bone.matrix_local` is armature-space, and
    everything an aim is stated against - `up_reference`, a target point - is world-space. The
    columns are normalised so a rig scaled unevenly cannot tip a comparison towards whichever
    axis the object matrix happens to have stretched.

    Args:
        armature: The armature object the bone belongs to.
        bone: A rest bone, from `armature.data.bones` or `pose_bone.bone`.

    Returns:
        dict: `{axis letter: unit vector}`, missing any axis the object transform collapses to
        nothing, and empty when it collapses all three.

    """
    rest = armature.matrix_world.to_3x3() @ bone.matrix_local.to_3x3()
    units = {}
    for letter, index in _AXIS_INDEX.items():
        column = rest.col[index]
        if column.length > _AIM_MIN_LENGTH:
            units[letter] = column.normalized()
    return units


def _nearest_rest_axis(units, direction):
    """
    Name the signed bone axis pointing most nearly along one world direction.

    Args:
        units: `_rest_axis_units` output for the bone.
        direction: A unit world direction.

    Returns:
        str | None: A signed axis name such as `-X`, or None when no axis points anywhere.

    """
    if not units:
        return None
    alignments = {letter: _alignment(column, direction) for letter, column in units.items()}
    letter = max(alignments, key=lambda name: abs(alignments[name]))
    return letter if alignments[letter] >= 0.0 else f"-{letter}"


def _nearest_world_direction(column):
    """
    Name the world direction one unit vector points most nearly along.

    Args:
        column: A unit vector in world space.

    Returns:
        str: One of `_WORLD_DIRECTIONS`' keys.

    """
    return max(_WORLD_DIRECTIONS, key=lambda name: _alignment(column, _WORLD_DIRECTIONS[name]))


def _rest_aim_axes(armature, bone):
    """
    Name, for each world direction, the bone axis that already points that way at rest.

    This is the table an aim is chosen from: `track_axis` is the entry for the direction the
    caller wants the bone to point, and `up_axis` is `+Z`'s entry. Deriving it is not something
    the reported nine rest numbers permit - they are armature-space, and an aim is stated in the
    scene - so a rig laid over in the scene is exactly where a caller's own derivation parts
    company with the aim it then makes.

    Args:
        armature: The armature object the bone belongs to.
        bone: A rest bone from `armature.data.bones`.

    Returns:
        dict: `{world direction: signed bone axis}`, each value None where the rig's object
        transform collapses the bone's axes and no axis points anywhere.

    """
    units = _rest_axis_units(armature, bone)
    return {name: _nearest_rest_axis(units, direction) for name, direction in _WORLD_DIRECTIONS.items()}


def _free_axis_advice(armature, pose_bone, track_letter):
    """
    Say which of the bone's axes are still free for `up_axis`, and where each points at rest.

    The refusal this completes is raised by the call the tools themselves used to prescribe:
    `length_axis` is `Y` on every bone Blender builds and an upright bone's `up_axis` is `Y`
    too, so passing both straight through named one axis twice. Stating the remaining two, with
    the world direction each holds, turns the rule into the remedy.

    Args:
        armature: The armature the bone belongs to, read for its object matrix.
        pose_bone: The bone being aimed.
        track_letter: The bare axis letter the aim tracks, which `up_axis` may not repeat.

    Returns:
        str: One sentence naming the two free axes and, where the rig's transform leaves them a
        direction, the world direction each points along at rest.

    """
    units = _rest_axis_units(armature, pose_bone.bone)
    described = [
        f"{letter} points {_nearest_world_direction(units[letter])}" if letter in units else letter
        for letter in "XYZ"
        if letter != track_letter
    ]
    return (
        f"The bone's other axes at rest: {' and '.join(described)} - name one of those, signed, as up_axis, "
        "or track an axis that is not the one that should stay upright."
    )


def _aim_target_bone(target_object, aim, bone_name):
    """
    Resolve `aim_at.target_bone_name` to a pose bone on the object the aim names.

    Aiming at an object aims at its origin, which on a character rig is the floor under it;
    "look at that character" means a bone on it, and which point on that bone is the caller's
    to choose.

    Args:
        target_object: The object `aim_at.target_object_name` named, or None when the aim names a
            world point instead.
        aim: The raw `aim_at` record.
        bone_name: The bone being aimed, for error messages.

    Returns:
        tuple: The target pose bone, or None when none was named, and the position on it.

    Raises:
        ValueError: If a bone is named without an object, on an object that is not an armature,
            or that the armature does not carry; or if a position is named without a bone or is
            not one of `_BONE_POSITIONS`.

    """
    name = aim.get("target_bone_name")
    position = aim.get("target_bone_position")
    if name is None:
        if position is not None:
            raise ValueError(
                f"aim_at.target_bone_position for '{bone_name}' names a point on a bone, and this aim names no "
                "target_bone; target_object on its own aims at the object's own origin"
            )
        return None, "HEAD"
    required = _required_name(name, "aim_at.target_bone_name")
    if target_object is None:
        raise ValueError(
            f"aim_at.target_bone_name '{required}' for '{bone_name}' requires aim_at.target_object_name to name the "
            "armature carrying it"
        )
    position = "HEAD" if position is None else position
    if position not in _BONE_POSITIONS:
        raise ValueError(
            f"aim_at.target_bone_position for '{bone_name}' must be one of {', '.join(_BONE_POSITIONS)}; "
            f"got {position!r}"
        )
    if target_object.type != "ARMATURE":
        raise ValueError(
            f"aim_at.target_bone_name '{required}' needs an armature to live on, and aim_at.target_object_name "
            f"'{target_object.name}' is type={target_object.type}"
        )
    target_bone = target_object.pose.bones.get(required)
    if target_bone is None:
        raise ValueError(f"aim_at.target_bone_name not found on '{target_object.name}': {required}")
    return target_bone, position


def _validated_aim(armature, pose_bone, aim):
    """
    Check one `aim_at` record and resolve it to the values the write loop needs.

    `track_axis` and `up_axis` name the bone's own axes - the letters
    `list_character_bones(rest_axes=True)` reports in `aim_axis_for_world`, whose directions it
    gives in armature space - and never scene axes. `target` and `up_reference` are the
    world-space side of the record.

    The target object and bone are looked up now so a missing one fails before any bone moves,
    but where they are is read at apply time, after the pose the same call authored has settled
    and with the playhead on the frame being keyed.

    Args:
        armature: The armature being posed, read for the rest axes a refusal has to name.
        pose_bone: The bone the aim applies to.
        aim: The raw `aim_at` record from a pose entry.

    Returns:
        dict: `target_point` (a world-space vector or None), `target_object` (an object or None),
        `target_bone` (a pose bone on that object, or None) with `target_bone_position`,
        `track` from `_signed_axis`, and `up` as `(axis letter, sign, world reference)` or None
        for a minimal-arc aim.

    Raises:
        ValueError: If the record names neither or both target forms, names an object or bone
            that does not exist, asks for a point on a bone it does not name, spells an axis
            wrongly, points `up_axis` at the tracked axis, or gives a zero up reference.

    """
    bone_name = pose_bone.name
    if not isinstance(aim, dict):
        raise ValueError(f"aim_at for '{bone_name}' must be an object")
    target = aim.get("target_point")
    target_name = aim.get("target_object_name")
    if (target is None) == (target_name is None):
        raise ValueError(f"aim_at for '{bone_name}' requires exactly one of target_point or target_object_name")
    target_object = None
    point = None
    if target_name is not None:
        target_object = bpy.data.objects.get(_required_name(target_name, "aim_at.target_object_name"))
        if target_object is None:
            raise ValueError(f"aim_at.target_object_name not found: {target_name}")
    else:
        point = _vector(target, f"aim_at.target_point for '{bone_name}'")
    target_bone, position = _aim_target_bone(target_object, aim, bone_name)
    track_letter, track_sign = _signed_axis(aim.get("track_axis"), f"aim_at.track_axis for '{bone_name}'")
    up = None
    if aim.get("up_axis") is not None:
        up_letter, up_sign = _signed_axis(aim["up_axis"], f"aim_at.up_axis for '{bone_name}'")
        if up_letter == track_letter:
            raise ValueError(
                f"aim_at.up_axis for '{bone_name}' must name a different bone axis than track_axis; "
                f"'{aim['up_axis']}' and '{aim['track_axis']}' are both the {up_letter} axis. "
                f"{_free_axis_advice(armature, pose_bone, track_letter)}"
            )
        reference = _vector(aim.get("up_reference") or _DEFAULT_UP_REFERENCE, f"aim_at.up_reference for '{bone_name}'")
        if reference.length <= _AIM_MIN_LENGTH:
            raise ValueError(f"aim_at.up_reference for '{bone_name}' is a zero vector; roll is undefined")
        up = (up_letter, up_sign, reference)
    return {
        "target_point": point,
        "target_object": target_object,
        "target_bone": target_bone,
        "target_bone_position": position,
        "track": (track_letter, track_sign),
        "up": up,
    }


def _validated_rotate(rotate, bone_name):
    """
    Check one `rotate` record and resolve its axis and angle.

    Unlike `aim_at`, a named axis here is an axis of the call's pose space, not of the bone:
    under `LOCAL` and `LOCAL_WITH_PARENT` that space is the bone's own rest basis, so `Z` is the
    bone's own rest Z as `list_character_bones(rest_axes=True)` reports it; under `POSE` it is
    the armature's Z and under `WORLD` the scene's.

    Args:
        rotate: The raw `rotate` record from a pose entry.
        bone_name: The bone it applies to, for error messages.

    Returns:
        dict: `axis` as a unit vector in the call's pose space, `angle` in radians, and
        `relative`.

    Raises:
        ValueError: If the axis is neither a signed axis name nor a non-zero vector, or if
            `degrees` is missing or not finite.

    """
    if not isinstance(rotate, dict):
        raise ValueError(f"rotate for '{bone_name}' must be an object")
    axis = rotate.get("axis")
    if isinstance(axis, str):
        letter, sign = _signed_axis(axis, f"rotate.axis for '{bone_name}'")
        vector = mathutils.Vector((0.0, 0.0, 0.0))
        vector[_AXIS_INDEX[letter]] = sign
    else:
        vector = _vector(axis, f"rotate.axis for '{bone_name}'")
        if vector.length <= _AIM_MIN_LENGTH:
            raise ValueError(f"rotate.axis for '{bone_name}' must be a non-zero vector")
        vector.normalize()
    if "degrees" not in rotate:
        raise ValueError(f"rotate for '{bone_name}' requires degrees")
    degrees = _finite(rotate["degrees"], f"rotate.degrees for '{bone_name}'")
    return {"axis": vector, "angle": math.radians(degrees), "relative": bool(rotate.get("relative", False))}


def _pose_matrix_from_channels(armature, pose_bone, spec, space):
    current = armature.convert_space(pose_bone=pose_bone, matrix=pose_bone.matrix, from_space="POSE", to_space=space)
    location, rotation, scale = current.decompose()
    if "location" in spec:
        location = mathutils.Vector(spec["location"])
    if "rotation_quaternion" in spec:
        values = spec["rotation_quaternion"]
        rotation = mathutils.Quaternion(values).normalized()
    elif "rotation_euler" in spec:
        rotation = mathutils.Euler(spec["rotation_euler"], "XYZ").to_quaternion()
    elif "rotation_axis_angle" in spec:
        angle, x, y, z = spec["rotation_axis_angle"]
        rotation = mathutils.Quaternion((x, y, z), angle)
    elif "rotate" in spec:
        record = spec["rotate"]
        delta = mathutils.Quaternion(record["axis"], record["angle"])
        # The shipped rotation channels are set-to; `relative` is the compose-with form, so
        # "a bit more bend" does not have to be worked out by the caller.
        rotation = (delta @ mathutils.Quaternion(rotation)) if record["relative"] else delta
    if "scale" in spec:
        scale = mathutils.Vector(spec["scale"])
    return mathutils.Matrix.LocRotScale(tuple(location), rotation, tuple(scale))


def _aim_target_point(aim):
    """
    Read where in the world this aim points, at the moment the bone is posed.

    A bone target is read off the target rig's evaluated pose - `PoseBone.head`/`tail` are
    armature-space and follow whatever is driving that rig - so an aim keyed at frame 30 looks
    at where that bone is at frame 30, not at where it rests. A plain object target is its
    origin, which on a character rig is usually the floor under it.

    Args:
        aim: A record from `_validated_aim`.

    Returns:
        mathutils.Vector | tuple: The world-space point to aim at.

    """
    bone = aim["target_bone"]
    if bone is not None:
        position = aim["target_bone_position"]
        if position == "HEAD":
            local = bone.head
        elif position == "TAIL":
            local = bone.tail
        else:
            local = (bone.head + bone.tail) * 0.5
        return aim["target_object"].matrix_world @ mathutils.Vector(local)
    if aim["target_object"] is not None:
        return aim["target_object"].matrix_world.translation
    return aim["target_point"]


def _aim_pose_matrix(armature, pose_bone, aim):
    """
    Build the armature-space matrix that points one bone axis at a world target.

    Only the orientation is replaced: the bone keeps the head position and scale it already
    evaluates to, so the resolved pose reduces to the bone's own rotation channel.

    Args:
        armature: The armature object the bone belongs to.
        pose_bone: The bone to aim, read after its parents have been written this call.
        aim: A record from `_validated_aim`.

    Returns:
        mathutils.Matrix: The bone's new POSE-space matrix.

    Raises:
        ValueError: If the target sits on the bone head, if the up reference is zero in armature
            space or parallel to the aim direction, or if a minimal-arc aim would swing past
            `_AIM_MAX_MINIMAL_ARC` degrees, where the roll it preserves is arbitrary.

    """
    world_to_pose = armature.matrix_world.inverted()
    current = pose_bone.matrix.copy()
    head = current.translation.copy()
    target = _aim_target_point(aim)
    direction = (world_to_pose @ mathutils.Vector(target)) - head
    distance = direction.length
    if distance <= _AIM_MIN_DISTANCE:
        raise ValueError(
            f"aim_at target is at the head of '{pose_bone.name}' (distance {distance:.3e} m); no direction is defined"
        )
    direction.normalize()
    # Unit axes: the scale is carried separately so it survives the aim untouched.
    basis = current.to_3x3().normalized()
    track_letter, track_sign = aim["track"]
    if aim["up"] is None:
        axis = (basis.col[_AXIS_INDEX[track_letter]] * track_sign).normalized()
        swing = math.degrees(axis.angle(direction, 0.0))
        if swing > _AIM_MAX_MINIMAL_ARC:
            raise ValueError(
                f"aim_at for '{pose_bone.name}' swings {swing:.1f} degrees without an up reference, and the "
                "roll a minimal-arc aim keeps is arbitrary at that angle; supply up_axis and up_reference"
            )
        rotated = (axis.rotation_difference(direction).to_matrix() @ basis).normalized()
    else:
        rotated = _aim_basis_with_up(direction, aim, world_to_pose, pose_bone.name)
    return mathutils.Matrix.LocRotScale(head, rotated.to_quaternion(), current.to_scale())


def _aim_basis_with_up(direction, aim, world_to_pose, bone_name):
    """
    Build the bone's new orientation from the aim direction and an up reference.

    Args:
        direction: The unit aim direction in armature space.
        aim: A record from `_validated_aim` whose `up` is set.
        world_to_pose: The inverse of the rig's object matrix.
        bone_name: The bone being aimed, for error messages.

    Returns:
        mathutils.Matrix: A right-handed 3x3 whose tracked axis is the aim direction and whose
        up axis leans as far toward the reference as perpendicularity allows.

    Raises:
        ValueError: If the up reference is zero in armature space or parallel to the aim.

    """
    track_letter, track_sign = aim["track"]
    up_letter, up_sign, up_world = aim["up"]
    up_pose = world_to_pose.to_3x3() @ up_world
    if up_pose.length <= _AIM_MIN_LENGTH:
        raise ValueError(f"aim_at.up_reference for '{bone_name}' is a zero vector in armature space")
    up_pose.normalize()
    # Gram-Schmidt: the up axis leans toward the reference in the plane perpendicular to the aim.
    residual = up_pose - direction * up_pose.dot(direction)
    if residual.length <= _AIM_MIN_RESIDUAL:
        raise ValueError(
            f"aim_at.up_reference is parallel to the aim direction for '{bone_name}' "
            f"(residual {residual.length:.3e}); roll is undefined - choose a different up_reference"
        )
    columns = {track_letter: direction * track_sign, up_letter: residual.normalized() * up_sign}
    third = next(name for name in "XYZ" if name not in columns)
    first, second = _RIGHT_HANDED[third]
    columns[third] = columns[first].cross(columns[second])
    return mathutils.Matrix(tuple(tuple(columns[name][row] for name in "XYZ") for row in range(3)))


def _reject_singular(pose_bone, matrix):
    if abs(matrix.determinant()) <= _SINGULAR_TOLERANCE:
        raise ValueError(f"Pose matrix for '{pose_bone.name}' is singular")


def _posable_armature(armature_object_name, purpose):
    """
    Resolve the named armature, refusing one that is showing its rest pose.

    Args:
        armature_object_name: The object to resolve.
        purpose: What the caller is about to do, for the refusal to name.

    Returns:
        bpy.types.Object: The armature object.

    Raises:
        ValueError: If the object is not an armature, or `pose_position` is not 'POSE' - a
            rig on REST ignores every channel the call is about to write, so it would report
            a pose nobody can see.

    """
    armature = _armature_object(armature_object_name)
    if armature.data.pose_position != "POSE":
        raise ValueError(f"Armature must use pose_position='POSE' to {purpose}")
    return armature


def _validate_pose_specs(armature, poses, space):
    """
    Check every pose entry and pre-build the targets that do not depend on other bones.

    A `matrix` entry is already absolute and a `LOCAL` entry is the bone's own channel delta, so
    both are built here and validated before anything is written. Every other space, and every
    aim, is left as None and resolved in `_apply_pose_specs` against the freshly written parent.

    Args:
        armature: The armature object being posed.
        poses: The raw pose entries.
        space: The pose space the entries are expressed in.

    Returns:
        list: `(pose_bone, spec, matrix or None)` triples, where `spec` carries the validated
        `aim_at` and `rotate` records in place of the raw ones.

    Raises:
        ValueError: For an unsupported space, an empty or duplicated pose list, an unknown bone
            or custom property, a malformed matrix, aim or rotate record, or a singular target.

    """
    if space not in _POSE_SPACES:
        raise ValueError(f"Unsupported pose space: {space}")
    if not poses:
        raise ValueError("At least one pose entry is required")
    names = [item.get("bone_name") for item in poses]
    if len(names) != len(set(names)):
        raise ValueError("Each pose bone may appear only once")
    prepared = []
    for entry in poses:
        pose_bone = armature.pose.bones.get(entry.get("bone_name"))
        if pose_bone is None:
            raise ValueError(f"Pose bone not found: {entry.get('bone_name')}")
        custom = entry.get("custom_properties", {})
        missing = sorted(name for name in custom if name not in pose_bone)
        if missing:
            raise ValueError(f"Custom properties not found on '{pose_bone.name}': {missing}")
        spec = dict(entry)
        if "aim_at" in spec:
            spec["aim_at"] = _validated_aim(armature, pose_bone, spec["aim_at"])
        if "rotate" in spec:
            spec["rotate"] = _validated_rotate(spec["rotate"], pose_bone.name)
        matrix_values = spec.get("matrix")
        if matrix_values is not None:
            if len(matrix_values) != 4 or any(len(row) != 4 for row in matrix_values):
                raise ValueError(f"matrix for '{pose_bone.name}' must be 4x4")
            matrix = mathutils.Matrix(
                tuple(tuple(_finite(value, f"{pose_bone.name}.matrix") for value in row) for row in matrix_values)
            )
            _reject_singular(pose_bone, matrix)
        elif space == _PARENT_RELATIVE_SPACE and "aim_at" not in spec:
            matrix = _pose_matrix_from_channels(armature, pose_bone, spec, space)
            _reject_singular(pose_bone, matrix)
        else:
            matrix = None
        prepared.append((pose_bone, spec, matrix))
    return prepared


# Blender stores a pose matrix as float32, so the seventeen digits a full decimal expansion
# prints are encoding noise - and those digits are most of what a per-bone matrix costs.
_POSE_MATRIX_DECIMALS = 6
# Three decimals hold a unit axis to about 0.06 degrees, far finer than any axis an agent picks
# off the table, and they halve what full float precision would add to the page (measured on a
# 187-bone rig: +33,840 wire bytes at 3 dp).
_REST_AXIS_DECIMALS = 3
# Blender builds every bone with its own +Y running head to tail, whatever the rig's naming or
# roll, so which axis is the bone's length is a fact about the format and not about the bone.
# The reply states it once per page rather than on every row: repeating it per bone measured
# 28 wire bytes a row, which is 5,236 bytes and a whole page of a 187-bone walk (22 bones a
# page rather than 20), to say the same letter 187 times.
# `tests/blender_rest_axis_letters_smoke.py` checks it holds for every bone of a real one.
_LENGTH_AXIS = "Y"
# The channels a pose entry can set, in the order a record names them.
_POSE_CHANNELS = (
    "matrix",
    "location",
    "rotation_euler",
    "rotation_quaternion",
    "rotation_axis_angle",
    "rotate",
    "aim_at",
    "scale",
)
# Everything that ends up writing the bone's rotation channel, whether the caller spelled the
# rotation or asked for one to be worked out.
_ROTATION_CHANNELS = ("rotation_euler", "rotation_quaternion", "rotation_axis_angle", "rotate", "aim_at")


def _rounded_matrix_list(matrix):
    return [[round(float(value), _POSE_MATRIX_DECIMALS) for value in row] for row in matrix]


def _rest_axes(bone):
    """
    Flatten a bone's rest orientation into the nine numbers an axis choice needs.

    Args:
        bone: A rest bone from `armature.data.bones`.

    Returns:
        list: The X, then Y, then Z columns of `matrix_local` - each of the bone's own rest axes
        as a unit direction in armature space - rounded to `_REST_AXIS_DECIMALS`.

    """
    matrix = bone.matrix_local.to_3x3()
    return [round(float(value), _REST_AXIS_DECIMALS) for index in range(3) for value in matrix.col[index]]


def _changed_channels(spec):
    """
    Name what one pose entry changed, so its record identifies the change without the request.

    Args:
        spec: One validated pose entry.

    Returns:
        list: Its transform channels in `_POSE_CHANNELS` order, then every custom property it
        set, each spelled as the data path that keys it.

    """
    channels = [name for name in _POSE_CHANNELS if name in spec]
    channels.extend(f'["{name}"]' for name in sorted(spec.get("custom_properties", {})))
    return channels


# The three unit axes, as plain tuples: a `mathutils.Vector` is built from one rather than
# zeroed and indexed into, because that keeps this free of in-place mutation.
_BASIS_VECTORS = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
# How far off the bone's length axis a rotation may sit and still be reported as a twist.
# Two degrees: an agent reading an axis off `list_character_bones` gets an exact basis vector,
# so anything this close was meant to be that axis.
_LENGTH_AXIS_TOLERANCE_DEGREES = 2.0
# Below this the rotation is too small for "it did not move" to mean anything - a zero
# rotation is a no-op, not a roll, and re-applying a pose must not accuse the caller.
_INERT_ROTATION_MINIMUM_DEGREES = 0.5
# The spaces whose axis letters resolve to the bone's own rest basis, and so the only two in
# which "Y" names this bone's length rather than the armature's or the scene's axis.
_BONE_LOCAL_SPACES = frozenset({"LOCAL", "LOCAL_WITH_PARENT"})


def _unit_axis_and_angle(axis, angle):
    """Normalize a rotation's axis, or report that it names no direction at all."""
    return (axis.normalized(), angle) if axis.length > _AIM_MIN_LENGTH else None


def _rotation_axis_angle(spec):
    """
    Read one pose entry's rotation back as a single axis and angle in the call's space.

    Only the representations that name one rotation outright are readable this way. A Euler
    triple with two or more non-zero components is a composition, not an axis, and an `aim_at`
    or an explicit `matrix` is stated against the scene rather than the bone - all three return
    None rather than a guess.

    Args:
        spec: One validated pose entry.

    Returns:
        tuple | None: `(axis, radians)` with axis a unit `mathutils.Vector` in the call's pose
        space, or None when this entry states no single-axis rotation.

    """
    if "rotate" in spec:
        record = spec["rotate"]
        return mathutils.Vector(record["axis"]), record["angle"]
    if "rotation_axis_angle" in spec:
        angle, x, y, z = spec["rotation_axis_angle"]
        return _unit_axis_and_angle(mathutils.Vector((x, y, z)), angle)
    if "rotation_quaternion" in spec:
        w, x, y, z = tuple(mathutils.Quaternion(spec["rotation_quaternion"]).normalized())
        return _unit_axis_and_angle(mathutils.Vector((x, y, z)), 2.0 * math.acos(max(-1.0, min(1.0, float(w)))))
    if "rotation_euler" in spec:
        turning = [(index, value) for index, value in enumerate(spec["rotation_euler"]) if value]
        if len(turning) == 1:
            index, value = turning[0]
            return mathutils.Vector(_BASIS_VECTORS[index]), value
    return None


def _length_axis_twist_degrees(spec):
    """
    Measure how far one pose entry turns a bone about its own length, if that is what it does.

    Pure: the decision is the arithmetic on the entry alone, so the rule can be read, tested
    and changed without a bone, a rig or a Blender session in hand.

    Args:
        spec: One validated pose entry, whose rotation is read in the call's pose space.

    Returns:
        float | None: The turn in degrees when the entry rotates about the length axis by
        enough to mean something, otherwise None - which covers a rotation about another
        axis, a rotation too small to be deliberate, and an entry stating no single-axis
        rotation at all.

    """
    resolved = _rotation_axis_angle(spec)
    if resolved is None:
        return None
    axis, angle = resolved
    if abs(angle) < math.radians(_INERT_ROTATION_MINIMUM_DEGREES):
        return None
    length_axis = mathutils.Vector(_BASIS_VECTORS[_AXIS_INDEX[_LENGTH_AXIS]])
    if abs(axis.dot(length_axis)) < math.cos(math.radians(_LENGTH_AXIS_TOLERANCE_DEGREES)):
        return None
    return math.degrees(abs(angle))


def _twist_notice(bone_name, degrees, space):
    """Word the notice for one bone whose rotation cannot move it."""
    return (
        f"Bone '{bone_name}': {degrees:.4g} degrees about the bone's own length axis "
        f"({_LENGTH_AXIS} in {space} space, the axis running head to tail) is a twist. It rolls the bone and "
        "whatever is parented to it, but the bone's tail - and every child bone's head, which sits on it - "
        "stay exactly where they are, so this joint does not bend and nothing swings. If a bend was intended, "
        "rotate about one of the bone's other two axes; list_character_bones(rest_axes=True) reports where "
        "each one points."
    )


def _inert_rotation_warnings(prepared, space):
    """
    Say which of this call's rotations turn a bone about its own length, moving nothing.

    A bone's own +Y runs head to tail (`_LENGTH_AXIS`), so a rotation about it rolls the bone
    where it stands: the call succeeds, the keys land, the bone's tail and every child's head
    stay exactly put, and the render shows no bend. That failure is silent in every other
    channel this handler reports - the pose matrix genuinely changed - which is why it is worth
    a warning rather than leaving the caller to measure the tail themselves.

    Only LOCAL and LOCAL_WITH_PARENT are judged: in those spaces the axis letters resolve to
    the bone's own rest basis, so `Y` is the bone's length. Under POSE and WORLD the same letter
    names the armature's or the scene's axis, which says nothing about this bone.

    Args:
        prepared: `(pose_bone, spec, matrix or None)` triples from `_validate_pose_specs`.
        space: The call's pose space.

    Returns:
        list[str]: One notice per bone whose rotation is a twist about its own length, in the
        order posed.

    """
    if space not in _BONE_LOCAL_SPACES:
        return []
    turns = ((pose_bone.name, _length_axis_twist_degrees(spec)) for pose_bone, spec, _matrix in prepared)
    return [_twist_notice(name, degrees, space) for name, degrees in turns if degrees is not None]


def _resolved_target(armature, pose_bone, spec, space, prepared_matrix):
    """
    Produce one bone's target matrix and the space it is stated in, at the moment it is written.

    Args:
        armature: The armature object being posed.
        pose_bone: The bone about to be written; its parents are already posed.
        spec: Its validated pose entry.
        space: The call's pose space.
        prepared_matrix: The matrix `_validate_pose_specs` could build ahead of time, or None.

    Returns:
        tuple: The target matrix, and the space to convert it from.

    Raises:
        ValueError: If the resolved matrix is singular, or the aim is degenerate.

    """
    if prepared_matrix is not None:
        return prepared_matrix, space
    if "aim_at" in spec:
        # An aim is stated in the scene, not in the call's space, and lands in armature space.
        return _aim_pose_matrix(armature, pose_bone, spec["aim_at"]), "POSE"
    matrix = _pose_matrix_from_channels(armature, pose_bone, spec, space)
    _reject_singular(pose_bone, matrix)
    return matrix, space


def _apply_pose_specs(armature, prepared, space, reset_unspecified=False, detail=False):
    """
    Pose every prepared bone and report what changed.

    Args:
        armature: The armature object being posed.
        prepared: `(pose_bone, spec, matrix or None)` triples from `_validate_pose_specs`.
        space: The space the input matrices are expressed in.
        reset_unspecified: Reset every bone the call did not name to rest before posing.
        detail: Report the pre-call matrix as well, and neither matrix rounded.

    Returns:
        list: One record per posed bone - its name, the channels the call set, and the pose
        matrix it ended on, rounded to `_POSE_MATRIX_DECIMALS` unless `detail` is set.

    """
    targeted = {pose_bone.name for pose_bone, _spec, _matrix in prepared}
    before = {pose_bone.name: pose_bone.matrix.copy() for pose_bone, _spec, _matrix in prepared} if detail else {}
    if reset_unspecified:
        for pose_bone in armature.pose.bones:
            if pose_bone.name not in targeted:
                pose_bone.matrix_basis.identity()
        bpy.context.view_layer.update()
    # `pose_bone.matrix` reads the last evaluation, so each bone is resolved and set after its
    # parent has been re-evaluated; otherwise a child's pose-, world- or rest-space target is
    # solved against a stale parent and the compensation lands in a channel nothing keys.
    for pose_bone, spec, prepared_matrix in sorted(prepared, key=lambda item: len(item[0].parent_recursive)):
        input_matrix, input_space = _resolved_target(armature, pose_bone, spec, space, prepared_matrix)
        pose_bone.matrix = armature.convert_space(
            pose_bone=pose_bone,
            matrix=input_matrix,
            from_space=input_space,
            to_space="POSE",
        )
        for name, value in spec.get("custom_properties", {}).items():
            pose_bone[name] = value
        bpy.context.view_layer.update()
    records = []
    for pose_bone, spec, _matrix in prepared:
        record = {"bone": pose_bone.name, "channels": _changed_channels(spec)}
        if detail:
            record["before_pose_matrix"] = _matrix_list(before[pose_bone.name])
            record["after_pose_matrix"] = _matrix_list(pose_bone.matrix)
        else:
            record["after_pose_matrix"] = _rounded_matrix_list(pose_bone.matrix)
        records.append(record)
    return records


def _rotation_path(pose_bone):
    if pose_bone.rotation_mode == "QUATERNION":
        return "rotation_quaternion"
    if pose_bone.rotation_mode == "AXIS_ANGLE":
        return "rotation_axis_angle"
    return "rotation_euler"


def _pose_key_paths(pose_bone, spec):
    paths = []
    if "matrix" in spec:
        paths.extend(["location", "scale", _rotation_path(pose_bone)])
    else:
        if "location" in spec:
            paths.append("location")
        if "scale" in spec:
            paths.append("scale")
        if any(name in spec for name in _ROTATION_CHANNELS):
            paths.append(_rotation_path(pose_bone))
    paths.extend(f'["{name}"]' for name in spec.get("custom_properties", {}))
    return paths


def _previous_channel_values(action, pose_bone, path, frame):
    """
    Read what the action already holds for one rotation channel before `frame`.

    A derived rotation has no preferred representation, so the value keyed at the previous key
    is the only thing that says which of the equivalent spellings interpolates sanely.

    Args:
        action: The action being keyed.
        pose_bone: The bone whose channel is being keyed.
        path: A key of `_ROTATION_CHANNEL_WIDTH`.
        frame: The frame about to be keyed.

    Returns:
        list: One value per array index at the last key before `frame`, or None when the channel
        is not yet animated or has no earlier key.

    """
    width = _ROTATION_CHANNEL_WIDTH[path]
    data_path = f"{_bone_path_token(pose_bone.name)}.{path}"
    curves = {}
    for collection in action_fcurve_collections(action):
        for curve in collection:
            if curve.data_path == data_path and 0 <= curve.array_index < width:
                curves[curve.array_index] = curve
    if len(curves) != width:
        return None
    times = [
        float(point.co[0])
        for curve in curves.values()
        for point in curve.keyframe_points
        if float(point.co[0]) < frame - _FRAME_TOLERANCE
    ]
    if not times:
        return None
    previous = max(times)
    return [float(curves[index].evaluate(previous)) for index in range(width)]


def _match_previous_rotation(action, pose_bone, path, frame):
    """
    Re-spell a derived rotation so it interpolates from the previous key the short way round.

    A quaternion and its negation are the same orientation but interpolate opposite ways, and an
    Euler triple has infinitely many equivalent spellings; both produce a spin in the graph
    editor when the neighbouring key happens to sit on the other branch.

    Args:
        action: The action being keyed, read for the previous key.
        pose_bone: The bone whose channel was just written.
        path: The rotation data path about to be keyed.
        frame: The frame about to be keyed.

    Returns:
        bool: Whether the bone's channel was rewritten.

    """
    previous = _previous_channel_values(action, pose_bone, path, frame)
    if previous is None:
        return False
    if path == "rotation_quaternion":
        current = mathutils.Quaternion(pose_bone.rotation_quaternion)
        if current.dot(mathutils.Quaternion(previous)) >= 0.0:
            return False
        pose_bone.rotation_quaternion = [-value for value in current]
        return True
    if path == "rotation_euler":
        mode = pose_bone.rotation_mode
        rotation = mathutils.Euler(pose_bone.rotation_euler, mode).to_quaternion()
        pose_bone.rotation_euler = rotation.to_euler(mode, mathutils.Euler(previous, mode))
        return True
    # An axis-angle pair has the same two-branch problem, but no bone on the rigs here uses it
    # and there is no measurement to justify a guess at the right convention.
    return False


def _bone_curve_path(bone_name, data_path):
    """
    Spell the F-Curve data_path one bone channel or custom property is keyed under.

    Args:
        bone_name: The posed bone.
        data_path: A channel name, or a custom property's `["name"]` subscript.

    Returns:
        str: The full curve path. A channel joins with a dot; a subscript concatenates
        directly, the two spellings `_pose_key_paths` emits.

    """
    separator = "" if data_path.startswith("[") else "."
    return f"{_bone_path_token(bone_name)}{separator}{data_path}"


def _style_written_keys(action, changed_keys, style):
    """
    Style only the points this call wrote, named by (bone, data_path, frame).

    The previous implementation walked every F-Curve in the action and restyled anything
    sharing the frame. `keyframe_character_pose` tells callers to put object root motion in
    this same action, so keying one bone at frame 12 silently rewrote the root's keys at
    frame 12 as well - a tool changing data it was not asked to change.

    Args:
        action: The action just keyed.
        changed_keys: `_write_pose_keys`' records: bone, data_path and frame.
        style: The `KeyStyle` every point this call wrote is shaped with.

    Returns:
        int: How many keyframe points were styled.

    """
    wanted = {}
    for record in changed_keys:
        path = _bone_curve_path(record["bone"], record["data_path"])
        wanted.setdefault(path, []).append(float(record["frame"]))
    changed = 0
    for collection in action_fcurve_collections(action):
        for curve in collection:
            frames = wanted.get(curve.data_path)
            if frames is None:
                continue
            for point in curve.keyframe_points:
                if any(math.isclose(float(point.co[0]), frame, abs_tol=_FRAME_TOLERANCE) for frame in frames):
                    style_point(point, style)
                    changed += 1
    return changed


def _cycle_extension_warnings(action, prepared, frame):
    """
    Warn once per bone whose new key lands outside a cycle its curves already repeat.

    A Cycles F-Modifier repeats its own curve's key extent, so keying a bone at frame 162
    when its curves cycle over frames 1-24 does not add a frame to a 24-frame loop: it makes
    the loop 161 frames long, and that bone stops moving at the rate the rest of the rig
    does. Blender does exactly what it was asked and says nothing, which is how a walk's arms
    drifted through one slow interpolation for a whole shot. Extending a cycle on purpose is
    legitimate authoring, so this warns and keys rather than refusing.

    Args:
        action: The action about to be keyed, read before `_write_pose_keys` writes to it.
        prepared: `_validate_pose_specs` output: the bones and channels about to be keyed.
        frame: The frame this call keys.

    Returns:
        list[str]: One warning per affected bone, naming the frame, the extent it fell
        outside and the period that extent just became, bounded by `_MAX_CYCLE_WARNINGS`
        with one summary line for the rest. Warnings are lifted whole into the envelope and
        never paged, so 500 posed bones must not be able to spend the reply budget on them.

    """
    owners = {}
    for pose_bone, spec, _matrix in prepared:
        for path in _pose_key_paths(pose_bone, spec):
            owners[_bone_curve_path(pose_bone.name, path)] = pose_bone.name
    stretched = {}
    for collection in action_fcurve_collections(action):
        for curve in collection:
            bone = owners.get(curve.data_path)
            if bone is None or bone in stretched:
                continue
            if not any(modifier.type == "CYCLES" for modifier in curve.modifiers):
                continue
            frames = [float(point.co[0]) for point in curve.keyframe_points]
            if len(frames) <= 1:
                continue
            first, last = min(frames), max(frames)
            if first - _FRAME_TOLERANCE <= frame <= last + _FRAME_TOLERANCE:
                continue
            stretched[bone] = (curve.data_path, first, last)
    affected = list(stretched)
    warnings = []
    for bone in affected[:_MAX_CYCLE_WARNINGS]:
        path, first, last = stretched[bone]
        warnings.append(
            f"Bone '{bone}' is keyed at frame {frame:g}, outside the frames {first:g}-{last:g} its curves "
            f"already cycle over ({path}). A Cycles modifier repeats its own curve's key extent, so this "
            f"bone's period becomes {max(last, frame) - min(first, frame):g} frames instead of "
            f"{last - first:g} and it stops looping with the rest of the rig. Key it inside the cycle, or "
            "re-cycle the action deliberately."
        )
    remainder = affected[_MAX_CYCLE_WARNINGS:]
    if remainder:
        listed = ", ".join(remainder[:_MAX_CYCLE_WARNINGS])
        trailing = f" and {len(remainder) - _MAX_CYCLE_WARNINGS} more" if len(remainder) > _MAX_CYCLE_WARNINGS else ""
        warnings.append(
            f"{len(remainder)} further bone(s) keyed at frame {frame:g} land outside the cycle their own curves "
            f"carry, stretching it the same way: {listed}{trailing}."
        )
    return warnings


def _refuse_unkeyable_request(keying_policy, style, action_policy, action_name, prepared):
    """
    Reject a keying request before an action is created or a bone is moved.

    The aim check is here rather than in `_validate_pose_specs` because it is about keying, not
    about posing: a minimal-arc aim keeps whatever roll the bone already holds, so the same call
    at two frames keys two different rolls. `set_character_pose` may do that; an action may not.

    Args:
        keying_policy: INSERT, REPLACE or REMOVE.
        style: The requested `KeyStyle`.
        action_policy: ENSURE, CREATE or REUSE.
        action_name: The action the caller asked to key, checked for existence when removing.
        prepared: `_validate_pose_specs` output, read for its bone names and specs.

    Raises:
        ValueError: If a policy is unknown, a removal would need an action that is not there,
            or an aim would key an undefined roll.

    """
    if keying_policy not in {"INSERT", "REPLACE", "REMOVE"}:
        raise ValueError("keying_policy must be INSERT, REPLACE, or REMOVE")
    if keying_policy == "REMOVE":
        # Removing keys means removing them from keys that are already there. Under any policy
        # that would first have to invent the action - CREATE by definition, ENSURE when the
        # name answers to nothing - the call would create an empty action, delete nothing from
        # it, and leave it assigned to the rig as if it were the shot's animation.
        if action_policy == "CREATE":
            raise ValueError(
                "Removing keys requires an action that already exists, which action_policy='CREATE' forbids"
            )
        if bpy.data.actions.get(action_name) is None:
            raise ValueError(f"Removing keys requires an action that already exists: {action_name}")
    style.validate()
    for pose_bone, spec, _matrix in prepared:
        if "aim_at" in spec and spec["aim_at"]["up"] is None:
            raise ValueError(
                f"aim_at for '{pose_bone.name}' requires up_axis and up_reference when keying: without "
                "them the aim keeps whatever roll the bone happens to hold, so the same call at two "
                "frames keys two different rolls"
            )


def _write_pose_keys(action, prepared, frame, keying_policy):
    """
    Write one frame's keys for every prepared bone and report what changed.

    Args:
        action: The action being authored, read for the previous rotation an aim must match.
        prepared: `_validate_pose_specs` output, already applied to the rig.
        frame: The frame to key, subframe included.
        keying_policy: INSERT adds, REPLACE deletes then adds, REMOVE only deletes.

    Returns:
        list[dict]: One record per channel touched: bone, data_path and frame.

    Raises:
        RuntimeError: If Blender refuses to insert a key, which would otherwise leave the
            action holding some of this frame's channels and not others.

    """
    changed_keys = []
    for pose_bone, spec, _matrix in prepared:
        for path in _pose_key_paths(pose_bone, spec):
            if keying_policy in {"REPLACE", "REMOVE"}:
                # A channel with no key at this frame raises rather than reporting nothing.
                with contextlib.suppress(TypeError):
                    pose_bone.keyframe_delete(data_path=path, frame=frame)
            if keying_policy != "REMOVE":
                if "aim_at" in spec and path in _ROTATION_CHANNEL_WIDTH:
                    _match_previous_rotation(action, pose_bone, path, frame)
                if not pose_bone.keyframe_insert(data_path=path, frame=frame, group=pose_bone.name):
                    raise RuntimeError(f"Could not insert key for {pose_bone.name}.{path}")
            changed_keys.append({"bone": pose_bone.name, "data_path": path, "frame": frame})
    return changed_keys


def _action_reply(
    armature, animation, action, previous_action, keying_policy, changed_bones, changed_keys, interpolation_updates
):
    """
    Describe the assignment and the keys any keying call left behind.

    Both keying tools end on the same eleven fields and the same rule for naming a displaced
    action; stating that rule twice is how the two replies drift into disagreeing about what
    `assigned_action` means.

    Args:
        armature: The keyed armature object.
        animation: Its `animation_data`, read for the assignment this call ended on.
        action: The action that was authored.
        previous_action: The action that drove the rig before, or None.
        keying_policy: The policy the caller asked for.
        changed_bones: Every bone the call posed, complete.
        changed_keys: One record per channel keyed.
        interpolation_updates: How many keyframe points this call styled.

    Returns:
        dict: The shared reply; each tool adds only its own extras on top.

    """
    assigned_name = getattr(getattr(animation, "action", None), "name", None)
    reply = {
        "armature_object": armature.name,
        "action": action.name,
        "action_slot": getattr(getattr(animation, "action_slot", None), "identifier", None),
        # An action with no user is dropped at save, so whether the rig is left driven by what
        # this call authored is the difference between animation an artist can open and a reply
        # that claims success for discarded work.
        "assigned_action": assigned_name,
        "keying_policy": keying_policy,
        # The keys are the deliverable, but the page of them is what the reply budget shortens,
        # so name every posed bone separately.
        "changed_bones": changed_bones,
        "changed_keys": changed_keys,
        "interpolation_updates": interpolation_updates,
        "changed_objects": [armature.name],
        "changed_resources": [action.name],
    }
    previous_name = getattr(previous_action, "name", None)
    if previous_name is not None and previous_name != assigned_name:
        # The rig was driven by something else; say what this call displaced.
        reply["unassigned_action"] = previous_name
    return reply


def _keyframe_reply(
    armature,
    animation,
    action,
    previous_action,
    *,
    keying_policy,
    changed_bones,
    keyed,
    warnings,
    detail,
):
    """
    Describe what one pose keying call left behind, whether it keyed one frame or twenty.

    Args:
        armature: The posed armature object.
        animation: Its `animation_data`, read for the assignment this call ended on.
        action: The action that was authored.
        previous_action: The action that drove the rig before, or None.
        keying_policy: The policy the caller asked for.
        changed_bones: Every bone any frame of the call posed, named once, complete.
        keyed: `_key_pose_frames` output.
        warnings: Non-fatal notices about the rig and about what this call's keys did to it.
        detail: Whether the caller asked for the per-bone matrices.

    Returns:
        dict: The handler reply.

    """
    reply = _action_reply(
        armature,
        animation,
        action,
        previous_action,
        keying_policy,
        changed_bones,
        keyed["changed_keys"],
        keyed["styled"],
    )
    # Which frames the action now holds this call's keys at, ascending: the keys themselves are
    # what the reply budget shortens, so a batched call still says what it covered.
    reply["keyed_frames"] = keyed["frames"]
    # The envelope lifts these and never shortens them, so a notice about a cycle this call
    # just stretched survives the page of keys being cut down to fit the budget.
    reply["warnings"] = warnings
    if detail:
        # The pose is restored before this returns, so these matrices describe what was keyed at
        # each requested frame, not what the rig is holding now.
        reply["bones"] = keyed["records"]
    return reply


def _place_playhead(scene, frame):
    """
    Put the playhead on a frame that may carry a subframe.

    `frame_set` takes the integer frame and the fraction separately, and a key inserted while
    the playhead sits elsewhere records the wrong pose.

    Args:
        scene: The scene whose playhead moves.
        frame: The frame to key, fractional part included.

    """
    whole = math.floor(frame)
    scene.frame_set(whole, subframe=frame - whole)


# What one batched keying call may apply, across every frame it names: 250 frames of 500 bones
# would be 125,000 bone writes behind one socket call, each with its own view-layer update.
# Mirrors `keys`' own caps on the server side, and is checked here as well because the socket
# is the boundary an unvalidated caller reaches.
_MAX_KEYED_POSE_ENTRIES = 2000


def _pose_key_requests(frame, poses, keys):
    """
    Resolve the two shapes a keying call may take into one ascending list of frames.

    A stride is thirteen keys of the same few bones, and keying it one call at a time is
    thirteen round trips carrying the same rig name, action name and policy. `keys` says the
    whole stride once; `frame` with `poses` stays the single-frame spelling.

    Args:
        frame: The single-frame form's frame, or None.
        poses: The single-frame form's pose entries, or None.
        keys: The batched form's `{frame, poses}` records, or None.

    Returns:
        tuple: `(frame, poses)` pairs in ascending frame order, and whether the caller used the
        batched form - which names a frame on every per-bone record, where the single-frame
        form would only repeat the one frame the call already carries.

    Raises:
        ValueError: If neither or both forms are given, if a batched entry is malformed or
            empty, if a frame is named twice, or if the batch carries more pose entries than
            one call may apply.

    """
    if (frame is not None or poses is not None) == (keys is not None):
        raise ValueError(
            "Supply exactly one of frame with poses (one frame) or keys (several frames, each with its own poses)"
        )
    if keys is None:
        if frame is None or not poses:
            raise ValueError("The single-frame form requires both frame and poses")
        return [(_finite(frame, "frame"), list(poses))], False
    requests = []
    total = 0
    for index, entry in enumerate(keys):
        if not isinstance(entry, dict) or "frame" not in entry:
            raise ValueError(f"keys[{index}] must be an object carrying frame and poses")
        entry_poses = list(entry.get("poses") or ())
        if not entry_poses:
            raise ValueError(f"keys[{index}] requires at least one pose entry")
        total += len(entry_poses)
        requests.append((_finite(entry["frame"], f"keys[{index}].frame"), entry_poses))
    if total > _MAX_KEYED_POSE_ENTRIES:
        raise ValueError(
            f"keys carries {total} pose entries across {len(requests)} frames, more than the "
            f"{_MAX_KEYED_POSE_ENTRIES} one call may apply; split the frame range across calls"
        )
    frames = [at for at, _entry_poses in requests]
    repeated = sorted({value for value in frames if frames.count(value) > 1})
    if repeated:
        # Two entries for one frame would key the second over the first, and which pose survived
        # would depend on the order the list happened to be written in.
        raise ValueError(f"keys names the same frame more than once: {repeated}")
    return sorted(requests, key=lambda item: item[0]), True


def _keyed_bone_names(prepared_frames):
    """
    Name every bone any frame of this call poses, once, in the order the call first names it.

    Args:
        prepared_frames: `(frame, prepared)` pairs.

    Returns:
        list[str]: The bone names, deduplicated across frames.

    """
    names = {}
    for _frame, prepared in prepared_frames:
        for pose_bone, _spec, _matrix in prepared:
            names[pose_bone.name] = None
    return list(names)


def _keyed_custom_properties(prepared_frames):
    """
    Collect the custom properties this call writes, by bone, across every frame.

    Args:
        prepared_frames: `(frame, prepared)` pairs.

    Returns:
        dict: `{bone name: property names}`, in the order the call first names each, for the
        pose restore to snapshot and for the library-override notice to name.

    """
    properties = {}
    for _frame, prepared in prepared_frames:
        for pose_bone, spec, _matrix in prepared:
            written = properties.setdefault(pose_bone.name, [])
            written.extend(name for name in spec.get("custom_properties", {}) if name not in written)
    return properties


def _bare_write_warnings(armature, custom_properties):
    """
    Report what a pose write, as opposed to a key, cannot make stick.

    A custom property written straight onto a library override reads back correctly and is the
    library's value again after save and reopen, and nothing the reply measures can say so. The
    same value keyed into an action survives, because the action is local data - so this is
    `set_character_pose`'s notice and not `keyframe_character_pose`'s.

    Args:
        armature: The rig being posed.
        custom_properties: `{bone name: property names}` this call writes.

    Returns:
        list[str]: The notices for the reply, empty when the rig is local or nothing is written.

    """
    warning = _override_property_warning(armature, [name for name, written in custom_properties.items() if written])
    return [] if warning is None else [warning]


def _bounded_frame_warnings(per_frame):
    """
    Bound a batched call's per-frame notices to what the envelope can carry whole.

    Warnings are lifted into the envelope and never paged, so 250 keyed frames each warning
    about the same stretched cycle would spend the whole reply budget saying it.

    Args:
        per_frame: One list of warnings per keyed frame, in the order the frames were keyed.

    Returns:
        list[str]: Every warning when few frames raised any - which is what a single-frame call
        always gets - otherwise the first `_MAX_CYCLE_WARNINGS` frames' warnings and one line
        counting what is not listed.

    """
    speaking = [warnings for warnings in per_frame if warnings]
    listed = [warning for warnings in speaking[:_MAX_CYCLE_WARNINGS] for warning in warnings]
    remainder = speaking[_MAX_CYCLE_WARNINGS:]
    if not remainder:
        return listed
    unlisted = sum(len(warnings) for warnings in remainder)
    listed.append(
        f"{len(remainder)} further keyed frame(s) raised {unlisted} more notice(s) of the same kind, not "
        "listed so the reply can still carry the keys this call wrote."
    )
    return listed


def _key_pose_frames(armature, action, prepared_frames, space, keying_policy, style, *, detail, report_frames):
    """
    Apply, key and style every requested frame, in ascending order.

    The playhead moves to each frame before that frame's poses are resolved. That is what makes
    a multi-frame call correct rather than merely fast: an `aim_at` then reads its target where
    the target is at that frame, and an absolute-space pose is built on whatever root and parent
    motion the action already holds there.

    Args:
        armature: The armature being keyed.
        action: The action every key lands in, already assigned to the rig.
        prepared_frames: `(frame, prepared)` pairs in ascending order.
        space: The pose space every entry is expressed in.
        keying_policy: INSERT, REPLACE or REMOVE.
        style: The `KeyStyle` every key this call writes is shaped with.
        detail: Whether to capture per-bone matrices at Blender's own precision.
        report_frames: Whether each per-bone record names the frame it describes.

    Returns:
        dict: frames (ascending), changed_keys, styled (how many points were styled), records
        (per-bone, per-frame) and warnings.

    """
    scene = bpy.context.scene
    changed_keys = []
    records = []
    per_frame_warnings = []
    styled = 0
    for frame, prepared in prepared_frames:
        _place_playhead(scene, frame)
        if keying_policy != "REMOVE":
            posed = _apply_pose_specs(armature, prepared, space, detail=detail)
            records.extend({"frame": frame, **record} if report_frames else record for record in posed)
            # Measured before the write: afterwards this frame is inside the extent it widened,
            # and the stretched cycle is invisible again. REMOVE narrows an extent rather than
            # widening one, and has no key landing outside anything.
            per_frame_warnings.append(
                _cycle_extension_warnings(action, prepared, frame) + _inert_rotation_warnings(prepared, space)
            )
        written = _write_pose_keys(action, prepared, frame, keying_policy)
        if keying_policy != "REMOVE":
            styled += _style_written_keys(action, written, style)
        changed_keys.extend(written)
    return {
        "frames": [frame for frame, _prepared in prepared_frames],
        "changed_keys": changed_keys,
        "styled": styled,
        "records": records,
        "warnings": _bounded_frame_warnings(per_frame_warnings),
    }


# --- What a posing call borrows, and the order it hands it back ----------------------------
#
# A call that poses or keys borrows three things from the file: the bones' own channel values,
# the playhead, and the rig's action assignment. Each has a context manager below that
# snapshots on entry, so every call states what it is borrowing instead of re-spelling a
# `finally` block - and the ordering argument is made once, here, rather than in a comment per
# call site. Nest them playhead-outermost, then the action, then the pose: they unwind
# inside-out, so the bones are back on their own values before the playhead moves, and from
# that instant the assigned action drives them. Restore them the other way round and the rig
# is left holding whatever pose the last solved frame happened to produce.


@contextlib.contextmanager
def restored_bone_pose(armature, bone_names, custom_properties=None, *, only_on_error=False):
    """
    Hand the named bones back the channel values, and custom properties, they arrived with.

    Args:
        armature: The armature whose pose bones are borrowed.
        bone_names: The bones to snapshot, by name.
        custom_properties: `{bone_name: property names}` to snapshot alongside the pose, or
            None when the call writes none.
        only_on_error: Restore only if the block raises. `set_character_pose`'s pose is the
            deliverable, so a call that succeeds keeps it; a keying call's pose is scaffolding
            for the keys it wrote and is handed back either way.

    """
    pose_bones = [armature.pose.bones[name] for name in bone_names]
    matrices = {bone.name: bone.matrix_basis.copy() for bone in pose_bones}
    properties = {
        bone.name: {name: bone[name] for name in (custom_properties or {}).get(bone.name, ())} for bone in pose_bones
    }

    def restore():
        for bone in pose_bones:
            bone.matrix_basis = matrices[bone.name]
            for name, value in properties[bone.name].items():
                bone[name] = value

    try:
        yield
    except BaseException:
        restore()
        raise
    else:
        if not only_on_error:
            restore()


@contextlib.contextmanager
def restored_playhead(scene):
    """
    Put the playhead back where the call found it, and re-evaluate the scene on the way out.

    Args:
        scene: The scene whose `frame_current` is borrowed.

    """
    previous_frame = scene.frame_current
    try:
        yield
    finally:
        scene.frame_set(previous_frame)
        bpy.context.view_layer.update()


@contextlib.contextmanager
def restored_action_assignment(animation):
    """
    Give the rig its action and slot back if the block raises, and leave them if it does not.

    A call that authored keys leaves its own action assigned on purpose: an action nothing
    references carries zero users and Blender drops it at save. A call that raised authored
    nothing, so the action it displaced has to come back - `object_state` does not snapshot
    `animation_data.action`, so nothing else in the transaction would put it back, and the
    only symptom is a shot whose character has quietly stopped moving.

    Args:
        animation: The rig's `animation_data`.

    Yields:
        bpy.types.Action | None: The action the rig arrived on, for the reply to name.

    """
    previous_action = animation.action
    previous_slot = getattr(animation, "action_slot", None)
    try:
        yield previous_action
    except BaseException:
        animation.action = previous_action
        if previous_action is not None and previous_slot is not None:
            # Assigning an action resets the slot, and a slot Blender no longer considers
            # suitable is its refusal to make, not this unwind's to force.
            with contextlib.suppress(Exception):
                animation.action_slot = previous_slot
        raise


# Longest chain solve_bone_reach will auto-resolve or accept explicitly - matches
# BoneReach.chain_length's Field(le=32) on the server side.
_MAX_REACH_CHAIN = 32
# Names for the scratch Empty objects and IK constraint solve_bone_reach creates for one
# reach's evaluation. Never left behind: removed before the call returns on every path,
# success or failure (mutation_transaction's rollback additionally covers the Empties, as
# ordinary created objects, if an exception unwinds past their own try/finally).
_REACH_HELPER_PREFIX = "__solve_bone_reach__"
# How close tip_bone's tail must land to the target before a reach counts as converged.
# 0.1 mm is below what a 24-frame shot shows and above what a 500-iteration Blender IK solve
# leaves on a bent chain, so it separates "solved" from "the solver stopped short" without
# calling ordinary solver residue a failure. A shot needing more or less says so per call.
_DEFAULT_REACH_TOLERANCE_M = 1e-4


def _validated_tolerance(tolerance_m):
    """
    Read a reach's convergence tolerance, refusing one that names no precision.

    Args:
        tolerance_m: The caller's tolerance, in metres.

    Returns:
        float: The tolerance every reach in the call is judged against.

    Raises:
        ValueError: If it is not finite, or not greater than zero - `converged` would then be
            a claim about nothing.

    """
    tolerance_m = _finite(tolerance_m, "tolerance_m")
    if tolerance_m <= 0.0:
        raise ValueError(f"tolerance_m must be greater than 0 metres, not {tolerance_m}")
    return tolerance_m


def _rest_ancestor_chain(tip, length):
    """
    Build an exact-length ancestor run above tip, root-most last, ignoring branching.

    Args:
        tip: The rest bone (`armature.data.bones[...]`) the reach targets.
        length: The exact chain length the caller asked for explicitly.

    Returns:
        list: `length` `bpy.types.Bone`s, tip first.

    Raises:
        ValueError: If tip has fewer than `length - 1` ancestors.

    """
    chain = [tip]
    bone = tip
    for _step in range(length - 1):
        if bone.parent is None:
            raise ValueError(f"'{tip.name}' has only {len(chain)} ancestor(s); chain_length={length} exceeds them")
        chain.append(bone.parent)
        bone = bone.parent
    return chain


def _unbranched_ancestor_chain(tip, max_length):
    """
    Tip-to-root ancestor run, stopping before the first fork (2+ children) or the last root.

    Args:
        tip: The rest bone the reach targets.
        max_length: The longest chain this will return.

    Returns:
        list: 1 to `max_length` `bpy.types.Bone`s, tip first.

    """
    chain = [tip]
    bone = tip
    while len(chain) < max_length:
        parent = bone.parent
        if parent is None or len(parent.children) > 1:
            break
        chain.append(parent)
        bone = parent
    return chain


def _synthesize_pole(armature, chain):
    """
    Infer a pole point from the chain's REST-pose bend, in world space.

    The chain's middle joint - found by walking tip_tail, then every chain bone's head in
    order down to root_head, and taking the one at the midpoint of that list - is projected
    off the straight root-to-tip line; what is left of it is the bend direction, scaled out
    by the chain's total rest length. A chain with only one bone, or whose rest pose is
    dead straight, has no such direction and is refused rather than guessed.

    Args:
        armature: The armature object.
        chain: The reach's resolved chain, tip first, as returned by
            `_unbranched_ancestor_chain` or `_rest_ancestor_chain`.

    Returns:
        mathutils.Vector: A world-space point off to the bend side of the chain.

    Raises:
        ValueError: If the chain's root and tip coincide, or its rest pose is straight.

    """
    root_head = chain[-1].head_local
    tip_tail = chain[0].tail_local
    axis = tip_tail - root_head
    if axis.length <= _AIM_MIN_LENGTH:
        raise ValueError(f"'{chain[0].name}' chain root and tip coincide; supply pole_target_point explicitly")
    axis = axis.normalized()
    joints = [tip_tail, *(bone.head_local for bone in chain)]
    mid = joints[len(joints) // 2]
    offset = mid - root_head
    projected = offset - axis * offset.dot(axis)
    if projected.length <= _AIM_MIN_RESIDUAL:
        raise ValueError(
            f"'{chain[0].name}' chain's rest pose is straight; no natural pole direction can "
            "be inferred - supply pole_target_point or pole_target_object_name"
        )
    projected = projected.normalized()
    pole_local = mid + projected * sum(bone.length for bone in chain)
    return armature.matrix_world @ pole_local


def _reach_helper_object(location):
    """Create a scratch Empty at a world point, for an IK target/pole with no named object."""
    empty = bpy.data.objects.new(f"{_REACH_HELPER_PREFIX}{uuid.uuid4().hex}", None)
    bpy.context.collection.objects.link(empty)
    empty.location = location
    return empty


def _resolved_reach_target(point, object_name, label):
    """
    Resolve an existing named object, or a scratch Empty at an explicit world point.

    Args:
        point: A raw world-space point, or None.
        object_name: An existing object's name, or None. Exactly one of point/object_name is
            non-None; the caller has already enforced that (BoneReach's own validator, for
            target_point/target_object_name, and solve_bone_reach's own pole handling, for
            pole_target_point/pole_target_object_name).
        label: What this target is, for the not-found message.

    Returns:
        tuple: `(object, is_temporary)`. is_temporary is True for a scratch Empty this
            function created, which the caller must remove once the IK solve has read it.

    Raises:
        ValueError: If object_name does not name an existing object.

    """
    if object_name is not None:
        obj = bpy.data.objects.get(object_name)
        if obj is None:
            raise ValueError(f"{label} object not found: {object_name}")
        return obj, False
    return _reach_helper_object(_vector_tuple(point, label)), True


def _configured_reach_constraint(tip_pose_bone, reach, target_obj, pole_obj, chain_count):
    """Add and configure one reach's temporary IK constraint on tip_pose_bone."""
    fields = {
        "name": f"{_REACH_HELPER_PREFIX}{uuid.uuid4().hex}",
        "target": target_obj,
        "pole_target": pole_obj,
        "chain_count": chain_count,
        "pole_angle": math.radians(reach.get("pole_angle_degrees", 0.0)),
        "use_stretch": bool(reach.get("use_stretch", False)),
        "iterations": int(reach.get("iterations", 500)),
        # tip_bone's TAIL is what the model promises reaches target, not just its rotation.
        "use_tail": True,
    }
    constraint = tip_pose_bone.constraints.new(type="IK")
    try:
        for field, value in fields.items():
            setattr(constraint, field, value)
    except Exception:
        # The constraint is on the rig from `new()` onwards, and the caller's own try/finally
        # only covers a constraint this function returned. A value Blender's RNA refuses must
        # not leave a live IK constraint behind - the same reason `add_pose_bone_constraint`
        # removes a constraint it created but could not configure.
        tip_pose_bone.constraints.remove(constraint)
        raise
    return constraint


def _resolve_reach_chain(armature, reach, captured):
    """Resolve the rest-bone chain a reach's tip_bone/chain_length imply, refusing any overlap."""
    tip_name = _required_name(reach.get("tip_bone"), "tip_bone")
    rest_tip = armature.data.bones.get(tip_name)
    if rest_tip is None:
        raise ValueError(f"Pose bone not found: {tip_name}")
    requested_length = reach.get("chain_length")
    if requested_length is None:
        rest_chain = _unbranched_ancestor_chain(rest_tip, _MAX_REACH_CHAIN)
        chain_length_source = "resolved"
    else:
        rest_chain = _rest_ancestor_chain(rest_tip, requested_length)
        chain_length_source = "explicit"
    overlap = sorted(name for name in (bone.name for bone in rest_chain) if name in captured)
    if overlap:
        raise ValueError(f"Bones claimed by more than one reach: {overlap}")
    return rest_chain, chain_length_source


def _resolved_reach_pole(armature, reach, rest_chain):
    """Resolve the reach's pole object, synthesizing one from the rest bend when none is named."""
    pole_point = reach.get("pole_target_point")
    pole_object_name = reach.get("pole_target_object_name")
    if pole_object_name is not None or pole_point is not None:
        pole_obj, pole_is_temp = _resolved_reach_target(pole_point, pole_object_name, "pole_target_point")
        return pole_obj, pole_is_temp, "explicit"
    return _reach_helper_object(tuple(_synthesize_pole(armature, rest_chain))), True, "resolved"


def _resolve_reach_geometry(armature, reach, rest_chain):
    """Resolve the reach's target and pole objects, and where the pole came from."""
    target_obj, target_is_temp = _resolved_reach_target(
        reach.get("target_point"), reach.get("target_object_name"), "target_point"
    )
    try:
        pole_obj, pole_is_temp, pole_source = _resolved_reach_pole(armature, reach, rest_chain)
    except Exception:
        # A scratch Empty for the target already exists by the time the pole is resolved, and
        # `_solve_one_reach`'s try/finally has not started yet. An unresolvable pole - an
        # unknown object, or a rest chain too straight to infer one from - must not strand it.
        if target_is_temp:
            bpy.data.objects.remove(target_obj, do_unlink=True)
        raise
    return target_obj, target_is_temp, pole_obj, pole_is_temp, pole_source


def _world_chain_reach(armature, rest_chain):
    """
    Measure how far a rest chain can extend, in the world space the reach is reported in.

    Args:
        armature: The armature object the chain belongs to, for its world matrix - a scaled
            rig's bones are longer or shorter in the scene than their rest lengths say.
        rest_chain: The reach's rest bones, tip first.

    Returns:
        float: The sum of the chain bones' world-space lengths - the straight-line distance
        from the chain's root head that the chain can span with every joint extended.

    """
    matrix = armature.matrix_world
    return sum((matrix @ bone.tail_local - matrix @ bone.head_local).length for bone in rest_chain)


def _reach_convergence_warning(solution, tolerance_m):
    """
    Say why one reach missed its tolerance, separating an unreachable target from a stall.

    Args:
        solution: One `ReachSolution`.
        tolerance_m: The convergence tolerance this call asked for, in metres.

    Returns:
        str | None: A notice naming the tip bone, the achieved error, the tolerance and the
        cause, or None when the reach converged and there is nothing to act on.

    """
    if solution.converged:
        return None
    measured = solution.measurements
    missed = (
        f"Reach '{solution.chain[0].name}' did not converge: achieved_error_m "
        f"{measured['achieved_error_m']:.6g} exceeds tolerance_m {tolerance_m:.6g}"
    )
    if solution.out_of_reach:
        return (
            f"{missed}. The target is out of reach: target_distance_m "
            f"{measured['target_distance_m']:.6g} is beyond chain_reach_m {measured['chain_reach_m']:.6g}. "
            "Move the target closer, lengthen the chain with chain_length, or move the armature."
        )
    return (
        f"{missed}. The target is within the chain's range (target_distance_m "
        f"{measured['target_distance_m']:.6g} of chain_reach_m {measured['chain_reach_m']:.6g}), so the "
        "solve stalled short of it: raise iterations, supply a pole_target_point, or loosen tolerance_m."
    )


def _reach_measurements(armature, rest_chain, tip_pose_bone, target_obj):
    """
    Measure where the solved tip landed, and how the target sits against the chain's reach.

    Args:
        armature: The armature object being posed, for its world matrix and pose bones.
        rest_chain: The reach's rest bones, tip first.
        tip_pose_bone: The chain's tip pose bone, with the IK constraint still evaluated.
        target_obj: The object (or scratch Empty) the reach is solving towards.

    Returns:
        dict: target_world, head_world, tail_world, achieved_error_m, chain_reach_m and
        target_distance_m, every one of them in world space.

    """
    matrix_world = armature.matrix_world
    target_point = target_obj.matrix_world.translation
    tail_point = matrix_world @ tip_pose_bone.tail
    # The chain's root head is where the chain is anchored, so the distance a target sits at
    # is measured from there - not from the tip, which the solve has already moved.
    root_head_point = matrix_world @ armature.pose.bones[rest_chain[-1].name].head
    return {
        "target_world": list(target_point),
        "head_world": list(matrix_world @ tip_pose_bone.head),
        "tail_world": list(tail_point),
        "achieved_error_m": (tail_point - target_point).length,
        "chain_reach_m": _world_chain_reach(armature, rest_chain),
        "target_distance_m": (target_point - root_head_point).length,
    }


def _validated_reach_hinge(reach, rest_chain):
    """
    Read and check a reach's optional hinge without applying it.

    Separate from applying it so a multi-frame keying call can refuse a bad hinge before it
    keys the first frame, rather than partway through the range.

    Args:
        reach: The reach entry, read for its optional `hinge`.
        rest_chain: The reach's resolved rest bones, tip first.

    Returns:
        tuple | None: `(bone_name, axis, min_radians, max_radians)`, or None when the reach
        asked for no hinge.

    Raises:
        ValueError: If the hinge names a bone outside this reach's chain - limiting a bone
            the solve does not drive would silently do nothing - an unknown axis, or an
            inverted limit interval.

    """
    hinge = reach.get("hinge")
    if hinge is None:
        return None
    bone_name = _required_name(hinge.get("bone_name"), "hinge.bone_name")
    chain_names = [bone.name for bone in rest_chain]
    if bone_name not in chain_names:
        raise ValueError(f"hinge.bone_name '{bone_name}' is not in this reach's chain: {chain_names}")
    axis = str(hinge.get("axis", "")).upper()
    if axis not in _AXIS_INDEX:
        raise ValueError(f"hinge.axis must be X, Y or Z, not {axis!r}")
    minimum = _finite(hinge.get("min_degrees"), "hinge.min_degrees")
    maximum = _finite(hinge.get("max_degrees"), "hinge.max_degrees")
    if minimum > maximum:
        raise ValueError("hinge.min_degrees must not exceed hinge.max_degrees")
    return bone_name, axis, math.radians(minimum), math.radians(maximum)


@contextlib.contextmanager
def _reach_hinge(armature, hinge):
    """
    Constrain one chain bone to a single rotation axis for the duration of the solve.

    A knee has one axis and one sign; an unconstrained IK solver will invert it to save the
    solve an iteration, which is how a walk cycle ends up with a backwards leg. The limit is
    temporary: every value it overwrote is back before the block ends.

    Args:
        armature: The armature being solved.
        hinge: `_validated_reach_hinge` output, or None when the reach asked for no hinge.

    """
    if hinge is None:
        yield None
        return
    bone_name, axis, minimum, maximum = hinge
    pose_bone = armature.pose.bones[bone_name]
    lowered = axis.lower()
    fields = {
        f"use_ik_limit_{lowered}": True,
        f"ik_min_{lowered}": minimum,
        f"ik_max_{lowered}": maximum,
        # Locking the other two axes is what makes this a hinge rather than a limited ball
        # joint: a knee that can still twist reads as broken just as fast as one that bends
        # backwards.
        **{f"lock_ik_{other.lower()}": True for other in _AXIS_INDEX if other != axis},
    }
    restore = {field: getattr(pose_bone, field) for field in fields}
    for field, value in fields.items():
        setattr(pose_bone, field, value)
    try:
        yield pose_bone
    finally:
        for field, value in restore.items():
            setattr(pose_bone, field, value)


@contextlib.contextmanager
def _reach_constraint(tip_pose_bone, reach, target_obj, pole_obj, chain_count):
    """
    Drive one chain from a temporary IK constraint, and never leave it live on the rig.

    Args:
        tip_pose_bone: The chain's tip pose bone, which carries the constraint.
        reach: The reach entry, read for pole angle, stretch and iteration count.
        target_obj: The object the tip's tail solves towards.
        pole_obj: The object the chain bends towards.
        chain_count: How many bones up the chain the solve drives.

    """
    constraint = _configured_reach_constraint(tip_pose_bone, reach, target_obj, pole_obj, chain_count)
    try:
        yield constraint
    finally:
        tip_pose_bone.constraints.remove(constraint)


class ReachSolution(NamedTuple):
    """
    Everything one solved reach knows, before any tool decides what to report of it.

    `solve_bone_reach` and `keyframe_bone_reach` report overlapping but different subsets of
    this. Returning a wire-shaped dict made the reply format the solver's business, and both
    tools re-projections of a dict neither of them owned: one grafted an extra key onto it,
    one narrowed it back down, and a third reached into `solved[0][1]["pole_source"]`.
    """

    chain: tuple
    chain_length_source: str
    pole_source: str
    measurements: dict
    converged: bool
    out_of_reach: bool
    captured: dict


def _solve_one_reach(armature, reach, rest_chain, chain_length_source, hinge, tolerance_m):
    """
    Temporarily IK-solve one reach and capture the pose its chain was left holding.

    Args:
        armature: The armature object being posed.
        reach: One validated reach entry, as a dict, read for its target, pole and solver
            settings.
        rest_chain: The reach's resolved rest bones, tip first.
        chain_length_source: "explicit" or "resolved", for the reply to repeat.
        hinge: `_validated_reach_hinge` output, or None.
        tolerance_m: How close tip_bone's tail must land to the target to count as converged.

    Returns:
        ReachSolution: the chain, where the pole came from, the world measurements, whether
        the solve landed, and each chain bone's evaluated pose-space matrix.

    Raises:
        ValueError: If an explicit target or pole names an object that does not exist, or an
            omitted pole cannot be synthesized from a straight or degenerate rest chain.

    """
    target_obj, target_is_temp, pole_obj, pole_is_temp, pole_source = _resolve_reach_geometry(
        armature, reach, rest_chain
    )
    tip_pose_bone = armature.pose.bones[rest_chain[0].name]
    try:
        with (
            _reach_hinge(armature, hinge),
            _reach_constraint(tip_pose_bone, reach, target_obj, pole_obj, len(rest_chain)),
        ):
            bpy.context.view_layer.update()
            captured = {bone.name: armature.pose.bones[bone.name].matrix.copy() for bone in rest_chain}
            measured = _reach_measurements(armature, rest_chain, tip_pose_bone, target_obj)
    finally:
        if target_is_temp:
            bpy.data.objects.remove(target_obj, do_unlink=True)
        if pole_is_temp:
            bpy.data.objects.remove(pole_obj, do_unlink=True)
        bpy.context.view_layer.update()
    return ReachSolution(
        chain=tuple(rest_chain),
        chain_length_source=chain_length_source,
        pole_source=pole_source,
        measurements=measured,
        converged=measured["achieved_error_m"] <= tolerance_m,
        out_of_reach=measured["target_distance_m"] > measured["chain_reach_m"],
        captured=captured,
    )


def _apply_captured_matrices(armature, captured, detail=False):
    """
    Apply pose matrices this call evaluated itself, without re-checking Blender's own output.

    The reach paths used to flatten every captured matrix to sixteen floats, rebuild a
    client-shaped pose entry from them and call `set_character_pose`, which re-resolved the
    armature by name, re-checked its pose position and ran a finiteness test over all sixteen
    - per bone, per frame. None of that was validating client input: the matrices came out of
    the depsgraph moments earlier. `set_character_pose` keeps that validation for callers who
    really do hand it numbers.

    Args:
        armature: The armature being posed.
        captured: `{bone_name: pose-space matrix}`, as the solve evaluated them.
        detail: Report each bone's pre-call matrix too, and neither matrix rounded.

    Returns:
        tuple: the `(pose_bone, spec, matrix)` triples the key writer takes, and one record
        per posed bone.

    """
    prepared = [
        (armature.pose.bones[name], {"bone_name": name, "matrix": matrix}, matrix) for name, matrix in captured.items()
    ]
    return prepared, _apply_pose_specs(armature, prepared, "POSE", detail=detail)


def _prepared_keyed_reaches(armature, reaches):
    """
    Resolve every keyed reach's chain, frames and hinge before a single key is written.

    Everything that can be known without moving the rig is checked here, because the
    alternative is a call that keys frames 1 to 7 and then refuses frame 8 over a misspelled
    bone, leaving the action holding half a move.

    Args:
        armature: The armature being keyed.
        reaches: The raw keyed-reach entries.

    Returns:
        list[dict]: Per reach: `spec` (the reach's solver fields, without its keys),
        `rest_chain`, `chain_length_source`, `hinge` (validated once here, not once per
        frame), and `keys`, mapping frame to that frame's target fields.

    Raises:
        ValueError: If a reach names no keys, two reaches claim the same bone, a chain or
            hinge does not resolve, or a key names a target object that does not exist.

    """
    claimed = {}
    prepared = []
    for index, reach in enumerate(reaches):
        if not isinstance(reach, dict):
            raise ValueError(f"reaches[{index}] must be an object")
        rest_chain, chain_length_source = _resolve_reach_chain(armature, reach, claimed)
        for bone in rest_chain:
            claimed[bone.name] = index
        hinge = _validated_reach_hinge(reach, rest_chain)
        keys = reach.get("keys") or []
        if not keys:
            raise ValueError(f"reaches[{index}] ('{rest_chain[0].name}') must supply at least one key")
        frames = {}
        for key_index, key in enumerate(keys):
            label = f"reaches[{index}].keys[{key_index}]"
            frame = _finite(key.get("frame"), f"{label}.frame")
            if frame in frames:
                raise ValueError(f"{label}: frame {frame} is keyed twice in one reach")
            target_object = key.get("target_object_name")
            if target_object is not None and bpy.data.objects.get(target_object) is None:
                raise ValueError(f"{label}: target object not found: {target_object}")
            if target_object is None and key.get("target_point") is None:
                raise ValueError(f"{label} must supply exactly one of target_point or target_object_name")
            frames[frame] = {
                key_name: key[key_name] for key_name in ("target_point", "target_object_name") if key_name in key
            }
        spec = {name: value for name, value in reach.items() if name != "keys"}
        prepared.append(
            {
                "spec": spec,
                "rest_chain": rest_chain,
                "chain_length_source": chain_length_source,
                "hinge": hinge,
                "keys": frames,
            }
        )
    return prepared


def _solved_reach_frame(armature, prepared, frame, tolerance_m):
    """
    Move the playhead to one frame and solve every reach that has a key there.

    The playhead move is what makes the solve correct: the chain's parents - the hips, the
    root - then hold whatever the action already says at this frame, so the IK solves against
    the body's real position rather than against frame one's.

    Args:
        armature: The armature being keyed.
        prepared: `_prepared_keyed_reaches` output.
        frame: The frame to solve, subframe included.
        tolerance_m: The convergence tolerance, in metres.

    Returns:
        tuple: `captured` (pose-space matrices per bone) and a list of
        `(reach_index, ReachSolution)` for the reaches keyed at this frame.

    """
    _place_playhead(bpy.context.scene, frame)
    # The solve reads the evaluated parent pose, and `frame_set` alone does not guarantee the
    # depsgraph has caught up with an action assigned moments ago in this same call.
    bpy.context.view_layer.update()
    captured = {}
    solved = []
    for index, entry in enumerate(prepared):
        key = entry["keys"].get(frame)
        if key is None:
            continue
        solution = _solve_one_reach(
            armature,
            {**entry["spec"], **key},
            entry["rest_chain"],
            entry["chain_length_source"],
            entry["hinge"],
            tolerance_m,
        )
        captured.update(solution.captured)
        solved.append((index, solution))
    return captured, solved


def _reach_frame_record(solution, frame):
    """
    Narrow one frame's solve to what the reply reports per frame.

    Args:
        solution: One `ReachSolution`.
        frame: The frame it was solved at.

    Returns:
        dict: frame, target_world, tail_world, achieved_error_m, converged, chain_reach_m,
        target_distance_m and out_of_reach.

    """
    measured = solution.measurements
    return {
        "frame": frame,
        "target_world": measured["target_world"],
        "tail_world": measured["tail_world"],
        "achieved_error_m": measured["achieved_error_m"],
        "converged": solution.converged,
        "chain_reach_m": measured["chain_reach_m"],
        "target_distance_m": measured["target_distance_m"],
        "out_of_reach": solution.out_of_reach,
    }


def _keyed_reach_warning(solution, frame, tolerance_m):
    """
    Name the frame a reach missed its tolerance on.

    Args:
        solution: One `ReachSolution`.
        frame: The frame it was solved at.
        tolerance_m: The tolerance it was judged against.

    Returns:
        str | None: The frame-prefixed notice, or None when that frame converged.

    """
    warning = _reach_convergence_warning(solution, tolerance_m)
    return None if warning is None else f"Frame {frame:g}: {warning}"


def _solved_reach_record(solution, by_bone):
    """
    Report one immediately-applied reach: what it achieved, and the bones it left posed.

    Args:
        solution: One `ReachSolution`.
        by_bone: `_apply_captured_matrices`' records, keyed by bone name.

    Returns:
        dict: tip_bone, chain_bones, chain_length, chain_length_source, pole_source, the
        world measurements, converged, out_of_reach and this chain's per-bone records.

    """
    return {
        "tip_bone": solution.chain[0].name,
        "chain_bones": [bone.name for bone in solution.chain],
        "chain_length": len(solution.chain),
        "chain_length_source": solution.chain_length_source,
        "pole_source": solution.pole_source,
        **solution.measurements,
        "converged": solution.converged,
        "out_of_reach": solution.out_of_reach,
        "bones": [by_bone[bone.name] for bone in solution.chain],
    }


def _keyed_reach_record(entry, solved):
    """
    Report one keyed reach: its resolved chain, and what each of its frames achieved.

    Args:
        entry: The `_prepared_keyed_reaches` record for this reach.
        solved: `(frame, ReachSolution)` for every frame this reach was solved at.

    Returns:
        dict: tip_bone, chain_bones, chain_length, chain_length_source, pole_source and
        keys - one `_reach_frame_record` per frame, in frame order.

    """
    rest_chain = entry["rest_chain"]
    return {
        "tip_bone": rest_chain[0].name,
        "chain_bones": [bone.name for bone in rest_chain],
        "chain_length": len(rest_chain),
        "chain_length_source": entry["chain_length_source"],
        # Every frame resolves the pole the same way, so the first frame's answer is the
        # reach's answer; a reach with no frames cannot happen (the preparation refuses it).
        "pole_source": solved[0][1].pole_source if solved else None,
        "keys": [_reach_frame_record(solution, frame) for frame, solution in solved],
    }


def _key_reach_frames(armature, action, prepared, keying_policy, style, tolerance_m, detail):
    """
    Solve and key every frame any reach asked for, in ascending order.

    Args:
        armature: The armature being keyed.
        action: The action every key lands in, already assigned to the rig.
        prepared: `_prepared_keyed_reaches` output.
        keying_policy: INSERT or REPLACE, passed through to `_write_pose_keys`.
        style: The `KeyStyle` every key this call writes is shaped with.
        tolerance_m: The convergence tolerance, in metres.
        detail: Whether to capture per-bone matrices at Blender's own precision.

    Returns:
        dict: frames (ascending), changed_keys, changed_bones, styled (how many points were
        styled), reaches (one record per requested reach) and warnings (one per frame that
        did not converge).

    """
    frames = sorted({frame for entry in prepared for frame in entry["keys"]})
    changed_keys = []
    changed_bones = []
    styled = 0
    solved_by_reach = {index: [] for index in range(len(prepared))}
    for frame in frames:
        captured, solved = _solved_reach_frame(armature, prepared, frame, tolerance_m)
        specs, _records = _apply_captured_matrices(armature, captured, detail)
        written = _write_pose_keys(action, specs, frame, keying_policy)
        styled += _style_written_keys(action, written, style)
        changed_keys.extend(written)
        changed_bones.extend(name for name in captured if name not in changed_bones)
        for index, solution in solved:
            solved_by_reach[index].append((frame, solution))
    return {
        "frames": frames,
        "changed_keys": changed_keys,
        "changed_bones": changed_bones,
        "styled": styled,
        "reaches": [_keyed_reach_record(entry, solved_by_reach[index]) for index, entry in enumerate(prepared)],
        # A frame that missed says so in its own `converged` flag and here: the envelope
        # lifts warnings, so a caller reading only those still learns the foot did not land.
        "warnings": [
            warning
            for results in solved_by_reach.values()
            for frame, solution in results
            for warning in [_keyed_reach_warning(solution, frame, tolerance_m)]
            if warning is not None
        ],
    }


def _keyed_reach_reply(armature, animation, action, previous_action, keyed, tolerance_m, keying_policy):
    """
    Describe what one multi-frame reach keying call left behind.

    Args:
        armature: The keyed armature object.
        animation: Its `animation_data`, read for the assignment this call ended on.
        action: The action that was authored.
        previous_action: The action that drove the rig before, or None.
        keyed: `_key_reach_frames` output.
        tolerance_m: The tolerance every frame was judged against.
        keying_policy: The policy the caller asked for.

    Returns:
        dict: The handler reply, with one warning per frame that did not converge.

    """
    reply = _action_reply(
        armature,
        animation,
        action,
        previous_action,
        keying_policy,
        keyed["changed_bones"],
        keyed["changed_keys"],
        keyed["styled"],
    )
    reply["tolerance_m"] = tolerance_m
    reply["keyed_frames"] = keyed["frames"]
    reply["reaches"] = keyed["reaches"]
    reply["warnings"] = keyed["warnings"]
    return reply


class PoseAnimationHandlersMixin:
    """Apply pose-space transforms and author named animation actions."""

    def list_character_bones(self, armature_object_name, limit=100, offset=0, rest_axes=False, bone_names=None):
        """Page the armature's rest bones with their parent, deform flag and optional rest axes."""
        armature = _armature_object(armature_object_name)
        _validate_limit_offset(limit, offset, _MAX_BONE_PAGE, "bone")
        # Rest-bone names, parents and deform flags are edited in Edit Mode, which keeps its own
        # copy of the armature until it exits; flush it rather than report stale bones.
        sync_from_editmode(armature)
        bones = _selected_bones(armature, bone_names)
        start, end, truncated, next_offset = paginate(len(bones), offset, limit, _MAX_BONE_PAGE)
        items = []
        for bone in bones[start:end]:
            item = {
                "name": bone.name,
                "parent": getattr(bone.parent, "name", None),
                "deform": bool(bone.use_deform),
            }
            if rest_axes:
                item["rest_axes"] = _rest_axes(bone)
                # The conclusions the nine numbers leave to the reader, and the ones the reply
                # cannot otherwise support: they turn on the rig's object matrix, not on them.
                # `up_axis` is the "+Z" entry, read off the same derivation rather than beside
                # it, so the two can never disagree about which way this bone stands.
                aim_axes = _rest_aim_axes(armature, bone)
                item["up_axis"] = aim_axes["+Z"]
                item["aim_axis_for_world"] = aim_axes
            items.append(item)
        reply = {"armature_object": armature.name}
        if rest_axes:
            reply["length_axis"] = _LENGTH_AXIS
        reply["bones"] = {
            "items": items,
            "total": len(bones),
            "offset": start,
            "limit": limit,
            "truncated": truncated,
            "next_offset": next_offset,
        }
        return reply

    def set_character_pose(
        self,
        armature_object_name,
        poses,
        space="LOCAL",
        reset_unspecified=False,
        confirm_reset_unspecified=False,
        detail=False,
    ):
        armature = _posable_armature(armature_object_name, "set a character pose")
        if reset_unspecified and not confirm_reset_unspecified:
            raise ValueError("confirm_reset_unspecified=True is required")
        prepared = _validate_pose_specs(armature, list(poses or ()), space)
        # A reset touches every bone, so every bone is what has to be handed back if the call
        # refuses part way through.
        affected = (
            [bone.name for bone in armature.pose.bones]
            if reset_unspecified
            else [pose_bone.name for pose_bone, _spec, _matrix in prepared]
        )
        custom_properties = {
            pose_bone.name: tuple(spec.get("custom_properties", {})) for pose_bone, spec, _matrix in prepared
        }
        # The pose is this tool's deliverable, so a call that succeeds keeps it.
        with restored_bone_pose(armature, affected, custom_properties, only_on_error=True):
            records = _apply_pose_specs(armature, prepared, space, reset_unspecified, detail)
        return {
            "armature_object": armature.name,
            "space": space,
            # Complete, and cheap enough to stay complete: the per-bone records are what the
            # reply budget shortens, so this is what still names every bone the call posed.
            "changed_bones": [record["bone"] for record in records],
            "bones": records,
            "warnings": _bare_write_warnings(armature, custom_properties) + _inert_rotation_warnings(prepared, space),
            "changed_objects": [armature.name],
        }

    def solve_bone_reach(self, armature_object_name, reaches, tolerance_m=_DEFAULT_REACH_TOLERANCE_M, detail=False):
        """Bend one or more unbranched chains so each tip_bone's tail reaches a point."""
        tolerance_m = _validated_tolerance(tolerance_m)
        armature = _posable_armature(armature_object_name, "solve a bone reach")
        if not reaches:
            raise ValueError("At least one reach entry is required")
        captured = {}
        solutions = []
        for reach in reaches:
            # `captured` doubles as the claim register: a chain overlapping an earlier reach's
            # is refused here rather than letting the later solve silently win.
            rest_chain, chain_length_source = _resolve_reach_chain(armature, reach, captured)
            hinge = _validated_reach_hinge(reach, rest_chain)
            solution = _solve_one_reach(armature, reach, rest_chain, chain_length_source, hinge, tolerance_m)
            captured.update(solution.captured)
            solutions.append(solution)
        _specs, records = _apply_captured_matrices(armature, captured, detail)
        by_bone = {record["bone"]: record for record in records}
        return {
            "armature_object": armature.name,
            "tolerance_m": tolerance_m,
            "changed_bones": [record["bone"] for record in records],
            "reaches": [_solved_reach_record(solution, by_bone) for solution in solutions],
            "changed_objects": [armature.name],
            # A reach that missed says so here as well as in its own `converged` flag: the
            # envelope lifts these, so a caller reading only the warnings still sees it.
            "warnings": [
                warning
                for warning in (_reach_convergence_warning(solution, tolerance_m) for solution in solutions)
                if warning is not None
            ],
        }

    def keyframe_character_pose(
        self,
        armature_object_name,
        action_name,
        frame=None,
        poses=None,
        keys=None,
        space="LOCAL",
        keying_policy="INSERT",
        interpolation="BEZIER",
        handle_left="AUTO_CLAMPED",
        handle_right="AUTO_CLAMPED",
        easing=None,
        action_policy="ENSURE",
        confirm_displace_action=False,
        action_slot_identifier=None,
        detail=False,
    ):
        """Pose and key one frame, or every frame of a stride, into one named action."""
        armature = _armature_object(armature_object_name)
        requests, batched = _pose_key_requests(frame, poses, keys)
        prepared_frames = [(at, _validate_pose_specs(armature, entries, space)) for at, entries in requests]
        style = KeyStyle(interpolation, handle_left, handle_right, easing)
        # Every frame is checked before the first key is written: a bone that does not exist at
        # frame 20 must not leave frames 1-19 keyed, an action created and the rig moved onto it.
        _refuse_unkeyable_request(
            keying_policy,
            style,
            action_policy,
            action_name,
            [entry for _at, prepared in prepared_frames for entry in prepared],
        )
        changed_bones = _keyed_bone_names(prepared_frames)
        custom_properties = _keyed_custom_properties(prepared_frames)
        scene = bpy.context.scene
        animation = armature.animation_data_create()
        with (
            restored_playhead(scene),
            restored_action_assignment(animation) as previous_action,
            restored_bone_pose(armature, changed_bones, custom_properties),
        ):
            action = assign_named_action(
                armature,
                action_name,
                action_policy,
                action_slot_identifier,
                confirm_displace=confirm_displace_action,
            )
            keyed = _key_pose_frames(
                armature,
                action,
                prepared_frames,
                space,
                keying_policy,
                style,
                detail=detail,
                report_frames=batched,
            )
        return _keyframe_reply(
            armature,
            animation,
            action,
            previous_action,
            keying_policy=keying_policy,
            changed_bones=changed_bones,
            keyed=keyed,
            warnings=keyed["warnings"],
            detail=detail,
        )

    def keyframe_bone_reach(
        self,
        armature_object_name,
        action_name,
        reaches,
        tolerance_m=_DEFAULT_REACH_TOLERANCE_M,
        keying_policy="REPLACE",
        interpolation="BEZIER",
        handle_left="AUTO_CLAMPED",
        handle_right="AUTO_CLAMPED",
        easing=None,
        action_policy="ENSURE",
        confirm_displace_action=False,
        action_slot_identifier=None,
        detail=False,
    ):
        """Solve each reach at each of its frames against the evaluated body pose, and key it."""
        armature = _posable_armature(armature_object_name, "key a bone reach")
        tolerance_m = _validated_tolerance(tolerance_m)
        if keying_policy not in {"INSERT", "REPLACE"}:
            raise ValueError("keying_policy must be INSERT or REPLACE; use keyframe_character_pose to remove keys")
        style = KeyStyle(interpolation, handle_left, handle_right, easing)
        style.validate()
        prepared = _prepared_keyed_reaches(armature, list(reaches or ()))
        scene = bpy.context.scene
        animation = armature.animation_data_create()
        with (
            restored_playhead(scene),
            restored_action_assignment(animation) as previous_action,
            restored_bone_pose(armature, [bone.name for entry in prepared for bone in entry["rest_chain"]]),
        ):
            action = assign_named_action(
                armature,
                action_name,
                action_policy,
                action_slot_identifier,
                confirm_displace=confirm_displace_action,
            )
            keyed = _key_reach_frames(armature, action, prepared, keying_policy, style, tolerance_m, detail)
        return _keyed_reach_reply(armature, animation, action, previous_action, keyed, tolerance_m, keying_policy)
