# pyright: reportGeneralTypeIssues=false, reportOptionalSubscript=false
"""Blender-main-thread handlers for creating retopology targets, inspecting them, and checkpointing."""

import contextlib
import statistics

from collections import Counter

import bmesh
import bpy

from ...helpers import get_mesh_object, mesh_counts, paginate
from ._shared import (
    _configure_shrinkwrap_modifier,
    _ensure_indices,
    _kd_tree_class,
    _modifier_order,
    _percentile,
    _positive,
    _read_bmesh,
    topology_revision,
)

_RETARGET_SOURCES = "blender_mcp_retopology_sources"
_CHECKPOINT_COLLECTION = "_BlenderMCP_Retopology_Checkpoints"
_CHECKPOINT_TARGET = "blender_mcp_checkpoint_target"
_CHECKPOINT_LABEL = "blender_mcp_checkpoint_name"
_MAX_INSPECT_LIMIT = 500


def _mesh_objects(names, label="source_object_names"):
    if not names:
        raise ValueError(f"{label} must contain at least one mesh object name")
    objects = []
    seen = set()
    for name in names:
        if not isinstance(name, str) or not name:
            raise ValueError(f"Every item in {label} must be a non-empty string")
        obj = get_mesh_object(name)
        if obj.name not in seen:
            objects.append(obj)
            seen.add(obj.name)
    return objects


def _copy_evaluated_sources(mesh, sources, target_matrix):
    bm = bmesh.new()
    inverse = target_matrix.inverted_safe()
    depsgraph = bpy.context.evaluated_depsgraph_get()
    try:
        for source in sources:
            evaluated = source.evaluated_get(depsgraph)
            evaluated_mesh = evaluated.to_mesh()
            try:
                transform = inverse @ evaluated.matrix_world
                vertex_map = {vertex.index: bm.verts.new(transform @ vertex.co) for vertex in evaluated_mesh.vertices}
                for polygon in evaluated_mesh.polygons:
                    with contextlib.suppress(ValueError):
                        bm.faces.new(tuple(vertex_map[index] for index in polygon.vertices))
            finally:
                evaluated.to_mesh_clear()
        bm.to_mesh(mesh)
    finally:
        bm.free()


def _new_target_geometry(mesh, geometry, size, grid_segments, sources, matrix_world):
    if geometry == "DUPLICATED_EVALUATED_SURFACE":
        _copy_evaluated_sources(mesh, sources, matrix_world)
        return
    bm = bmesh.new()
    try:
        if geometry == "SINGLE_VERTEX":
            bm.verts.new((0.0, 0.0, 0.0))
        elif geometry == "PLANE":
            half = size / 2.0
            vertices = [
                bm.verts.new(co) for co in ((-half, -half, 0), (half, -half, 0), (half, half, 0), (-half, half, 0))
            ]
            bm.faces.new(vertices)
        elif geometry == "GRID":
            u_count, v_count = grid_segments
            grid = []
            for v in range(v_count):
                y = size * (v / (v_count - 1) - 0.5)
                row = []
                for u in range(u_count):
                    x = size * (u / (u_count - 1) - 0.5)
                    row.append(bm.verts.new((x, y, 0.0)))
                grid.append(row)
            for v in range(v_count - 1):
                for u in range(u_count - 1):
                    bm.faces.new((grid[v][u], grid[v][u + 1], grid[v + 1][u + 1], grid[v + 1][u]))
        bm.to_mesh(mesh)
    finally:
        bm.free()


def _ordered_boundary_paths(bm):
    boundary_edges = {edge for edge in bm.edges if edge.is_boundary}
    paths = []
    while boundary_edges:
        seed = min(boundary_edges, key=lambda edge: edge.index)
        component_edges = set()
        queue = [seed]
        while queue:
            edge = queue.pop()
            if edge in component_edges:
                continue
            component_edges.add(edge)
            for vertex in edge.verts:
                queue.extend(
                    link for link in vertex.link_edges if link in boundary_edges and link not in component_edges
                )
        component_all = set(component_edges)
        degrees = Counter(vertex for edge in component_edges for vertex in edge.verts)
        if any(degree > 2 for degree in degrees.values()):
            paths.append({"closed": False, "branched": True, "vertex_indices": sorted(v.index for v in degrees)})
            boundary_edges -= component_all
            continue
        endpoints = [vertex for vertex, degree in degrees.items() if degree == 1]
        current = min(endpoints or list(degrees), key=lambda vertex: vertex.index)
        ordered = [current.index]
        previous = None
        while True:
            candidates = [edge for edge in current.link_edges if edge in component_edges and edge is not previous]
            if not candidates:
                break
            edge = min(candidates, key=lambda candidate: candidate.index)
            component_edges.remove(edge)
            next_vertex = edge.other_vert(current)
            if next_vertex.index == ordered[0]:
                break
            ordered.append(next_vertex.index)
            previous, current = edge, next_vertex
        paths.append({"closed": not endpoints, "branched": False, "vertex_indices": ordered})
        boundary_edges -= component_all
    return paths


