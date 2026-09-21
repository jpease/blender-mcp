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
import uuid

import bpy
import mathutils

from ...helpers import paginate, sync_from_editmode
from ..action_assignment import action_fcurve_collections, assign_named_action
from ..key_style import style_point, validate_key_style
from .foundation import (
    _MAX_BONE_PAGE,
    _armature_object,
    _bone_path_token,
    _finite,
    _matrix_list,
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


def _style_written_keys(action, changed_keys, interpolation, handle_left, handle_right, easing):
    """
    Style only the points this call wrote, named by (bone, data_path, frame).

    The previous implementation walked every F-Curve in the action and restyled anything
    sharing the frame. `keyframe_character_pose` tells callers to put object root motion in
    this same action, so keying one bone at frame 12 silently rewrote the root's keys at
    frame 12 as well - a tool changing data it was not asked to change.

    Args:
        action: The action just keyed.
        changed_keys: `_write_pose_keys`' records: bone, data_path and frame.
        interpolation: The interpolation to apply.
        handle_left: The left Bézier handle type.
        handle_right: The right Bézier handle type.
        easing: The easing direction, or None to leave Blender's.

    Returns:
        int: How many keyframe points were styled.

    """
    wanted = {}
    for record in changed_keys:
        # A channel path joins with a dot; a custom property's path is already a `["name"]`
        # subscript and concatenates directly, the same two spellings `_pose_key_paths` emits.
        token = _bone_path_token(record["bone"])
        separator = "" if record["data_path"].startswith("[") else "."
        wanted.setdefault(f"{token}{separator}{record['data_path']}", []).append(float(record["frame"]))
    changed = 0
    for collection in action_fcurve_collections(action):
        for curve in collection:
            frames = wanted.get(curve.data_path)
            if frames is None:
                continue
            for point in curve.keyframe_points:
                if any(math.isclose(float(point.co[0]), frame, abs_tol=_FRAME_TOLERANCE) for frame in frames):
                    style_point(point, interpolation, handle_left=handle_left, handle_right=handle_right, easing=easing)
                    changed += 1
    return changed


def _refuse_unkeyable_request(keying_policy, style, action_policy, action_name, prepared):
    """
    Reject a keying request before an action is created or a bone is moved.

    The aim check is here rather than in `_validate_pose_specs` because it is about keying, not
    about posing: a minimal-arc aim keeps whatever roll the bone already holds, so the same call
    at two frames keys two different rolls. `set_character_pose` may do that; an action may not.

    Args:
        keying_policy: INSERT, REPLACE or REMOVE.
        style: The requested `(interpolation, handle_left, handle_right, easing)`.
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
    validate_key_style(*style)
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
    previous_action,
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
        previous_action: The action that drove the rig before, or None.
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
    previous_name = getattr(previous_action, "name", None)
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
        raise ValueError(f"'{chain[0].name}' chain root and tip coincide; supply pole_target explicitly")
    axis = axis.normalized()
    joints = [tip_tail, *(bone.head_local for bone in chain)]
    mid = joints[len(joints) // 2]
    offset = mid - root_head
    projected = offset - axis * offset.dot(axis)
    if projected.length <= _AIM_MIN_RESIDUAL:
        raise ValueError(
            f"'{chain[0].name}' chain's rest pose is straight; no natural pole direction can "
            "be inferred - supply pole_target or pole_target_object"
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
            target/target_object, and solve_bone_reach's own pole handling, for pole_target/
            pole_target_object).
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
    pole_point = reach.get("pole_target")
    pole_object_name = reach.get("pole_target_object")
    if pole_object_name is not None or pole_point is not None:
        pole_obj, pole_is_temp = _resolved_reach_target(pole_point, pole_object_name, "pole_target")
        return pole_obj, pole_is_temp, "explicit"
    return _reach_helper_object(tuple(_synthesize_pole(armature, rest_chain))), True, "resolved"


def _resolve_reach_geometry(armature, reach, rest_chain):
    """Resolve the reach's target and pole objects, and where the pole came from."""
    target_obj, target_is_temp = _resolved_reach_target(reach.get("target"), reach.get("target_object"), "target")
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


def _reach_convergence_warning(entry, tolerance_m):
    """
    Say why one reach missed its tolerance, separating an unreachable target from a stall.

    Args:
        entry: One `_solve_one_reach` result.
        tolerance_m: The convergence tolerance this call asked for, in metres.

    Returns:
        str | None: A notice naming the tip bone, the achieved error, the tolerance and the
        cause, or None when the reach converged and there is nothing to act on.

    """
    if entry["converged"]:
        return None
    missed = (
        f"Reach '{entry['tip_bone']}' did not converge: achieved_error_m "
        f"{entry['achieved_error_m']:.6g} exceeds tolerance_m {tolerance_m:.6g}"
    )
    if entry["out_of_reach"]:
        return (
            f"{missed}. The target is out of reach: target_distance_m "
            f"{entry['target_distance_m']:.6g} is beyond chain_reach_m {entry['chain_reach_m']:.6g}. "
            "Move the target closer, lengthen the chain with chain_length, or move the armature."
        )
    return (
        f"{missed}. The target is within the chain's range (target_distance_m "
        f"{entry['target_distance_m']:.6g} of chain_reach_m {entry['chain_reach_m']:.6g}), so the "
        "solve stalled short of it: raise iterations, supply a pole_target, or loosen tolerance_m."
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


def _applied_reach_hinge(armature, reach, rest_chain):
    """
    Constrain one chain bone to a single rotation axis for the duration of the solve.

    A knee has one axis and one sign; an unconstrained IK solver will invert it to save the
    solve an iteration, which is how a walk cycle ends up with a backwards leg. The limit is
    temporary: the caller's `finally` puts every value back.

    Args:
        armature: The armature being solved.
        reach: The reach entry, read for its optional `hinge`.
        rest_chain: The reach's resolved rest bones, tip first.

    Returns:
        tuple | None: The pose bone and the property values to restore, or None when the
        reach asked for no hinge.

    Raises:
        ValueError: If the hinge names a bone that is not in this reach's chain - limiting a
            bone the solve does not drive would silently do nothing.

    """
    validated = _validated_reach_hinge(reach, rest_chain)
    if validated is None:
        return None
    bone_name, axis, minimum, maximum = validated
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
    return pose_bone, restore


def _solve_one_reach(armature, reach, captured, tolerance_m):
    """
    Resolve, temporarily IK-solve, and capture one reach's chain into captured.

    Args:
        armature: The armature object being posed.
        reach: One validated BoneReach entry, as a dict.
        captured: `{pose_bone_name: pose_space_matrix}`, shared across every reach in this
            call and extended in place with this reach's chain bones.
        tolerance_m: How close tip_bone's tail must land to the target to count as converged.

    Returns:
        dict: This reach's solver metadata - tip_bone, chain_bones, chain_length,
        chain_length_source, pole_source, target_world, head_world, tail_world,
        achieved_error_m, converged, chain_reach_m, target_distance_m and out_of_reach.
        Does not include "bones"; the caller adds that once every reach's captured matrices
        have been applied together.

    Raises:
        ValueError: If tip_bone is unknown, chain_length is explicit but exceeds tip_bone's
            ancestors, this reach's chain overlaps an earlier reach's, an explicit target or
            pole names an object that does not exist, or an omitted pole cannot be
            synthesized from a straight or degenerate rest chain.

    """
    rest_chain, chain_length_source = _resolve_reach_chain(armature, reach, captured)
    target_obj, target_is_temp, pole_obj, pole_is_temp, pole_source = _resolve_reach_geometry(
        armature, reach, rest_chain
    )
    tip_pose_bone = armature.pose.bones[rest_chain[0].name]
    constraint = None
    hinge_restore = None
    try:
        hinge_restore = _applied_reach_hinge(armature, reach, rest_chain)
        constraint = _configured_reach_constraint(tip_pose_bone, reach, target_obj, pole_obj, len(rest_chain))
        bpy.context.view_layer.update()
        for rest_bone in rest_chain:
            captured[rest_bone.name] = _matrix_list(armature.pose.bones[rest_bone.name].matrix)
        measured = _reach_measurements(armature, rest_chain, tip_pose_bone, target_obj)
    finally:
        if constraint is not None:
            tip_pose_bone.constraints.remove(constraint)
        if hinge_restore is not None:
            hinge_bone, values = hinge_restore
            for field, value in values.items():
                setattr(hinge_bone, field, value)
        if target_is_temp:
            bpy.data.objects.remove(target_obj, do_unlink=True)
        if pole_is_temp:
            bpy.data.objects.remove(pole_obj, do_unlink=True)
        bpy.context.view_layer.update()
    return {
        "tip_bone": rest_chain[0].name,
        "chain_bones": [bone.name for bone in rest_chain],
        "chain_length": len(rest_chain),
        "chain_length_source": chain_length_source,
        "pole_source": pole_source,
        **measured,
        "converged": measured["achieved_error_m"] <= tolerance_m,
        "out_of_reach": measured["target_distance_m"] > measured["chain_reach_m"],
    }


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
        `rest_chain`, `chain_length_source`, and `keys`, mapping frame to that frame's
        target fields.

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
        _validated_reach_hinge(reach, rest_chain)
        keys = reach.get("keys") or []
        if not keys:
            raise ValueError(f"reaches[{index}] ('{rest_chain[0].name}') must supply at least one key")
        frames = {}
        for key_index, key in enumerate(keys):
            label = f"reaches[{index}].keys[{key_index}]"
            frame = _finite(key.get("frame"), f"{label}.frame")
            if frame in frames:
                raise ValueError(f"{label}: frame {frame} is keyed twice in one reach")
            target_object = key.get("target_object")
            if target_object is not None and bpy.data.objects.get(target_object) is None:
                raise ValueError(f"{label}: target object not found: {target_object}")
            if target_object is None and key.get("target") is None:
                raise ValueError(f"{label} must supply exactly one of target or target_object")
            frames[frame] = {key_name: key[key_name] for key_name in ("target", "target_object") if key_name in key}
        spec = {name: value for name, value in reach.items() if name != "keys"}
        prepared.append(
            {
                "spec": spec,
                "rest_chain": rest_chain,
                "chain_length_source": chain_length_source,
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
        `(reach_index, solver_result)` for the reaches keyed at this frame.

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
        solved.append((index, _solve_one_reach(armature, {**entry["spec"], **key}, captured, tolerance_m)))
    return captured, solved


def _reach_frame_record(result, frame):
    """
    Narrow one frame's solver result to what the reply reports per frame.

    Args:
        result: One `_solve_one_reach` result.
        frame: The frame it was solved at.

    Returns:
        dict: frame, target_world, tail_world, achieved_error_m, converged, chain_reach_m,
        target_distance_m and out_of_reach.

    """
    return {
        "frame": frame,
        "target_world": result["target_world"],
        "tail_world": result["tail_world"],
        "achieved_error_m": result["achieved_error_m"],
        "converged": result["converged"],
        "chain_reach_m": result["chain_reach_m"],
        "target_distance_m": result["target_distance_m"],
        "out_of_reach": result["out_of_reach"],
    }


def _keyed_reach_warning(result, frame, tolerance_m):
    """
    Name the frame a reach missed its tolerance on.

    Args:
        result: One `_solve_one_reach` result.
        frame: The frame it was solved at.
        tolerance_m: The tolerance it was judged against.

    Returns:
        str | None: The frame-prefixed notice, or None when that frame converged.

    """
    warning = _reach_convergence_warning(result, tolerance_m)
    return None if warning is None else f"Frame {frame:g}: {warning}"


def _keyed_reach_record(entry, solved):
    """
    Report one reach: its resolved chain, and what each of its frames achieved.

    Args:
        entry: The `_prepared_keyed_reaches` record for this reach.
        solved: `(frame, solver_result)` for every frame this reach was solved at.

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
        "pole_source": solved[0][1]["pole_source"] if solved else None,
        "keys": [_reach_frame_record(result, frame) for frame, result in solved],
    }


def _key_reach_frames(armature, action, prepared, keying_policy, style, tolerance_m, detail):
    """
    Solve and key every frame any reach asked for, in ascending order.

    Args:
        armature: The armature being keyed.
        action: The action every key lands in, already assigned to the rig.
        prepared: `_prepared_keyed_reaches` output.
        keying_policy: INSERT or REPLACE, passed through to `_write_pose_keys`.
        style: `(interpolation, handle_left, handle_right, easing)` for every key written.
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
        specs = _validate_pose_specs(
            armature, [{"bone_name": name, "matrix": matrix} for name, matrix in captured.items()], "POSE"
        )
        _apply_pose_specs(armature, specs, "POSE", detail=detail)
        written = _write_pose_keys(action, specs, frame, keying_policy)
        styled += _style_written_keys(action, written, *style)
        changed_keys.extend(written)
        changed_bones.extend(name for name in captured if name not in changed_bones)
        for index, result in solved:
            solved_by_reach[index].append((frame, result))
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
            for frame, result in results
            for warning in [_keyed_reach_warning(result, frame, tolerance_m)]
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
    assigned_name = getattr(getattr(animation, "action", None), "name", None)
    previous_name = getattr(previous_action, "name", None)
    reply = {
        "armature_object": armature.name,
        "action": action.name,
        "action_slot": getattr(getattr(animation, "action_slot", None), "identifier", None),
        "assigned_action": assigned_name,
        "tolerance_m": tolerance_m,
        "keying_policy": keying_policy,
        "changed_bones": keyed["changed_bones"],
        "keyed_frames": keyed["frames"],
        "changed_keys": keyed["changed_keys"],
        "interpolation_updates": keyed["styled"],
        "reaches": keyed["reaches"],
        "changed_objects": [armature.name],
        "changed_resources": [{"type": "ACTION", "name": action.name}],
        "warnings": keyed["warnings"],
    }
    if previous_name is not None and previous_name != assigned_name:
        reply["unassigned_action"] = previous_name
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

    def solve_bone_reach(self, armature_object_name, reaches, tolerance_m=_DEFAULT_REACH_TOLERANCE_M, detail=False):
        """Bend one or more unbranched chains so each tip_bone's tail reaches a point."""
        tolerance_m = _finite(tolerance_m, "tolerance_m")
        if tolerance_m <= 0.0:
            raise ValueError(f"tolerance_m must be greater than 0 metres, not {tolerance_m}")
        armature = _armature_object(armature_object_name)
        if armature.data.pose_position != "POSE":
            raise ValueError("Armature must use pose_position='POSE' to solve a bone reach")
        if not reaches:
            raise ValueError("At least one reach entry is required")
        captured = {}
        solver_info = [_solve_one_reach(armature, reach, captured, tolerance_m) for reach in reaches]
        poses = [{"bone_name": name, "matrix": matrix} for name, matrix in captured.items()]
        pose_reply = self.set_character_pose(armature_object_name, poses, space="POSE", detail=detail)
        by_bone = {record["bone"]: record for record in pose_reply["bones"]}
        for entry in solver_info:
            entry["bones"] = [by_bone[name] for name in entry["chain_bones"]]
        return {
            "armature_object": armature.name,
            "tolerance_m": tolerance_m,
            "changed_bones": pose_reply["changed_bones"],
            "reaches": solver_info,
            "changed_objects": [armature.name],
            # A reach that missed says so here as well as in its own `converged` flag: the
            # envelope lifts these, so a caller reading only the warnings still sees it.
            "warnings": [
                warning
                for warning in (_reach_convergence_warning(entry, tolerance_m) for entry in solver_info)
                if warning is not None
            ],
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
        handle_left="AUTO_CLAMPED",
        handle_right="AUTO_CLAMPED",
        easing=None,
        action_policy="ENSURE",
        confirm_displace_action=False,
        action_slot_identifier=None,
        detail=False,
    ):
        armature = _armature_object(armature_object_name)
        frame = _finite(frame, "frame")
        prepared = _validate_pose_specs(armature, list(poses or ()), space)
        style = (interpolation, handle_left, handle_right, easing)
        _refuse_unkeyable_request(keying_policy, style, action_policy, action_name, prepared)
        scene = bpy.context.scene
        animation = armature.animation_data_create()
        previous_action = animation.action
        previous_slot = getattr(animation, "action_slot", None)
        previous_frame = scene.frame_current
        matrices = {bone.name: bone.matrix_basis.copy() for bone, _spec, _matrix in prepared}
        action = assign_named_action(
            armature,
            action_name,
            action_policy,
            action_slot_identifier,
            confirm_displace=confirm_displace_action,
        )
        keyed = False
        try:
            _place_playhead(scene, frame)
            pose_records = []
            if keying_policy != "REMOVE":
                pose_records = _apply_pose_specs(armature, prepared, space, detail=detail)
            changed_keys = _write_pose_keys(action, prepared, frame, keying_policy)
            interpolation_count = 0 if keying_policy == "REMOVE" else _style_written_keys(action, changed_keys, *style)
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
            previous_action,
            keying_policy=keying_policy,
            prepared=prepared,
            changed_keys=changed_keys,
            interpolation_count=interpolation_count,
            pose_records=pose_records if detail else None,
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
        armature = _armature_object(armature_object_name)
        if armature.data.pose_position != "POSE":
            raise ValueError("Armature must use pose_position='POSE' to key a bone reach")
        tolerance_m = _finite(tolerance_m, "tolerance_m")
        if tolerance_m <= 0.0:
            raise ValueError(f"tolerance_m must be greater than 0 metres, not {tolerance_m}")
        if keying_policy not in {"INSERT", "REPLACE"}:
            raise ValueError("keying_policy must be INSERT or REPLACE; use keyframe_character_pose to remove keys")
        style = (interpolation, handle_left, handle_right, easing)
        validate_key_style(*style)
        prepared = _prepared_keyed_reaches(armature, list(reaches or ()))
        scene = bpy.context.scene
        animation = armature.animation_data_create()
        previous_action = animation.action
        previous_frame = scene.frame_current
        chain_names = [bone.name for entry in prepared for bone in entry["rest_chain"]]
        matrices = {name: armature.pose.bones[name].matrix_basis.copy() for name in chain_names}
        action = assign_named_action(
            armature,
            action_name,
            action_policy,
            action_slot_identifier,
            confirm_displace=confirm_displace_action,
        )
        try:
            keyed = _key_reach_frames(armature, action, prepared, keying_policy, style, tolerance_m, detail)
        finally:
            # The pose is handed back exactly as it arrived, then the playhead: from here the
            # assigned action drives the bones, so the rig shows its animation rather than
            # the last frame this call happened to solve.
            for name in chain_names:
                armature.pose.bones[name].matrix_basis = matrices[name]
            scene.frame_set(previous_frame)
            bpy.context.view_layer.update()
        return _keyed_reach_reply(armature, animation, action, previous_action, keyed, tolerance_m, keying_policy)
