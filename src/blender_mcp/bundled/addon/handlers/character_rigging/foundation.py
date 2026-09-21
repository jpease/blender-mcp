"""Blender-main-thread handlers for armature foundations and skinning."""

# Blender's generated Python stubs widen many bpy collections to bpy_struct.
# pyright: reportArgumentType=false, reportGeneralTypeIssues=false

import contextlib
import math
import re

from collections import Counter, defaultdict

import bpy
import mathutils

from ...helpers import paginate, preserve_mode_and_selection, set_active, sync_from_editmode

_MAX_BONES = 100_000
_MAX_MEMBERSHIPS = 10_000_000
_BONE_DATA_FIELDS = (
    "use_deform",
    "use_inherit_rotation",
    "inherit_scale",
    "use_local_location",
    "use_relative_parent",
    "use_envelope_multiply",
    "envelope_distance",
    "envelope_weight",
    "head_radius",
    "tail_radius",
)
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
_EDIT_BONE_COPY_FIELDS = (
    "roll",
    "use_connect",
    "use_deform",
    "use_inherit_rotation",
    "inherit_scale",
    "use_local_location",
    "use_relative_parent",
    "use_envelope_multiply",
    "envelope_distance",
    "envelope_weight",
    "head_radius",
    "tail_radius",
    "bbone_segments",
    "bbone_mapping_mode",
    "bbone_x",
    "bbone_z",
    "hide_select",
    "show_wire",
)
_CONSTRAINT_COMMON = {"influence", "owner_space", "target_space"}
_CONSTRAINT_FIELDS = {
    "IK": {
        "target",
        "subtarget",
        "iterations",
        "pole_target",
        "pole_subtarget",
        "pole_angle",
        "weight",
        "orient_weight",
        "chain_count",
        "use_tail",
        "use_stretch",
    },
    "SPLINE_IK": {
        "target",
        "chain_count",
        "use_chain_offset",
        "use_even_divisions",
        "xz_scale_mode",
        "y_scale_mode",
        "use_original_scale",
        "bulge",
    },
    "COPY_TRANSFORMS": {"target", "subtarget", "remove_target_shear", "mix_mode", "head_tail"},
    "COPY_LOCATION": {
        "target",
        "subtarget",
        "use_x",
        "use_y",
        "use_z",
        "invert_x",
        "invert_y",
        "invert_z",
        "use_offset",
        "head_tail",
    },
    "COPY_ROTATION": {
        "target",
        "subtarget",
        "use_x",
        "use_y",
        "use_z",
        "invert_x",
        "invert_y",
        "invert_z",
        "mix_mode",
        "euler_order",
    },
    "COPY_SCALE": {
        "target",
        "subtarget",
        "use_x",
        "use_y",
        "use_z",
        "power",
        "use_make_uniform",
        "use_offset",
        "use_add",
    },
    "CHILD_OF": {
        "target",
        "subtarget",
        "inverse_matrix",
        "use_location_x",
        "use_location_y",
        "use_location_z",
        "use_rotation_x",
        "use_rotation_y",
        "use_rotation_z",
        "use_scale_x",
        "use_scale_y",
        "use_scale_z",
    },
    "DAMPED_TRACK": {"target", "subtarget", "track_axis", "head_tail"},
    "TRACK_TO": {"target", "subtarget", "track_axis", "up_axis", "head_tail"},
    "STRETCH_TO": {"target", "subtarget", "head_tail", "volume", "keep_axis", "rest_length", "bulge"},
    "LIMIT_LOCATION": {
        "use_min_x",
        "use_min_y",
        "use_min_z",
        "use_max_x",
        "use_max_y",
        "use_max_z",
        "min_x",
        "min_y",
        "min_z",
        "max_x",
        "max_y",
        "max_z",
        "use_transform_limit",
    },
    "LIMIT_ROTATION": {
        "use_limit_x",
        "use_limit_y",
        "use_limit_z",
        "min_x",
        "min_y",
        "min_z",
        "max_x",
        "max_y",
        "max_z",
        "use_transform_limit",
    },
    "LIMIT_SCALE": {
        "use_min_x",
        "use_min_y",
        "use_min_z",
        "use_max_x",
        "use_max_y",
        "use_max_z",
        "min_x",
        "min_y",
        "min_z",
        "max_x",
        "max_y",
        "max_z",
        "use_transform_limit",
    },
    "TRANSFORM": {
        "target",
        "subtarget",
        "map_from",
        "map_to",
        "map_to_x_from",
        "map_to_y_from",
        "map_to_z_from",
        "use_motion_extrapolate",
    },
    "ACTION": {
        "target",
        "subtarget",
        "action",
        "action_slot",
        "transform_channel",
        "frame_start",
        "frame_end",
        "min",
        "max",
        "mix_mode",
    },
}
_BONE_PATH = re.compile(r'pose\.bones\["((?:[^"\\]|\\.)*)"\]')


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


def _transform_info(obj):
    local_location, local_rotation, local_scale = obj.matrix_basis.decompose()
    location, rotation, scale = obj.matrix_world.decompose()
    if obj.rotation_mode == "QUATERNION":
        native_rotation = list(obj.rotation_quaternion)
    elif obj.rotation_mode == "AXIS_ANGLE":
        native_rotation = list(obj.rotation_axis_angle)
    else:
        native_rotation = list(obj.rotation_euler)
    return {
        "local": {
            "space": "OBJECT_LOCAL",
            "matrix": _matrix_list(obj.matrix_basis),
            "location": list(local_location),
            "rotation_mode": obj.rotation_mode,
            "rotation": native_rotation,
            "rotation_quaternion": list(local_rotation),
            "scale": list(local_scale),
        },
        "world": {
            "space": "WORLD",
            "matrix": _matrix_list(obj.matrix_world),
            "location": list(location),
            "rotation_quaternion": list(rotation),
            "scale": list(scale),
        },
    }


def _constraint_info(constraint):
    result = {
        "name": constraint.name,
        "type": constraint.type,
        "influence": float(constraint.influence),
        "mute": bool(constraint.mute),
        "is_valid": bool(getattr(constraint, "is_valid", True)),
        "owner_space": getattr(constraint, "owner_space", None),
        "target_space": getattr(constraint, "target_space", None),
    }
    for field in sorted(_CONSTRAINT_FIELDS.get(constraint.type, ())):
        if hasattr(constraint, field):
            value = getattr(constraint, field)
            result[field] = getattr(value, "identifier", None) if field == "action_slot" else _plain(value)
    return result


def _animation_info(owner):
    animation = getattr(owner, "animation_data", None)
    if animation is None:
        return {"action": None, "nla_tracks": [], "drivers": []}
    action = getattr(animation, "action", None)
    tracks = []
    for track in animation.nla_tracks:
        tracks.append(
            {
                "name": track.name,
                "mute": bool(track.mute),
                "is_solo": bool(track.is_solo),
                "strips": [
                    {
                        "name": strip.name,
                        "action": getattr(strip.action, "name", None),
                        "frame_start": float(strip.frame_start),
                        "frame_end": float(strip.frame_end),
                    }
                    for strip in track.strips
                ],
            }
        )
    drivers = []
    for curve in animation.drivers:
        variables = []
        for variable in curve.driver.variables:
            variables.append(
                {
                    "name": variable.name,
                    "type": variable.type,
                    "targets": [
                        {
                            "id": getattr(target.id, "name", None),
                            "data_path": target.data_path,
                            "bone_target": target.bone_target,
                            "transform_type": target.transform_type,
                            "transform_space": target.transform_space,
                        }
                        for target in variable.targets
                    ],
                }
            )
        drivers.append(
            {
                "data_path": curve.data_path,
                "array_index": curve.array_index,
                "type": curve.driver.type,
                "expression": curve.driver.expression,
                "variables": variables,
            }
        )
    return {
        "action": getattr(action, "name", None),
        "action_frame_range": list(action.frame_range) if action is not None else None,
        "nla_tracks": tracks,
        "drivers": drivers,
    }


def _bone_info(bone, include_custom_properties=True):
    _axis, roll = bone.AxisRollFromMatrix(bone.matrix_local.to_3x3())
    return {
        "name": bone.name,
        "parent": getattr(bone.parent, "name", None),
        "children": [child.name for child in bone.children],
        "use_connect": bool(bone.use_connect),
        "use_deform": bool(bone.use_deform),
        "head": list(bone.head_local),
        "tail": list(bone.tail_local),
        "roll": float(roll),
        "length": float(bone.length),
        "coordinate_space": "ARMATURE_LOCAL_REST",
        "inherit_scale": bone.inherit_scale,
        "use_inherit_rotation": bool(bone.use_inherit_rotation),
        "use_local_location": bool(bone.use_local_location),
        "collections": [collection.name for collection in bone.collections],
        "envelope": {
            "distance": float(bone.envelope_distance),
            "weight": float(bone.envelope_weight),
            "head_radius": float(bone.head_radius),
            "tail_radius": float(bone.tail_radius),
            "multiply": bool(bone.use_envelope_multiply),
        },
        "bbone": {
            "segments": int(bone.bbone_segments),
            "x": float(bone.bbone_x),
            "z": float(bone.bbone_z),
            "mapping_mode": bone.bbone_mapping_mode,
            "handle_type_start": bone.bbone_handle_type_start,
            "handle_type_end": bone.bbone_handle_type_end,
            "custom_handle_start": getattr(bone.bbone_custom_handle_start, "name", None),
            "custom_handle_end": getattr(bone.bbone_custom_handle_end, "name", None),
            "handle_use_scale_start": list(bone.bbone_handle_use_scale_start),
            "handle_use_scale_end": list(bone.bbone_handle_use_scale_end),
            "handle_use_ease_start": bool(bone.bbone_handle_use_ease_start),
            "handle_use_ease_end": bool(bone.bbone_handle_use_ease_end),
            "rollin": float(bone.bbone_rollin),
            "rollout": float(bone.bbone_rollout),
            "curveinx": float(bone.bbone_curveinx),
            "curveinz": float(bone.bbone_curveinz),
            "curveoutx": float(bone.bbone_curveoutx),
            "curveoutz": float(bone.bbone_curveoutz),
            "easein": float(bone.bbone_easein),
            "easeout": float(bone.bbone_easeout),
            "scalein": list(bone.bbone_scalein),
            "scaleout": list(bone.bbone_scaleout),
            "use_scale_easing": bool(bone.use_scale_easing),
        },
        "custom_properties": _custom_properties(bone) if include_custom_properties else None,
    }


def _pose_bone_info(armature_obj, pose_bone, include_custom_properties):
    constraints = list(pose_bone.constraints)
    result = {
        "name": pose_bone.name,
        "rotation_mode": pose_bone.rotation_mode,
        "location": list(pose_bone.location),
        "rotation_quaternion": list(pose_bone.rotation_quaternion),
        "rotation_axis_angle": list(pose_bone.rotation_axis_angle),
        "rotation_euler": list(pose_bone.rotation_euler),
        "scale": list(pose_bone.scale),
        "matrix_basis": _matrix_list(pose_bone.matrix_basis),
        "matrix_armature": _matrix_list(pose_bone.matrix),
        "matrix_world": _matrix_list(armature_obj.matrix_world @ pose_bone.matrix),
        "locks": {
            "location": list(pose_bone.lock_location),
            "rotation": list(pose_bone.lock_rotation),
            "rotation_w": bool(pose_bone.lock_rotation_w),
            "rotations_4d": bool(pose_bone.lock_rotations_4d),
            "scale": list(pose_bone.lock_scale),
        },
        "ik": {
            field: _plain(getattr(pose_bone, field))
            for field in _POSE_FIELDS
            if field.startswith(("lock_ik", "use_ik", "ik_"))
        },
        "custom_shape": getattr(pose_bone.custom_shape, "name", None),
        "custom_shape_transform": getattr(pose_bone.custom_shape_transform, "name", None),
        "constraints": [_constraint_info(constraint) for constraint in constraints[:200]],
        "constraint_count": len(constraints),
        "constraints_truncated": len(constraints) > 200,
    }
    result["ik"].update(
        {
            "use_ik_rotation_control": bool(pose_bone.use_ik_rotation_control),
            "use_ik_linear_control": bool(pose_bone.use_ik_linear_control),
            "ik_rotation_weight": float(pose_bone.ik_rotation_weight),
            "ik_linear_weight": float(pose_bone.ik_linear_weight),
        }
    )
    if include_custom_properties:
        result["custom_properties"] = _custom_properties(pose_bone)
    return result


def _bone_collection_info(collection):
    return {
        "name": collection.name,
        "parent": getattr(collection.parent, "name", None),
        "index": int(collection.index),
        "child_number": int(collection.child_number),
        "is_visible": bool(collection.is_visible),
        "is_visible_effectively": bool(collection.is_visible_effectively),
        "is_solo": bool(collection.is_solo),
        "bones": [bone.name for bone in collection.bones],
    }


def _armature_modifier_info(modifier):
    return {
        "name": modifier.name,
        "stack_index": list(modifier.id_data.modifiers).index(modifier),
        "target": getattr(modifier.object, "name", None),
        "use_vertex_groups": bool(modifier.use_vertex_groups),
        "use_bone_envelopes": bool(modifier.use_bone_envelopes),
        "use_deform_preserve_volume": bool(modifier.use_deform_preserve_volume),
        "show_viewport": bool(modifier.show_viewport),
        "show_render": bool(modifier.show_render),
    }


def _dependent_meshes(armature_obj):
    dependencies = []
    for obj in bpy.data.objects:
        if obj.type != "MESH":
            continue
        modifiers = [
            modifier for modifier in obj.modifiers if modifier.type == "ARMATURE" and modifier.object == armature_obj
        ]
        if modifiers or obj.parent == armature_obj:
            dependencies.append(
                {
                    "object": obj.name,
                    "parented": obj.parent == armature_obj,
                    "modifiers": [_armature_modifier_info(modifier) for modifier in modifiers],
                }
            )
    return dependencies


def _mesh_uses_armature(mesh_obj, armature_obj):
    return mesh_obj.parent == armature_obj or any(
        modifier.type == "ARMATURE" and modifier.object == armature_obj for modifier in mesh_obj.modifiers
    )


def _deform_names(armature_obj):
    return {bone.name for bone in armature_obj.data.bones if bone.use_deform}


