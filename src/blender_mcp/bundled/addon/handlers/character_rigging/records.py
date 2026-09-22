# Inherited findings, carried over with the code: every line in this module was moved verbatim out of
# `foundation.py`, where one file-wide pragma covered them, and `scripts/lint_changed.py` attributes a
# moved line to the branch that moved it. Suppressed, not fixed: restructuring the bodies would make a
# pure move unreviewable. They stay in the whole-tree backlog `just lint-all` reports.
# ruff: file-ignore[magic-value-comparison]
"""
The read-only serializers: a rig datablock in, a reply dict out.

Nothing here writes. These are the shapes `get_character_rig_info`, `get_skinning_info` and
`validate_character_rig` publish, and they are not kept beside those handlers because the
editing modules report what they changed with the same functions - a bind reports its
modifier through `_armature_modifier_info`, a weight edit its spread through
`_influence_histogram`. One spelling of each record, in one place.
"""

from collections import Counter

import bpy

from ...helpers import sync_from_editmode
from .constraints import _constraint_info
from .primitives import _POSE_FIELDS, _custom_properties, _matrix_list, _plain


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
