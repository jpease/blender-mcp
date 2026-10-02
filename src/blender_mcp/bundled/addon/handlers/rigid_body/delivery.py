# pyright: reportGeneralTypeIssues=false
"""Bake evaluated rigid-body transforms into editable animation actions."""

import contextlib

import bpy

from ..animation import _continuous_rotation
from .inspection_and_setup import (
    _ensure_collection,
    _remove_rigid_body,
    _scene,
    _validate_object_batch,
    _view_layer_for,
)


def _create_output_action(obj, name, overwrite):
    animation = obj.animation_data_create()
    if animation.action is not None and not overwrite:
        raise ValueError(f"Object '{obj.name}' already has action '{animation.action.name}'")
    existing = bpy.data.actions.get(name)
    if existing is not None and not overwrite:
        raise ValueError(f"Action already exists: {name}")
    action = bpy.data.actions.new(name)
    animation.action = action
    return action


def _key_transform(obj, frame, key_scale):
    paths = ["location", "rotation_quaternion"]
    if key_scale:
        paths.append("scale")
    for path in paths:
        if not obj.keyframe_insert(data_path=path, frame=frame, group="Rigid Body Bake"):
            raise RuntimeError(f"Failed to key {obj.name}.{path} at frame {frame}")


def _key_outputs(sources, outputs, depsgraph, frame, key_scale, previous_rotations):
    """
    Place and key every output at its source's evaluated matrix for one frame.

    The `matrix_world` setter spells each orientation with w >= 0, so a body turning through a
    half turn between two frames flipped hemisphere and its curve spun the long way round. Each
    rotation is re-spelled nearest the one this output was keyed with at the previous frame.

    Args:
        sources: The simulated objects, read before any output moves.
        outputs: The objects receiving the bake, one per source.
        depsgraph: The depsgraph evaluated at this frame.
        frame: The frame to key.
        key_scale: Whether scale is keyed too.
        previous_rotations: Each output's last keyed quaternion by name; updated in place.

    """
    matrices = [source.evaluated_get(depsgraph).matrix_world.copy() for source in sources]
    for output, matrix in zip(outputs, matrices, strict=True):
        output.matrix_world = matrix
        rotation = _continuous_rotation(
            "QUATERNION", output.rotation_quaternion.copy(), previous_rotations.get(output.name)
        )
        output.rotation_quaternion = previous_rotations[output.name] = rotation
        _key_transform(output, frame, key_scale)


class RigidBodyDeliveryHandlers:
    """Deliver evaluated simulation as ordinary transform animation without destroying sources."""

    def bake_rigid_bodies_to_keyframes(
        self,
        scene_name,
        object_names,
        frame_start,
        frame_end,
        frame_step=1,
        output_mode="DUPLICATES",
        output_collection_name="Rigid Body Bakes",
        action_name_prefix="Rigid Body Bake",
        key_scale=False,
        confirm_overwrite_animation=False,
    ):
        scene = _scene(scene_name)
        sources = _validate_object_batch(scene, object_names, require_body=True)
        if frame_start > frame_end or not 1 <= frame_step <= 120:
            raise ValueError("Require frame_start <= frame_end and frame_step in [1, 120]")
        frames = list(range(frame_start, frame_end + 1, frame_step))
        if len(frames) > 10_000:
            raise ValueError("Keyframe bake exceeds the 10000-frame-step limit")
        if output_mode not in {"DUPLICATES", "SOURCE"}:
            raise ValueError("output_mode must be DUPLICATES or SOURCE")
        if output_mode == "SOURCE" and not confirm_overwrite_animation:
            occupied = [obj.name for obj in sources if obj.animation_data and obj.animation_data.action]
            if occupied:
                raise ValueError(
                    f"SOURCE output would replace existing actions on {occupied}; set confirm_overwrite_animation=True"
                )
        collection = None
        outputs = []
        created = []
        if output_mode == "DUPLICATES":
            collection, _created = _ensure_collection(scene, output_collection_name)
            for source in sources:
                output = source.copy()
                output.data = source.data
                output.name = f"{source.name} Baked"
                output.parent = None
                collection.objects.link(output)
                if output.rigid_body is not None:
                    _remove_rigid_body(scene, output)
                output["blendermcp_rigid_body_role"] = "baked_animation"
                output["blendermcp_rigid_body_source"] = source.name
                output["blendermcp_rigid_body_schema"] = 1
                outputs.append(output)
                created.append(output)
        else:
            outputs = list(sources)
        action_names = []
        created_actions = []
        original_actions = {
            output.name: output.animation_data.action if output.animation_data else None for output in outputs
        }
        for source, output in zip(sources, outputs, strict=True):
            action = _create_output_action(
                output,
                f"{action_name_prefix} - {source.name}",
                confirm_overwrite_animation,
            )
            action_names.append(action.name)
            created_actions.append(action)
            output.rotation_mode = "QUATERNION"
        original_frame = scene.frame_current
        original_subframe = scene.frame_subframe
        original_matrices = {obj.name: obj.matrix_world.copy() for obj in outputs}
        previous_rotations = {}
        try:
            for frame in frames:
                scene.frame_set(frame)
                view_layer = _view_layer_for(scene)
                view_layer.update()
                depsgraph = view_layer.depsgraph
                _key_outputs(sources, outputs, depsgraph, frame, key_scale, previous_rotations)
        except Exception:
            for output in outputs:
                if output.animation_data:
                    output.animation_data.action = original_actions[output.name]
            for action in created_actions:
                if action.users == 0:
                    with contextlib.suppress(Exception):
                        bpy.data.actions.remove(action)
            for output in reversed(created):
                with contextlib.suppress(Exception):
                    bpy.data.objects.remove(output, do_unlink=True)
            raise
        finally:
            for output in outputs:
                if output.name in original_matrices:
                    output.matrix_world = original_matrices[output.name]
            scene.frame_set(original_frame, subframe=original_subframe)
            _view_layer_for(scene).update()
        return {
            "changed_objects": [obj.name for obj in outputs],
            "source_objects": [obj.name for obj in sources],
            "output_objects": [obj.name for obj in outputs],
            "created_duplicates": [obj.name for obj in created],
            "output_collection": collection.name if collection else None,
            "actions": action_names,
            "channels": ["location", "rotation_quaternion", *(["scale"] if key_scale else [])],
            "frames": frames,
            # Declares `frames` a page to the reply budget: up to 10,000 keyed frames, while the same
            # key is a `[start, end]` interval in other replies. The budget sets it when it cuts.
            "frames_truncated": False,
            "source_rigid_bodies_retained": True,
            "timeline_restored": {"frame": scene.frame_current, "subframe": scene.frame_subframe},
        }
