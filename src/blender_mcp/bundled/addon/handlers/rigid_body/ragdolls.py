# pyright: reportGeneralTypeIssues=false
"""Character ragdoll construction and pose-bone animation delivery."""

import contextlib
import json
import math
import uuid

from itertools import combinations

import bpy
import mathutils

from ..rna_patch import get_object
from .inspection_and_setup import (
    _BODY_FIELDS,
    _aabb_overlap,
    _add_rigid_body,
    _apply_patch,
    _body_info,
    _box_mesh,
    _clear_action_fcurves,
    _convex_hull_mesh,
    _ensure_collection,
    _ensure_world,
    _evaluated_geometry,
    _mesh_volume,
    _preflight_collection_name,
    _prepare_cache_mutation,
    _primitive_proxy_mesh,
    _scene,
    _view_layer_for,
)


def _bone_world_matrix(armature, pose_bone):
    return armature.matrix_world @ pose_bone.matrix


def _bone_proxy_mesh(name, shape, length, radius):
    dimensions = [radius * 2.0, length, radius * 2.0]
    local_bounds = {
        "min": [-radius, 0.0, -radius],
        "max": [radius, length, radius],
        "center": [0.0, length * 0.5, 0.0],
        "dimensions": dimensions,
    }
    if shape == "BOX":
        return _box_mesh(name, local_bounds)
    return _primitive_proxy_mesh(name, "CAPSULE", local_bounds)


def _convex_mesh_in_bone_space(name, source, bone_world):
    view_layer = _view_layer_for(next(scene for scene in bpy.data.scenes if source.name in scene.objects), source)
    evaluated = source.evaluated_get(view_layer.depsgraph)
    mesh = evaluated.to_mesh()
    try:
        inverse = bone_world.inverted()
        vertices = [inverse @ evaluated.matrix_world @ vertex.co for vertex in mesh.vertices]
    finally:
        evaluated.to_mesh_clear()
    if len(vertices) < 4:
        raise ValueError(f"Convex ragdoll source '{source.name}' needs at least four evaluated vertices")
    return _convex_hull_mesh(name, vertices)


def _blend_matrix(authored, simulated, factor):
    authored_location, authored_rotation, authored_scale = authored.decompose()
    simulated_location, simulated_rotation, simulated_scale = simulated.decompose()
    return mathutils.Matrix.LocRotScale(
        authored_location.lerp(simulated_location, factor),
        authored_rotation.slerp(simulated_rotation, factor),
        authored_scale.lerp(simulated_scale, factor),
    )


def _blend_factor(frame, frame_start, frame_end, blend_in_frames, blend_out_frames):
    factor = 1.0
    if blend_in_frames:
        factor = min(factor, max(0.0, (frame - frame_start) / blend_in_frames))
    if blend_out_frames:
        factor = min(factor, max(0.0, (frame_end - frame) / blend_out_frames))
    return factor


def _matrix_interpolation_error(first, middle, last, ratio):
    expected = _blend_matrix(first, last, ratio)
    expected_location, expected_rotation, _expected_scale = expected.decompose()
    actual_location, actual_rotation, _actual_scale = middle.decompose()
    return (
        float((expected_location - actual_location).length),
        abs(float(expected_rotation.rotation_difference(actual_rotation).angle)),
    )


def _reduced_frame_indices(frames, matrices, position_tolerance, angular_tolerance):
    keep = {0, len(frames) - 1}

    def split(start, end):
        if end - start <= 1:
            return
        span = frames[end] - frames[start]
        worst = None
        for index in range(start + 1, end):
            ratio = (frames[index] - frames[start]) / span
            position_error, angular_error = _matrix_interpolation_error(
                matrices[start], matrices[index], matrices[end], ratio
            )
            score = max(
                position_error / max(position_tolerance, 1e-12),
                angular_error / max(angular_tolerance, 1e-12),
            )
            if worst is None or score > worst[0]:
                worst = (score, index, position_error, angular_error)
        if worst and (worst[2] > position_tolerance or worst[3] > angular_tolerance):
            keep.add(worst[1])
            split(start, worst[1])
            split(worst[1], end)

    split(0, len(frames) - 1)
    return keep