def _vertex_weight_map(mesh_obj, vertex):
    return {
        mesh_obj.vertex_groups[item.group].name: float(item.weight)
        for item in vertex.groups
        if item.group < len(mesh_obj.vertex_groups)
    }


def _skinning_record(mesh_obj, armature_obj, influence_limit, tolerance, epsilon):
    sync_from_editmode(mesh_obj)
    deform_names = _deform_names(armature_obj)
    group_names = {group.name for group in mesh_obj.vertex_groups}
    group_stats = {
        group.name: {
            "group": group.name,
            "locked": bool(group.lock_weight),
            "vertices": 0,
            "total_weight": 0.0,
            "max_weight": 0.0,
        }
        for group in mesh_obj.vertex_groups
    }
    memberships = []
    unweighted = []
    excessive = []
    non_normalized = []
    near_zero = []
    for vertex in mesh_obj.data.vertices:
        weights = _vertex_weight_map(mesh_obj, vertex)
        deform_weights = {name: weight for name, weight in weights.items() if name in deform_names and weight > epsilon}
        if not deform_weights:
            unweighted.append(vertex.index)
        if len(deform_weights) > influence_limit:
            excessive.append({"vertex": vertex.index, "count": len(deform_weights)})
        total = sum(deform_weights.values())
        if deform_weights and abs(total - 1.0) > tolerance:
            non_normalized.append({"vertex": vertex.index, "sum": total})
        for name, weight in weights.items():
            stats = group_stats[name]
            stats["vertices"] += 1
            stats["total_weight"] += weight
            stats["max_weight"] = max(stats["max_weight"], weight)
            memberships.append({"mesh": mesh_obj.name, "vertex": vertex.index, "group": name, "weight": weight})
            if weight <= epsilon:
                near_zero.append({"vertex": vertex.index, "group": name, "weight": weight})
    for stats in group_stats.values():
        stats["mean_weight"] = stats["total_weight"] / stats["vertices"] if stats["vertices"] else 0.0
    modifiers = [modifier for modifier in mesh_obj.modifiers if modifier.type == "ARMATURE"]
    return {
        "mesh": mesh_obj.name,
        "base_mesh": mesh_obj.data.name,
        "coordinate_space": "BASE_MESH_LOCAL",
        "parent": getattr(mesh_obj.parent, "name", None),
        "parent_type": mesh_obj.parent_type,
        "armature_modifiers": [_armature_modifier_info(modifier) for modifier in modifiers],
        "vertex_groups": list(group_stats.values()),
        "deform_bone_groups": sorted(group_names & deform_names),
        "absent_deform_groups": sorted(deform_names - group_names),
        "orphan_groups": sorted(group_names - deform_names),
        "groups_for_missing_bones": sorted(group_names - {bone.name for bone in armature_obj.data.bones}),
        "unweighted_vertices": unweighted,
        "excessive_influences": excessive,
        "non_normalized_vertices": non_normalized,
        "zero_or_near_zero_assignments": near_zero,
        "memberships": memberships,
    }


def _hierarchy_cycles(parent_by_name):
    cycles = []
    visited = set()
    active = []
    active_set = set()

    def visit(name):
        if name in active_set:
            start = active.index(name)
            cycles.append([*active[start:], name])
            return
        if name in visited:
            return
        active.append(name)
        active_set.add(name)
        parent = parent_by_name.get(name)
        if parent is not None:
            visit(parent)
        active.pop()
        active_set.remove(name)
        visited.add(name)

    for name in parent_by_name:
        visit(name)
    return cycles


def _validate_bone_specs(specs, collection_names):
    names = [_required_name(spec["name"], "bone.name") for spec in specs]
    _unique_names(names, "bone names")
    name_set = set(names)
    parents = {}
    for spec in specs:
        name = spec["name"]
        head = _vector_tuple(spec.get("head"), f"bone '{name}' head")
        tail = _vector_tuple(spec.get("tail"), f"bone '{name}' tail")
        if _distance(tail, head) <= 1e-8:
            raise ValueError(f"Bone '{name}' must have non-zero length")
        parent = spec.get("parent")
        if parent is not None and parent not in name_set:
            raise ValueError(f"Parent bone '{parent}' for '{name}' is not in the requested hierarchy")
        if parent == name:
            raise ValueError(f"Bone '{name}' cannot parent itself")
        parents[name] = parent
        missing = set(spec.get("collections", ())) - collection_names
        if missing:
            raise ValueError(f"Bone '{name}' references missing collections: {', '.join(sorted(missing))}")
    cycles = _hierarchy_cycles(parents)
    if cycles:
        raise ValueError(f"Bone hierarchy contains a cycle: {' -> '.join(cycles[0])}")
    by_name = {spec["name"]: spec for spec in specs}
    for spec in specs:
        if spec.get("use_connect") and spec.get("parent"):
            parent_tail = _vector_tuple(by_name[spec["parent"]]["tail"], "parent tail")
            if _distance(_vector_tuple(spec["head"], "connected head"), parent_tail) > 1e-6:
                raise ValueError(f"Connected bone '{spec['name']}' head must equal parent '{spec['parent']}' tail")


def _enter_armature_edit(obj):
    set_active(obj)
    result = bpy.ops.object.mode_set(mode="EDIT")
    if isinstance(result, (set, frozenset)) and "FINISHED" not in result:
        raise RuntimeError(f"Could not enter Edit Mode for armature '{obj.name}': {result}")


def _exit_object_mode():
    if bpy.context.mode != "OBJECT":
        result = bpy.ops.object.mode_set(mode="OBJECT")
        if isinstance(result, (set, frozenset)) and "FINISHED" not in result:
            raise RuntimeError(f"Could not return to Object Mode: {result}")


def _set_edit_bone_fields(edit_bone, spec):
    edit_bone.head = _vector(spec["head"], f"bone '{edit_bone.name}' head")
    edit_bone.tail = _vector(spec["tail"], f"bone '{edit_bone.name}' tail")
    for field in (
        "roll",
        "use_connect",
        "use_deform",
        "inherit_scale",
        "envelope_distance",
        "envelope_weight",
        "head_radius",
        "tail_radius",
    ):
        if field in spec:
            setattr(edit_bone, field, spec[field])


def _has_animation(armature_obj):
    for owner in _rig_animation_owners(armature_obj):
        animation = getattr(owner, "animation_data", None)
        if animation is not None and (
            getattr(animation, "action", None) is not None or len(animation.nla_tracks) or len(animation.drivers)
        ):
            return True
    return False


def _all_fcurves(owner):
    animation = getattr(owner, "animation_data", None)
    if animation is None:
        return []
    curves = list(animation.drivers)
    for action in _animation_actions(owner):
        for collection in _action_fcurve_collections(action):
            curves.extend(collection)
    return list({curve.as_pointer(): curve for curve in curves}.values())


def _animation_actions(owner):
    animation = getattr(owner, "animation_data", None)
    if animation is None:
        return []
    actions = []
    action = getattr(animation, "action", None)
    if action is not None:
        actions.append(action)
    for track in animation.nla_tracks:
        for strip in track.strips:
            if strip.action is not None:
                actions.append(strip.action)
    return list({action.as_pointer(): action for action in actions}.values())


def _action_fcurve_collections(action):
    """Return mutable F-Curve collections for legacy and Blender 5.1 layered Actions."""
    collections = []
    legacy = getattr(action, "fcurves", None)
    if legacy is not None:
        collections.append(legacy)
    for layer in getattr(action, "layers", ()):
        for strip in getattr(layer, "strips", ()):
            for channelbag in getattr(strip, "channelbags", ()):
                collections.append(channelbag.fcurves)
    return list({id(collection): collection for collection in collections}.values())


def _armature_users(armature_obj):
    return [obj for obj in bpy.data.objects if obj.type == "ARMATURE" and obj.data == armature_obj.data]


def _rig_animation_owners(armature_obj):
    return [*_armature_users(armature_obj), armature_obj.data]


def _mesh_uses_armature_data(mesh_obj, armature_obj):
    users = set(_armature_users(armature_obj))
    return mesh_obj.parent in users or any(
        modifier.type == "ARMATURE" and modifier.object in users for modifier in mesh_obj.modifiers
    )


def _bone_dependencies(armature_obj, bone_name):
    dependencies = []
    users = _armature_users(armature_obj)
    for obj in bpy.data.objects:
        if obj.type == "MESH" and _mesh_uses_armature_data(obj, armature_obj):
            group = obj.vertex_groups.get(bone_name)
            if group is not None:
                dependencies.append({"kind": "VERTEX_GROUP", "object": obj.name, "name": group.name})
        for constraint in getattr(obj, "constraints", ()):
            if getattr(constraint, "target", None) in users and getattr(constraint, "subtarget", "") == bone_name:
                dependencies.append({"kind": "OBJECT_CONSTRAINT", "object": obj.name, "name": constraint.name})
    for user in users:
        for pose_bone in user.pose.bones:
            for constraint in pose_bone.constraints:
                if getattr(constraint, "target", None) in users and getattr(constraint, "subtarget", "") == bone_name:
                    dependencies.append(
                        {
                            "kind": "POSE_CONSTRAINT",
                            "object": user.name,
                            "bone": pose_bone.name,
                            "name": constraint.name,
                        }
                    )
    token = _bone_path_token(bone_name)
    for owner in _rig_animation_owners(armature_obj):
        for curve in _all_fcurves(owner):
            if token in curve.data_path:
                dependencies.append({"kind": "FCURVE", "owner": owner.name, "data_path": curve.data_path})
            driver = getattr(curve, "driver", None)
            for variable in getattr(driver, "variables", ()) if driver is not None else ():
                for target in variable.targets:
                    if target.id in users and target.bone_target == bone_name:
                        dependencies.append(
                            {"kind": "DRIVER_TARGET", "owner": owner.name, "data_path": curve.data_path}
                        )
    return dependencies


def _rename_references(armature_obj, old_name, new_name):
    affected = []
    users = _armature_users(armature_obj)
    for obj in bpy.data.objects:
        if obj.type == "MESH" and _mesh_uses_armature_data(obj, armature_obj):
            group = obj.vertex_groups.get(old_name)
            if group is not None:
                group.name = new_name
                affected.append({"kind": "VERTEX_GROUP", "object": obj.name, "old": old_name, "new": group.name})
        for constraint in getattr(obj, "constraints", ()):
            if getattr(constraint, "target", None) in users and getattr(constraint, "subtarget", "") == old_name:
                constraint.subtarget = new_name
                affected.append({"kind": "OBJECT_CONSTRAINT", "object": obj.name, "name": constraint.name})
    for user in users:
        for pose_bone in user.pose.bones:
            for constraint in pose_bone.constraints:
                if getattr(constraint, "target", None) in users and getattr(constraint, "subtarget", "") == old_name:
                    constraint.subtarget = new_name
                    affected.append(
                        {
                            "kind": "POSE_CONSTRAINT",
                            "object": user.name,
                            "bone": pose_bone.name,
                            "name": constraint.name,
                        }
                    )
    old_token = _bone_path_token(old_name)
    new_token = _bone_path_token(new_name)
    for owner in _rig_animation_owners(armature_obj):
        for curve in _all_fcurves(owner):
            old_path = curve.data_path
            curve.data_path = old_path.replace(old_token, new_token)
            if curve.data_path != old_path:
                affected.append({"kind": "FCURVE", "owner": owner.name, "old": old_path, "new": curve.data_path})
            driver = getattr(curve, "driver", None)
            for variable in getattr(driver, "variables", ()) if driver is not None else ():
                for target in variable.targets:
                    if target.id in users and target.bone_target == old_name:
                        target.bone_target = new_name
                        affected.append({"kind": "DRIVER_TARGET", "owner": owner.name, "data_path": curve.data_path})
    return affected


def _remove_bone_references(armature_obj, bone_name):
    affected = []
    users = _armature_users(armature_obj)
    for obj in bpy.data.objects:
        if obj.type == "MESH" and _mesh_uses_armature_data(obj, armature_obj):
            group = obj.vertex_groups.get(bone_name)
            if group is not None:
                obj.vertex_groups.remove(group)
                affected.append({"kind": "VERTEX_GROUP", "object": obj.name, "name": bone_name})
        for constraint in getattr(obj, "constraints", ()):
            if getattr(constraint, "target", None) in users and getattr(constraint, "subtarget", "") == bone_name:
                constraint.subtarget = ""
                affected.append({"kind": "OBJECT_CONSTRAINT_SUBTARGET", "object": obj.name, "name": constraint.name})
    for user in users:
        for pose_bone in user.pose.bones:
            for constraint in pose_bone.constraints:
                if getattr(constraint, "target", None) in users and getattr(constraint, "subtarget", "") == bone_name:
                    constraint.subtarget = ""
                    affected.append(
                        {
                            "kind": "POSE_CONSTRAINT_SUBTARGET",
                            "object": user.name,
                            "bone": pose_bone.name,
                            "name": constraint.name,
                        }
                    )
    token = _bone_path_token(bone_name)
    for owner in _rig_animation_owners(armature_obj):
        animation = getattr(owner, "animation_data", None)
        if animation is None:
            continue
        for action in _animation_actions(owner):
            for collection in _action_fcurve_collections(action):
                for curve in list(collection):
                    if token in curve.data_path:
                        path = curve.data_path
                        collection.remove(curve)
                        affected.append(
                            {"kind": "FCURVE", "owner": owner.name, "action": action.name, "data_path": path}
                        )
        for curve in list(animation.drivers):
            if token in curve.data_path:
                path = curve.data_path
                animation.drivers.remove(curve)
                affected.append({"kind": "DRIVER", "owner": owner.name, "data_path": path})
                continue
            for variable in curve.driver.variables:
                for target in variable.targets:
                    if target.id in users and target.bone_target == bone_name:
                        target.bone_target = ""
                        affected.append(
                            {
                                "kind": "DRIVER_TARGET",
                                "owner": owner.name,
                                "data_path": curve.data_path,
                            }
                        )
    return affected


def _references_any_bone(curve, armature_users, bone_names):
    if any(_bone_path_token(name) in curve.data_path for name in bone_names):
        return True
    driver = getattr(curve, "driver", None)
    return any(
        target.id in armature_users and target.bone_target in bone_names
        for variable in getattr(driver, "variables", ())
        if driver is not None
        for target in variable.targets
    )


