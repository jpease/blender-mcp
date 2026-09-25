# pyright: reportGeneralTypeIssues=false
"""Compound, network, fracture, chain, and animated-collider handlers."""

import contextlib
import math
import uuid

from collections import Counter, deque
from itertools import combinations

import bpy
import mathutils

from ..rna_patch import get_object
from .inspection_and_setup import (
    _BODY_FIELDS,
    _aabb_overlap,
    _add_rigid_body,
    _animation_info,
    _apply_patch,
    _body_info,
    _body_snapshot,
    _bounds,
    _ensure_world,
    _evaluated_geometry,
    _evaluated_mesh_payload,
    _mesh_volume,
    _native_transform,
    _prepare_cache_mutation,
    _remove_rigid_body,
    _restore_fields,
    _scene,
    _validate_object_batch,
)


def _world_location(obj):
    return obj.matrix_world.translation.copy()


def _pair_key(first, second):
    return tuple(sorted((first.name, second.name)))


def _constraint_components(names, pairs):
    neighbors = {name: set() for name in names}
    for first, second in pairs:
        neighbors[first].add(second)
        neighbors[second].add(first)
    components = []
    remaining = set(names)
    while remaining:
        root = min(remaining)
        queue = deque([root])
        component = []
        remaining.remove(root)
        while queue:
            name = queue.popleft()
            component.append(name)
            for neighbor in sorted(neighbors[name]):
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    queue.append(neighbor)
        components.append(component)
    return components


def _generated_pairs(objects, pairing, radius, max_neighbors):
    if pairing == "CHAIN":
        return [(objects[index], objects[index + 1]) for index in range(len(objects) - 1)]
    if pairing == "PARENT":
        available = set(objects)
        return [(obj.parent, obj) for obj in objects if obj.parent in available]
    distances = sorted(
        (
            float((_world_location(first) - _world_location(second)).length),
            first.name,
            second.name,
            first,
            second,
        )
        for first, second in combinations(objects, 2)
    )
    if pairing == "RADIUS":
        return [(first, second) for distance, _a, _b, first, second in distances if distance <= radius]
    degrees = Counter()
    pairs = []
    for _distance, _a, _b, first, second in distances:
        if degrees[first.name] >= max_neighbors or degrees[second.name] >= max_neighbors:
            continue
        pairs.append((first, second))
        degrees[first.name] += 1
        degrees[second.name] += 1
    return pairs


