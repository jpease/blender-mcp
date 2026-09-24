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

import bpy
import mathutils

from ...helpers import deforming_meshes, paginate, sync_from_editmode
from ..action_assignment import action_fcurve_collections, assign_named_action, cycled_curve_extent
from ..key_style import KeyStyle, style_point
from .axes import (
    _AIM_MIN_LENGTH,
    _AXIS_INDEX,
    _BASIS_VECTORS,
    _BONE_POSITIONS,
    _LENGTH_AXIS,
    _aim_pose_matrix,
    _reject_singular,
    _rest_aim_axes,
    _rest_axes,
    _signed_axis,
    _unit_axis_and_angle,
    _validated_aim,
    _validated_rotate,
)
from .primitives import (
    _MAX_BONE_PAGE,
    _armature_object,
    _bone_path_token,
    _finite,
    _matrix_list,
    _override_property_warning,
    _plain,
    _property_value_as_stored,
    _required_name,
    _selected_bones,
    _unique_names,
    _validate_limit_offset,
    _vector,
)
from .references import _mesh_uses_armature_data

_POSE_SPACES = {"LOCAL", "LOCAL_WITH_PARENT", "POSE", "WORLD"}
# LOCAL is the only pose space that is relative to the parent: it is `pose_bone.matrix_basis`,
# the channel delta from rest. Every other space states an absolute placement in the armature
# (or the scene), so a child's target has to be built after its parent has moved.
_PARENT_RELATIVE_SPACE = "LOCAL"

# Rotation channels by the number of F-Curves they occupy, used to read a previous key back.
_ROTATION_CHANNEL_WIDTH = {"rotation_quaternion": 4, "rotation_axis_angle": 4, "rotation_euler": 3}
# Frames compare as floats; a key at 12 and a request at 12.0000001 are the same key.
_FRAME_TOLERANCE = 1e-6
# One call may pose 500 bones, and the envelope lifts warnings whole rather than paging them,
# so every per-bone notice list below names up to this many bones and then counts the rest.
_MAX_CYCLE_WARNINGS = 4
# How many of one bone's custom properties a page carries. A face control bone can hold 199
# sliders, which is more than the 8 KiB reply budget fits whole, so the page is bounded here
# and `custom_property_next_offset` resumes it; the envelope's own shortening cuts whole bones
# and could not reach inside one.
_MAX_BONE_PROPERTIES = 40
# At or past this, a custom property's UI range is Blender's "unbounded" rather than a slider a
# rig author drew: an unbounded float answers +-FLT_MAX (3.4028235e+38) and an int the 32-bit
# limits, measured on Blender 5.2. Reporting either would put two meaningless fields on every
# property of a 199-slider bone.
_UNBOUNDED_PROPERTY_LIMIT = 2_147_483_647
# Deformed meshes per page. A character is body, hair, clothing, eyes, brows and teeth - a
# couple of dozen at most - so one page covers every real rig and `mesh_offset` exists for the
# scene that proves otherwise rather than leaving `truncated` with nowhere to resume from.
_MAX_DEFORMED_MESHES = 200


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


def _stored_custom_properties(pose_bone, custom):
    """
    Check a pose entry's custom properties exist, and spell each value in its property's type.

    Args:
        pose_bone: The bone the entry poses.
        custom: `{property name: value}` from the entry.

    Returns:
        dict: The same names, each value as `_property_value_as_stored` spells it.

    Raises:
        ValueError: For a property the bone does not carry, or a value its type cannot hold.

    """
    missing = sorted(name for name in custom if name not in pose_bone)
    if missing:
        raise ValueError(f"Custom properties not found on '{pose_bone.name}': {missing}")
    return {name: _property_value_as_stored(pose_bone, name, value) for name, value in custom.items()}


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
        spec = dict(entry)
        if entry.get("custom_properties"):
            spec["custom_properties"] = _stored_custom_properties(pose_bone, entry["custom_properties"])
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
# How far the furthest witness has to travel before a roll counts as having moved something,
# as a fraction of the rolled bone's own world length. A roll about the length axis carries a
# point at perpendicular distance r through 2*r*sin(theta/2): zero only where every witness sits
# on the axis. A relay bone carries nothing off its axis and a head bone carries a face 6.47 cm
# off it, and those are the same arithmetic with a different radius - so the radius, not the
# bone's job, is what decides whether the caller made a mistake. One percent of the bone's own
# length keeps a 5 mm finger and a 2 m spine judged the same way, which a scene-sized threshold
# would not.
_TWIST_TRAVEL_FRACTION = 0.01
# The floor under that fraction, so a hair-thin or zero-length bone cannot make a sub-micrometre
# travel count as visible motion.
_TWIST_TRAVEL_FLOOR_M = 1e-5
# What the skinning scan may look at before it stops and says so. The judgement is made per
# rolled bone and a call may pose 500 of them, on top of the view-layer update `_apply_pose_specs`
# already spends per bone, so an unbounded walk of a million-vertex body would cost more than the
# warning is worth. When either bound is hit the radius is a floor rather than a maximum, and the
# notice says so instead of quietly under-reporting.
_MAX_TWIST_VERTICES = 20_000
_MAX_TWIST_WEIGHTED_VERTICES = 512


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


