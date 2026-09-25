"""Blender-main-thread handlers for cloth sewing, pressure, and internal-spring dynamics."""

from __future__ import annotations

import contextlib
import math
import statistics

from collections import Counter
from itertools import pairwise

import bmesh
import bpy

from ...helpers import preserve_mode_and_selection, set_active, sync_from_editmode
from ..rna_patch import finite, patch_rna, restore_rna, validate_rna_value
from ..simulation_cache import set_cache_frame_range
from .inspection_and_setup import (
    _DEFORMING_MODIFIERS,
    _TOPOLOGY_MODIFIERS,
    _animation_info,
    _cache_info,
    _collection_in_scene,
    _field_relationships,
    _get_cloth,
    _modifier_is_animated,
    _object_scenes,
    _reject_baked,
    _tag_update,
)

_PRESSURE_FIELDS = {
    "use_pressure",
    "uniform_pressure_force",
    "use_pressure_volume",
    "target_volume",
    "pressure_factor",
    "fluid_density",
    "vertex_group_pressure",
}
_INTERNAL_SPRING_FIELDS = {
    "use_internal_springs",
    "internal_spring_max_length",
    "internal_spring_max_diversion",
    "internal_spring_normal_check",
    "internal_tension_stiffness",
    "internal_compression_stiffness",
    "internal_tension_stiffness_max",
    "internal_compression_stiffness_max",
    "internal_friction",
    "vertex_group_intern",
}
_FIELD_WEIGHT_FIELDS = {
    "all",
    "gravity",
    "force",
    "vortex",
    "magnetic",
    "wind",
    "curve_guide",
    "texture",
    "harmonic",
    "charge",
    "lennardjones",
    "turbulence",
    "drag",
    "boid",
    "smokeflow",
    "apply_to_hair_growing",
}


def _sewing_plan(obj, seam_pairs, max_pair_distance):
    if not seam_pairs or len(seam_pairs) > 5_000:
        raise ValueError("seam_pairs must contain 1-5000 explicit pairs")
    vertex_count = len(obj.data.vertices)
    edge_face_uses = Counter()
    for polygon in obj.data.polygons:
        vertices = list(polygon.vertices)
        for index, first in enumerate(vertices):
            second = vertices[(index + 1) % len(vertices)]
            edge_face_uses[tuple(sorted((first, second)))] += 1
    boundary_vertices = {vertex for edge, uses in edge_face_uses.items() if uses == 1 for vertex in edge}
    mesh_edges = {}
    for edge in obj.data.edges:
        mesh_edges.setdefault(tuple(sorted(edge.vertices)), []).append(edge.index)
    seen = set()
    endpoint_uses = Counter()
    records = []
    connector_vectors = []
    for pair_index, pair in enumerate(seam_pairs):
        first = int(pair["source_vertex"])
        second = int(pair["target_vertex"])
        if first == second:
            raise ValueError(f"Sewing pair {pair_index} repeats vertex {first}")
        if not 0 <= first < vertex_count or not 0 <= second < vertex_count:
            raise ValueError(f"Sewing pair {pair_index} contains an index outside [0, {vertex_count - 1}]")
        key = tuple(sorted((first, second)))
        if key in seen:
            raise ValueError(f"Duplicate sewing pair for vertices {list(key)}")
        seen.add(key)
        endpoint_uses.update((first, second))
        distance = float((obj.data.vertices[first].co - obj.data.vertices[second].co).length)
        if max_pair_distance is not None and distance > max_pair_distance:
            raise ValueError(
                f"Sewing pair {pair_index} distance {distance:g} exceeds max_pair_distance {max_pair_distance:g}"
            )
        face_uses = edge_face_uses.get(key, 0)
        if face_uses:
            raise ValueError(f"Edge {list(key)} belongs to {face_uses} face(s) and is not a loose sewing edge")
        connector_vectors.append(obj.data.vertices[second].co - obj.data.vertices[first].co)
        records.append(
            {
                "pair_index": pair_index,
                "vertices": [first, second],
                "distance_object_local": distance,
                "source_is_boundary": first in boundary_vertices,
                "target_is_boundary": second in boundary_vertices,
                "existing_loose_edge": key in mesh_edges,
                "edge_indices": mesh_edges.get(key, []),
                "duplicate_existing_edges": len(mesh_edges.get(key, [])) > 1,
            }
        )
    reversals = []
    for index, (previous, current) in enumerate(pairwise(connector_vectors), start=1):
        if previous.length_squared and current.length_squared and previous.dot(current) < 0:
            reversals.append(index)
    distances = [record["distance_object_local"] for record in records]
    unused_boundary_vertices = sorted(boundary_vertices - endpoint_uses.keys())
    return {
        "pairs": records,
        "existing_loose_edges": sum(record["existing_loose_edge"] for record in records),
        "missing_loose_edges": sum(not record["existing_loose_edge"] for record in records),
        "duplicate_requested_mesh_edges": sum(record["duplicate_existing_edges"] for record in records),
        "non_boundary_endpoints": sum(
            not record["source_is_boundary"] or not record["target_is_boundary"] for record in records
        ),
        "boundary_vertices": len(boundary_vertices),
        "unmatched_boundary_vertices": len(unused_boundary_vertices),
        "unmatched_boundary_vertex_sample": unused_boundary_vertices[:100],
        "unmatched_boundary_vertices_truncated": len(unused_boundary_vertices) > 100,
        "multiply_mapped_boundary_vertices": sorted(vertex for vertex, uses in endpoint_uses.items() if uses > 1)[:100],
        "pair_distance": {
            "minimum": min(distances),
            "maximum": max(distances),
            "mean": statistics.fmean(distances),
        },
        "direction_reversal_pair_indices": reversals,
        "likely_fold": bool(reversals),
    }