def _connected_components(bm):
    remaining = set(bm.verts)
    components = []
    while remaining:
        seed = min(remaining, key=lambda vertex: vertex.index)
        found = set()
        queue = [seed]
        while queue:
            vertex = queue.pop()
            if vertex in found:
                continue
            found.add(vertex)
            queue.extend(edge.other_vert(vertex) for edge in vertex.link_edges if edge.other_vert(vertex) not in found)
        remaining -= found
        components.append(sorted(vertex.index for vertex in found))
    return components


def _symmetry_summary(bm):
    if not bm.verts:
        return {axis: {"matched": 0, "unmatched": 0, "tolerance": 0.0} for axis in "XYZ"}
    extent = max((vertex.co.length for vertex in bm.verts), default=1.0)
    tolerance = max(1e-6, extent * 1e-5)
    tree = _kd_tree_class()(len(bm.verts))
    for vertex in bm.verts:
        tree.insert(vertex.co, vertex.index)
    tree.balance()
    result = {}
    for axis_index, axis in enumerate("XYZ"):
        unmatched = 0
        for vertex in bm.verts:
            mirrored = vertex.co.copy()
            mirrored[axis_index] *= -1.0
            _co, _index, distance = tree.find(mirrored)
            unmatched += distance is None or distance > tolerance
        result[axis] = {"matched": len(bm.verts) - unmatched, "unmatched": unmatched, "tolerance": tolerance}
    return result


def _modifier_snapshot(modifier):
    result = {"name": modifier.name, "type": modifier.type, "show_viewport": modifier.show_viewport}
    for name in ("levels", "render_levels", "wrap_method", "wrap_mode", "offset", "project_limit", "merge_threshold"):
        if hasattr(modifier, name):
            value = getattr(modifier, name)
            result[name] = value.name if hasattr(value, "name") else value
    for name in ("target", "auxiliary_target", "mirror_object"):
        if hasattr(modifier, name):
            value = getattr(modifier, name)
            result[name] = value.name if value else None
    return result


