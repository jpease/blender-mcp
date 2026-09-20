"""
Blender handlers for deterministic pose application and keyframing.

Two rules shape this module. A bone's target is resolved inside the depth-sorted write loop,
not before it, because a pose-, world- or rest-space target for a child is only meaningful
against a parent this same call has already written. And a call that authors animation leaves
the action it authored assigned to the rig: an action nobody references carries zero users and
Blender drops it at save, so restoring a previous assignment would throw the work away.
"""

import contextlib
import math

import bpy
import mathutils

from ...helpers import paginate, sync_from_editmode
from .foundation import (
    _action_fcurve_collections,
    _armature_object,
    _bone_path_token,
    _finite,
    _matrix_list,
    _required_name,
    _validate_limit_offset,
    _vector,
)

_POSE_SPACES = {"LOCAL", "LOCAL_WITH_PARENT", "POSE", "WORLD"}
# LOCAL is the only pose space that is relative to the parent: it is `pose_bone.matrix_basis`,
# the channel delta from rest. Every other space states an absolute placement in the armature
# (or the scene), so a child's target has to be built after its parent has moved.
_PARENT_RELATIVE_SPACE = "LOCAL"
# A page an agent can read in full without spending its context on a whole rig; the heaviest
# production rigs here carry 187-238 bones, so two pages cover one.
_MAX_BONE_PAGE = 200

_AXIS_INDEX = {"X": 0, "Y": 1, "Z": 2}
_SIGNED_AXIS_SIGNS = {"X": 1.0, "-X": -1.0, "Y": 1.0, "-Y": -1.0, "Z": 1.0, "-Z": -1.0}
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


def _validated_aim(aim, bone_name):
    """
    Check one `aim_at` record and resolve it to the values the write loop needs.

    The target object is looked up now so a missing object fails before any bone moves, but its
    world origin is read at apply time, after the pose the same call authored has settled.

    Args:
        aim: The raw `aim_at` record from a pose entry.
        bone_name: The bone it applies to, for error messages.

    Returns:
        dict: `target` (a world-space vector or None), `target_object` (an object or None),
        `track` from `_signed_axis`, and `up` as `(axis letter, sign, world reference)` or None
        for a minimal-arc aim.

    Raises:
        ValueError: If the record names neither or both target forms, names an object that does
            not exist, spells an axis wrongly, points `up_axis` at the tracked axis, or gives a
            zero up reference.

    """
    if not isinstance(aim, dict):
        raise ValueError(f"aim_at for '{bone_name}' must be an object")
    target = aim.get("target")
    target_name = aim.get("target_object")
    if (target is None) == (target_name is None):
        raise ValueError(f"aim_at for '{bone_name}' requires exactly one of target or target_object")
    target_object = None
    point = None
    if target_name is not None:
        target_object = bpy.data.objects.get(_required_name(target_name, "aim_at.target_object"))
        if target_object is None:
            raise ValueError(f"aim_at.target_object not found: {target_name}")
    else:
        point = _vector(target, f"aim_at.target for '{bone_name}'")
    track_letter, track_sign = _signed_axis(aim.get("track_axis"), f"aim_at.track_axis for '{bone_name}'")
    up = None
    if aim.get("up_axis") is not None:
        up_letter, up_sign = _signed_axis(aim["up_axis"], f"aim_at.up_axis for '{bone_name}'")
        if up_letter == track_letter:
            raise ValueError(
                f"aim_at.up_axis for '{bone_name}' must name a different bone axis than track_axis; "
                f"'{aim['up_axis']}' and '{aim['track_axis']}' are both the {up_letter} axis"
            )
        reference = _vector(aim.get("up_reference") or _DEFAULT_UP_REFERENCE, f"aim_at.up_reference for '{bone_name}'")
        if reference.length <= _AIM_MIN_LENGTH:
            raise ValueError(f"aim_at.up_reference for '{bone_name}' is a zero vector; roll is undefined")
        up = (up_letter, up_sign, reference)
    return {"target": point, "target_object": target_object, "track": (track_letter, track_sign), "up": up}


