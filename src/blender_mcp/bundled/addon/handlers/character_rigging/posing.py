"""Blender handlers for deterministic pose application and keyframing."""

import contextlib
import math

import bpy
import mathutils

from .foundation import _action_fcurve_collections, _armature_object, _finite, _matrix_list

_POSE_SPACES = {"LOCAL", "LOCAL_WITH_PARENT", "POSE", "WORLD"}


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
    if "scale" in spec:
        scale = mathutils.Vector(spec["scale"])
    return mathutils.Matrix.LocRotScale(tuple(location), rotation, tuple(scale))


def _validate_pose_specs(armature, poses, space):
    if space not in _POSE_SPACES:
        raise ValueError(f"Unsupported pose space: {space}")
    if not poses:
        raise ValueError("At least one pose entry is required")
    names = [item.get("bone_name") for item in poses]
    if len(names) != len(set(names)):
        raise ValueError("Each pose bone may appear only once")
    prepared = []
    for spec in poses:
        pose_bone = armature.pose.bones.get(spec.get("bone_name"))
        if pose_bone is None:
            raise ValueError(f"Pose bone not found: {spec.get('bone_name')}")
        custom = spec.get("custom_properties", {})
        missing = sorted(name for name in custom if name not in pose_bone)
        if missing:
            raise ValueError(f"Custom properties not found on '{pose_bone.name}': {missing}")
        matrix_values = spec.get("matrix")
        if matrix_values is not None:
            if len(matrix_values) != 4 or any(len(row) != 4 for row in matrix_values):
                raise ValueError(f"matrix for '{pose_bone.name}' must be 4x4")
            matrix = mathutils.Matrix(
                tuple(tuple(_finite(value, f"{pose_bone.name}.matrix") for value in row) for row in matrix_values)
            )
        else:
            matrix = _pose_matrix_from_channels(armature, pose_bone, spec, space)
        if abs(matrix.determinant()) <= 1e-12:
            raise ValueError(f"Pose matrix for '{pose_bone.name}' is singular")
        prepared.append((pose_bone, spec, matrix))
    return prepared


def _apply_pose_specs(armature, prepared, space, reset_unspecified=False):
    targeted = {pose_bone.name for pose_bone, _spec, _matrix in prepared}
    before = {pose_bone.name: pose_bone.matrix.copy() for pose_bone, _spec, _matrix in prepared}
    if reset_unspecified:
        for pose_bone in armature.pose.bones:
            if pose_bone.name not in targeted:
                pose_bone.matrix_basis.identity()
        bpy.context.view_layer.update()
    # `pose_bone.matrix` reads the last evaluation, so each bone is set after its parent has been
    # re-evaluated; otherwise a child's pose- or world-space target is solved against a stale parent.
    for pose_bone, spec, input_matrix in sorted(prepared, key=lambda item: len(item[0].parent_recursive)):
        pose_bone.matrix = armature.convert_space(
            pose_bone=pose_bone,
            matrix=input_matrix,
            from_space=space,
            to_space="POSE",
        )
        for name, value in spec.get("custom_properties", {}).items():
            pose_bone[name] = value
        bpy.context.view_layer.update()
    return [
        {
            "bone": pose_bone.name,
            "before_pose_matrix": _matrix_list(before[pose_bone.name]),
            "after_pose_matrix": _matrix_list(pose_bone.matrix),
            "input_space": space,
            "custom_properties": sorted(spec.get("custom_properties", {})),
        }
        for pose_bone, spec, _matrix in prepared
    ]


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


def _pose_key_paths(pose_bone, spec):
    paths = []
    if "matrix" in spec:
        paths.extend(["location", "scale"])
        if pose_bone.rotation_mode == "QUATERNION":
            paths.append("rotation_quaternion")
        elif pose_bone.rotation_mode == "AXIS_ANGLE":
            paths.append("rotation_axis_angle")
        else:
            paths.append("rotation_euler")
    else:
        if "location" in spec:
            paths.append("location")
        if "scale" in spec:
            paths.append("scale")
        if any(name in spec for name in ("rotation_euler", "rotation_quaternion", "rotation_axis_angle")):
            paths.append(
                "rotation_quaternion"
                if pose_bone.rotation_mode == "QUATERNION"
                else "rotation_axis_angle"
                if pose_bone.rotation_mode == "AXIS_ANGLE"
                else "rotation_euler"
            )
    paths.extend(f'["{name}"]' for name in spec.get("custom_properties", {}))
    return paths


