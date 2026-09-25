"""Blender-main-thread handlers for animating cloth simulation parameters."""

from __future__ import annotations

import contextlib
import math

import bpy

from ..rna_patch import get_object, rna_property, serialize, validate_rna_value
from .collisions import _affected_cloths
from .dynamics import _FIELD_WEIGHT_FIELDS
from .inspection_and_setup import (
    _action_fcurves,
    _cache_info,
    _get_modifier,
    _object_scenes,
    _reject_baked,
    _tag_update,
)

_ANIMATABLE_FIELDS = {
    "CLOTH_SETTINGS": {
        "uniform_pressure_force",
        "target_volume",
        "pressure_factor",
        "fluid_density",
        "shrink_min",
        "shrink_max",
        "pin_stiffness",
        "goal_min",
        "goal_max",
        "goal_default",
        "goal_spring",
        "goal_friction",
        "time_scale",
        "gravity",
    },
    "EFFECTOR_WEIGHTS": _FIELD_WEIGHT_FIELDS - {"apply_to_hair_growing"},
    "FIELD_SETTINGS": {"strength"},
    "COLLIDER_SETTINGS": {"thickness_outer", "cloth_friction", "damping", "use_culling", "use_normal"},
    "SHAPE_KEY": {"value"},
    "HOOK_MODIFIER": {"strength", "falloff_radius"},
    "ARMATURE_MODIFIER": {"strength"},
    "MESH_DEFORM_MODIFIER": {"strength"},
    "SURFACE_DEFORM_MODIFIER": {"strength"},
    "OBJECT": {"location", "rotation_euler", "rotation_quaternion", "scale"},
}


def _resolve_animation_owner(obj, cloth_modifier_name, record):
    owner_kind = record["owner"]
    target_name = record.get("target_name")
    cloth_modifier = None
    if owner_kind in {"CLOTH_SETTINGS", "EFFECTOR_WEIGHTS"}:
        if not cloth_modifier_name:
            raise ValueError(f"cloth_modifier_name is required for {owner_kind}")
        cloth_modifier = _get_modifier(obj, cloth_modifier_name, "CLOTH")
        owner = cloth_modifier.settings if owner_kind == "CLOTH_SETTINGS" else cloth_modifier.settings.effector_weights
        allowlist = _ANIMATABLE_FIELDS[owner_kind]
    elif owner_kind == "COLLIDER_SETTINGS":
        if obj.collision is None or not any(modifier.type == "COLLISION" for modifier in obj.modifiers):
            raise ValueError(f"Object '{obj.name}' is not a cloth collider")
        owner = obj.collision
        allowlist = _ANIMATABLE_FIELDS[owner_kind]
    elif owner_kind == "FIELD_SETTINGS":
        owner = obj.field
        if owner is None or owner.type == "NONE":
            raise ValueError(f"Object '{obj.name}' is not an active force field")
        allowlist = _ANIMATABLE_FIELDS[owner_kind]
    elif owner_kind == "SHAPE_KEY":
        shape_keys = getattr(getattr(obj, "data", None), "shape_keys", None)
        owner = shape_keys.key_blocks.get(target_name) if shape_keys and target_name else None
        if owner is None:
            raise ValueError(f"Shape key not found: {target_name}")
        allowlist = _ANIMATABLE_FIELDS[owner_kind]
    elif owner_kind == "MODIFIER":
        if not target_name:
            raise ValueError("target_name must name an attachment modifier")
        owner = obj.modifiers.get(target_name)
        if owner is None:
            raise ValueError(f"Modifier not found: {target_name}")
        key = f"{owner.type}_MODIFIER"
        allowlist = _ANIMATABLE_FIELDS.get(key)
        if allowlist is None:
            raise ValueError(f"Modifier type {owner.type} is not animatable through this tool")
    elif owner_kind == "OBJECT":
        owner = obj
        allowlist = _ANIMATABLE_FIELDS[owner_kind]
    else:
        raise ValueError(f"Unsupported animation owner: {owner_kind}")
    property_name = record["property_name"]
    if property_name not in allowlist:
        raise ValueError(f"Property '{property_name}' is not allowed for {owner_kind}")
    prop = rna_property(owner, property_name)
    if not prop.is_animatable:
        raise ValueError(f"{owner_kind}.{property_name} is not animatable in Blender {bpy.app.version_string}")
    value = record["value"]
    array_index = record.get("array_index", -1)
    if prop.is_array:
        if array_index == -1:
            validate_rna_value(owner, property_name, value)
        elif not isinstance(value, (int, float)):
            raise ValueError(f"Indexed animation of {property_name} requires one numeric value")
        elif not 0 <= array_index < prop.array_length:
            raise ValueError(f"array_index must be in [0, {prop.array_length - 1}] for {property_name}")
    else:
        if array_index != -1:
            raise ValueError(f"array_index is not valid for scalar property {property_name}")
        validate_rna_value(owner, property_name, value)
    path = owner.path_from_id(property_name)
    return owner, property_name, path, cloth_modifier


