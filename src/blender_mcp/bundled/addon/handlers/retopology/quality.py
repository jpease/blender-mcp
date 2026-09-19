# pyright: reportGeneralTypeIssues=false, reportOptionalSubscript=false
"""Blender-main-thread handlers for measuring and validating retopology quality."""

import contextlib
import math
import statistics

import bpy
import mathutils

from ...helpers import get_mesh_object
from ._shared import (
    _bvh_class,
    _kd_tree_class,
    _modifier_order,
    _nearest_projection,
    _percentile,
    _positive,
    _read_bmesh,
    _self_intersections,
    _uv_overlap_pairs,
    _world_bvh,
    topology_revision,
)


def _evaluated_mesh_snapshot(obj, depsgraph):
    evaluated = obj.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh()
    try:
        vertices = [evaluated.matrix_world @ vertex.co for vertex in mesh.vertices]
        edges = [tuple(edge.vertices) for edge in mesh.edges]
        polygons = [tuple(polygon.vertices) for polygon in mesh.polygons]
        return vertices, edges, polygons
    finally:
        evaluated.to_mesh_clear()


def _surface_statistics(vertices, edges, polygons):
    edge_lengths = [(vertices[second] - vertices[first]).length for first, second in edges]
    areas, normals = [], []
    volume = 0.0
    for polygon in polygons:
        points = [vertices[index] for index in polygon]
        if len(points) < 3:
            areas.append(0.0)
            normals.append(mathutils.Vector((0.0, 0.0, 0.0)))
            continue
        normal = mathutils.geometry.normal(points)
        area = sum(
            mathutils.geometry.area_tri(points[0], points[index], points[index + 1])
            for index in range(1, len(points) - 1)
        )
        areas.append(area)
        normals.append(normal)
        volume += sum(
            points[0].dot(points[index].cross(points[index + 1])) / 6.0 for index in range(1, len(points) - 1)
        )
    return edge_lengths, areas, normals, volume


