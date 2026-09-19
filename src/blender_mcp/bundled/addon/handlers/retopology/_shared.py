# pyright: reportGeneralTypeIssues=false, reportOptionalSubscript=false
"""Geometry, validation, and BVH/BMesh helpers shared by two or more retopology topic modules."""

import contextlib
import hashlib
import math

import bmesh
import bpy
import mathutils

from ...helpers import preserve_mode_and_selection, sync_from_editmode

_TRANSFER_TYPES = {
    "VERTEX_GROUPS",
    "UVS",
    "COLOR_ATTRIBUTES",
    "CUSTOM_NORMALS",
    "SEAMS",
    "CREASES",
    "BEVEL_WEIGHTS",
    "SHARP_EDGES",
    "SMOOTH_SHADING",
    "MATERIAL_INDICES",
}


def _bvh_class():
    from mathutils.bvhtree import BVHTree

    return BVHTree


def _kd_tree_class():
    from mathutils.kdtree import KDTree

    return KDTree


def _finite(value, label):
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{label} must be finite")
    return value


def _positive(value, label, *, allow_zero=False):
    value = _finite(value, label)
    if value < 0 if allow_zero else value <= 0:
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{label} must be {qualifier}")
    return value


def _vector(value, label, *, nonzero=False):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError(f"{label} must contain exactly three numbers")
    vector = mathutils.Vector(tuple(_finite(component, label) for component in value))
    if nonzero and vector.length <= 1e-12:
        raise ValueError(f"{label} must not be zero")
    return vector


def topology_revision(obj):
    """Return a deterministic hash over base-mesh connectivity and coordinates."""
    sync_from_editmode(obj)
    digest = hashlib.blake2b(digest_size=16)
    mesh = obj.data
    digest.update(f"{len(mesh.vertices)}:{len(mesh.edges)}:{len(mesh.polygons)}|".encode())
    for vertex in mesh.vertices:
        digest.update(("{:.9g},{:.9g},{:.9g};".format(*tuple(vertex.co))).encode())
    for edge in mesh.edges:
        digest.update(f"{edge.vertices[0]},{edge.vertices[1]};".encode())
    for polygon in mesh.polygons:
        digest.update((",".join(str(index) for index in polygon.vertices) + ";").encode())
    return digest.hexdigest()


def _require_revision(obj, expected_revision):
    current = topology_revision(obj)
    if expected_revision is not None and expected_revision != current:
        raise ValueError(
            f"Stale topology revision for '{obj.name}': expected {expected_revision}, current {current}. "
            "Call inspect_retopology again and use its indices/revision."
        )
    return current


def _ensure_indices(elements, indices, label, *, required=False):
    if indices is None:
        if required:
            raise ValueError(f"{label} is required")
        return []
    if not isinstance(indices, (list, tuple)):
        raise ValueError(f"{label} must be a list of integer indices")
    result = []
    seen = set()
    total = len(elements)
    for raw in indices:
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise ValueError(f"{label} contains non-integer index {raw!r}")
        if raw < 0 or raw >= total:
            raise ValueError(f"Index {raw} out of range for {label} (0-{total - 1})")
        if raw not in seen:
            result.append(raw)
            seen.add(raw)
    if required and not result:
        raise ValueError(f"{label} must not be empty")
    return result


@contextlib.contextmanager
def _editable_bmesh(obj):
    """Edit a base mesh through an owned BMesh while preserving UI context."""
    sync_from_editmode(obj)
    with preserve_mode_and_selection():
        bm = bmesh.new()
        try:
            bm.from_mesh(obj.data)
            bm.verts.ensure_lookup_table()
            bm.edges.ensure_lookup_table()
            bm.faces.ensure_lookup_table()
            yield bm
            bm.normal_update()
            bm.to_mesh(obj.data)
            obj.data.update()
        finally:
            bm.free()


@contextlib.contextmanager
def _read_bmesh(obj):
    sync_from_editmode(obj)
    bm = bmesh.new()
    try:
        bm.from_mesh(obj.data)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
        bm.normal_update()
        yield bm
    finally:
        bm.free()


def _world_bvh(source):
    """Build a BVH in world coordinates from the dependency-graph result."""
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = source.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh()
    try:
        vertices = [evaluated.matrix_world @ vertex.co for vertex in mesh.vertices]
        polygons = [tuple(polygon.vertices) for polygon in mesh.polygons if len(polygon.vertices) >= 3]
        if not polygons:
            raise ValueError(f"Evaluated source '{source.name}' has no faces to project onto")
        return _bvh_class().FromPolygons(vertices, polygons, all_triangles=False, epsilon=0.0)
    finally:
        evaluated.to_mesh_clear()


def _nearest_projection(tree, world_point, offset=0.0, max_distance=None):
    distance_limit = float(max_distance) if max_distance is not None else 1.84467e19
    hit, normal, face_index, distance = tree.find_nearest(world_point, distance_limit)
    if hit is None:
        return None
    return hit + normal.normalized() * offset, normal.normalized(), face_index, float(distance)


