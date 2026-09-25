# pyright: reportGeneralTypeIssues=false, reportOptionalSubscript=false
"""Blender-main-thread handlers for specialized retopology accelerators."""

import contextlib
import math
import statistics

import bmesh
import bpy
import mathutils

from ...helpers import apply_modifier, get_mesh_object, mesh_counts, preserve_mode_and_selection, set_active
from ._shared import (
    _TRANSFER_TYPES,
    _editable_bmesh,
    _ensure_indices,
    _finite,
    _kd_tree_class,
    _modifier_order,
    _named_collection,
    _positive,
    _project_vertices,
    _require_finished,
    _require_revision,
    topology_revision,
)

_PROFILES = {"CHARACTER", "HARD_SURFACE", "VFX", "GAME"}
_LOD_METHODS = {"DECIMATE", "QUADRIFLOW"}
_QUADRIFLOW_MODES = {"RATIO", "EDGE", "FACES"}
_LOD_LEVEL_KEYS = {
    "name",
    "ratio",
    "method",
    "use_symmetry",
    "symmetry_axis",
    "vertex_group",
    "vertex_group_factor",
    "invert_vertex_group",
    "seed",
    "preserve_sharp",
    "preserve_boundary",
    "preserve_attributes",
    "smooth_normals",
}


def _integer(value, label, minimum, maximum=None):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        suffix = f" between {minimum} and {maximum}" if maximum is not None else f" at least {minimum}"
        raise ValueError(f"{label} must be{suffix}")
    return value


def _validate_profile(value):
    profile = str(value).upper()
    if profile not in _PROFILES:
        raise ValueError(f"profile must be one of {sorted(_PROFILES)}")
    return profile


def _evaluated_mesh_copy(source, name, collection):
    """Create an independent object from source's current evaluated mesh."""
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = source.evaluated_get(depsgraph)
    mesh = bpy.data.meshes.new_from_object(evaluated, preserve_all_data_layers=True, depsgraph=depsgraph)
    mesh.name = f"{name}_Mesh"
    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)
    obj.matrix_world = source.matrix_world.copy()
    for group in source.vertex_groups:
        obj.vertex_groups.new(name=group.name)
    return obj


def _data_layer_snapshot(obj):
    mesh = obj.data
    builtins = {"position", ".edge_verts", ".corner_vert", ".corner_edge"}
    return {
        "uv_layers": sorted(layer.name for layer in mesh.uv_layers),
        "color_attributes": sorted(attribute.name for attribute in mesh.color_attributes),
        "attributes": sorted(
            attribute.name
            for attribute in mesh.attributes
            if attribute.name not in builtins and not attribute.name.startswith(".")
        ),
        "vertex_groups": sorted(group.name for group in obj.vertex_groups),
        "materials": [slot.material.name if slot.material else None for slot in obj.material_slots],
    }


def _lost_data_layers(before, after):
    lost = {}
    for key in before:
        before_values = set(before[key])
        after_values = set(after[key])
        missing = sorted(before_values - after_values, key=lambda value: "" if value is None else str(value))
        if missing:
            lost[key] = missing
    return lost


def _run_quadriflow(obj, settings):
    with preserve_mode_and_selection():
        set_active(obj)
        result = bpy.ops.object.quadriflow_remesh(
            use_mesh_symmetry=settings["use_mesh_symmetry"],
            use_preserve_sharp=settings["preserve_sharp"],
            use_preserve_boundary=settings["preserve_boundary"],
            preserve_attributes=settings["preserve_attributes"],
            smooth_normals=settings["smooth_normals"],
            mode=settings["mode"],
            target_ratio=settings["target_ratio"],
            target_edge_length=settings["target_edge_length"],
            target_faces=settings["target_faces"],
            seed=settings["seed"],
        )
        _require_finished(result, "QuadriFlow Remesh")


def _quadriflow_settings(
    mode,
    target_faces,
    target_ratio,
    target_edge_length,
    use_mesh_symmetry,
    preserve_sharp,
    preserve_boundary,
    preserve_attributes,
    smooth_normals,
    seed,
):
    mode = str(mode).upper()
    if mode not in _QUADRIFLOW_MODES:
        raise ValueError(f"mode must be one of {sorted(_QUADRIFLOW_MODES)}")
    faces = _integer(target_faces, "target_faces", 1)
    ratio = _positive(target_ratio, "target_ratio")
    edge_length = _positive(target_edge_length, "target_edge_length")
    seed = _integer(seed, "seed", 0)
    return {
        "mode": mode,
        "target_faces": faces,
        "target_ratio": ratio,
        "target_edge_length": edge_length,
        "use_mesh_symmetry": bool(use_mesh_symmetry),
        "preserve_sharp": bool(preserve_sharp),
        "preserve_boundary": bool(preserve_boundary),
        "preserve_attributes": bool(preserve_attributes),
        "smooth_normals": bool(smooth_normals),
        "seed": seed,
    }


def _mean_point(points):
    return sum(points, mathutils.Vector((0.0, 0.0, 0.0))) / len(points)