def _set_action_interpolation(action, frame, interpolation):
    changed = 0
    for collection in _action_fcurve_collections(action):
        for curve in collection:
            for point in curve.keyframe_points:
                if math.isclose(float(point.co[0]), float(frame), abs_tol=1e-6):
                    point.interpolation = interpolation
                    changed += 1
    return changed


class PoseAnimationHandlersMixin:
    """Apply pose-space transforms and author named animation actions."""

    def set_character_pose(
        self,
        armature_object_name,
        poses,
        space="LOCAL",
        reset_unspecified=False,
        confirm_reset_unspecified=False,
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
            records = _apply_pose_specs(armature, prepared, space, reset_unspecified)
        except Exception:
            for bone in affected:
                bone.matrix_basis = matrices[bone.name]
                for name, value in properties[bone.name].items():
                    bone[name] = value
            raise
        return {"armature_object": armature.name, "space": space, "bones": records, "changed_objects": [armature.name]}

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
    ):
        armature = _armature_object(armature_object_name)
        frame = _finite(frame, "frame")
        if keying_policy not in {"INSERT", "REPLACE", "REMOVE"}:
            raise ValueError("keying_policy must be INSERT, REPLACE, or REMOVE")
        if keying_policy == "REMOVE" and action_policy != "REUSE":
            raise ValueError("Removing keys requires action_policy='REUSE'")
        if interpolation not in {"CONSTANT", "LINEAR", "BEZIER"}:
            raise ValueError("Unsupported interpolation")
        prepared = _validate_pose_specs(armature, list(poses or ()), space)
        scene = bpy.context.scene
        animation = armature.animation_data_create()
        previous_action = animation.action
        previous_slot = getattr(animation, "action_slot", None)
        previous_frame = scene.frame_current
        matrices = {bone.name: bone.matrix_basis.copy() for bone, _spec, _matrix in prepared}
        action = _assign_named_action(armature, action_name, action_policy, action_slot_identifier)
        changed_keys = []
        try:
            whole_frame = math.floor(frame)
            scene.frame_set(whole_frame, subframe=frame - whole_frame)
            if keying_policy != "REMOVE":
                _apply_pose_specs(armature, prepared, space)
            for pose_bone, spec, _matrix in prepared:
                for path in _pose_key_paths(pose_bone, spec):
                    if keying_policy in {"REPLACE", "REMOVE"}:
                        with contextlib.suppress(TypeError):
                            pose_bone.keyframe_delete(data_path=path, frame=frame)
                    if keying_policy != "REMOVE":
                        if not pose_bone.keyframe_insert(data_path=path, frame=frame, group=pose_bone.name):
                            raise RuntimeError(f"Could not insert key for {pose_bone.name}.{path}")
                    changed_keys.append({"bone": pose_bone.name, "data_path": path, "frame": frame})
            interpolation_count = (
                0 if keying_policy == "REMOVE" else _set_action_interpolation(action, frame, interpolation)
            )
            written_slot_identifier = getattr(getattr(animation, "action_slot", None), "identifier", None)
        finally:
            for pose_bone, _spec, _matrix in prepared:
                pose_bone.matrix_basis = matrices[pose_bone.name]
            scene.frame_set(previous_frame)
            animation.action = previous_action
            if previous_action is not None and previous_slot is not None:
                with contextlib.suppress(Exception):
                    animation.action_slot = previous_slot
            bpy.context.view_layer.update()
        return {
            "armature_object": armature.name,
            "action": action.name,
            "action_slot": written_slot_identifier,
            "keying_policy": keying_policy,
            "changed_keys": changed_keys,
            "interpolation_updates": interpolation_count,
            "changed_objects": [armature.name],
            "changed_resources": [{"type": "ACTION", "name": action.name}],
        }