def _perpendicular_radius(point, origin, axis):
    """
    Measure how far one point sits off the line through `origin` along `axis`.

    Args:
        point: The world-space point to measure.
        origin: A world-space point on the line - the rolled bone's head.
        axis: The line's unit direction - the rolled bone's world-space length direction.

    Returns:
        float: The perpendicular distance in metres, which is the radius this point swings on
        when the bone rolls about its own length.

    """
    offset = point - origin
    return (offset - axis * offset.dot(axis)).length


def _skinned_twist_radius(pose_bone, meshes, origin, axis):
    """
    Measure how far the geometry weighted to this bone reaches off its length axis.

    The base mesh, not the evaluated one: the question is where a vertex sits relative to the
    bone that carries it, which the rest positions answer, and evaluating a deformed mesh per
    posed bone per frame is exactly the cost this warning cannot afford.

    Args:
        pose_bone: The bone being rolled; its name is the vertex group that weights to it.
        meshes: The bound meshes to scan, empty for a bone that deforms nothing.
        origin: The bone's world-space head.
        axis: The bone's world-space unit length direction.

    Returns:
        tuple: the largest perpendicular distance found in metres, how many weighted vertices
        it was taken over, how many bound meshes carry a vertex group for this bone, and whether
        the scan stopped on one of its own bounds rather than reaching the end.

    """
    radius = 0.0
    weighted = 0
    examined = 0
    bound_meshes = 0
    for mesh_obj in meshes:
        group = mesh_obj.vertex_groups.get(pose_bone.name)
        if group is None:
            continue
        bound_meshes += 1
        to_world = mesh_obj.matrix_world
        for vertex in mesh_obj.data.vertices:
            if examined >= _MAX_TWIST_VERTICES or weighted >= _MAX_TWIST_WEIGHTED_VERTICES:
                return radius, weighted, bound_meshes, True
            examined += 1
            if not any(item.group == group.index and item.weight > 0.0 for item in vertex.groups):
                continue
            weighted += 1
            radius = max(radius, _perpendicular_radius(to_world @ vertex.co, origin, axis))
    return radius, weighted, bound_meshes, False


def _twist_witnesses(armature, pose_bone, meshes):
    """
    Measure how far the furthest thing this bone carries sits off its own length axis.

    That radius is the whole question a roll warning turns on, and it is a property of the rest
    rig and the skinning rather than of the frame - which is why `_TwistWitnesses` measures it
    once per bone per call and every frame of a stride reuses the answer.

    Args:
        armature: The armature being posed, for the world matrix a scaled rig needs - its bones
            are longer or shorter in the scene than their rest lengths say.
        pose_bone: The bone about to be rolled.
        meshes: The bound meshes to scan for vertices weighted to it, empty when the bone
            deforms nothing.

    Returns:
        dict: radius_m (the largest perpendicular distance any witness sits at), length_m (the
        bone's own world length), bone_ends (how many descendant bone heads and tails were
        measured), vertices (how many weighted vertices were measured), meshes (how many bound
        meshes carry a vertex group for this bone), bounded (whether the vertex scan stopped on
        its own bound) and deforms (whether the bone is flagged to deform geometry).

    """
    bone = pose_bone.bone
    to_world = armature.matrix_world
    origin = to_world @ bone.head_local
    along = (to_world @ bone.tail_local) - origin
    record = {
        "radius_m": 0.0,
        "length_m": along.length,
        "bone_ends": 0,
        "vertices": 0,
        "meshes": 0,
        "bounded": False,
        "deforms": bool(getattr(bone, "use_deform", False)),
    }
    if along.length <= _AIM_MIN_LENGTH:
        # A bone with no length has no length axis, so there is no roll to measure against it.
        return record
    axis = along.normalized()
    # The bone's own tail lies on the line head-to-tail by construction, so its perpendicular
    # radius is exactly zero: the maximum starts there, and only a descendant end or a weighted
    # vertex sitting off the axis can raise it.
    radius = 0.0
    for child in bone.children_recursive:
        for point in (child.head_local, child.tail_local):
            radius = max(radius, _perpendicular_radius(to_world @ point, origin, axis))
        record["bone_ends"] += 2
    skinned, weighted, bound_meshes, bounded = _skinned_twist_radius(pose_bone, meshes, origin, axis)
    record.update({"vertices": weighted, "meshes": bound_meshes, "bounded": bounded})
    record["radius_m"] = max(radius, skinned)
    return record


