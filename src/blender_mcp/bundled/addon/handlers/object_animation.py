# ruff: file-ignore[too-many-branches, too-many-locals, undocumented-public-method]
"""Blender-side handlers for generic object transform keyframing (location/rotation/scale)."""

import math

from contextlib import suppress

import bpy
import mathutils

from ..helpers import MAX_FRAME, MIN_FRAME
from .action_assignment import (
    ACTION_POLICIES,
    action_fcurve_collections,
    assign_named_action,
    assigned_slot_identifier,
    cycled_curve_extent,
)
from .key_style import KeyStyle, style_point
from .scene import _object, _required_name
from .scene_physics import _scene, _scene_fps

_MAX_BATCH = 500
_KEYFRAME_MATCH_TOLERANCE = 1e-5
# One call may key 500 records, and the envelope lifts warnings whole rather than paging them,
# so the per-channel cycle notices are named up to this many and then counted.
_MAX_CYCLE_WARNINGS = 4
_SPACES = {"LOCAL", "WORLD"}
_POLICIES = {"INSERT_ONLY", "REPLACE_EXISTING"}
_CHANNEL_LENGTHS = {"location": 3, "rotation_euler": 3, "rotation_quaternion": 4, "scale": 3}
_EULER_ORDERS = {"XYZ", "XZY", "YXZ", "YZX", "ZXY", "ZYX"}


def _finite_number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{label} must be finite")
    return value


def _finite_sequence(value, length, label):
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise ValueError(f"{label} must contain exactly {length} numbers")
    return tuple(_finite_number(component, f"{label}[{index}]") for index, component in enumerate(value))


def _frame_value(value, label):
    value = _finite_number(value, label)
    if not MIN_FRAME <= value <= MAX_FRAME:
        raise ValueError(f"{label} must be between {MIN_FRAME} and {MAX_FRAME}")
    return value


def _resolve_frame(record, label, scene_cache):
    frame = record.get("frame")
    at_seconds = record.get("at_seconds")
    if (frame is None) == (at_seconds is None):
        raise ValueError(f"{label} must supply exactly one of frame or at_seconds")
    if frame is not None:
        return _frame_value(frame, f"{label}.frame")
    seconds = _finite_number(at_seconds, f"{label}.at_seconds")
    scene_name = record.get("scene_name")
    scene = scene_cache.get(scene_name)
    if scene is None:
        scene = _scene(scene_name)
        scene_cache[scene_name] = scene
    return _frame_value(scene.frame_start + seconds * _scene_fps(scene), f"{label}.at_seconds")


def _resolve_channels(record, label, obj):
    channels = {}
    for name, length in _CHANNEL_LENGTHS.items():
        value = record.get(name)
        if value is not None:
            channels[name] = _finite_sequence(value, length, f"{label}.{name}")
    if not channels:
        raise ValueError(f"{label} must supply at least one of {sorted(_CHANNEL_LENGTHS)}")
    if "rotation_euler" in channels and "rotation_quaternion" in channels:
        raise ValueError(f"{label}: supply rotation_euler or rotation_quaternion, not both")
    if "rotation_euler" in channels and obj.rotation_mode not in _EULER_ORDERS:
        raise ValueError(
            f"{label}: object '{obj.name}' has rotation_mode={obj.rotation_mode}; supply rotation_quaternion "
            "(QUATERNION mode) or use edit_keyframes directly (AXIS_ANGLE is not supported here)"
        )
    if "rotation_quaternion" in channels and obj.rotation_mode != "QUATERNION":
        raise ValueError(
            f"{label}: object '{obj.name}' has rotation_mode={obj.rotation_mode}; supply rotation_euler instead"
        )
    return channels


