"""Shared geometry sampling and measurement helpers for cloth handlers."""

from __future__ import annotations

import statistics

import bpy
import mathutils

from ...helpers import evaluated_world_bounds, spread_indices


def _evaluated_world_vertices(obj, limit, depsgraph=None):
    depsgraph = depsgraph or bpy.context.evaluated_depsgraph_get()
    evaluated = obj.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh()
    try:
        indices = spread_indices(len(mesh.vertices), limit)
        return {
            "total": len(mesh.vertices),
            "indices": indices,
            "positions": [evaluated.matrix_world @ mesh.vertices[index].co for index in indices],
        }
    finally:
        evaluated.to_mesh_clear()


def _evaluated_surface_measurements(evaluated_obj, mesh, polygon_limit):
    matrix = evaluated_obj.matrix_world
    scanned = min(len(mesh.polygons), polygon_limit)
    area = 0.0
    signed_volume = 0.0
    degenerate = []
    for polygon in list(mesh.polygons)[:scanned]:
        indices = list(polygon.vertices)
        if len(indices) < 3:
            degenerate.append(polygon.index)
            continue
        origin = matrix @ mesh.vertices[indices[0]].co
        polygon_area = 0.0
        for index in range(1, len(indices) - 1):
            second = matrix @ mesh.vertices[indices[index]].co
            third = matrix @ mesh.vertices[indices[index + 1]].co
            cross = (second - origin).cross(third - origin)
            polygon_area += cross.length * 0.5
            signed_volume += float(origin.dot(second.cross(third))) / 6.0
        area += polygon_area
        if polygon_area <= 1e-12:
            degenerate.append(polygon.index)
    complete = scanned == len(mesh.polygons)
    return {
        "surface_area_world_squared": area if complete else None,
        "signed_volume_world_cubed": signed_volume if complete else None,
        "polygons_scanned": scanned,
        "total_polygons": len(mesh.polygons),
        "complete": complete,
        "degenerate_face_count_scanned": len(degenerate),
        "degenerate_face_indices_sample": degenerate[:100],
    }


def _evaluated_geometry_evidence(obj, depsgraph=None):
    depsgraph = depsgraph or bpy.context.evaluated_depsgraph_get()
    evaluated = obj.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh()
    try:
        return {
            "vertices": len(mesh.vertices),
            "edges": len(mesh.edges),
            "faces": len(mesh.polygons),
            "bounds": evaluated_world_bounds(evaluated),
        }
    finally:
        evaluated.to_mesh_clear()


def _proxy_proximity_evidence(render_obj, proxy_obj, depsgraph, sample_limit=10_000):
    from mathutils.bvhtree import BVHTree

    evaluated_render = render_obj.evaluated_get(depsgraph)
    evaluated_proxy = proxy_obj.evaluated_get(depsgraph)
    render_mesh = evaluated_render.to_mesh()
    proxy_mesh = evaluated_proxy.to_mesh()
    try:
        if not proxy_mesh.polygons:
            raise ValueError(f"Proxy '{proxy_obj.name}' evaluates without faces")
        proxy_vertices = [evaluated_proxy.matrix_world @ vertex.co for vertex in proxy_mesh.vertices]
        proxy_polygons = [list(polygon.vertices) for polygon in proxy_mesh.polygons]
        tree = BVHTree.FromPolygons(proxy_vertices, proxy_polygons, all_triangles=False, epsilon=0.0)
        distances = []
        missed = 0
        for index in spread_indices(len(render_mesh.vertices), sample_limit):
            position = evaluated_render.matrix_world @ render_mesh.vertices[index].co
            _location, _normal, _face_index, distance = tree.find_nearest(position)
            if distance is None:
                missed += 1
            else:
                distances.append(float(distance))
        return {
            "coordinate_space": "WORLD",
            "render_vertices": len(render_mesh.vertices),
            "proxy_vertices": len(proxy_mesh.vertices),
            "samples": len(distances),
            "missed_samples": missed,
            "minimum_distance": min(distances) if distances else None,
            "maximum_distance": max(distances) if distances else None,
            "mean_distance": statistics.fmean(distances) if distances else None,
            "sampled": len(render_mesh.vertices) > sample_limit,
        }
    finally:
        evaluated_render.to_mesh_clear()
        evaluated_proxy.to_mesh_clear()


def _collider_proximity(sample_positions, colliders, face_limit, depsgraph=None):
    from mathutils.bvhtree import BVHTree

    evidence = []
    for collider in colliders:
        depsgraph = depsgraph or bpy.context.evaluated_depsgraph_get()
        evaluated = collider.evaluated_get(depsgraph)
        mesh = evaluated.to_mesh()
        try:
            if len(mesh.polygons) > face_limit:
                evidence.append(
                    {
                        "collider": collider.name,
                        "skipped": True,
                        "reason": "evaluated_face_limit",
                        "evaluated_faces": len(mesh.polygons),
                        "face_limit": face_limit,
                    }
                )
                continue
            vertices = [evaluated.matrix_world @ vertex.co for vertex in mesh.vertices]
            polygons = [list(polygon.vertices) for polygon in mesh.polygons]
            bvh = BVHTree.FromPolygons(vertices, polygons, all_triangles=False, epsilon=0.0)
            distances = []
            behind_surface = 0
            for position in sample_positions:
                location, normal, _face_index, distance = bvh.find_nearest(position)
                if location is None or distance is None:
                    continue
                distances.append(float(distance))
                if normal is not None and (position - location).dot(normal) < 0:
                    behind_surface += 1
            evidence.append(
                {
                    "collider": collider.name,
                    "skipped": False,
                    "samples_checked": len(distances),
                    "minimum_surface_distance_world": min(distances) if distances else None,
                    "mean_surface_distance_world": statistics.fmean(distances) if distances else None,
                    "behind_nearest_surface_normal": behind_surface,
                    "heuristic_only": True,
                }
            )
        finally:
            evaluated.to_mesh_clear()
    return evidence


def _evaluated_bvh_overlap(first, second, depsgraph, face_limit=100_000):
    from mathutils.bvhtree import BVHTree

    evaluated_first = first.evaluated_get(depsgraph)
    evaluated_second = second.evaluated_get(depsgraph)
    first_mesh = evaluated_first.to_mesh()
    second_mesh = evaluated_second.to_mesh()
    try:
        if len(first_mesh.polygons) > face_limit or len(second_mesh.polygons) > face_limit:
            return {
                "checked": False,
                "reason": "evaluated_face_limit",
                "face_limit": face_limit,
                "faces": [len(first_mesh.polygons), len(second_mesh.polygons)],
            }
        first_bvh = BVHTree.FromPolygons(
            [evaluated_first.matrix_world @ vertex.co for vertex in first_mesh.vertices],
            [list(polygon.vertices) for polygon in first_mesh.polygons],
            all_triangles=False,
            epsilon=0.0,
        )
        second_bvh = BVHTree.FromPolygons(
            [evaluated_second.matrix_world @ vertex.co for vertex in second_mesh.vertices],
            [list(polygon.vertices) for polygon in second_mesh.polygons],
            all_triangles=False,
            epsilon=0.0,
        )
        overlaps = first_bvh.overlap(second_bvh)
        return {
            "checked": True,
            "coordinate_space": "WORLD",
            "geometry": "EVALUATED_AT_REST_FRAME",
            "overlapping_face_pairs": len(overlaps),
            "sample": [list(pair) for pair in overlaps[:20]],
            "sample_truncated": len(overlaps) > 20,
        }
    finally:
        evaluated_first.to_mesh_clear()
        evaluated_second.to_mesh_clear()