def _bone_path_token(bone_name):
    escaped = bone_name.replace("\\", "\\\\").replace('"', '\\"')
    return f'pose.bones["{escaped}"]'


def _restore_drivers(owner, backup_holder):
    animation = getattr(owner, "animation_data", None)
    if animation is not None:
        for curve in list(animation.drivers):
            animation.drivers.remove(curve)
    backup_animation = getattr(backup_holder, "animation_data", None)
    if backup_animation is None or not len(backup_animation.drivers):
        return
    owner_animation = owner.animation_data_create()
    for curve in backup_animation.drivers:
        owner_animation.drivers.from_existing(src_driver=curve)


@contextlib.contextmanager
def _bone_reference_transaction(armature_obj, bone_names):
    """Rollback every external reference edited by rename/delete operations."""
    names = set(bone_names)
    users = set(_armature_users(armature_obj))
    group_snapshots = []
    constraint_snapshots = []
    action_copies = []
    driver_backups = []
    try:
        for obj in bpy.data.objects:
            if (
                obj.type == "MESH"
                and _mesh_uses_armature_data(obj, armature_obj)
                and any(obj.vertex_groups.get(name) is not None for name in names)
            ):
                group_snapshots.append((obj, _snapshot_groups(obj)))
            for constraint in getattr(obj, "constraints", ()):
                if getattr(constraint, "target", None) in users and constraint.subtarget in names:
                    constraint_snapshots.append((constraint, constraint.subtarget))
        for user in users:
            for pose_bone in user.pose.bones:
                for constraint in pose_bone.constraints:
                    if getattr(constraint, "target", None) in users and constraint.subtarget in names:
                        constraint_snapshots.append((constraint, constraint.subtarget))

        owners = _rig_animation_owners(armature_obj)
        actions = {
            action.as_pointer(): action
            for owner in owners
            for action in _animation_actions(owner)
            if any(
                any(_bone_path_token(name) in curve.data_path for name in names)
                for collection in _action_fcurve_collections(action)
                for curve in collection
            )
        }
        for action in actions.values():
            original_name = action.name
            backup = action.copy()
            action_copies.append((action, backup, original_name))

        for owner in owners:
            animation = getattr(owner, "animation_data", None)
            if animation is None or not any(_references_any_bone(curve, users, names) for curve in animation.drivers):
                continue
            backup_holder = bpy.data.objects.new(f"{owner.name}.MCP Driver Backup", None)
            backup_animation = backup_holder.animation_data_create()
            for curve in animation.drivers:
                backup_animation.drivers.from_existing(src_driver=curve)
            driver_backups.append((owner, backup_holder))
        yield
    except Exception:
        for owner, backup_holder in reversed(driver_backups):
            _restore_drivers(owner, backup_holder)
        for action, backup, original_name in reversed(action_copies):
            action.user_remap(backup)
            if bpy.data.actions.get(action.name) is action:
                bpy.data.actions.remove(action, do_unlink=True)
            backup.name = original_name
        for constraint, subtarget in reversed(constraint_snapshots):
            constraint.subtarget = subtarget
        for mesh, snapshot in reversed(group_snapshots):
            _restore_groups(mesh, snapshot)
        raise
    else:
        for _action, backup, _original_name in action_copies:
            if bpy.data.actions.get(backup.name) is backup:
                bpy.data.actions.remove(backup, do_unlink=True)
    finally:
        for _owner, backup_holder in driver_backups:
            if bpy.data.objects.get(backup_holder.name) is backup_holder:
                bpy.data.objects.remove(backup_holder, do_unlink=True)


def _snapshot_groups(mesh_obj):
    sync_from_editmode(mesh_obj)
    groups = [{"name": group.name, "lock_weight": bool(group.lock_weight)} for group in mesh_obj.vertex_groups]
    weights = [_vertex_weight_map(mesh_obj, vertex) for vertex in mesh_obj.data.vertices]
    return groups, weights


def _restore_groups(mesh_obj, snapshot):
    groups, weights = snapshot
    for group in list(mesh_obj.vertex_groups):
        mesh_obj.vertex_groups.remove(group)
    for record in groups:
        group = mesh_obj.vertex_groups.new(name=record["name"])
        group.lock_weight = record["lock_weight"]
    for vertex_index, assignments in enumerate(weights):
        for name, weight in assignments.items():
            mesh_obj.vertex_groups[name].add([vertex_index], weight, "REPLACE")


def _snapshot_modifiers(mesh_obj):
    return [
        {
            "name": modifier.name,
            "type": modifier.type,
            "object": getattr(modifier, "object", None),
            "use_vertex_groups": getattr(modifier, "use_vertex_groups", None),
            "use_bone_envelopes": getattr(modifier, "use_bone_envelopes", None),
            "use_deform_preserve_volume": getattr(modifier, "use_deform_preserve_volume", None),
        }
        for modifier in mesh_obj.modifiers
    ]


def _restore_armature_modifiers(mesh_obj, records):
    for modifier in list(mesh_obj.modifiers):
        if modifier.type == "ARMATURE":
            mesh_obj.modifiers.remove(modifier)
    for index, record in enumerate(records):
        if record["type"] != "ARMATURE":
            continue
        modifier = mesh_obj.modifiers.new(name=record["name"], type="ARMATURE")
        for field in ("object", "use_vertex_groups", "use_bone_envelopes", "use_deform_preserve_volume"):
            if record[field] is not None:
                setattr(modifier, field, record[field])
        mesh_obj.modifiers.move(len(mesh_obj.modifiers) - 1, min(index, len(mesh_obj.modifiers) - 1))


def _influence_histogram(weight_maps, included_names):
    return dict(
        sorted(
            Counter(
                sum(1 for name, weight in weights.items() if name in included_names and weight > 0)
                for weights in weight_maps
            ).items()
        )
    )


def _issue(code, severity, message, **location):
    return {"code": code, "severity": severity, "message": message, **location}


@contextlib.contextmanager
def _working_armature_data(armature_obj):
    """Edit a private copy, atomically swapping every user only after the body succeeds."""
    original = armature_obj.data
    if not getattr(original, "is_editable", True):
        raise ValueError(f"Armature data '{original.name}' is linked or otherwise not editable")
    original_name = original.name
    users = [obj for obj in bpy.data.objects if obj.data == original]
    working = original.copy()
    working.name = f"{original_name}.MCP Working"
    for obj in users:
        obj.data = working
    try:
        yield working, users
    except Exception:
        for obj in users:
            obj.data = original
        bpy.data.armatures.remove(working, do_unlink=True)
        raise
    else:
        bpy.data.armatures.remove(original, do_unlink=True)
        working.name = original_name


@contextlib.contextmanager
def _working_armature_with_references(armature_obj, operations):
    """Apply final reference outcomes around one atomic rest-data edit."""
    reference_outcomes = {bone.name: bone.name for bone in armature_obj.data.bones}
    for operation in operations:
        if operation["operation"] == "RENAME" and operation["reference_policy"] == "UPDATE":
            old_name = operation["bone_name"]
            for original_name, current_name in reference_outcomes.items():
                if current_name == old_name:
                    reference_outcomes[original_name] = operation["new_name"]
        elif operation["operation"] == "DELETE" and operation["reference_policy"] == "REMOVE_REFERENCES":
            deleted_name = operation["bone_name"]
            for original_name, current_name in reference_outcomes.items():
                if current_name == deleted_name:
                    reference_outcomes[original_name] = None
    reference_outcomes = {
        original_name: final_name
        for original_name, final_name in reference_outcomes.items()
        if final_name != original_name
    }
    with _bone_reference_transaction(armature_obj, reference_outcomes):
        affected = []
        for original_name, final_name in reference_outcomes.items():
            if final_name is None:
                affected.extend(_remove_bone_references(armature_obj, original_name))
        with _working_armature_data(armature_obj) as working:
            armature_data, users = working
            yield armature_data, users, affected
            for original_name, final_name in reference_outcomes.items():
                if final_name is not None:
                    affected.extend(_rename_references(armature_obj, original_name, final_name))


def _edit_bone_specs(armature_obj):
    specs = []
    with preserve_mode_and_selection():
        _enter_armature_edit(armature_obj)
        try:
            for bone in armature_obj.data.edit_bones:
                specs.append(
                    {
                        "name": bone.name,
                        "head": list(bone.head),
                        "tail": list(bone.tail),
                        "roll": float(bone.roll),
                        "parent": getattr(bone.parent, "name", None),
                        "use_connect": bool(bone.use_connect),
                        "use_deform": bool(bone.use_deform),
                        "inherit_scale": bone.inherit_scale,
                        "envelope_distance": float(bone.envelope_distance),
                        "envelope_weight": float(bone.envelope_weight),
                        "head_radius": float(bone.head_radius),
                        "tail_radius": float(bone.tail_radius),
                        "collections": [collection.name for collection in bone.collections],
                    }
                )
        finally:
            _exit_object_mode()
    return specs


def _apply_patch_to_specs(specs, operations):
    final = {spec["name"]: dict(spec) for spec in specs}
    rename_map = {}
    deleted = []
    for operation in operations:
        kind = operation["operation"]
        if kind == "CREATE":
            name = operation["name"]
            if name in final:
                raise ValueError(f"Bone already exists: {name}")
            final[name] = {key: value for key, value in operation.items() if key != "operation"}
        elif kind == "RENAME":
            old = operation["bone_name"]
            new = operation["new_name"]
            if old not in final:
                raise ValueError(f"Bone not found: {old}")
            if new in final:
                raise ValueError(f"Bone name collision: {new}")
            spec = final.pop(old)
            spec["name"] = new
            final[new] = spec
            for child in final.values():
                if child.get("parent") == old:
                    child["parent"] = new
            rename_map[old] = new
        elif kind == "UPDATE":
            name = operation["bone_name"]
            if name not in final:
                raise ValueError(f"Bone not found: {name}")
            spec = final[name]
            if operation.get("clear_parent"):
                spec["parent"] = None
            for field in (
                "head",
                "tail",
                "roll",
                "parent",
                "use_connect",
                "use_deform",
                "inherit_scale",
                "envelope_distance",
                "envelope_weight",
                "head_radius",
                "tail_radius",
            ):
                if field in operation:
                    spec[field] = operation[field]
        elif kind == "DELETE":
            name = operation["bone_name"]
            if name not in final:
                raise ValueError(f"Bone not found: {name}")
            children = [child["name"] for child in final.values() if child.get("parent") == name]
            future_reparents = {
                item["bone_name"]
                for item in operations
                if item["operation"] == "UPDATE" and (item.get("clear_parent") or "parent" in item)
            }
            requested_deletions = {item["bone_name"] for item in operations if item["operation"] == "DELETE"}
            undealt = sorted(set(children) - future_reparents - requested_deletions)
            if undealt:
                raise ValueError(
                    f"Deleting bone '{name}' requires explicitly reparenting or deleting its children: "
                    f"{', '.join(undealt)}"
                )
            del final[name]
            deleted.append(name)
        else:
            raise ValueError(f"Unsupported bone operation: {kind}")
    return list(final.values()), rename_map, deleted


def _replace_bone_path(path, old_name, new_name):
    escaped_old = old_name.replace("\\", "\\\\").replace('"', '\\"')
    escaped_new = new_name.replace("\\", "\\\\").replace('"', '\\"')
    return path.replace(f'pose.bones["{escaped_old}"]', f'pose.bones["{escaped_new}"]')


def _resolve_renamed(name, rename_map):
    seen = set()
    while name in rename_map and name not in seen:
        seen.add(name)
        name = rename_map[name]
    return name


def _snapshot_constraint(owner, constraint):
    fields = {}
    for field in _CONSTRAINT_COMMON | _CONSTRAINT_FIELDS.get(constraint.type, set()):
        if hasattr(constraint, field):
            value = getattr(constraint, field)
            fields[field] = value.copy() if hasattr(value, "copy") else value
    return {
        "type": constraint.type,
        "name": constraint.name,
        "index": list(owner.constraints).index(constraint),
        "fields": fields,
    }


def _restore_constraint(owner, name, snapshot):
    existing = owner.constraints.get(name)
    if snapshot is None:
        if existing is not None:
            owner.constraints.remove(existing)
        return
    if existing is None or existing.type != snapshot["type"]:
        if existing is not None:
            owner.constraints.remove(existing)
        existing = owner.constraints.new(type=snapshot["type"])
        existing.name = snapshot["name"]
    for field, value in snapshot["fields"].items():
        setattr(existing, field, value)
    if snapshot["index"] is not None:
        owner.constraints.move(list(owner.constraints).index(existing), snapshot["index"])


def _copy_pose_constraint(source, destination, armature_obj, name_map):
    created = destination.constraints.new(type=source.type)
    created.name = source.name
    for field in _CONSTRAINT_COMMON | _CONSTRAINT_FIELDS.get(source.type, set()):
        if not hasattr(source, field) or not hasattr(created, field):
            continue
        value = getattr(source, field)
        if field in {"subtarget", "pole_subtarget"} and getattr(source, "target", None) == armature_obj:
            value = name_map.get(value, value)
        setattr(created, field, value)
    return created


def _constraint_dependency_cycle(armature_obj, owner_name, target_name, ignored_constraint=None):
    graph = defaultdict(set)
    for pose_bone in armature_obj.pose.bones:
        for constraint in pose_bone.constraints:
            if constraint is ignored_constraint:
                continue
            if getattr(constraint, "target", None) == armature_obj:
                subtarget = getattr(constraint, "subtarget", "")
                if subtarget and armature_obj.pose.bones.get(subtarget) is not None:
                    graph[pose_bone.name].add(subtarget)
    graph[owner_name].add(target_name)
    stack = [target_name]
    visited = set()
    while stack:
        name = stack.pop()
        if name == owner_name:
            return True
        if name in visited:
            continue
        visited.add(name)
        stack.extend(graph[name])
    return False