def _set_loose_edges(obj, vertex_pairs, *, create):
    created_keys = []
    with preserve_mode_and_selection():
        set_active(obj)
        if obj.mode != "OBJECT":
            result = bpy.ops.object.mode_set(mode="OBJECT")
            if "FINISHED" not in result:
                raise RuntimeError(f"Could not enter Object Mode for sewing topology: {sorted(result)}")
        mesh = obj.data
        bm = bmesh.new()
        try:
            bm.from_mesh(mesh)
            bm.verts.ensure_lookup_table()
            for first, second in vertex_pairs:
                first_vertex = bm.verts[first]
                second_vertex = bm.verts[second]
                if first_vertex is None or second_vertex is None:
                    raise RuntimeError("BMesh vertex lookup failed after validated sewing preflight")
                vertices = (first_vertex, second_vertex)
                if bm.edges.get(vertices) is None:
                    if not create:
                        continue
                    bm.edges.new(vertices)
                    created_keys.append(tuple(sorted((first, second))))
            if created_keys:
                bm.to_mesh(mesh)
                mesh.update()
        finally:
            bm.free()
    return created_keys


def _remove_edges_by_vertices(obj, vertex_pairs):
    if not vertex_pairs:
        return
    with preserve_mode_and_selection():
        set_active(obj)
        if obj.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        bm = bmesh.new()
        try:
            bm.from_mesh(obj.data)
            bm.verts.ensure_lookup_table()
            edges = []
            for first, second in vertex_pairs:
                first_vertex = bm.verts[first]
                second_vertex = bm.verts[second]
                if first_vertex is not None and second_vertex is not None:
                    edges.append(bm.edges.get((first_vertex, second_vertex)))
            bmesh.ops.delete(bm, geom=[edge for edge in edges if edge is not None], context="EDGES")
            bm.to_mesh(obj.data)
            obj.data.update()
        finally:
            bm.free()