def _canonical_direction(vector):
    direction = vector.normalized()
    dominant = max(range(3), key=lambda index: abs(direction[index]))
    if direction[dominant] < 0.0:
        direction.negate()
    return direction


def _principal_axes(points):
    """Return ascending covariance eigenpairs using a bounded Jacobi solve."""
    center = _mean_point(points)
    covariance = [[0.0] * 3 for _ in range(3)]
    for point in points:
        delta = point - center
        for row in range(3):
            for column in range(3):
                covariance[row][column] += delta[row] * delta[column]
    scale = 1.0 / len(points)
    matrix = [[value * scale for value in row] for row in covariance]
    vectors = [[1.0 if row == column else 0.0 for column in range(3)] for row in range(3)]
    for _iteration in range(32):
        row, column = max(((0, 1), (0, 2), (1, 2)), key=lambda pair: abs(matrix[pair[0]][pair[1]]))
        if abs(matrix[row][column]) <= 1e-14:
            break
        angle = 0.5 * math.atan2(2.0 * matrix[row][column], matrix[column][column] - matrix[row][row])
        cosine, sine = math.cos(angle), math.sin(angle)
        old = [item[:] for item in matrix]
        for index in range(3):
            matrix[index][row] = cosine * old[index][row] - sine * old[index][column]
            matrix[index][column] = sine * old[index][row] + cosine * old[index][column]
        old = [item[:] for item in matrix]
        for index in range(3):
            matrix[row][index] = cosine * old[row][index] - sine * old[column][index]
            matrix[column][index] = sine * old[row][index] + cosine * old[column][index]
        matrix[row][column] = matrix[column][row] = 0.0
        for index in range(3):
            old_row, old_column = vectors[index][row], vectors[index][column]
            vectors[index][row] = cosine * old_row - sine * old_column
            vectors[index][column] = sine * old_row + cosine * old_column
    pairs = []
    for column in range(3):
        vector = mathutils.Vector(tuple(vectors[row][column] for row in range(3)))
        pairs.append((max(0.0, float(matrix[column][column])), _canonical_direction(vector)))
    return center, sorted(pairs, key=lambda item: item[0])


def _orthogonal_frame(axis):
    reference = min(
        (mathutils.Vector((1.0, 0.0, 0.0)), mathutils.Vector((0.0, 1.0, 0.0)), mathutils.Vector((0.0, 0.0, 1.0))),
        key=lambda candidate: abs(axis.dot(candidate)),
    )
    first = _canonical_direction(axis.cross(reference))
    second = axis.cross(first).normalized()
    return first, second


def _residual_statistics(values):
    return {
        "mean": statistics.fmean(values),
        "rms": math.sqrt(statistics.fmean(value * value for value in values)),
        "maximum": max(values),
    }


def _fit_plane(points):
    center, eigenpairs = _principal_axes(points)
    extent_squared = max((point - center).length_squared for point in points)
    if eigenpairs[1][0] <= max(1e-16, extent_squared * 1e-10):
        raise ValueError("PLANE fit is ambiguous because the samples are collinear or coincident")
    normal = eigenpairs[0][1]
    first = eigenpairs[2][1]
    second = normal.cross(first).normalized()
    u_values = [(point - center).dot(first) for point in points]
    v_values = [(point - center).dot(second) for point in points]
    residuals = [abs((point - center).dot(normal)) for point in points]
    return {
        "center": center,
        "axis": normal,
        "basis_u": first,
        "basis_v": second,
        "u_range": (min(u_values), max(u_values)),
        "v_range": (min(v_values), max(v_values)),
        "residuals": residuals,
    }


def _axis_model(points, axis, cone):
    center = _mean_point(points)
    axial = [(point - center).dot(axis) for point in points]
    radial = [((point - center) - axis * distance).length for point, distance in zip(points, axial, strict=True)]
    if cone:
        mean_axis = statistics.fmean(axial)
        mean_radius = statistics.fmean(radial)
        denominator = sum((value - mean_axis) ** 2 for value in axial)
        if denominator <= 1e-16:
            return None
        slope = (
            sum((distance - mean_axis) * (radius - mean_radius) for distance, radius in zip(axial, radial, strict=True))
            / denominator
        )
        intercept = mean_radius - slope * mean_axis
        predicted = [intercept + slope * distance for distance in axial]
    else:
        slope = 0.0
        intercept = statistics.fmean(radial)
        predicted = [intercept] * len(radial)
    residuals = [abs(actual - expected) for actual, expected in zip(radial, predicted, strict=True)]
    return {
        "center": center,
        "axis": axis,
        "z_range": (min(axial), max(axial)),
        "radius_intercept": intercept,
        "radius_slope": slope,
        "residuals": residuals,
    }


