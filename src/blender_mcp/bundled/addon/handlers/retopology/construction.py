# pyright: reportGeneralTypeIssues=false, reportOptionalSubscript=false
"""Blender-main-thread handlers for building new retopology geometry: patches, extensions, bridges, and guides."""

import itertools
import math

from collections import Counter

import bmesh
import bpy
import mathutils

from ...helpers import edit_mesh, get_mesh_object, mesh_counts
from ._shared import (
    _editable_bmesh,
    _ensure_indices,
    _finite,
    _modifier_order,
    _named_collection,
    _nearest_projection,
    _positive,
    _project_vertices,
    _read_bmesh,
    _require_finished,
    _require_name,
    _require_revision,
    _vector,
    _world_bvh,
    topology_revision,
)

_GUIDE_ROLES = {
    "EYE_LOOP",
    "MOUTH_LOOP",
    "JOINT_RING",
    "HARD_EDGE",
    "SEAM",
    "DENSITY_TRANSITION",
    "PANEL_BOUNDARY",
    "CUSTOM",
}


def _segment_distance(first_start, first_end, second_start, second_end):
    """Return the shortest distance between two finite 3D line segments."""
    first_direction = first_end - first_start
    second_direction = second_end - second_start
    offset = first_start - second_start
    first_length = first_direction.dot(first_direction)
    second_length = second_direction.dot(second_direction)
    cross = first_direction.dot(second_direction)
    first_offset = first_direction.dot(offset)
    second_offset = second_direction.dot(offset)
    denominator = first_length * second_length - cross * cross
    first_parameter = (
        0.0 if denominator <= 1e-20 else (cross * second_offset - first_offset * second_length) / denominator
    )
    first_parameter = max(0.0, min(1.0, first_parameter))
    second_parameter = 0.0 if second_length <= 1e-20 else (cross * first_parameter + second_offset) / second_length
    second_parameter = max(0.0, min(1.0, second_parameter))
    if first_length > 1e-20:
        first_parameter = max(0.0, min(1.0, (cross * second_parameter - first_offset) / first_length))
    return (first_start + first_direction * first_parameter - second_start - second_direction * second_parameter).length


def _curve_object(name, collection, points, cyclic, properties):
    curve = bpy.data.curves.new(name=f"{name}_Curve", type="CURVE")
    curve.dimensions = "3D"
    spline = curve.splines.new("POLY")
    spline.points.add(len(points) - 1)
    for item, coordinate in zip(spline.points, points, strict=True):
        item.co = (*coordinate, 1.0)
    spline.use_cyclic_u = bool(cyclic)
    obj = bpy.data.objects.new(name, curve)
    collection.objects.link(obj)
    for key, value in properties.items():
        obj[key] = value
    return obj


def _project_world_points(source, points, offset, max_distance=None):
    tree = _world_bvh(source)
    projected = []
    for index, point in enumerate(points):
        hit = _nearest_projection(tree, point, offset, max_distance)
        if hit is None:
            raise ValueError(f"Point {index} did not project within max_projection_distance")
        projected.append(hit[0])
    return projected


def _polyline_length(points, cyclic):
    pairs = list(zip(points, points[1:], strict=False))
    if cyclic and len(points) > 1:
        pairs.append((points[-1], points[0]))
    return sum((second - first).length for first, second in pairs)


def _resample_polyline(points, count, cyclic):
    if len(points) < 2:
        raise ValueError("A section component must contain at least two distinct points")
    segment_count = len(points) if cyclic else len(points) - 1
    lengths = [(points[(index + 1) % len(points)] - points[index]).length for index in range(segment_count)]
    total = sum(lengths)
    if total <= 1e-12:
        raise ValueError("Cannot resample a zero-length section")
    targets = [total * index / (count if cyclic else count - 1) for index in range(count)]
    result = []
    for target in targets:
        walked = 0.0
        for index, length in enumerate(lengths):
            if walked + length >= target or index == len(lengths) - 1:
                factor = 0.0 if length <= 1e-12 else min(1.0, (target - walked) / length)
                result.append(points[index].lerp(points[(index + 1) % len(points)], factor))
                break
            walked += length
    return result


def _ordered_edge_components(edges):
    remaining = set(edges)
    components = []
    while remaining:
        seed = min(remaining, key=lambda edge: edge.index)
        component = set()
        queue = [seed]
        while queue:
            edge = queue.pop()
            if edge in component:
                continue
            component.add(edge)
            queue.extend(
                linked
                for vertex in edge.verts
                for linked in vertex.link_edges
                if linked in remaining and linked not in component
            )
        remaining -= component
        degrees = {}
        for edge in component:
            for vertex in edge.verts:
                degrees[vertex] = degrees.get(vertex, 0) + 1
        if any(degree > 2 for degree in degrees.values()):
            continue
        endpoints = [vertex for vertex, degree in degrees.items() if degree == 1]
        cyclic = not endpoints
        current = min(endpoints or degrees, key=lambda vertex: vertex.index)
        start = current
        ordered = [current.co.copy()]
        unused = set(component)
        while unused:
            candidates = [edge for edge in current.link_edges if edge in unused]
            if not candidates:
                break
            edge = min(candidates, key=lambda candidate: candidate.index)
            unused.remove(edge)
            current = edge.other_vert(current)
            if current is not start:
                ordered.append(current.co.copy())
        components.append((ordered, cyclic))
    return components


