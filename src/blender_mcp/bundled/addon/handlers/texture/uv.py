# pyright: reportAttributeAccessIssue=false, reportOptionalSubscript=false
"""UV map lifecycle, seam editing, unwrap, optimization, and audit handlers."""

import math
import statistics

import bpy

from ...helpers import edit_mesh
from ..retopology._shared import _require_finished, _uv_overlap_pairs
from ._shared import material_by_name, mesh_object, required_name


def _uv_references(name):
    references = []
    for material in bpy.data.materials:
        if not material.use_nodes or material.node_tree is None:
            continue
        for node in material.node_tree.nodes:
            if node.bl_idname == "ShaderNodeUVMap" and node.uv_map == name:
                references.append({"material": material.name, "node": node.name})
    return references


def _uv_metrics(obj, layer, overlap_pair_limit):
    zero_area, outside, mirrored, stretch, density = [], set(), [], [], []
    summed_uv_area = 0.0
    min_u = min_v = math.inf
    max_u = max_v = -math.inf
    for polygon in obj.data.polygons:
        uvs = [layer.data[index].uv for index in polygon.loop_indices]
        if len(uvs) < 3:
            continue
        signed_area = (
            sum(
                uvs[index].x * uvs[(index + 1) % len(uvs)].y - uvs[(index + 1) % len(uvs)].x * uvs[index].y
                for index in range(len(uvs))
            )
            * 0.5
        )
        uv_area = abs(signed_area)
        summed_uv_area += uv_area
        if uv_area <= 1e-12:
            zero_area.append(polygon.index)
        if signed_area < -1e-12:
            mirrored.append(polygon.index)
        for uv in uvs:
            min_u, min_v = min(min_u, uv.x), min(min_v, uv.y)
            max_u, max_v = max(max_u, uv.x), max(max_v, uv.y)
            if uv.x < -1e-8 or uv.x > 1.0 + 1e-8 or uv.y < -1e-8 or uv.y > 1.0 + 1e-8:
                outside.add(polygon.index)
        world_vertices = [obj.matrix_world @ obj.data.vertices[index].co for index in polygon.vertices]
        origin = world_vertices[0]
        world_area = sum(
            (world_vertices[index] - origin).cross(world_vertices[index + 1] - origin).length * 0.5
            for index in range(1, len(world_vertices) - 1)
        )
        if world_area > 1e-12 and uv_area > 1e-12:
            ratio = uv_area / world_area
            density.append(math.sqrt(ratio))
            stretch.append(max(ratio, 1.0 / ratio))
    overlap_probe_limit = int(overlap_pair_limit) + 1
    prior_active = obj.data.uv_layers.active
    try:
        obj.data.uv_layers.active = layer
        overlaps = _uv_overlap_pairs(obj.data, overlap_probe_limit) if overlap_pair_limit else []
    finally:
        obj.data.uv_layers.active = prior_active
    if overlaps is None:
        overlaps, truncated = [], True
    else:
        truncated = len(overlaps) > int(overlap_pair_limit)
    overlaps = overlaps[: int(overlap_pair_limit)]
    edge_faces = {}
    for polygon in obj.data.polygons:
        for key in polygon.edge_keys:
            edge_faces.setdefault(key, []).append(polygon.index)
    adjacency = {polygon.index: set() for polygon in obj.data.polygons}
    for edge in obj.data.edges:
        faces = edge_faces.get(edge.key, [])
        if len(faces) != 2:
            continue
        face_uvs = []
        for face_index in faces:
            polygon = obj.data.polygons[face_index]
            values = {}
            for loop_index in polygon.loop_indices:
                vertex_index = obj.data.loops[loop_index].vertex_index
                if vertex_index in edge.vertices:
                    values[vertex_index] = layer.data[loop_index].uv
            face_uvs.append(values)
        continuous = all(
            vertex_index in face_uvs[0]
            and vertex_index in face_uvs[1]
            and (face_uvs[0][vertex_index] - face_uvs[1][vertex_index]).length_squared <= 1e-16
            for vertex_index in edge.vertices
        )
        if continuous:
            adjacency[faces[0]].add(faces[1])
            adjacency[faces[1]].add(faces[0])
    island_count, remaining = 0, set(adjacency)
    while remaining:
        island_count += 1
        stack = [remaining.pop()]
        while stack:
            connected = adjacency[stack.pop()] & remaining
            remaining -= connected
            stack.extend(connected)
    mean_density = statistics.fmean(density) if density else None
    return {
        "uv_map": layer.name,
        "island_count": island_count,
        "bounds": None if min_u == math.inf else {"minimum": [min_u, min_v], "maximum": [max_u, max_v]},
        "zero_area_faces": zero_area,
        "overlap_pairs": overlaps,
        "overlap_truncated": truncated,
        "mirrored_faces": mirrored,
        "out_of_range_faces": sorted(outside),
        "occupied_area_estimate": min(summed_uv_area, 1.0),
        "padding_estimate": None,
        "padding_note": "Padding cannot be inferred reliably without a target texture resolution.",
        "stretch": {
            "mean": statistics.fmean(stretch) if stretch else None,
            "maximum": max(stretch) if stretch else None,
        },
        "texel_density_uv_per_world_unit": {
            "mean": mean_density,
            "minimum": min(density) if density else None,
            "maximum": max(density) if density else None,
            "coefficient_of_variation": statistics.pstdev(density) / mean_density if density and mean_density else None,
        },
    }