def _fit_axis_surface(points, cone, axis_hint):
    center, eigenpairs = _principal_axes(points)
    del center
    candidates = [_canonical_direction(axis_hint)] if axis_hint is not None else [pair[1] for pair in eigenpairs]
    models = [model for axis in candidates if (model := _axis_model(points, axis, cone)) is not None]
    if not models:
        raise ValueError("Primitive fit is degenerate: samples have no measurable axial span")
    models.sort(key=lambda item: _residual_statistics(item["residuals"])["rms"])
    selected = models[0]
    z_min, z_max = selected["z_range"]
    if z_max - z_min <= 1e-8:
        raise ValueError("Primitive fit is degenerate: samples have no measurable axial span")
    if axis_hint is None and len(models) > 1:
        best = _residual_statistics(models[0]["residuals"])["rms"]
        second = _residual_statistics(models[1]["residuals"])["rms"]
        scale = max(statistics.fmean(abs(value) for value in selected["residuals"]), 1.0)
        if second - best <= max(scale * 1e-6, best * 0.05):
            raise ValueError("Primitive axis is ambiguous; supply axis_hint_world to disambiguate the fit")
    radius_min = selected["radius_intercept"] + selected["radius_slope"] * z_min
    radius_max = selected["radius_intercept"] + selected["radius_slope"] * z_max
    if min(radius_min, radius_max) <= 1e-8:
        raise ValueError("Primitive fit produced a zero or negative end radius")
    if cone and abs(radius_max - radius_min) <= max(radius_min, radius_max) * 1e-4:
        raise ValueError("CONE fit is indistinguishable from a cylinder for these samples")
    selected["radius_range"] = (radius_min, radius_max)
    return selected


def _solve_linear_system(matrix, values):
    size = len(values)
    augmented = [list(matrix[row]) + [values[row]] for row in range(size)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) <= 1e-12:
            raise ValueError("SPHERE fit is ambiguous because the samples are coplanar or otherwise singular")
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        augmented[column] = [value / divisor for value in augmented[column]]
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            augmented[row] = [
                value - factor * pivot_value
                for value, pivot_value in zip(augmented[row], augmented[column], strict=True)
            ]
    return [augmented[index][-1] for index in range(size)]


def _fit_sphere(points):
    rows = [[2.0 * point.x, 2.0 * point.y, 2.0 * point.z, 1.0] for point in points]
    values = [point.length_squared for point in points]
    normal_matrix = [[sum(row[i] * row[j] for row in rows) for j in range(4)] for i in range(4)]
    normal_values = [sum(row[i] * value for row, value in zip(rows, values, strict=True)) for i in range(4)]
    solution = _solve_linear_system(normal_matrix, normal_values)
    center = mathutils.Vector(solution[:3])
    radius_squared = center.length_squared + solution[3]
    if radius_squared <= 1e-16:
        raise ValueError("SPHERE fit produced a non-positive radius")
    radius = math.sqrt(radius_squared)
    return {
        "center": center,
        "radius": radius,
        "residuals": [abs((point - center).length - radius) for point in points],
    }


def _grid_faces(u_count, v_count, *, wrap_u=False):
    faces = []
    columns = u_count if wrap_u else u_count + 1
    for v_index in range(v_count):
        for u_index in range(u_count):
            next_u = (u_index + 1) % columns
            first = v_index * columns + u_index
            second = v_index * columns + next_u
            third = (v_index + 1) * columns + next_u
            fourth = (v_index + 1) * columns + u_index
            faces.append((first, second, third, fourth))
    return faces


def _plane_geometry(fit, u_segments, v_segments):
    vertices = []
    u_min, u_max = fit["u_range"]
    v_min, v_max = fit["v_range"]
    if u_max - u_min <= 1e-8 or v_max - v_min <= 1e-8:
        raise ValueError("PLANE fit has zero width or height")
    for v_index in range(v_segments + 1):
        v_value = v_min + (v_max - v_min) * v_index / v_segments
        for u_index in range(u_segments + 1):
            u_value = u_min + (u_max - u_min) * u_index / u_segments
            vertices.append(fit["center"] + fit["basis_u"] * u_value + fit["basis_v"] * v_value)
    return vertices, _grid_faces(u_segments, v_segments)


def _axis_geometry(fit, u_segments, v_segments):
    first, second = _orthogonal_frame(fit["axis"])
    z_min, z_max = fit["z_range"]
    vertices = []
    for v_index in range(v_segments + 1):
        z_value = z_min + (z_max - z_min) * v_index / v_segments
        radius = fit["radius_intercept"] + fit["radius_slope"] * z_value
        ring_center = fit["center"] + fit["axis"] * z_value
        for u_index in range(u_segments):
            angle = math.tau * u_index / u_segments
            vertices.append(ring_center + radius * (math.cos(angle) * first + math.sin(angle) * second))
    return vertices, _grid_faces(u_segments, v_segments, wrap_u=True)


