# pyright: reportGeneralTypeIssues=false
"""Measure what a baked liquid shot actually produced, not just whether it is set up correctly."""

from __future__ import annotations

import math

from collections import defaultdict

import bpy
import mathutils

from ..rna_patch import get_object
from ._frame_evaluation import (
    _evaluate_frames,
    _normalize_frames,
    _plan_frame_evaluation,
    _require_cache_available,
)
from .inspection_and_setup import (
    _get_domain,
    _liquid_object_identity,
    _topology_from_mesh,
    _world_bounds,
)
from .shot import SHOT_ID_PROPERTY, VOLUME_CONTAINER_PROPERTY
from .simulation import _baked_frame_ceiling, _scene_context_for_object

_MAX_FRAMES = 32
_MIN_SAMPLE_RESOLUTION = 4
_MAX_SAMPLE_RESOLUTION = 32
_MAX_TOTAL_SAMPLES = 4_000_000
_OVERFLOW_POLICIES = frozenset({"ALLOW", "FORBID"})
_SPILL_EPSILON = 1e-9
_VOLUME_ROLES = frozenset({"CONTAINER_VOLUME", "SPILL_VOLUME"})
# A ray direction with no zero or repeated components in any axis; this keeps ray_cast from grazing
# an axis-aligned validation-volume face edge-on, which would make the parity crossing count flaky.
_RAY_DIRECTION_COMPONENTS = (0.6180339887, 0.7136441795, 0.9510565162)


def _ray_direction():
    return mathutils.Vector(_RAY_DIRECTION_COMPONENTS).normalized()


def _volume_of_bounds(bounds):
    return math.prod(max(bounds["maximum"][axis] - bounds["minimum"][axis], 0.0) for axis in range(3))


def _union_bounds(bounds_list):
    minimum = [min(bounds["minimum"][axis] for bounds in bounds_list) for axis in range(3)]
    maximum = [max(bounds["maximum"][axis] for bounds in bounds_list) for axis in range(3)]
    return {"minimum": minimum, "maximum": maximum}


def _point_in_bounds(bounds, point):
    return all(bounds["minimum"][axis] <= point[axis] <= bounds["maximum"][axis] for axis in range(3))


def _grid_points(bounds, resolution):
    minimum = bounds["minimum"]
    maximum = bounds["maximum"]
    axes = [
        [minimum[axis] + (index + 0.5) * (maximum[axis] - minimum[axis]) / resolution for index in range(resolution)]
        for axis in range(3)
    ]
    for x in axes[0]:
        for y in axes[1]:
            for z in axes[2]:
                yield (x, y, z)


def _mesh_volume(matrix_world, mesh):
    """Return the mesh's enclosed volume via the divergence theorem over its triangulation."""
    mesh.calc_loop_triangles()
    volume = 0.0
    for triangle in mesh.loop_triangles:
        v0, v1, v2 = (matrix_world @ mesh.vertices[index].co for index in triangle.vertices)
        volume += v0.dot(v1.cross(v2))
    return abs(volume) / 6.0


def _connected_components(mesh):
    adjacency = defaultdict(set)
    for edge in mesh.edges:
        first, second = edge.vertices
        adjacency[first].add(second)
        adjacency[second].add(first)
    visited = set()
    components = 0
    for vertex in range(len(mesh.vertices)):
        if vertex in visited:
            continue
        components += 1
        stack = [vertex]
        visited.add(vertex)
        while stack:
            current = stack.pop()
            for neighbor in adjacency[current]:
                if neighbor not in visited:
                    visited.add(neighbor)
                    stack.append(neighbor)
    return components


