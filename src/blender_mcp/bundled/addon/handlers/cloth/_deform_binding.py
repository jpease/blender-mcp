"""Shared deform-modifier binding helpers for cloth handlers."""

from __future__ import annotations

import contextlib

import bpy

from ...helpers import preserve_mode_and_selection, set_active
from .inspection_and_setup import _scene_context_for_object


def _bind_deform_modifier(obj, modifier):
    operator = {
        "MESH_DEFORM": bpy.ops.object.meshdeform_bind,
        "SURFACE_DEFORM": bpy.ops.object.surfacedeform_bind,
    }[modifier.type]
    scene, view_layer = _scene_context_for_object(obj)
    with preserve_mode_and_selection():
        set_active(obj)
        if obj.mode != "OBJECT":
            result = bpy.ops.object.mode_set(mode="OBJECT")
            if "FINISHED" not in result:
                raise RuntimeError(f"Could not enter Object Mode for binding: {sorted(result)}")
        with bpy.context.temp_override(
            scene=scene,
            view_layer=view_layer,
            object=obj,
            active_object=obj,
            selected_objects=[obj],
            selected_editable_objects=[obj],
        ):
            result = operator(modifier=modifier.name)
    if "FINISHED" not in result or not modifier.is_bound:
        raise RuntimeError(f"Blender did not bind {modifier.type} modifier '{modifier.name}': {sorted(result)}")


def _unbind_deform_modifier(obj, modifier):
    if not modifier.is_bound:
        return
    operator = {
        "MESH_DEFORM": bpy.ops.object.meshdeform_bind,
        "SURFACE_DEFORM": bpy.ops.object.surfacedeform_bind,
    }[modifier.type]
    scene, view_layer = _scene_context_for_object(obj)
    with preserve_mode_and_selection():
        set_active(obj)
        if obj.mode != "OBJECT":
            result = bpy.ops.object.mode_set(mode="OBJECT")
            if "FINISHED" not in result:
                raise RuntimeError(f"Could not enter Object Mode for unbinding: {sorted(result)}")
        with bpy.context.temp_override(
            scene=scene,
            view_layer=view_layer,
            object=obj,
            active_object=obj,
            selected_objects=[obj],
            selected_editable_objects=[obj],
        ):
            result = operator(modifier=modifier.name)
    if "FINISHED" not in result or modifier.is_bound:
        raise RuntimeError(f"Blender did not unbind {modifier.type} modifier '{modifier.name}': {sorted(result)}")


def _bind_corrective_smooth(obj, modifier):
    if modifier.is_bind:
        return
    scene, view_layer = _scene_context_for_object(obj)
    with preserve_mode_and_selection():
        set_active(obj)
        if obj.mode != "OBJECT":
            result = bpy.ops.object.mode_set(mode="OBJECT")
            if "FINISHED" not in result:
                raise RuntimeError(f"Could not enter Object Mode for Corrective Smooth binding: {sorted(result)}")
        with bpy.context.temp_override(
            scene=scene,
            view_layer=view_layer,
            object=obj,
            active_object=obj,
            selected_objects=[obj],
            selected_editable_objects=[obj],
        ):
            result = bpy.ops.object.correctivesmooth_bind(modifier=modifier.name)
    if "FINISHED" not in result or not modifier.is_bind:
        raise RuntimeError(f"Blender did not bind Corrective Smooth modifier '{modifier.name}': {sorted(result)}")


def _unbind_corrective_smooth(obj, modifier):
    if not modifier.is_bind:
        return
    scene, view_layer = _scene_context_for_object(obj)
    with preserve_mode_and_selection():
        set_active(obj)
        with bpy.context.temp_override(
            scene=scene,
            view_layer=view_layer,
            object=obj,
            active_object=obj,
            selected_objects=[obj],
            selected_editable_objects=[obj],
        ):
            result = bpy.ops.object.correctivesmooth_bind(modifier=modifier.name)
    if "FINISHED" not in result or modifier.is_bind:
        raise RuntimeError(f"Blender did not unbind Corrective Smooth modifier '{modifier.name}': {sorted(result)}")


def _attachment_target_matrix(target, bone_name=None):
    target_matrix = target.matrix_world.copy()
    if bone_name:
        pose_bone = target.pose.bones.get(bone_name) if target.pose else None
        if pose_bone is None:
            raise ValueError(f"Pose bone not found: {bone_name}")
        target_matrix = target_matrix @ pose_bone.matrix
    if abs(float(target_matrix.determinant())) <= 1e-12:
        raise ValueError(f"Attachment target '{target.name}' has a singular evaluated transform")
    return target_matrix


def _snapshot_attachment_modifier(modifier):
    fields = {
        "HOOK": ("object", "subtarget", "vertex_group", "matrix_inverse", "center"),
        "ARMATURE": ("object", "vertex_group", "use_vertex_groups"),
        "MESH_DEFORM": ("object", "vertex_group"),
        "SURFACE_DEFORM": ("target", "vertex_group"),
    }[modifier.type]
    snapshot = {}
    for name in fields:
        value = getattr(modifier, name)
        snapshot[name] = value.copy() if hasattr(value, "copy") else value
    return snapshot


def _restore_attachment_modifier(modifier, snapshot):
    for name, value in snapshot.items():
        with contextlib.suppress(Exception):
            setattr(modifier, name, value)


def _move_modifier_immediately_before(obj, modifier, following_modifier):
    current = list(obj.modifiers).index(modifier)
    following = list(obj.modifiers).index(following_modifier)
    target = following - 1 if current < following else following
    if current != target:
        obj.modifiers.move(current, target)
    if list(obj.modifiers).index(modifier) + 1 != list(obj.modifiers).index(following_modifier):
        raise RuntimeError(f"Could not place modifier '{modifier.name}' immediately before '{following_modifier.name}'")


def _move_modifier_immediately_after(obj, modifier, preceding_modifier):
    current = list(obj.modifiers).index(modifier)
    preceding = list(obj.modifiers).index(preceding_modifier)
    target = preceding if current < preceding else preceding + 1
    if current != target:
        obj.modifiers.move(current, target)
    if list(obj.modifiers).index(modifier) != list(obj.modifiers).index(preceding_modifier) + 1:
        raise RuntimeError(f"Could not place modifier '{modifier.name}' immediately after '{preceding_modifier.name}'")