def _sphere_geometry(fit, subdivisions):
    """Create a welded all-quad cube sphere in world space."""
    face_coordinates = (
        lambda u, v: (subdivisions, u, v),
        lambda u, v: (-subdivisions, u, -v),
        lambda u, v: (-u, subdivisions, v),
        lambda u, v: (u, -subdivisions, v),
        lambda u, v: (u, v, subdivisions),
        lambda u, v: (u, -v, -subdivisions),
    )
    vertices = []
    faces = []
    indices = {}
    values = [(-subdivisions + 2 * index) for index in range(subdivisions + 1)]
    for coordinate in face_coordinates:
        grid = []
        for v_value in values:
            row = []
            for u_value in values:
                key = coordinate(u_value, v_value)
                if key not in indices:
                    direction = mathutils.Vector(key).normalized()
                    indices[key] = len(vertices)
                    vertices.append(fit["center"] + direction * fit["radius"])
                row.append(indices[key])
            grid.append(row)
        for v_index in range(subdivisions):
            for u_index in range(subdivisions):
                faces.append(
                    (
                        grid[v_index][u_index],
                        grid[v_index][u_index + 1],
                        grid[v_index + 1][u_index + 1],
                        grid[v_index + 1][u_index],
                    )
                )
    return vertices, faces


def _create_world_geometry_object(name, collection, source, vertices, faces):
    inverse = source.matrix_world.inverted_safe()
    mesh = bpy.data.meshes.new(f"{name}_Mesh")
    mesh.from_pydata([inverse @ point for point in vertices], [], faces)
    mesh.update(calc_edges=True)
    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)
    obj.matrix_world = source.matrix_world.copy()
    return obj


def _target_preflight(target, duplicate_tolerance):
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = target.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh()
    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        bm.edges.ensure_lookup_table()
        non_manifold = [edge.index for edge in bm.edges if len(edge.link_faces) > 2]
        world_vertices = [evaluated.matrix_world @ vertex.co for vertex in mesh.vertices]
        tree = _kd_tree_class()(len(world_vertices))
        for index, point in enumerate(world_vertices):
            tree.insert(point, index)
        tree.balance()
        doubles = set()
        for index, point in enumerate(world_vertices):
            for _coordinate, other, _distance in tree.find_range(point, duplicate_tolerance):
                if other != index:
                    doubles.add(tuple(sorted((index, other))))
        concave = []
        collinear = []
        for polygon in mesh.polygons:
            points = [mesh.vertices[index].co for index in polygon.vertices]
            if len(points) < 3:
                collinear.append(polygon.index)
                continue
            face_scale = max(
                ((second - first).length for first, second in zip(points, points[1:] + points[:1], strict=True)),
                default=1.0,
            )
            threshold = max(1e-12, face_scale * face_scale * 1e-10)
            for index, current in enumerate(points):
                previous = points[index - 1]
                following = points[(index + 1) % len(points)]
                turn = (current - previous).cross(following - current)
                if turn.length <= threshold:
                    collinear.append(polygon.index)
                    break
                if turn.dot(polygon.normal) < -threshold:
                    concave.append(polygon.index)
                    break
        report = {
            "coordinate_space": "EVALUATED_TARGET; duplicate_tolerance is world-space",
            "vertices": len(mesh.vertices),
            "edges": len(mesh.edges),
            "polygons": len(mesh.polygons),
            "non_manifold_edge_count": len(non_manifold),
            "non_manifold_edge_indices": non_manifold[:100],
            "concave_face_count": len(set(concave)),
            "concave_face_indices": sorted(set(concave))[:100],
            "duplicate_pair_count": len(doubles),
            "duplicate_vertex_pairs": [list(pair) for pair in sorted(doubles)[:100]],
            "collinear_face_count": len(set(collinear)),
            "collinear_face_indices": sorted(set(collinear))[:100],
        }
        if not mesh.polygons:
            raise ValueError(f"Surface Deform target '{target.name}' evaluates to no faces")
        problems = []
        for key, label in (
            ("non_manifold_edge_count", "edges with more than two faces"),
            ("concave_face_count", "concave faces"),
            ("duplicate_pair_count", "overlapping vertex pairs"),
            ("collinear_face_count", "faces with collinear edges"),
        ):
            if report[key]:
                problems.append(f"{report[key]} {label}")
        if problems:
            raise ValueError(f"Surface Deform target '{target.name}' is invalid: {', '.join(problems)}")
        return report
    finally:
        bm.free()
        evaluated.to_mesh_clear()