def _liquid_mesh_measurement(domain_obj):
    """Return the evaluated liquid mesh's volume/topology plus a local-space BVH for containment tests."""
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = domain_obj.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh()
    try:
        topology = _topology_from_mesh(mesh)
        measurement = {
            "vertices": len(mesh.vertices),
            "faces": len(mesh.polygons),
            "volume": _mesh_volume(evaluated.matrix_world, mesh) if mesh.polygons else 0.0,
            "connected_components": _connected_components(mesh) if mesh.vertices else 0,
            "non_manifold_edges": topology["non_manifold_edges"],
            "boundary_edges": topology["boundary_edges"],
        }
    finally:
        evaluated.to_mesh_clear()
    # BVHTree.FromObject builds its own evaluated mesh internally and is safe to construct after the
    # sample above releases its temporary mesh.
    bvh = mathutils.bvhtree.BVHTree.FromObject(domain_obj, depsgraph, deform=True, cage=False, epsilon=0.0)
    return measurement, bvh, evaluated.matrix_world.copy()


def _point_inside_mesh(bvh, point_local, direction_local, epsilon):
    """Odd/even ray-crossing parity test; ``point_local``/``direction_local`` must match the BVH's space."""
    origin = point_local.copy()
    crossings = 0
    for _attempt in range(64):
        location, _normal, _index, _distance = bvh.ray_cast(origin, direction_local)
        if location is None:
            break
        crossings += 1
        origin = location + direction_local * epsilon
    return crossings % 2 == 1


def _measure_container(bvh, world_matrix, spec, resolution, epsilon):
    interior_bounds = spec["interior_bounds"]
    outer_bounds = spec["outer_bounds"]
    spill_bounds = spec.get("spill_bounds")
    padded = _union_bounds([bounds for bounds in (interior_bounds, outer_bounds, spill_bounds) if bounds])
    inverse = world_matrix.inverted_safe()
    direction_local = (inverse.to_3x3() @ _ray_direction()).normalized()
    total_samples = resolution**3
    cell_volume = _volume_of_bounds(padded) / total_samples
    counts = {"FILL": 0, "SPILL": 0, "WALL_PENETRATION": 0, "ESCAPED": 0}
    for point in _grid_points(padded, resolution):
        point_local = inverse @ mathutils.Vector(point)
        if not _point_inside_mesh(bvh, point_local, direction_local, epsilon):
            continue
        if _point_in_bounds(interior_bounds, point):
            counts["FILL"] += 1
        elif spill_bounds and _point_in_bounds(spill_bounds, point):
            counts["SPILL"] += 1
        elif _point_in_bounds(outer_bounds, point):
            counts["WALL_PENETRATION"] += 1
        else:
            counts["ESCAPED"] += 1
    interior_volume = _volume_of_bounds(interior_bounds)
    volumes = {role: count * cell_volume for role, count in counts.items()}
    return {
        "container": spec["container_name"],
        "interior_volume": interior_volume,
        "fill_volume": volumes["FILL"],
        "fill_fraction": (volumes["FILL"] / interior_volume) if interior_volume > 1e-9 else None,
        "spill_volume": volumes["SPILL"],
        "wall_penetration_volume": volumes["WALL_PENETRATION"],
        "escaped_volume_near_container": volumes["ESCAPED"],
        "sample_resolution": resolution,
        "sample_counts": counts,
    }


def _resolve_container_specs(domain_obj, volume_object_names):
    """Group validation volumes by the container they measure, resolving their interior/spill/outer bounds."""
    if volume_object_names:
        volumes = [get_object(name, {"MESH"}) for name in volume_object_names]
        for volume in volumes:
            if not volume.get(VOLUME_CONTAINER_PROPERTY):
                raise ValueError(
                    f"'{volume.name}' has no {VOLUME_CONTAINER_PROPERTY} property; only validation volumes "
                    "created by setup_liquid_shot (create_validation_volumes=True) can be measured"
                )
    else:
        shot_id = domain_obj.get(SHOT_ID_PROPERTY)
        if not shot_id:
            return []
        volumes = [
            candidate
            for candidate in bpy.data.objects
            if candidate.get(SHOT_ID_PROPERTY) == shot_id
            and _liquid_object_identity(candidate)["role"] in _VOLUME_ROLES
        ]
    grouped = {}
    for volume in volumes:
        role = _liquid_object_identity(volume)["role"]
        if role not in _VOLUME_ROLES:
            raise ValueError(f"'{volume.name}' is not tagged as a CONTAINER_VOLUME or SPILL_VOLUME")
        container_name = volume.get(VOLUME_CONTAINER_PROPERTY)
        if not container_name:
            raise ValueError(f"'{volume.name}' has no {VOLUME_CONTAINER_PROPERTY} property recording its container")
        bounds = _world_bounds(volume, evaluated=False)
        entry = grouped.setdefault(container_name, {"container_name": container_name})
        entry["interior_bounds" if role == "CONTAINER_VOLUME" else "spill_bounds"] = bounds
    specs = []
    for container_name, entry in grouped.items():
        if "interior_bounds" not in entry:
            raise ValueError(f"Container '{container_name}' has a spill volume but no CONTAINER_VOLUME")
        container_obj = get_object(container_name, {"MESH"})
        entry["outer_bounds"] = _world_bounds(container_obj, evaluated=True)
        specs.append(entry)
    return specs