def _surface_report(obj):
    if not obj.data.vertices or not obj.data.polygons:
        raise ValueError(f"Mesh '{obj.name}' must contain vertices and faces")
    edge_uses = Counter()
    directed = Counter()
    signed_volume = 0.0
    center = sum((vertex.co for vertex in obj.data.vertices), obj.data.vertices[0].co * 0.0) / max(
        len(obj.data.vertices), 1
    )
    inward_faces = []
    for polygon in obj.data.polygons:
        vertices = list(polygon.vertices)
        for index, first in enumerate(vertices):
            second = vertices[(index + 1) % len(vertices)]
            edge_uses[tuple(sorted((first, second)))] += 1
            directed[first, second] += 1
        if len(vertices) >= 3:
            origin = obj.data.vertices[vertices[0]].co
            for index in range(1, len(vertices) - 1):
                second = obj.data.vertices[vertices[index]].co
                third = obj.data.vertices[vertices[index + 1]].co
                signed_volume += float(origin.dot(second.cross(third))) / 6.0
        if polygon.area > 1e-12 and polygon.normal.dot(polygon.center - center) < 0:
            inward_faces.append(polygon.index)
    loose_edges = sum(edge_uses.get(tuple(sorted(edge.vertices)), 0) == 0 for edge in obj.data.edges)
    inconsistent = sum(
        directed[second, first] == 0 for first, second in directed if edge_uses[tuple(sorted((first, second)))] == 2
    )
    return {
        "signed_volume_object_local_cubed": signed_volume,
        "absolute_volume_object_local_cubed": abs(signed_volume),
        "boundary_edges": sum(count == 1 for count in edge_uses.values()),
        "non_manifold_edges": sum(count != 2 for count in edge_uses.values()) + loose_edges,
        "loose_edges": loose_edges,
        "inconsistent_winding_edges": inconsistent,
        "inward_face_candidates": inward_faces[:100],
        "inward_face_candidates_truncated": len(inward_faces) > 100,
        "orientation_evidence": "POSITIVE_SIGNED_VOLUME" if signed_volume > 0 else "NON_POSITIVE_SIGNED_VOLUME",
    }