def _constraint_payload_fields(spec, target, pole_target, action, action_slot=None):
    constraint_type = spec["type"]
    fields = {key: spec[key] for key in _CONSTRAINT_COMMON if key in spec}
    if "target" in _CONSTRAINT_FIELDS[constraint_type]:
        fields["target"] = target
    if "subtarget" in _CONSTRAINT_FIELDS[constraint_type] and spec.get("subtarget") is not None:
        fields["subtarget"] = spec["subtarget"]
    if constraint_type == "IK":
        fields.update({key: spec[key] for key in _CONSTRAINT_FIELDS[constraint_type] if key in spec})
        fields["target"] = target
        if pole_target is not None:
            fields["pole_target"] = pole_target
            fields["pole_subtarget"] = spec.get("pole_subtarget", "")
    elif constraint_type == "ACTION":
        fields.update({key: spec[key] for key in _CONSTRAINT_FIELDS[constraint_type] if key in spec})
        fields["target"] = target
        fields["action"] = action
        if action_slot is not None:
            fields["action_slot"] = action_slot
    elif constraint_type.startswith("LIMIT_"):
        prefix = "use_limit" if constraint_type == "LIMIT_ROTATION" else "use"
        for axis in "xyz":
            enabled = bool(spec.get(f"use_{axis}", False))
            fields[f"{prefix}_min_{axis}" if prefix == "use" else f"use_limit_{axis}"] = enabled
            if prefix == "use":
                fields[f"use_max_{axis}"] = enabled
            fields[f"min_{axis}"] = spec.get(f"min_{axis}", 0.0)
            fields[f"max_{axis}"] = spec.get(f"max_{axis}", 0.0)
        fields["use_transform_limit"] = bool(spec.get("use_transform_limit", False))
    elif constraint_type == "TRANSFORM":
        fields.update(
            {
                key: spec[key]
                for key in (
                    "map_from",
                    "map_to",
                    "map_to_x_from",
                    "map_to_y_from",
                    "map_to_z_from",
                    "use_motion_extrapolate",
                )
                if key in spec
            }
        )
        suffix = {"LOCATION": "", "ROTATION": "_rot", "SCALE": "_scale"}
        from_suffix = suffix[spec.get("map_from", "LOCATION")]
        to_suffix = suffix[spec.get("map_to", "LOCATION")]
        for index, axis in enumerate("xyz"):
            fields[f"from_min_{axis}{from_suffix}"] = spec.get("from_min", (0, 0, 0))[index]
            fields[f"from_max_{axis}{from_suffix}"] = spec.get("from_max", (1, 1, 1))[index]
            fields[f"to_min_{axis}{to_suffix}"] = spec.get("to_min", (0, 0, 0))[index]
            fields[f"to_max_{axis}{to_suffix}"] = spec.get("to_max", (1, 1, 1))[index]
    else:
        fields.update(
            {
                key: spec[key]
                for key in _CONSTRAINT_FIELDS[constraint_type]
                if key in spec and key not in {"target", "subtarget"}
            }
        )
    return fields


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


