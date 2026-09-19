# pyright: reportArgumentType=false
# ruff: file-ignore[too-many-branches, too-many-locals, undocumented-public-method]
"""Blender-side handlers for generic object transform keyframing (location/rotation/scale)."""

import math

from contextlib import suppress

import bpy
import mathutils

from .scene import _object, _required_name
from .scene_physics import _scene, _scene_fps

_MIN_FRAME = -1_048_574
_MAX_FRAME = 1_048_574
_MAX_BATCH = 500
_KEYFRAME_MATCH_TOLERANCE = 1e-5
_SPACES = {"LOCAL", "WORLD"}
_POLICIES = {"INSERT_ONLY", "REPLACE_EXISTING"}
_INTERPOLATIONS = {"CONSTANT", "LINEAR", "BEZIER"}
_HANDLE_TYPES = {"FREE", "ALIGNED", "VECTOR", "AUTO", "AUTO_CLAMPED"}
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
    if not _MIN_FRAME <= value <= _MAX_FRAME:
        raise ValueError(f"{label} must be between {_MIN_FRAME} and {_MAX_FRAME}")
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
    animation = getattr(id_owner, "animation_data", None)
    action = getattr(animation, "action", None) if animation is not None else None
    if action is None:
        return None, ()
    if hasattr(action, "fcurves"):
        return action, action.fcurves
    slot = getattr(animation, "action_slot", None)
    curves = []
    for layer in getattr(action, "layers", ()):
        for strip in getattr(layer, "strips", ()):
            if getattr(strip, "type", None) != "KEYFRAME":
                continue
            channelbag = strip.channelbag(slot, ensure=False) if slot is not None else None
            if channelbag is not None:
                curves.extend(channelbag.fcurves)
    return action, curves


def _has_key_at(obj, data_path, frame):
    _action, curves = _action_fcurves(obj)
    for curve in curves:
        if curve.data_path == data_path and any(
            abs(point.co[0] - frame) <= _KEYFRAME_MATCH_TOLERANCE for point in curve.keyframe_points
        ):
            return True
    return False


def _style_inserted_keys(obj, data_path, frame, interpolation, *, handle_left, handle_right):
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
        point.interpolation = interpolation
        if interpolation == "BEZIER":
            point.handle_left_type = handle_left
            point.handle_right_type = handle_right
        changed.append({"data_path": data_path, "array_index": curve.array_index, "frame": frame})
    return changed


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


class ObjectAnimationHandlersMixin:
    """Keyframe an object's location/rotation/scale, in local or world space, across a scene."""

    def keyframe_object_transform(
        self,
        keyframes,
        policy="REPLACE_EXISTING",
        interpolation="BEZIER",
        handle_left="AUTO_CLAMPED",
        handle_right="AUTO_CLAMPED",
    ):
        if not isinstance(keyframes, list) or not 1 <= len(keyframes) <= _MAX_BATCH:
            raise ValueError(f"keyframes must contain between 1 and {_MAX_BATCH} records")
        if policy not in _POLICIES:
            raise ValueError(f"policy must be one of {sorted(_POLICIES)}")
        if interpolation not in _INTERPOLATIONS:
            raise ValueError(f"Unsupported interpolation: {interpolation}")
        if handle_left not in _HANDLE_TYPES or handle_right not in _HANDLE_TYPES:
            raise ValueError("Unsupported Bezier handle type")

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
            if policy == "INSERT_ONLY":
                existing = [path for path in channels if _has_key_at(obj, path, frame)]
                if existing:
                    raise ValueError(f"A key already exists at {label} for {existing}; INSERT_ONLY made no changes")
            prepared.append(
                {"object": obj, "object_name": object_name, "frame": frame, "space": space, "channels": channels}
            )

        changed_keys = []
        for entry in prepared:
            inserted = _apply_and_key(entry["object"], entry["frame"], entry["space"], entry["channels"])
            for data_path in inserted:
                changed_keys.extend(
                    _style_inserted_keys(
                        entry["object"],
                        data_path,
                        entry["frame"],
                        interpolation,
                        handle_left=handle_left,
                        handle_right=handle_right,
                    )
                )

        changed_objects = list(dict.fromkeys(entry["object_name"] for entry in prepared))
        actions = sorted(
            {
                action.name
                for entry in prepared
                for action in [_action_fcurves(entry["object"])[0]]
                if action is not None
            }
        )
        return {
            "keyframes": changed_keys,
            "actions": actions,
            "policy": policy,
            "changed_objects": changed_objects,
            "changed_resources": actions,
        }