def _measure_frame(domain_obj, frame, container_specs, resolution, epsilon):
    measurement, bvh, matrix_world = _liquid_mesh_measurement(domain_obj)
    containers = [_measure_container(bvh, matrix_world, spec, resolution, epsilon) for spec in container_specs]
    return {"frame": frame, "liquid_mesh": measurement, "containers": containers}


def _evaluate_targets(frame_reports, target_fill_fraction, deadline_frame, overflow_policy):
    findings = []

    def add(severity, code, message, *, frame=None, container=None, evidence=None, remediation=None):
        findings.append(
            {
                "severity": severity,
                "code": code,
                "frame": frame,
                "container": container,
                "message": message,
                "evidence": evidence,
                "remediation": remediation,
            }
        )

    for report in frame_reports:
        frame = report["frame"]
        mesh = report["liquid_mesh"]
        if mesh["non_manifold_edges"] > 0:
            add(
                "INFO",
                "NON_MANIFOLD_LIQUID_MESH",
                "Evaluated liquid mesh has non-manifold edges at this frame.",
                frame=frame,
                evidence={"non_manifold_edges": mesh["non_manifold_edges"]},
                remediation="Expected during active motion; review if it persists on a settled frame.",
            )
        if mesh["connected_components"] > 1:
            add(
                "INFO",
                "MULTIPLE_LIQUID_BODIES",
                f"Liquid is split into {mesh['connected_components']} disconnected bodies.",
                frame=frame,
                evidence={"connected_components": mesh["connected_components"]},
            )
        for container in report["containers"]:
            name = container["container"]
            if overflow_policy == "FORBID" and (
                container["spill_volume"] > _SPILL_EPSILON
                or container["escaped_volume_near_container"] > _SPILL_EPSILON
            ):
                add(
                    "ERROR",
                    "OVERFLOW_FORBIDDEN",
                    f"Liquid escaped or spilled from '{name}' while overflow_policy=FORBID.",
                    frame=frame,
                    container=name,
                    evidence={
                        "spill_volume": container["spill_volume"],
                        "escaped_volume_near_container": container["escaped_volume_near_container"],
                    },
                    remediation=(
                        "Raise the container walls, add/extend a spill catch, or set overflow_policy=ALLOW if "
                        "some spill is expected."
                    ),
                )
            if (
                target_fill_fraction is not None
                and deadline_frame is not None
                and frame >= deadline_frame
                and container["fill_fraction"] is not None
                and container["fill_fraction"] < target_fill_fraction
            ):
                add(
                    "ERROR",
                    "FILL_TARGET_MISSED",
                    f"'{name}' fill fraction {container['fill_fraction']:.3f} is below target "
                    f"{target_fill_fraction:.3f} at/after deadline_frame={deadline_frame}.",
                    frame=frame,
                    container=name,
                    evidence={
                        "fill_fraction": container["fill_fraction"],
                        "target_fill_fraction": target_fill_fraction,
                    },
                    remediation="Extend the inflow's enabled_seconds window, add another source, or lower target_fill_fraction.",
                )
    return findings