class FoundationHandlersMixin:
    """Inspect and edit armature foundations, bindings, weights, and constraints."""

    def get_character_rig_info(
        self,
        armature_object_name,
        bone_limit=100,
        bone_offset=0,
        bone_names=None,
        dependency_limit=100,
        dependency_offset=0,
        include_custom_properties=True,
    ):
        armature_obj = _armature_object(armature_object_name)
        bpy.context.view_layer.update()
        _validate_limit_offset(bone_limit, bone_offset, 500, "bone")
        _validate_limit_offset(dependency_limit, dependency_offset, 500, "dependency")
        bones = _selected_bones(armature_obj, bone_names)
        start, end, truncated, next_offset = paginate(len(bones), bone_offset, bone_limit, 500)
        dependencies = _dependent_meshes(armature_obj)
        dep_start, dep_end, dep_truncated, dep_next = paginate(
            len(dependencies), dependency_offset, dependency_limit, 500
        )
        return {
            "armature_object": armature_obj.name,
            "armature_data": armature_obj.data.name,
            "transforms": _transform_info(armature_obj),
            "pose_position": armature_obj.data.pose_position,
            "display": {
                "display_type": armature_obj.data.display_type,
                "show_axes": bool(armature_obj.data.show_axes),
                "axes_position": float(armature_obj.data.axes_position),
                "show_names": bool(armature_obj.data.show_names),
                "relation_line_position": armature_obj.data.relation_line_position,
                "show_bone_custom_shapes": bool(armature_obj.data.show_bone_custom_shapes),
                "show_bone_colors": bool(armature_obj.data.show_bone_colors),
                "show_in_front": bool(armature_obj.show_in_front),
            },
            "data_users": sorted(obj.name for obj in bpy.data.objects if obj.data == armature_obj.data),
            "bone_collections": {
                "items": [
                    _bone_collection_info(collection) for collection in list(armature_obj.data.collections_all)[:500]
                ],
                "total": len(armature_obj.data.collections_all),
                "truncated": len(armature_obj.data.collections_all) > 500,
            },
            "bones": {
                "items": [_bone_info(bone, include_custom_properties) for bone in bones[start:end]],
                "total": len(bones),
                "offset": start,
                "limit": bone_limit,
                "truncated": truncated,
                "next_offset": next_offset,
                "coordinate_space": "ARMATURE_LOCAL_REST",
            },
            "pose_bones": [
                _pose_bone_info(armature_obj, armature_obj.pose.bones[bone.name], include_custom_properties)
                for bone in bones[start:end]
            ],
            "animation": {
                "object": _animation_info(armature_obj),
                "armature_data": _animation_info(armature_obj.data),
            },
            "custom_properties": _custom_properties(armature_obj) if include_custom_properties else None,
            "armature_data_custom_properties": _custom_properties(armature_obj.data)
            if include_custom_properties
            else None,
            "dependent_meshes": {
                "items": dependencies[dep_start:dep_end],
                "total": len(dependencies),
                "offset": dep_start,
                "limit": dependency_limit,
                "truncated": dep_truncated,
                "next_offset": dep_next,
            },
        }

    def get_skinning_info(
        self,
        armature_object_name,
        mesh_object_names=None,
        influence_limit=4,
        normalization_tolerance=1e-4,
        weight_epsilon=1e-6,
        membership_limit=500,
        membership_offset=0,
    ):
        armature_obj = _armature_object(armature_object_name)
        _validate_limit_offset(membership_limit, membership_offset, 2_000, "membership")
        if not 1 <= int(influence_limit) <= 64:
            raise ValueError("influence_limit must be in [1, 64]")
        tolerance = _finite(normalization_tolerance, "normalization_tolerance")
        epsilon = _finite(weight_epsilon, "weight_epsilon")
        if not 0 <= tolerance <= 1 or not 0 <= epsilon <= 1:
            raise ValueError("normalization_tolerance and weight_epsilon must be in [0, 1]")
        if mesh_object_names is None:
            meshes = [_mesh_object(record["object"]) for record in _dependent_meshes(armature_obj)]
        else:
            _unique_names(mesh_object_names, "mesh object names")
            meshes = [_mesh_object(name) for name in mesh_object_names]
        records = [_skinning_record(mesh, armature_obj, influence_limit, tolerance, epsilon) for mesh in meshes]
        memberships = [membership for record in records for membership in record.pop("memberships")]
        for record in records:
            for key in (
                "unweighted_vertices",
                "excessive_influences",
                "non_normalized_vertices",
                "zero_or_near_zero_assignments",
            ):
                values = record[key]
                record[f"{key}_total"] = len(values)
                record[f"{key}_truncated"] = len(values) > membership_limit
                record[key] = values[:membership_limit]
        start, end, truncated, next_offset = paginate(
            len(memberships), membership_offset, membership_limit, _MAX_MEMBERSHIPS
        )
        return {
            "armature_object": armature_obj.name,
            "weight_source": "BASE_MESH_VERTEX_GROUPS",
            "evaluated_deformation_included": False,
            "meshes": records,
            "memberships": {
                "items": memberships[start:end],
                "total": len(memberships),
                "offset": start,
                "limit": membership_limit,
                "truncated": truncated,
                "next_offset": next_offset,
            },
        }

    def create_armature(self, name, collection_name, bones=None, world_transform=None, display=None):
        _required_name(name, "name")
        collection = _ensure_object_collection(collection_name)
        if bpy.data.objects.get(name) is not None or bpy.data.armatures.get(name) is not None:
            raise ValueError(f"Armature object or datablock already exists: {name}")
        bones = list(bones or ())
        display = dict(display or {})
        world_transform = dict(world_transform or {})
        requested_collections = {collection_name for spec in bones for collection_name in spec.get("collections", ())}
        # Initial bone collection names belong to the new armature and are created by this request.
        collection_names = requested_collections
        _validate_bone_specs(bones, collection_names)
        location = _vector(world_transform.get("location", (0, 0, 0)), "world_transform.location")
        scale = _vector(world_transform.get("scale", (1, 1, 1)), "world_transform.scale")
        if any(abs(value) <= 1e-12 for value in scale):
            raise ValueError("world_transform.scale components must be non-zero")
        quaternion_values = world_transform.get("rotation_quaternion", (1, 0, 0, 0))
        if len(quaternion_values) != 4:
            raise ValueError("world_transform.rotation_quaternion must contain four values [w, x, y, z]")
        quaternion_values = tuple(_finite(value, "rotation_quaternion") for value in quaternion_values)
        if sum(value * value for value in quaternion_values) <= 1e-16:
            raise ValueError("world_transform.rotation_quaternion must be non-zero")
        quaternion = mathutils.Quaternion(quaternion_values)
        quaternion.normalize()

        armature_data = bpy.data.armatures.new(name)
        armature_obj = bpy.data.objects.new(name, armature_data)
        try:
            collection.objects.link(armature_obj)
            armature_obj.matrix_world = mathutils.Matrix.LocRotScale(location, quaternion, scale)
            armature_data.pose_position = display.get("pose_position", "POSE")
            armature_data.display_type = display.get("display_type", "OCTAHEDRAL")
            armature_data.show_axes = bool(display.get("show_axes", False))
            armature_data.show_names = bool(display.get("show_names", True))
            armature_data.axes_position = _finite(display.get("axes_position", 0.0), "axes_position")
            armature_data.relation_line_position = display.get("relation_line_position", "TAIL")
            armature_data.show_bone_custom_shapes = bool(display.get("show_bone_custom_shapes", True))
            armature_data.show_bone_colors = bool(display.get("show_bone_colors", True))
            armature_obj.show_in_front = bool(display.get("show_in_front", True))
            for bone_collection_name in sorted(requested_collections):
                armature_data.collections.new(bone_collection_name)
            if bones:
                with preserve_mode_and_selection():
                    _enter_armature_edit(armature_obj)
                    try:
                        created = {}
                        for spec in bones:
                            edit_bone = armature_data.edit_bones.new(spec["name"])
                            _set_edit_bone_fields(edit_bone, spec)
                            created[spec["name"]] = edit_bone
                        for spec in bones:
                            edit_bone = created[spec["name"]]
                            parent_name = spec.get("parent")
                            if parent_name is not None:
                                edit_bone.parent = created[parent_name]
                                edit_bone.use_connect = bool(spec.get("use_connect", False))
                            for bone_collection_name in spec.get("collections", ()):
                                armature_data.collections_all[bone_collection_name].assign(edit_bone)
                    finally:
                        _exit_object_mode()
        except Exception:
            if bpy.data.objects.get(armature_obj.name) is armature_obj:
                bpy.data.objects.remove(armature_obj, do_unlink=True)
            if bpy.data.armatures.get(armature_data.name) is armature_data:
                bpy.data.armatures.remove(armature_data, do_unlink=True)
            raise
        return {
            "armature_object": armature_obj.name,
            "armature_data": armature_data.name,
            "collection": collection.name,
            "bones": [bone.name for bone in armature_data.bones],
            "bone_collections": [item.name for item in armature_data.collections_all],
            "transforms": _transform_info(armature_obj),
            "changed_objects": [armature_obj.name],
            "changed_resources": [armature_data.name],
        }

    def patch_armature_bones(self, armature_object_name, operations, confirm_animated_rest_changes=False):
        armature_obj = _armature_object(armature_object_name)
        operations = list(operations or ())
        if not operations:
            raise ValueError("At least one bone operation is required")
        if len(operations) > 1_000:
            raise ValueError("At most 1000 bone operations are allowed")
        if _has_animation(armature_obj) and not confirm_animated_rest_changes:
            raise ValueError("confirm_animated_rest_changes=True is required because this rig has animation")
        existing_specs = _edit_bone_specs(armature_obj)
        final_specs, rename_map, deleted = _apply_patch_to_specs(existing_specs, operations)
        _validate_bone_specs(final_specs, {collection.name for collection in armature_obj.data.collections_all})
        final_names = {spec["name"] for spec in final_specs}
        for operation in operations:
            alignment_name = operation.get("align_orientation_bone")
            if alignment_name is not None and _resolve_renamed(alignment_name, rename_map) not in final_names:
                raise ValueError(f"Alignment bone not found in final hierarchy: {alignment_name}")
        dependencies = {}
        for operation in operations:
            if operation["operation"] not in {"RENAME", "DELETE"}:
                continue
            name = operation["bone_name"]
            found = _bone_dependencies(armature_obj, name)
            dependencies[name] = found
            policy = operation["reference_policy"]
            if found and policy == "ERROR":
                raise ValueError(f"Bone '{name}' has references; use an explicit update/removal policy: {found[:10]}")
        # Bone names are evaluated in request order, which also makes chained renames deterministic.
        changed_users = []
        with _working_armature_with_references(armature_obj, operations) as (
            armature_data,
            users,
            affected_dependencies,
        ):
            changed_users = [obj.name for obj in users]
            with preserve_mode_and_selection():
                _enter_armature_edit(armature_obj)
                try:
                    for operation in operations:
                        kind = operation["operation"]
                        if kind == "CREATE":
                            edit_bone = armature_data.edit_bones.new(operation["name"])
                            _set_edit_bone_fields(edit_bone, operation)
                        elif kind == "RENAME":
                            armature_data.edit_bones[operation["bone_name"]].name = operation["new_name"]
                        elif kind == "UPDATE":
                            edit_bone = armature_data.edit_bones[operation["bone_name"]]
                            for field in (
                                "head",
                                "tail",
                                "roll",
                                "use_connect",
                                "use_deform",
                                "inherit_scale",
                                "envelope_distance",
                                "envelope_weight",
                                "head_radius",
                                "tail_radius",
                            ):
                                if field not in operation:
                                    continue
                                value = operation[field]
                                if field in {"head", "tail"}:
                                    value = _vector(value, f"{edit_bone.name}.{field}")
                                setattr(edit_bone, field, value)
                        elif kind == "DELETE":
                            armature_data.edit_bones.remove(armature_data.edit_bones[operation["bone_name"]])
                    for spec in final_specs:
                        edit_bone = armature_data.edit_bones[spec["name"]]
                        parent_name = spec.get("parent")
                        edit_bone.parent = armature_data.edit_bones.get(parent_name) if parent_name else None
                        edit_bone.use_connect = bool(spec.get("use_connect", False) and parent_name)
                    for operation in operations:
                        if operation["operation"] != "UPDATE":
                            continue
                        edit_bone = armature_data.edit_bones[_resolve_renamed(operation["bone_name"], rename_map)]
                        if "align_roll_vector" in operation:
                            vector = _vector(operation["align_roll_vector"], "align_roll_vector")
                            if vector.length <= 1e-8:
                                raise ValueError("align_roll_vector must be non-zero")
                            edit_bone.align_roll(vector)
                        if "align_orientation_bone" in operation:
                            alignment_name = _resolve_renamed(operation["align_orientation_bone"], rename_map)
                            edit_bone.align_orientation(armature_data.edit_bones[alignment_name])
                    for operation in operations:
                        if operation["operation"] != "CREATE":
                            continue
                        edit_bone = armature_data.edit_bones[_resolve_renamed(operation["name"], rename_map)]
                        for collection_name in operation.get("collections", ()):
                            armature_data.collections_all[collection_name].assign(edit_bone)
                finally:
                    _exit_object_mode()
        changed_objects = set(changed_users)
        changed_objects.update(
            record["object"] for record in affected_dependencies if isinstance(record.get("object"), str)
        )
        return {
            "armature_object": armature_obj.name,
            "armature_data": armature_obj.data.name,
            "operations_applied": len(operations),
            "bone_names": [bone.name for bone in armature_obj.data.bones],
            "renamed_bones": rename_map,
            "deleted_bones": deleted,
            "dependencies_before": dependencies,
            "affected_dependencies": affected_dependencies,
            "data_users_changed": changed_users,
            "changed_objects": sorted(changed_objects),
            "changed_resources": [armature_obj.data.name],
            "warnings": ["Rest-pose edits can invalidate authored deformation and animation."]
            if _has_animation(armature_obj)
            else [],
        }

    def mirror_armature_bones(
        self,
        armature_object_name,
        bone_names,
        axis="X",
        source_token=".L",
        target_token=".R",
        mirror_constraints=False,
    ):
        armature_obj = _armature_object(armature_object_name)
        names = list(bone_names or ())
        if not names:
            raise ValueError("At least one source bone is required")
        _unique_names(names, "source bone names")
        if axis not in {"X", "Y", "Z"}:
            raise ValueError("axis must be X, Y, or Z")
        if not source_token or source_token == target_token:
            raise ValueError("source_token must be non-empty and differ from target_token")
        source_bones = {}
        name_map = {}
        for name in names:
            bone = armature_obj.data.bones.get(name)
            if bone is None:
                raise ValueError(f"Bone not found: {name}")
            if source_token not in name:
                raise ValueError(f"Bone '{name}' does not contain source token '{source_token}'")
            target_name = name.replace(source_token, target_token)
            if target_name == name or armature_obj.data.bones.get(target_name) is not None:
                raise ValueError(f"Mirrored bone name collides or is unchanged: {target_name}")
            source_bones[name] = bone
            name_map[name] = target_name
        _unique_names(list(name_map.values()), "mirrored bone names")
        ambiguous = []
        axis_index = {"X": 0, "Y": 1, "Z": 2}[axis]
        for name, bone in source_bones.items():
            if abs(bone.head_local[axis_index]) <= 1e-7 and abs(bone.tail_local[axis_index]) <= 1e-7:
                ambiguous.append(name)
        changed_users = []
        with _working_armature_data(armature_obj) as (armature_data, users):
            changed_users = [obj.name for obj in users]
            with preserve_mode_and_selection():
                _enter_armature_edit(armature_obj)
                try:
                    created = {}
                    # Parent sources are created first regardless of the caller's order.
                    pending = set(names)
                    while pending:
                        progressed = False
                        for name in list(pending):
                            source = armature_data.edit_bones[name]
                            if source.parent and source.parent.name in pending:
                                continue
                            target = armature_data.edit_bones.new(name_map[name])
                            target.head = source.head.copy()
                            target.tail = source.tail.copy()
                            target.head[axis_index] *= -1
                            target.tail[axis_index] *= -1
                            reflected_z = source.z_axis.copy()
                            reflected_z[axis_index] *= -1
                            target.align_roll(reflected_z)
                            for field in _EDIT_BONE_COPY_FIELDS:
                                if field not in {"roll", "use_connect"} and hasattr(source, field):
                                    setattr(target, field, getattr(source, field))
                            if source.parent is not None:
                                target.parent = created.get(
                                    source.parent.name,
                                    armature_data.edit_bones.get(name_map.get(source.parent.name, "")),
                                )
                                if target.parent is None:
                                    target.parent = source.parent
                                target.use_connect = bool(source.use_connect and source.parent.name in name_map)
                            for collection in source.collections:
                                armature_data.collections_all[collection.name].assign(target)
                            created[name] = target
                            pending.remove(name)
                            progressed = True
                        if not progressed:
                            raise RuntimeError("Could not resolve mirrored bone parent order")
                finally:
                    _exit_object_mode()
            mirrored_constraints = []
            if mirror_constraints:
                for source_name, target_name in name_map.items():
                    source_pose = armature_obj.pose.bones[source_name]
                    target_pose = armature_obj.pose.bones[target_name]
                    for constraint in source_pose.constraints:
                        copied = _copy_pose_constraint(constraint, target_pose, armature_obj, name_map)
                        mirrored_constraints.append({"bone": target_name, "constraint": copied.name})
        return {
            "armature_object": armature_obj.name,
            "axis": axis,
            "source_to_target": name_map,
            "mirrored_constraints": mirrored_constraints,
            "centerline_ambiguities": ambiguous,
            "data_users_changed": changed_users,
            "changed_objects": changed_users,
            "changed_resources": [armature_obj.data.name],
            "warnings": [f"Source bones on the {axis} center plane produce overlapping mirrored geometry: {ambiguous}"]
            if ambiguous
            else [],
        }

    def manage_bone_collections(self, armature_object_name, operations):
        armature_obj = _armature_object(armature_object_name)
        operations = list(operations or ())
        if not operations:
            raise ValueError("At least one collection operation is required")
        # Simulate names and parents before the first mutation.
        names = {collection.name for collection in armature_obj.data.collections_all}
        parents = {
            collection.name: getattr(collection.parent, "name", None)
            for collection in armature_obj.data.collections_all
        }
        for operation in operations:
            kind = operation["operation"]
            name = operation["name"]
            if kind == "CREATE":
                if name in names and operation.get("existing_policy", "ERROR") == "ERROR":
                    raise ValueError(f"Bone collection already exists: {name}")
                names.add(name)
                parents.setdefault(name, operation.get("parent"))
            elif name not in names:
                raise ValueError(f"Bone collection not found: {name}")
            if kind == "RENAME":
                new_name = operation["new_name"]
                if new_name in names:
                    raise ValueError(f"Bone collection name collision: {new_name}")
                names.remove(name)
                names.add(new_name)
                parents[new_name] = parents.pop(name)
                parents = {key: new_name if value == name else value for key, value in parents.items()}
            elif kind == "CONFIGURE":
                if operation.get("clear_parent"):
                    parents[name] = None
                elif "parent" in operation:
                    parents[name] = operation["parent"]
            elif kind == "REMOVE":
                if not operation.get("confirm_destructive"):
                    raise ValueError(f"confirm_destructive=True is required to remove collection '{name}'")
                removed_parent = parents[name]
                names.remove(name)
                parents.pop(name, None)
                parents = {key: removed_parent if value == name else value for key, value in parents.items()}
            if kind in {"ASSIGN", "UNASSIGN"}:
                if operation.get("replace_memberships") and not operation.get("confirm_destructive"):
                    raise ValueError("confirm_destructive=True is required when replace_memberships=True")
                if kind == "UNASSIGN" and not operation.get("confirm_destructive"):
                    raise ValueError("confirm_destructive=True is required to unassign bone memberships")
                missing_bones = [bone for bone in operation["bone_names"] if armature_obj.data.bones.get(bone) is None]
                if missing_bones:
                    raise ValueError(f"Unknown bones for collection '{name}': {missing_bones}")
        for name, parent in parents.items():
            if parent is not None and parent not in names:
                raise ValueError(f"Collection '{name}' references missing parent '{parent}'")
        cycles = _hierarchy_cycles(parents)
        if cycles:
            raise ValueError(f"Bone collection hierarchy contains a cycle: {' -> '.join(cycles[0])}")
        displaced = []
        changed_users = []
        with _working_armature_data(armature_obj) as (armature_data, users):
            changed_users = [obj.name for obj in users]
            for operation in operations:
                if operation["operation"] == "CREATE" and armature_data.collections_all.get(operation["name"]) is None:
                    armature_data.collections.new(operation["name"])
            for operation in operations:
                kind = operation["operation"]
                name = operation["name"]
                collection = armature_data.collections_all.get(name)
                if kind == "CREATE":
                    parent_name = operation.get("parent")
                    collection.parent = armature_data.collections_all.get(parent_name) if parent_name else None
                    collection.is_visible = bool(operation.get("is_visible", True))
                    collection.is_solo = bool(operation.get("is_solo", False))
                elif kind == "RENAME":
                    collection.name = operation["new_name"]
                elif kind == "CONFIGURE":
                    if operation.get("clear_parent"):
                        collection.parent = None
                    elif "parent" in operation:
                        collection.parent = armature_data.collections_all[operation["parent"]]
                    for field in ("is_visible", "is_solo"):
                        if field in operation:
                            setattr(collection, field, operation[field])
                    if "position" in operation:
                        siblings = [
                            item
                            for item in armature_data.collections_all
                            if item.parent == collection.parent and item != collection
                        ]
                        destination_position = min(operation["position"], len(siblings))
                        if siblings:
                            destination = (
                                siblings[destination_position].index
                                if destination_position < len(siblings)
                                else siblings[-1].index
                            )
                            armature_data.collections.move(collection.index, destination)
                elif kind in {"ASSIGN", "UNASSIGN"}:
                    for bone_name in operation["bone_names"]:
                        bone = armature_data.bones[bone_name]
                        if kind == "ASSIGN" and operation.get("replace_memberships"):
                            for prior in list(bone.collections):
                                prior.unassign(bone)
                                displaced.append({"bone": bone_name, "collection": prior.name})
                        (collection.assign if kind == "ASSIGN" else collection.unassign)(bone)
                elif kind == "REMOVE":
                    displaced.extend({"bone": bone.name, "collection": name} for bone in collection.bones)
                    armature_data.collections.remove(collection)
        return {
            "armature_object": armature_obj.name,
            "collections": [_bone_collection_info(item) for item in armature_obj.data.collections_all],
            "displaced_memberships": displaced,
            "data_users_changed": changed_users,
            "changed_objects": changed_users,
            "changed_resources": [armature_obj.data.name],
        }

    def configure_armature_bones(self, armature_object_name, bone_patches=None, pose_bone_patches=None):
        armature_obj = _armature_object(armature_object_name)
        bone_patches = list(bone_patches or ())
        pose_bone_patches = list(pose_bone_patches or ())
        if not bone_patches and not pose_bone_patches:
            raise ValueError("At least one bone or pose-bone patch is required")
        _unique_names([item["bone_name"] for item in bone_patches], "bone patch targets")
        _unique_names([item["bone_name"] for item in pose_bone_patches], "pose-bone patch targets")
        for patch in [*bone_patches, *pose_bone_patches]:
            if armature_obj.data.bones.get(patch["bone_name"]) is None:
                raise ValueError(f"Bone not found: {patch['bone_name']}")
        for patch in pose_bone_patches:
            for axis in "xyz":
                minimum = patch.get(f"ik_min_{axis}")
                maximum = patch.get(f"ik_max_{axis}")
                if minimum is not None and maximum is not None and minimum > maximum:
                    raise ValueError(f"ik_min_{axis} must not exceed ik_max_{axis} on '{patch['bone_name']}'")
            for field in ("ik_stiffness_x", "ik_stiffness_y", "ik_stiffness_z"):
                if field in patch and not 0 <= _finite(patch[field], field) <= 0.99:
                    raise ValueError(f"{field} must be in [0, 0.99]")
        old_values = []
        try:
            for patch in bone_patches:
                bone = armature_obj.data.bones[patch["bone_name"]]
                for field in _BONE_DATA_FIELDS:
                    if field in patch:
                        old_values.append((bone, field, getattr(bone, field)))
                        setattr(bone, field, patch[field])
            for patch in pose_bone_patches:
                pose_bone = armature_obj.pose.bones[patch["bone_name"]]
                for field in _POSE_FIELDS:
                    if field in patch:
                        old = getattr(pose_bone, field)
                        old_values.append((pose_bone, field, old.copy() if hasattr(old, "copy") else old))
                        setattr(pose_bone, field, patch[field])
                if "custom_properties" in patch:
                    for key, value in patch["custom_properties"].items():
                        prior = pose_bone.get(key, None)
                        existed = key in pose_bone
                        old_values.append((pose_bone, ("custom_property", key, existed), prior))
                        pose_bone[key] = value
        except Exception:
            for owner, field, value in reversed(old_values):
                if isinstance(field, tuple):
                    _, key, existed = field
                    if existed:
                        owner[key] = value
                    elif key in owner:
                        del owner[key]
                else:
                    setattr(owner, field, value)
            raise
        changes = []
        for owner, field, old in old_values:
            if isinstance(field, tuple):
                key = field[1]
                new = owner[key]
                field_name = f"custom_properties.{key}"
            else:
                new = getattr(owner, field)
                field_name = field
            changes.append({"bone": owner.name, "field": field_name, "old": _plain(old), "new": _plain(new)})
        return {
            "armature_object": armature_obj.name,
            "changes": changes,
            "changed_objects": [armature_obj.name],
            "changed_resources": [armature_obj.data.name] if bone_patches else [],
        }

    def bind_mesh_to_armature(
        self,
        armature_object_name,
        mesh_object_names,
        method="EMPTY_GROUPS",
        modifier_name="Armature",
        existing_modifier_policy="REUSE",
        parent_meshes=False,
        preserve_volume=False,
        modifier_index=None,
        replacement_policy="PRESERVE",
        confirm_replace_weights=False,
    ):
        armature_obj = _armature_object(armature_object_name)
        names = list(mesh_object_names or ())
        if not names:
            raise ValueError("At least one mesh object is required")
        _unique_names(names, "mesh object names")
        meshes = [_mesh_object(name) for name in names]
        if method not in {"EMPTY_GROUPS", "AUTOMATIC", "ENVELOPES", "EXISTING_WEIGHTS"}:
            raise ValueError(f"Unsupported binding method: {method}")
        if replacement_policy == "REPLACE" and not confirm_replace_weights:
            raise ValueError("confirm_replace_weights=True is required when replacement_policy='REPLACE'")
        deform_names = sorted(_deform_names(armature_obj))
        for mesh in meshes:
            if parent_meshes:
                ancestor = armature_obj.parent
                while ancestor is not None:
                    if ancestor == mesh:
                        raise ValueError(f"Parenting '{mesh.name}' to '{armature_obj.name}' would create a cycle")
                    ancestor = ancestor.parent
            conflicts = [
                modifier.name
                for modifier in mesh.modifiers
                if modifier.name == modifier_name and modifier.type != "ARMATURE"
            ]
            if conflicts:
                raise ValueError(f"Modifier '{modifier_name}' on '{mesh.name}' is not an Armature modifier")
            if mesh.modifiers.get(modifier_name) is not None and existing_modifier_policy == "ERROR":
                raise ValueError(f"Modifier '{modifier_name}' already exists on '{mesh.name}'")
            existing_deform_groups = [name for name in deform_names if mesh.vertex_groups.get(name) is not None]
            if method == "EXISTING_WEIGHTS" and not existing_deform_groups:
                raise ValueError(f"'{mesh.name}' has no vertex groups matching deform bones on '{armature_obj.name}'")
            if replacement_policy == "REPLACE":
                locked = [name for name in existing_deform_groups if mesh.vertex_groups[name].lock_weight]
                if locked:
                    raise ValueError(f"Cannot replace locked deform groups on '{mesh.name}': {locked}")
            if method in {"AUTOMATIC", "ENVELOPES"} and replacement_policy == "PRESERVE":
                existing = sorted(existing_deform_groups)
                if existing:
                    raise ValueError(
                        f"{method} would replace existing deform weights on '{mesh.name}': {existing[:20]}; "
                        "use replacement_policy='REPLACE' with confirmation"
                    )
        snapshots = {
            mesh.name: {
                "groups": _snapshot_groups(mesh),
                "modifiers": _snapshot_modifiers(mesh),
                "modifier_pointers": {modifier.as_pointer() for modifier in mesh.modifiers},
                "parent": mesh.parent,
                "parent_type": mesh.parent_type,
                "parent_inverse": mesh.matrix_parent_inverse.copy(),
                "world": mesh.matrix_world.copy(),
            }
            for mesh in meshes
        }
        try:
            if replacement_policy == "REPLACE":
                for mesh in meshes:
                    for name in deform_names:
                        group = mesh.vertex_groups.get(name)
                        if group is not None:
                            mesh.vertex_groups.remove(group)
            if method in {"AUTOMATIC", "ENVELOPES"}:
                with preserve_mode_and_selection():
                    bpy.ops.object.select_all(action="DESELECT")
                    for mesh in meshes:
                        mesh.select_set(True)
                    armature_obj.select_set(True)
                    bpy.context.view_layer.objects.active = armature_obj
                    operator_type = "ARMATURE_AUTO" if method == "AUTOMATIC" else "ARMATURE_ENVELOPE"
                    result = bpy.ops.object.parent_set(type=operator_type, keep_transform=True)
                    if not isinstance(result, (set, frozenset)) or "FINISHED" not in result:
                        raise RuntimeError(f"Automatic binding operator did not finish: {result}")
            else:
                for mesh in meshes:
                    if method == "EMPTY_GROUPS":
                        for name in deform_names:
                            if mesh.vertex_groups.get(name) is None:
                                mesh.vertex_groups.new(name=name)
            for mesh in meshes:
                modifier = mesh.modifiers.get(modifier_name)
                if modifier is not None and modifier.type != "ARMATURE":
                    raise ValueError(f"Modifier '{modifier_name}' on '{mesh.name}' is not an Armature modifier")
                new_matching = [
                    item
                    for item in mesh.modifiers
                    if item.type == "ARMATURE"
                    and item.object == armature_obj
                    and item.as_pointer() not in snapshots[mesh.name]["modifier_pointers"]
                ]
                if modifier is None and method in {"AUTOMATIC", "ENVELOPES"} and new_matching:
                    modifier = new_matching[-1]
                if modifier is None:
                    modifier = mesh.modifiers.new(name=modifier_name, type="ARMATURE")
                for duplicate in new_matching:
                    if duplicate != modifier:
                        mesh.modifiers.remove(duplicate)
                modifier.name = modifier_name
                modifier.object = armature_obj
                modifier.use_vertex_groups = method != "ENVELOPES"
                modifier.use_bone_envelopes = method == "ENVELOPES"
                modifier.use_deform_preserve_volume = bool(preserve_volume)
                if modifier_index is not None:
                    mesh.modifiers.move(
                        list(mesh.modifiers).index(modifier), min(int(modifier_index), len(mesh.modifiers) - 1)
                    )
                world = mesh.matrix_world.copy()
                if parent_meshes:
                    mesh.parent = armature_obj
                    mesh.parent_type = "OBJECT"
                    mesh.matrix_parent_inverse = armature_obj.matrix_world.inverted()
                    mesh.matrix_world = world
                elif method in {"AUTOMATIC", "ENVELOPES"}:
                    # parent_set is the documented weighting operation.
                    # Undo only its parenting side effect when omitted.
                    mesh.parent = snapshots[mesh.name]["parent"]
                    mesh.parent_type = snapshots[mesh.name]["parent_type"]
                    mesh.matrix_parent_inverse = snapshots[mesh.name]["parent_inverse"]
                    mesh.matrix_world = world
        except Exception:
            for mesh in meshes:
                snapshot = snapshots[mesh.name]
                _restore_groups(mesh, snapshot["groups"])
                _restore_armature_modifiers(mesh, snapshot["modifiers"])
                mesh.parent = snapshot["parent"]
                mesh.parent_type = snapshot["parent_type"]
                mesh.matrix_parent_inverse = snapshot["parent_inverse"]
                mesh.matrix_world = snapshot["world"]
            raise
        bindings = []
        for mesh in meshes:
            modifier = mesh.modifiers.get(modifier_name)
            bindings.append(
                {
                    "mesh": mesh.name,
                    "method": method,
                    "parent": getattr(mesh.parent, "name", None),
                    "modifier": _armature_modifier_info(modifier),
                    "deform_groups": sorted(name for name in deform_names if mesh.vertex_groups.get(name) is not None),
                }
            )
        return {
            "armature_object": armature_obj.name,
            "bindings": bindings,
            "changed_objects": [mesh.name for mesh in meshes],
        }

    def set_skin_weights(self, assignments=None, normalized_vertices=None):
        assignments = list(assignments or ())
        normalized_vertices = list(normalized_vertices or ())
        if not assignments and not normalized_vertices:
            raise ValueError("At least one assignment or normalized vertex payload is required")
        meshes = {}
        for item in [*assignments, *normalized_vertices]:
            mesh = meshes.setdefault(item["mesh_object_name"], _mesh_object(item["mesh_object_name"]))
            sync_from_editmode(mesh)
        # Complete preflight, including locks and indices, before the first write.
        for item in assignments:
            mesh = meshes[item["mesh_object_name"]]
            group = mesh.vertex_groups.get(item["group_name"])
            if group is None and not item.get("create_missing_group"):
                raise ValueError(f"Vertex group '{item['group_name']}' not found on '{mesh.name}'")
            if group is not None and group.lock_weight:
                raise ValueError(f"Vertex group '{group.name}' on '{mesh.name}' is locked")
            invalid = [index for index in item["vertex_indices"] if not 0 <= index < len(mesh.data.vertices)]
            if invalid:
                raise ValueError(f"Vertex indices out of range on '{mesh.name}': {invalid[:20]}")
            weight = _finite(item["weight"], "weight")
            if not 0 <= weight <= 1:
                raise ValueError("weight must be in [0, 1]")
        for item in normalized_vertices:
            mesh = meshes[item["mesh_object_name"]]
            index = item["vertex_index"]
            if not 0 <= index < len(mesh.data.vertices):
                raise ValueError(f"Vertex index {index} out of range on '{mesh.name}'")
            if abs(sum(_finite(weight, "weight") for weight in item["weights"].values()) - 1.0) > 1e-6:
                raise ValueError(f"Normalized weights for '{mesh.name}' vertex {index} must sum to 1")
            for name in item["weights"]:
                group = mesh.vertex_groups.get(name)
                if group is None and not item.get("create_missing_groups"):
                    raise ValueError(f"Vertex group '{name}' not found on '{mesh.name}'")
                if group is not None and group.lock_weight:
                    current = _vertex_weight_map(mesh, mesh.data.vertices[index]).get(name, 0.0)
                    if abs(current - item["weights"][name]) > 1e-8:
                        raise ValueError(f"Normalized payload would alter locked group '{name}' on '{mesh.name}'")
            current = _vertex_weight_map(mesh, mesh.data.vertices[index])
            omitted_locked = [
                group.name
                for group in mesh.vertex_groups
                if group.lock_weight and current.get(group.name, 0.0) > 0 and group.name not in item["weights"]
            ]
            if omitted_locked:
                raise ValueError(f"Normalized payload omits locked assignments on '{mesh.name}': {omitted_locked}")
        snapshots = {name: _snapshot_groups(mesh) for name, mesh in meshes.items()}
        changes = []
        try:
            for item in assignments:
                mesh = meshes[item["mesh_object_name"]]
                group = mesh.vertex_groups.get(item["group_name"])
                if group is None:
                    group = mesh.vertex_groups.new(name=item["group_name"])
                indices = item["vertex_indices"]
                weight = float(item["weight"])
                mode = item.get("mode", "REPLACE")
                previous = [
                    {
                        "vertex": index,
                        "weight": _vertex_weight_map(mesh, mesh.data.vertices[index]).get(group.name),
                    }
                    for index in indices[:100]
                ]
                if mode == "REPLACE" and weight == 0:
                    group.remove(indices)
                else:
                    group.add(indices, weight, mode)
                changes.append(
                    {
                        "mesh": mesh.name,
                        "group": group.name,
                        "vertices": len(indices),
                        "mode": mode,
                        "weight": weight,
                        "previous": previous,
                        "previous_truncated": len(indices) > len(previous),
                    }
                )
            for item in normalized_vertices:
                mesh = meshes[item["mesh_object_name"]]
                index = item["vertex_index"]
                desired = item["weights"]
                previous = _vertex_weight_map(mesh, mesh.data.vertices[index])
                for name in desired:
                    if mesh.vertex_groups.get(name) is None:
                        mesh.vertex_groups.new(name=name)
                for group in mesh.vertex_groups:
                    if not group.lock_weight:
                        group.remove([index])
                for name, weight in desired.items():
                    group = mesh.vertex_groups[name]
                    if not group.lock_weight and weight > 0:
                        group.add([index], weight, "REPLACE")
                changes.append(
                    {
                        "mesh": mesh.name,
                        "vertex": index,
                        "previous_weights": previous,
                        "normalized_weights": desired,
                    }
                )
        except Exception:
            for name, mesh in meshes.items():
                _restore_groups(mesh, snapshots[name])
            raise
        changed_meshes = []
        for name, mesh in meshes.items():
            before_groups, before_weights = snapshots[name]
            current_groups = [
                {"name": group.name, "lock_weight": bool(group.lock_weight)} for group in mesh.vertex_groups
            ]
            current_weights = [_vertex_weight_map(mesh, vertex) for vertex in mesh.data.vertices]
            if before_groups != current_groups or before_weights != current_weights:
                changed_meshes.append(name)
        return {
            "changes": changes,
            "changed_objects": sorted(changed_meshes),
            "warnings": ["Refresh topology indices before reusing this payload after any topology-changing operation."],
        }

    def clean_skin_weights(
        self,
        mesh_object_name,
        armature_object_name=None,
        vertex_indices=None,
        threshold=1e-4,
        influence_limit=4,
        normalize="DEFORM",
        protected_group_names=None,
        remove_orphan_groups=False,
        confirm_remove_orphan_groups=False,
    ):
        mesh = _mesh_object(mesh_object_name)
        sync_from_editmode(mesh)
        armature_obj = _armature_object(armature_object_name) if armature_object_name else None
        threshold = _finite(threshold, "threshold")
        if not 0 <= threshold <= 1:
            raise ValueError("threshold must be in [0, 1]")
        if influence_limit is not None and not 1 <= int(influence_limit) <= 64:
            raise ValueError("influence_limit must be in [1, 64]")
        if normalize not in {"NONE", "ALL", "DEFORM"}:
            raise ValueError("normalize must be NONE, ALL, or DEFORM")
        if normalize == "DEFORM" and armature_obj is None:
            raise ValueError("armature_object_name is required when normalize='DEFORM'")
        if remove_orphan_groups and not confirm_remove_orphan_groups:
            raise ValueError("confirm_remove_orphan_groups=True is required to remove orphan groups")
        protected_names = set(protected_group_names or ())
        unknown_protected = protected_names - {group.name for group in mesh.vertex_groups}
        if unknown_protected:
            raise ValueError(f"Protected groups not found on '{mesh.name}': {sorted(unknown_protected)}")
        locked_names = {group.name for group in mesh.vertex_groups if group.lock_weight}
        protected_names |= locked_names
        deform_names = _deform_names(armature_obj) if armature_obj else set()
        included_names = (
            {group.name for group in mesh.vertex_groups}
            if normalize == "ALL"
            else deform_names
            if normalize == "DEFORM"
            else {group.name for group in mesh.vertex_groups}
        )
        if vertex_indices is None:
            indices = list(range(len(mesh.data.vertices)))
        else:
            indices = list(vertex_indices)
            _unique_names(indices, "vertex indices")
            invalid = [index for index in indices if not 0 <= index < len(mesh.data.vertices)]
            if invalid:
                raise ValueError(f"Vertex indices out of range on '{mesh.name}': {invalid[:20]}")
        before_maps = [_vertex_weight_map(mesh, vertex) for vertex in mesh.data.vertices]
        proposals = {}
        removed_assignments = []
        for index in indices:
            before = before_maps[index]
            proposed = dict(before)
            candidates = [name for name in included_names if name not in protected_names]
            for name in candidates:
                weight = proposed.get(name, 0.0)
                if weight <= threshold:
                    if name in proposed:
                        removed_assignments.append(
                            {"vertex": index, "group": name, "weight": weight, "reason": "THRESHOLD"}
                        )
                    proposed.pop(name, None)
            if influence_limit is not None:
                protected_influences = [
                    name for name in protected_names & included_names if proposed.get(name, 0.0) > 0
                ]
                if len(protected_influences) > influence_limit:
                    raise ValueError(
                        f"Vertex {index} has {len(protected_influences)} protected influences, "
                        f"above limit {influence_limit}"
                    )
                editable = sorted(
                    ((name, proposed.get(name, 0.0)) for name in candidates if proposed.get(name, 0.0) > 0),
                    key=lambda item: (-item[1], item[0]),
                )
                keep = {name for name, _weight in editable[: influence_limit - len(protected_influences)]}
                for name, weight in editable:
                    if name not in keep:
                        proposed.pop(name, None)
                        removed_assignments.append(
                            {"vertex": index, "group": name, "weight": weight, "reason": "INFLUENCE_LIMIT"}
                        )
            if normalize != "NONE":
                locked_sum = sum(proposed.get(name, 0.0) for name in protected_names & included_names)
                if locked_sum > 1.0 + 1e-8:
                    raise ValueError(
                        f"Protected weights sum to {locked_sum:.6g} on vertex {index}, so normalization is impossible"
                    )
                editable_names = [name for name in candidates if proposed.get(name, 0.0) > 0]
                editable_sum = sum(proposed[name] for name in editable_names)
                target = max(0.0, 1.0 - locked_sum)
                if editable_names and editable_sum > 0:
                    factor = target / editable_sum
                    for name in editable_names:
                        proposed[name] *= factor
            proposals[index] = proposed
        snapshot = _snapshot_groups(mesh)
        orphan_names = []
        if remove_orphan_groups:
            if armature_obj is None:
                raise ValueError("armature_object_name is required to identify orphan groups")
            orphan_names = sorted(
                group.name
                for group in mesh.vertex_groups
                if group.name not in deform_names and group.name not in protected_names
            )
        before_histogram = _influence_histogram([before_maps[index] for index in indices], included_names)
        try:
            for index, proposed in proposals.items():
                before = before_maps[index]
                touched = (set(before) | set(proposed)) & included_names - protected_names
                for name in touched:
                    group = mesh.vertex_groups.get(name)
                    if group is not None:
                        group.remove([index])
                for name in touched:
                    weight = proposed.get(name, 0.0)
                    group = mesh.vertex_groups.get(name)
                    if group is not None and weight > 0:
                        group.add([index], weight, "REPLACE")
            for name in orphan_names:
                group = mesh.vertex_groups.get(name)
                if group is not None:
                    mesh.vertex_groups.remove(group)
        except Exception:
            _restore_groups(mesh, snapshot)
            raise
        after_maps = [_vertex_weight_map(mesh, vertex) for vertex in mesh.data.vertices]
        changed_vertices = [index for index in indices if before_maps[index] != after_maps[index]]
        residual_unweighted = [
            index
            for index in indices
            if not any(name in included_names and weight > 0 for name, weight in after_maps[index].items())
        ]
        detail_limit = 2_000
        return {
            "mesh_object": mesh.name,
            "changed_vertices": changed_vertices[:detail_limit],
            "changed_vertex_count": len(changed_vertices),
            "changed_vertices_truncated": len(changed_vertices) > detail_limit,
            "removed_assignments": removed_assignments[:detail_limit],
            "removed_assignment_count": len(removed_assignments),
            "removed_assignments_truncated": len(removed_assignments) > detail_limit,
            "removed_orphan_groups": orphan_names,
            "before_influence_histogram": before_histogram,
            "after_influence_histogram": _influence_histogram([after_maps[index] for index in indices], included_names),
            "residual_unweighted_vertices": residual_unweighted[:detail_limit],
            "residual_unweighted_count": len(residual_unweighted),
            "residual_unweighted_truncated": len(residual_unweighted) > detail_limit,
            "untouched_protected_groups": sorted(protected_names),
            "changed_objects": [mesh.name] if changed_vertices or orphan_names else [],
        }

    def add_pose_bone_constraint(self, armature_object_name, bone_name, constraint):
        armature_obj = _armature_object(armature_object_name)
        pose_bone = armature_obj.pose.bones.get(_required_name(bone_name, "bone_name"))
        if pose_bone is None:
            raise ValueError(f"Pose bone not found: {bone_name}")
        spec = dict(constraint or {})
        constraint_type = spec.get("type")
        if constraint_type not in _CONSTRAINT_FIELDS:
            raise ValueError(f"Unsupported pose constraint type: {constraint_type}")
        name = _required_name(spec.get("name"), "constraint.name")
        existing = pose_bone.constraints.get(name)
        if existing is not None and spec.get("existing_policy", "ERROR") == "ERROR":
            raise ValueError(f"Constraint '{name}' already exists on pose bone '{bone_name}'")
        if existing is not None and existing.type != constraint_type:
            raise ValueError(
                f"Constraint '{name}' on '{bone_name}' has type {existing.type}, not requested {constraint_type}"
            )
        target_name = spec.get("target_object_name")
        target = bpy.data.objects.get(target_name) if target_name else None
        if target_name and target is None:
            raise ValueError(f"Constraint target object not found: {target_name}")
        if "target" in _CONSTRAINT_FIELDS[constraint_type] and target is None:
            raise ValueError(f"target_object_name is required for {constraint_type}")
        subtarget = spec.get("subtarget")
        if subtarget:
            if target is None or target.type != "ARMATURE" or target.data.bones.get(subtarget) is None:
                raise ValueError(f"Constraint subtarget bone '{subtarget}' does not exist on '{target_name}'")
            if target == armature_obj and _constraint_dependency_cycle(
                armature_obj, pose_bone.name, subtarget, ignored_constraint=existing
            ):
                raise ValueError(f"Constraint would create a dependency cycle: {pose_bone.name} -> {subtarget}")
        pole_target_name = spec.get("pole_target_object_name")
        pole_target = bpy.data.objects.get(pole_target_name) if pole_target_name else None
        if pole_target_name and pole_target is None:
            raise ValueError(f"Pole target object not found: {pole_target_name}")
        pole_subtarget = spec.get("pole_subtarget")
        if pole_subtarget and (
            pole_target is None or pole_target.type != "ARMATURE" or pole_target.data.bones.get(pole_subtarget) is None
        ):
            raise ValueError(f"Pole subtarget bone '{pole_subtarget}' does not exist on '{pole_target_name}'")
        action = None
        action_slot = None
        if constraint_type == "ACTION":
            action = bpy.data.actions.get(spec.get("action_name"))
            if action is None:
                raise ValueError(f"Action not found: {spec.get('action_name')}")
            action_slots = list(getattr(action, "slots", ()))
            slot_identifier = spec.get("action_slot_identifier")
            if slot_identifier is not None:
                action_slot = next(
                    (slot for slot in action_slots if slot.identifier == slot_identifier),
                    None,
                )
                if action_slot is None:
                    raise ValueError(f"Action slot not found on '{action.name}': {slot_identifier}")
            elif len(action_slots) == 1:
                action_slot = action_slots[0]
            elif len(action_slots) > 1:
                raise ValueError(f"Action '{action.name}' has multiple slots; action_slot_identifier is required")
        if constraint_type == "SPLINE_IK" and target is not None and target.type != "CURVE":
            raise ValueError("SPLINE_IK target must be a Curve object")
        before_matrix = pose_bone.matrix.copy()
        snapshot = _snapshot_constraint(pose_bone, existing) if existing is not None else None
        created = existing is None
        try:
            configured = existing or pose_bone.constraints.new(type=constraint_type)
            configured.name = name
            fields = _constraint_payload_fields(spec, target, pole_target, action, action_slot)
            for field, value in fields.items():
                if not hasattr(configured, field):
                    raise ValueError(f"{constraint_type} does not support property '{field}' in this Blender build")
                setattr(configured, field, value)
            if spec.get("stack_index") is not None:
                source = list(pose_bone.constraints).index(configured)
                destination = min(int(spec["stack_index"]), len(pose_bone.constraints) - 1)
                pose_bone.constraints.move(source, destination)
            if constraint_type == "CHILD_OF" and spec.get("preserve_pose"):
                # Blender 5.1 documents this as the data-API request to recalculate the inverse.
                configured.set_inverse_pending = True
            bpy.context.view_layer.update()
        except Exception:
            if created:
                candidate = pose_bone.constraints.get(name)
                if candidate is not None:
                    pose_bone.constraints.remove(candidate)
            else:
                _restore_constraint(pose_bone, name, snapshot)
            raise
        return {
            "armature_object": armature_obj.name,
            "bone": pose_bone.name,
            "constraint": _constraint_info(configured),
            "constraint_index": list(pose_bone.constraints).index(configured),
            "evaluated_matrix_before": _matrix_list(before_matrix),
            "evaluated_matrix_after": _matrix_list(pose_bone.matrix),
            "matrix_space": "ARMATURE_POSE",
            "changed_objects": [armature_obj.name],
        }

    def validate_character_rig(
        self,
        armature_object_names=None,
        mesh_object_names=None,
        frames=None,
        influence_limit=4,
        normalization_tolerance=1e-4,
        issue_limit=500,
        issue_offset=0,
    ):
        _validate_limit_offset(issue_limit, issue_offset, 2_000, "issue")
        if not 1 <= int(influence_limit) <= 64:
            raise ValueError("influence_limit must be in [1, 64]")
        tolerance = _finite(normalization_tolerance, "normalization_tolerance")
        if not 0 <= tolerance <= 1:
            raise ValueError("normalization_tolerance must be in [0, 1]")
        if armature_object_names is None:
            armatures = [obj for obj in bpy.data.objects if obj.type == "ARMATURE"]
        else:
            _unique_names(armature_object_names, "armature object names")
            armatures = [_armature_object(name) for name in armature_object_names]
        if mesh_object_names is None:
            armature_set = set(armatures)
            meshes = [
                obj
                for obj in bpy.data.objects
                if obj.type == "MESH"
                and (
                    obj.parent in armature_set
                    or any(
                        modifier.type == "ARMATURE" and modifier.object in armature_set for modifier in obj.modifiers
                    )
                )
            ]
        else:
            _unique_names(mesh_object_names, "mesh object names")
            meshes = [_mesh_object(name) for name in mesh_object_names]
        issues = []
        rig_ids = defaultdict(list)
        for armature_obj in armatures:
            rig_id = armature_obj.get("rig_id")
            if rig_id is not None:
                rig_ids[str(rig_id)].append(armature_obj.name)
            if any(abs(abs(float(value)) - 1.0) > 1e-5 for value in armature_obj.scale):
                severity = "ERROR" if any(float(value) < 0 for value in armature_obj.scale) else "WARNING"
                issues.append(
                    _issue(
                        "ARMATURE_NONUNIT_SCALE",
                        severity,
                        "Armature object scale is non-unit or negative",
                        object=armature_obj.name,
                        evidence={"scale": list(armature_obj.scale)},
                        remediation="Review and intentionally apply or preserve armature scale before delivery.",
                    )
                )
            collections = list(armature_obj.data.collections_all)
            if not collections:
                issues.append(
                    _issue(
                        "NO_BONE_COLLECTIONS",
                        "WARNING",
                        "Armature has no bone collections",
                        object=armature_obj.name,
                        remediation="Organize deform, mechanism, and control bones in named bone collections.",
                    )
                )
            for collection in collections:
                if not collection.bones:
                    issues.append(
                        _issue(
                            "EMPTY_BONE_COLLECTION",
                            "INFO",
                            "Bone collection is empty",
                            object=armature_obj.name,
                            bone_collection=collection.name,
                            remediation="Assign intended bones or remove the collection after confirmation.",
                        )
                    )
            parents = {bone.name: getattr(bone.parent, "name", None) for bone in armature_obj.data.bones}
            for cycle in _hierarchy_cycles(parents):
                issues.append(
                    _issue(
                        "BONE_HIERARCHY_CYCLE",
                        "ERROR",
                        "Bone hierarchy contains a cycle",
                        object=armature_obj.name,
                        evidence={"cycle": cycle},
                        remediation="Break the parent cycle in the rest hierarchy.",
                    )
                )
            for bone in armature_obj.data.bones:
                if bone.length <= 1e-8:
                    issues.append(
                        _issue(
                            "ZERO_LENGTH_BONE",
                            "ERROR",
                            "Bone has zero or near-zero rest length",
                            object=armature_obj.name,
                            bone=bone.name,
                            evidence={"length": float(bone.length)},
                            remediation="Move the head or tail in Edit Mode.",
                        )
                    )
                if (
                    bone.use_connect
                    and bone.parent is not None
                    and (bone.head_local - bone.parent.tail_local).length > 1e-6
                ):
                    issues.append(
                        _issue(
                            "CONNECTED_BONE_GAP",
                            "ERROR",
                            "Connected bone head does not match parent tail",
                            object=armature_obj.name,
                            bone=bone.name,
                            evidence={"gap": float((bone.head_local - bone.parent.tail_local).length)},
                            remediation="Snap the connected head to its parent tail.",
                        )
                    )
                pose_bone = armature_obj.pose.bones.get(bone.name)
                if pose_bone is None:
                    continue
                if pose_bone.custom_shape is not None and bpy.data.objects.get(pose_bone.custom_shape.name) is None:
                    issues.append(
                        _issue(
                            "INVALID_CUSTOM_SHAPE",
                            "ERROR",
                            "Pose bone custom shape is no longer a live object",
                            object=armature_obj.name,
                            bone=bone.name,
                            remediation="Assign an existing custom-shape object.",
                        )
                    )
                for constraint in pose_bone.constraints:
                    target = getattr(constraint, "target", None)
                    subtarget = getattr(constraint, "subtarget", "")
                    if not getattr(constraint, "is_valid", True):
                        issues.append(
                            _issue(
                                "INVALID_CONSTRAINT",
                                "ERROR",
                                "Blender reports that the pose constraint is invalid",
                                object=armature_obj.name,
                                bone=bone.name,
                                constraint=constraint.name,
                                remediation="Inspect its target, spaces, and dependency graph.",
                            )
                        )
                    if (
                        hasattr(constraint, "target")
                        and target is None
                        and constraint.type
                        not in {
                            "LIMIT_LOCATION",
                            "LIMIT_ROTATION",
                            "LIMIT_SCALE",
                        }
                    ):
                        issues.append(
                            _issue(
                                "MISSING_CONSTRAINT_TARGET",
                                "ERROR",
                                "Constraint has no target",
                                object=armature_obj.name,
                                bone=bone.name,
                                constraint=constraint.name,
                                remediation="Assign a valid target or remove the constraint.",
                            )
                        )
                    elif subtarget and (target.type != "ARMATURE" or target.data.bones.get(subtarget) is None):
                        issues.append(
                            _issue(
                                "MISSING_CONSTRAINT_SUBTARGET",
                                "ERROR",
                                "Constraint subtarget does not exist",
                                object=armature_obj.name,
                                bone=bone.name,
                                constraint=constraint.name,
                                evidence={"target": target.name, "subtarget": subtarget},
                                remediation="Choose an existing target bone.",
                            )
                        )
                    elif (
                        target == armature_obj
                        and subtarget
                        and _constraint_dependency_cycle(
                            armature_obj, bone.name, subtarget, ignored_constraint=constraint
                        )
                    ):
                        issues.append(
                            _issue(
                                "CONSTRAINT_DEPENDENCY_CYCLE",
                                "ERROR",
                                "Pose constraint participates in a bone dependency cycle",
                                object=armature_obj.name,
                                bone=bone.name,
                                constraint=constraint.name,
                                evidence={"subtarget": subtarget},
                                remediation="Remove or redirect one constraint edge in the cycle.",
                            )
                        )
                    if constraint.type == "IK":
                        if constraint.chain_count > 0:
                            available = 1
                            cursor = bone.parent
                            while cursor is not None:
                                available += 1
                                cursor = cursor.parent
                            if constraint.chain_count > available:
                                issues.append(
                                    _issue(
                                        "INVALID_IK_CHAIN_LENGTH",
                                        "ERROR",
                                        "IK chain_count exceeds the available parent chain",
                                        object=armature_obj.name,
                                        bone=bone.name,
                                        constraint=constraint.name,
                                        evidence={"chain_count": constraint.chain_count, "available": available},
                                        remediation="Reduce chain_count or extend the parent chain.",
                                    )
                                )
                        if (
                            constraint.pole_target is not None
                            and constraint.pole_target == armature_obj
                            and constraint.pole_subtarget == bone.name
                        ):
                            issues.append(
                                _issue(
                                    "INVALID_IK_POLE",
                                    "ERROR",
                                    "IK pole targets the constrained bone itself",
                                    object=armature_obj.name,
                                    bone=bone.name,
                                    constraint=constraint.name,
                                    remediation="Use a separate pole control.",
                                )
                            )
            for bone in armature_obj.data.bones:
                if not bone.name.endswith(".L"):
                    continue
                partner = armature_obj.data.bones.get(f"{bone.name[:-2]}.R")
                if partner is None:
                    continue
                expected_head = bone.head_local.copy()
                expected_tail = bone.tail_local.copy()
                expected_head.x *= -1
                expected_tail.x *= -1
                expected_z = bone.matrix_local.to_3x3().col[2].copy()
                expected_z.x *= -1
                partner_z = partner.matrix_local.to_3x3().col[2]
                geometry_error = max(
                    float((expected_head - partner.head_local).length),
                    float((expected_tail - partner.tail_local).length),
                )
                roll_alignment = float(expected_z.normalized().dot(partner_z.normalized()))
                if geometry_error > 1e-5 or roll_alignment < 0.9999:
                    issues.append(
                        _issue(
                            "INCONSISTENT_MIRROR_PAIR",
                            "WARNING",
                            "Left/right bone pair is not an X-reflected rest transform",
                            object=armature_obj.name,
                            bone=bone.name,
                            evidence={
                                "partner": partner.name,
                                "geometry_error": geometry_error,
                                "roll_axis_alignment": roll_alignment,
                            },
                            remediation="Mirror the pair from the authoritative side or review intentional asymmetry.",
                        )
                    )
            for owner in (armature_obj, armature_obj.data):
                for curve in _all_fcurves(owner):
                    for encoded_name in _BONE_PATH.findall(curve.data_path):
                        referenced_name = encoded_name.replace('\\"', '"').replace("\\\\", "\\")
                        if armature_obj.data.bones.get(referenced_name) is None:
                            issues.append(
                                _issue(
                                    "BROKEN_ANIMATION_BONE_PATH",
                                    "ERROR",
                                    "Animation or driver data path references a missing bone",
                                    object=armature_obj.name,
                                    bone=referenced_name,
                                    evidence={"owner": owner.name, "data_path": curve.data_path},
                                    remediation="Repair or remove the stale F-Curve/driver path.",
                                )
                            )
                    driver = getattr(curve, "driver", None)
                    for variable in getattr(driver, "variables", ()) if driver is not None else ():
                        for target in variable.targets:
                            if target.id is None:
                                issues.append(
                                    _issue(
                                        "BROKEN_DRIVER_TARGET",
                                        "ERROR",
                                        "Driver variable has no target ID",
                                        object=armature_obj.name,
                                        evidence={"owner": owner.name, "data_path": curve.data_path},
                                        remediation="Assign a live target or remove the driver variable.",
                                    )
                                )
                            elif target.bone_target and (
                                getattr(target.id, "type", None) != "ARMATURE"
                                or target.id.data.bones.get(target.bone_target) is None
                            ):
                                issues.append(
                                    _issue(
                                        "BROKEN_DRIVER_BONE_TARGET",
                                        "ERROR",
                                        "Driver variable references a missing bone target",
                                        object=armature_obj.name,
                                        bone=target.bone_target,
                                        evidence={
                                            "owner": owner.name,
                                            "data_path": curve.data_path,
                                            "target": getattr(target.id, "name", None),
                                        },
                                        remediation="Choose an existing armature bone target.",
                                    )
                                )
            users = [obj.name for obj in bpy.data.objects if obj.data == armature_obj.data]
            if len(users) > 1:
                issues.append(
                    _issue(
                        "SHARED_ARMATURE_DATA",
                        "WARNING",
                        "Armature datablock is shared by multiple objects",
                        object=armature_obj.name,
                        evidence={"users": users},
                        remediation="Confirm shared rest-data edits are intentional.",
                    )
                )
            animation = getattr(armature_obj, "animation_data", None)
            action = getattr(animation, "action", None) if animation else None
            if action is not None:
                action_users = [
                    obj.name
                    for obj in bpy.data.objects
                    if getattr(getattr(obj, "animation_data", None), "action", None) == action
                ]
                if len(action_users) > 1:
                    issues.append(
                        _issue(
                            "SHARED_ACTION",
                            "WARNING",
                            "Action is shared by multiple objects",
                            object=armature_obj.name,
                            evidence={"action": action.name, "users": action_users},
                            remediation="Confirm edits to the shared action are intentional.",
                        )
                    )
        for rig_id, names in rig_ids.items():
            if len(names) > 1:
                issues.append(
                    _issue(
                        "DUPLICATE_RIG_ID",
                        "ERROR",
                        "Multiple armature objects share the same rig_id",
                        evidence={"rig_id": rig_id, "objects": names},
                        remediation="Assign a unique rig_id to each independent rig.",
                    )
                )
        for mesh in meshes:
            if any(abs(abs(float(value)) - 1.0) > 1e-5 for value in mesh.scale):
                severity = "ERROR" if any(float(value) < 0 for value in mesh.scale) else "WARNING"
                issues.append(
                    _issue(
                        "MESH_NONUNIT_SCALE",
                        severity,
                        "Skinned mesh scale is non-unit or negative",
                        object=mesh.name,
                        evidence={"scale": list(mesh.scale)},
                        remediation="Review mesh scale before binding or export.",
                    )
                )
            modifiers = [
                (index, modifier) for index, modifier in enumerate(mesh.modifiers) if modifier.type == "ARMATURE"
            ]
            if not modifiers:
                issues.append(
                    _issue(
                        "MISSING_ARMATURE_MODIFIER",
                        "ERROR",
                        "Skinned mesh has no Armature modifier",
                        object=mesh.name,
                        remediation="Bind the mesh to an explicit armature.",
                    )
                )
                continue
            for index, modifier in modifiers:
                if modifier.object not in armatures:
                    issues.append(
                        _issue(
                            "WRONG_ARMATURE_TARGET",
                            "ERROR",
                            "Armature modifier targets an armature outside the validation scope",
                            object=mesh.name,
                            evidence={"modifier": modifier.name, "target": getattr(modifier.object, "name", None)},
                            remediation="Assign the intended armature or include it in validation scope.",
                        )
                    )
                    continue
                topology_indices = [
                    i for i, item in enumerate(mesh.modifiers) if item.type in {"SUBSURF", "REMESH", "NODES"}
                ]
                if topology_indices and index > min(topology_indices):
                    issues.append(
                        _issue(
                            "ARMATURE_MODIFIER_ORDER",
                            "WARNING",
                            "Armature modifier follows a topology-changing modifier",
                            object=mesh.name,
                            evidence={"modifier": modifier.name, "stack_index": index},
                            remediation="Review modifier order so weights address the intended topology.",
                        )
                    )
                skin = _skinning_record(mesh, modifier.object, influence_limit, tolerance, 1e-8)
                for vertex in skin["unweighted_vertices"]:
                    issues.append(
                        _issue(
                            "UNWEIGHTED_VERTEX",
                            "ERROR",
                            "Vertex has no positive deform-bone weight",
                            object=mesh.name,
                            vertex=vertex,
                            remediation="Assign and normalize deform weights.",
                        )
                    )
                for record in skin["non_normalized_vertices"]:
                    issues.append(
                        _issue(
                            "NON_NORMALIZED_VERTEX",
                            "WARNING",
                            "Deform weights do not sum to one",
                            object=mesh.name,
                            vertex=record["vertex"],
                            evidence={"sum": record["sum"]},
                            remediation="Normalize deform weights while respecting locked groups.",
                        )
                    )
                for record in skin["excessive_influences"]:
                    issues.append(
                        _issue(
                            "EXCESSIVE_INFLUENCES",
                            "WARNING",
                            "Vertex exceeds the configured deform influence limit",
                            object=mesh.name,
                            vertex=record["vertex"],
                            evidence={"count": record["count"], "limit": influence_limit},
                            remediation="Prune and normalize weights with a stable influence limit.",
                        )
                    )
                for group in skin["groups_for_missing_bones"]:
                    issues.append(
                        _issue(
                            "GROUP_FOR_MISSING_BONE",
                            "WARNING",
                            "Vertex group does not match any bone on the target armature",
                            object=mesh.name,
                            vertex_group=group,
                            remediation="Confirm it is non-deforming or remove it explicitly.",
                        )
                    )
                for group in skin["absent_deform_groups"]:
                    issues.append(
                        _issue(
                            "ABSENT_DEFORM_GROUP",
                            "INFO",
                            "Deform bone has no corresponding vertex group",
                            object=mesh.name,
                            bone=group,
                            remediation="Create the group if this bone should influence the mesh.",
                        )
                    )
        frame_records = []
        frames = list(frames or ())
        if len(frames) > 50:
            raise ValueError("At most 50 validation frames are allowed")
        scene = bpy.context.scene
        original_frame = scene.frame_current
        try:
            for frame in frames:
                scene.frame_set(int(frame))
                bpy.context.view_layer.update()
                for armature_obj in armatures:
                    invalid_bones = []
                    for pose_bone in armature_obj.pose.bones:
                        if any(not math.isfinite(float(value)) for row in pose_bone.matrix for value in row):
                            invalid_bones.append(pose_bone.name)
                    frame_records.append(
                        {"frame": int(frame), "armature": armature_obj.name, "finite_pose_matrices": not invalid_bones}
                    )
                    for bone_name in invalid_bones:
                        issues.append(
                            _issue(
                                "NONFINITE_EVALUATED_POSE",
                                "ERROR",
                                "Evaluated pose matrix contains a non-finite value",
                                object=armature_obj.name,
                                bone=bone_name,
                                frame=int(frame),
                                remediation="Inspect constraints, drivers, and keyed transforms at this frame.",
                            )
                        )
        finally:
            if frames:
                scene.frame_set(original_frame)
                bpy.context.view_layer.update()
        severity_order = {"ERROR": 0, "WARNING": 1, "INFO": 2}
        issues.sort(
            key=lambda item: (
                severity_order[item["severity"]],
                item["code"],
                item.get("object", ""),
                item.get("bone", ""),
                item.get("vertex", -1),
            )
        )
        start, end, truncated, next_offset = paginate(len(issues), issue_offset, issue_limit, 2_000)
        counts = Counter(item["severity"] for item in issues)
        return {
            "valid": counts["ERROR"] == 0,
            "summary": {"errors": counts["ERROR"], "warnings": counts["WARNING"], "info": counts["INFO"]},
            "issues": {
                "items": issues[start:end],
                "total": len(issues),
                "offset": start,
                "limit": issue_limit,
                "truncated": truncated,
                "next_offset": next_offset,
            },
            "evaluated_frames": frame_records,
            "scope": {
                "armatures": [obj.name for obj in armatures],
                "meshes": [obj.name for obj in meshes],
                "frames": frames,
            },
            "limitations": [
                "Structural validation does not certify artistic deformation or control behavior.",
                "Rest-pose changes made before this request cannot be inferred without an external baseline.",
                "IK pole quality is checked structurally; artistic pole placement still requires review.",
            ],
        }