def _keyframe_points(owner, data_path, array_index, frame):
    owner_id = getattr(owner, "id_data", owner)
    animation = getattr(owner_id, "animation_data", None)
    matches = []
    for curve in _action_fcurves(animation):
        if curve.data_path != data_path or (array_index >= 0 and curve.array_index != array_index):
            continue
        for point in curve.keyframe_points:
            if abs(float(point.co[0]) - frame) <= 1e-6:
                matches.append((curve, point))
    return matches


def _set_animated_property(owner, property_name, value, array_index):
    if array_index >= 0:
        values = list(getattr(owner, property_name))
        values[array_index] = value
        setattr(owner, property_name, values)
    else:
        setattr(owner, property_name, value)


def _snapshot_keyframe_point(point):
    return {
        "co": list(point.co),
        "interpolation": point.interpolation,
        "easing": point.easing,
        "handle_left": list(point.handle_left),
        "handle_right": list(point.handle_right),
        "handle_left_type": point.handle_left_type,
        "handle_right_type": point.handle_right_type,
    }


def _restore_keyframe_point(point, snapshot):
    point.co = snapshot["co"]
    point.interpolation = snapshot["interpolation"]
    point.easing = snapshot["easing"]
    point.handle_left = snapshot["handle_left"]
    point.handle_right = snapshot["handle_right"]
    point.handle_left_type = snapshot["handle_left_type"]
    point.handle_right_type = snapshot["handle_right_type"]


def _cloths_affected_by_effector(effector):
    affected = {}
    for scene in _object_scenes(effector):
        for candidate in scene.objects:
            for modifier in candidate.modifiers:
                if modifier.type != "CLOTH":
                    continue
                collection = modifier.settings.effector_weights.collection
                if collection is None or effector.name in collection.all_objects:
                    affected[candidate.name, modifier.name] = (candidate, modifier)
    return list(affected.values())


def _cloths_depending_on_object(target):
    affected = {}
    for candidate in bpy.data.objects:
        attachment_indices = []
        for index, modifier in enumerate(candidate.modifiers):
            referenced = getattr(modifier, "object", None) == target or getattr(modifier, "target", None) == target
            if referenced and modifier.type in {"HOOK", "ARMATURE", "MESH_DEFORM", "SURFACE_DEFORM"}:
                attachment_indices.append(index)
        for attachment_index in attachment_indices:
            for cloth_modifier in list(candidate.modifiers)[attachment_index + 1 :]:
                if cloth_modifier.type == "CLOTH":
                    affected[candidate.name, cloth_modifier.name] = (candidate, cloth_modifier)
    return list(affected.values())