def _mesh_attribute(mesh, name, data_type):
    attribute = mesh.attributes.get(name)
    if attribute is not None and (attribute.domain != "EDGE" or attribute.data_type != data_type):
        raise ValueError(f"Mesh attribute '{name}' exists but is not an EDGE/{data_type} attribute")
    return attribute or mesh.attributes.new(name=name, type=data_type, domain="EDGE")


def _curve_world_points(obj):
    if obj.type != "CURVE":
        raise ValueError(f"Guide object '{obj.name}' is not a Curve")
    result = []
    for spline in obj.data.splines:
        if spline.type == "BEZIER":
            result.extend(obj.matrix_world @ point.co for point in spline.bezier_points)
        else:
            result.extend(obj.matrix_world @ point.co.xyz for point in spline.points)
    return result


class _ConstructionMixin:
    """Provide handlers for building new retopology geometry from scratch or from guides."""

    def build_quad_patch(
        self,
        object_name,
        corners,
        u_segments,
        v_segments,
        source_object_name=None,
        coordinate_space="WORLD",
        interpolation="BILINEAR",
        boundary_u0=None,
        boundary_u1=None,
        boundary_v0=None,
        boundary_v1=None,
        projection_offset=0.0,
        expected_revision=None,
    ):
        obj = get_mesh_object(object_name)
        _require_revision(obj, expected_revision)
        if not isinstance(corners, (list, tuple)) or len(corners) != 4:
            raise ValueError("corners must contain exactly four points ordered [u0v0, u1v0, u1v1, u0v1]")
        corner_vectors = [_vector(point, f"corners[{index}]") for index, point in enumerate(corners)]
        u_segments, v_segments = int(u_segments), int(v_segments)
        if not 1 <= u_segments <= 1000 or not 1 <= v_segments <= 1000:
            raise ValueError("u_segments and v_segments must each be between 1 and 1000")
        if (u_segments + 1) * (v_segments + 1) > 250_000:
            raise ValueError("Requested patch exceeds the 250,000-vertex safety limit")
        coordinate_space = str(coordinate_space).upper()
        if coordinate_space not in {"LOCAL", "WORLD"}:
            raise ValueError("coordinate_space must be LOCAL or WORLD")
        inverse = obj.matrix_world.inverted_safe()
        if coordinate_space == "WORLD":
            corner_vectors = [inverse @ point for point in corner_vectors]
        interpolation = str(interpolation).upper()
        if interpolation not in {"BILINEAR", "COONS"}:
            raise ValueError("interpolation must be BILINEAR or COONS")
        guides = (boundary_u0, boundary_u1, boundary_v0, boundary_v1)
        if interpolation == "COONS" and any(guide is None or len(guide) < 2 for guide in guides):
            raise ValueError("COONS requires boundary_u0, boundary_u1, boundary_v0, and boundary_v1")

        def local_guide(points):
            converted = [_vector(point, "boundary guide") for point in points]
            return [inverse @ point for point in converted] if coordinate_space == "WORLD" else converted

        local_guides = tuple(local_guide(guide) for guide in guides) if interpolation == "COONS" else None
        if local_guides:
            expected = (
                (corner_vectors[0], corner_vectors[3]),
                (corner_vectors[1], corner_vectors[2]),
                (corner_vectors[0], corner_vectors[1]),
                (corner_vectors[3], corner_vectors[2]),
            )
            tolerance = max(1e-6, max((point.length for point in corner_vectors), default=1.0) * 1e-5)
            for guide, endpoints in zip(local_guides, expected, strict=True):
                if (guide[0] - endpoints[0]).length > tolerance or (guide[-1] - endpoints[1]).length > tolerance:
                    raise ValueError("A Coons boundary endpoint does not match its corresponding corner")
            for first_guide, second_guide in ((local_guides[0], local_guides[1]), (local_guides[2], local_guides[3])):
                if any(
                    _segment_distance(first_guide[a], first_guide[a + 1], second_guide[b], second_guide[b + 1])
                    <= tolerance
                    for a in range(len(first_guide) - 1)
                    for b in range(len(second_guide) - 1)
                ):
                    raise ValueError("Opposite Coons boundaries cross or touch")
        else:
            tolerance = max(1e-9, max((point.length for point in corner_vectors), default=1.0) * 1e-7)
            if (
                min(
                    (first - second).length
                    for index, first in enumerate(corner_vectors)
                    for second in corner_vectors[index + 1 :]
                )
                <= tolerance
            ):
                raise ValueError("Patch corners must be distinct")
            c00, c10, c11, c01 = corner_vectors
            if _segment_distance(c00, c10, c11, c01) <= tolerance or _segment_distance(c10, c11, c01, c00) <= tolerance:
                raise ValueError("Patch boundary edges cross")

        def sample_polyline(points, t):
            lengths = [(points[index + 1] - points[index]).length for index in range(len(points) - 1)]
            total = sum(lengths)
            if total <= 1e-12:
                return points[0].copy()
            target = t * total
            walked = 0.0
            for index, length in enumerate(lengths):
                if walked + length >= target or index == len(lengths) - 1:
                    factor = 0.0 if length <= 1e-12 else (target - walked) / length
                    return points[index].lerp(points[index + 1], factor)
                walked += length
            return points[-1].copy()

        created_verts = []
        created_faces = []
        projection_failed = []
        with _editable_bmesh(obj) as bm:
            old_vert_count, old_edge_count, old_face_count = len(bm.verts), len(bm.edges), len(bm.faces)
            grid = []
            c00, c10, c11, c01 = corner_vectors
            for v_index in range(v_segments + 1):
                v = v_index / v_segments
                row = []
                for u_index in range(u_segments + 1):
                    u = u_index / u_segments
                    bilinear = c00 * ((1 - u) * (1 - v)) + c10 * (u * (1 - v)) + c11 * (u * v) + c01 * ((1 - u) * v)
                    if interpolation == "COONS":
                        left = sample_polyline(local_guides[0], v)
                        right = sample_polyline(local_guides[1], v)
                        bottom = sample_polyline(local_guides[2], u)
                        top = sample_polyline(local_guides[3], u)
                        point = left * (1 - u) + right * u + bottom * (1 - v) + top * v - bilinear
                    else:
                        point = bilinear
                    vertex = bm.verts.new(point)
                    row.append(vertex)
                    created_verts.append(vertex)
                grid.append(row)
            for v_index in range(v_segments):
                for u_index in range(u_segments):
                    created_faces.append(
                        bm.faces.new(
                            (
                                grid[v_index][u_index],
                                grid[v_index][u_index + 1],
                                grid[v_index + 1][u_index + 1],
                                grid[v_index + 1][u_index],
                            )
                        )
                    )
            if source_object_name:
                projection_failed = _project_vertices(
                    obj, created_verts, get_mesh_object(source_object_name), projection_offset
                )
            bm.verts.index_update()
            bm.edges.index_update()
            bm.faces.index_update()
            vertex_indices = sorted(vertex.index for vertex in created_verts)
            face_indices = sorted(face.index for face in created_faces)
            edge_indices = sorted(edge.index for edge in bm.edges if edge.index >= old_edge_count)
        return {
            "name": obj.name,
            "created_vertex_indices": vertex_indices,
            "created_edge_indices": edge_indices,
            "created_face_indices": face_indices,
            "failed_projection_vertex_indices": projection_failed,
            "previous_counts": {"vertices": old_vert_count, "edges": old_edge_count, "polygons": old_face_count},
            "counts": mesh_counts(obj),
            "topology_revision": topology_revision(obj),
        }

    def extend_boundary(
        self,
        object_name,
        ordered_boundary_vertex_indices,
        rows=1,
        distance=0.1,
        mode="VERTEX_NORMAL",
        vector=(0.0, 0.0, 1.0),
        guide_points=None,
        source_object_name=None,
        projection_offset=0.0,
        expected_revision=None,
    ):
        obj = get_mesh_object(object_name)
        _require_revision(obj, expected_revision)
        indices = _ensure_indices(
            obj.data.vertices, ordered_boundary_vertex_indices, "ordered_boundary_vertex_indices", required=True
        )
        if len(indices) != len(ordered_boundary_vertex_indices):
            raise ValueError("ordered_boundary_vertex_indices must not contain duplicates")
        if len(indices) < 2:
            raise ValueError("ordered_boundary_vertex_indices must contain at least two vertices")
        rows = int(rows)
        if not 1 <= rows <= 1000:
            raise ValueError("rows must be between 1 and 1000")
        if rows * len(indices) > 250_000:
            raise ValueError("Requested extension exceeds the 250,000-vertex safety limit")
        distance = _positive(distance, "distance")
        mode = str(mode).upper()
        if mode not in {"FIXED_VECTOR", "VERTEX_NORMAL", "GUIDE_DIRECTED", "SURFACE_TANGENT"}:
            raise ValueError("Unsupported extension mode")
        extension_vector = _vector(vector, "vector", nonzero=mode in {"FIXED_VECTOR", "SURFACE_TANGENT"})
        if mode == "GUIDE_DIRECTED":
            if guide_points is None or len(guide_points) != len(indices):
                raise ValueError("GUIDE_DIRECTED requires one world-space guide point per boundary vertex")
            guides = [_vector(point, "guide_points") for point in guide_points]
        else:
            guides = None
        source = get_mesh_object(source_object_name) if source_object_name else None
        source_tree = _world_bvh(source) if source and mode == "SURFACE_TANGENT" else None
        created_verts = []
        created_faces = []
        projection_failed = []
        with _editable_bmesh(obj) as bm:
            boundary = [bm.verts[index] for index in indices]
            for first, second in itertools.pairwise(boundary):
                edge = bm.edges.get((first, second))
                if edge is None or not edge.is_boundary:
                    raise ValueError("Vertices do not form one ordered open manifold boundary")
            if bm.edges.get((boundary[-1], boundary[0])) is not None:
                raise ValueError("extend_boundary requires an open boundary, not a closed loop")
            old_edge_count = len(bm.edges)
            current = boundary
            inverse = obj.matrix_world.inverted_safe()
            for row_index in range(1, rows + 1):
                new_row = []
                for item_index, vertex in enumerate(current):
                    if mode == "FIXED_VECTOR":
                        delta = extension_vector.normalized() * distance
                    elif mode == "VERTEX_NORMAL":
                        normal = (
                            vertex.normal.normalized() if vertex.normal.length_squared else mathutils.Vector((0, 0, 1))
                        )
                        delta = normal * distance
                    elif mode == "GUIDE_DIRECTED":
                        target_local = inverse @ guides[item_index]
                        delta = (target_local - boundary[item_index].co) * (row_index / rows)
                        delta = target_local - vertex.co if row_index == rows else delta / row_index
                    else:
                        world = obj.matrix_world @ vertex.co
                        projection = _nearest_projection(source_tree, world)
                        if projection is None:
                            raise ValueError(f"Could not determine source tangent at boundary vertex {vertex.index}")
                        world_vector = obj.matrix_world.to_3x3() @ extension_vector
                        tangent = world_vector - projection[1] * world_vector.dot(projection[1])
                        if tangent.length <= 1e-12:
                            raise ValueError(
                                f"Extension vector is parallel to the source normal at vertex {vertex.index}"
                            )
                        delta = obj.matrix_world.to_3x3().inverted_safe() @ (tangent.normalized() * distance)
                    new_vertex = bm.verts.new(vertex.co + delta)
                    new_row.append(new_vertex)
                    created_verts.append(new_vertex)
                for index in range(len(current) - 1):
                    created_faces.append(
                        bm.faces.new((current[index], current[index + 1], new_row[index + 1], new_row[index]))
                    )
                current = new_row
            if source:
                projection_failed = _project_vertices(obj, created_verts, source, projection_offset)
            bm.verts.index_update()
            bm.edges.index_update()
            bm.faces.index_update()
            vertex_indices = sorted(vertex.index for vertex in created_verts)
            face_indices = sorted(face.index for face in created_faces)
            edge_indices = sorted(edge.index for edge in bm.edges if edge.index >= old_edge_count)
            new_boundary_indices = [vertex.index for vertex in current]
        return {
            "name": obj.name,
            "created_vertex_indices": vertex_indices,
            "created_edge_indices": edge_indices,
            "created_face_indices": face_indices,
            "failed_projection_vertex_indices": projection_failed,
            "new_boundary_vertex_indices": new_boundary_indices,
            "counts": mesh_counts(obj),
            "topology_revision": topology_revision(obj),
        }

    def mesh_bridge(
        self,
        object_name,
        loop_a_edge_indices=None,
        loop_b_edge_indices=None,
        edge_indices=None,
        cuts=0,
        interpolation="LINEAR",
        smoothness=0.0,
        twist_offset=0,
        expected_revision=None,
    ):
        obj = get_mesh_object(object_name)
        _require_revision(obj, expected_revision)
        if edge_indices is not None:
            if loop_a_edge_indices is not None or loop_b_edge_indices is not None:
                raise ValueError("Use either legacy edge_indices or the two separate loop inputs, not both")
            legacy = _ensure_indices(obj.data.edges, edge_indices, "edge_indices", required=True)
            with _read_bmesh(obj) as bm:
                selected = {bm.edges[index] for index in legacy}
                components = []
                while selected:
                    seed = selected.pop()
                    component = {seed}
                    queue = [seed]
                    while queue:
                        current = queue.pop()
                        linked = {
                            linked_edge
                            for vertex in current.verts
                            for linked_edge in vertex.link_edges
                            if linked_edge in selected
                        }
                        selected -= linked
                        component |= linked
                        queue.extend(linked)
                    components.append(sorted(item.index for item in component))
            if len(components) != 2:
                raise ValueError("Legacy edge_indices must contain exactly two disconnected boundary loops")
            loop_a_edge_indices, loop_b_edge_indices = components
        first = _ensure_indices(obj.data.edges, loop_a_edge_indices, "loop_a_edge_indices", required=True)
        second = _ensure_indices(obj.data.edges, loop_b_edge_indices, "loop_b_edge_indices", required=True)
        if len(first) != len(loop_a_edge_indices) or len(second) != len(loop_b_edge_indices):
            raise ValueError("Bridge loop inputs must not contain duplicate edge indices")

        def validate_loop(bm, indices, label):
            edges = [bm.edges[index] for index in indices]
            if any(not edge.is_boundary for edge in edges):
                raise ValueError(f"{label} contains an edge that is not a manifold boundary")
            for previous, current in itertools.pairwise(edges):
                if not set(previous.verts) & set(current.verts):
                    raise ValueError(f"{label} is not ordered: consecutive edges do not share a vertex")
            degrees = Counter(vertex for edge in edges for vertex in edge.verts)
            if any(degree > 2 for degree in degrees.values()):
                raise ValueError(f"{label} branches and is not a single loop/chain")
            endpoints = [vertex for vertex, degree in degrees.items() if degree == 1]
            if len(endpoints) not in {0, 2}:
                raise ValueError(f"{label} is neither a closed loop nor an open chain")
            if not endpoints and not (set(edges[-1].verts) & set(edges[0].verts)):
                raise ValueError(f"{label} is closed but the supplied order does not close")
            if len(edges) == 1:
                ordered_vertices = [edges[0].verts[0], edges[0].verts[1]]
            elif endpoints:
                start = next(vertex for vertex in edges[0].verts if vertex not in edges[1].verts)
                ordered_vertices = [start]
                for edge in edges:
                    ordered_vertices.append(edge.other_vert(ordered_vertices[-1]))
            else:
                start = next(vertex for vertex in edges[0].verts if vertex in edges[-1].verts)
                ordered_vertices = [start]
                for edge in edges:
                    ordered_vertices.append(edge.other_vert(ordered_vertices[-1]))
            winding = []
            for edge, start, end in zip(edges, ordered_vertices, ordered_vertices[1:], strict=True):
                face = edge.link_faces[0]
                loop = next(loop for loop in face.loops if loop.edge == edge)
                winding.append(loop.vert == start and loop.link_loop_next.vert == end)
            if len(set(winding)) > 1:
                raise ValueError(f"{label} has inconsistent face winding along its ordered boundary")
            return set(degrees), not endpoints, "FORWARD" if winding[0] else "REVERSED"

        with _read_bmesh(obj) as bm:
            first_vertices, first_closed, first_winding = validate_loop(bm, first, "loop_a_edge_indices")
            second_vertices, second_closed, second_winding = validate_loop(bm, second, "loop_b_edge_indices")
            if first_vertices & second_vertices:
                raise ValueError("Bridge loops may not share vertices")
            if first_closed != second_closed:
                raise ValueError("Both bridge inputs must be open chains or both must be closed loops")
            if any(
                any(vertex in first_vertices for vertex in face.verts)
                and any(vertex in second_vertices for vertex in face.verts)
                for face in bm.faces
            ):
                raise ValueError("The two boundaries already have faces spanning between them")
        cuts = int(cuts)
        if not 0 <= cuts <= 1000:
            raise ValueError("cuts must be between 0 and 1000")
        interpolation = str(interpolation).upper()
        if interpolation not in {"LINEAR", "PATH", "SURFACE"}:
            raise ValueError("interpolation must be LINEAR, PATH, or SURFACE")
        smoothness = _finite(smoothness, "smoothness")
        if not -1000.0 <= smoothness <= 1000.0:
            raise ValueError("smoothness must be between -1000 and 1000")
        before = mesh_counts(obj)
        with edit_mesh(obj, edge_indices=first + second):
            result = bpy.ops.mesh.bridge_edge_loops(
                type="SINGLE",
                twist_offset=int(twist_offset),
                number_cuts=cuts,
                interpolation=interpolation,
                smoothness=smoothness,
            )
            if "FINISHED" not in result:
                raise RuntimeError(f"mesh.bridge_edge_loops did not finish (status: {result})")
        after = mesh_counts(obj)
        return {
            "name": obj.name,
            **after,
            "created_vertex_indices": list(range(before["vertices"], after["vertices"])),
            "created_edge_indices": list(range(before["edges"], after["edges"])),
            "created_face_indices": list(range(before["polygons"], after["polygons"])),
            "input_winding": {"loop_a": first_winding, "loop_b": second_winding},
            "topology_revision": topology_revision(obj),
        }

    def fill_boundary_quads(
        self,
        object_name,
        boundary_edge_indices,
        span=1,
        offset=0,
        use_interp_simple=False,
        source_object_name=None,
        projection_offset=0.0,
        expected_revision=None,
    ):
        obj = get_mesh_object(object_name)
        _require_revision(obj, expected_revision)
        indices = _ensure_indices(obj.data.edges, boundary_edge_indices, "boundary_edge_indices", required=True)
        if len(indices) != len(boundary_edge_indices):
            raise ValueError("boundary_edge_indices must not contain duplicates")
        span = int(span)
        offset = int(offset)
        if not 1 <= span <= 1000:
            raise ValueError("span must be between 1 and 1000")
        if not -1000 <= offset <= 1000:
            raise ValueError("offset must be between -1000 and 1000")
        with _read_bmesh(obj) as bm:
            edges = [bm.edges[index] for index in indices]
            if any(not edge.is_boundary for edge in edges):
                raise ValueError("Every selected edge must be a manifold boundary edge")
            degrees = Counter(vertex for edge in edges for vertex in edge.verts)
            if any(degree != 2 for degree in degrees.values()):
                raise ValueError("boundary_edge_indices must form exactly one closed, non-branching boundary")
            if len(degrees) % 2:
                raise ValueError("Grid Fill requires an even boundary vertex count")
            reached = set()
            queue = [edges[0]]
            edge_set = set(edges)
            while queue:
                edge = queue.pop()
                if edge in reached:
                    continue
                reached.add(edge)
                queue.extend(link for vertex in edge.verts for link in vertex.link_edges if link in edge_set)
            if len(reached) != len(edges):
                raise ValueError("boundary_edge_indices contains more than one boundary")
        before = mesh_counts(obj)
        with edit_mesh(obj, edge_indices=indices):
            result = bpy.ops.mesh.fill_grid(span=span, offset=offset, use_interp_simple=bool(use_interp_simple))
            if "FINISHED" not in result:
                raise RuntimeError(f"mesh.fill_grid did not finish (status: {result})")
        after = mesh_counts(obj)
        new_faces = list(range(before["polygons"], after["polygons"]))
        if any(len(obj.data.polygons[index].vertices) != 4 for index in new_faces):
            raise RuntimeError("Grid Fill produced a non-quad face; the complete edit was rolled back")
        new_vertices = list(range(before["vertices"], after["vertices"]))
        projection_failed = []
        if source_object_name and new_vertices:
            source = get_mesh_object(source_object_name)
            with _editable_bmesh(obj) as bm:
                projection_failed = _project_vertices(
                    obj, [bm.verts[index] for index in new_vertices], source, projection_offset
                )
        return {
            "name": obj.name,
            **mesh_counts(obj),
            "created_vertex_indices": new_vertices,
            "created_edge_indices": list(range(before["edges"], after["edges"])),
            "created_face_indices": new_faces,
            "failed_projection_vertex_indices": projection_failed,
            "topology_revision": topology_revision(obj),
        }

    def create_retopology_guides(
        self,
        source_object_name,
        guides,
        collection_name="Retopology Guides",
        projection_offset=0.0,
        max_projection_distance=None,
    ):
        source = get_mesh_object(source_object_name)
        offset = _finite(projection_offset, "projection_offset")
        max_distance = (
            _positive(max_projection_distance, "max_projection_distance")
            if max_projection_distance is not None
            else None
        )
        if not isinstance(guides, list) or not guides:
            raise ValueError("guides must be a non-empty list")
        prepared = []
        for guide_index, guide in enumerate(guides):
            if not isinstance(guide, dict):
                raise ValueError(f"guides[{guide_index}] must be an object")
            name = _require_name(guide.get("name"), f"guides[{guide_index}].name")
            role = str(guide.get("role", "")).upper()
            if role not in _GUIDE_ROLES:
                raise ValueError(f"guides[{guide_index}].role must be one of {sorted(_GUIDE_ROLES)}")
            has_points = guide.get("points") is not None
            has_indices = guide.get("source_vertex_indices") is not None
            if has_points == has_indices:
                raise ValueError(f"guides[{guide_index}] needs exactly one of points or source_vertex_indices")
            if has_points:
                raw_points = guide["points"]
                if not isinstance(raw_points, list) or len(raw_points) < 2:
                    raise ValueError(f"guides[{guide_index}].points needs at least two world-space points")
                points = [_vector(point, f"guides[{guide_index}].points") for point in raw_points]
            else:
                indices = _ensure_indices(
                    source.data.vertices,
                    guide["source_vertex_indices"],
                    f"guides[{guide_index}].source_vertex_indices",
                    required=True,
                )
                if len(indices) < 2:
                    raise ValueError(f"guides[{guide_index}].source_vertex_indices needs at least two vertices")
                points = [source.matrix_world @ source.data.vertices[index].co for index in indices]
            prepared.append(
                (name, role, _project_world_points(source, points, offset, max_distance), bool(guide.get("cyclic")))
            )
        collection = _named_collection(collection_name)
        records = []
        for name, role, points, cyclic in prepared:
            obj = _curve_object(
                name,
                collection,
                points,
                cyclic,
                {
                    "blender_mcp_retopology_guide": True,
                    "blender_mcp_guide_role": role,
                    "blender_mcp_guide_source": source.name,
                },
            )
            records.append(
                {
                    "name": obj.name,
                    "role": role,
                    "cyclic": cyclic,
                    "point_count": len(points),
                    "world_points": [list(point) for point in points],
                }
            )
        return {
            "source": source.name,
            "collection": collection.name,
            "coordinate_space": "WORLD",
            "guides": records,
            "created_guide_objects": [record["name"] for record in records],
        }

    def create_surface_section(
        self,
        source_object_name,
        plane_origin,
        plane_normal,
        vertex_count,
        name=None,
        collection_name="Retopology Guides",
        component_index=0,
        cyclic=True,
        projection_offset=0.0,
    ):
        source = get_mesh_object(source_object_name)
        origin = _vector(plane_origin, "plane_origin")
        normal = _vector(plane_normal, "plane_normal", nonzero=True).normalized()
        count = int(vertex_count)
        selected_component = int(component_index)
        if count < 3 or count > 10000:
            raise ValueError("vertex_count must be between 3 and 10000")
        if selected_component < 0:
            raise ValueError("component_index must be non-negative")
        depsgraph = bpy.context.evaluated_depsgraph_get()
        evaluated = source.evaluated_get(depsgraph)
        mesh = evaluated.to_mesh()
        bm = bmesh.new()
        try:
            bm.from_mesh(mesh)
            bmesh.ops.transform(bm, matrix=evaluated.matrix_world, verts=bm.verts)
            cut = bmesh.ops.bisect_plane(
                bm,
                geom=[*bm.verts, *bm.edges, *bm.faces],
                dist=1e-6,
                plane_co=origin,
                plane_no=normal,
                clear_inner=False,
                clear_outer=False,
            )
            components = _ordered_edge_components(
                [element for element in cut["geom_cut"] if isinstance(element, bmesh.types.BMEdge)]
            )
        finally:
            bm.free()
            evaluated.to_mesh_clear()
        if not components:
            raise ValueError("The plane does not produce a usable edge section on the evaluated source")
        components.sort(key=lambda item: _polyline_length(item[0], item[1]), reverse=True)
        if selected_component >= len(components):
            raise ValueError(f"component_index {selected_component} is out of range for {len(components)} sections")
        points, detected_cyclic = components[selected_component]
        if cyclic and not detected_cyclic:
            raise ValueError("Selected section is open; call with cyclic=False or choose another component")
        projected = _project_world_points(
            source, _resample_polyline(points, count, bool(cyclic)), _finite(projection_offset, "projection_offset")
        )
        collection = _named_collection(collection_name)
        obj = _curve_object(
            name or f"{source.name}_Section",
            collection,
            projected,
            bool(cyclic),
            {
                "blender_mcp_retopology_guide": True,
                "blender_mcp_guide_role": "SECTION",
                "blender_mcp_guide_source": source.name,
            },
        )
        return {
            "source": source.name,
            "guide_object": obj.name,
            "collection": collection.name,
            "coordinate_space": "WORLD",
            "plane_origin": list(origin),
            "plane_normal": list(normal),
            "selected_component": selected_component,
            "detected_cyclic": detected_cyclic,
            "vertex_count": len(projected),
            "components": [
                {"index": index, "cyclic": item[1], "point_count": len(item[0]), "length": _polyline_length(*item)}
                for index, item in enumerate(components)
            ],
        }

    def set_retopology_features(
        self,
        object_name,
        edge_indices=None,
        detect_source_object_name=None,
        source_dihedral_angle=None,
        include_material_boundaries=False,
        guide_object_names=None,
        guide_distance=0.01,
        apply_detected=False,
        seam=None,
        sharp=None,
        crease=None,
        bevel_weight=None,
        expected_revision=None,
    ):
        obj = get_mesh_object(object_name)
        _require_revision(obj, expected_revision)
        explicit = set(_ensure_indices(obj.data.edges, edge_indices, "edge_indices"))
        if all(value is None for value in (seam, sharp, crease, bevel_weight)):
            raise ValueError("At least one of seam, sharp, crease, or bevel_weight must be supplied")
        if crease is not None and not 0.0 <= _finite(crease, "crease") <= 1.0:
            raise ValueError("crease must be between 0 and 1")
        if bevel_weight is not None and not 0.0 <= _finite(bevel_weight, "bevel_weight") <= 1.0:
            raise ValueError("bevel_weight must be between 0 and 1")
        distance = _positive(guide_distance, "guide_distance", allow_zero=True)
        candidates, candidate_reasons, source_features = set(), {}, []
        if source_dihedral_angle is not None or include_material_boundaries:
            if not detect_source_object_name:
                raise ValueError("detect_source_object_name is required for source-derived candidates")
            source = get_mesh_object(detect_source_object_name)
            threshold = None
            if source_dihedral_angle is not None:
                threshold = _finite(source_dihedral_angle, "source_dihedral_angle")
                if not 0.0 <= threshold <= math.pi:
                    raise ValueError("source_dihedral_angle must be between 0 and pi radians")
            with _read_bmesh(source) as bm:
                for edge in bm.edges:
                    reasons = []
                    if threshold is not None and len(edge.link_faces) == 2 and edge.calc_face_angle(0.0) >= threshold:
                        reasons.append("SOURCE_DIHEDRAL")
                    if (
                        include_material_boundaries
                        and len(edge.link_faces) == 2
                        and (edge.link_faces[0].material_index != edge.link_faces[1].material_index)
                    ):
                        reasons.append("SOURCE_MATERIAL_BOUNDARY")
                    if reasons:
                        point = source.matrix_world @ ((edge.verts[0].co + edge.verts[1].co) * 0.5)
                        source_features.append((point, reasons))
        guide_points = []
        for guide_name in guide_object_names or []:
            guide = bpy.data.objects.get(guide_name)
            if guide is None:
                raise ValueError(f"Guide object not found: {guide_name}")
            guide_points.extend((point, guide.name) for point in _curve_world_points(guide))
        for edge in obj.data.edges:
            point = obj.matrix_world @ (
                (obj.data.vertices[edge.vertices[0]].co + obj.data.vertices[edge.vertices[1]].co) * 0.5
            )
            reasons = [
                reason
                for source_point, source_reasons in source_features
                if (point - source_point).length <= distance
                for reason in source_reasons
            ]
            reasons.extend(
                f"GUIDE:{name}" for guide_point, name in guide_points if (point - guide_point).length <= distance
            )
            if reasons:
                candidates.add(edge.index)
                candidate_reasons[edge.index] = sorted(set(reasons))
        changed = sorted(explicit | (candidates if apply_detected else set()))
        if not changed:
            raise ValueError("No explicit edges were supplied and no detected candidates were activated")
        if seam is not None:
            for index in changed:
                obj.data.edges[index].use_seam = bool(seam)
        for attribute_name, data_type, value in (
            ("sharp_edge", "BOOLEAN", sharp),
            ("crease_edge", "FLOAT", crease),
            ("bevel_weight_edge", "FLOAT", bevel_weight),
        ):
            if value is not None:
                attribute = _mesh_attribute(obj.data, attribute_name, data_type)
                for index in changed:
                    attribute.data[index].value = value
        obj.data.update()
        return {
            "name": obj.name,
            "changed_edge_indices": changed,
            "explicit_edge_indices": sorted(explicit),
            "detected_candidate_edge_indices": sorted(candidates),
            "candidate_reasons": {str(index): candidate_reasons[index] for index in sorted(candidate_reasons)},
            "applied_detected": bool(apply_detected),
            "marks": {"seam": seam, "sharp": sharp, "crease": crease, "bevel_weight": bevel_weight},
            "topology_revision": topology_revision(obj),
        }

    def add_support_loops(
        self,
        object_name,
        edge_indices,
        width,
        side="BOTH",
        clamp=True,
        corner_policy="MITER",
        source_object_name=None,
        projection_offset=0.0,
        subdivision_levels=2,
        expected_revision=None,
    ):
        obj = get_mesh_object(object_name)
        _require_revision(obj, expected_revision)
        indices = _ensure_indices(obj.data.edges, edge_indices, "edge_indices", required=True)
        factor = _positive(width, "width")
        if factor > 10.0:
            raise ValueError("width is Blender's Edge Slide factor and must not exceed 10")
        side, corner_policy = str(side).upper(), str(corner_policy).upper()
        if side not in {"BOTH", "LEFT", "RIGHT"}:
            raise ValueError("side must be BOTH, LEFT, or RIGHT")
        if corner_policy not in {"MITER", "CAP_ENDPOINTS"}:
            raise ValueError("corner_policy must be MITER or CAP_ENDPOINTS")
        levels = int(subdivision_levels)
        if not 0 <= levels <= 6:
            raise ValueError("subdivision_levels must be between 0 and 6")
        with _read_bmesh(obj) as bm:
            selected = [bm.edges[index] for index in indices]
            if any(not edge.is_manifold for edge in selected):
                raise ValueError("Support-loop input edges must all be manifold")
            degree = {}
            for edge in selected:
                for vertex in edge.verts:
                    degree[vertex] = degree.get(vertex, 0) + 1
            if any(value > 2 for value in degree.values()):
                raise ValueError("Support-loop input must contain non-branching edge chains")
        before = mesh_counts(obj)
        with edit_mesh(obj, edge_indices=indices):
            result = bpy.ops.mesh.offset_edge_loops_slide(
                MESH_OT_offset_edge_loops={"use_cap_endpoint": corner_policy == "CAP_ENDPOINTS"},
                TRANSFORM_OT_edge_slide={
                    "value": -factor if side == "LEFT" else factor,
                    "single_side": side != "BOTH",
                    "use_even": True,
                    "flipped": side == "LEFT",
                    "use_clamp": bool(clamp),
                    "correct_uv": True,
                },
            )
            _require_finished(result, "Offset Edge Loops")
        after = mesh_counts(obj)
        created_vertices = list(range(before["vertices"], after["vertices"]))
        created_edges = list(range(before["edges"], after["edges"]))
        created_faces = list(range(before["polygons"], after["polygons"]))
        source = get_mesh_object(source_object_name) if source_object_name else None
        projection_failed = []
        if source and created_vertices:
            with _editable_bmesh(obj) as bm:
                projection_failed = _project_vertices(
                    obj, [bm.verts[index] for index in created_vertices], source, projection_offset
                )
        with _read_bmesh(obj) as bm:
            invalid = [edge.index for edge in bm.edges if len(edge.link_faces) > 2]
        if invalid:
            raise ValueError(f"Support loops would leave non-manifold edges: {invalid[:20]}")
        subdivision = next((modifier for modifier in obj.modifiers if modifier.type == "SUBSURF"), None)
        if levels:
            subdivision = subdivision or obj.modifiers.new(name="RetopologySubdivision", type="SUBSURF")
            subdivision.levels = levels
            subdivision.render_levels = levels
        return {
            "name": obj.name,
            "counts_before": before,
            "counts_after": after,
            "created_vertex_indices": created_vertices,
            "created_edge_indices": created_edges,
            "created_face_indices": created_faces,
            "failed_projection_vertex_indices": projection_failed,
            "manifold": not invalid,
            "subdivision_modifier": subdivision.name if subdivision else None,
            "modifier_order": _modifier_order(obj),
            "topology_revision": topology_revision(obj),
        }