def _action_fcurves(id_owner):
    """
    Return the action driving `id_owner` and the F-Curves its own keys live in.

    Args:
        id_owner: The object being keyed.

    Returns:
        tuple: The assigned action (or None) and its F-Curves, narrowed to this owner's slot
        so a second object sharing the action never answers for this one's keys.

    """
    animation = getattr(id_owner, "animation_data", None)
    action = getattr(animation, "action", None) if animation is not None else None
    if action is None:
        return None, ()
    slot = getattr(animation, "action_slot", None)
    return action, [curve for collection in action_fcurve_collections(action, slot) for curve in collection]


def _has_key_at(obj, data_path, frame):
    _action, curves = _action_fcurves(obj)
    for curve in curves:
        if curve.data_path == data_path and any(
            abs(point.co[0] - frame) <= _KEYFRAME_MATCH_TOLERANCE for point in curve.keyframe_points
        ):
            return True
    return False


def _style_inserted_keys(obj, data_path, frame, style):
    changed = []
    _action, curves = _action_fcurves(obj)
    for curve in curves:
        if curve.data_path != data_path:
            continue
        point = next(
            (item for item in curve.keyframe_points if abs(item.co[0] - frame) <= _KEYFRAME_MATCH_TOLERANCE), None
        )
        if point is None:
            continue
        style_point(point, style)
        changed.append({"data_path": data_path, "array_index": curve.array_index, "frame": frame})
    return changed


def _cycled_extent(obj, data_path):
    """
    Measure the key extent a cycling channel already repeats, if it cycles at all.

    One channel is several curves - `location` is three - and each carries its own modifier and
    its own keys, so the extent this channel repeats is the union of the cycling ones. A curve
    that cycles nothing contributes nothing: it has no period for a new key to redefine.

    Args:
        obj: The object about to be keyed.
        data_path: The channel this call writes.

    Returns:
        tuple | None: (first, last) key frame across that channel's cycling curves, or None
        when none of them carries a Cycles modifier or they hold one frame between them.

    """
    _action, curves = _action_fcurves(obj)
    extents = [
        extent
        for curve in curves
        if curve.data_path == data_path and (extent := cycled_curve_extent(curve)) is not None
    ]
    if not extents:
        return None
    return min(first for first, _last in extents), max(last for _first, last in extents)


def _cycle_extension_warnings(prepared):
    """
    Warn once per channel whose new key lands outside a cycle that channel already repeats.

    `keyframe_character_pose` has warned about this since the walk whose arms drifted; the
    object path carried the same trap and said nothing, which is worse, because the channel it
    redefines is usually the root's `location`. A rehearsal keyed a root at frame 199 over a
    16-frame travelling cycle and moved the whole character: the period became 199 frames, the
    stride stopped repeating, and every REPEAT_OFFSET repeat now carried the wrong distance.
    Extending a cycle on purpose is legitimate authoring, so this warns and keys rather than
    refusing.

    Args:
        prepared: The validated records, read after the batch's action is assigned so the
            curves measured are the ones this call is about to write into.

    Returns:
        list[str]: One warning per affected object channel, naming the frame, the extent it
        fell outside and the period that extent becomes, bounded by `_MAX_CYCLE_WARNINGS` with
        one summary line for the rest. Warnings are lifted whole into the envelope and never
        paged, so a 500-record batch must not be able to spend the reply budget on them.

    """
    stretched = {}
    for entry in prepared:
        for data_path in entry["channels"]:
            identity = (entry["object_name"], data_path)
            if identity in stretched:
                continue
            extent = _cycled_extent(entry["object"], data_path)
            if extent is None:
                continue
            first, last = extent
            if first - _KEYFRAME_MATCH_TOLERANCE <= entry["frame"] <= last + _KEYFRAME_MATCH_TOLERANCE:
                continue
            stretched[identity] = (first, last, entry["frame"])
    affected = list(stretched)
    warnings = []
    for identity in affected[:_MAX_CYCLE_WARNINGS]:
        name, data_path = identity
        first, last, frame = stretched[identity]
        warnings.append(
            f"'{name}'.{data_path} is keyed at frame {frame:g}, outside the frames {first:g}-{last:g} it already "
            f"cycles over. A Cycles modifier repeats its own curve's key extent, so this channel's period becomes "
            f"{max(last, frame) - min(first, frame):g} frames instead of {last - first:g}; under REPEAT_OFFSET "
            "each repeat then carries that much further, so a travelling root stops arriving where the cycle put "
            "it. Key it inside the cycle, or re-cycle the action deliberately."
        )
    remainder = affected[_MAX_CYCLE_WARNINGS:]
    if remainder:
        listed = ", ".join(f"'{name}'.{path}" for name, path in remainder[:_MAX_CYCLE_WARNINGS])
        trailing = f" and {len(remainder) - _MAX_CYCLE_WARNINGS} more" if len(remainder) > _MAX_CYCLE_WARNINGS else ""
        warnings.append(
            f"{len(remainder)} further channel(s) are keyed outside the cycle their own curves carry, stretching "
            f"it the same way: {listed}{trailing}."
        )
    return warnings