class ClothDynamicsHandlers:
    """Blender-main-thread handlers for cloth sewing, pressure, and internal-spring dynamics."""

    def configure_cloth_sewing(
        self,
        object_name,
        modifier_name,
        seam_pairs,
        sewing_force_max,
        create_missing_edges=False,
        dry_run=True,
        max_pair_distance=None,
    ):
        obj, modifier = _get_cloth(object_name, modifier_name)
        sync_from_editmode(obj)
        if max_pair_distance is not None:
            finite(max_pair_distance, "max_pair_distance")
            if max_pair_distance <= 0:
                raise ValueError("max_pair_distance must be positive")
        validate_rna_value(modifier.settings, "sewing_force_max", sewing_force_max)
        plan = _sewing_plan(obj, seam_pairs, max_pair_distance)
        if plan["non_boundary_endpoints"]:
            raise ValueError(
                f"{plan['non_boundary_endpoints']} sewing pair(s) contain endpoints outside panel boundaries"
            )
        if dry_run:
            return {
                "changed_objects": [],
                "object": obj.name,
                "modifier": modifier.name,
                "dry_run": True,
                "would_create_edges": plan["missing_loose_edges"] if create_missing_edges else 0,
                "analysis": plan,
                "point_cache": _cache_info(modifier.point_cache),
            }
        _reject_baked([(obj, modifier)])
        if plan["duplicate_requested_mesh_edges"]:
            raise ValueError(
                f"{plan['duplicate_requested_mesh_edges']} requested seam pair(s) already have duplicate mesh edges"
            )
        if plan["missing_loose_edges"] and not create_missing_edges:
            raise ValueError(
                f"{plan['missing_loose_edges']} requested sewing edges do not exist; "
                "set create_missing_edges=True or provide existing loose edges"
            )
        old_settings = {
            "use_sewing_springs": modifier.settings.use_sewing_springs,
            "sewing_force_max": modifier.settings.sewing_force_max,
        }
        missing_pairs = [tuple(record["vertices"]) for record in plan["pairs"] if not record["existing_loose_edge"]]
        created_edges = []
        try:
            created_edges = _set_loose_edges(obj, missing_pairs, create=create_missing_edges)
            modifier.settings.use_sewing_springs = True
            modifier.settings.sewing_force_max = sewing_force_max
            _tag_update(obj)
        except Exception:
            with contextlib.suppress(Exception):
                _remove_edges_by_vertices(obj, created_edges)
            for name, value in old_settings.items():
                with contextlib.suppress(Exception):
                    setattr(modifier.settings, name, value)
            raise
        updated = _sewing_plan(obj, seam_pairs, max_pair_distance)
        return {
            "changed_objects": [obj.name],
            "object": obj.name,
            "modifier": modifier.name,
            "dry_run": False,
            "settings": {
                "use_sewing_springs": {"old": old_settings["use_sewing_springs"], "new": True},
                "sewing_force_max": {
                    "old": old_settings["sewing_force_max"],
                    "new": modifier.settings.sewing_force_max,
                },
            },
            "created_edges": [list(pair) for pair in created_edges],
            "analysis": updated,
            "point_cache": _cache_info(modifier.point_cache),
            "warnings": ["Topology changed; query get_mesh_data again before reusing any mesh indices."]
            if created_edges
            else ["Sewing settings changed and invalidate unbaked simulation state."],
        }

    def configure_cloth_pressure(self, object_name, modifier_name, patch):
        obj, modifier = _get_cloth(object_name, modifier_name)
        sync_from_editmode(obj)
        _reject_baked([(obj, modifier)])
        if not patch:
            raise ValueError("Pressure patch cannot be empty")
        group_name = patch.get("vertex_group_pressure")
        if group_name and obj.vertex_groups.get(group_name) is None:
            raise ValueError(f"Vertex group not found: {group_name}")
        report = _surface_report(obj)
        enabling = patch.get("use_pressure", modifier.settings.use_pressure)
        volume_control = patch.get("use_pressure_volume", modifier.settings.use_pressure_volume)
        target_volume = patch.get("target_volume", modifier.settings.target_volume)
        if enabling:
            if report["non_manifold_edges"]:
                raise ValueError("Pressure requires a closed manifold mesh with no boundary or loose edges")
            if report["inconsistent_winding_edges"]:
                raise ValueError("Pressure requires consistently oriented faces")
            if report["signed_volume_object_local_cubed"] <= 1e-12:
                raise ValueError("Pressure requires outward orientation and positive nonzero signed volume")
            if volume_control and target_volume <= 0:
                raise ValueError("Pressure volume control requires a positive target_volume")
        changes = patch_rna(modifier.settings, patch, _PRESSURE_FIELDS)
        try:
            _tag_update(obj)
        except Exception:
            restore_rna(modifier.settings, changes)
            raise
        return {
            "changed_objects": [obj.name],
            "object": obj.name,
            "modifier": modifier.name,
            "changes": changes,
            "surface": report,
            "material_relationship": {
                "tension_stiffness": modifier.settings.tension_stiffness,
                "compression_stiffness": modifier.settings.compression_stiffness,
                "bending_stiffness": modifier.settings.bending_stiffness,
                "pressure_factor": modifier.settings.pressure_factor,
                "uniform_pressure_force": modifier.settings.uniform_pressure_force,
            },
            "point_cache": _cache_info(modifier.point_cache),
            "warnings": ["Pressure settings changed and invalidate unbaked simulation state."],
        }

    def configure_cloth_internal_springs(
        self,
        object_name,
        modifier_name,
        patch,
        max_estimated_springs=2_000_000,
    ):
        obj, modifier = _get_cloth(object_name, modifier_name)
        sync_from_editmode(obj)
        _reject_baked([(obj, modifier)])
        if not patch:
            raise ValueError("Internal-spring patch cannot be empty")
        if not 1 <= max_estimated_springs <= 20_000_000:
            raise ValueError("max_estimated_springs must be in [1, 20000000]")
        group_name = patch.get("vertex_group_intern")
        if group_name and obj.vertex_groups.get(group_name) is None:
            raise ValueError(f"Vertex group not found: {group_name}")
        report = _surface_report(obj)
        enabling = patch.get("use_internal_springs", modifier.settings.use_internal_springs)
        vertex_count = len(obj.data.vertices)
        all_pairs = vertex_count * max(vertex_count - 1, 0) // 2
        max_length = patch.get("internal_spring_max_length", modifier.settings.internal_spring_max_length)
        finite(max_length, "internal_spring_max_length")
        coordinates = [vertex.co for vertex in obj.data.vertices]
        extents = [max(axis) - min(axis) for axis in zip(*coordinates, strict=False)] if coordinates else [0.0] * 3
        bounds_volume = math.prod(max(float(extent), 1e-12) for extent in extents)
        if max_length > 0 and vertex_count:
            local_density = vertex_count / bounds_volume
            neighborhood = local_density * (4.0 / 3.0) * math.pi * max_length**3
            estimated_pairs = min(all_pairs, math.ceil(vertex_count * neighborhood * 0.5))
        else:
            estimated_pairs = all_pairs
        if enabling:
            if report["non_manifold_edges"] or report["inconsistent_winding_edges"]:
                raise ValueError("Internal springs require closed, consistently oriented volumetric geometry")
            if estimated_pairs > max_estimated_springs:
                raise ValueError(
                    f"Estimated internal-spring candidates {estimated_pairs} exceed "
                    f"max_estimated_springs {max_estimated_springs}; reduce density or maximum length"
                )
        changes = patch_rna(modifier.settings, patch, _INTERNAL_SPRING_FIELDS)
        try:
            _tag_update(obj)
        except Exception:
            restore_rna(modifier.settings, changes)
            raise
        return {
            "changed_objects": [obj.name],
            "object": obj.name,
            "modifier": modifier.name,
            "changes": changes,
            "surface": report,
            "spring_estimate": {
                "vertices": vertex_count,
                "absolute_pair_upper_bound": all_pairs,
                "density_length_estimate": estimated_pairs,
                "maximum_length_object_local": max_length,
                "accepted_limit": max_estimated_springs,
                "estimate_only": True,
            },
            "point_cache": _cache_info(modifier.point_cache),
            "warnings": ["Internal-spring settings changed and invalidate unbaked simulation state."],
        }

    def configure_cloth_rest_shape(
        self,
        object_name,
        modifier_name,
        shape_key_name,
        use_dynamic_mesh,
        cache_frame_start,
        cache_frame_end,
    ):
        obj, modifier = _get_cloth(object_name, modifier_name)
        sync_from_editmode(obj)
        _reject_baked([(obj, modifier)])
        shape_keys = getattr(obj.data, "shape_keys", None)
        if shape_keys is None or shape_keys.reference_key is None:
            raise ValueError(f"Mesh '{obj.name}' has no Basis shape key")
        shape_key = shape_keys.key_blocks.get(shape_key_name)
        if shape_key is None:
            raise ValueError(f"Shape key not found: {shape_key_name}")
        if shape_key == shape_keys.reference_key:
            raise ValueError("Choose a non-Basis shape key as the cloth rest shape")
        if len(shape_key.data) != len(obj.data.vertices):
            raise ValueError("Rest shape key vertex count does not match the base mesh")
        if cache_frame_start > cache_frame_end:
            raise ValueError("cache_frame_start must be <= cache_frame_end")
        cache = modifier.point_cache
        validate_rna_value(cache, "frame_start", cache_frame_start)
        validate_rna_value(cache, "frame_end", cache_frame_end)
        validate_rna_value(modifier.settings, "use_dynamic_mesh", use_dynamic_mesh)
        old_shape = modifier.settings.rest_shape_key
        old_dynamic = modifier.settings.use_dynamic_mesh
        old_range = (cache.frame_start, cache.frame_end)
        try:
            modifier.settings.rest_shape_key = shape_key
            modifier.settings.use_dynamic_mesh = use_dynamic_mesh
            set_cache_frame_range(cache, cache_frame_start, cache_frame_end)
            _tag_update(obj)
        except Exception:
            modifier.settings.rest_shape_key = old_shape
            modifier.settings.use_dynamic_mesh = old_dynamic
            set_cache_frame_range(cache, *old_range)
            raise
        cloth_index = list(obj.modifiers).index(modifier)
        upstream = list(obj.modifiers)[:cloth_index]
        topology_modifiers = [item.name for item in upstream if item.type in _TOPOLOGY_MODIFIERS]
        upstream_deformers = [item.name for item in upstream if item.type in _DEFORMING_MODIFIERS]
        animated_upstream = [item.name for item in upstream if _modifier_is_animated(obj, item)]
        warnings = []
        if use_dynamic_mesh and topology_modifiers:
            warnings.append(
                f"Dynamic mesh is enabled with upstream topology modifiers {topology_modifiers}; "
                "topology must remain identical throughout the cache range."
            )
        return {
            "changed_objects": [obj.name],
            "object": obj.name,
            "modifier": modifier.name,
            "rest_shape_key": {
                "old": old_shape.name if old_shape else None,
                "new": shape_key.name,
                "vertex_count": len(shape_key.data),
            },
            "use_dynamic_mesh": {"old": old_dynamic, "new": modifier.settings.use_dynamic_mesh},
            "cache_range": {"old": list(old_range), "new": [cache.frame_start, cache.frame_end]},
            "upstream_deformers": upstream_deformers,
            "upstream_topology_modifiers": topology_modifiers,
            "animated_upstream_modifiers": animated_upstream,
            "shape_key_animation": _animation_info(obj),
            "rest_source_intent": (
                "DYNAMIC_PRE_SIMULATION_MESH_WITH_REST_SHAPE_KEY"
                if use_dynamic_mesh
                else "STATIC_SHAPE_KEY_REST_SURFACE"
            ),
            "point_cache": _cache_info(cache),
            "warnings": warnings,
        }

    def configure_cloth_field_weights(self, object_name, modifier_name, patch):
        obj, modifier = _get_cloth(object_name, modifier_name)
        _reject_baked([(obj, modifier)])
        if not patch:
            raise ValueError("Field-weight patch cannot be empty")
        patch = dict(patch)
        collection_name = patch.pop("collection_name", None)
        clear_collection = patch.pop("clear_collection", False)
        if collection_name and clear_collection:
            raise ValueError("collection_name and clear_collection cannot be combined")
        scenes = _object_scenes(obj)
        if not scenes:
            raise ValueError(f"Cloth object '{obj.name}' is not linked to a scene")
        scene = scenes[0]
        collection = None
        if collection_name:
            collection = bpy.data.collections.get(collection_name)
            if collection is None:
                raise ValueError(f"Collection not found: {collection_name}")
            if not _collection_in_scene(collection, scene):
                raise ValueError(f"Effector collection '{collection_name}' is not linked to scene '{scene.name}'")
        weights = modifier.settings.effector_weights
        old_collection = weights.collection
        changes = patch_rna(weights, patch, _FIELD_WEIGHT_FIELDS)
        try:
            if collection_name or clear_collection:
                weights.collection = collection if collection_name else None
                changes["collection"] = {
                    "old": old_collection.name if old_collection else None,
                    "new": collection.name if collection else None,
                }
            _tag_update(obj)
        except Exception:
            restore_rna(weights, changes)
            weights.collection = old_collection
            raise
        relationships = _field_relationships(modifier.settings, scene)
        cloth_location = obj.matrix_world.translation
        proximity = []
        threshold = max(float(obj.dimensions.length), 1e-6)
        for item in relationships["effectors"]:
            field_obj = bpy.data.objects.get(item["object"])
            if field_obj is None:
                continue
            distance = float((field_obj.matrix_world.translation - cloth_location).length)
            proximity.append({**item, "origin_distance_world": distance})
        warnings = []
        for item in relationships["effectors"]:
            field_obj = bpy.data.objects.get(item["object"])
            if field_obj and not any(field_obj.name in layer.objects for layer in scene.view_layers):
                warnings.append(f"Force field '{field_obj.name}' is excluded from every scene view layer.")
        close = [item["object"] for item in proximity if item["origin_distance_world"] < threshold]
        if close:
            warnings.append(f"Force-field origins within one cloth bounding-box diagonal: {close}")
        return {
            "changed_objects": [obj.name],
            "object": obj.name,
            "modifier": modifier.name,
            "changes": changes,
            "cloth_gravity_vector": list(modifier.settings.gravity),
            "effector_gravity_multiplier": weights.gravity,
            "combined_gravity_intent": [component * weights.gravity for component in modifier.settings.gravity],
            "field_relationships": {**relationships, "proximity": proximity},
            "point_cache": _cache_info(modifier.point_cache),
            "warnings": warnings,
        }