class RigidBodyAssemblyHandlers:
    """Build validated multi-object rigid-body mechanisms and collider assemblies."""

    def create_compound_rigid_body(
        self,
        scene_name,
        root_object_name,
        child_object_names,
        render_object_name=None,
        total_mass=None,
        child_collision_shape="CONVEX_HULL",
        confirm_delete_baked_cache=False,
    ):
        scene = _scene(scene_name)
        if not child_object_names or len(child_object_names) > 128:
            raise ValueError("child_object_names must contain 1-128 names")
        names = [root_object_name, *child_object_names]
        if len(set(names)) != len(names):
            raise ValueError("Compound root and children must be unique")
        root, *children = _validate_object_batch(scene, names)
        if any(obj.type != "MESH" for obj in [root, *children]):
            raise ValueError("Compound roots and children must be mesh objects")
        render = get_object(render_object_name) if render_object_name else None
        if render is not None and (render.name not in scene.objects or render in [root, *children]):
            raise ValueError("render_object_name must identify a separate object in the scene")
        if any(child.parent is not None and child.parent != root for child in children):
            conflicts = [child.name for child in children if child.parent is not None and child.parent != root]
            raise ValueError(f"Compound children already belong to another hierarchy: {conflicts}")
        ownership_conflicts = [
            child.name
            for child in children
            if child.get("blendermcp_rigid_body_role") == "compound_child"
            and child.get("blendermcp_rigid_body_source") != root.name
        ]
        if ownership_conflicts:
            raise ValueError(f"Compound children already belong to another rigid-body root: {ownership_conflicts}")
        if any(root in child.children_recursive for child in children):
            raise ValueError("Compound hierarchy would create a parenting cycle")
        if child_collision_shape not in {"BOX", "SPHERE", "CAPSULE", "CYLINDER", "CONE", "CONVEX_HULL"}:
            raise ValueError("Compound children require a primitive or convex collision shape")
        if total_mass is not None and (not math.isfinite(total_mass) or total_mass < 0.001):
            raise ValueError("total_mass must be finite and at least 0.001")
        world = _ensure_world(scene)
        cache_freed = _prepare_cache_mutation(scene, world, confirm_delete_baked_cache)
        snapshots = {
            obj.name: (_body_snapshot(obj.rigid_body) if obj.rigid_body else None) for obj in [root, *children]
        }
        parents = {
            child.name: (child.parent, child.matrix_parent_inverse.copy(), child.matrix_world.copy())
            for child in children
        }
        created_bodies = []
        rig_id = uuid.uuid4().hex
        try:
            for obj in [root, *children]:
                if obj.rigid_body is None:
                    _add_rigid_body(scene, obj, "ACTIVE")
                    created_bodies.append(obj)
            _apply_patch(root.rigid_body, {"type": "ACTIVE", "collision_shape": "COMPOUND"}, _BODY_FIELDS)
            if total_mass is not None:
                root.rigid_body.mass = total_mass
            for child in children:
                child_world = child.matrix_world.copy()
                child.parent = root
                child.matrix_world = child_world
                _apply_patch(
                    child.rigid_body,
                    {"type": "ACTIVE", "collision_shape": child_collision_shape},
                    _BODY_FIELDS,
                )
                child["blendermcp_rigid_body_rig_id"] = rig_id
                child["blendermcp_rigid_body_role"] = "compound_child"
                child["blendermcp_rigid_body_source"] = root.name
                child["blendermcp_rigid_body_schema"] = 1
            root["blendermcp_rigid_body_rig_id"] = rig_id
            root["blendermcp_rigid_body_role"] = "compound_root"
            root["blendermcp_rigid_body_schema"] = 1
            if render is not None:
                render_world = render.matrix_world.copy()
                render.parent = root
                render.matrix_world = render_world
                render["blendermcp_rigid_body_rig_id"] = rig_id
                render["blendermcp_rigid_body_role"] = "compound_render"
                render["blendermcp_rigid_body_schema"] = 1
        except Exception:
            for child in children:
                parent, inverse, matrix = parents[child.name]
                child.parent = parent
                child.matrix_parent_inverse = inverse
                child.matrix_world = matrix
            for obj in reversed(created_bodies):
                with contextlib.suppress(Exception):
                    _remove_rigid_body(scene, obj)
            for obj in [root, *children]:
                snapshot = snapshots[obj.name]
                if snapshot and obj.rigid_body:
                    layers = snapshot.pop("collision_collections")
                    _restore_fields(obj.rigid_body, snapshot)
                    obj.rigid_body.collision_collections = layers
            raise
        bounds = []
        for child in children:
            vertices, _faces = _evaluated_mesh_payload(child)
            child_bounds = _bounds([child.matrix_world @ vertex for vertex in vertices])
            if child_bounds:
                bounds.append(child_bounds)
        return {
            "changed_objects": [root.name, *[child.name for child in children], *([render.name] if render else [])],
            "rig_id": rig_id,
            "root": root.name,
            "children": [child.name for child in children],
            "render_object": render.name if render else None,
            "root_rigid_body": _body_info(root),
            "child_collision_shape": child_collision_shape,
            "child_world_bounds": bounds,
            "cache_freed": cache_freed,
            "contract": "Blender COMPOUND combines direct rigid-body children into the root rigid object.",
        }

    def create_rigid_body_constraint_network(
        self,
        scene_name,
        network_name,
        body_names,
        configuration,
        edges=None,
        pairing="EXPLICIT",
        radius=None,
        max_neighbors=4,
        collection_name=None,
        confirm_delete_baked_cache=False,
    ):
        scene = _scene(scene_name)
        objects = _validate_object_batch(scene, body_names, require_body=True)
        if len(objects) < 2 or len(objects) > 256:
            raise ValueError("body_names must contain 2-256 objects")
        by_name = {obj.name: obj for obj in objects}
        if pairing == "EXPLICIT":
            if not edges:
                raise ValueError("EXPLICIT pairing requires edges")
            resolved = []
            explicit_names = []
            explicit_locations = {}
            for edge in edges:
                first = by_name.get(edge["object1_name"])
                second = by_name.get(edge["object2_name"])
                if first is None or second is None or first == second:
                    raise ValueError(f"Invalid explicit constraint edge: {edge}")
                resolved.append((first, second))
                key = _pair_key(first, second)
                explicit_names.append(edge.get("name"))
                explicit_locations[key] = edge.get("location")
        else:
            if pairing not in {"CHAIN", "NEAREST", "RADIUS", "PARENT"}:
                raise ValueError(f"Unsupported pairing: {pairing}")
            if pairing == "RADIUS" and (radius is None or radius <= 0):
                raise ValueError("RADIUS pairing requires positive radius")
            resolved = _generated_pairs(objects, pairing, radius, max_neighbors)
            explicit_names = [None] * len(resolved)
            explicit_locations = {}
        keys = [_pair_key(first, second) for first, second in resolved]
        if len(keys) != len(set(keys)):
            raise ValueError("Constraint network contains duplicate body pairs")
        if not resolved:
            raise ValueError("Pairing produced no constraint edges")
        if len(resolved) > 512:
            raise ValueError("Constraint network exceeds the 512-edge limit")
        degree = Counter(name for pair in keys for name in pair)
        if max(degree.values(), default=0) > max_neighbors and pairing != "CHAIN":
            raise ValueError(f"Constraint network exceeds max_neighbors={max_neighbors}")
        constraint_names = [
            explicit_name or f"{network_name} {index:03d}" for index, explicit_name in enumerate(explicit_names, 1)
        ]
        if len(set(constraint_names)) != len(constraint_names):
            raise ValueError("Constraint object names must be unique")
        conflicts = [name for name in constraint_names if bpy.data.objects.get(name) is not None]
        if conflicts:
            raise ValueError(f"Constraint objects already exist: {conflicts}")
        world = _ensure_world(scene)
        cache_freed = _prepare_cache_mutation(scene, world, confirm_delete_baked_cache)
        rig_id = uuid.uuid4().hex
        created = []
        for index, ((first, second), explicit_name) in enumerate(zip(resolved, explicit_names, strict=True), 1):
            key = _pair_key(first, second)
            name = explicit_name or f"{network_name} {index:03d}"
            location = explicit_locations.get(key)
            if location is None:
                location = tuple((_world_location(first) + _world_location(second)) * 0.5)
            result = self.create_rigid_body_constraint(
                scene.name,
                name,
                first.name,
                second.name,
                {"location": location},
                configuration,
                collection_name,
                False,
            )
            constraint = bpy.data.objects[result["constraint_object"]]
            constraint["blendermcp_rigid_body_rig_id"] = rig_id
            constraint["blendermcp_rigid_body_role"] = "constraint_network_edge"
            constraint["blendermcp_rigid_body_network"] = network_name
            constraint["blendermcp_rigid_body_schema"] = 1
            created.append(result)
        return {
            "changed_objects": [item["constraint_object"] for item in created],
            "network": network_name,
            "rig_id": rig_id,
            "nodes": body_names,
            "edges": [
                {"constraint": item["constraint_object"], "object1": first.name, "object2": second.name}
                for item, (first, second) in zip(created, resolved, strict=True)
            ],
            "connected_components": _constraint_components(body_names, keys),
            "degree": dict(sorted(degree.items())),
            "cache_freed": cache_freed,
        }

    def prepare_fracture_rigid_bodies(
        self,
        scene_name,
        piece_object_names,
        density,
        collision_shape="CONVEX_HULL",
        use_deactivation=True,
        collision_margin=0.001,
        bond_distance=None,
        breaking_threshold=None,
        constraint_collection_name="Rigid Body Fracture Bonds",
        confirm_delete_baked_cache=False,
    ):
        scene = _scene(scene_name)
        pieces = _validate_object_batch(scene, piece_object_names)
        if len(pieces) < 2 or any(obj.type != "MESH" for obj in pieces):
            raise ValueError("Fracture preparation requires at least two mesh objects")
        volumes = []
        for obj in pieces:
            volume, determinant = _mesh_volume(obj)
            volumes.append((obj, volume, determinant))
        settings = {
            "collision_shape": collision_shape,
            "use_deactivation": bool(use_deactivation),
            "use_margin": True,
            "collision_margin": collision_margin,
        }
        existing = [obj.name for obj in pieces if obj.rigid_body is not None]
        self.add_rigid_bodies(
            scene.name,
            piece_object_names,
            "ACTIVE",
            settings=settings,
            existing_policy="REUSE",
            confirm_delete_baked_cache=confirm_delete_baked_cache,
        )
        assignments = [{"object_name": obj.name, "density": density} for obj in pieces]
        mass_result = self.set_rigid_body_mass(scene.name, assignments, confirm_delete_baked_cache=False)
        rig_id = uuid.uuid4().hex
        for obj in pieces:
            obj["blendermcp_rigid_body_rig_id"] = rig_id
            obj["blendermcp_rigid_body_role"] = "fracture_piece"
            obj["blendermcp_rigid_body_schema"] = 1
        constraints = []
        if bond_distance is not None:
            edges = []
            for first, second in combinations(pieces, 2):
                if (_world_location(first) - _world_location(second)).length <= bond_distance:
                    edges.append({"object1_name": first.name, "object2_name": second.name})
            if edges:
                config = {
                    "type": "FIXED",
                    "use_breaking": True,
                    "breaking_threshold": breaking_threshold,
                    "disable_collisions": False,
                }
                network = self.create_rigid_body_constraint_network(
                    scene.name,
                    f"Fracture {rig_id[:8]}",
                    piece_object_names,
                    config,
                    edges=edges,
                    pairing="EXPLICIT",
                    max_neighbors=32,
                    collection_name=constraint_collection_name,
                )
                constraints = network["edges"]
        sizes = [max(obj.dimensions) for obj in pieces]
        evaluated_bounds = {obj.name: _evaluated_geometry(obj)["bounds_world"] for obj in pieces}
        overlaps = []
        for first, second in combinations(pieces, 2):
            first_bounds = evaluated_bounds[first.name]
            second_bounds = evaluated_bounds[second.name]
            if first_bounds is not None and second_bounds is not None and _aabb_overlap(first_bounds, second_bounds):
                overlaps.append([first.name, second.name])
        return {
            "changed_objects": [*piece_object_names, *[item["constraint"] for item in constraints]],
            "rig_id": rig_id,
            "pieces": piece_object_names,
            "previously_configured": existing,
            "total_mass": sum(record["new_mass"] for record in mass_result["assignments"]),
            "piece_size_world": {"minimum": min(sizes), "maximum": max(sizes), "mean": sum(sizes) / len(sizes)},
            "piece_volumes_world": {obj.name: volume for obj, volume, _determinant in volumes},
            "initial_aabb_overlap_candidates": overlaps,
            "constraints": constraints,
            "warnings": ["This tool prepared existing pieces; it did not fracture source geometry."],
        }

    def create_rigid_body_chain(
        self,
        scene_name,
        chain_name,
        body_names,
        configuration,
        axis=(0.0, 0.0, 1.0),
        start_anchor_name=None,
        end_anchor_name=None,
        collection_name=None,
        confirm_delete_baked_cache=False,
    ):
        scene = _scene(scene_name)
        ordered_names = [*body_names]
        for anchor in (start_anchor_name, end_anchor_name):
            if anchor:
                obj = get_object(anchor)
                if obj.name not in scene.objects or obj.rigid_body is None or obj.rigid_body.type != "PASSIVE":
                    raise ValueError(f"Anchor '{anchor}' must be a passive rigid body in scene '{scene.name}'")
        nodes = [
            *([start_anchor_name] if start_anchor_name else []),
            *ordered_names,
            *([end_anchor_name] if end_anchor_name else []),
        ]
        if len(set(nodes)) != len(nodes):
            raise ValueError("Chain bodies and anchors must be unique")
        objects = _validate_object_batch(scene, nodes, require_body=True)
        axis_vector = mathutils.Vector(axis)
        if axis_vector.length <= 1e-12:
            raise ValueError("axis must be non-zero")
        edges = []
        for first, second in zip(objects, objects[1:], strict=False):
            edges.append(
                {
                    "object1_name": first.name,
                    "object2_name": second.name,
                    "location": tuple((_world_location(first) + _world_location(second)) * 0.5),
                }
            )
        result = self.create_rigid_body_constraint_network(
            scene.name,
            chain_name,
            nodes,
            configuration,
            edges=edges,
            pairing="EXPLICIT",
            max_neighbors=2,
            collection_name=collection_name,
            confirm_delete_baked_cache=confirm_delete_baked_cache,
        )
        for edge in result["edges"]:
            constraint = bpy.data.objects[edge["constraint"]]
            local_axis = mathutils.Vector((0.0, 0.0, 1.0) if configuration["type"] == "HINGE" else (1.0, 0.0, 0.0))
            constraint.rotation_mode = "QUATERNION"
            constraint.rotation_quaternion = local_axis.rotation_difference(axis_vector.normalized())
        masses = [obj.rigid_body.mass for obj in objects if obj.rigid_body.type == "ACTIVE"]
        sizes = [max(obj.dimensions) for obj in objects]
        ratio = max(masses) / min(masses) if masses else 1.0
        warnings = []
        if ratio > 10:
            warnings.append(f"Chain active-body mass ratio is {ratio:.3g}; consider a ratio below 10 for stability.")
        if min(sizes) > 0 and max(sizes) / min(sizes) > 10:
            warnings.append("Chain link size ratio exceeds 10; additional solver substeps may be needed.")
        return {
            **result,
            "chain": chain_name,
            "ordered_nodes": nodes,
            "axis_world": list(axis_vector.normalized()),
            "warnings": warnings,
        }

    def setup_animated_passive_collider(
        self,
        scene_name,
        object_name,
        collision_shape,
        mesh_source="FINAL",
        use_deform=False,
        sample_frames=None,
        maximum_evaluated_faces=100000,
        confirm_delete_baked_cache=False,
    ):
        scene = _scene(scene_name)
        obj = _validate_object_batch(scene, [object_name])[0]
        if obj.type != "MESH":
            raise ValueError("Animated passive colliders must be mesh objects")
        if use_deform and collision_shape != "MESH":
            raise ValueError("use_deform=True requires collision_shape='MESH'")
        frames = list(sample_frames or [])
        if len(frames) > 32 or len(set(frames)) != len(frames):
            raise ValueError("sample_frames must contain at most 32 unique frames")
        original_frame = scene.frame_current
        original_subframe = scene.frame_subframe
        samples = []
        try:
            for frame in sorted(frames):
                scene.frame_set(frame)
                bpy.context.view_layer.update()
                evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
                mesh = evaluated.to_mesh()
                try:
                    if len(mesh.polygons) > maximum_evaluated_faces:
                        raise ValueError(
                            f"Evaluated collider has {len(mesh.polygons)} faces at frame {frame}, "
                            f"exceeding maximum_evaluated_faces={maximum_evaluated_faces}"
                        )
                    samples.append(
                        {
                            "frame": frame,
                            "matrix_world": [list(row) for row in evaluated.matrix_world],
                            "faces": len(mesh.polygons),
                        }
                    )
                finally:
                    evaluated.to_mesh_clear()
        finally:
            scene.frame_set(original_frame, subframe=original_subframe)
            bpy.context.view_layer.update()
        self.add_rigid_bodies(
            scene.name,
            [obj.name],
            "PASSIVE",
            settings={
                "kinematic": True,
                "collision_shape": collision_shape,
                "mesh_source": mesh_source,
                "use_deform": use_deform,
            },
            existing_policy="REUSE",
            confirm_delete_baked_cache=confirm_delete_baked_cache,
        )
        teleports = []
        for previous, current in zip(samples, samples[1:], strict=False):
            before = mathutils.Matrix(previous["matrix_world"]).translation
            after = mathutils.Matrix(current["matrix_world"]).translation
            distance = float((after - before).length)
            if distance > max(obj.dimensions):
                teleports.append({"from_frame": previous["frame"], "to_frame": current["frame"], "distance": distance})
        warnings = []
        if use_deform:
            warnings.append(
                "Deforming MESH collision is expensive; prefer a primitive or convex proxy when topology is rigid."
            )
        return {
            "changed_objects": [obj.name],
            "object": obj.name,
            "rigid_body": _body_info(obj),
            "animation": _animation_info(obj),
            "transform": _native_transform(obj),
            "samples": samples,
            "teleport_warnings": teleports,
            "timeline_restored": {"frame": scene.frame_current, "subframe": scene.frame_subframe},
            "warnings": warnings,
        }