class _QualityMixin:
    """Provide handlers for measuring and validating retopology quality."""

    def analyze_surface_conformity(
        self,
        object_name,
        source_object_name,
        sample_vertices=True,
        sample_edge_midpoints=False,
        sample_face_centroids=False,
        max_distance=None,
        worst_limit=20,
        create_heat_map=False,
        attribute_name="retopology_distance",
    ):
        obj = get_mesh_object(object_name)
        source = get_mesh_object(source_object_name)
        if not any((sample_vertices, sample_edge_midpoints, sample_face_centroids)):
            raise ValueError("Enable at least one sample type")
        if max_distance is not None:
            max_distance = _positive(max_distance, "max_distance")
        worst_limit = max(1, min(int(worst_limit), 500))
        tree = _world_bvh(source)
        samples = []
        with _read_bmesh(obj) as bm:
            if sample_vertices:
                samples.extend(("VERTEX", vertex.index, obj.matrix_world @ vertex.co) for vertex in bm.verts)
            if sample_edge_midpoints:
                samples.extend(
                    ("EDGE", edge.index, obj.matrix_world @ ((edge.verts[0].co + edge.verts[1].co) * 0.5))
                    for edge in bm.edges
                )
            if sample_face_centroids:
                samples.extend(("FACE", face.index, obj.matrix_world @ face.calc_center_median()) for face in bm.faces)
        hits = []
        missed = []
        vertex_distances = {}
        for kind, index, point in samples:
            projection = _nearest_projection(tree, point, 0.0, max_distance)
            if projection is None:
                missed.append({"element_type": kind, "index": index})
                continue
            hit, normal, face_index, distance = projection
            signed = float((point - hit).dot(normal)) if normal.length_squared > 0 else None
            record = {
                "element_type": kind,
                "index": index,
                "distance": distance,
                "signed_offset": signed,
                "source_face_index": face_index,
            }
            hits.append(record)
            if kind == "VERTEX":
                vertex_distances[index] = distance
        distances = [record["distance"] for record in hits]
        if create_heat_map:
            if not sample_vertices:
                raise ValueError("create_heat_map requires sample_vertices=True")
            attribute = obj.data.attributes.get(attribute_name)
            if attribute is not None and (attribute.domain != "POINT" or attribute.data_type != "FLOAT"):
                raise ValueError(f"Attribute '{attribute_name}' exists but is not a POINT/FLOAT attribute")
            if attribute is None:
                attribute = obj.data.attributes.new(name=attribute_name, type="FLOAT", domain="POINT")
            for index, item in enumerate(attribute.data):
                item.value = float(vertex_distances.get(index, -1.0))
        return {
            "name": obj.name,
            "source": source.name,
            "coordinate_space": "WORLD",
            "sample_count": len(samples),
            "hit_count": len(hits),
            "missed_count": len(missed),
            "missed": missed[:worst_limit],
            "statistics": {
                "mean": statistics.fmean(distances) if distances else None,
                "rms": math.sqrt(statistics.fmean(distance * distance for distance in distances))
                if distances
                else None,
                "p50": _percentile(distances, 50),
                "p90": _percentile(distances, 90),
                "p95": _percentile(distances, 95),
                "p99": _percentile(distances, 99),
                "maximum": max(distances, default=None),
            },
            "signed_offset_reliability": "NEAREST_TRIANGLE_NORMAL",
            "worst": sorted(hits, key=lambda item: item["distance"], reverse=True)[:worst_limit],
            "heat_map_attribute": attribute_name if create_heat_map else None,
            "topology_revision": topology_revision(obj),
        }

    def validate_retopology(
        self,
        object_name,
        profile="CHARACTER",
        source_object_name=None,
        thresholds=None,
        check_self_intersections=True,
        check_uv_overlap=True,
        check_skin_weights=True,
        issue_limit=100,
    ):
        obj = get_mesh_object(object_name)
        profile = str(profile).upper()
        profiles = {
            "CHARACTER": {
                "max_aspect": 6.0,
                "max_pole_valence": 6.0,
                "max_density_ratio": 4.0,
                "max_triangle_fraction": 0.1,
                "max_ngon_fraction": 0.02,
                "max_conformity": 0.01,
                "double_distance": 1e-5,
                "symmetry_tolerance": 0.001,
            },
            "HARD_SURFACE": {
                "max_aspect": 12.0,
                "max_pole_valence": 8.0,
                "max_density_ratio": 8.0,
                "max_triangle_fraction": 0.25,
                "max_ngon_fraction": 0.1,
                "max_conformity": 0.005,
                "double_distance": 1e-6,
                "symmetry_tolerance": 0.0001,
            },
            "VFX": {
                "max_aspect": 8.0,
                "max_pole_valence": 8.0,
                "max_density_ratio": 5.0,
                "max_triangle_fraction": 0.1,
                "max_ngon_fraction": 0.02,
                "max_conformity": 0.0025,
                "double_distance": 1e-6,
                "symmetry_tolerance": 0.0001,
            },
            "GAME": {
                "max_aspect": 10.0,
                "max_pole_valence": 8.0,
                "max_density_ratio": 8.0,
                "max_triangle_fraction": 0.5,
                "max_ngon_fraction": 0.1,
                "max_conformity": 0.02,
                "double_distance": 1e-5,
                "symmetry_tolerance": 0.001,
            },
        }
        if profile not in profiles:
            raise ValueError(f"profile must be one of {sorted(profiles)}")
        active_thresholds = dict(profiles[profile])
        if thresholds:
            unknown = set(thresholds) - set(active_thresholds)
            if unknown:
                raise ValueError(f"Unknown threshold names: {sorted(unknown)}")
            for name, value in thresholds.items():
                active_thresholds[name] = _positive(value, f"thresholds.{name}", allow_zero=True)
        issue_limit = max(1, min(int(issue_limit), 1000))
        checks = []

        def add_check(name, status, summary, elements=None):
            checks.append(
                {
                    "name": name,
                    "status": status,
                    "summary": summary,
                    "element_indices": list(elements or [])[:issue_limit],
                    "truncated": len(elements or []) > issue_limit,
                }
            )

        temporary = obj.data.copy()
        try:
            validation_changed = temporary.validate(verbose=False, clean_customdata=False)
        finally:
            bpy.data.meshes.remove(temporary)
        add_check(
            "mesh_validate_copy",
            "FAIL" if validation_changed else "PASS",
            "Mesh.validate() found correctable invalid data on a disposable copy"
            if validation_changed
            else "No invalid mesh data found",
        )
        with _read_bmesh(obj) as bm:
            non_manifold = [edge.index for edge in bm.edges if len(edge.link_faces) > 2]
            boundaries = [edge.index for edge in bm.edges if edge.is_boundary]
            degenerates = [face.index for face in bm.faces if face.calc_area() <= 1e-12]
            winding = [edge.index for edge in bm.edges if edge.is_manifold and not edge.is_contiguous]
            isolated = [vertex.index for vertex in bm.verts if not vertex.link_edges]
            aspects = {}
            for face in bm.faces:
                lengths = [edge.calc_length() for edge in face.edges]
                if lengths and min(lengths) > 1e-12:
                    aspects[face.index] = max(lengths) / min(lengths)
            poor_aspect = [index for index, value in aspects.items() if value > active_thresholds["max_aspect"]]
            high_poles = [
                vertex.index for vertex in bm.verts if len(vertex.link_edges) > active_thresholds["max_pole_valence"]
            ]
            face_total = max(1, len(bm.faces))
            triangle_indices = [face.index for face in bm.faces if len(face.verts) == 3]
            ngon_indices = [face.index for face in bm.faces if len(face.verts) > 4]
            density_edges = []
            for edge in bm.edges:
                if len(edge.link_faces) != 2:
                    continue
                first_area, second_area = (face.calc_area() for face in edge.link_faces)
                if min(first_area, second_area) <= 1e-12:
                    continue
                if max(first_area, second_area) / min(first_area, second_area) > active_thresholds["max_density_ratio"]:
                    density_edges.append(edge.index)
            add_check(
                "non_manifold",
                "FAIL" if non_manifold else "PASS",
                f"{len(non_manifold)} edges have more than two faces",
                non_manifold,
            )
            boundary_status = "WARN" if boundaries else "PASS"
            add_check(
                "boundaries",
                boundary_status,
                f"{len(boundaries)} boundary edges; open retopology patches may intentionally retain them",
                boundaries,
            )
            add_check(
                "degenerate_faces",
                "FAIL" if degenerates else "PASS",
                f"{len(degenerates)} zero-area faces",
                degenerates,
            )
            add_check(
                "winding",
                "FAIL" if winding else "PASS",
                f"{len(winding)} manifold edges have inconsistent winding",
                winding,
            )
            add_check(
                "isolated_vertices", "WARN" if isolated else "PASS", f"{len(isolated)} isolated vertices", isolated
            )
            add_check(
                "face_aspect",
                "WARN" if poor_aspect else "PASS",
                f"{len(poor_aspect)} faces exceed aspect {active_thresholds['max_aspect']}",
                poor_aspect,
            )
            add_check(
                "poles",
                "WARN" if high_poles else "PASS",
                f"{len(high_poles)} vertices exceed valence {active_thresholds['max_pole_valence']}",
                high_poles,
            )
            triangle_fraction = len(triangle_indices) / face_total
            ngon_fraction = len(ngon_indices) / face_total
            add_check(
                "face_composition",
                "WARN"
                if triangle_fraction > active_thresholds["max_triangle_fraction"]
                or ngon_fraction > active_thresholds["max_ngon_fraction"]
                else "PASS",
                f"Triangle fraction {triangle_fraction:.4f} (limit {active_thresholds['max_triangle_fraction']}), "
                f"n-gon fraction {ngon_fraction:.4f} (limit {active_thresholds['max_ngon_fraction']})",
                triangle_indices + ngon_indices,
            )
            add_check(
                "density_changes",
                "WARN" if density_edges else "PASS",
                f"{len(density_edges)} adjacent face pairs exceed area ratio {active_thresholds['max_density_ratio']}",
                density_edges,
            )
            tree = _kd_tree_class()(len(bm.verts))
            for vertex in bm.verts:
                tree.insert(vertex.co, vertex.index)
            tree.balance()
            doubles = set()
            for vertex in bm.verts:
                for _coordinate, other_index, _distance in tree.find_range(
                    vertex.co, active_thresholds["double_distance"]
                ):
                    if other_index != vertex.index:
                        doubles.add(max(vertex.index, other_index))
            add_check(
                "doubles", "FAIL" if doubles else "PASS", f"{len(doubles)} near-duplicate vertices", sorted(doubles)
            )
            if check_self_intersections:
                intersections = _self_intersections(bm, issue_limit)
                add_check(
                    "self_intersections",
                    "FAIL" if intersections else "PASS",
                    f"{len(intersections)} non-adjacent intersecting face pairs found within report limit",
                    intersections,
                )
        uv_layers = list(obj.data.uv_layers)
        if not uv_layers:
            add_check("uv_layers", "WARN", "No UV layer exists")
        elif check_uv_overlap:
            uv_overlaps = _uv_overlap_pairs(obj.data, issue_limit)
            if uv_overlaps is None:
                add_check(
                    "uv_overlap",
                    "WARN",
                    "UV overlap skipped because the triangulated mesh exceeds the 2,500-triangle safety limit",
                )
            else:
                add_check(
                    "uv_overlap",
                    "WARN" if uv_overlaps else "PASS",
                    f"{len(uv_overlaps)} positive-area UV face overlaps found within report limit",
                    uv_overlaps,
                )
        else:
            add_check("uv_layers", "PASS", f"{len(uv_layers)} UV layer(s) present")
        if check_skin_weights:
            unweighted = []
            unnormalized = []
            deform_group_names = set()
            for modifier in obj.modifiers:
                if modifier.type == "ARMATURE" and modifier.object and modifier.object.type == "ARMATURE":
                    deform_group_names.update(bone.name for bone in modifier.object.data.bones if bone.use_deform)
            deform_groups = {group.index for group in obj.vertex_groups if group.name in deform_group_names}
            for vertex in obj.data.vertices:
                total = sum(item.weight for item in vertex.groups if item.group in deform_groups)
                if deform_groups and total <= 1e-8:
                    unweighted.append(vertex.index)
                elif deform_groups and abs(total - 1.0) > 1e-3:
                    unnormalized.append(vertex.index)
            add_check(
                "skin_weights",
                "WARN" if unweighted or unnormalized else "PASS",
                f"{len(unweighted)} unweighted and {len(unnormalized)} non-normalized vertices",
                unweighted + unnormalized,
            )
        modifier_order = _modifier_order(obj)
        subdivision_indices = [item["index"] for item in modifier_order if item["type"] == "SUBSURF"]
        projection_indices = [item["index"] for item in modifier_order if item["type"] in {"SHRINKWRAP", "MIRROR"}]
        bad_order = bool(
            subdivision_indices and projection_indices and max(projection_indices) > min(subdivision_indices)
        )
        unready_modifiers = [
            modifier.name
            for modifier in obj.modifiers
            if (modifier.type == "SHRINKWRAP" and modifier.target is None)
            or (modifier.type == "MIRROR" and not any(modifier.use_axis))
        ]
        if unready_modifiers:
            modifier_summary = f"Unready modifiers: {unready_modifiers}"
        elif bad_order:
            modifier_summary = "Mirror/Shrinkwrap must precede Subdivision"
        else:
            modifier_summary = "Modifier order is ready"
        add_check(
            "modifier_readiness",
            "FAIL" if bad_order or unready_modifiers else "PASS",
            modifier_summary,
        )
        mirror_modifier = next((modifier for modifier in obj.modifiers if modifier.type == "MIRROR"), None)
        if mirror_modifier:
            axes = [index for index, enabled in enumerate(mirror_modifier.use_axis) if enabled]
            unmatched = []
            if axes:
                coordinates = (
                    [
                        mirror_modifier.mirror_object.matrix_world.inverted_safe() @ (obj.matrix_world @ vertex.co)
                        for vertex in obj.data.vertices
                    ]
                    if mirror_modifier.mirror_object
                    else [vertex.co.copy() for vertex in obj.data.vertices]
                )
                tree = _kd_tree_class()(len(coordinates))
                for index, coordinate in enumerate(coordinates):
                    tree.insert(coordinate, index)
                tree.balance()
                for index, coordinate in enumerate(coordinates):
                    mirrored = coordinate.copy()
                    for axis_index in axes:
                        mirrored[axis_index] *= -1.0
                    _hit, _other, distance = tree.find(mirrored)
                    if distance is None or distance > active_thresholds["symmetry_tolerance"]:
                        unmatched.append(index)
            add_check(
                "symmetry",
                "WARN" if unmatched else "PASS",
                f"{len(unmatched)} vertices lack a symmetric counterpart",
                unmatched,
            )
        else:
            add_check("symmetry", "PASS", "No live Mirror modifier; symmetry is not required by this target")
        conformity = None
        if source_object_name:
            conformity = self.analyze_surface_conformity(
                object_name,
                source_object_name,
                sample_vertices=True,
                sample_edge_midpoints=True,
                sample_face_centroids=False,
                max_distance=None,
                worst_limit=min(issue_limit, 20),
                create_heat_map=False,
            )
            maximum = conformity["statistics"]["maximum"]
            exceeds = maximum is not None and maximum > active_thresholds["max_conformity"]
            add_check(
                "surface_conformity",
                "WARN" if exceeds else "PASS",
                f"Maximum world-space offset {maximum}; threshold {active_thresholds['max_conformity']}",
            )
        statuses = {check["status"] for check in checks}
        overall = "FAIL" if "FAIL" in statuses else "WARN" if "WARN" in statuses else "PASS"
        return {
            "name": obj.name,
            "profile": profile,
            "status": overall,
            "thresholds": active_thresholds,
            "checks": checks,
            "modifier_order": modifier_order,
            "conformity": conformity,
            "topology_revision": topology_revision(obj),
        }

    def test_deformation(
        self,
        object_name,
        frames,
        reference_frame=None,
        source_object_name=None,
        joint_vertex_groups=None,
        stretch_warning_ratio=1.25,
        area_warning_ratio=1.5,
        check_self_intersections=True,
        issue_limit=100,
    ):
        obj = get_mesh_object(object_name)
        source = get_mesh_object(source_object_name) if source_object_name else None
        if source == obj:
            raise ValueError("source_object_name must differ from object_name")
        if (
            not isinstance(frames, list)
            or not frames
            or any(isinstance(frame, bool) or not isinstance(frame, int) for frame in frames)
        ):
            raise ValueError("frames must be a non-empty list of integers")
        stretch_limit = _positive(stretch_warning_ratio, "stretch_warning_ratio")
        area_limit = _positive(area_warning_ratio, "area_warning_ratio")
        if stretch_limit < 1.0 or area_limit < 1.0:
            raise ValueError("stretch_warning_ratio and area_warning_ratio must be at least 1")
        issue_limit = max(1, min(int(issue_limit), 1000))
        group_indices = set()
        for name in joint_vertex_groups or []:
            group = obj.vertex_groups.get(name)
            if group is None:
                raise ValueError(f"Joint vertex group not found on '{obj.name}': {name}")
            for vertex in obj.data.vertices:
                with contextlib.suppress(RuntimeError):
                    if group.weight(vertex.index) > 0.0:
                        group_indices.add(vertex.index)
        scene = bpy.context.scene
        original_frame = scene.frame_current
        reference = original_frame if reference_frame is None else int(reference_frame)
        depsgraph = bpy.context.evaluated_depsgraph_get()
        results = []
        try:
            scene.frame_set(reference)
            depsgraph.update()
            base_vertices, base_edges, base_polygons = _evaluated_mesh_snapshot(obj, depsgraph)
            base_lengths, base_areas, base_normals, base_volume = _surface_statistics(
                base_vertices, base_edges, base_polygons
            )
            for frame in frames:
                scene.frame_set(frame)
                depsgraph.update()
                vertices, edges, polygons = _evaluated_mesh_snapshot(obj, depsgraph)
                if edges != base_edges or polygons != base_polygons:
                    raise ValueError(
                        "The evaluated modifier stack changes topology across frames; correspondence is undefined"
                    )
                lengths, areas, normals, volume = _surface_statistics(vertices, edges, polygons)
                edge_ratios = [
                    current / original if original > 1e-12 else 1.0
                    for current, original in zip(lengths, base_lengths, strict=True)
                ]
                area_ratios = [
                    current / original if original > 1e-12 else 1.0
                    for current, original in zip(areas, base_areas, strict=True)
                ]
                stretched = [
                    index
                    for index, ratio in enumerate(edge_ratios)
                    if (ratio > stretch_limit or ratio < 1.0 / stretch_limit)
                    and (not group_indices or set(edges[index]) & group_indices)
                ]
                changed_area = [
                    index
                    for index, ratio in enumerate(area_ratios)
                    if (ratio > area_limit or ratio < 1.0 / area_limit)
                    and (not group_indices or set(polygons[index]) & group_indices)
                ]
                flipped = [
                    index
                    for index, (current, original) in enumerate(zip(normals, base_normals, strict=True))
                    if current.length_squared and original.length_squared and current.dot(original) < 0.0
                ]
                intersections = []
                if check_self_intersections and polygons:
                    tree = _bvh_class().FromPolygons(vertices, polygons, all_triangles=False, epsilon=0.0)
                    for first, second in tree.overlap(tree):
                        if first >= second or set(polygons[first]) & set(polygons[second]):
                            continue
                        intersections.append([first, second])
                        if len(intersections) >= issue_limit:
                            break
                conformity = None
                if source:
                    tree = _world_bvh(source)
                    conformity_indices = sorted(group_indices) if group_indices else range(len(vertices))
                    distances = []
                    misses = []
                    for index in conformity_indices:
                        hit = _nearest_projection(tree, vertices[index])
                        if hit is None:
                            misses.append(index)
                        else:
                            distances.append((index, hit[3]))
                    conformity = {
                        "coordinate_space": "WORLD",
                        "sample_count": len(distances) + len(misses),
                        "missed_vertex_indices": misses[:issue_limit],
                        "mean_distance": statistics.fmean(value for _index, value in distances) if distances else None,
                        "maximum_distance": max((value for _index, value in distances), default=None),
                        "worst_vertices": [
                            {"vertex_index": index, "distance": value}
                            for index, value in sorted(distances, key=lambda item: item[1], reverse=True)[:issue_limit]
                        ],
                    }
                results.append(
                    {
                        "frame": frame,
                        "edge_stretch": {
                            "minimum_ratio": min(edge_ratios, default=None),
                            "mean_ratio": statistics.fmean(edge_ratios) if edge_ratios else None,
                            "maximum_ratio": max(edge_ratios, default=None),
                            "warning_edge_indices": stretched[:issue_limit],
                            "warning_count": len(stretched),
                        },
                        "face_area_change": {
                            "minimum_ratio": min(area_ratios, default=None),
                            "mean_ratio": statistics.fmean(area_ratios) if area_ratios else None,
                            "maximum_ratio": max(area_ratios, default=None),
                            "warning_face_indices": changed_area[:issue_limit],
                            "warning_count": len(changed_area),
                        },
                        "signed_volume": volume,
                        "volume_ratio": volume / base_volume if abs(base_volume) > 1e-12 else None,
                        "flipped_face_indices": flipped[:issue_limit],
                        "flipped_face_count": len(flipped),
                        "self_intersection_face_pairs": intersections,
                        "source_conformity": conformity,
                    }
                )
        finally:
            scene.frame_set(original_frame)
        return {
            "name": obj.name,
            "source": source.name if source else None,
            "reference_frame": reference,
            "tested_frames": frames,
            "joint_vertex_groups": joint_vertex_groups or [],
            "thresholds": {"edge_stretch_ratio": stretch_limit, "face_area_ratio": area_limit},
            "results": results,
            "topology_revision": topology_revision(obj),
        }
