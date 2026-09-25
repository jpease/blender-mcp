# Inherited scope metrics: `validate_cloth_simulation` was over the branch/statement/local/nesting
# limits before this branch touched it, and `scripts/lint_changed.py` attributes a whole-scope
# finding to any branch that rewrites a line inside the scope - here two call sites moved onto
# the shared `spread_indices`/`evaluated_world_bounds` helpers. Suppressed, not fixed: splitting
# the function is a separate change with its own risk, and it stays in the whole-tree backlog
# `just lint-all` reports.
# ruff: file-ignore[too-many-branches, too-many-statements, too-many-locals, too-many-nested-blocks]
"""Blender-main-thread handlers for cloth diagnostics and performance analysis."""

from __future__ import annotations

import contextlib
import math
import statistics
import time
import uuid

from collections import Counter

import bpy

from ...helpers import evaluated_world_bounds, paginate, spread_indices, sync_from_editmode
from ..rna_patch import finite, read_fields, validate_rna_value
from ..simulation_cache import set_cache_frame_range
from ._cache_helpers import (
    _configure_independent_cache,
    _external_cache_path_status,
    _run_point_cache_operator,
    _shared_cache_identity,
)
from ._geometry_sampling import (
    _collider_proximity,
    _evaluated_geometry_evidence,
    _evaluated_surface_measurements,
)
from .collisions import _eligible_active_colliders, _is_high_resolution_collider
from .inspection_and_setup import (
    _DEFORMING_MODIFIERS,
    _TOPOLOGY_MODIFIERS,
    _WEIGHT_ROLES,
    _cache_info,
    _collection_in_scene,
    _edge_lengths,
    _evaluated_counts,
    _get_cloth,
    _get_modifier,
    _max_keyed_location_delta,
    _modifier_is_animated,
    _scene_context_for_object,
    _scene_scope,
    _topology_summary,
    _vertex_group_stats,
)
from .proxy_rigs import _remove_created_object


def _modifier_cost_evidence(obj, cloth_modifier, depsgraph):
    base = {"vertices": len(obj.data.vertices), "edges": len(obj.data.edges), "faces": len(obj.data.polygons)}
    evaluated = _evaluated_geometry_evidence(obj, depsgraph)
    colliders = _eligible_active_colliders(obj, cloth_modifier.collision_settings)
    collider_faces = 0
    collider_records = []
    for collider in colliders:
        evaluated_collider = collider.evaluated_get(depsgraph)
        mesh = evaluated_collider.to_mesh()
        try:
            faces = len(mesh.polygons)
            collider_faces += faces
            collider_records.append({"object": collider.name, "evaluated_faces": faces})
        finally:
            evaluated_collider.to_mesh_clear()
    settings = cloth_modifier.settings
    collisions = cloth_modifier.collision_settings
    return {
        "base_geometry": base,
        "evaluated_geometry": evaluated,
        "constraints_heuristic": len(obj.data.edges),
        "solver_quality": settings.quality,
        "collision_quality": collisions.collision_quality,
        "self_collision": collisions.use_self_collision,
        "pressure": settings.use_pressure,
        "internal_springs": settings.use_internal_springs,
        "colliders": collider_records,
        "collider_evaluated_faces": collider_faces,
        "topology_changing_modifiers": [
            modifier.name for modifier in obj.modifiers if modifier.type in _TOPOLOGY_MODIFIERS
        ],
        "modifier_execution_seconds": {
            modifier.name: float(modifier.execution_time)
            for modifier in obj.modifiers
            if hasattr(modifier, "execution_time")
        },
    }


def _validate_frames(frames, *, maximum, label="frames"):
    if not frames:
        raise ValueError(f"{label} must contain at least one frame")
    if len(frames) > maximum:
        raise ValueError(f"{label} exceeds the maximum of {maximum}")
    normalized = [int(frame) for frame in frames]
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{label} must not contain duplicates")
    return sorted(normalized)


