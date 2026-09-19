# pyright: reportGeneralTypeIssues=false
"""Animation handoff between authored transforms and rigid-body simulation."""

import contextlib
import math

import bpy
import mathutils

from .inspection_and_setup import (
    _animation_info,
    _clear_action_fcurves,
    _ensure_world,
    _object,
    _prepare_cache_mutation,
    _scene,
)


def _finite_vector(value, label):
    if value is None:
        return None
    if len(value) != 3 or any(
        isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item) for item in value
    ):
        raise ValueError(f"{label} must contain three finite numbers")
    return mathutils.Vector(value)


def _new_action(obj, action_name, overwrite, frame):
    animation = obj.animation_data_create()
    previous = animation.action
    name = action_name or f"{obj.name} Rigid Body Handoff"
    existing = bpy.data.actions.get(name)
    if existing is not None and not overwrite:
        raise ValueError(f"Action already exists: {name}")
    preserved_track = None
    if previous is not None and previous != existing:
        preserved_track = animation.nla_tracks.new()
        preserved_track.name = f"Preserved {previous.name}"
        preserved_track.strips.new(previous.name, int(frame), previous)
    if existing is not None:
        action = existing
        _clear_action_fcurves(action)
    else:
        action = bpy.data.actions.new(name)
    animation.action = action
    return action, previous, preserved_track


def _insert_transform_keys(obj, frame):
    for path in ("location", "rotation_quaternion", "scale"):
        if not obj.keyframe_insert(data_path=path, frame=frame, group="Rigid Body Handoff"):
            raise RuntimeError(f"Failed to key {obj.name}.{path} at frame {frame}")


def _ragdoll_pose_drivers(obj):
    driver_name = obj.get("blendermcp_rigid_body_pose_driver")
    driver = obj.constraints.get(driver_name) if driver_name else None
    return [driver] if driver is not None else []


def _key_driver_influence(drivers, frame, influence):
    for driver in drivers:
        driver.influence = influence
        if not driver.keyframe_insert(data_path="influence", frame=frame, group="Rigid Body Handoff"):
            raise RuntimeError(f"Failed to key ragdoll pose driver '{driver.name}' at frame {frame}")