def _validate_faces(obj, face_indices):
    if face_indices is None:
        return None
    invalid = [index for index in face_indices if not isinstance(index, int) or not 0 <= index < len(obj.data.polygons)]
    if invalid:
        raise ValueError(f"Invalid face indices for '{obj.name}': {invalid[:10]}")
    return list(dict.fromkeys(face_indices))


class TextureUVHandlers:
    """Inspect and safely mutate UV layers and seam attributes."""

    def manage_uv_maps(
        self, object_name, action, uv_map_name=None, new_name=None, source_uv_map_name=None, confirm=False
    ):
        obj, action = mesh_object(object_name), str(action).upper()
        layers = obj.data.uv_layers
        if action == "LIST":
            return {
                "object": obj.name,
                "uv_maps": [
                    {
                        "name": layer.name,
                        "active": layer == layers.active,
                        "active_render": bool(layer.active_render),
                        "references": _uv_references(layer.name),
                    }
                    for layer in layers
                ],
            }
        if action == "CREATE":
            name = required_name(new_name or uv_map_name, "new_name")
            if layers.get(name):
                raise ValueError(f"UV map already exists: {name}")
            layer = layers.new(name=name, do_init=False)
        elif action == "DUPLICATE":
            source = layers.get(required_name(source_uv_map_name, "source_uv_map_name"))
            if source is None:
                raise ValueError(f"UV map not found: {source_uv_map_name}")
            name = required_name(new_name, "new_name")
            if layers.get(name):
                raise ValueError(f"UV map already exists: {name}")
            old_active = layers.active
            layers.active = source
            layer = layers.new(name=name, do_init=True)
            layers.active = old_active
        else:
            layer = layers.get(required_name(uv_map_name, "uv_map_name"))
            if layer is None:
                raise ValueError(f"UV map not found: {uv_map_name}")
            if action == "RENAME":
                name = required_name(new_name, "new_name")
                if layers.get(name):
                    raise ValueError(f"UV map already exists: {name}")
                old_name = layer.name
                layer.name = name
                for reference in _uv_references(old_name):
                    material_by_name(reference["material"]).node_tree.nodes[reference["node"]].uv_map = name
            elif action == "ACTIVATE":
                layers.active = layer
            elif action == "SET_RENDER":
                layer.active_render = True
            elif action == "REMOVE":
                if not confirm:
                    raise ValueError("Removing a UV map requires confirm=True")
                references = _uv_references(layer.name)
                layers.remove(layer)
                return {
                    "object": obj.name,
                    "action": action,
                    "removed": uv_map_name,
                    "material_references": references,
                    "changed_objects": [obj.name],
                }
            else:
                raise ValueError("Unsupported UV map action")
        return {"object": obj.name, "action": action, "uv_map": layer.name, "changed_objects": [obj.name]}

    def set_uv_seams(self, object_name, action, edge_indices=None, rule=None, angle_threshold=None):
        obj, action = mesh_object(object_name), str(action).upper()
        if action not in {"MARK", "CLEAR"}:
            raise ValueError("action must be MARK or CLEAR")
        if edge_indices is not None:
            invalid = [
                index for index in edge_indices if not isinstance(index, int) or not 0 <= index < len(obj.data.edges)
            ]
            if invalid:
                raise ValueError(f"Invalid edge indices: {invalid[:10]}")
            selected = list(dict.fromkeys(edge_indices))
        else:
            rule = str(rule).upper()
            if rule == "BOUNDARY":
                counts = {}
                for polygon in obj.data.polygons:
                    for key in polygon.edge_keys:
                        counts[key] = counts.get(key, 0) + 1
                selected = [edge.index for edge in obj.data.edges if counts.get(edge.key, 0) == 1]
            elif rule == "SHARP":
                selected = [edge.index for edge in obj.data.edges if edge.use_edge_sharp]
            elif rule == "ANGLE":
                threshold = float(angle_threshold)
                edge_faces = {}
                for polygon in obj.data.polygons:
                    for key in polygon.edge_keys:
                        edge_faces.setdefault(key, []).append(polygon)
                selected = []
                for edge in obj.data.edges:
                    faces = edge_faces.get(edge.key, [])
                    if len(faces) == 2 and faces[0].normal.angle(faces[1].normal, 0.0) >= threshold:
                        selected.append(edge.index)
            else:
                raise ValueError("rule must be BOUNDARY, SHARP, or ANGLE")
        target = action == "MARK"
        changed = [index for index in selected if bool(obj.data.edges[index].use_seam) != target]
        for index in changed:
            obj.data.edges[index].use_seam = target
        obj.data.update()
        return {
            "object": obj.name,
            "action": action,
            "changed_edge_indices": changed,
            "changed_objects": [obj.name] if changed else [],
        }

    def unwrap_uvs(
        self, object_name, uv_map_name, method="ANGLE_BASED", face_indices=None, create_if_missing=True, margin=0.001
    ):
        obj, name = mesh_object(object_name), required_name(uv_map_name, "uv_map_name")
        faces = _validate_faces(obj, face_indices)
        layer = obj.data.uv_layers.get(name)
        created = False
        if layer is None:
            if not create_if_missing:
                raise ValueError(f"UV map not found: {name}")
            layer = obj.data.uv_layers.new(name=name, do_init=False)
            created = True
        old_active = obj.data.uv_layers.active
        try:
            obj.data.uv_layers.active = layer
            with edit_mesh(obj, face_indices=faces):
                _require_finished(bpy.ops.uv.unwrap(method=str(method).upper(), margin=float(margin)), "UV Unwrap")
        except Exception:
            if created:
                obj.data.uv_layers.remove(layer)
            raise
        finally:
            if not created and old_active is not None:
                obj.data.uv_layers.active = old_active
        return {
            "object": obj.name,
            "uv_map": name,
            "created": created,
            "face_count": len(obj.data.polygons) if faces is None else len(faces),
            "changed_objects": [obj.name],
        }

    def optimize_uv_layout(
        self,
        object_name,
        uv_map_name,
        face_indices=None,
        average_island_scale=True,
        minimize_stretch_iterations=10,
        pack_islands=True,
        rotate=True,
        scale=True,
        margin_method="SCALED",
        margin=0.001,
        udim_source="CLOSEST_UDIM",
    ):
        obj = mesh_object(object_name)
        layer = obj.data.uv_layers.get(required_name(uv_map_name, "uv_map_name"))
        if layer is None:
            raise ValueError(f"UV map not found: {uv_map_name}")
        faces, stages = _validate_faces(obj, face_indices), []
        old_active = obj.data.uv_layers.active
        try:
            obj.data.uv_layers.active = layer
            with edit_mesh(obj, face_indices=faces):
                if average_island_scale:
                    _require_finished(
                        bpy.ops.uv.average_islands_scale(scale_uv=False, shear=False), "Average Islands Scale"
                    )
                    stages.append("AVERAGE_ISLAND_SCALE")
                if int(minimize_stretch_iterations):
                    _require_finished(
                        bpy.ops.uv.minimize_stretch(
                            fill_holes=True, blend=0.0, iterations=int(minimize_stretch_iterations)
                        ),
                        "Minimize Stretch",
                    )
                    stages.append("MINIMIZE_STRETCH")
                if pack_islands:
                    _require_finished(
                        bpy.ops.uv.pack_islands(
                            rotate=bool(rotate),
                            scale=bool(scale),
                            margin_method=margin_method,
                            margin=float(margin),
                            udim_source=udim_source,
                        ),
                        "Pack Islands",
                    )
                    stages.append("PACK_ISLANDS")
        finally:
            if old_active is not None:
                obj.data.uv_layers.active = old_active
        return {
            "object": obj.name,
            "uv_map": layer.name,
            "stages": stages,
            "metrics": _uv_metrics(obj, layer, 100),
            "changed_objects": [obj.name] if stages else [],
        }

    def inspect_uv_layout(self, object_name, uv_map_name=None, overlap_pair_limit=100):
        obj = mesh_object(object_name)
        if obj.mode == "EDIT":
            obj.update_from_editmode()
        layers = [obj.data.uv_layers.get(uv_map_name)] if uv_map_name else list(obj.data.uv_layers)
        if any(layer is None for layer in layers):
            raise ValueError(f"UV map not found: {uv_map_name}")
        return {
            "object": obj.name,
            "uv_maps": [_uv_metrics(obj, layer, overlap_pair_limit) for layer in layers],
            "world_scale": list(obj.matrix_world.to_scale()),
            "read_only": True,
        }