def _validated_rotate(rotate, bone_name):
    """
    Check one `rotate` record and resolve its axis and angle.

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
    target = aim["target_object"].matrix_world.translation if aim["target_object"] is not None else aim["target"]
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
            spec["aim_at"] = _validated_aim(spec["aim_at"], pose_bone.name)
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
        list: The X, then Y, then Z columns of `matrix_local` - the bone's rest axes in armature
        space - rounded to `_REST_AXIS_DECIMALS`.

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


def _assign_named_action(armature, action_name, policy, slot_identifier=None):
    action = bpy.data.actions.get(action_name)
    if action is None:
        if policy == "REUSE":
            raise ValueError(f"Action not found: {action_name}")
        action = bpy.data.actions.new(action_name)
    elif policy == "CREATE":
        raise ValueError(f"Action already exists: {action_name}")
    animation = armature.animation_data_create()
    animation.action = action
    slots = list(getattr(action, "slots", ()))
    if slot_identifier is not None:
        slot = next((candidate for candidate in slots if candidate.identifier == slot_identifier), None)
        if slot is None:
            raise ValueError(f"Action slot not found on '{action.name}': {slot_identifier}")
        animation.action_slot = slot
    elif slots:
        suitable = list(getattr(animation, "action_suitable_slots", ()))
        if len(suitable) == 1:
            animation.action_slot = suitable[0]
        elif len(suitable) > 1:
            raise ValueError(f"Action '{action.name}' has multiple suitable slots; action_slot_identifier is required")
        elif len(slots) == 1:
            animation.action_slot = slots[0]
        else:
            raise ValueError(f"Action '{action.name}' has multiple slots; action_slot_identifier is required")
    return action


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
    for collection in _action_fcurve_collections(action):
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


def _set_action_interpolation(action, frame, interpolation):
    changed = 0
    for collection in _action_fcurve_collections(action):
        for curve in collection:
            for point in curve.keyframe_points:
                if math.isclose(float(point.co[0]), float(frame), abs_tol=_FRAME_TOLERANCE):
                    point.interpolation = interpolation
                    changed += 1
    return changed


def _refuse_unkeyable_request(keying_policy, interpolation, action_policy, prepared):
    """
    Reject a keying request before an action is created or a bone is moved.

    The aim check is here rather than in `_validate_pose_specs` because it is about keying, not
    about posing: a minimal-arc aim keeps whatever roll the bone already holds, so the same call
    at two frames keys two different rolls. `set_character_pose` may do that; an action may not.

    Args:
        keying_policy: INSERT, REPLACE or REMOVE.
        interpolation: CONSTANT, LINEAR or BEZIER.
        action_policy: CREATE or REUSE.
        prepared: `_validate_pose_specs` output, read for its bone names and specs.

    Raises:
        ValueError: If a policy is unknown, a removal would need an action it cannot reuse, or
            an aim would key an undefined roll.

    """
    if keying_policy not in {"INSERT", "REPLACE", "REMOVE"}:
        raise ValueError("keying_policy must be INSERT, REPLACE, or REMOVE")
    if keying_policy == "REMOVE" and action_policy != "REUSE":
        raise ValueError("Removing keys requires action_policy='REUSE'")
    if interpolation not in {"CONSTANT", "LINEAR", "BEZIER"}:
        raise ValueError("Unsupported interpolation")
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


def _keyframe_reply(
    armature,
    animation,
    action,
    previous_name,
    *,
    keying_policy,
    prepared,
    changed_keys,
    interpolation_count,
    pose_records,
):
    """
    Describe what one keying call left behind, assignment included.

    Args:
        armature: The posed armature object.
        animation: Its `animation_data`, read for the assignment this call ended on.
        action: The action that was authored.
        previous_name: The action that drove the rig before, or None.
        keying_policy: The policy the caller asked for.
        prepared: `_validate_pose_specs` output, read for the posed bone names.
        changed_keys: One record per channel keyed.
        interpolation_count: How many keys had their interpolation set.
        pose_records: Per-bone matrices for a `detail` request, or None to omit them.

    Returns:
        dict: The handler reply.

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
        "changed_bones": [pose_bone.name for pose_bone, _spec, _matrix in prepared],
        "changed_keys": changed_keys,
        "interpolation_updates": interpolation_count,
        "changed_objects": [armature.name],
        "changed_resources": [{"type": "ACTION", "name": action.name}],
    }
    if previous_name is not None and previous_name != assigned_name:
        # The rig was driven by something else; say what this call displaced.
        reply["unassigned_action"] = previous_name
    if pose_records is not None:
        # The pose is restored before this returns, so these matrices describe what was keyed at
        # the requested frame, not what the rig is holding now.
        reply["bones"] = pose_records
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


