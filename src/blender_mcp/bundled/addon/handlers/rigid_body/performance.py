# pyright: reportGeneralTypeIssues=false
"""Structural and bounded timing diagnostics for rigid-body simulations."""

import math
import time

from collections import Counter, deque
from itertools import combinations, islice

from .inspection_and_setup import (
    _aabb_overlap,
    _cache_info,
    _evaluated_geometry,
    _scene,
    _validate_object_batch,
    _view_layer_for,
)


def _collision_layers(body):
    return {index + 1 for index, enabled in enumerate(body.collision_collections) if enabled}


def _constraint_islands(names, pairs):
    neighbors = {name: set() for name in names}
    for first, second in pairs:
        if first in neighbors and second in neighbors:
            neighbors[first].add(second)
            neighbors[second].add(first)
    remaining = set(names)
    islands = []
    while remaining:
        root = min(remaining)
        remaining.remove(root)
        queue = deque([root])
        island = []
        while queue:
            name = queue.popleft()
            island.append(name)
            for neighbor in sorted(neighbors[name] & remaining):
                remaining.remove(neighbor)
                queue.append(neighbor)
        islands.append(sorted(island))
    return sorted(islands, key=lambda island: (-len(island), island))


class RigidBodyPerformanceHandlers:
    """Measure whole-frame evaluation and identify structural rigid-body bottlenecks."""

    def analyze_rigid_body_performance(
        self,
        scene_name,
        object_names,
        sample_frames=None,
        maximum_pair_checks=512,
        triangle_warning_threshold=50_000,
        timeout_seconds=20.0,
    ):
        scene = _scene(scene_name)
        world = scene.rigidbody_world
        if world is None:
            raise ValueError(f"Scene '{scene.name}' has no rigid-body world")
        objects = _validate_object_batch(scene, object_names, require_body=True)
        if not 1 <= len(objects) <= 500:
            raise ValueError("object_names must contain 1-500 rigid bodies")
        frames = list(sample_frames or [])
        if len(frames) > 20 or frames != sorted(set(frames)):
            raise ValueError("sample_frames must contain at most 20 unique ordered frames")
        if not 1 <= maximum_pair_checks <= 10_000:
            raise ValueError("maximum_pair_checks must be in [1, 10000]")
        if not 100 <= triangle_warning_threshold <= 10_000_000:
            raise ValueError("triangle_warning_threshold must be in [100, 10000000]")
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 120:
            raise ValueError("timeout_seconds must be finite and in (0, 120]")
        findings = []

        def add(severity, code, subject, evidence, remediation):
            findings.append(
                {
                    "severity": severity,
                    "code": code,
                    "subject": subject,
                    "evidence": evidence,
                    "remediation": remediation,
                }
            )

        geometry = {obj.name: _evaluated_geometry(obj) for obj in objects}
        masses = [float(obj.rigid_body.mass) for obj in objects if obj.rigid_body.type == "ACTIVE"]
        sizes = [
            max(info["bounds_world"]["dimensions"]) for info in geometry.values() if info["bounds_world"] is not None
        ]
        shape_counts = Counter(obj.rigid_body.collision_shape for obj in objects)
        type_counts = Counter(obj.rigid_body.type for obj in objects)
        mesh_triangles = {
            obj.name: geometry[obj.name]["triangles"] for obj in objects if obj.rigid_body.collision_shape == "MESH"
        }
        for object_name, triangles in mesh_triangles.items():
            if triangles is not None and triangles > triangle_warning_threshold:
                add(
                    "WARNING",
                    "EXPENSIVE_MESH_COLLIDER",
                    object_name,
                    {"triangles": triangles, "threshold": triangle_warning_threshold},
                    "Use a primitive, convex hull, or lower-resolution collision proxy.",
                )
        deforming = [obj.name for obj in objects if obj.rigid_body.use_deform]
        if deforming:
            add(
                "WARNING",
                "DEFORMING_COLLIDERS",
                deforming,
                {"count": len(deforming)},
                "Keep deforming colliders bounded and prefer rigid primitive proxies when possible.",
            )
        if masses:
            mass_ratio = max(masses) / max(min(masses), 1e-12)
            if mass_ratio > 100.0:
                add(
                    "WARNING",
                    "EXTREME_MASS_RATIO",
                    "active_bodies",
                    {"minimum": min(masses), "maximum": max(masses), "ratio": mass_ratio},
                    "Reduce mass ratios inside connected islands before increasing solver iterations.",
                )
        else:
            mass_ratio = None
        if sizes:
            size_ratio = max(sizes) / max(min(sizes), 1e-12)
            if size_ratio > 100.0:
                add(
                    "INFO",
                    "EXTREME_SIZE_RATIO",
                    "rigid_bodies",
                    {"minimum": min(sizes), "maximum": max(sizes), "ratio": size_ratio},
                    "Use collision layers and adequate substeps for small fast bodies.",
                )
        else:
            size_ratio = None
        pair_candidates = list(islice(combinations(objects, 2), maximum_pair_checks))
        pair_checks_truncated = len(objects) * (len(objects) - 1) // 2 > len(pair_candidates)
        filtered_pairs = []
        overlap_pairs = []
        for first, second in pair_candidates:
            if not _collision_layers(first.rigid_body) & _collision_layers(second.rigid_body):
                filtered_pairs.append([first.name, second.name])
                continue
            first_bounds = geometry[first.name]["bounds_world"]
            second_bounds = geometry[second.name]["bounds_world"]
            if first_bounds and second_bounds and _aabb_overlap(first_bounds, second_bounds):
                overlap_pairs.append([first.name, second.name])
        if overlap_pairs:
            add(
                "WARNING",
                "INITIAL_AABB_OVERLAPS",
                "rigid_bodies",
                {"pairs": overlap_pairs[:50], "count": len(overlap_pairs)},
                "Resolve initial intersections before increasing solver quality.",
            )
        object_set = set(objects)
        constraint_records = []
        endpoint_pairs = []
        degree = Counter()
        for constraint_object in scene.objects:
            constraint = constraint_object.rigid_body_constraint
            if constraint is None or constraint.object1 not in object_set or constraint.object2 not in object_set:
                continue
            first = constraint.object1.name
            second = constraint.object2.name
            endpoint_pairs.append((first, second))
            degree[first] += 1
            degree[second] += 1
            constraint_records.append(
                {
                    "object": constraint_object.name,
                    "type": constraint.type,
                    "endpoints": [first, second],
                    "solver_override": (
                        constraint.solver_iterations if constraint.use_override_solver_iterations else None
                    ),
                }
            )
        islands = _constraint_islands([obj.name for obj in objects], endpoint_pairs)
        dense = {name: count for name, count in degree.items() if count > 8}
        if dense:
            add(
                "INFO",
                "HIGH_CONSTRAINT_DEGREE",
                sorted(dense),
                dense,
                "Simplify constraint graphs or split independent mechanisms when practical.",
            )
        compound_parts = Counter(
            obj.parent.name
            for obj in objects
            if obj.parent is not None
            and obj.parent.rigid_body is not None
            and obj.parent.rigid_body.collision_shape == "COMPOUND"
        )
        timing = None
        samples = []
        timed_out = False
        if frames:
            original_frame = scene.frame_current
            original_subframe = scene.frame_subframe
            deadline = time.monotonic() + timeout_seconds
            previous = {}
            started = time.perf_counter()
            try:
                for frame in frames:
                    if time.monotonic() >= deadline:
                        timed_out = True
                        break
                    frame_started = time.perf_counter()
                    scene.frame_set(frame)
                    view_layer = _view_layer_for(scene)
                    view_layer.update()
                    depsgraph = view_layer.depsgraph
                    records = []
                    for obj in objects:
                        matrix = obj.evaluated_get(depsgraph).matrix_world
                        location = matrix.translation.copy()
                        displacement = None
                        prior = previous.get(obj.name)
                        if prior is not None:
                            displacement = float((location - prior).length)
                        previous[obj.name] = location
                        records.append(
                            {
                                "object": obj.name,
                                "location_world": list(location),
                                "displacement_since_previous_sample": displacement,
                                "finite": all(math.isfinite(value) for row in matrix for value in row),
                            }
                        )
                    samples.append(
                        {
                            "frame": frame,
                            "wall_seconds": time.perf_counter() - frame_started,
                            "objects": records,
                        }
                    )
            finally:
                elapsed = time.perf_counter() - started
                scene.frame_set(original_frame, subframe=original_subframe)
                _view_layer_for(scene).update()
            timing = {
                "wall_seconds": elapsed,
                "evaluated_frames": len(samples),
                "seconds_per_frame": elapsed / len(samples) if samples else None,
                "timed_out": timed_out,
            }
            nonfinite = [record["object"] for sample in samples for record in sample["objects"] if not record["finite"]]
            if nonfinite:
                add(
                    "ERROR",
                    "NONFINITE_SIMULATION_RESULT",
                    sorted(set(nonfinite)),
                    {"frames": [sample["frame"] for sample in samples]},
                    "Inspect initial overlaps, scales, mass ratios, and constraint limits.",
                )
            size_reference = max(sizes, default=1.0)
            explosive = [
                {
                    "frame": sample["frame"],
                    "object": record["object"],
                    "displacement": record["displacement_since_previous_sample"],
                }
                for sample in samples
                for record in sample["objects"]
                if record["displacement_since_previous_sample"] is not None
                and record["displacement_since_previous_sample"] > size_reference * 10.0
            ]
            if explosive:
                add(
                    "WARNING",
                    "EXPLOSIVE_SEPARATION_CANDIDATE",
                    "sampled_bodies",
                    explosive[:20],
                    "Resolve intersections and inspect mass ratios and constraint frames.",
                )
        if world.substeps_per_frame < 5 and (overlap_pairs or (size_ratio is not None and size_ratio > 100)):
            add(
                "INFO",
                "LOW_SUBSTEPS_FOR_SCENE_SCALE",
                scene.name,
                {"substeps_per_frame": world.substeps_per_frame},
                "After fixing geometry issues, consider increasing world substeps for small or fast bodies.",
            )
        return {
            "changed_objects": [obj.name for obj in objects] if frames else [],
            "scene": scene.name,
            "body_counts": dict(type_counts),
            "collision_shape_counts": dict(shape_counts),
            "mesh_collider_triangles": mesh_triangles,
            "compound_part_counts": dict(compound_parts),
            "collision_pair_analysis": {
                "checked": len(pair_candidates),
                "truncated": pair_checks_truncated,
                "filtered_by_layers": len(filtered_pairs),
                "initial_aabb_overlap_candidates": overlap_pairs,
            },
            "mass_ratio": mass_ratio,
            "size_ratio": size_ratio,
            "constraints": constraint_records,
            "constraint_degree": dict(degree),
            "constraint_islands": islands,
            "world": {
                "substeps_per_frame": world.substeps_per_frame,
                "solver_iterations": world.solver_iterations,
                "time_scale": world.time_scale,
            },
            "point_cache": _cache_info(world.point_cache),
            "sampling": {"requested_frames": frames, "samples": samples, "timing": timing},
            "findings": findings,
            "claim": (
                "Timing covers whole dependency-graph frame evaluation. Blender exposes no stable per-contact "
                "or per-body Bullet profiler data; all stability findings are labeled heuristics."
            ),
        }