def _apply_and_key(obj, frame, space, channels):
    if space == "WORLD":
        # obj.matrix_world (and its parent chain) is only refreshed by a depsgraph
        # evaluation, so a parent reassigned or moved earlier in this same command
        # (or an earlier command in the same batch) can still read as stale here.
        bpy.context.view_layer.update()
        current_location, current_rotation, current_scale = obj.matrix_world.decompose()
        location = mathutils.Vector(channels["location"]) if "location" in channels else current_location
        if "rotation_euler" in channels:
            rotation = mathutils.Euler(channels["rotation_euler"], obj.rotation_mode)
        elif "rotation_quaternion" in channels:
            rotation = mathutils.Quaternion(channels["rotation_quaternion"])
        else:
            rotation = current_rotation
        scale = mathutils.Vector(channels["scale"]) if "scale" in channels else current_scale
        obj.matrix_world = mathutils.Matrix.LocRotScale(location, rotation, scale)
    else:
        if "location" in channels:
            obj.location = channels["location"]
        if "rotation_euler" in channels:
            obj.rotation_euler = channels["rotation_euler"]
        if "rotation_quaternion" in channels:
            obj.rotation_quaternion = channels["rotation_quaternion"]
        if "scale" in channels:
            obj.scale = channels["scale"]

    inserted = []
    try:
        for data_path in channels:
            if not obj.keyframe_insert(data_path=data_path, frame=frame):
                raise RuntimeError(f"Blender refused keyframe insertion for {obj.name}:{data_path} at frame {frame}")
            inserted.append(data_path)
    except Exception:
        for data_path in inserted:
            with suppress(Exception):
                obj.keyframe_delete(data_path=data_path, frame=frame)
        raise
    return inserted


def _assign_batch_action(prepared, action_name, policy, slot_identifier, confirm_displace):
    """
    Put the batch's object on the named action, before a single key is written.

    An ID holds one action, so a batch naming several objects while naming one action is asking
    for every object's keys to land in the same place: whichever object is assigned last wins
    the action and the rest are keyed into whatever else was driving them. That is almost never
    what an agent means by it, and it is indistinguishable afterwards from the keys having
    worked, so it is refused rather than resolved.

    Args:
        prepared: The validated records, read for the objects they key.
        action_name: The action every key in this batch belongs in.
        policy: ENSURE, CREATE or REUSE - see `assign_named_action`.
        slot_identifier: Which of the action's slots to key into, or None to resolve it.
        confirm_displace: Whether the caller confirmed displacing an action that holds keys.

    Raises:
        ValueError: If the batch names more than one distinct object.

    """
    objects = {entry["object_name"]: entry["object"] for entry in prepared}
    if len(objects) > 1:
        raise ValueError(
            f"action_name='{action_name}' names one action, but this batch keys {len(objects)} objects "
            f"({', '.join(sorted(objects))}): an object holds one action, so call keyframe_object_transform "
            "once per object, naming the action that object's keys belong in"
        )
    assign_named_action(
        next(iter(objects.values())), action_name, policy, slot_identifier, confirm_displace=confirm_displace
    )