class _TwistWitnesses:
    """
    One call's memo of how far a roll of each bone would carry the things that bone carries.

    A batched keying call runs the roll check once a frame for as many as 500 bones, and the
    answer cannot change between frames: the radius comes from the rest hierarchy and the
    skinning, not from the pose. Measuring it per frame would put a vertex scan behind every one
    of the 250-frame ceiling's writes, so it is measured once per bone and recalled after that.
    """

    def __init__(self, armature):
        self._armature = armature
        self._meshes = None
        self._by_bone = {}

    @property
    def meshes(self):
        """The meshes this armature deforms, found once: `_mesh_uses_armature_data` is itself a scan."""
        if self._meshes is None:
            self._meshes = [
                obj for obj in bpy.data.objects if obj.type == "MESH" and _mesh_uses_armature_data(obj, self._armature)
            ]
        return self._meshes

    def witnesses(self, pose_bone):
        """
        Measure, or recall, what one bone carries off its own length axis.

        Args:
            pose_bone: The bone about to be rolled.

        Returns:
            dict: `_twist_witnesses`' record for it.

        """
        record = self._by_bone.get(pose_bone.name)
        if record is None:
            # The mesh scan is the expensive half, so a bone that deforms nothing never asks for
            # the bound-mesh list and a rig with no skinned bone rolled never builds it.
            deforms = bool(getattr(pose_bone.bone, "use_deform", False))
            record = _twist_witnesses(self._armature, pose_bone, self.meshes if deforms else ())
            self._by_bone[pose_bone.name] = record
        return record


def _twist_travel_m(radius_m, degrees):
    """
    Work out how far a point at `radius_m` off the axis moves when the bone rolls `degrees`.

    Exact rather than sampled: a rotation carries a point at perpendicular distance r along a
    chord of 2*r*sin(theta/2), so the travel needs no trial pose, no depsgraph round trip and no
    `to_mesh()` - which is what makes measuring it affordable enough to gate a warning on.

    Args:
        radius_m: The witness's perpendicular distance from the axis, in metres.
        degrees: How far the bone is rolled about its length.

    Returns:
        float: The distance the witness travels, in metres.

    """
    return 2.0 * radius_m * math.sin(math.radians(abs(degrees)) / 2.0)


def _twist_examination(witnesses):
    """
    Say what the roll measurement looked at, and what it did not look at.

    A warning that names its evidence can be checked; one that asserts an outcome it never
    computed trains the reader to skip every warning the tool sends, which is the failure this
    sentence exists to prevent.

    Args:
        witnesses: `_twist_witnesses`' record for the rolled bone.

    Returns:
        str: One sentence naming the witnesses measured, and naming the skin when the skin was
        not reachable rather than ruling on it.

    """
    measured = f"Measured against the bone's own tail and {witnesses['bone_ends']} descendant bone end(s)"
    if not witnesses["deforms"]:
        return f"{measured}; this bone deforms no geometry, so what it moves, it moves through a child or a constraint."
    if witnesses["meshes"] == 0:
        return (
            f"{measured}. This bone is flagged to deform geometry, but no mesh bound to this armature carries a "
            "vertex group named after it, so nothing about the skin was measured here."
        )
    bounded = (
        " The vertex scan stopped on its own bound, so this is a floor: a vertex further off the axis may exist."
        if witnesses["bounded"]
        else ""
    )
    return (
        f"{measured}, plus {witnesses['vertices']} vertices weighted to it across "
        f"{witnesses['meshes']} bound mesh(es).{bounded}"
    )