def _selected_bones(armature, bone_names):
    """
    Narrow an armature's rest bones to the ones the caller named, in armature order.

    Reading three bones' rest axes off a 187-bone rig otherwise costs six paginated calls,
    because `rest_axes` spends the reply budget at roughly 22 bones a page. A name that does
    not exist is refused rather than silently omitted: a caller asking for three bones and
    receiving two would pose the wrong one.

    Args:
        armature: The armature object.
        bone_names: Exact bone names to keep, or None for every bone.

    Returns:
        list: The matching `bpy.types.Bone`s, in armature order, so paging a filtered list
        behaves exactly like paging an unfiltered one.

    Raises:
        ValueError: When `bone_names` is not a list of 1 to `_MAX_BONE_PAGE` non-empty
            strings, or names a bone this armature does not have.

    """
    bones = list(armature.data.bones)
    if bone_names is None:
        return bones
    if not isinstance(bone_names, list) or not 1 <= len(bone_names) <= _MAX_BONE_PAGE:
        raise ValueError(f"bone_names must be a list of 1 to {_MAX_BONE_PAGE} bone names")
    wanted = []
    for name in bone_names:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("each bone_names entry must be a non-empty string")
        wanted.append(name.strip())
    present = {bone.name for bone in bones}
    missing = sorted({name for name in wanted if name not in present})
    if missing:
        raise ValueError(f"Bones not found in armature '{armature.name}': {missing}")
    requested = set(wanted)
    return [bone for bone in bones if bone.name in requested]


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
            items.append(item)
        return {
            "armature_object": armature.name,
            "bones": {
                "items": items,
                "total": len(bones),
                "offset": start,
                "limit": limit,
                "truncated": truncated,
                "next_offset": next_offset,
            },
        }

    def set_character_pose(
        self,
        armature_object_name,
        poses,
        space="LOCAL",
        reset_unspecified=False,
        confirm_reset_unspecified=False,
        detail=False,
    ):
        armature = _armature_object(armature_object_name)
        if armature.data.pose_position != "POSE":
            raise ValueError("Armature must use pose_position='POSE' to set a character pose")
        if reset_unspecified and not confirm_reset_unspecified:
            raise ValueError("confirm_reset_unspecified=True is required")
        prepared = _validate_pose_specs(armature, list(poses or ()), space)
        affected = list(armature.pose.bones) if reset_unspecified else [item[0] for item in prepared]
        matrices = {bone.name: bone.matrix_basis.copy() for bone in affected}
        properties = {}
        for bone in affected:
            properties[bone.name] = {
                name: bone[name]
                for prepared_bone, spec, _matrix in prepared
                if prepared_bone == bone
                for name in spec.get("custom_properties", {})
            }
        try:
            records = _apply_pose_specs(armature, prepared, space, reset_unspecified, detail)
        except Exception:
            for bone in affected:
                bone.matrix_basis = matrices[bone.name]
                for name, value in properties[bone.name].items():
                    bone[name] = value
            raise
        return {
            "armature_object": armature.name,
            "space": space,
            # Complete, and cheap enough to stay complete: the per-bone records are what the
            # reply budget shortens, so this is what still names every bone the call posed.
            "changed_bones": [record["bone"] for record in records],
            "bones": records,
            "changed_objects": [armature.name],
        }

    def keyframe_character_pose(
        self,
        armature_object_name,
        action_name,
        frame,
        poses,
        space="LOCAL",
        keying_policy="INSERT",
        interpolation="BEZIER",
        action_policy="CREATE",
        action_slot_identifier=None,
        detail=False,
    ):
        armature = _armature_object(armature_object_name)
        frame = _finite(frame, "frame")
        prepared = _validate_pose_specs(armature, list(poses or ()), space)
        _refuse_unkeyable_request(keying_policy, interpolation, action_policy, prepared)
        scene = bpy.context.scene
        animation = armature.animation_data_create()
        previous_action = animation.action
        previous_name = getattr(previous_action, "name", None)
        previous_slot = getattr(animation, "action_slot", None)
        previous_frame = scene.frame_current
        matrices = {bone.name: bone.matrix_basis.copy() for bone, _spec, _matrix in prepared}
        action = _assign_named_action(armature, action_name, action_policy, action_slot_identifier)
        keyed = False
        try:
            _place_playhead(scene, frame)
            pose_records = []
            if keying_policy != "REMOVE":
                pose_records = _apply_pose_specs(armature, prepared, space, detail=detail)
            changed_keys = _write_pose_keys(action, prepared, frame, keying_policy)
            interpolation_count = (
                0 if keying_policy == "REMOVE" else _set_action_interpolation(action, frame, interpolation)
            )
            keyed = True
        finally:
            for pose_bone, _spec, _matrix in prepared:
                pose_bone.matrix_basis = matrices[pose_bone.name]
            if not keyed:
                # Nothing was authored, so hand the rig back exactly as it arrived.
                animation.action = previous_action
                if previous_action is not None and previous_slot is not None:
                    with contextlib.suppress(Exception):
                        animation.action_slot = previous_slot
            # Restoring the pose happens before the playhead moves back, so an assigned action
            # drives the bones from here: the rig holds animation, not a stranded pose.
            scene.frame_set(previous_frame)
            bpy.context.view_layer.update()
        return _keyframe_reply(
            armature,
            animation,
            action,
            previous_name,
            keying_policy=keying_policy,
            prepared=prepared,
            changed_keys=changed_keys,
            interpolation_count=interpolation_count,
            pose_records=pose_records if detail else None,
        )