class ObjectAnimationHandlersMixin:
    """Keyframe an object's location/rotation/scale, in local or world space, across a scene."""

    def keyframe_object_transform(
        self,
        keyframes,
        policy="REPLACE_EXISTING",
        interpolation="BEZIER",
        handle_left="AUTO_CLAMPED",
        handle_right="AUTO_CLAMPED",
        action_name=None,
        action_policy="ENSURE",
        action_slot_identifier=None,
        confirm_displace_action=False,
    ):
        if not isinstance(keyframes, list) or not 1 <= len(keyframes) <= _MAX_BATCH:
            raise ValueError(f"keyframes must contain between 1 and {_MAX_BATCH} records")
        if policy not in _POLICIES:
            raise ValueError(f"policy must be one of {sorted(_POLICIES)}")
        style = KeyStyle(interpolation, handle_left, handle_right)
        style.validate()
        if action_policy not in ACTION_POLICIES:
            raise ValueError(f"action_policy must be one of {list(ACTION_POLICIES)}")

        prepared = []
        seen = set()
        scene_cache = {}
        for index, source in enumerate(keyframes):
            if not isinstance(source, dict):
                raise ValueError(f"keyframes[{index}] must be an object")
            record = dict(source)
            label = f"keyframes[{index}]"
            object_name = _required_name(record.get("object_name"), f"{label}.object_name")
            obj = _object(object_name)
            frame = _resolve_frame(record, label, scene_cache)
            space = record.get("space", "WORLD")
            if space not in _SPACES:
                raise ValueError(f"{label}.space must be one of {sorted(_SPACES)}")
            channels = _resolve_channels(record, label, obj)
            identity = (object_name, frame)
            if identity in seen:
                raise ValueError(
                    f"Duplicate keyframe destination at {label}: combine every channel for one object at "
                    "one frame into a single record instead of separate records"
                )
            seen.add(identity)
            prepared.append(
                {
                    "object": obj,
                    "object_name": object_name,
                    "label": label,
                    "frame": frame,
                    "space": space,
                    "channels": channels,
                }
            )

        if action_name is not None:
            _assign_batch_action(prepared, action_name, action_policy, action_slot_identifier, confirm_displace_action)
        if policy == "INSERT_ONLY":
            # Asked after the action is assigned, because "a key already exists here" is a
            # question about the action this call is about to write into, not about whatever
            # happened to be driving the object when the call arrived.
            for entry in prepared:
                existing = [path for path in entry["channels"] if _has_key_at(entry["object"], path, entry["frame"])]
                if existing:
                    raise ValueError(
                        f"A key already exists at {entry['label']} for {existing}; INSERT_ONLY made no changes"
                    )

        # Measured before a key is written: inserting one moves the extent it is measured
        # against, and the question is which cycle the call arrived to.
        warnings = _cycle_extension_warnings(prepared)
        changed_keys = []
        for entry in prepared:
            inserted = _apply_and_key(entry["object"], entry["frame"], entry["space"], entry["channels"])
            for data_path in inserted:
                changed_keys.extend(_style_inserted_keys(entry["object"], data_path, entry["frame"], style))

        changed_objects = list(dict.fromkeys(entry["object_name"] for entry in prepared))
        actions = sorted(
            {
                action.name
                for entry in prepared
                for action in [_action_fcurves(entry["object"])[0]]
                if action is not None
            }
        )
        # One slot is only well defined when one object was keyed: a batch spanning several
        # objects lands in as many slots as it has objects, and `actions` already names those.
        single_owner = prepared[0]["object"] if len(changed_objects) == 1 else None
        return {
            "keyframes": changed_keys,
            "actions": actions,
            "action_slot": assigned_slot_identifier(single_owner) if single_owner is not None else None,
            "policy": policy,
            # The envelope lifts these, so a period this call silently redefined reaches a
            # caller who read nothing but the warnings.
            "warnings": warnings,
            "changed_objects": changed_objects,
            "changed_resources": actions,
        }
