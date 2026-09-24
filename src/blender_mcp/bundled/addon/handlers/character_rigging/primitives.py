# Inherited findings, carried over with the code: every line in this module was moved verbatim out of
# `foundation.py`, where one file-wide pragma covered them, and `scripts/lint_changed.py` attributes a
# moved line to the branch that moved it. Suppressed, not fixed: restructuring the bodies would make a
# pure move unreviewable. They stay in the whole-tree backlog `just lint-all` reports.
# ruff: file-ignore[magic-value-comparison]
"""
The vocabulary every character-rigging handler shares.

Validation, datablock lookup, vector/matrix conversion, the pose-field table and the bone
paging filter: the layer that has no domain of its own and that every other module in this
package imports. It is separated from them so that a rig-inspection change cannot reach
posing, and so the dependency runs one way - primitives is imported by everything here and
imports none of it.
"""

import math

from collections import Counter

import bpy
import mathutils

# How many bones an override notice names before it counts the rest: the warning has to fit
# beside the reply's own records in the byte budget, and a rig posed wholesale would otherwise
# spend the budget on a list the caller already has.
_MAX_LISTED_OVERRIDE_BONES = 8
_POSE_FIELDS = (
    "rotation_mode",
    "lock_location",
    "lock_rotation",
    "lock_rotation_w",
    "lock_rotations_4d",
    "lock_scale",
    "lock_ik_x",
    "lock_ik_y",
    "lock_ik_z",
    "use_ik_limit_x",
    "use_ik_limit_y",
    "use_ik_limit_z",
    "ik_min_x",
    "ik_max_x",
    "ik_min_y",
    "ik_max_y",
    "ik_min_z",
    "ik_max_z",
    "ik_stiffness_x",
    "ik_stiffness_y",
    "ik_stiffness_z",
    "ik_stretch",
)


def _required_name(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _finite(value, label):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    return number


def _vector(value, label):
    return mathutils.Vector(_vector_tuple(value, label))


def _vector_tuple(value, label):
    if value is None or len(value) != 3:
        raise ValueError(f"{label} must contain exactly three numbers")
    return tuple(_finite(item, f"{label}[{index}]") for index, item in enumerate(value))


def _distance(left, right):
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(left, right, strict=True)))


def _matrix_list(matrix):
    return [[float(value) for value in row] for row in matrix]