def _twist_notice(pose_bone, degrees, space, travel_m, witnesses):
    """
    Word the notice for a roll that moves nothing this call could measure.

    The previous wording asserted in prose that "the bone's tail - and every child bone's head,
    which sits on it - stay exactly where they are", which the code never computed and which is
    false for an unconnected child offset from the axis. Measured on one real rig, a 30-degree
    head roll moved the tail 0.000 cm and the face 6.47 cm, so the notice fired on a correct
    pose - and an agent that had learned the warnings were noise then dismissed a correct,
    quantitative cycle warning and lost a thirteen-key walk. Every number here is measured.

    Args:
        pose_bone: The bone rolled.
        degrees: How far the entry turns it about its length axis.
        space: The call's pose space, whose axis letters the notice quotes.
        travel_m: How far the furthest measured witness moves, in metres.
        witnesses: `_twist_witnesses`' record for the bone, for scale and for evidence.

    Returns:
        str: The notice.

    """
    return (
        f"Bone '{pose_bone.name}': {degrees:.4g} degrees about the bone's own length axis "
        f"({_LENGTH_AXIS} in {space} space, the axis running head to tail) moves the furthest thing measured by "
        f"{travel_m:.6g} m, on a bone {witnesses['length_m']:.6g} m long. {_twist_examination(witnesses)} If a "
        "bend was intended, rotate about one of the bone's other two axes; list_character_bones(rest_axes=True) "
        "reports where each one points."
    )