class LiquidResultValidationHandlers:
    """Measure the physical outcome of a baked liquid shot against fill/spill/deadline targets."""

    def validate_liquid_result(
        self,
        domain_object_name,
        modifier_name,
        frames,
        volume_object_names=None,
        sample_resolution=16,
        target_fill_fraction=None,
        deadline_frame=None,
        overflow_policy="ALLOW",
        timeout_seconds=30.0,
        max_preroll_frames=250,
    ):
        obj, modifier, settings = _get_domain(domain_object_name, modifier_name)
        normalized = _normalize_frames(frames, max_frames=_MAX_FRAMES)
        if overflow_policy not in _OVERFLOW_POLICIES:
            raise ValueError(f"overflow_policy must be one of {sorted(_OVERFLOW_POLICIES)}")
        resolution = int(sample_resolution)
        if not _MIN_SAMPLE_RESOLUTION <= resolution <= _MAX_SAMPLE_RESOLUTION:
            raise ValueError(f"sample_resolution must be in [{_MIN_SAMPLE_RESOLUTION}, {_MAX_SAMPLE_RESOLUTION}]")
        if target_fill_fraction is not None and not 0.0 <= target_fill_fraction <= 1.0:
            raise ValueError("target_fill_fraction must be in [0.0, 1.0]")
        _require_cache_available(settings, operation="Measuring")
        container_specs = _resolve_container_specs(obj, volume_object_names)
        if not container_specs:
            raise ValueError(
                "No validation volumes were found for this domain; pass volume_object_names explicitly or "
                "create them via setup_liquid_shot(create_validation_volumes=True)"
            )
        total_samples = len(normalized) * len(container_specs) * resolution**3
        if total_samples > _MAX_TOTAL_SAMPLES:
            raise ValueError(
                f"Requested measurement needs {total_samples} samples (frames x containers x "
                f"sample_resolution^3), exceeding the {_MAX_TOTAL_SAMPLES} cap; reduce frames, containers, "
                "or sample_resolution"
            )

        scene, view_layer = _scene_context_for_object(obj)
        plan = _plan_frame_evaluation(
            normalized,
            scene,
            settings,
            baked_frame_ceiling=_baked_frame_ceiling,
            max_frames=_MAX_FRAMES,
            max_preroll_frames=max_preroll_frames,
            operation="Measuring",
        )
        domain_bounds = _world_bounds(obj, evaluated=False)
        cell_size = max(domain_bounds["dimensions"]) / settings.resolution_max
        epsilon = max(cell_size * 1e-3, 1e-6)
        frame_reports, timed_out = _evaluate_frames(
            scene,
            view_layer,
            plan,
            timeout_seconds,
            lambda frame: _measure_frame(obj, frame, container_specs, resolution, epsilon),
        )

        findings = _evaluate_targets(frame_reports, target_fill_fraction, deadline_frame, overflow_policy)
        return {
            "changed_objects": [obj.name] if plan.is_replay else [],
            "domain": obj.name,
            "modifier": modifier.name,
            "cache_type": settings.cache_type,
            "requested_frames": plan.frames,
            "evaluated_frames": [report["frame"] for report in frame_reports],
            "timed_out": timed_out,
            "timeout_seconds": timeout_seconds,
            "preroll_frames": plan.preroll_frames,
            "max_preroll_frames": max_preroll_frames if plan.is_replay else None,
            "sample_resolution": resolution,
            "target_fill_fraction": target_fill_fraction,
            "deadline_frame": deadline_frame,
            "overflow_policy": overflow_policy,
            "frames": frame_reports,
            "findings": findings,
            "passed": not any(finding["severity"] == "ERROR" for finding in findings),
            "timeline_restored": {"frame": scene.frame_current, "subframe": scene.frame_subframe},
            "limitations": [
                "Fill/spill/escape volumes are grid-sampled estimates against axis-aligned validation "
                "volumes, not exact boolean geometry; raise sample_resolution for a tighter estimate.",
                "This measures the evaluated liquid mesh only; it does not advance, bake, or otherwise "
                "mutate the simulation.",
            ],
        }