class _TargetMixin:
    """Provide retopology-target creation, inspection, and checkpoint handlers."""

    def create_retopology_target(
        self,
        source_object_names,
        name=None,
        initial_geometry="EMPTY",
        collection_name="Retopology",
        size=1.0,
        grid_segments=(4, 4),
        add_mirror=False,
        add_shrinkwrap=True,
        subdivision_levels=0,
    ):
        sources = _mesh_objects(source_object_names)
        geometry = str(initial_geometry).upper()
        allowed = {"EMPTY", "SINGLE_VERTEX", "PLANE", "GRID", "DUPLICATED_EVALUATED_SURFACE"}
        if geometry not in allowed:
            raise ValueError(f"initial_geometry must be one of {sorted(allowed)}")
        size = _positive(size, "size")
        if len(grid_segments) != 2 or any(isinstance(value, bool) or int(value) < 2 for value in grid_segments):
            raise ValueError("grid_segments must contain two integers, each at least 2")
        levels = int(subdivision_levels)
        if levels < 0 or levels > 6:
            raise ValueError("subdivision_levels must be between 0 and 6")
        if not isinstance(collection_name, str) or not collection_name.strip():
            raise ValueError("collection_name must be a non-empty string")

        collection = bpy.data.collections.get(collection_name)
        if collection is None:
            collection = bpy.data.collections.new(collection_name)
            bpy.context.scene.collection.children.link(collection)
        mesh = bpy.data.meshes.new(f"{name or sources[0].name + '_Retopology'}_Mesh")
        obj = bpy.data.objects.new(name or f"{sources[0].name}_Retopology", mesh)
        collection.objects.link(obj)
        obj.matrix_world = sources[0].matrix_world.copy()
        obj[_RETARGET_SOURCES] = [source.name for source in sources]
        obj["blender_mcp_retopology_target"] = True
        _new_target_geometry(mesh, geometry, size, tuple(map(int, grid_segments)), sources, obj.matrix_world)

        if add_mirror:
            mirror = obj.modifiers.new(name="RetopologyMirror", type="MIRROR")
            mirror.use_axis = (True, False, False)
            mirror.use_mirror_merge = True
            mirror.use_clip = True
        if add_shrinkwrap:
            _configure_shrinkwrap_modifier(
                obj,
                sources[0],
                "RetopologyProjection",
                "NEAREST_SURFACEPOINT",
                "ON_SURFACE",
                0.0,
                0.0,
                (False, False, True),
                True,
                True,
                "OFF",
                False,
                None,
                "",
                False,
            )
        if levels:
            subdivision = obj.modifiers.new(name="RetopologySubdivision", type="SUBSURF")
            subdivision.levels = levels
            subdivision.render_levels = levels
        return {
            "name": obj.name,
            "collection": collection.name,
            "sources": [source.name for source in sources],
            "initial_geometry": geometry,
            **mesh_counts(obj),
            "modifier_order": _modifier_order(obj),
            "topology_revision": topology_revision(obj),
        }

    def inspect_retopology(self, object_name, selected_vertex_indices=None, adjacency_depth=1, limit=100, offset=0):
        obj = get_mesh_object(object_name)
        depth = int(adjacency_depth)
        if depth < 0 or depth > 8:
            raise ValueError("adjacency_depth must be between 0 and 8")
        selected = _ensure_indices(obj.data.vertices, selected_vertex_indices, "selected_vertex_indices")
        if len(selected) > 100:
            raise ValueError("selected_vertex_indices is limited to 100 vertices per inspection")
        depsgraph = bpy.context.evaluated_depsgraph_get()
        evaluated_object = obj.evaluated_get(depsgraph)
        evaluated_mesh = evaluated_object.to_mesh()
        try:
            evaluated_counts = {
                "vertices": len(evaluated_mesh.vertices),
                "edges": len(evaluated_mesh.edges),
                "polygons": len(evaluated_mesh.polygons),
            }
        finally:
            evaluated_object.to_mesh_clear()
        with _read_bmesh(obj) as bm:
            components = _connected_components(bm)
            boundary_paths = _ordered_boundary_paths(bm)
            non_manifold = [edge.index for edge in bm.edges if not edge.is_manifold]
            isolated_vertices = [vertex.index for vertex in bm.verts if not vertex.link_edges]
            isolated_edges = [edge.index for edge in bm.edges if not edge.link_faces]
            degenerates = [face.index for face in bm.faces if face.calc_area() <= 1e-12]
            face_types = Counter(
                "triangles" if len(face.verts) == 3 else "quads" if len(face.verts) == 4 else "ngons"
                for face in bm.faces
            )
            poles = {}
            for vertex in bm.verts:
                valence = len(vertex.link_edges)
                if valence != 4:
                    poles.setdefault(str(valence), []).append(vertex.index)
            edge_lengths = [edge.calc_length() for edge in bm.edges]
            aspects = []
            for face in bm.faces:
                lengths = [edge.calc_length() for edge in face.edges]
                if lengths and min(lengths) > 1e-12:
                    aspects.append(max(lengths) / min(lengths))
            neighborhoods = []
            for index in selected:
                reached = {bm.verts[index]}
                frontier = {bm.verts[index]}
                for _ in range(depth):
                    frontier = {edge.other_vert(vertex) for vertex in frontier for edge in vertex.link_edges} - reached
                    reached |= frontier
                neighborhoods.append(
                    {
                        "vertex_index": index,
                        "adjacent_vertex_indices": sorted(vertex.index for vertex in reached if vertex.index != index),
                        "edge_indices": sorted({edge.index for vertex in reached for edge in vertex.link_edges}),
                        "face_indices": sorted({face.index for vertex in reached for face in vertex.link_faces}),
                    }
                )
            detail_records = (
                [
                    {
                        "kind": "boundary_vertex",
                        "loop_index": loop_index,
                        "order": order,
                        "index": vertex_index,
                    }
                    for loop_index, path in enumerate(boundary_paths)
                    for order, vertex_index in enumerate(path["vertex_indices"])
                ]
                + [
                    {"kind": "pole", "valence": int(valence), "index": index}
                    for valence, indices in poles.items()
                    for index in indices
                ]
                + [{"kind": "non_manifold_edge", "index": index} for index in non_manifold]
                + [{"kind": "isolated_vertex", "index": index} for index in isolated_vertices]
                + [{"kind": "isolated_edge", "index": index} for index in isolated_edges]
                + [{"kind": "degenerate_face", "index": index} for index in degenerates]
            )
            start, end, truncated, next_offset = paginate(len(detail_records), offset, limit, _MAX_INSPECT_LIMIT)
            return {
                "name": obj.name,
                "coordinate_space": "LOCAL_BASE_MESH",
                "topology_revision": topology_revision(obj),
                "counts": mesh_counts(obj),
                "evaluated_counts": evaluated_counts,
                "components": {"count": len(components), "vertex_counts": [len(component) for component in components]},
                "boundary_loops": [
                    {
                        "loop_index": loop_index,
                        "closed": path["closed"],
                        "branched": path["branched"],
                        "vertex_count": len(path["vertex_indices"]),
                    }
                    for loop_index, path in enumerate(boundary_paths)
                ],
                "face_types": dict(face_types),
                "poles_by_valence": {valence: len(indices) for valence, indices in poles.items()},
                "diagnostic_counts": {
                    "non_manifold_edges": len(non_manifold),
                    "isolated_vertices": len(isolated_vertices),
                    "isolated_edges": len(isolated_edges),
                    "degenerate_faces": len(degenerates),
                },
                "statistics": {
                    "edge_length": {
                        "min": min(edge_lengths, default=None),
                        "mean": statistics.fmean(edge_lengths) if edge_lengths else None,
                        "max": max(edge_lengths, default=None),
                    },
                    "face_aspect_ratio": {
                        "min": min(aspects, default=None),
                        "mean": statistics.fmean(aspects) if aspects else None,
                        "p95": _percentile(aspects, 95),
                        "max": max(aspects, default=None),
                    },
                },
                "uv_layers": [layer.name for layer in obj.data.uv_layers],
                "vertex_groups": [{"name": group.name, "index": group.index} for group in obj.vertex_groups],
                "modifiers": [_modifier_snapshot(modifier) for modifier in obj.modifiers],
                "symmetry": _symmetry_summary(bm),
                "selected_adjacency": neighborhoods,
                "details": detail_records[start:end],
                "offset": start,
                "limit": min(max(1, int(limit)), _MAX_INSPECT_LIMIT),
                "returned_count": end - start,
                "total_details": len(detail_records),
                "truncated": truncated,
                "next_offset": next_offset,
            }

    def manage_retopology_checkpoint(self, action, object_name, checkpoint_name=None, confirm_destructive=False):
        action = str(action).upper()
        if action not in {"CREATE", "LIST", "COMPARE", "RESTORE", "DELETE"}:
            raise ValueError("action must be CREATE, LIST, COMPARE, RESTORE, or DELETE")
        obj = get_mesh_object(object_name)
        collection = bpy.data.collections.get(_CHECKPOINT_COLLECTION)
        checkpoints = (
            []
            if collection is None
            else [
                candidate
                for candidate in collection.objects
                if candidate.get(_CHECKPOINT_TARGET) == obj.name and candidate.type == "MESH"
            ]
        )
        if action == "LIST":
            return {
                "name": obj.name,
                "checkpoints": [
                    {
                        "checkpoint_name": checkpoint.get(_CHECKPOINT_LABEL),
                        "backup_object": checkpoint.name,
                        "counts": mesh_counts(checkpoint),
                        "topology_revision": topology_revision(checkpoint),
                    }
                    for checkpoint in sorted(checkpoints, key=lambda item: item.name)
                ],
            }
        if not isinstance(checkpoint_name, str) or not checkpoint_name.strip():
            raise ValueError("checkpoint_name is required for this action")
        checkpoint = next(
            (candidate for candidate in checkpoints if candidate.get(_CHECKPOINT_LABEL) == checkpoint_name), None
        )
        if action == "CREATE":
            if checkpoint is not None:
                raise ValueError(f"Checkpoint '{checkpoint_name}' already exists for '{obj.name}'")
            if collection is None:
                collection = bpy.data.collections.new(_CHECKPOINT_COLLECTION)
                bpy.context.scene.collection.children.link(collection)
                collection.hide_viewport = True
                collection.hide_render = True
            checkpoint = obj.copy()
            checkpoint.data = obj.data.copy()
            checkpoint.name = f"{obj.name}__checkpoint__{checkpoint_name}"
            checkpoint.data.name = f"{checkpoint.name}_Mesh"
            checkpoint[_CHECKPOINT_TARGET] = obj.name
            checkpoint[_CHECKPOINT_LABEL] = checkpoint_name
            checkpoint.hide_viewport = True
            checkpoint.hide_render = True
            checkpoint.hide_select = True
            collection.objects.link(checkpoint)
            return {
                "action": action,
                "name": obj.name,
                "checkpoint_name": checkpoint_name,
                "backup_object": checkpoint.name,
                "counts": mesh_counts(checkpoint),
                "topology_revision": topology_revision(checkpoint),
            }
        if checkpoint is None:
            raise ValueError(f"Checkpoint '{checkpoint_name}' not found for '{obj.name}'")
        if action == "COMPARE":
            current_counts = mesh_counts(obj)
            backup_counts = mesh_counts(checkpoint)
            current_revision = topology_revision(obj)
            backup_revision = topology_revision(checkpoint)
            current_modifiers = [_modifier_snapshot(modifier) for modifier in obj.modifiers]
            backup_modifiers = [_modifier_snapshot(modifier) for modifier in checkpoint.modifiers]
            current_attributes = [
                {"name": attribute.name, "domain": attribute.domain, "data_type": attribute.data_type}
                for attribute in obj.data.attributes
            ]
            backup_attributes = [
                {"name": attribute.name, "domain": attribute.domain, "data_type": attribute.data_type}
                for attribute in checkpoint.data.attributes
            ]
            transform_matches = obj.matrix_world == checkpoint.matrix_world
            return {
                "action": action,
                "name": obj.name,
                "checkpoint_name": checkpoint_name,
                "matches": (
                    current_revision == backup_revision
                    and transform_matches
                    and current_modifiers == backup_modifiers
                    and current_attributes == backup_attributes
                ),
                "current": {
                    "counts": current_counts,
                    "topology_revision": current_revision,
                    "modifiers": current_modifiers,
                    "attributes": current_attributes,
                },
                "checkpoint": {
                    "counts": backup_counts,
                    "topology_revision": backup_revision,
                    "modifiers": backup_modifiers,
                    "attributes": backup_attributes,
                },
                "count_delta": {key: current_counts[key] - backup_counts[key] for key in current_counts},
                "transform_matches": transform_matches,
            }
        if not confirm_destructive:
            raise ValueError(
                f"{action} requires confirm_destructive=True because it changes recoverable checkpoint data"
            )
        if action == "DELETE":
            backup_object_name = checkpoint.name
            backup_mesh = checkpoint.data
            bpy.data.objects.remove(checkpoint, do_unlink=True)
            if backup_mesh.users == 0:
                bpy.data.meshes.remove(backup_mesh)
            return {
                "action": action,
                "name": obj.name,
                "checkpoint_name": checkpoint_name,
                "backup_object": backup_object_name,
                "deleted": True,
            }

        previous_mesh = obj.data
        previous_name = previous_mesh.name
        obj.data = checkpoint.data.copy()
        obj.matrix_world = checkpoint.matrix_world.copy()
        for modifier in list(obj.modifiers):
            obj.modifiers.remove(modifier)
        for source_modifier in checkpoint.modifiers:
            destination = obj.modifiers.new(name=source_modifier.name, type=source_modifier.type)
            for prop in source_modifier.bl_rna.properties:
                if prop.identifier in {"rna_type", "name", "type"} or prop.is_readonly:
                    continue
                with contextlib.suppress(Exception):
                    setattr(destination, prop.identifier, getattr(source_modifier, prop.identifier))
        for group in list(obj.vertex_groups):
            obj.vertex_groups.remove(group)
        for source_group in checkpoint.vertex_groups:
            destination_group = obj.vertex_groups.new(name=source_group.name)
            for vertex in checkpoint.data.vertices:
                with contextlib.suppress(RuntimeError):
                    weight = source_group.weight(vertex.index)
                    destination_group.add([vertex.index], weight, "REPLACE")
        if previous_mesh.users == 0:
            bpy.data.meshes.remove(previous_mesh)
        obj.data.name = previous_name
        original_properties = {key: obj[key] for key in obj}
        try:
            for key in list(obj.keys()):
                del obj[key]
            for key in checkpoint:
                if key not in {_CHECKPOINT_TARGET, _CHECKPOINT_LABEL}:
                    obj[key] = checkpoint[key]
        except Exception:
            for key in list(obj.keys()):
                del obj[key]
            for key, value in original_properties.items():
                obj[key] = value
            raise
        return {
            "action": action,
            "name": obj.name,
            "checkpoint_name": checkpoint_name,
            "restored": True,
            "counts": mesh_counts(obj),
            "topology_revision": topology_revision(obj),
            "modifier_order": _modifier_order(obj),
        }
