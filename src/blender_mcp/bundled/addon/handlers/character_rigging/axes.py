"""
Which way a bone's own axes point, and the aim arithmetic built on them.

Blender builds every bone along its own local +Y whatever the rig's naming, so "which axis
swings this joint" is a question about the rest basis and not about the name - and getting it
wrong is silent: the call succeeds, the keys land, and the render shows a twist where a bend
was asked for. That arithmetic is collected here, away from the pose writing that consumes it,
because it is also what `list_character_bones` reports and what an aim resolves against, and
because it is pure: a matrix and a letter in, a matrix and a letter out, no rig state touched.
"""

import math

import bpy
import mathutils

from .primitives import _finite, _required_name, _vector

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


# The three unit axes, as plain tuples: a `mathutils.Vector` is built from one rather than
# zeroed and indexed into, because that keeps this free of in-place mutation.
_BASIS_VECTORS = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))


def _unit_axis_and_angle(axis, angle):
    """Normalize a rotation's axis, or report that it names no direction at all."""
    return (axis.normalized(), angle) if axis.length > _AIM_MIN_LENGTH else None
