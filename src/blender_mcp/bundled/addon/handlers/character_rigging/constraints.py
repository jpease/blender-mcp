# Inherited scope metrics: `add_pose_bone_constraint` was over the branch/statement/local limits
# before this pass, and `scripts/lint_changed.py` attributes a whole-scope finding to any branch
# that writes inside the scope.
# Inherited findings, carried over with the code: every line in this module was moved verbatim out of
# `foundation.py`, where one file-wide pragma covered them, and `scripts/lint_changed.py` attributes a
# moved line to the branch that moved it. Suppressed, not fixed: restructuring the bodies would make a
# pure move unreviewable. They stay in the whole-tree backlog `just lint-all` reports.
# ruff: file-ignore[too-many-branches, too-many-locals, too-many-statements, undocumented-public-method, too-many-statements-in-try-clause]
"""
Pose-bone constraints: the per-type field tables, and adding, copying and restoring one.

`_CONSTRAINT_FIELDS` is the whitelist deciding which properties a caller may set and which
this package reads back, so it has to sit below both the records that serialize a constraint
and the edit-mode machinery that rebuilds one around a rename. Nothing else in this package
is imported from here beyond the shared vocabulary.
"""

from collections import defaultdict

import bpy

from .primitives import _armature_object, _matrix_list, _plain, _required_name

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


class PoseConstraintHandlersMixin:
    """Add pose-bone constraints to a rig."""

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