def _bone_depth(pose_bone):
    depth = 0
    parent = pose_bone.parent
    while parent is not None:
        depth += 1
        parent = parent.parent
    return depth


class RigidBodyRagdollHandlers:
    """Create armature-mapped rigid bodies and bake their motion back to pose bones."""

    def create_ragdoll_rig(
        self,
        scene_name,
        armature_object_name,
        rig_name,
        bodies,
        joints,
        total_mass,
        proxy_collection_name="Ragdoll Proxies",
        constraint_collection_name="Ragdoll Constraints",
        collision_layers=(4,),
        start_kinematic=True,
        confirm_delete_baked_cache=False,
    ):
        scene = _scene(scene_name)
        armature = get_object(armature_object_name, {"ARMATURE"})
        if armature.name not in scene.objects:
            raise ValueError(f"Armature '{armature.name}' is not linked to scene '{scene.name}'")
        if not rig_name or not 2 <= len(bodies) <= 64 or not 1 <= len(joints) <= 128:
            raise ValueError("Ragdoll requires a name, 2-64 bodies, and 1-128 joints")
        if not math.isfinite(total_mass) or total_mass <= 0:
            raise ValueError("total_mass must be finite and positive")
        bone_names = [body.get("bone_name") for body in bodies]
        if len(bone_names) != len(set(bone_names)):
            raise ValueError("Ragdoll bone names must be unique")
        missing = [name for name in bone_names if armature.pose.bones.get(name) is None]
        if missing:
            raise ValueError(f"Armature is missing mapped pose bones: {missing}")
        body_by_bone = {body["bone_name"]: body for body in bodies}
        pairs = []
        for joint in joints:
            pair = (joint.get("parent_bone_name"), joint.get("child_bone_name"))
            if pair[0] not in body_by_bone or pair[1] not in body_by_bone or pair[0] == pair[1]:
                raise ValueError(f"Invalid ragdoll joint endpoints: {pair}")
            pairs.append(pair)
        if len(pairs) != len(set(pairs)):
            raise ValueError("Ragdoll joint pairs must be unique")
        layers = set(collision_layers)
        if not layers or len(layers) != len(collision_layers) or any(not 1 <= layer <= 20 for layer in layers):
            raise ValueError("collision_layers must contain unique values in [1, 20]")
        for name in (proxy_collection_name, constraint_collection_name):
            _preflight_collection_name(scene, name)
        proxy_names = [body.get("proxy_name") or f"{rig_name} - {body['bone_name']}" for body in bodies]
        constraint_names = [
            joint.get("constraint_name") or f"{rig_name} - {joint['parent_bone_name']} to {joint['child_bone_name']}"
            for joint in joints
        ]
        all_names = [*proxy_names, *constraint_names]
        if len(all_names) != len(set(all_names)):
            raise ValueError("Generated ragdoll object names must be unique")
        conflicts = [name for name in all_names if bpy.data.objects.get(name) is not None]
        if conflicts:
            raise ValueError(f"Ragdoll object names already exist: {conflicts}")
        convex_sources = {}
        for body in bodies:
            source_name = body.get("convex_source_object_name")
            if (body.get("shape", "CAPSULE") == "CONVEX_HULL") != bool(source_name):
                raise ValueError("CONVEX_HULL ragdoll bodies require convex_source_object_name")
            if source_name:
                source = get_object(source_name, {"MESH"})
                if source.name not in scene.objects:
                    raise ValueError(f"Convex source '{source.name}' is not in scene '{scene.name}'")
                convex_sources[body["bone_name"]] = source
        world = _ensure_world(scene)
        cache_freed = _prepare_cache_mutation(scene, world, confirm_delete_baked_cache)
        proxy_collection, _proxy_created = _ensure_collection(scene, proxy_collection_name)
        constraint_collection, _constraint_created = _ensure_collection(scene, constraint_collection_name)
        rig_id = uuid.uuid4().hex
        created_objects = []
        created_meshes = []
        proxies = {}
        raw_mass = {}
        try:
            for body, proxy_name in zip(bodies, proxy_names, strict=True):
                pose_bone = armature.pose.bones[body["bone_name"]]
                length = max(float(pose_bone.length) * float(body.get("length_scale", 0.9)), 1e-4)
                radius = body.get("radius")
                if radius is None:
                    radius = max(length * 0.18, 1e-4)
                if not math.isfinite(radius) or radius <= 0:
                    raise ValueError(f"Ragdoll radius for '{pose_bone.name}' must be finite and positive")
                bone_world = _bone_world_matrix(armature, pose_bone)
                shape = body.get("shape", "CAPSULE")
                if shape == "CONVEX_HULL":
                    mesh = _convex_mesh_in_bone_space(f"{proxy_name} Mesh", convex_sources[pose_bone.name], bone_world)
                    collision_shape = "CONVEX_HULL"
                elif shape in {"CAPSULE", "BOX"}:
                    mesh = _bone_proxy_mesh(f"{proxy_name} Mesh", shape, length, radius)
                    collision_shape = shape
                else:
                    raise ValueError(f"Unsupported ragdoll shape: {shape}")
                created_meshes.append(mesh)
                proxy = bpy.data.objects.new(proxy_name, mesh)
                proxy_collection.objects.link(proxy)
                proxy.matrix_world = bone_world
                proxy.display_type = "WIRE"
                proxy.hide_render = True
                _add_rigid_body(scene, proxy, "ACTIVE")
                _apply_patch(
                    proxy.rigid_body,
                    {
                        "type": "ACTIVE",
                        "collision_shape": collision_shape,
                        "kinematic": bool(start_kinematic),
                        "use_deactivation": True,
                    },
                    _BODY_FIELDS,
                )
                if start_kinematic:
                    driver = proxy.constraints.new("COPY_TRANSFORMS")
                    driver.name = f"{rig_name} Pose Driver"
                    driver.target = armature
                    driver.subtarget = pose_bone.name
                    driver.target_space = "WORLD"
                    driver.owner_space = "WORLD"
                    driver.mix_mode = "REPLACE"
                    proxy["blendermcp_rigid_body_pose_driver"] = driver.name
                proxy.rigid_body.collision_collections = tuple(layer in layers for layer in range(1, 21))
                if world.collection is not None and proxy.name not in world.collection.objects:
                    world.collection.objects.link(proxy)
                volume, _determinant = _mesh_volume(proxy)
                raw_mass[pose_bone.name] = volume * float(body.get("mass_weight", 1.0))
                proxy["blendermcp_rigid_body_rig_id"] = rig_id
                proxy["blendermcp_rigid_body_role"] = "ragdoll_body"
                proxy["blendermcp_rigid_body_source"] = armature.name
                proxy["blendermcp_rigid_body_bone"] = pose_bone.name
                proxy["blendermcp_rigid_body_schema"] = 1
                proxies[pose_bone.name] = proxy
                created_objects.append(proxy)
            raw_total = sum(raw_mass.values())
            for bone_name, proxy in proxies.items():
                mass = total_mass * raw_mass[bone_name] / raw_total
                if not 0.001 <= mass <= 10_000:
                    raise ValueError(f"Mass allocation for '{bone_name}' is outside Blender's [0.001, 10000] range")
                proxy.rigid_body.mass = mass
            created_constraints = []
            for joint, constraint_name in zip(joints, constraint_names, strict=True):
                parent_proxy = proxies[joint["parent_bone_name"]]
                child_proxy = proxies[joint["child_bone_name"]]
                child_bone = armature.pose.bones[joint["child_bone_name"]]
                child_world = _bone_world_matrix(armature, child_bone)
                transform = {"location": tuple(child_world.translation)}
                if joint.get("axis") is not None:
                    transform["axis"] = joint["axis"]
                else:
                    transform["rotation_quaternion"] = tuple(child_world.to_quaternion())
                configuration = dict(joint["configuration"])
                result = self.create_rigid_body_constraint(
                    scene.name,
                    constraint_name,
                    parent_proxy.name,
                    child_proxy.name,
                    transform,
                    configuration,
                    constraint_collection.name,
                    False,
                )
                constraint = bpy.data.objects[result["constraint_object"]]
                constraint["blendermcp_rigid_body_rig_id"] = rig_id
                constraint["blendermcp_rigid_body_role"] = "ragdoll_joint"
                constraint["blendermcp_rigid_body_parent_bone"] = joint["parent_bone_name"]
                constraint["blendermcp_rigid_body_child_bone"] = joint["child_bone_name"]
                constraint["blendermcp_rigid_body_schema"] = 1
                created_objects.append(constraint)
                created_constraints.append(result)
        except Exception:
            for obj in reversed(created_objects):
                with contextlib.suppress(Exception):
                    bpy.data.objects.remove(obj, do_unlink=True)
            for mesh in reversed(created_meshes):
                if mesh.users == 0:
                    with contextlib.suppress(Exception):
                        bpy.data.meshes.remove(mesh)
            raise
        armature["blendermcp_rigid_body_ragdoll_rig_id"] = rig_id
        armature["blendermcp_rigid_body_ragdoll_mapping"] = json.dumps(
            {bone_name: proxy.name for bone_name, proxy in proxies.items()}, sort_keys=True
        )
        geometry = {proxy.name: _evaluated_geometry(proxy) for proxy in proxies.values()}
        overlaps = []
        for first, second in combinations(proxies.values(), 2):
            first_bounds = geometry[first.name]["bounds_world"]
            second_bounds = geometry[second.name]["bounds_world"]
            if first_bounds and second_bounds and _aabb_overlap(first_bounds, second_bounds):
                overlaps.append([first.name, second.name])
        connected = {frozenset(pair) for pair in pairs}
        unconnected_overlaps = [
            pair
            for pair in overlaps
            if frozenset(
                (
                    bpy.data.objects[pair[0]].get("blendermcp_rigid_body_bone"),
                    bpy.data.objects[pair[1]].get("blendermcp_rigid_body_bone"),
                )
            )
            not in connected
        ]
        return {
            "changed_objects": [armature.name, *[obj.name for obj in created_objects]],
            "rig": rig_name,
            "rig_id": rig_id,
            "armature": armature.name,
            "bodies": [
                {
                    "bone": bone_name,
                    "proxy": proxy.name,
                    "rigid_body": _body_info(proxy),
                    "pose_driver": proxy.get("blendermcp_rigid_body_pose_driver"),
                }
                for bone_name, proxy in proxies.items()
            ],
            "constraints": [result["constraint_object"] for result in created_constraints],
            "total_mass": sum(proxy.rigid_body.mass for proxy in proxies.values()),
            "collision_layers": sorted(layers),
            "start_kinematic": bool(start_kinematic),
            "initial_aabb_overlap_candidates": overlaps,
            "unconnected_overlap_candidates": unconnected_overlaps,
            "cache_freed": cache_freed,
            "warnings": [
                "Joint types and anatomical limits came only from the explicit joint specifications.",
                "Use animate_rigid_body_release on mapped proxies for reviewed kinematic handoff timing.",
            ],
        }

    def bake_ragdoll_to_armature(
        self,
        scene_name,
        armature_object_name,
        mappings,
        frame_start,
        frame_end,
        frame_step=1,
        action_name="Ragdoll Bake",
        blend_in_frames=0,
        blend_out_frames=0,
        reduce_keys=False,
        position_tolerance=0.001,
        angular_tolerance_radians=0.001,
        confirm_overwrite_action=False,
    ):
        scene = _scene(scene_name)
        armature = get_object(armature_object_name, {"ARMATURE"})
        if armature.name not in scene.objects:
            raise ValueError(f"Armature '{armature.name}' is not linked to scene '{scene.name}'")
        if frame_start > frame_end or not 1 <= frame_step <= 120:
            raise ValueError("Require frame_start <= frame_end and frame_step in [1, 120]")
        frames = list(range(frame_start, frame_end + 1, frame_step))
        if not frames or len(frames) > 10_000:
            raise ValueError("Ragdoll bake must contain 1-10000 sampled frames")
        if blend_in_frames + blend_out_frames > frame_end - frame_start:
            raise ValueError("Blend intervals must fit inside the bake range")
        if not mappings or len(mappings) > 64:
            raise ValueError("mappings must contain 1-64 entries")
        bone_names = [mapping.get("bone_name") for mapping in mappings]
        proxy_names = [mapping.get("proxy_object_name") for mapping in mappings]
        if len(bone_names) != len(set(bone_names)) or len(proxy_names) != len(set(proxy_names)):
            raise ValueError("Bone and proxy mappings must each be unique")
        missing = [name for name in bone_names if armature.pose.bones.get(name) is None]
        if missing:
            raise ValueError(f"Armature is missing mapped pose bones: {missing}")
        proxies = [get_object(name, {"MESH"}) for name in proxy_names]
        if any(proxy.name not in scene.objects or proxy.rigid_body is None for proxy in proxies):
            raise ValueError("Every ragdoll bake proxy must be a rigid-body mesh in the requested scene")
        existing = bpy.data.actions.get(action_name)
        if existing is not None and not confirm_overwrite_action:
            raise ValueError(f"Action already exists: {action_name}")
        original_frame = scene.frame_current
        original_subframe = scene.frame_subframe
        pose_snapshots = {
            name: (armature.pose.bones[name].matrix_basis.copy(), armature.pose.bones[name].rotation_mode)
            for name in bone_names
        }
        authored = {name: [] for name in bone_names}
        simulated = {name: [] for name in bone_names}
        proxy_by_bone = dict(zip(bone_names, proxies, strict=True))
        try:
            for frame in frames:
                scene.frame_set(frame)
                view_layer = _view_layer_for(scene)
                view_layer.update()
                depsgraph = view_layer.depsgraph
                evaluated_armature = armature.evaluated_get(depsgraph)
                for bone_name in bone_names:
                    authored[bone_name].append(
                        evaluated_armature.matrix_world @ evaluated_armature.pose.bones[bone_name].matrix
                    )
                    simulated[bone_name].append(proxy_by_bone[bone_name].evaluated_get(depsgraph).matrix_world.copy())
        finally:
            scene.frame_set(original_frame, subframe=original_subframe)
            _view_layer_for(scene).update()
        targets = {name: [] for name in bone_names}
        for index, frame in enumerate(frames):
            factor = _blend_factor(frame, frame_start, frame_end, blend_in_frames, blend_out_frames)
            for bone_name in bone_names:
                targets[bone_name].append(
                    _blend_matrix(authored[bone_name][index], simulated[bone_name][index], factor)
                )
        keep_by_bone = {
            name: (
                _reduced_frame_indices(
                    frames,
                    targets[name],
                    position_tolerance,
                    angular_tolerance_radians,
                )
                if reduce_keys and len(frames) > 2
                else set(range(len(frames)))
            )
            for name in bone_names
        }
        animation = armature.animation_data_create()
        original_action = animation.action
        preserved_track = None
        action = existing
        try:
            if original_action is not None and original_action != existing:
                preserved_track = animation.nla_tracks.new()
                preserved_track.name = f"Preserved {original_action.name}"
                preserved_track.strips.new(original_action.name, frame_start, original_action)
            if action is None:
                action = bpy.data.actions.new(action_name)
            else:
                _clear_action_fcurves(action)
            animation.action = action
            ordered_bones = sorted((armature.pose.bones[name] for name in bone_names), key=_bone_depth)
            keyed = {name: [] for name in bone_names}
            for index, frame in enumerate(frames):
                scene.frame_set(frame)
                for pose_bone in ordered_bones:
                    pose_matrix = armature.convert_space(
                        pose_bone=pose_bone,
                        matrix=targets[pose_bone.name][index],
                        from_space="WORLD",
                        to_space="POSE",
                    )
                    pose_bone.rotation_mode = "QUATERNION"
                    pose_bone.matrix = pose_matrix
                    if index not in keep_by_bone[pose_bone.name]:
                        continue
                    for path in ("location", "rotation_quaternion"):
                        if not pose_bone.keyframe_insert(
                            data_path=path,
                            frame=frame,
                            group=f"Ragdoll {pose_bone.name}",
                        ):
                            raise RuntimeError(f"Failed to key pose bone '{pose_bone.name}' at frame {frame}")
                    keyed[pose_bone.name].append(frame)
            verification = []
            fully_simulated = [
                index
                for index, frame in enumerate(frames)
                if _blend_factor(frame, frame_start, frame_end, blend_in_frames, blend_out_frames) >= 1.0
            ]
            for index in fully_simulated[:: max(1, len(fully_simulated) // 5)][:5]:
                scene.frame_set(frames[index])
                view_layer = _view_layer_for(scene)
                view_layer.update()
                depsgraph = view_layer.depsgraph
                evaluated_armature = armature.evaluated_get(depsgraph)
                for bone_name in bone_names:
                    actual = evaluated_armature.matrix_world @ evaluated_armature.pose.bones[bone_name].matrix
                    expected = proxy_by_bone[bone_name].evaluated_get(depsgraph).matrix_world
                    expected_location, expected_rotation, _expected_scale = expected.decompose()
                    actual_location, actual_rotation, _actual_scale = actual.decompose()
                    verification.append(
                        {
                            "frame": frames[index],
                            "bone": bone_name,
                            "position_error": float((actual_location - expected_location).length),
                            "angle_error_radians": abs(
                                float(actual_rotation.rotation_difference(expected_rotation).angle)
                            ),
                        }
                    )
        except Exception:
            animation.action = original_action
            if preserved_track is not None:
                animation.nla_tracks.remove(preserved_track)
            if action is not None and action != original_action and action.users == 0:
                with contextlib.suppress(Exception):
                    bpy.data.actions.remove(action)
            raise
        finally:
            for name, (matrix_basis, rotation_mode) in pose_snapshots.items():
                pose_bone = armature.pose.bones.get(name)
                if pose_bone is not None:
                    pose_bone.matrix_basis = matrix_basis
                    pose_bone.rotation_mode = rotation_mode
            scene.frame_set(original_frame, subframe=original_subframe)
            _view_layer_for(scene).update()
        maximum_position_error = max((item["position_error"] for item in verification), default=0.0)
        maximum_angle_error = max((item["angle_error_radians"] for item in verification), default=0.0)
        return {
            "changed_objects": [armature.name],
            "armature": armature.name,
            "action": action.name,
            "source_action_preserved": original_action.name if original_action else None,
            "mappings": mappings,
            "frames_sampled": frames,
            "keyed_frames_by_bone": keyed,
            "channels": ["location", "rotation_quaternion"],
            "key_reduction": {
                "enabled": bool(reduce_keys),
                "position_tolerance": position_tolerance,
                "angular_tolerance_radians": angular_tolerance_radians,
            },
            "verification": {
                "samples": verification,
                "maximum_position_error": maximum_position_error,
                "maximum_angle_error_radians": maximum_angle_error,
            },
            "timeline_restored": {"frame": scene.frame_current, "subframe": scene.frame_subframe},
        }