class ClothAnimationHandlers:
    """Blender-main-thread handlers for animating cloth simulation parameters."""

    def animate_cloth_parameters(
        self,
        object_name,
        keyframes,
        cloth_modifier_name=None,
        policy="INSERT_ONLY",
    ):
        obj = get_object(object_name)
        if policy not in {"INSERT_ONLY", "REPLACE_EXISTING"}:
            raise ValueError("policy must be INSERT_ONLY or REPLACE_EXISTING")
        if not keyframes or len(keyframes) > 500:
            raise ValueError("keyframes must contain 1-500 records")
        resolved = []
        affected = []
        seen = set()
        for index, record in enumerate(keyframes):
            frame = float(record["frame"])
            if not math.isfinite(frame) or not -1_000_000 <= frame <= 1_000_000:
                raise ValueError(f"Keyframe {index} frame must be finite and in [-1000000, 1000000]")
            owner, property_name, data_path, cloth_modifier = _resolve_animation_owner(obj, cloth_modifier_name, record)
            array_index = int(record.get("array_index", -1))
            identity = (id(owner), data_path, array_index, frame)
            if identity in seen:
                raise ValueError(f"Duplicate keyframe target at record {index}")
            seen.add(identity)
            existing = _keyframe_points(owner, data_path, array_index, frame)
            if policy == "INSERT_ONLY" and existing:
                raise ValueError(f"Key already exists for {record['owner']}.{property_name} at frame {frame:g}")
            if cloth_modifier is not None:
                affected.append((obj, cloth_modifier))
            elif record["owner"] == "COLLIDER_SETTINGS":
                affected.extend(_affected_cloths(obj))
            elif record["owner"] == "FIELD_SETTINGS":
                affected.extend(_cloths_affected_by_effector(obj))
            elif record["owner"] == "OBJECT":
                affected.extend(_cloths_depending_on_object(obj))
                affected.extend(_affected_cloths(obj))
                affected.extend((obj, mod) for mod in obj.modifiers if mod.type == "CLOTH")
            else:
                affected.extend((obj, mod) for mod in obj.modifiers if mod.type == "CLOTH")
            resolved.append(
                {
                    "record": record,
                    "owner": owner,
                    "property_name": property_name,
                    "data_path": data_path,
                    "array_index": array_index,
                    "frame": frame,
                    "old_value": serialize(getattr(owner, property_name)),
                    "existing": [(curve, point, _snapshot_keyframe_point(point)) for curve, point in existing],
                }
            )
        affected = list({(cloth.name, modifier.name): (cloth, modifier) for cloth, modifier in affected}.values())
        _reject_baked(affected)
        applied = []
        try:
            for entry in resolved:
                record = entry["record"]
                owner = entry["owner"]
                applied.append(entry)
                _set_animated_property(
                    owner,
                    entry["property_name"],
                    record["value"],
                    entry["array_index"],
                )
                inserted = owner.keyframe_insert(
                    data_path=entry["property_name"],
                    index=entry["array_index"],
                    frame=entry["frame"],
                    group="Cloth MCP",
                )
                if not inserted:
                    raise RuntimeError(
                        f"Blender did not insert {record['owner']}.{entry['property_name']} at frame {entry['frame']:g}"
                    )
                points = _keyframe_points(
                    owner,
                    entry["data_path"],
                    entry["array_index"],
                    entry["frame"],
                )
                if not points:
                    raise RuntimeError("Inserted keyframe could not be found in the owner action slot")
                for curve, point in points:
                    point.interpolation = record["interpolation"]
                    curve.update()
            _tag_update(obj)
        except Exception:
            for entry in reversed(applied):
                owner = entry["owner"]
                with contextlib.suppress(Exception):
                    setattr(owner, entry["property_name"], entry["old_value"])
                if entry["existing"]:
                    for curve, point, snapshot in entry["existing"]:
                        with contextlib.suppress(Exception):
                            _restore_keyframe_point(point, snapshot)
                            curve.update()
                else:
                    with contextlib.suppress(Exception):
                        owner.keyframe_delete(
                            data_path=entry["property_name"],
                            index=entry["array_index"],
                            frame=entry["frame"],
                        )
            raise
        keyed = []
        for entry in resolved:
            record = entry["record"]
            owner_id = getattr(entry["owner"], "id_data", entry["owner"])
            animation = getattr(owner_id, "animation_data", None)
            action = getattr(animation, "action", None)
            keyed.append(
                {
                    "owner": record["owner"],
                    "target_name": record.get("target_name"),
                    "property": entry["property_name"],
                    "data_path": entry["data_path"],
                    "array_index": entry["array_index"],
                    "frame": entry["frame"],
                    "value": serialize(getattr(entry["owner"], entry["property_name"])),
                    "interpolation": record["interpolation"],
                    "action": action.name if action else None,
                    "action_slot": getattr(getattr(animation, "action_slot", None), "identifier", None),
                }
            )
        return {
            "changed_objects": [obj.name],
            "object": obj.name,
            "policy": policy,
            "keyframes": keyed,
            "affected_cloth_caches": [
                {"object": cloth.name, "modifier": mod.name, "point_cache": _cache_info(mod.point_cache)}
                for cloth, mod in affected
            ],
            "warnings": ["Keyframed simulation dependencies invalidate unbaked cloth state."],
        }