def _prepare_lod_levels(levels, master, base_faces):
    if not isinstance(levels, list) or not levels:
        raise ValueError("levels must be a non-empty list")
    if len(levels) > 8:
        raise ValueError("levels is limited to 8 LODs per request")
    prepared = []
    previous_ratio = 1.0
    for index, raw in enumerate(levels):
        if not isinstance(raw, dict):
            raise ValueError(f"levels[{index}] must be an object")
        unknown = set(raw) - _LOD_LEVEL_KEYS
        if unknown:
            raise ValueError(f"levels[{index}] has unknown fields: {sorted(unknown)}")
        ratio = _finite(raw.get("ratio"), f"levels[{index}].ratio")
        if not 0.0 < ratio < 1.0:
            raise ValueError(f"levels[{index}].ratio must be between 0 and 1 (exclusive)")
        if ratio >= previous_ratio:
            raise ValueError("LOD ratios must be strictly decreasing in list order")
        previous_ratio = ratio
        method = str(raw.get("method", "DECIMATE")).upper()
        if method not in _LOD_METHODS:
            raise ValueError(f"levels[{index}].method must be one of {sorted(_LOD_METHODS)}")
        name = raw.get("name", f"{master.name}_LOD{index + 1}")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"levels[{index}].name must be a non-empty string")
        symmetry_axis = str(raw.get("symmetry_axis", "X")).upper()
        if symmetry_axis not in {"X", "Y", "Z"}:
            raise ValueError(f"levels[{index}].symmetry_axis must be X, Y, or Z")
        vertex_group = raw.get("vertex_group")
        if vertex_group is not None and master.vertex_groups.get(vertex_group) is None:
            raise ValueError(f"levels[{index}].vertex_group '{vertex_group}' does not exist on '{master.name}'")
        prepared.append(
            {
                "name": name.strip(),
                "ratio": ratio,
                "method": method,
                "target_faces": max(1, round(base_faces * ratio)),
                "use_symmetry": bool(raw.get("use_symmetry", False)),
                "symmetry_axis": symmetry_axis,
                "vertex_group": vertex_group or "",
                "vertex_group_factor": _positive(
                    raw.get("vertex_group_factor", 1.0), f"levels[{index}].vertex_group_factor", allow_zero=True
                ),
                "invert_vertex_group": bool(raw.get("invert_vertex_group", False)),
                "seed": _integer(raw.get("seed", 0), f"levels[{index}].seed", 0),
                "preserve_sharp": bool(raw.get("preserve_sharp", False)),
                "preserve_boundary": bool(raw.get("preserve_boundary", False)),
                "preserve_attributes": bool(raw.get("preserve_attributes", True)),
                "smooth_normals": bool(raw.get("smooth_normals", True)),
            }
        )
    return prepared


def _lod_transfer_types(master, transfer_data_types):
    requested_types = {str(value).upper() for value in (transfer_data_types or [])}
    unknown_types = requested_types - _TRANSFER_TYPES
    if unknown_types:
        raise ValueError(f"Unknown transfer_data_types: {sorted(unknown_types)}")
    if "UVS" in requested_types and not master.data.uv_layers:
        raise ValueError(f"Master '{master.name}' has no UV layers to transfer")
    if "VERTEX_GROUPS" in requested_types and not master.vertex_groups:
        raise ValueError(f"Master '{master.name}' has no vertex groups to transfer")
    if "COLOR_ATTRIBUTES" in requested_types and not master.data.color_attributes:
        raise ValueError(f"Master '{master.name}' has no color attributes to transfer")
    if "MATERIAL_INDICES" in requested_types and not master.material_slots:
        raise ValueError(f"Master '{master.name}' has no materials to transfer")
    return requested_types


def _evaluated_counts(obj):
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = obj.evaluated_get(depsgraph)
    evaluated_mesh = evaluated.to_mesh()
    try:
        return {
            "vertices": len(evaluated_mesh.vertices),
            "edges": len(evaluated_mesh.edges),
            "polygons": len(evaluated_mesh.polygons),
        }
    finally:
        evaluated.to_mesh_clear()


def _reduce_lod(lod, spec):
    if spec["method"] == "DECIMATE":
        modifier = lod.modifiers.new(name="RetopologyLODDecimate", type="DECIMATE")
        modifier.decimate_type = "COLLAPSE"
        modifier.ratio = spec["ratio"]
        modifier.use_collapse_triangulate = False
        modifier.use_symmetry = spec["use_symmetry"]
        modifier.symmetry_axis = spec["symmetry_axis"]
        modifier.vertex_group = spec["vertex_group"]
        modifier.vertex_group_factor = spec["vertex_group_factor"]
        modifier.invert_vertex_group = spec["invert_vertex_group"]
        apply_modifier(lod, modifier)
        return
    _run_quadriflow(
        lod,
        {
            "mode": "FACES",
            "target_faces": spec["target_faces"],
            "target_ratio": spec["ratio"],
            "target_edge_length": 0.1,
            "use_mesh_symmetry": spec["use_symmetry"],
            "preserve_sharp": spec["preserve_sharp"],
            "preserve_boundary": spec["preserve_boundary"],
            "preserve_attributes": spec["preserve_attributes"],
            "smooth_normals": spec["smooth_normals"],
            "seed": spec["seed"],
        },
    )


