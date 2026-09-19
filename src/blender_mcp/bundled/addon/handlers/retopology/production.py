# pyright: reportArgumentType=false, reportGeneralTypeIssues=false, reportOptionalSubscript=false
"""Blender-main-thread handlers for asset handoff: attribute transfer, UV unwrapping, bake cages, and baking."""

import contextlib
import math
import os
import statistics

from pathlib import Path

import bpy

from ...helpers import apply_modifier, edit_mesh, get_mesh_object, mesh_counts, preserve_mode_and_selection
from ._shared import (
    _TRANSFER_TYPES,
    _bvh_class,
    _finite,
    _modifier_order,
    _named_collection,
    _nearest_projection,
    _positive,
    _read_bmesh,
    _require_finished,
    _require_name,
    _self_intersections,
    _uv_overlap_pairs,
    _world_bvh,
    topology_revision,
)


class _ProductionMixin:
    """Provide handlers for transferring attributes, unwrapping UVs, and baking maps for asset handoff."""

    def transfer_mesh_attributes(
        self,
        source_object_name,
        object_name,
        data_types,
        modifier_name="RetopologyDataTransfer",
        vertex_mapping="POLYINTERP_NEAREST",
        edge_mapping="NEAREST",
        loop_mapping="POLYINTERP_NEAREST",
        polygon_mapping="NEAREST",
        use_object_transform=True,
        max_distance=None,
        source_layers="ALL",
        destination_layers="NAME",
        mix_mode="REPLACE",
        mix_factor=1.0,
        apply=False,
    ):
        source, obj = get_mesh_object(source_object_name), get_mesh_object(object_name)
        if source == obj:
            raise ValueError("source_object_name must differ from object_name")
        requested = {str(value).upper() for value in data_types}
        unknown = requested - _TRANSFER_TYPES
        if not requested or unknown:
            raise ValueError(
                f"data_types must be a non-empty subset of {sorted(_TRANSFER_TYPES)}; unknown={sorted(unknown)}"
            )
        if "UVS" in requested and not source.data.uv_layers:
            raise ValueError(f"Source '{source.name}' has no UV layers to transfer")
        if "VERTEX_GROUPS" in requested and not source.vertex_groups:
            raise ValueError(f"Source '{source.name}' has no vertex groups to transfer")
        if "COLOR_ATTRIBUTES" in requested and not source.data.color_attributes:
            raise ValueError(f"Source '{source.name}' has no color attributes to transfer")
        if "MATERIAL_INDICES" in requested and not source.material_slots:
            raise ValueError(f"Source '{source.name}' has no material slots to map")
        factor = _finite(mix_factor, "mix_factor")
        if not 0.0 <= factor <= 1.0:
            raise ValueError("mix_factor must be between 0 and 1")
        maximum = _positive(max_distance, "max_distance", allow_zero=True) if max_distance is not None else 1.0
        source_layers, destination_layers = str(source_layers).upper(), str(destination_layers).upper()
        if source_layers not in {"ACTIVE", "ALL"} or destination_layers not in {"NAME", "INDEX"}:
            raise ValueError("source_layers must be ACTIVE/ALL and destination_layers must be NAME/INDEX")
        modifier_types = requested - {"MATERIAL_INDICES"}
        modifier = None
        if modifier_types:
            modifier = obj.modifiers.get(modifier_name)
            if modifier is not None and modifier.type != "DATA_TRANSFER":
                raise ValueError(f"Modifier '{modifier_name}' exists but is type {modifier.type}, not DATA_TRANSFER")
            modifier = modifier or obj.modifiers.new(name=modifier_name, type="DATA_TRANSFER")
            modifier.object = source
            modifier.use_object_transform = bool(use_object_transform)
            modifier.use_max_distance = max_distance is not None
            modifier.max_distance = maximum
            modifier.mix_mode = str(mix_mode).upper()
            modifier.mix_factor = factor
            # Assignment through RNA deliberately validates these public Blender enums.
            modifier.vert_mapping = str(vertex_mapping).upper()
            modifier.edge_mapping = str(edge_mapping).upper()
            modifier.loop_mapping = str(loop_mapping).upper()
            modifier.poly_mapping = str(polygon_mapping).upper()
            modifier.data_types_verts = {
                item
                for key, item in {
                    "VERTEX_GROUPS": "VGROUP_WEIGHTS",
                    "COLOR_ATTRIBUTES": "COLOR_VERTEX",
                    "BEVEL_WEIGHTS": "BEVEL_WEIGHT_VERT",
                }.items()
                if key in modifier_types
            }
            modifier.data_types_edges = {
                item
                for key, item in {
                    "SEAMS": "SEAM",
                    "CREASES": "CREASE",
                    "BEVEL_WEIGHTS": "BEVEL_WEIGHT_EDGE",
                    "SHARP_EDGES": "SHARP_EDGE",
                }.items()
                if key in modifier_types
            }
            modifier.data_types_loops = {
                item
                for key, item in {
                    "UVS": "UV",
                    "COLOR_ATTRIBUTES": "COLOR_CORNER",
                    "CUSTOM_NORMALS": "CUSTOM_NORMAL",
                }.items()
                if key in modifier_types
            }
            modifier.data_types_polys = {"SMOOTH"} if "SMOOTH_SHADING" in modifier_types else set()
            modifier.layers_vgroup_select_src = source_layers
            modifier.layers_vgroup_select_dst = destination_layers
            modifier.layers_uv_select_src = source_layers
            modifier.layers_uv_select_dst = destination_layers
            if apply:
                apply_modifier(obj, modifier)
        material_faces = []
        if "MATERIAL_INDICES" in requested:
            depsgraph = bpy.context.evaluated_depsgraph_get()
            evaluated = source.evaluated_get(depsgraph)
            mesh = evaluated.to_mesh()
            try:
                vertices = [evaluated.matrix_world @ vertex.co for vertex in mesh.vertices]
                polygons = [tuple(polygon.vertices) for polygon in mesh.polygons if len(polygon.vertices) >= 3]
                tree = _bvh_class().FromPolygons(vertices, polygons, all_triangles=False, epsilon=0.0)
                destination_indices = {}
                for index, slot in enumerate(source.material_slots):
                    material = slot.material
                    if material is None:
                        continue
                    destination = next(
                        (
                            slot_index
                            for slot_index, target_slot in enumerate(obj.material_slots)
                            if target_slot.material == material
                        ),
                        None,
                    )
                    if destination is None:
                        obj.data.materials.append(material)
                        destination = len(obj.material_slots) - 1
                    destination_indices[index] = destination
                for polygon in obj.data.polygons:
                    _hit, _normal, source_face, _distance = tree.find_nearest(obj.matrix_world @ polygon.center)
                    if source_face is not None:
                        polygon.material_index = destination_indices.get(mesh.polygons[source_face].material_index, 0)
                        material_faces.append(polygon.index)
            finally:
                evaluated.to_mesh_clear()
        return {
            "name": obj.name,
            "source": source.name,
            "data_types": sorted(requested),
            "modifier": None if modifier is None or apply else modifier.name,
            "applied": bool(apply and modifier_types),
            "material_index_face_indices": material_faces,
            "modifier_order": _modifier_order(obj),
            "topology_revision": topology_revision(obj),
        }

    def unwrap_retopology_uvs(
        self,
        object_name,
        uv_map_name="RetopologyUV",
        method="ANGLE_BASED",
        replace_existing=False,
        average_island_scale=True,
        minimize_stretch_iterations=10,
        pack_islands=True,
        margin=0.001,
    ):
        obj = get_mesh_object(object_name)
        name, method = _require_name(uv_map_name, "uv_map_name"), str(method).upper()
        if method not in {"ANGLE_BASED", "CONFORMAL", "MINIMUM_STRETCH"}:
            raise ValueError("method must be ANGLE_BASED, CONFORMAL, or MINIMUM_STRETCH")
        iterations = int(minimize_stretch_iterations)
        if not 0 <= iterations <= 10000:
            raise ValueError("minimize_stretch_iterations must be between 0 and 10000")
        margin = _positive(margin, "margin", allow_zero=True)
        if margin > 1.0:
            raise ValueError("margin must not exceed 1")
        existing = obj.data.uv_layers.get(name)
        if existing and not replace_existing:
            raise ValueError(f"UV map '{name}' already exists; set replace_existing=True to replace only that map")
        if existing:
            obj.data.uv_layers.remove(existing)
        layer = obj.data.uv_layers.new(name=name, do_init=False)
        obj.data.uv_layers.active = layer
        with edit_mesh(obj):
            _require_finished(bpy.ops.uv.unwrap(method=method, margin=margin), "UV Unwrap")
            if average_island_scale:
                _require_finished(
                    bpy.ops.uv.average_islands_scale(scale_uv=False, shear=False), "Average Islands Scale"
                )
            if iterations:
                _require_finished(
                    bpy.ops.uv.minimize_stretch(fill_holes=True, blend=0.0, iterations=iterations), "Minimize Stretch"
                )
            if pack_islands:
                _require_finished(
                    bpy.ops.uv.pack_islands(rotate=True, scale=True, margin_method="ADD", margin=margin), "Pack Islands"
                )
        obj.data.uv_layers.active = obj.data.uv_layers.get(name)
        uv_layer = obj.data.uv_layers[name]
        zero_area, outside, stretch, density = [], set(), [], []
        for polygon in obj.data.polygons:
            uvs = [uv_layer.data[index].uv for index in polygon.loop_indices]
            if len(uvs) < 3:
                continue
            uv_area = (
                abs(
                    sum(
                        uvs[index].x * uvs[(index + 1) % len(uvs)].y - uvs[(index + 1) % len(uvs)].x * uvs[index].y
                        for index in range(len(uvs))
                    )
                )
                * 0.5
            )
            if uv_area <= 1e-12:
                zero_area.append(polygon.index)
            if any(component < -1e-8 or component > 1.0 + 1e-8 for uv in uvs for component in uv):
                outside.add(polygon.index)
            if polygon.area > 1e-12 and uv_area > 1e-12:
                density.append(math.sqrt(uv_area / polygon.area))
                stretch.append(max(polygon.area / uv_area, uv_area / polygon.area))
        overlaps = _uv_overlap_pairs(obj.data, 100)
        seam_edges = {edge.index for edge in obj.data.edges if edge.use_seam}
        adjacency = {polygon.index: set() for polygon in obj.data.polygons}
        edge_faces = {}
        for polygon in obj.data.polygons:
            for key in polygon.edge_keys:
                edge_faces.setdefault(key, []).append(polygon.index)
        for edge in obj.data.edges:
            if edge.index not in seam_edges:
                for first in edge_faces.get(edge.key, []):
                    adjacency[first].update(second for second in edge_faces[edge.key] if second != first)
        islands, remaining = 0, set(adjacency)
        while remaining:
            islands += 1
            queue = [remaining.pop()]
            while queue:
                connected = adjacency[queue.pop()] & remaining
                remaining -= connected
                queue.extend(connected)
        mean_density = statistics.fmean(density) if density else None
        variation = (
            statistics.pstdev(density) / mean_density
            if mean_density and len(density) > 1
            else (0.0 if density else None)
        )
        return {
            "name": obj.name,
            "uv_map": name,
            "method": method,
            "island_count": islands,
            "zero_area_face_indices": zero_area,
            "overlap_face_pairs": overlaps,
            "overlap_check_skipped": overlaps is None,
            "out_of_range_face_indices": sorted(outside),
            "stretch": {
                "mean_area_ratio": statistics.fmean(stretch) if stretch else None,
                "maximum_area_ratio": max(stretch, default=None),
            },
            "texel_density": {"mean": mean_density, "coefficient_of_variation": variation},
            "topology_revision": topology_revision(obj),
        }

    def create_bake_cage(
        self,
        object_name,
        high_poly_object_names=None,
        name=None,
        collection_name="Retopology Bake Cages",
        offset=0.02,
        vertex_group=None,
        validate_enclosure=True,
    ):
        obj = get_mesh_object(object_name)
        high_objects = [get_mesh_object(value) for value in (high_poly_object_names or [])]
        if not high_objects:
            raise ValueError("high_poly_object_names must contain at least one mesh")
        if obj in high_objects:
            raise ValueError("The low-poly object cannot also be a high-poly source")
        amount = _positive(offset, "offset", allow_zero=True)
        group = obj.vertex_groups.get(vertex_group) if vertex_group else None
        if vertex_group and group is None:
            raise ValueError(f"Vertex group not found on '{obj.name}': {vertex_group}")
        mesh = obj.data.copy()
        cage = bpy.data.objects.new(name or f"{obj.name}_Cage", mesh)
        collection = _named_collection(collection_name)
        collection.objects.link(cage)
        cage.matrix_world = obj.matrix_world.copy()
        cage.hide_render = True
        cage.display_type = "WIRE"
        cage.show_in_front = True
        cage["blender_mcp_bake_cage_for"] = obj.name
        for vertex in mesh.vertices:
            weight = 1.0
            if group is not None:
                try:
                    weight = group.weight(vertex.index)
                except RuntimeError:
                    weight = 0.0
            vertex.co += vertex.normal.normalized() * amount * weight
        mesh.update()
        topology_identical = (
            len(mesh.vertices) == len(obj.data.vertices)
            and [tuple(edge.vertices) for edge in mesh.edges] == [tuple(edge.vertices) for edge in obj.data.edges]
            and [tuple(poly.vertices) for poly in mesh.polygons] == [tuple(poly.vertices) for poly in obj.data.polygons]
        )
        outside, ray_misses, self_intersections = [], [], []
        if validate_enclosure:
            cage_tree = _world_bvh(cage)
            depsgraph = bpy.context.evaluated_depsgraph_get()
            for high in high_objects:
                evaluated = high.evaluated_get(depsgraph)
                high_mesh = evaluated.to_mesh()
                try:
                    for vertex in high_mesh.vertices:
                        world = evaluated.matrix_world @ vertex.co
                        hit = _nearest_projection(cage_tree, world)
                        if hit and (world - hit[0]).dot(hit[1]) > 1e-6:
                            outside.append({"object": high.name, "vertex_index": vertex.index})
                finally:
                    evaluated.to_mesh_clear()
            high_trees = [_world_bvh(high) for high in high_objects]
            normal_matrix = obj.matrix_world.to_3x3().inverted_safe().transposed()
            for vertex in obj.data.vertices:
                origin = obj.matrix_world @ vertex.co
                direction = (normal_matrix @ vertex.normal).normalized()
                found = any(
                    tree.ray_cast(origin, direction, 1.84467e19)[0] is not None
                    or tree.ray_cast(origin, -direction, 1.84467e19)[0] is not None
                    for tree in high_trees
                )
                if not found:
                    ray_misses.append(vertex.index)
            with _read_bmesh(cage) as bm:
                self_intersections = _self_intersections(bm, 100)
        return {
            "source_low_poly": obj.name,
            "high_poly_objects": [item.name for item in high_objects],
            "cage_object": cage.name,
            "collection": collection.name,
            "offset": amount,
            "vertex_group": vertex_group,
            "topology_identical": topology_identical,
            "counts": mesh_counts(cage),
            "validation": {
                "high_poly_samples_outside": outside[:100],
                "outside_count": len(outside),
                "normal_ray_miss_vertex_indices": ray_misses[:100],
                "ray_miss_count": len(ray_misses),
                "self_intersection_face_pairs": self_intersections,
            },
        }

    def bake_retopology_maps(
        self,
        object_name,
        high_poly_object_names,
        map_type,
        output_path,
        width=2048,
        height=2048,
        uv_map_name=None,
        cage_object_name=None,
        cage_extrusion=0.0,
        max_ray_distance=0.0,
        margin=16,
        normal_space="TANGENT",
        normal_swizzle=("POS_X", "POS_Y", "POS_Z"),
        overwrite=False,
        confirm=False,
    ):
        if not confirm:
            raise ValueError("Baking is expensive and writes a file; call again with confirm=True")
        obj = get_mesh_object(object_name)
        high_objects = [get_mesh_object(value) for value in (high_poly_object_names or [])]
        if obj in high_objects:
            raise ValueError("Every high-poly source must differ from the bake target")
        bake_type = str(map_type).upper()
        bake_types = {
            "NORMAL": "NORMAL",
            "DISPLACEMENT": "DISPLACEMENT",
            "AO": "AO",
            "POSITION": "POSITION",
            "DIFFUSE": "DIFFUSE",
            "ROUGHNESS": "ROUGHNESS",
            "EMISSION": "EMIT",
            "COMBINED": "COMBINED",
            "GLOSSY": "GLOSSY",
            "SHADOW": "SHADOW",
            "UV": "UV",
        }
        if bake_type not in bake_types:
            raise ValueError(f"map_type must be one of {sorted(bake_types)}")
        path = Path(output_path).expanduser()
        if not path.is_absolute():
            raise ValueError("output_path must be absolute")
        if not path.parent.is_dir():
            raise ValueError(f"Output directory does not exist: {path.parent}")
        existed = path.exists()
        if existed and not overwrite:
            raise ValueError(f"Output file already exists: {path}; set overwrite=True to replace it")
        width, height, margin = int(width), int(height), int(margin)
        if not 1 <= width <= 32768 or not 1 <= height <= 32768:
            raise ValueError("width and height must be between 1 and 32768")
        if not 0 <= margin <= 32767:
            raise ValueError("margin must be between 0 and 32767 pixels")
        if not obj.data.uv_layers:
            raise ValueError("The low-poly mesh has no UV map; run unwrap_retopology_uvs first")
        uv_layer = obj.data.uv_layers.get(uv_map_name) if uv_map_name else obj.data.uv_layers.active
        if uv_layer is None or not uv_layer.data:
            raise ValueError(f"UV map is missing or empty: {uv_map_name or '<active>'}")
        cage = get_mesh_object(cage_object_name) if cage_object_name else None
        if cage and (
            len(cage.data.vertices) != len(obj.data.vertices)
            or [tuple(edge.vertices) for edge in cage.data.edges] != [tuple(edge.vertices) for edge in obj.data.edges]
            or [tuple(face.vertices) for face in cage.data.polygons]
            != [tuple(face.vertices) for face in obj.data.polygons]
        ):
            raise ValueError("The bake cage topology does not match the low-poly mesh")
        extrusion = _positive(cage_extrusion, "cage_extrusion", allow_zero=True)
        ray_distance = _positive(max_ray_distance, "max_ray_distance", allow_zero=True)
        normal_space = str(normal_space).upper()
        if normal_space not in {"TANGENT", "OBJECT"}:
            raise ValueError("normal_space must be TANGENT or OBJECT")
        valid_swizzle = {f"{sign}_{axis}" for sign in ("POS", "NEG") for axis in "XYZ"}
        if len(normal_swizzle) != 3 or any(str(value).upper() not in valid_swizzle for value in normal_swizzle):
            raise ValueError(f"normal_swizzle must contain three values from {sorted(valid_swizzle)}")
        extension_formats = {
            ".png": "PNG",
            ".tif": "TIFF",
            ".tiff": "TIFF",
            ".exr": "OPEN_EXR",
            ".jpg": "JPEG",
        }
        if path.suffix.lower() not in extension_formats:
            raise ValueError("output_path extension must be .png, .tif, .tiff, .exr, or .jpg")
        image = bpy.data.images.new(name=f"{obj.name}_{bake_type}", width=width, height=height, alpha=False)
        image.filepath_raw = str(path)
        image.file_format = extension_formats[path.suffix.lower()]
        scene = bpy.context.scene
        prior_engine, prior_uv = scene.render.engine, obj.data.uv_layers.active
        material_states, temporary_material = [], None
        try:
            obj.data.uv_layers.active = uv_layer
            if not obj.material_slots:
                temporary_material = bpy.data.materials.new(name=f"{obj.name}_BakeTarget")
                temporary_material.use_nodes = True
                obj.data.materials.append(temporary_material)
            for slot in obj.material_slots:
                material = slot.material
                if material is None:
                    continue
                previous_use_nodes = material.use_nodes
                material.use_nodes = True
                nodes = material.node_tree.nodes
                previous_active = nodes.active
                node = nodes.new("ShaderNodeTexImage")
                node.image = image
                nodes.active = node
                material_states.append((material, previous_use_nodes, previous_active, node))
            if not material_states:
                raise ValueError("The low-poly object has no usable material slot for an active bake image")
            with preserve_mode_and_selection():
                scene.render.engine = "CYCLES"
                bpy.ops.object.select_all(action="DESELECT")
                for high in high_objects:
                    high.select_set(True)
                obj.select_set(True)
                bpy.context.view_layer.objects.active = obj
                settings = scene.render.bake
                settings.use_selected_to_active = bool(high_objects)
                settings.use_cage = cage is not None
                settings.cage_object = cage
                settings.cage_extrusion = extrusion
                settings.max_ray_distance = ray_distance
                settings.margin = margin
                settings.normal_space = normal_space
                settings.normal_r, settings.normal_g, settings.normal_b = tuple(
                    str(value).upper() for value in normal_swizzle
                )
                _require_finished(bpy.ops.object.bake(type=bake_types[bake_type]), f"{bake_type} Bake")
            image.save_render(filepath=str(path), scene=scene)
            if not path.is_file():
                raise RuntimeError(f"Bake completed but output file was not written: {path}")
        except Exception:
            if not existed:
                with contextlib.suppress(OSError):
                    os.unlink(path)
            raise
        finally:
            scene.render.engine = prior_engine
            obj.data.uv_layers.active = prior_uv
            for material, previous_use_nodes, previous_active, node in material_states:
                with contextlib.suppress(Exception):
                    material.node_tree.nodes.remove(node)
                with contextlib.suppress(Exception):
                    material.node_tree.nodes.active = previous_active
                material.use_nodes = previous_use_nodes
            if temporary_material:
                with contextlib.suppress(Exception):
                    obj.data.materials.pop(index=len(obj.data.materials) - 1)
                with contextlib.suppress(Exception):
                    bpy.data.materials.remove(temporary_material)
        return {
            "name": obj.name,
            "high_poly_objects": [item.name for item in high_objects],
            "map_type": bake_type,
            "image": image.name,
            "dimensions": [width, height],
            "uv_map": uv_layer.name,
            "cage_object": cage.name if cage else None,
            "output_path": str(path),
            "overwrote_existing": existed,
        }