def _plain(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if hasattr(value, "name"):
        return value.name
    try:
        return [_plain(item) for item in value]
    except TypeError:
        return str(value)


def _custom_properties(owner):
    result = {}
    for key in getattr(owner, "keys", lambda: ())():
        if key == "_RNA_UI":
            continue
        result[str(key)] = _plain(owner[key])
    return result


def _property_value_as_stored(owner, name, value):
    """
    Spell a custom-property value in the type the property already holds.

    An ID property takes the type of whatever Python value is assigned to it, so writing a
    JSON `0` into a float slider silently turns it into an int property. Keying it then flags
    the F-Curve to round everything it evaluates, and that flag follows the property's type at
    the latest key: one integral key left a face slider keyed 0.35 playing back 0, and 0.85
    playing back 1, with every keyframe's stored value intact.

    Args:
        owner: The pose bone (or other ID-property holder) being written.
        name: The property name.
        value: The caller's value.

    Returns:
        The value converted to the stored property's scalar type; unchanged when the property
        does not exist yet or is not a bool, int or float.

    Raises:
        ValueError: When the conversion would lose the value - a fraction into an integer
            property, or anything but 0 or 1 into a boolean one.

    """
    if name not in owner:
        return value
    stored = owner[name]
    label = f"Custom property '{name}' on '{owner.name}'"
    # bool before int: a Python bool is an int, and a boolean property must stay one.
    if isinstance(stored, bool):
        if isinstance(value, bool):
            return value
        if value not in {0, 1}:
            raise ValueError(f"{label} is a boolean; {value!r} is neither 0 nor 1")
        return bool(value)
    if isinstance(stored, int):
        if isinstance(value, float) and not value.is_integer():
            raise ValueError(f"{label} is an integer; {value!r} would be rounded")
        return int(value)
    if isinstance(stored, float):
        return float(value)
    return value


def _override_property_warning(armature_obj, bone_names):
    """
    Warn that a bare custom-property write on a library override will not survive the file.

    Blender records an override for a pose bone's RNA channels - `rotation_quaternion` lands in
    `override_library.properties` and reopens as written - and records nothing for a bare ID
    property write. Measured on Blender 5.2, four cases: a property the source rig does not
    define is gone after save and reopen; one the source does define reverts to the library's
    value; registering the override property by hand (`override_library.properties.add` plus a
    REPLACE operation) changes neither; and keying the property instead is durable, because the
    action is local data and Blender records `animation_data` itself as an override property - the
    keyed value read back 0.75 at every frame after reopen where the bare write had reverted to
    0.1. A bare write succeeds and reads back correctly in-session, so nothing else in the reply
    can tell a caller that the value is scenery.

    Args:
        armature_obj: The armature the properties are written on.
        bone_names: The bones this call writes custom properties on without keying them, in any
            order; empty when it writes none. A keyed write is durable and must not be passed.

    Returns:
        str | None: A notice naming the rig, the bones and the remedy, or None when the rig is
        local (where a bare write is durable) or the call wrote no custom property.

    """
    names = sorted(set(bone_names))
    if not names or getattr(armature_obj, "override_library", None) is None:
        return None
    listed = ", ".join(names[:_MAX_LISTED_OVERRIDE_BONES])
    if len(names) > _MAX_LISTED_OVERRIDE_BONES:
        listed = f"{listed} (+{len(names) - _MAX_LISTED_OVERRIDE_BONES} more)"
    return (
        f"Custom properties written on '{armature_obj.name}' will not survive save and reopen: the rig is a "
        f"library override, and Blender carries no bare ID property write into an override, so the values on "
        f"{listed} revert to the library's on load. Bone transforms are unaffected - they are recorded as "
        f"override properties. Key the value instead - keyframe_character_pose writes it into the action, which "
        f"is local data and does survive - and make sure the source rig defines the property, since a property "
        f"the library does not carry is lost either way."
    )


def _armature_object(name):
    obj = bpy.data.objects.get(_required_name(name, "armature_object_name"))
    if obj is None:
        raise ValueError(f"Object not found: {name}")
    if obj.type != "ARMATURE" or obj.data is None:
        raise ValueError(f"Object '{name}' is not an armature (type={obj.type})")
    return obj


def _mesh_object(name):
    obj = bpy.data.objects.get(_required_name(name, "mesh_object_name"))
    if obj is None:
        raise ValueError(f"Object not found: {name}")
    if obj.type != "MESH" or obj.data is None:
        raise ValueError(f"Object '{name}' is not a mesh (type={obj.type})")
    return obj


def _ensure_object_collection(name):
    collection = bpy.data.collections.get(_required_name(name, "collection_name"))
    if collection is None:
        collection = bpy.data.collections.new(name)
    scene_root = bpy.context.scene.collection
    descendants = getattr(scene_root, "children_recursive", scene_root.children)
    if collection != scene_root and collection.name not in descendants:
        scene_root.children.link(collection)
    return collection


def _unique_names(values, label):
    duplicates = sorted(name for name, count in Counter(values).items() if count > 1)
    if duplicates:
        raise ValueError(f"Duplicate {label}: {', '.join(str(value) for value in duplicates)}")


def _validate_limit_offset(limit, offset, maximum, label):
    if isinstance(limit, bool) or not 1 <= int(limit) <= maximum:
        raise ValueError(f"{label}_limit must be in [1, {maximum}]")
    if isinstance(offset, bool) or int(offset) < 0:
        raise ValueError(f"{label}_offset must be non-negative")


def _bone_path_token(bone_name):
    escaped = bone_name.replace("\\", "\\\\").replace('"', '\\"')
    return f'pose.bones["{escaped}"]'


# Longest bone_names filter accepted, and the page size list_character_bones pages a whole
# rig at. The heaviest production rigs here carry 187-238 bones, so two pages cover one.
# get_character_rig_info paginates further (500) but shares this narrower cap for its own
# bone_names filter, so naming exact bones behaves identically in both tools.
_MAX_BONE_PAGE = 200


def _selected_bones(armature, bone_names):
    """
    Narrow an armature's rest bones to the ones the caller named, in armature order.

    Shared by list_character_bones and get_character_rig_info: reading a few bones' data off
    a 187-bone rig otherwise costs several paginated calls, because a per-bone payload spends
    the reply budget at a few dozen bones a page at best. A name that does not exist is refused
    rather than silently omitted: a caller asking for three bones and receiving two would pose
    or inspect the wrong one.

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