class _AdvancedMixin:
    """Provide automatic drafts, primitive fits, surface binding, and LODs."""

    def generate_quadriflow_draft(
        self,
        source_object_name,
        name=None,
        collection_name="Retopology Drafts",
        mode="FACES",
        target_faces=4000,
        target_ratio=1.0,
        target_edge_length=0.1,
        use_mesh_symmetry=False,
        preserve_sharp=False,
        preserve_boundary=False,
        preserve_attributes=True,
        smooth_normals=True,
        seed=0,
        validation_profile="CHARACTER",
    ):
        source = get_mesh_object(source_object_name)
        profile = _validate_profile(validation_profile)
        settings = _quadriflow_settings(
            mode,
            target_faces,
            target_ratio,
            target_edge_length,
            use_mesh_symmetry,
            preserve_sharp,
            preserve_boundary,
            preserve_attributes,
            smooth_normals,
            seed,
        )
        collection = _named_collection(collection_name)
        draft = _evaluated_mesh_copy(source, name or f"{source.name}_QuadriFlowDraft", collection)
        before = mesh_counts(draft)
        layers_before = _data_layer_snapshot(draft)
        _run_quadriflow(draft, settings)
        layers_after = _data_layer_snapshot(draft)
        lost_layers = _lost_data_layers(layers_before, layers_after)
        validation = self.validate_retopology(
            draft.name,
            profile=profile,
            source_object_name=source.name,
            check_skin_weights=bool(layers_after["vertex_groups"]),
        )
        warnings = [
            "QuadriFlow output is a draft and is not animation-ready until controlled loops and deformation tests pass."
        ]
        if lost_layers:
            warnings.append(f"QuadriFlow did not retain these source data layers: {lost_layers}")
        draft["blender_mcp_retopology_draft"] = True
        draft["blender_mcp_retopology_source"] = source.name
        return {
            "name": draft.name,
            "source": source.name,
            "collection": collection.name,
            "mode": settings["mode"],
            "requested": settings,
            "counts_before": before,
            "counts_after": mesh_counts(draft),
            "data_layers_before": layers_before,
            "data_layers_after": layers_after,
            "lost_data_layers": lost_layers,
            "validation": validation,
            "topology_revision": topology_revision(draft),
            "warnings": warnings,
        }

    def fit_surface_primitive(
        self,
        source_object_name,
        primitive,
        source_vertex_indices,
        name=None,
        collection_name="Retopology",
        u_segments=16,
        v_segments=4,
        project_to_source=True,
        projection_offset=0.0,
        max_fit_residual=None,
        expected_source_revision=None,
        axis_hint_world=None,
    ):
        source = get_mesh_object(source_object_name)
        if expected_source_revision is None:
            raise ValueError("expected_source_revision is required for source_vertex_indices")
        _require_revision(source, expected_source_revision)
        primitive = str(primitive).upper()
        if primitive not in {"PLANE", "CYLINDER", "CONE", "SPHERE"}:
            raise ValueError("primitive must be PLANE, CYLINDER, CONE, or SPHERE")
        minimum_samples = {"PLANE": 3, "CYLINDER": 6, "CONE": 6, "SPHERE": 4}[primitive]
        indices = _ensure_indices(source.data.vertices, source_vertex_indices, "source_vertex_indices", required=True)
        if len(indices) < minimum_samples:
            raise ValueError(f"{primitive} fitting requires at least {minimum_samples} unique sample vertices")
        u_segments = _integer(u_segments, "u_segments", 2 if primitive in {"PLANE", "SPHERE"} else 3, 256)
        v_segments = _integer(v_segments, "v_segments", 1, 256)
        offset = _finite(projection_offset, "projection_offset")
        residual_limit = _positive(max_fit_residual, "max_fit_residual") if max_fit_residual is not None else None
        axis_hint = None
        if axis_hint_world is not None:
            if not isinstance(axis_hint_world, (list, tuple)) or len(axis_hint_world) != 3:
                raise ValueError("axis_hint_world must contain exactly three finite numbers")
            axis_hint = mathutils.Vector(tuple(_finite(value, "axis_hint_world") for value in axis_hint_world))
            if axis_hint.length <= 1e-12:
                raise ValueError("axis_hint_world must not be zero")
            axis_hint.normalize()
        points = [source.matrix_world @ source.data.vertices[index].co for index in indices]
        if primitive == "PLANE":
            fit = _fit_plane(points)
            vertices, faces = _plane_geometry(fit, u_segments, v_segments)
        elif primitive in {"CYLINDER", "CONE"}:
            fit = _fit_axis_surface(points, primitive == "CONE", axis_hint)
            vertices, faces = _axis_geometry(fit, u_segments, v_segments)
        else:
            fit = _fit_sphere(points)
            vertices, faces = _sphere_geometry(fit, u_segments)
        residuals = _residual_statistics(fit["residuals"])
        if residual_limit is not None and residuals["maximum"] > residual_limit:
            raise ValueError(
                f"{primitive} fit maximum residual {residuals['maximum']:.9g} exceeds max_fit_residual {residual_limit:.9g}"
            )
        collection = _named_collection(collection_name)
        obj = _create_world_geometry_object(
            name or f"{source.name}_{primitive.title()}Fit", collection, source, vertices, faces
        )
        obj["blender_mcp_retopology_primitive"] = primitive
        obj["blender_mcp_retopology_source"] = source.name
        failed = []
        if project_to_source:
            with _editable_bmesh(obj) as bm:
                failed = _project_vertices(obj, list(bm.verts), source, offset)
        fit_parameters = {
            key: list(value)
            if isinstance(value, mathutils.Vector)
            else list(value)
            if isinstance(value, tuple)
            else value
            for key, value in fit.items()
            if key != "residuals"
        }
        return {
            "name": obj.name,
            "source": source.name,
            "collection": collection.name,
            "primitive": primitive,
            "coordinate_space": "WORLD",
            "source_vertex_indices": indices,
            "fit_parameters": fit_parameters,
            "fit_residuals_world": residuals,
            "projected_to_source": bool(project_to_source),
            "failed_projection_vertex_indices": failed,
            "all_quad_faces": all(len(face.vertices) == 4 for face in obj.data.polygons),
            **mesh_counts(obj),
            "topology_revision": topology_revision(obj),
        }

    def bind_surface_deformation(
        self,
        object_name,
        action,
        target_object_name=None,
        modifier_name="RetopologySurfaceDeform",
        falloff=4.0,
        strength=1.0,
        vertex_group=None,
        invert_vertex_group=False,
        sparse_bind=False,
        duplicate_tolerance=1e-6,
    ):
        obj = get_mesh_object(object_name)
        action = str(action).upper()
        if action not in {"BIND", "UNBIND"}:
            raise ValueError("action must be BIND or UNBIND")
        if not isinstance(modifier_name, str) or not modifier_name.strip():
            raise ValueError("modifier_name must be a non-empty string")
        modifier = obj.modifiers.get(modifier_name)
        if modifier is not None and modifier.type != "SURFACE_DEFORM":
            raise ValueError(f"Modifier '{modifier_name}' exists but is type {modifier.type}, not SURFACE_DEFORM")
        if action == "UNBIND":
            if modifier is None or not modifier.is_bound:
                return {
                    "name": obj.name,
                    "modifier": modifier.name if modifier else None,
                    "action": action,
                    "bound": False,
                    "changed": False,
                    "modifier_order": _modifier_order(obj),
                }
            with preserve_mode_and_selection():
                set_active(obj)
                result = bpy.ops.object.surfacedeform_bind(modifier=modifier.name)
                _require_finished(result, "Surface Deform unbind")
            if modifier.is_bound:
                raise RuntimeError("Surface Deform operator finished but the modifier remains bound")
            return {
                "name": obj.name,
                "modifier": modifier.name,
                "action": action,
                "bound": False,
                "changed": True,
                "modifier_order": _modifier_order(obj),
            }

        if not target_object_name:
            raise ValueError("target_object_name is required for BIND")
        target = get_mesh_object(target_object_name)
        if target == obj:
            raise ValueError("Surface Deform target must differ from the deformed object")
        falloff = _finite(falloff, "falloff")
        strength = _finite(strength, "strength")
        tolerance = _positive(duplicate_tolerance, "duplicate_tolerance")
        if not 2.0 <= falloff <= 16.0:
            raise ValueError("falloff must be between 2 and 16")
        if not -100.0 <= strength <= 100.0:
            raise ValueError("strength must be between -100 and 100")
        group_name = vertex_group or ""
        if group_name and obj.vertex_groups.get(group_name) is None:
            raise ValueError(f"Vertex group '{group_name}' does not exist on '{obj.name}'")
        if sparse_bind and not group_name:
            raise ValueError("sparse_bind=True requires vertex_group")
        preflight = _target_preflight(target, tolerance)
        if modifier is not None and modifier.is_bound:
            immutable_changes = []
            if modifier.target != target:
                immutable_changes.append("target")
            if abs(modifier.falloff - falloff) > 1e-9:
                immutable_changes.append("falloff")
            if modifier.use_sparse_bind != bool(sparse_bind):
                immutable_changes.append("sparse_bind")
            if immutable_changes:
                raise ValueError(
                    f"Modifier '{modifier.name}' is already bound; UNBIND before changing {immutable_changes}"
                )
            changed = any(
                (
                    abs(modifier.strength - strength) > 1e-9,
                    modifier.vertex_group != group_name,
                    modifier.invert_vertex_group != bool(invert_vertex_group),
                )
            )
            old_mutable = (modifier.strength, modifier.vertex_group, modifier.invert_vertex_group)
            try:
                modifier.strength = strength
                modifier.vertex_group = group_name
                modifier.invert_vertex_group = bool(invert_vertex_group)
            except Exception:
                modifier.strength, modifier.vertex_group, modifier.invert_vertex_group = old_mutable
                raise
            return {
                "name": obj.name,
                "target": target.name,
                "modifier": modifier.name,
                "action": action,
                "bound": True,
                "changed": changed,
                "preflight": preflight,
                "settings": {
                    "falloff": modifier.falloff,
                    "strength": modifier.strength,
                    "vertex_group": modifier.vertex_group,
                    "invert_vertex_group": modifier.invert_vertex_group,
                    "sparse_bind": modifier.use_sparse_bind,
                },
                "modifier_order": _modifier_order(obj),
            }
        created = modifier is None
        modifier = modifier or obj.modifiers.new(name=modifier_name, type="SURFACE_DEFORM")
        old = None
        if not created:
            old = (
                modifier.target,
                modifier.falloff,
                modifier.strength,
                modifier.vertex_group,
                modifier.invert_vertex_group,
                modifier.use_sparse_bind,
            )
        try:
            modifier.target = target
            modifier.falloff = falloff
            modifier.strength = strength
            modifier.vertex_group = group_name
            modifier.invert_vertex_group = bool(invert_vertex_group)
            modifier.use_sparse_bind = bool(sparse_bind)
            with preserve_mode_and_selection():
                set_active(obj)
                bpy.context.view_layer.update()
                result = bpy.ops.object.surfacedeform_bind(modifier=modifier.name)
                _require_finished(result, "Surface Deform bind")
            if not modifier.is_bound:
                raise RuntimeError("Surface Deform operator finished but the modifier is not bound")
        except Exception:
            if getattr(modifier, "is_bound", False):
                with contextlib.suppress(Exception), preserve_mode_and_selection():
                    set_active(obj)
                    bpy.ops.object.surfacedeform_bind(modifier=modifier.name)
            if old is not None:
                (
                    modifier.target,
                    modifier.falloff,
                    modifier.strength,
                    modifier.vertex_group,
                    modifier.invert_vertex_group,
                    modifier.use_sparse_bind,
                ) = old
            raise
        return {
            "name": obj.name,
            "target": target.name,
            "modifier": modifier.name,
            "action": action,
            "bound": True,
            "changed": True,
            "preflight": preflight,
            "settings": {
                "falloff": modifier.falloff,
                "strength": modifier.strength,
                "vertex_group": modifier.vertex_group,
                "invert_vertex_group": modifier.invert_vertex_group,
                "sparse_bind": modifier.use_sparse_bind,
            },
            "modifier_order": _modifier_order(obj),
        }

    def generate_retopology_lods(
        self,
        object_name,
        levels,
        profile="GAME",
        collection_name="Retopology LODs",
        source_object_name=None,
        reproject=False,
        projection_offset=0.0,
        transfer_data_types=None,
        confirm=False,
    ):
        if not confirm:
            raise ValueError("generate_retopology_lods requires confirm=True because it materializes reduced meshes")
        master = get_mesh_object(object_name)
        profile = _validate_profile(profile)
        offset = _finite(projection_offset, "projection_offset")
        source = get_mesh_object(source_object_name) if source_object_name else None
        if reproject and source is None:
            raise ValueError("source_object_name is required when reproject=True")
        requested_types = _lod_transfer_types(master, transfer_data_types)
        base_counts = _evaluated_counts(master)
        if base_counts["polygons"] < 2:
            raise ValueError("The evaluated master needs at least two polygons to generate reduced LODs")
        prepared = _prepare_lod_levels(levels, master, base_counts["polygons"])
        collection = _named_collection(collection_name)
        records = [
            self._generate_lod_level(
                master, source, collection, index, spec, profile, reproject, offset, requested_types
            )
            for index, spec in enumerate(prepared)
        ]
        warnings = [
            f"{record['name']} lost data layers: {record['lost_data_layers']}"
            for record in records
            if record["lost_data_layers"]
        ]
        return {
            "master": master.name,
            "source": source.name if source else None,
            "profile": profile,
            "collection": collection.name,
            "evaluated_master_counts": base_counts,
            "levels": records,
            "created_objects": [record["name"] for record in records],
            "warnings": warnings,
        }

    def _generate_lod_level(self, master, source, collection, index, spec, profile, reproject, offset, requested_types):
        lod = _evaluated_mesh_copy(master, spec["name"], collection)
        layers_before = _data_layer_snapshot(lod)
        _reduce_lod(lod, spec)
        failed_projection = []
        if reproject:
            with _editable_bmesh(lod) as bm:
                failed_projection = _project_vertices(lod, list(bm.verts), source, offset)
        transfer_result = None
        if requested_types:
            transfer_result = self.transfer_mesh_attributes(
                master.name,
                lod.name,
                sorted(requested_types),
                modifier_name=f"LOD{index + 1}DataTransfer",
                apply=False,
            )
            modifier_name = transfer_result.get("modifier")
            if modifier_name:
                apply_modifier(lod, lod.modifiers.get(modifier_name))
        lost_layers = _lost_data_layers(layers_before, _data_layer_snapshot(lod))
        lod["blender_mcp_lod_master"] = master.name
        lod["blender_mcp_lod_level"] = index + 1
        lod["blender_mcp_lod_ratio"] = spec["ratio"]
        validation = self.validate_retopology(
            lod.name,
            profile=profile,
            source_object_name=source.name if source else None,
            check_skin_weights=bool(lod.vertex_groups),
        )
        return {
            "name": lod.name,
            "level": index + 1,
            "method": spec["method"],
            "requested_ratio": spec["ratio"],
            "target_faces": spec["target_faces"],
            "counts": mesh_counts(lod),
            "failed_projection_vertex_indices": failed_projection,
            "transferred_data_types": sorted(requested_types),
            "transfer_result": transfer_result,
            "lost_data_layers": lost_layers,
            "validation": validation,
            "topology_revision": topology_revision(lod),
        }