class ClothDiagnosticsHandlers:
    """Blender-main-thread handlers for cloth diagnostics and performance analysis."""

    def estimate_cloth_resources(
        self,
        scene_name,
        collection_name=None,
        cloth_object_names=None,
        object_limit=25,
        object_offset=0,
    ):
        scene, scope, _collection = _scene_scope(scene_name, collection_name)
        if cloth_object_names is not None:
            requested = set(cloth_object_names)
            missing = requested - {obj.name for obj in scope}
            if missing:
                raise ValueError(f"Cloth objects outside the requested scope or missing: {sorted(missing)}")
            scope = [obj for obj in scope if obj.name in requested]
        cloth_objects = sorted(
            (obj for obj in scope if any(modifier.type == "CLOTH" for modifier in obj.modifiers)),
            key=lambda obj: obj.name,
        )
        start, end, truncated, next_offset = paginate(len(cloth_objects), object_offset, object_limit, 100)
        estimates = []
        for obj in cloth_objects[start:end]:
            for modifier in obj.modifiers:
                if modifier.type != "CLOTH":
                    continue
                vertices = len(obj.data.vertices)
                edges = len(obj.data.edges)
                faces = len(obj.data.polygons)
                frames = max(1, modifier.point_cache.frame_end - modifier.point_cache.frame_start + 1)
                quality = max(1, modifier.settings.quality)
                collision_quality = max(1, modifier.collision_settings.collision_quality)
                colliders = [
                    collider
                    for collider in _eligible_active_colliders(obj, modifier.collision_settings)
                    if collider.name in scene.objects
                ]
                collider_evaluations = []
                for collider in colliders[:100]:
                    try:
                        counts = _evaluated_counts(collider)
                        collider_evaluations.append({"object": collider.name, **counts})
                    except Exception as exc:
                        collider_evaluations.append({"object": collider.name, "error": str(exc)})
                collider_faces = sum(record.get("faces", 0) for record in collider_evaluations)
                topology_modifiers = [
                    item.name
                    for item in list(obj.modifiers)[: list(obj.modifiers).index(modifier)]
                    if item.type in _TOPOLOGY_MODIFIERS
                ]
                keyed_motion = _max_keyed_location_delta(obj)
                constraint_units = quality * frames * max(1, edges + faces)
                contact_units = (
                    collision_quality * frames * vertices * max(1, collider_faces)
                    if modifier.collision_settings.use_collision
                    else 0
                )
                self_units = (
                    collision_quality * frames * vertices * vertices
                    if modifier.collision_settings.use_self_collision
                    else 0
                )
                feature_factor = 1.0
                if modifier.settings.use_internal_springs:
                    feature_factor += 1.0
                if modifier.settings.use_pressure:
                    feature_factor += 0.25
                cpu_raw = (constraint_units + contact_units * 0.01 + self_units * 0.05) * feature_factor
                memory_raw = vertices + edges * 2 + faces * 3 + (vertices * vertices if self_units else 0)
                cache_raw = vertices * frames * 3
                indices = {
                    "cpu": min(100.0, max(0.0, 12.5 * math.log10(max(1.0, cpu_raw)))),
                    "memory": min(100.0, max(0.0, 12.5 * math.log10(max(1.0, memory_raw)))),
                    "cache": min(100.0, max(0.0, 12.5 * math.log10(max(1.0, cache_raw)))),
                    "collision_pressure": min(100.0, max(0.0, 12.5 * math.log10(max(1.0, contact_units + self_units)))),
                }
                peak = max(indices.values())
                band = "LOW" if peak < 45 else "MEDIUM" if peak < 70 else "HIGH"
                estimates.append(
                    {
                        "object": obj.name,
                        "modifier": modifier.name,
                        "inputs": {
                            "vertices": vertices,
                            "edges": edges,
                            "faces": faces,
                            "frames": frames,
                            "solver_quality": quality,
                            "collision_quality": collision_quality,
                            "self_collision": modifier.collision_settings.use_self_collision,
                            "collider_count": len(colliders),
                            "collider_evaluated_faces_first_100": collider_faces,
                            "collider_evaluations": collider_evaluations,
                            "collider_evaluations_truncated": len(colliders) > 100,
                            "pressure": modifier.settings.use_pressure,
                            "internal_springs": modifier.settings.use_internal_springs,
                            "topology_modifiers_before_cloth": topology_modifiers,
                            "maximum_keyed_location_channel_units_per_frame": keyed_motion,
                            "edge_lengths_local": _edge_lengths(obj),
                        },
                        "relative_indices_0_100": indices,
                        "risk_band": band,
                        "recommendations": {
                            "preview": "Reduce quality/self-collision or use collider proxies"
                            if band == "HIGH"
                            else "Current relative settings are suitable for bounded previews",
                            "final": (
                                "Increase quality only after representative-frame validation and lock dependencies"
                            ),
                        },
                        "runtime_cache": _cache_info(modifier.point_cache),
                    }
                )
        return {
            "scene": scene.name,
            "estimates": estimates,
            "object_page": {
                "total": len(cloth_objects),
                "offset": start,
                "returned_count": end - start,
                "truncated": truncated,
                "next_offset": next_offset,
            },
            "disclaimer": "Relative deterministic indices, not byte, memory, or bake-duration promises.",
        }

    def validate_cloth_setup(
        self,
        scene_name,
        collection_name=None,
        cloth_object_names=None,
        max_findings=200,
        collision_pair_limit=64,
        evaluated_triangle_limit=250000,
    ):
        if not 1 <= max_findings <= 1000:
            raise ValueError("max_findings must be in [1, 1000]")
        if not 1 <= collision_pair_limit <= 256:
            raise ValueError("collision_pair_limit must be in [1, 256]")
        if not 1000 <= evaluated_triangle_limit <= 1_000_000:
            raise ValueError("evaluated_triangle_limit must be in [1000, 1000000]")
        scene, scope, _collection = _scene_scope(scene_name, collection_name)
        if cloth_object_names is not None:
            requested = set(cloth_object_names)
            missing = requested - {obj.name for obj in scope}
            if missing:
                raise ValueError(f"Cloth objects outside the requested scope or missing: {sorted(missing)}")
            scope = [obj for obj in scope if obj.name in requested]
        findings = []
        cache_owners = {}
        omitted_findings = 0

        def add(severity, code, obj, evidence, remediation, **extra):
            nonlocal omitted_findings
            if len(findings) < max_findings:
                findings.append(
                    {
                        "severity": severity,
                        "code": code,
                        "object": obj.name if obj else None,
                        "evidence": evidence,
                        "remediation": remediation,
                        **extra,
                    }
                )
            else:
                omitted_findings += 1

        checked = 0
        pair_count = 0
        pair_limit_reached = False
        for obj in scope:
            cloth_modifiers = [modifier for modifier in obj.modifiers if modifier.type == "CLOTH"]
            if not cloth_modifiers:
                if cloth_object_names is not None:
                    add(
                        "ERROR",
                        "MISSING_CLOTH_MODIFIER",
                        obj,
                        {"requested_as_cloth": True},
                        "Add or identify the intended Cloth modifier before validation.",
                    )
                continue
            checked += 1
            sync_from_editmode(obj)
            if len(cloth_modifiers) > 1:
                add(
                    "ERROR",
                    "DUPLICATE_CLOTH",
                    obj,
                    [m.name for m in cloth_modifiers],
                    "Keep one intentional Cloth modifier or validate each stack explicitly.",
                )
            zero_faces = [poly.index for poly in obj.data.polygons if poly.area <= 1e-12]
            if zero_faces:
                add(
                    "ERROR",
                    "ZERO_AREA_FACES",
                    obj,
                    {"count": len(zero_faces), "sample": zero_faces[:20]},
                    "Repair degenerate faces before simulation.",
                )
            diagonal = max((float(obj.dimensions.length), 1.0))
            tolerance = max(diagonal * 1e-7, 1e-9)
            buckets = Counter(
                tuple(round(float(value) / tolerance) for value in vertex.co) for vertex in obj.data.vertices
            )
            duplicate_count = sum(count - 1 for count in buckets.values() if count > 1)
            if duplicate_count:
                add(
                    "WARNING",
                    "DUPLICATE_VERTEX_POSITIONS",
                    obj,
                    {"count": duplicate_count, "tolerance": tolerance},
                    "Inspect intentional seams versus duplicate geometry; do not merge automatically.",
                )
            edge_uses = Counter()
            directed = Counter()
            for poly in obj.data.polygons:
                vertices = list(poly.vertices)
                for index, a in enumerate(vertices):
                    b = vertices[(index + 1) % len(vertices)]
                    edge_uses[tuple(sorted((a, b)))] += 1
                    directed[a, b] += 1
            boundary = sum(count == 1 for count in edge_uses.values())
            non_manifold = sum(count != 2 for count in edge_uses.values())
            inconsistent = sum(directed[b, a] == 0 for a, b in directed if edge_uses[tuple(sorted((a, b)))] == 2)
            if inconsistent:
                add(
                    "ERROR",
                    "INCONSISTENT_NORMAL_WINDING",
                    obj,
                    {"directed_edges": inconsistent},
                    "Recalculate or repair face winding after inspection.",
                )
            edges = _edge_lengths(obj)
            if edges["min"] == 0:
                add("ERROR", "ZERO_LENGTH_EDGES", obj, edges, "Repair zero-length edges before simulation.")
            elif edges["ratio"] and edges["ratio"] > 20:
                add(
                    "WARNING",
                    "EXTREME_EDGE_RATIO",
                    obj,
                    edges,
                    "Use more uniform simulation topology or a cloth proxy.",
                )
            absolute_scale = [abs(value) for value in obj.scale]
            if min(absolute_scale) == 0 or max(absolute_scale) / max(min(absolute_scale), 1e-12) > 1.01:
                add(
                    "WARNING",
                    "NONUNIFORM_SCALE",
                    obj,
                    list(obj.scale),
                    "Account for scale deliberately before tuning scale-sensitive settings.",
                )
            if obj.matrix_world.to_3x3().determinant() < 0:
                add(
                    "WARNING",
                    "NEGATIVE_DETERMINANT",
                    obj,
                    float(obj.matrix_world.to_3x3().determinant()),
                    "Inspect normals and one-sided collision behavior.",
                )
            for modifier in cloth_modifiers:
                settings = modifier.settings
                collision = modifier.collision_settings
                cache = modifier.point_cache
                cache_key = _shared_cache_identity(cache)
                if cache_key is not None:
                    cache_owners.setdefault(cache_key, []).append((obj.name, modifier.name))
                if cache.use_external:
                    path_status = _external_cache_path_status(cache)
                    if not path_status["valid_directory"]:
                        add(
                            "ERROR",
                            "INVALID_EXTERNAL_CACHE_PATH",
                            obj,
                            path_status,
                            "Choose an existing explicit cache directory before baking.",
                            modifier=modifier.name,
                        )
                if settings.use_pressure and non_manifold:
                    add(
                        "ERROR",
                        "PRESSURE_NON_MANIFOLD",
                        obj,
                        {"boundary_edges": boundary, "non_manifold_edges": non_manifold},
                        "Pressure requires a closed consistently oriented manifold surface.",
                        modifier=modifier.name,
                    )
                loose_edges = sum(edge_uses.get(tuple(sorted(edge.vertices)), 0) == 0 for edge in obj.data.edges)
                if settings.use_sewing_springs and not loose_edges:
                    add(
                        "ERROR",
                        "SEWING_WITHOUT_LOOSE_EDGES",
                        obj,
                        {"loose_edges": 0},
                        "Create and verify intentional loose sewing edges before enabling sewing springs.",
                        modifier=modifier.name,
                    )
                for owner, field in _WEIGHT_ROLES.values():
                    group_name = getattr(getattr(modifier, owner), field, "")
                    if group_name and obj.vertex_groups.get(group_name) is None:
                        add(
                            "ERROR",
                            "MISSING_VERTEX_GROUP",
                            obj,
                            {"property": f"{owner}.{field}", "group": group_name},
                            "Restore the referenced group or clear/reassign the property.",
                            modifier=modifier.name,
                        )
                pin_name = settings.vertex_group_mass
                if pin_name and obj.vertex_groups.get(pin_name):
                    stats = _vertex_group_stats(obj, obj.vertex_groups[pin_name])
                    if stats["nonzero"] == 0:
                        add(
                            "ERROR",
                            "EMPTY_PIN_GROUP",
                            obj,
                            stats,
                            "Assign deliberate nonzero pin weights.",
                            modifier=modifier.name,
                        )
                    elif stats["nonzero"] == len(obj.data.vertices):
                        add(
                            "WARNING",
                            "ALL_VERTICES_PINNED",
                            obj,
                            stats,
                            "Confirm that a fully pinned surface is intentional.",
                            modifier=modifier.name,
                        )
                elif any(
                    item.type in _DEFORMING_MODIFIERS
                    for item in list(obj.modifiers)[: list(obj.modifiers).index(modifier)]
                ):
                    add(
                        "INFO",
                        "ANIMATED_CLOTH_WITHOUT_PINS",
                        obj,
                        "Upstream deformation exists but no pin vertex group is assigned.",
                        "Confirm the entire surface should simulate freely, or assign deliberate pin weights.",
                        modifier=modifier.name,
                    )
                if collision.use_self_collision and edges["min"] and collision.self_distance_min > edges["min"] * 0.5:
                    add(
                        "WARNING",
                        "SELF_DISTANCE_TOO_LARGE",
                        obj,
                        {"distance": collision.self_distance_min, "smallest_edge": edges["min"]},
                        "Reduce self-collision distance or increase uniform mesh resolution.",
                        modifier=modifier.name,
                    )
                cloth_index = list(obj.modifiers).index(modifier)
                downstream = [m.name for m in list(obj.modifiers)[cloth_index + 1 :] if m.type in _DEFORMING_MODIFIERS]
                upstream_topology = [m.name for m in list(obj.modifiers)[:cloth_index] if m.type in _TOPOLOGY_MODIFIERS]
                animated_topology = [
                    m.name
                    for m in list(obj.modifiers)[:cloth_index]
                    if m.type in _TOPOLOGY_MODIFIERS and _modifier_is_animated(obj, m)
                ]
                if downstream:
                    add(
                        "WARNING",
                        "DEFORMER_AFTER_CLOTH",
                        obj,
                        downstream,
                        "Move animation intended to drive pins before Cloth.",
                        modifier=modifier.name,
                    )
                if upstream_topology:
                    add(
                        "WARNING",
                        "TOPOLOGY_MODIFIER_BEFORE_CLOTH",
                        obj,
                        upstream_topology,
                        "Verify topology remains constant throughout the cache range.",
                        modifier=modifier.name,
                    )
                if animated_topology:
                    add(
                        "ERROR",
                        "ANIMATED_TOPOLOGY_BEFORE_CLOTH",
                        obj,
                        animated_topology,
                        "Remove frame-varying topology from the simulation mesh or use a stable cloth proxy.",
                        modifier=modifier.name,
                    )
                effector_collection = settings.effector_weights.collection
                if effector_collection and not _collection_in_scene(effector_collection, scene):
                    add(
                        "ERROR",
                        "EFFECTOR_COLLECTION_OUTSIDE_SCENE",
                        obj,
                        effector_collection.name,
                        "Link the effector collection to this scene or choose a scene-local collection.",
                        modifier=modifier.name,
                    )
                keyed_motion = _max_keyed_location_delta(obj)
                if edges["min"] and keyed_motion and keyed_motion > edges["min"] * max(settings.quality, 1):
                    add(
                        "WARNING",
                        "FAST_KEYED_MOTION",
                        obj,
                        {
                            "maximum_location_channel_units_per_frame": keyed_motion,
                            "smallest_edge_local": edges["min"],
                            "quality": settings.quality,
                        },
                        "Use representative-frame testing and consider higher solver/collision quality.",
                        modifier=modifier.name,
                    )
                if modifier.point_cache.is_outdated:
                    add(
                        "ERROR" if modifier.point_cache.is_baked else "WARNING",
                        "BAKED_CACHE_OUTDATED" if modifier.point_cache.is_baked else "OUTDATED_CACHE",
                        obj,
                        _cache_info(modifier.point_cache),
                        "Revalidate dependencies and rebuild the exact cache when authorized.",
                        modifier=modifier.name,
                    )
                colliders = [
                    other
                    for other in scene.objects
                    if collision.use_collision
                    and any(m.type == "COLLISION" for m in other.modifiers)
                    and other.collision
                    and other.collision.use
                ]
                if collision.use_collision and collision.collection:
                    allowed = collision.collection.all_objects
                    colliders = [other for other in colliders if other.name in allowed]
                if collision.use_collision and not colliders:
                    add(
                        "WARNING",
                        "NO_ELIGIBLE_COLLIDERS",
                        obj,
                        {"collection": collision.collection.name if collision.collection else None},
                        "Add or register an intentional collider in the configured scope.",
                        modifier=modifier.name,
                    )
                for collider in colliders:
                    if pair_count >= collision_pair_limit:
                        pair_limit_reached = True
                        break
                    pair_count += 1
                    if obj.type != "MESH" or collider.type != "MESH":
                        continue
                    if _is_high_resolution_collider(obj, collider):
                        add(
                            "WARNING",
                            "HIGH_RESOLUTION_COLLIDER",
                            obj,
                            {
                                "collider": collider.name,
                                "collider_faces": len(collider.data.polygons),
                                "cloth_faces": len(obj.data.polygons),
                            },
                            "Use a dedicated lower-resolution collision proxy when possible.",
                            modifier=modifier.name,
                        )
                    if (
                        len(obj.data.polygons) > evaluated_triangle_limit
                        or len(collider.data.polygons) > evaluated_triangle_limit
                    ):
                        add(
                            "INFO",
                            "INTERSECTION_CHECK_SKIPPED",
                            obj,
                            {"collider": collider.name, "triangle_limit": evaluated_triangle_limit},
                            "Use lower-resolution collision proxies or raise the bounded limit deliberately.",
                            modifier=modifier.name,
                        )
                        continue
                    try:
                        from mathutils.bvhtree import BVHTree

                        cloth_vertices = [obj.matrix_world @ vertex.co for vertex in obj.data.vertices]
                        collider_vertices = [collider.matrix_world @ vertex.co for vertex in collider.data.vertices]
                        cloth_bvh = BVHTree.FromPolygons(
                            cloth_vertices,
                            [list(poly.vertices) for poly in obj.data.polygons],
                            all_triangles=False,
                            epsilon=0.0,
                        )
                        collider_bvh = BVHTree.FromPolygons(
                            collider_vertices,
                            [list(poly.vertices) for poly in collider.data.polygons],
                            all_triangles=False,
                            epsilon=0.0,
                        )
                        overlaps = cloth_bvh.overlap(collider_bvh)
                        if overlaps:
                            add(
                                "ERROR",
                                "INITIAL_COLLIDER_INTERSECTION",
                                obj,
                                {
                                    "collider": collider.name,
                                    "overlapping_face_pairs": len(overlaps),
                                    "sample": overlaps[:20],
                                },
                                "Resolve rest-frame intersections before simulation.",
                                modifier=modifier.name,
                                frame=scene.frame_current,
                            )
                    except Exception as exc:
                        add(
                            "INFO",
                            "INTERSECTION_CHECK_INCOMPLETE",
                            obj,
                            {"collider": collider.name, "reason": str(exc)},
                            "Inspect this pair in Blender at representative frames.",
                            modifier=modifier.name,
                        )
        for cache_key, owners in cache_owners.items():
            if len(owners) > 1:
                add(
                    "ERROR",
                    "SHARED_CACHE_IDENTITY",
                    None,
                    {"cache": cache_key, "owners": owners},
                    "Give every cloth modifier a unique cache name/path/index before baking.",
                )
        if pair_limit_reached:
            add(
                "INFO",
                "COLLISION_PAIR_LIMIT_REACHED",
                None,
                {"limit": collision_pair_limit},
                "Run a narrower collection/object validation for remaining pairs.",
            )
        truncated = omitted_findings > 0
        severity_counts = Counter(item["severity"] for item in findings)
        return {
            "scene": scene.name,
            "frame_observed": scene.frame_current,
            "cloth_objects_checked": checked,
            "collision_pairs_checked": pair_count,
            "findings": findings,
            "severity_counts": dict(severity_counts),
            "truncated": truncated,
            "omitted_findings": omitted_findings,
            "claim": "Structural preflight only; representative evaluated-frame review is still required.",
        }

    def sample_cloth_simulation(
        self,
        object_name,
        modifier_name,
        frames,
        vertex_sample_limit=10_000,
        collider_sample_limit=16,
        timeout_seconds=30.0,
    ):
        obj, modifier = _get_cloth(object_name, modifier_name)
        sync_from_editmode(obj)
        if not frames or len(frames) > 100:
            raise ValueError("frames must contain 1-100 explicit frame numbers")
        if any(isinstance(frame, bool) or not isinstance(frame, int) for frame in frames):
            raise ValueError("frames must contain integer frame numbers")
        normalized_frames = sorted(set(frames))
        if len(normalized_frames) != len(frames):
            raise ValueError("frames must not contain duplicates")
        if not 1 <= vertex_sample_limit <= 100_000:
            raise ValueError("vertex_sample_limit must be in [1, 100000]")
        if not 0 <= collider_sample_limit <= 64:
            raise ValueError("collider_sample_limit must be in [0, 64]")
        finite(timeout_seconds, "timeout_seconds")
        if not 0 < timeout_seconds <= 300:
            raise ValueError("timeout_seconds must be in (0, 300]")
        scene, view_layer = _scene_context_for_object(obj)
        for frame in normalized_frames:
            validate_rna_value(scene, "frame_current", frame)
        colliders = _eligible_active_colliders(obj, modifier.collision_settings)
        selected_colliders = colliders[:collider_sample_limit]
        original_frame = scene.frame_current
        original_subframe = scene.frame_subframe
        cache_before = _cache_info(modifier.point_cache)
        base_count = len(obj.data.vertices)
        base_indices = spread_indices(base_count, vertex_sample_limit)
        base_positions = {index: obj.matrix_world @ obj.data.vertices[index].co for index in base_indices}
        base_topology = _topology_summary(obj)
        polygon_limit = min(200_000, max(10_000, vertex_sample_limit * 4))
        collider_face_limit = min(250_000, max(25_000, vertex_sample_limit * 10))
        fps = float(scene.render.fps) / max(float(scene.render.fps_base), 1e-9)
        deadline = time.monotonic() + timeout_seconds
        samples = []
        previous = None
        timed_out = False
        try:
            for frame in normalized_frames:
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                scene.frame_set(frame)
                view_layer.update()
                depsgraph = view_layer.depsgraph
                evaluated = obj.evaluated_get(depsgraph)
                mesh = evaluated.to_mesh()
                try:
                    indices = spread_indices(len(mesh.vertices), vertex_sample_limit)
                    positions = [evaluated.matrix_world @ mesh.vertices[index].co for index in indices]
                    surface = _evaluated_surface_measurements(evaluated, mesh, polygon_limit)
                    displacement = None
                    if len(mesh.vertices) == base_count and indices == base_indices:
                        distances = [
                            float((position - base_positions[index]).length)
                            for index, position in zip(indices, positions, strict=True)
                        ]
                        displacement = {
                            "reference": "BASE_MESH_OBJECT_LOCAL_TRANSFORMED_TO_WORLD",
                            "sample_count": len(distances),
                            "minimum_world": min(distances, default=0.0),
                            "maximum_world": max(distances, default=0.0),
                            "mean_world": statistics.fmean(distances) if distances else 0.0,
                        }
                    velocity = None
                    if previous and previous["indices"] == indices:
                        delta_frames = frame - previous["frame"]
                        speeds = [
                            float((current - prior).length) * fps / delta_frames
                            for prior, current in zip(previous["positions"], positions, strict=True)
                        ]
                        velocity = {
                            "estimate_between_frames": [previous["frame"], frame],
                            "sample_count": len(speeds),
                            "maximum_world_units_per_second": max(speeds, default=0.0),
                            "mean_world_units_per_second": statistics.fmean(speeds) if speeds else 0.0,
                        }
                    inverted = []
                    if len(mesh.polygons) == len(obj.data.polygons):
                        world_normal_matrix = evaluated.matrix_world.to_3x3().inverted_safe().transposed()
                        base_normal_matrix = obj.matrix_world.to_3x3().inverted_safe().transposed()
                        for polygon in list(mesh.polygons)[:polygon_limit]:
                            current_normal = world_normal_matrix @ polygon.normal
                            base_normal = base_normal_matrix @ obj.data.polygons[polygon.index].normal
                            if (
                                current_normal.length_squared
                                and base_normal.length_squared
                                and current_normal.normalized().dot(base_normal.normalized()) < 0
                            ):
                                inverted.append(polygon.index)
                    proximity = _collider_proximity(
                        positions,
                        selected_colliders,
                        collider_face_limit,
                        depsgraph,
                    )
                    solver_result = modifier.solver_result
                    sample = {
                        "frame": frame,
                        "evaluated_geometry": {
                            "coordinate_space": "EVALUATED_OBJECT_LOCAL",
                            "vertices": len(mesh.vertices),
                            "edges": len(mesh.edges),
                            "faces": len(mesh.polygons),
                        },
                        "world_bounds": evaluated_world_bounds(evaluated),
                        "vertex_sampling": {
                            "sample_count": len(indices),
                            "total_vertices": len(mesh.vertices),
                            "truncated": len(indices) < len(mesh.vertices),
                        },
                        "displacement": displacement,
                        "velocity": velocity,
                        "surface": {
                            **surface,
                            "volume_meaningful": bool(
                                surface["complete"]
                                and len(mesh.vertices) == base_count
                                and base_topology["non_manifold_edges"] == 0
                            ),
                        },
                        "inverted_faces_relative_to_base": {
                            "inverted_count_scanned": len(inverted),
                            "indices_sample": inverted[:100],
                            "sample_truncated": len(inverted) > 100,
                            "available": len(mesh.polygons) == len(obj.data.polygons),
                        },
                        "collider_proximity": proximity,
                        "solver_status": "AVAILABLE" if solver_result is not None else "NOT_INITIALIZED",
                        "solver_result": (
                            read_fields(
                                solver_result,
                                {
                                    prop.identifier
                                    for prop in solver_result.bl_rna.properties
                                    if prop.identifier != "rna_type"
                                },
                            )
                            if solver_result is not None
                            else None
                        ),
                    }
                    samples.append(sample)
                    previous = {"frame": frame, "indices": indices, "positions": positions}
                finally:
                    evaluated.to_mesh_clear()
                if time.monotonic() >= deadline and frame != normalized_frames[-1]:
                    timed_out = True
                    break
        finally:
            scene.frame_set(original_frame, subframe=original_subframe)
            view_layer.update()
        return {
            "changed_objects": [obj.name],
            "object": obj.name,
            "modifier": modifier.name,
            "requested_frames": normalized_frames,
            "evaluated_frames": [sample["frame"] for sample in samples],
            "timed_out": timed_out,
            "timeout_seconds": timeout_seconds,
            "timeline_restored": {
                "frame": scene.frame_current,
                "subframe": scene.frame_subframe,
            },
            "collider_scope": {
                "eligible": [collider.name for collider in colliders],
                "sampled": [collider.name for collider in selected_colliders],
                "truncated": len(selected_colliders) < len(colliders),
            },
            "samples": samples,
            "point_cache_before": cache_before,
            "point_cache_after": _cache_info(modifier.point_cache),
            "cache_effect": "Evaluation may populate or invalidate Blender's in-memory point cache.",
            "claim": "Bounded measurements only; these samples do not prove stable convergence or visual correctness.",
        }

    def analyze_cloth_performance(
        self,
        object_name,
        modifier_name,
        frames,
        warm_repeats=2,
        max_total_evaluations=60,
        include_short_bake=False,
        confirm_short_bake=False,
        short_bake_frame_start=None,
        short_bake_frame_end=None,
    ):
        obj, cloth_modifier = _get_cloth(object_name, modifier_name)
        normalized_frames = _validate_frames(frames, maximum=30)
        if not 1 <= warm_repeats <= 5:
            raise ValueError("warm_repeats must be in [1, 5]")
        total_evaluations = len(normalized_frames) * (1 + warm_repeats)
        if not 1 <= max_total_evaluations <= 180 or total_evaluations > max_total_evaluations:
            raise ValueError("Requested first/warm evaluations exceed max_total_evaluations")
        if include_short_bake and not confirm_short_bake:
            raise ValueError("include_short_bake requires confirm_short_bake=True")
        if not include_short_bake and (short_bake_frame_start is not None or short_bake_frame_end is not None):
            raise ValueError("Short-bake frame bounds require include_short_bake=True")
        short_bake_range = None
        if include_short_bake:
            if short_bake_frame_start is None or short_bake_frame_end is None:
                raise ValueError("Both short-bake frame bounds are required")
            short_bake_range = (int(short_bake_frame_start), int(short_bake_frame_end))
            if short_bake_range[0] > short_bake_range[1]:
                raise ValueError("short_bake_frame_start must be <= short_bake_frame_end")
            if short_bake_range[1] - short_bake_range[0] + 1 > 20:
                raise ValueError("The isolated short bake is limited to 20 frames")
        scene, view_layer = _scene_context_for_object(obj)
        original_frame = scene.frame_current
        original_subframe = scene.frame_subframe

        def timed_pass(pass_frames):
            timings = []
            for frame in pass_frames:
                started = time.perf_counter()
                scene.frame_set(frame)
                view_layer.update()
                evaluated = obj.evaluated_get(view_layer.depsgraph)
                mesh = evaluated.to_mesh()
                try:
                    counts = [len(mesh.vertices), len(mesh.edges), len(mesh.polygons)]
                finally:
                    evaluated.to_mesh_clear()
                timings.append(
                    {
                        "frame": frame,
                        "seconds": time.perf_counter() - started,
                        "evaluated_counts": counts,
                    }
                )
            return timings

        temporary = None
        temporary_data = None
        short_bake = None
        try:
            first_pass = timed_pass(normalized_frames)
            warm_passes = [timed_pass(normalized_frames) for _repeat in range(warm_repeats)]
            cost_evidence = _modifier_cost_evidence(obj, cloth_modifier, view_layer.depsgraph)
            if short_bake_range is not None:
                short_bake_start, short_bake_end = short_bake_range
                temporary = obj.copy()
                temporary.name = f"__BlendMCP_Profile_{uuid.uuid4().hex[:8]}"
                temporary_data = obj.data.copy()
                temporary.data = temporary_data
                scene.collection.objects.link(temporary)
                view_layer.update()
                temporary_modifier = _get_modifier(temporary, modifier_name, "CLOTH")
                if temporary_modifier.point_cache.is_baked or temporary_modifier.point_cache.is_baking:
                    raise RuntimeError("Temporary profiling cache unexpectedly inherited active bake state")
                _configure_independent_cache(
                    temporary_modifier.point_cache,
                    temporary.name,
                    temporary_modifier.name,
                )
                set_cache_frame_range(
                    temporary_modifier.point_cache,
                    short_bake_start,
                    short_bake_end,
                )
                scene.frame_set(short_bake_start)
                started = time.perf_counter()
                _run_point_cache_operator(temporary, temporary_modifier.point_cache, bpy.ops.ptcache.bake, bake=True)
                short_bake = {
                    "frames": short_bake_end - short_bake_start + 1,
                    "seconds": time.perf_counter() - started,
                    "point_cache": _cache_info(temporary_modifier.point_cache),
                    "isolated_temporary_object": True,
                }
        finally:
            if temporary is not None:
                with contextlib.suppress(Exception):
                    temporary_modifier = _get_modifier(temporary, modifier_name, "CLOTH")
                    if temporary_modifier.point_cache.is_baked:
                        _run_point_cache_operator(
                            temporary,
                            temporary_modifier.point_cache,
                            bpy.ops.ptcache.free_bake,
                        )
                _remove_created_object(temporary, temporary_data)
            scene.frame_set(original_frame, subframe=original_subframe)
            view_layer.update()
        first_total = sum(record["seconds"] for record in first_pass)
        warm_totals = [sum(record["seconds"] for record in records) for records in warm_passes]
        recommendations = []
        if cost_evidence["self_collision"]:
            recommendations.append("Use a bounded self-collision vertex group or disable self-collision for previews.")
        if cost_evidence["collider_evaluated_faces"] > cost_evidence["base_geometry"]["faces"] * 4:
            recommendations.append("Use simpler collision proxies; collider face count dominates the cloth surface.")
        if cost_evidence["solver_quality"] > 8 or cost_evidence["collision_quality"] > 4:
            recommendations.append(
                "Lower solver/collision quality for preview variants and restore it for final baking."
            )
        if cost_evidence["topology_changing_modifiers"]:
            recommendations.append("Move or simplify topology-changing modifiers around Cloth where the shot permits.")
        if not recommendations:
            recommendations.append(
                "No dominant structural cost flag was detected; profile representative contact frames."
            )
        return {
            "changed_objects": [obj.name],
            "object": obj.name,
            "modifier": modifier_name,
            "frames": normalized_frames,
            "timings": {
                "first_pass": first_pass,
                "first_pass_seconds": first_total,
                "warm_passes": warm_passes,
                "warm_pass_seconds": warm_totals,
                "first_pass_is_cold_guaranteed": False,
                "note": "Existing point-cache state is preserved, so the first pass is not forcibly cold.",
            },
            "short_isolated_bake": short_bake,
            "cost_evidence": cost_evidence,
            "solver_result": read_fields(
                cloth_modifier.solver_result,
                {
                    prop.identifier
                    for prop in cloth_modifier.solver_result.bl_rna.properties
                    if prop.identifier != "rna_type"
                },
            )
            if cloth_modifier.solver_result
            else None,
            "point_cache": _cache_info(cloth_modifier.point_cache),
            "source_cache_freed_or_overwritten": False,
            "recommendations": recommendations,
            "warnings": ["Frame evaluation can populate the source object's in-memory cloth cache."],
        }
