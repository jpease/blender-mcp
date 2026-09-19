# pyright: reportGeneralTypeIssues=false, reportOptionalSubscript=false
"""Blender-main-thread handlers for editing existing retopology geometry: projection, rerouting, relaxing, symmetry."""

import contextlib
import itertools

import bmesh
import bpy
import mathutils

from ...helpers import apply_modifier, get_mesh_object, mesh_counts, sync_from_editmode
from ._shared import (
    _configure_shrinkwrap_modifier,
    _editable_bmesh,
    _ensure_indices,
    _finite,
    _kd_tree_class,
    _modifier_order,
    _move_before_subdivision,
    _nearest_projection,
    _positive,
    _project_vertices,
    _require_revision,
    _vector,
    _world_bvh,
    topology_revision,
)


def _ray_projection(
    tree,
    world_point,
    direction,
    max_distance,
    positive_direction,
    negative_direction,
    backface_policy,
    offset,
):
    limit = float(max_distance) if max_distance is not None else 1.84467e19
    candidates = []
    for sign, enabled in ((1.0, positive_direction), (-1.0, negative_direction)):
        if not enabled:
            continue
        ray = direction.normalized() * sign
        hit, normal, face_index, distance = tree.ray_cast(world_point, ray, limit)
        if hit is None:
            continue
        if backface_policy == "CULL" and normal.dot(-ray) <= 0.0:
            continue
        candidates.append((float(distance), hit + normal.normalized() * offset, normal.normalized(), face_index))
    if not candidates:
        return None
    distance, hit, normal, face_index = min(candidates, key=lambda item: item[0])
    return hit, normal, face_index, distance


def _perform_reroute(bm, action, vertex_indices, edge_indices, cuts):
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    vertices = [bm.verts[index] for index in vertex_indices]
    edges = [bm.edges[index] for index in edge_indices]
    if action == "CONNECT":
        if len(vertices) != 2:
            raise ValueError("CONNECT requires exactly two vertex_indices")
        if bm.edges.get(vertices) is not None:
            raise ValueError("CONNECT vertices already share an edge")
        return bmesh.ops.connect_vert_pair(bm, verts=vertices)
    if action == "ROTATE_DIAGONAL":
        if (
            len(edges) != 1
            or len(edges[0].link_faces) != 2
            or any(len(face.verts) != 3 for face in edges[0].link_faces)
        ):
            raise ValueError("ROTATE_DIAGONAL requires one interior edge shared by exactly two triangles")
        return bmesh.ops.rotate_edges(bm, edges=edges, use_ccw=False)
    if action == "COLLAPSE":
        if not edges:
            raise ValueError("COLLAPSE requires edge_indices")
        return bmesh.ops.collapse(bm, edges=edges, uvs=True)
    if action == "DISSOLVE":
        if not edges:
            raise ValueError("DISSOLVE requires edge_indices")
        return bmesh.ops.dissolve_edges(bm, edges=edges, use_verts=False, use_face_split=False)
    if action == "SPLIT":
        if not edges:
            raise ValueError("SPLIT requires edge_indices")
        if not 1 <= cuts <= 1000:
            raise ValueError("cuts must be between 1 and 1000")
        return bmesh.ops.subdivide_edges(bm, edges=edges, cuts=cuts, use_grid_fill=False)
    raise ValueError("action must be CONNECT, ROTATE_DIAGONAL, COLLAPSE, DISSOLVE, or SPLIT")


def _validate_reroute_result(bm):
    if any(len(edge.link_faces) > 2 for edge in bm.edges):
        raise ValueError("Action would create a non-manifold edge with more than two incident faces")
    seen_faces = set()
    for face in bm.faces:
        key = frozenset(vertex.index for vertex in face.verts)
        if key in seen_faces:
            raise ValueError("Action would create duplicate faces")
        seen_faces.add(key)
        if len(key) < 3 or face.calc_area() <= 1e-12:
            raise ValueError("Action would create a degenerate face")