def _project_vertices(obj, vertices, source, offset=0.0):
    tree = _world_bvh(source)
    inverse = obj.matrix_world.inverted_safe()
    failed = []
    for vertex in vertices:
        projected = _nearest_projection(tree, obj.matrix_world @ vertex.co, offset)
        if projected is None:
            failed.append(vertex.index)
            continue
        vertex.co = inverse @ projected[0]
    return failed


def _modifier_order(obj):
    return [
        {"index": index, "name": modifier.name, "type": modifier.type} for index, modifier in enumerate(obj.modifiers)
    ]


def _move_before_subdivision(obj, modifier):
    subdivisions = [index for index, candidate in enumerate(obj.modifiers) if candidate.type == "SUBSURF"]
    if subdivisions:
        obj.modifiers.move(obj.modifiers.find(modifier.name), min(subdivisions))


def _configure_shrinkwrap_modifier(
    obj,
    target,
    modifier_name,
    wrap_method,
    wrap_mode,
    offset,
    project_limit,
    project_axes,
    positive_direction,
    negative_direction,
    cull_face,
    invert_cull,
    auxiliary_target,
    vertex_group,
    invert_vertex_group,
):
    modifier = obj.modifiers.get(modifier_name)
    if modifier is not None and modifier.type != "SHRINKWRAP":
        raise ValueError(f"Modifier '{modifier_name}' exists but is type {modifier.type}, not SHRINKWRAP")
    if modifier is None:
        modifier = obj.modifiers.new(name=modifier_name, type="SHRINKWRAP")
    modifier.target = target
    modifier.wrap_method = wrap_method
    modifier.wrap_mode = wrap_mode
    modifier.offset = offset
    modifier.project_limit = project_limit
    modifier.use_project_x, modifier.use_project_y, modifier.use_project_z = project_axes
    modifier.use_positive_direction = positive_direction
    modifier.use_negative_direction = negative_direction
    modifier.cull_face = cull_face
    modifier.use_invert_cull = invert_cull
    modifier.auxiliary_target = auxiliary_target
    modifier.vertex_group = vertex_group
    modifier.invert_vertex_group = invert_vertex_group
    _move_before_subdivision(obj, modifier)
    return modifier


def _percentile(values, percent):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percent / 100.0
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (upper - position) + ordered[upper] * (position - lower)


def _named_collection(name):
    if not isinstance(name, str) or not name.strip():
        raise ValueError("collection_name must be a non-empty string")
    collection = bpy.data.collections.get(name)
    if collection is None:
        collection = bpy.data.collections.new(name)
        bpy.context.scene.collection.children.link(collection)
    return collection


def _require_name(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def _require_finished(result, operation):
    if not result or "FINISHED" not in result:
        raise RuntimeError(f"{operation} did not finish (operator returned {sorted(result or [])})")


def _self_intersections(bm, limit):
    if not bm.faces:
        return []
    tree = _bvh_class().FromBMesh(bm, epsilon=0.0)
    intersections = []
    for first, second in tree.overlap(tree):
        if first >= second:
            continue
        first_face, second_face = bm.faces[first], bm.faces[second]
        if set(first_face.verts) & set(second_face.verts):
            continue
        intersections.append([first, second])
        if len(intersections) >= limit:
            break
    return intersections


def _uv_overlap_pairs(mesh, limit):
    uv_layer = mesh.uv_layers.active
    if uv_layer is None:
        return []
    mesh.calc_loop_triangles()
    triangles = []
    for triangle in mesh.loop_triangles:
        coordinates = [uv_layer.data[loop_index].uv.copy() for loop_index in triangle.loops]
        triangles.append((triangle.polygon_index, coordinates))
    if len(triangles) > 2500:
        return None

    def overlap(first, second):
        for triangle in (first, second):
            for index in range(3):
                edge = triangle[(index + 1) % 3] - triangle[index]
                axis = mathutils.Vector((-edge.y, edge.x))
                if axis.length_squared <= 1e-20:
                    return False
                first_projection = [point.dot(axis) for point in first]
                second_projection = [point.dot(axis) for point in second]
                amount = min(max(first_projection), max(second_projection)) - max(
                    min(first_projection), min(second_projection)
                )
                if amount <= 1e-10:
                    return False
        return True

    pairs = []
    seen = set()
    for first_index, (first_polygon, first_uvs) in enumerate(triangles):
        for second_polygon, second_uvs in triangles[first_index + 1 :]:
            if first_polygon == second_polygon:
                continue
            pair = tuple(sorted((first_polygon, second_polygon)))
            if pair in seen:
                continue
            if overlap(first_uvs, second_uvs):
                seen.add(pair)
                pairs.append(list(pair))
                if len(pairs) >= limit:
                    return pairs
    return pairs