def _inert_rotation_warnings(prepared, space, witnesses):
    """
    Say which of this call's rolls moved nothing, having measured what each of them would move.

    A bone's own +Y runs head to tail (`_LENGTH_AXIS`), so a rotation about it rolls the bone
    where it stands. Whether that is a mistake depends entirely on what sits off the axis: a
    relay bone carries nothing and the joint simply fails to bend, while a head bone carries the
    whole face and the roll is the turn the caller asked for. Only the first is worth a warning,
    so the candidate filter is the cheap arithmetic on the entry and the verdict is the measured
    travel - a warning that fires here means something really did not move.

    Only LOCAL and LOCAL_WITH_PARENT are judged: in those spaces the axis letters resolve to
    the bone's own rest basis, so `Y` is the bone's length. Under POSE and WORLD the same letter
    names the armature's or the scene's axis, which says nothing about this bone.

    Args:
        prepared: `(pose_bone, spec, matrix or None)` triples from `_validate_pose_specs`.
        space: The call's pose space.
        witnesses: The call's `_TwistWitnesses`, which measures each bone once and recalls it.

    Returns:
        list[str]: One notice per bone whose roll moved less than the visible threshold, in the
        order posed, bounded by `_MAX_CYCLE_WARNINGS` with one counted summary for the rest -
        warnings are lifted whole into the envelope and never paged, so 500 posed bones must not
        be able to spend the reply budget on them.

    """
    if space not in _BONE_LOCAL_SPACES:
        return []
    silent = []
    for pose_bone, spec, _matrix in prepared:
        degrees = _length_axis_twist_degrees(spec)
        if degrees is None:
            continue
        measured = witnesses.witnesses(pose_bone)
        travel_m = _twist_travel_m(measured["radius_m"], degrees)
        if travel_m > max(_TWIST_TRAVEL_FLOOR_M, measured["length_m"] * _TWIST_TRAVEL_FRACTION):
            continue
        silent.append((pose_bone, degrees, travel_m, measured))
    notices = [
        _twist_notice(pose_bone, degrees, space, travel_m, measured)
        for pose_bone, degrees, travel_m, measured in silent[:_MAX_CYCLE_WARNINGS]
    ]
    remainder = [pose_bone.name for pose_bone, _degrees, _travel, _measured in silent[_MAX_CYCLE_WARNINGS:]]
    if remainder:
        listed = ", ".join(remainder[:_MAX_CYCLE_WARNINGS])
        trailing = f" and {len(remainder) - _MAX_CYCLE_WARNINGS} more" if len(remainder) > _MAX_CYCLE_WARNINGS else ""
        notices.append(
            f"{len(remainder)} further bone(s) were rolled about their own length axis and moved nothing this "
            f"call could measure: {listed}{trailing}."
        )
    return notices


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
            extent = cycled_curve_extent(curve)
            if extent is None:
                continue
            first, last = extent
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
    # What a roll of a given bone would move is a fact about the rest rig and the skinning, so
    # it is measured once here and recalled at every frame rather than re-scanned per frame.
    witnesses = _TwistWitnesses(armature)
    for frame, prepared in prepared_frames:
        _place_playhead(scene, frame)
        if keying_policy != "REMOVE":
            posed = _apply_pose_specs(armature, prepared, space, detail=detail)
            records.extend({"frame": frame, **record} if report_frames else record for record in posed)
            # Measured before the write: afterwards this frame is inside the extent it widened,
            # and the stretched cycle is invisible again. REMOVE narrows an extent rather than
            # widening one, and has no key landing outside anything.
            per_frame_warnings.append(
                _cycle_extension_warnings(action, prepared, frame)
                + _inert_rotation_warnings(prepared, space, witnesses)
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


def _property_bounds(pose_bone, name):
    """
    Read the slider range Blender's UI data records for one custom property.

    A range is only worth reporting when a rig author set one. Measured on Blender 5.2, a float
    property nobody bounded still answers `min: -3.4028235e+38, max: 3.4028235e+38` and an int
    one the 32-bit limits, which is Blender spelling "unbounded" - two fields per property that
    say nothing, on a bone that can carry 199 of them.

    Args:
        pose_bone: The bone holding the property.
        name: The property's key.

    Returns:
        dict: `min` and `max` when the property defines real ones, else empty - a property with
        no UI data, a string one, or one left at the type's full range has no range to state.

    """
    reader = getattr(pose_bone, "id_properties_ui", None)
    if reader is None:
        return {}
    try:
        described = dict(reader(name).as_dict())
    except (TypeError, KeyError, AttributeError):
        return {}
    bounds = {}
    for bound in ("min", "max"):
        value = described.get(bound)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if not math.isfinite(value) or abs(value) >= _UNBOUNDED_PROPERTY_LIMIT:
            continue
        bounds[bound] = float(value)
    return bounds


def _bone_custom_properties(pose_bone, offset):
    """
    Page one pose bone's custom properties, which is where a rig keeps its sliders.

    These are the names `set_character_pose` and `keyframe_character_pose` take in
    `custom_properties`, and until now no tool in the posing surface reported them: a rig
    shipping 45 composite and 199 direct face sliders could not be asked what it has, and every
    name had to come from a document beside the file. One rig's `face_ctrl` bone carries all
    199, so the page is per bone and resumable rather than whole.

    Args:
        pose_bone: The bone to read, or None when the rest bone has no pose bone.
        offset: Where in this bone's sorted property names to resume.

    Returns:
        tuple: (records, total, next_offset). Each record is `name`, `value`, and `min`/`max`
        where the property defines them. `next_offset` is None once the page reaches the end.

    """
    if pose_bone is None:
        return [], 0, None
    names = sorted(key for key in getattr(pose_bone, "keys", lambda: ())() if key != "_RNA_UI")
    page = names[offset : offset + _MAX_BONE_PROPERTIES]
    records = [{"name": name, "value": _plain(pose_bone[name]), **_property_bounds(pose_bone, name)} for name in page]
    consumed = offset + len(page)
    return records, len(names), consumed if consumed < len(names) else None


# --- Measuring what an axis actually does, rather than reading it off the rest pose ---------
#
# `list_character_bones(rest_axes=True)` reports nine numbers off `bone.matrix_local`. They name
# directions at rest, not rotations; they carry no witness and therefore no lever arm; and under
# POSE and WORLD a `rotate.axis` letter names the armature's or the scene's axis rather than the
# bone's. Constraints, drivers and IK can also null or invert a channel without appearing in any
# of it. So which axis swings a limb, and which sign of a roll turns a palm outward, is a
# measurement: turn the bone, read where a witness went, and put the pose back.


# The six signed axes are a whole basis, so a seventh entry could only repeat one - and one call
# answering the basis is what saves an agent three round trips per bone.
_MAX_PROBE_AXES = 6
# How many named world directions one probe may decompose its travel against. Each costs a
# signed number per probed axis in the reply, and the question "which way does this go" has
# never needed more than a handful of named directions to answer.
_MAX_PROBE_REFERENCES = 6
# Past a half turn a probe stops measuring a direction and starts measuring a fold-back: the
# chord shortens again, and the sign of the answer is no longer the sign of the rotation.
_MAX_PROBE_DEGREES = 180.0
# Six decimals is a micrometre at rig scale - far finer than any pose an animator judges - and a
# full float expansion of six axes' worth of points and travel would spend most of the reply on
# encoding noise.
_PROBE_DECIMALS = 6


def _rounded_point(vector):
    return [round(float(value), _PROBE_DECIMALS) for value in vector]


def _validated_probe_axes(axes, bone_name):
    """
    Check the axis letters one probe will try, before the bone is touched.

    Args:
        axes: The raw `axes` argument.
        bone_name: The bone being probed, for the error message.

    Returns:
        list[str]: The signed axis names, in the order given.

    Raises:
        ValueError: If `axes` is not a list of between one and `_MAX_PROBE_AXES` signed axis
            names, if one of them is not a signed axis, or if one is named twice - two identical
            probes would report the same travel twice and answer nothing.

    """
    if isinstance(axes, str) or not isinstance(axes, (list, tuple)):
        raise ValueError("axes must be a list of signed axis names")
    listed = list(axes)
    if not 1 <= len(listed) <= _MAX_PROBE_AXES:
        raise ValueError(f"axes takes 1 to {_MAX_PROBE_AXES} signed axis names")
    for name in listed:
        _signed_axis(name, f"axes entry for '{bone_name}'")
    _unique_names(listed, "probe axes")
    return listed


def _probe_degrees(degrees):
    """
    Check the angle a probe turns the bone by.

    Args:
        degrees: The raw `degrees` argument, signed.

    Returns:
        float: The angle, in degrees.

    Raises:
        ValueError: If it is not finite, too small for the travel to stand clear of float noise,
            or past the half turn at which the chord starts shortening again.

    """
    turn = _finite(degrees, "degrees")
    if abs(turn) < _INERT_ROTATION_MINIMUM_DEGREES:
        raise ValueError(
            f"degrees of {turn:g} is too small to measure: give the probe at least "
            f"{_INERT_ROTATION_MINIMUM_DEGREES:g} degrees to turn the bone by"
        )
    if abs(turn) > _MAX_PROBE_DEGREES:
        raise ValueError(f"degrees of {turn:g} is past the half turn a probe can read a direction from")
    return turn


def _validated_reference_directions(reference_directions):
    """
    Resolve the caller's named world directions into unit vectors.

    Args:
        reference_directions: `{name: (x, y, z)}` in world space, or None.

    Returns:
        dict: Each name mapped to its unit `mathutils.Vector`, empty when none were given.

    Raises:
        ValueError: If the mapping is not an object, names more than `_MAX_PROBE_REFERENCES`
            directions, or if one entry is not three finite numbers or names no direction at
            all - each refusal names the entry, because a caller passing six of them cannot
            otherwise tell which one was wrong.

    """
    if not reference_directions:
        return {}
    if not isinstance(reference_directions, dict):
        raise ValueError("reference_directions must be an object mapping names to world-space vectors")
    if len(reference_directions) > _MAX_PROBE_REFERENCES:
        raise ValueError(f"reference_directions takes at most {_MAX_PROBE_REFERENCES} named directions")
    resolved = {}
    for name, values in reference_directions.items():
        vector = _vector(values, f"reference_directions['{name}']")
        if vector.length <= _AIM_MIN_LENGTH:
            raise ValueError(f"reference_directions['{name}'] must be a non-zero vector")
        resolved[name] = vector.normalized()
    return resolved


def _probe_witness_bone(armature, pose_bone, witness_bone_name):
    """
    Choose the bone whose travel answers the probe, and say how it was chosen.

    A rotation is only readable through something it carries, and the further that something
    sits from the axis the larger the lever arm - so the default is the bone's farthest
    descendant, which is the hand at the end of an arm rather than the shoulder beside it.

    Args:
        armature: The armature being probed.
        pose_bone: The bone the probe turns.
        witness_bone_name: An explicit witness, or None to choose one.

    Returns:
        tuple: the witness pose bone, and how it was chosen - "explicit", "farthest_descendant",
        or "probed_bone" when the bone has no descendants to read it through.

    Raises:
        ValueError: If an explicit witness names a bone the armature does not have.

    """
    if witness_bone_name is not None:
        witness = armature.pose.bones.get(_required_name(witness_bone_name, "witness_bone_name"))
        if witness is None:
            raise ValueError(f"Pose bone not found: {witness_bone_name}")
        return witness, "explicit"
    head = pose_bone.bone.head_local
    descendants = list(pose_bone.bone.children_recursive)
    if not descendants:
        return pose_bone, "probed_bone"
    # Farthest by rest distance from the probed bone's head, ties broken by name: two bones at
    # the same distance must not make the same probe answer differently between calls.
    farthest = min(descendants, key=lambda bone: (-(bone.tail_local - head).length, bone.name))
    return armature.pose.bones[farthest.name], "farthest_descendant"


def _probe_witness_point(armature, witness, position):
    """
    Read where the witness sits in the world right now.

    Pose bones are read live rather than through `evaluated_get`: `PoseBone.head`/`tail` already
    carry whatever the last view-layer update solved, constraints and drivers included, which is
    exactly the part of the answer a rest reading cannot give.

    Args:
        armature: The armature being probed, for its world matrix.
        witness: The witness pose bone.
        position: HEAD, TAIL or CENTER.

    Returns:
        mathutils.Vector: The witness point in world space.

    """
    to_world = armature.matrix_world
    if position == "HEAD":
        return to_world @ witness.head
    if position == "TAIL":
        return to_world @ witness.tail
    return ((to_world @ witness.head) + (to_world @ witness.tail)) * 0.5


def _probe_record(axis, degrees, before, after, references):
    """
    Report what one probed axis did to the witness.

    Args:
        axis: The signed axis turned about, in the call's pose space.
        degrees: The signed angle applied.
        before: The witness's world point before the turn.
        after: The witness's world point with the turn applied.
        references: The caller's named unit directions, possibly empty.

    Returns:
        dict: axis, degrees, witness_before_world, witness_after_world, travel_world, travel_m,
        and - only when directions were named - reference_components_m, the signed metres the
        witness moved along each of them. The sign is the half of the answer a magnitude cannot
        carry: it is what says which way round to roll a wrist to turn the palm outward.

    """
    travel = after - before
    record = {
        "axis": axis,
        "degrees": degrees,
        "witness_before_world": _rounded_point(before),
        "witness_after_world": _rounded_point(after),
        "travel_world": _rounded_point(travel),
        "travel_m": round(travel.length, _PROBE_DECIMALS),
    }
    if references:
        record["reference_components_m"] = {
            name: round(travel.dot(direction), _PROBE_DECIMALS) for name, direction in references.items()
        }
    return record


def _deformed_mesh_page(armature, mesh_offset):
    """
    Page the meshes this armature deforms, and say how each one is bound to it.

    Armature-level, so it is not paged with the bones and not repeated per bone: one rig
    deforms a handful of meshes, and which ones is the question a bone's `deform: true` cannot
    answer. Until this existed it was reachable only as a side effect of
    `frame_camera_on_objects`, which moves a camera to answer it.

    Args:
        armature: The armature object to resolve.
        mesh_offset: Where to resume in the bound list.

    Returns:
        dict: A page of `{object, binding, modifier_enabled}` with the envelope's usual
        total/offset/limit/truncated/next_offset.

    """
    bound = deforming_meshes(armature)
    start, end, truncated, next_offset = paginate(len(bound), mesh_offset, _MAX_DEFORMED_MESHES, _MAX_DEFORMED_MESHES)
    return {
        "items": [
            {"object": mesh.name, "binding": binding, "modifier_enabled": enabled}
            for mesh, binding, enabled in bound[start:end]
        ],
        "total": len(bound),
        "offset": start,
        "limit": _MAX_DEFORMED_MESHES,
        "truncated": truncated,
        "next_offset": next_offset,
    }


class PoseAnimationHandlersMixin:
    """Apply pose-space transforms and author named animation actions."""

    def list_character_bones(
        self,
        armature_object_name,
        limit=100,
        offset=0,
        rest_axes=False,
        bone_names=None,
        custom_properties=False,
        property_offset=0,
        deformed_meshes=False,
        mesh_offset=0,
    ):
        """Page the armature's rest bones with their parent, deform flag, optional rest axes and sliders."""
        armature = _armature_object(armature_object_name)
        _validate_limit_offset(limit, offset, _MAX_BONE_PAGE, "bone")
        if isinstance(property_offset, bool) or int(property_offset) < 0:
            raise ValueError("property_offset must be a non-negative integer")
        if isinstance(mesh_offset, bool) or int(mesh_offset) < 0:
            raise ValueError("mesh_offset must be a non-negative integer")
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
            if custom_properties:
                # The pose bone, not the rest bone: the two hold separate ID property stores,
                # and the sliders a pose call writes are the pose bone's.
                properties, total, property_next = _bone_custom_properties(
                    armature.pose.bones.get(bone.name), int(property_offset)
                )
                item["custom_properties"] = properties
                item["custom_property_count"] = total
                item["custom_property_next_offset"] = property_next
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
        if deformed_meshes:
            reply["deformed_meshes"] = _deformed_mesh_page(armature, mesh_offset)
        return reply

    def probe_bone_axis(
        self,
        armature_object_name,
        bone_name,
        axes,
        degrees=15.0,
        space="LOCAL",
        witness_bone_name=None,
        witness_bone_position="TAIL",
        reference_directions=None,
    ):
        """Turn one bone about each named axis in turn, measure where a witness went, restore the pose."""
        armature = _posable_armature(armature_object_name, "probe a bone axis")
        if space not in _POSE_SPACES:
            raise ValueError(f"Unsupported pose space: {space}")
        pose_bone = armature.pose.bones.get(_required_name(bone_name, "bone_name"))
        if pose_bone is None:
            raise ValueError(f"Pose bone not found: {bone_name}")
        if witness_bone_position not in _BONE_POSITIONS:
            raise ValueError(f"witness_bone_position must be one of {', '.join(_BONE_POSITIONS)}")
        # Everything is checked before the bone is turned: a probe is read-only, so it runs
        # outside `mutation_transaction` and a refusal part way through would leave the rig
        # holding a trial pose with nothing to roll it back.
        turn = _probe_degrees(degrees)
        listed = _validated_probe_axes(axes, pose_bone.name)
        references = _validated_reference_directions(reference_directions)
        witness, witness_source = _probe_witness_bone(armature, pose_bone, witness_bone_name)
        bpy.context.view_layer.update()
        # Built before the first turn, so every axis is measured from the same starting pose
        # rather than from whatever the previous axis left behind.
        prepared = [
            _validate_pose_specs(
                armature, [{"bone_name": pose_bone.name, "rotate": {"axis": axis, "degrees": turn}}], space
            )
            for axis in listed
        ]
        before = _probe_witness_point(armature, witness, witness_bone_position)
        records = []
        for axis, entries in zip(listed, prepared, strict=True):
            try:
                with restored_bone_pose(armature, [pose_bone.name]):
                    _apply_pose_specs(armature, entries, space)
                    after = _probe_witness_point(armature, witness, witness_bone_position)
            finally:
                # The restore writes channels without re-solving, so the scene is evaluated again
                # here - on the way out of a failure as much as a success. Otherwise the next
                # axis, or whatever the caller reads next, sees the trial pose this call already
                # handed back, and the restore is only half a restore.
                bpy.context.view_layer.update()
            records.append(_probe_record(axis, turn, before, after, references))
        to_world = armature.matrix_world
        return {
            "armature_object": armature.name,
            "bone": pose_bone.name,
            "space": space,
            "degrees": turn,
            "bone_length_m": round(
                ((to_world @ pose_bone.bone.tail_local) - (to_world @ pose_bone.bone.head_local)).length,
                _PROBE_DECIMALS,
            ),
            "witness_bone": witness.name,
            "witness_bone_position": witness_bone_position,
            "witness_bone_source": witness_source,
            "axes": records,
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
            "warnings": _bare_write_warnings(armature, custom_properties)
            + _inert_rotation_warnings(prepared, space, _TwistWitnesses(armature)),
            "changed_objects": [armature.name],
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