class RigidBodyAnimationHandlers:
    """Create explicit, reversible animation-to-simulation transitions."""

    def animate_rigid_body_release(
        self,
        scene_name,
        object_name,
        transition,
        frame,
        pre_roll_frames=1,
        linear_velocity=None,
        angular_velocity=None,
        action_name=None,
        overwrite_existing_action=False,
        confirm_delete_baked_cache=False,
    ):
        scene = _scene(scene_name)
        obj = _object(object_name)
        if obj.name not in scene.objects or obj.rigid_body is None:
            raise ValueError(f"Object '{object_name}' must be a rigid body in scene '{scene.name}'")
        if transition not in {"RELEASE", "CAPTURE"}:
            raise ValueError("transition must be RELEASE or CAPTURE")
        if not isinstance(frame, int) or isinstance(frame, bool):
            raise ValueError("frame must be an integer")
        if not isinstance(pre_roll_frames, int) or not 1 <= pre_roll_frames <= 120:
            raise ValueError("pre_roll_frames must be in [1, 120]")
        velocity = _finite_vector(linear_velocity, "linear_velocity")
        spin = _finite_vector(angular_velocity, "angular_velocity")
        if transition == "CAPTURE" and (velocity is not None or spin is not None):
            raise ValueError("Initial velocity is only valid for RELEASE")
        world = _ensure_world(scene)
        cache_freed = _prepare_cache_mutation(scene, world, confirm_delete_baked_cache)
        original_frame = scene.frame_current
        original_subframe = scene.frame_subframe
        original_matrix = obj.matrix_world.copy()
        original_mode = obj.rotation_mode
        original_kinematic = obj.rigid_body.kinematic
        pose_drivers = _ragdoll_pose_drivers(obj)
        original_driver_influences = {driver.name: driver.influence for driver in pose_drivers}
        original_action = obj.animation_data.action if obj.animation_data else None
        action = None
        preserved_track = None
        try:
            scene.frame_set(frame)
            bpy.context.view_layer.update()
            transition_matrix = obj.evaluated_get(bpy.context.evaluated_depsgraph_get()).matrix_world.copy()
            action, _previous, preserved_track = _new_action(obj, action_name, overwrite_existing_action, frame)
            obj.rotation_mode = "QUATERNION"
            obj.matrix_world = transition_matrix
            if transition == "RELEASE":
                prior_frame = frame - pre_roll_frames
                seconds = pre_roll_frames / (float(scene.render.fps) / max(float(scene.render.fps_base), 1e-9))
                prior_matrix = transition_matrix.copy()
                location, rotation, scale = transition_matrix.decompose()
                if velocity is not None:
                    location -= velocity * seconds
                if spin is not None and spin.length > 1e-12:
                    delta = mathutils.Quaternion(spin.normalized(), -spin.length * seconds)
                    rotation = delta @ rotation
                prior_matrix = mathutils.Matrix.LocRotScale(location, rotation, scale)
                obj.matrix_world = prior_matrix
                obj.rigid_body.kinematic = True
                _key_driver_influence(pose_drivers, prior_frame, 1.0)
                _insert_transform_keys(obj, prior_frame)
                if not obj.keyframe_insert(
                    data_path="rigid_body.kinematic", frame=prior_frame, group="Rigid Body Handoff"
                ):
                    raise RuntimeError("Failed to key rigid_body.kinematic before release")
                _key_driver_influence(pose_drivers, frame, 0.0)
                bpy.context.view_layer.update()
                obj.matrix_world = transition_matrix
                _insert_transform_keys(obj, frame)
                obj.rigid_body.kinematic = False
                if not obj.keyframe_insert(data_path="rigid_body.kinematic", frame=frame, group="Rigid Body Handoff"):
                    raise RuntimeError("Failed to key rigid_body.kinematic at release")
                keyed_frames = [prior_frame, frame]
            else:
                before = frame - 1
                obj.rigid_body.kinematic = False
                _key_driver_influence(pose_drivers, before, 0.0)
                if not obj.keyframe_insert(data_path="rigid_body.kinematic", frame=before, group="Rigid Body Handoff"):
                    raise RuntimeError("Failed to key rigid_body.kinematic before capture")
                obj.matrix_world = transition_matrix
                _insert_transform_keys(obj, frame)
                obj.rigid_body.kinematic = True
                _key_driver_influence(pose_drivers, frame, 1.0)
                if not obj.keyframe_insert(data_path="rigid_body.kinematic", frame=frame, group="Rigid Body Handoff"):
                    raise RuntimeError("Failed to key rigid_body.kinematic at capture")
                keyed_frames = [before, frame]
        except Exception:
            if obj.animation_data:
                obj.animation_data.action = original_action
                if preserved_track is not None:
                    obj.animation_data.nla_tracks.remove(preserved_track)
            if action is not None and action != original_action and action.users == 0:
                with contextlib.suppress(Exception):
                    bpy.data.actions.remove(action)
            raise
        finally:
            obj.matrix_world = original_matrix
            obj.rotation_mode = original_mode
            obj.rigid_body.kinematic = original_kinematic
            for driver in pose_drivers:
                driver.influence = original_driver_influences[driver.name]
            scene.frame_set(original_frame, subframe=original_subframe)
            bpy.context.view_layer.update()
        return {
            "changed_objects": [obj.name],
            "object": obj.name,
            "transition": transition,
            "keyed_frames": keyed_frames,
            "action": action.name,
            "animation": _animation_info(obj),
            "source_action_preserved": original_action.name if original_action else None,
            "timeline_restored": {"frame": scene.frame_current, "subframe": scene.frame_subframe},
            "cache_freed": cache_freed,
            "warnings": [
                "Initial velocity is derived from the keyed pre-roll transform; "
                "Blender exposes no direct initial-velocity RNA property."
            ]
            if velocity is not None or spin is not None
            else [],
        }