class _EditingMixin:
    """Provide handlers for editing existing retopology geometry."""

    def configure_surface_projection(
        self,
        object_name,
        target_object_name,
        modifier_name="RetopologyProjection",
        wrap_method="NEAREST_SURFACEPOINT",
        wrap_mode="ON_SURFACE",
        offset=0.0,
        project_limit=0.0,
        project_axes=(False, False, True),
        positive_direction=True,
        negative_direction=True,
        cull_face="OFF",
        invert_cull=False,
        auxiliary_target_name=None,
        vertex_group="",
        invert_vertex_group=False,
        apply=False,
    ):
        obj = get_mesh_object(object_name)
        target = get_mesh_object(target_object_name)
        if obj == target:
            raise ValueError("target_object_name must differ from object_name")
        if not isinstance(modifier_name, str) or not modifier_name.strip():
            raise ValueError("modifier_name must be a non-empty string")
        wrap_method = str(wrap_method).upper()
        if wrap_method not in {"NEAREST_SURFACEPOINT", "PROJECT", "NEAREST_VERTEX", "TARGET_PROJECT"}:
            raise ValueError("Unsupported wrap_method")
        wrap_mode = str(wrap_mode).upper()
        if wrap_mode not in {"ON_SURFACE", "INSIDE", "OUTSIDE", "OUTSIDE_SURFACE", "ABOVE_SURFACE"}:
            raise ValueError("Unsupported wrap_mode")
        cull_face = str(cull_face).upper()
        if cull_face not in {"OFF", "FRONT", "BACK"}:
            raise ValueError("cull_face must be OFF, FRONT, or BACK")
        if len(project_axes) != 3:
            raise ValueError("project_axes must contain exactly three booleans")
        if not any(project_axes) and wrap_method == "PROJECT":
            raise ValueError("PROJECT wrap_method requires at least one projection axis")
        if vertex_group and obj.vertex_groups.get(vertex_group) is None:
            raise ValueError(f"Vertex group not found on '{obj.name}': {vertex_group}")
        auxiliary = None
        if auxiliary_target_name:
            auxiliary = get_mesh_object(auxiliary_target_name)
            if auxiliary == obj:
                raise ValueError("auxiliary_target_name must differ from object_name")
        modifier = _configure_shrinkwrap_modifier(
            obj,
            target,
            modifier_name,
            wrap_method,
            wrap_mode,
            _finite(offset, "offset"),
            _positive(project_limit, "project_limit", allow_zero=True),
            tuple(bool(value) for value in project_axes),
            bool(positive_direction),
            bool(negative_direction),
            cull_face,
            bool(invert_cull),
            auxiliary,
            vertex_group,
            bool(invert_vertex_group),
        )
        if apply:
            apply_modifier(obj, modifier)
        return {
            "name": obj.name,
            "target": target.name,
            "modifier": None if apply else modifier.name,
            "applied": bool(apply),
            "modifier_order": _modifier_order(obj),
            "topology_revision": topology_revision(obj),
        }

    def project_mesh_elements(
        self,
        object_name,
        source_object_name,
        vertex_indices=None,
        vertex_group=None,
        method="NEAREST",
        direction=(0.0, 0.0, -1.0),
        direction_space="WORLD",
        offset=0.0,
        max_distance=None,
        positive_direction=True,
        negative_direction=False,
        backface_policy="ALLOW",
        preserve_boundary=False,
        preserve_symmetry_axis="NONE",
        symmetry_tolerance=0.0001,
        expected_revision=None,
    ):
        obj = get_mesh_object(object_name)
        source = get_mesh_object(source_object_name)
        _require_revision(obj, expected_revision)
        if (vertex_indices is None) == (vertex_group is None):
            raise ValueError("Provide exactly one of vertex_indices or vertex_group")
        indices = _ensure_indices(obj.data.vertices, vertex_indices, "vertex_indices")
        if vertex_group is not None:
            group = obj.vertex_groups.get(vertex_group)
            if group is None:
                raise ValueError(f"Vertex group not found on '{obj.name}': {vertex_group}")
            indices = []
            for vertex in obj.data.vertices:
                with contextlib.suppress(RuntimeError):
                    if group.weight(vertex.index) > 0.0:
                        indices.append(vertex.index)
        if not indices:
            raise ValueError("No vertices were selected for projection")
        method = str(method).upper()
        if method not in {"NEAREST", "RAYCAST"}:
            raise ValueError("method must be NEAREST or RAYCAST")
        direction_space = str(direction_space).upper()
        if direction_space not in {"LOCAL", "WORLD"}:
            raise ValueError("direction_space must be LOCAL or WORLD")
        ray = _vector(direction, "direction", nonzero=method == "RAYCAST")
        if direction_space == "LOCAL":
            ray = obj.matrix_world.to_3x3() @ ray
        if method == "RAYCAST" and not (positive_direction or negative_direction):
            raise ValueError("RAYCAST requires positive_direction and/or negative_direction")
        backface_policy = str(backface_policy).upper()
        if backface_policy not in {"ALLOW", "CULL"}:
            raise ValueError("backface_policy must be ALLOW or CULL")
        if max_distance is not None:
            max_distance = _positive(max_distance, "max_distance")
        symmetry_axis = str(preserve_symmetry_axis).upper()
        if symmetry_axis not in {"NONE", "X", "Y", "Z"}:
            raise ValueError("preserve_symmetry_axis must be NONE, X, Y, or Z")
        symmetry_tolerance = _positive(symmetry_tolerance, "symmetry_tolerance", allow_zero=True)
        tree = _world_bvh(source)
        projected_indices = []
        failed = []
        preserved = []
        with _editable_bmesh(obj) as bm:
            boundary = {vertex.index for vertex in bm.verts if any(edge.is_boundary for edge in vertex.link_edges)}
            inverse = obj.matrix_world.inverted_safe()
            axis_index = "XYZ".find(symmetry_axis)
            for index in indices:
                vertex = bm.verts[index]
                if preserve_boundary and index in boundary:
                    preserved.append(index)
                    continue
                if axis_index >= 0 and abs(vertex.co[axis_index]) <= symmetry_tolerance:
                    preserved.append(index)
                    continue
                world = obj.matrix_world @ vertex.co
                if method == "NEAREST":
                    projection = _nearest_projection(tree, world, offset, max_distance)
                else:
                    projection = _ray_projection(
                        tree,
                        world,
                        ray,
                        max_distance,
                        positive_direction,
                        negative_direction,
                        backface_policy,
                        offset,
                    )
                if projection is None:
                    failed.append(index)
                    continue
                vertex.co = inverse @ projection[0]
                projected_indices.append(index)
        return {
            "name": obj.name,
            "source": source.name,
            "projected_vertex_indices": projected_indices,
            "preserved_vertex_indices": preserved,
            "failed_vertex_indices": failed,
            "topology_revision": topology_revision(obj),
        }

    def reroute_topology(
        self,
        object_name,
        action,
        vertex_indices=None,
        edge_indices=None,
        cuts=1,
        expected_revision=None,
    ):
        obj = get_mesh_object(object_name)
        _require_revision(obj, expected_revision)
        vertices = _ensure_indices(obj.data.vertices, vertex_indices, "vertex_indices")
        edges = _ensure_indices(obj.data.edges, edge_indices, "edge_indices")
        action = str(action).upper()
        cuts = int(cuts)
        simulation = bmesh.new()
        try:
            simulation.from_mesh(obj.data)
            simulation.verts.ensure_lookup_table()
            simulation.edges.ensure_lookup_table()
            original_vertices = set(simulation.verts)
            boundary_before = {
                vertex for vertex in simulation.verts if any(edge.is_boundary for edge in vertex.link_edges)
            }
            selected_vertices = {simulation.verts[index] for index in vertices}
            selected_edges = {simulation.edges[index] for index in edges}
            affected_vertices = selected_vertices | {vertex for edge in selected_edges for vertex in edge.verts}
            affected_vertices |= {
                vertex for edge in selected_edges for face in edge.link_faces for vertex in face.verts
            }
            selected_boundary = any(edge.is_boundary for edge in selected_edges)
            _perform_reroute(simulation, action, vertices, edges, cuts)
            simulation.verts.index_update()
            simulation.edges.index_update()
            simulation.faces.index_update()
            _validate_reroute_result(simulation)
            boundary_after = {
                vertex for vertex in simulation.verts if any(edge.is_boundary for edge in vertex.link_edges)
            }
            unintended_existing = (boundary_after & original_vertices) - boundary_before - affected_vertices
            unintended_new = boundary_after - original_vertices
            if unintended_existing or (unintended_new and not (action == "SPLIT" and selected_boundary)):
                raise ValueError("Action would create an unintended boundary outside the selected neighborhood")
        finally:
            simulation.free()
        before = mesh_counts(obj)
        with _editable_bmesh(obj) as bm:
            _perform_reroute(bm, action, vertices, edges, cuts)
            bm.verts.index_update()
            bm.edges.index_update()
            bm.faces.index_update()
            _validate_reroute_result(bm)
            after_counts = {"vertices": len(bm.verts), "edges": len(bm.edges), "polygons": len(bm.faces)}
        return {
            "name": obj.name,
            "action": action,
            **mesh_counts(obj),
            "created_vertex_indices": list(range(before["vertices"], after_counts["vertices"])),
            "created_edge_indices": list(range(before["edges"], after_counts["edges"])),
            "created_face_indices": list(range(before["polygons"], after_counts["polygons"])),
            "removed_input_vertex_indices": vertices if after_counts["vertices"] < before["vertices"] else [],
            "removed_input_edge_indices": edges if after_counts["edges"] < before["edges"] else [],
            "topology_revision": topology_revision(obj),
        }

    def relax_topology(
        self,
        object_name,
        vertex_indices,
        iterations=3,
        factor=0.5,
        lock_boundary=True,
        lock_vertex_group=None,
        source_object_name=None,
        projection_offset=0.0,
        expected_revision=None,
    ):
        obj = get_mesh_object(object_name)
        _require_revision(obj, expected_revision)
        indices = _ensure_indices(obj.data.vertices, vertex_indices, "vertex_indices", required=True)
        iterations = int(iterations)
        if not 1 <= iterations <= 100:
            raise ValueError("iterations must be between 1 and 100")
        factor = _finite(factor, "factor")
        if not 0.0 <= factor <= 1.0:
            raise ValueError("factor must be between 0 and 1")
        locked = set()
        if lock_vertex_group:
            group = obj.vertex_groups.get(lock_vertex_group)
            if group is None:
                raise ValueError(f"Vertex group not found on '{obj.name}': {lock_vertex_group}")
            for index in indices:
                with contextlib.suppress(RuntimeError):
                    if group.weight(index) > 0.0:
                        locked.add(index)
        source = get_mesh_object(source_object_name) if source_object_name else None
        moved = set(indices) - locked
        projection_failed = set()
        with _editable_bmesh(obj) as bm:
            if lock_boundary:
                locked |= {index for index in indices if any(edge.is_boundary for edge in bm.verts[index].link_edges)}
                moved -= locked
            selected_vertices = [bm.verts[index] for index in indices]
            for _ in range(iterations):
                bm.normal_update()
                updates = {}
                for vertex in selected_vertices:
                    if vertex.index in locked or not vertex.link_edges:
                        continue
                    center = sum(
                        (edge.other_vert(vertex).co for edge in vertex.link_edges), mathutils.Vector((0.0, 0.0, 0.0))
                    ) / len(vertex.link_edges)
                    delta = center - vertex.co
                    normal = vertex.normal.normalized() if vertex.normal.length_squared else mathutils.Vector()
                    tangent = delta - normal * delta.dot(normal)
                    updates[vertex] = vertex.co + tangent * factor
                for vertex, coordinate in updates.items():
                    vertex.co = coordinate
                if source and updates:
                    projection_failed.update(_project_vertices(obj, list(updates), source, projection_offset))
        return {
            "name": obj.name,
            "moved_vertex_indices": sorted(moved),
            "locked_vertex_indices": sorted(locked),
            "iterations": iterations,
            "failed_projection_vertex_indices": sorted(projection_failed),
            "topology_revision": topology_revision(obj),
        }

    def redistribute_edge_loop(
        self,
        object_name,
        loop_vertex_indices,
        closed=False,
        preserve_endpoints=True,
        corner_vertex_indices=None,
        source_object_name=None,
        projection_offset=0.0,
        expected_revision=None,
    ):
        obj = get_mesh_object(object_name)
        _require_revision(obj, expected_revision)
        indices = _ensure_indices(obj.data.vertices, loop_vertex_indices, "loop_vertex_indices", required=True)
        if len(indices) != len(loop_vertex_indices):
            raise ValueError("loop_vertex_indices must not contain duplicates")
        minimum = 3 if closed else 2
        if len(indices) < minimum:
            raise ValueError(f"A {'closed' if closed else 'open'} loop requires at least {minimum} vertices")
        corners = set(_ensure_indices(obj.data.vertices, corner_vertex_indices, "corner_vertex_indices"))
        if not corners.issubset(indices):
            raise ValueError("corner_vertex_indices must be members of loop_vertex_indices")
        source = get_mesh_object(source_object_name) if source_object_name else None
        projection_failed = []

        def resample(points, count, cyclic=False):
            segment_count = len(points) if cyclic else len(points) - 1
            lengths = [(points[(index + 1) % len(points)] - points[index]).length for index in range(segment_count)]
            total = sum(lengths)
            if total <= 1e-12:
                return [point.copy() for point in points]
            targets = [total * index / (count if cyclic else count - 1) for index in range(count)]
            result = []
            for target in targets:
                walked = 0.0
                for index, length in enumerate(lengths):
                    if walked + length >= target or index == len(lengths) - 1:
                        factor = 0.0 if length <= 1e-12 else (target - walked) / length
                        result.append(points[index].lerp(points[(index + 1) % len(points)], factor))
                        break
                    walked += length
            return result

        with _editable_bmesh(obj) as bm:
            vertices = [bm.verts[index] for index in indices]
            pairs = list(itertools.pairwise(vertices))
            if closed:
                pairs.append((vertices[-1], vertices[0]))
            for first, second in pairs:
                if bm.edges.get((first, second)) is None:
                    raise ValueError("loop_vertex_indices are not ordered along existing connected edges")
            original = [vertex.co.copy() for vertex in vertices]
            if not corners:
                coordinates = resample(original, len(original), cyclic=bool(closed))
                for vertex, coordinate in zip(vertices, coordinates, strict=True):
                    vertex.co = coordinate
            else:
                protected_positions = sorted(indices.index(index) for index in corners)
                if not closed:
                    if preserve_endpoints or len(protected_positions) < 2:
                        protected_positions = sorted({0, len(indices) - 1, *protected_positions})
                    for start, end in itertools.pairwise(protected_positions):
                        coordinates = resample(original[start : end + 1], end - start + 1)
                        for position, coordinate in enumerate(coordinates, start):
                            vertices[position].co = coordinate
                elif len(protected_positions) == 1:
                    start = protected_positions[0]
                    rotated_vertices = vertices[start:] + vertices[:start]
                    rotated_points = original[start:] + original[:start]
                    coordinates = resample(rotated_points, len(rotated_points), cyclic=True)
                    for vertex, coordinate in zip(rotated_vertices, coordinates, strict=True):
                        vertex.co = coordinate
                else:
                    positions = [*protected_positions, protected_positions[0] + len(vertices)]
                    extended_vertices = vertices + vertices
                    extended_points = original + original
                    for start, end in itertools.pairwise(positions):
                        coordinates = resample(extended_points[start : end + 1], end - start + 1)
                        for position, coordinate in enumerate(coordinates, start):
                            extended_vertices[position].co = coordinate
            if source:
                projection_failed = _project_vertices(obj, vertices, source, projection_offset)
        return {
            "name": obj.name,
            "redistributed_vertex_indices": indices,
            "preserved_corner_vertex_indices": sorted(corners),
            "closed": bool(closed),
            "failed_projection_vertex_indices": projection_failed,
            "topology_revision": topology_revision(obj),
        }

    def configure_retopology_symmetry(
        self,
        object_name,
        axis="X",
        mirror_object_name=None,
        source_side="POSITIVE",
        bisect=False,
        clipping=True,
        merge=True,
        merge_tolerance=0.001,
        mirror_vertex_groups=True,
        validate_seam=True,
        symmetry_tolerance=0.001,
        modifier_name="RetopologyMirror",
    ):
        obj = get_mesh_object(object_name)
        axis = str(axis).upper()
        if axis not in {"X", "Y", "Z"}:
            raise ValueError("axis must be X, Y, or Z")
        axis_index = "XYZ".index(axis)
        source_side = str(source_side).upper()
        if source_side not in {"POSITIVE", "NEGATIVE"}:
            raise ValueError("source_side must be POSITIVE or NEGATIVE")
        merge_tolerance = _positive(merge_tolerance, "merge_tolerance", allow_zero=True)
        symmetry_tolerance = _positive(symmetry_tolerance, "symmetry_tolerance", allow_zero=True)
        mirror_object = None
        if mirror_object_name:
            mirror_object = bpy.data.objects.get(mirror_object_name)
            if mirror_object is None:
                raise ValueError(f"Mirror object not found: {mirror_object_name}")
            if mirror_object == obj:
                raise ValueError("mirror_object_name must differ from object_name")
        modifier = obj.modifiers.get(modifier_name)
        if modifier is not None and modifier.type != "MIRROR":
            raise ValueError(f"Modifier '{modifier_name}' exists but is type {modifier.type}, not MIRROR")
        if modifier is None:
            modifier = obj.modifiers.new(name=modifier_name, type="MIRROR")
        modifier.use_axis = tuple(index == axis_index for index in range(3))
        modifier.mirror_object = mirror_object
        modifier.use_bisect_axis = tuple(bool(bisect) and index == axis_index for index in range(3))
        # Blender's manual: default bisect keeps the positive side; Flip keeps negative.
        modifier.use_bisect_flip_axis = tuple(
            bool(bisect) and source_side == "NEGATIVE" and index == axis_index for index in range(3)
        )
        modifier.use_clip = bool(clipping)
        modifier.use_mirror_merge = bool(merge)
        modifier.merge_threshold = merge_tolerance
        modifier.use_mirror_vertex_groups = bool(mirror_vertex_groups)
        _move_before_subdivision(obj, modifier)
        unmatched = []
        seam_outside = []
        if validate_seam:
            sync_from_editmode(obj)
            coordinates = []
            if mirror_object:
                plane_inverse = mirror_object.matrix_world.inverted_safe()
                coordinates = [plane_inverse @ (obj.matrix_world @ vertex.co) for vertex in obj.data.vertices]
            else:
                coordinates = [vertex.co.copy() for vertex in obj.data.vertices]
            tree = _kd_tree_class()(len(coordinates))
            for index, coordinate in enumerate(coordinates):
                tree.insert(coordinate, index)
            tree.balance()
            for index, coordinate in enumerate(coordinates):
                mirrored = coordinate.copy()
                mirrored[axis_index] *= -1.0
                _hit, _matched_index, distance = tree.find(mirrored)
                if distance is None or distance > symmetry_tolerance:
                    unmatched.append(index)
                if abs(coordinate[axis_index]) <= merge_tolerance and abs(coordinate[axis_index]) > symmetry_tolerance:
                    seam_outside.append(index)
        return {
            "name": obj.name,
            "modifier": modifier.name,
            "axis": axis,
            "plane_space": "MIRROR_OBJECT_LOCAL" if mirror_object else "OBJECT_LOCAL",
            "source_side": source_side,
            "unmatched_vertex_indices": unmatched,
            "seam_vertices_outside_tolerance": seam_outside,
            "modifier_order": _modifier_order(obj),
            "topology_revision": topology_revision(obj),
        }
