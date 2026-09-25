"""Scene inspection: `list_scene_objects`, `get_object_info` and `get_mesh_data`."""

import itertools
import logging

import bpy
import mathutils

from ..helpers import get_mesh_object, page_records, paginate, sync_from_editmode
from ..object_lookup import find_object
from ..text_hygiene import client_safe_name_leaf

logger = logging.getLogger(__name__)


class SceneInspectionHandlersMixin:
    """Read the scene, one object, or one mesh's elements, a bounded page at a time."""

    _SCENE_INFO_MAX_LIMIT = 200

    def list_scene_objects(self, limit=25, offset=0, search=None):
        """
        Get information about the current Blender scene, paginated over its objects.

        Args:
            limit: Maximum number of items to return.
            offset: Zero-based starting position.
            search: Case-insensitive substring an object's name must contain; None or empty
                pages every object.

        Returns:
            Result produced by the operation.

        """
        try:
            return self._scene_objects_page(limit, offset, search)
        except Exception as e:
            logger.exception("list_scene_objects failed")
            return {"error": str(e)}

    def _scene_objects_page(self, limit, offset, search):
        """
        Build `list_scene_objects`' reply; reporting a failure is left to it.

        Args:
            limit: Maximum number of items to return.
            offset: Zero-based starting position.
            search: Case-insensitive substring an object's name must contain; None or empty
                pages every object.

        Returns:
            dict: The scene summary and one page of its objects.

        Raises:
            ValueError: When `search` is not a string.

        """
        if search is not None and not isinstance(search, str):
            raise ValueError("search must be a string")
        query = (search or "").casefold()
        every_object = list(bpy.context.scene.objects)
        scene_objects = sorted(
            (obj for obj in every_object if query in obj.name.casefold()), key=lambda item: item.name.casefold()
        )
        total = len(scene_objects)
        start, end, truncated, next_offset = paginate(total, offset, limit, self._SCENE_INFO_MAX_LIMIT)

        objects = []
        for obj in scene_objects[start:end]:
            objects.append(
                {
                    "name": obj.name,
                    "type": obj.type,
                    # Only include basic location data
                    "location": [
                        float(obj.location.x),
                        float(obj.location.y),
                        float(obj.location.z),
                    ],
                    "parent": obj.parent.name if obj.parent else None,
                    "collections": sorted(collection.name for collection in obj.users_collection),
                    "selected": bool(obj.select_get()),
                    "visible": bool(obj.visible_get()),
                    "hide_viewport": bool(obj.hide_viewport),
                    "hide_render": bool(obj.hide_render),
                }
            )

        scene_info = {
            "name": bpy.context.scene.name,
            "object_count": len(every_object),
            "matched_count": total,
            "search": search or None,
            "objects": objects,
            "materials_count": len(bpy.data.materials),
            "active_object": getattr(getattr(bpy.context, "view_layer", None), "objects", None).active.name
            if getattr(getattr(getattr(bpy.context, "view_layer", None), "objects", None), "active", None)
            else None,
            # A count, not the names: a set dressed by linking selects hundreds of objects, and
            # that list beside the page pushed every page over the reply budget. Each record
            # carries its own `selected`.
            "selected_count": len(getattr(bpy.context, "selected_objects", ())),
            "mode": getattr(bpy.context, "mode", "UNKNOWN"),
            "unit_settings": {
                "system": bpy.context.scene.unit_settings.system,
                "scale_length": bpy.context.scene.unit_settings.scale_length,
                "length_unit": bpy.context.scene.unit_settings.length_unit,
            },
            "offset": start,
            "limit": limit,
            "returned_count": len(objects),
            "truncated": truncated,
            "next_offset": next_offset,
        }

        logger.debug("Scene info collected: %d of %d objects", len(objects), total)
        return scene_info

    @staticmethod
    def get_aabb(obj):
        """
        Return the world-space axis-aligned bounding box (AABB) of an object.

        Args:
            obj: Value for obj.

        Returns:
            the world-space axis-aligned bounding box (AABB) of an object.

        Raises:
            TypeError: If the operation cannot be completed.

        """
        if obj.type != "MESH":
            raise TypeError("Object must be a mesh")

        # Get the bounding box corners in local space
        local_bbox_corners = [mathutils.Vector(corner) for corner in obj.bound_box]

        # Convert to world coordinates
        world_bbox_corners = [obj.matrix_world @ corner for corner in local_bbox_corners]

        # Compute axis-aligned min/max coordinates
        min_corner = mathutils.Vector(map(min, zip(*world_bbox_corners, strict=False)))
        max_corner = mathutils.Vector(map(max, zip(*world_bbox_corners, strict=False)))

        return [[*min_corner], [*max_corner]]

    def get_object_info(self, name, sections=None, limit=100, offset=0):
        """
        Get detailed information about a specific object.

        `location`/`rotation`/`scale` are the object's local (parent-relative)
        transform; `world_bounding_box` (mesh objects only) is the world-space
        AABB computed via `matrix_world` (see `get_aabb`) - the two live in
        different spaces and are not directly comparable for a parented or
        transformed object.

        `rotation_mode` names how to read `rotation`: one of the six Euler
        orders ("XYZ", "XZY", "YXZ", "YZX", "ZXY", "ZYX") means
        `[x, y, z]` radians in that order; "QUATERNION" means `[w, x, y, z]`;
        "AXIS_ANGLE" means `[angle, x, y, z]`. Reading `rotation` without
        checking `rotation_mode` will misinterpret non-Euler objects.

        `mesh.vertices`/`edges`/`polygons` are base-mesh (pre-modifier)
        counts, same caveat as `get_mesh_data`.

        Args:
            name: Name to assign or look up.
            sections: The `type_data` sections to report, case-insensitive; None reports
                every one.
            limit: Maximum number of records one `type_data` list returns.
            offset: Zero-based starting position within each `type_data` list.

        Returns:
            Result produced by the operation.

        Raises:
            ValueError: If the operation cannot be completed.

        """
        if sections is not None:
            allowed_sections = {
                "GEOMETRY",
                "ATTRIBUTES",
                "VOLUME_GRIDS",
                "GREASE_PENCIL",
                "PARTICLES",
                "SOFT_BODY",
                "DYNAMIC_PAINT",
            }
            sections = {str(section).upper() for section in sections}
            unknown = sorted(sections - allowed_sections)
            if unknown:
                raise ValueError(f"Unsupported object-info sections: {unknown}")
        else:
            sections = {
                "GEOMETRY",
                "ATTRIBUTES",
                "VOLUME_GRIDS",
                "GREASE_PENCIL",
                "PARTICLES",
                "SOFT_BODY",
                "DYNAMIC_PAINT",
            }
        obj = find_object(bpy.data.objects, name)
        if not obj:
            raise ValueError(f"Object not found: {name}")
        sync_from_editmode(obj)

        if obj.rotation_mode == "QUATERNION":
            q = obj.rotation_quaternion
            rotation = [q.w, q.x, q.y, q.z]
        elif obj.rotation_mode == "AXIS_ANGLE":
            angle, x, y, z = obj.rotation_axis_angle
            rotation = [angle, x, y, z]
        else:
            rotation = [obj.rotation_euler.x, obj.rotation_euler.y, obj.rotation_euler.z]

        # Basic object info
        obj_info = {
            "name": obj.name,
            "type": obj.type,
            "library": client_safe_name_leaf(obj.library.name) if getattr(obj, "library", None) else None,
            "is_override": getattr(obj, "override_library", None) is not None,
            "location": [obj.location.x, obj.location.y, obj.location.z],
            "rotation_mode": obj.rotation_mode,
            "rotation": rotation,
            "scale": [obj.scale.x, obj.scale.y, obj.scale.z],
            "matrix_world": [[float(value) for value in row] for row in obj.matrix_world],
            "dimensions": [float(value) for value in obj.dimensions],
            "parent": obj.parent.name if obj.parent else None,
            "parent_type": obj.parent_type,
            "parent_bone": obj.parent_bone or None,
            "collections": sorted(collection.name for collection in obj.users_collection),
            "data_name": obj.data.name if obj.data else None,
            "selected": bool(obj.select_get()),
            "visible": obj.visible_get(),
            "hide_viewport": bool(obj.hide_viewport),
            "hide_render": bool(obj.hide_render),
            "materials": [],
            "modifiers": [
                {
                    "name": m.name,
                    "type": m.type,
                    "show_viewport": m.show_viewport,
                    "show_render": m.show_render,
                }
                for m in obj.modifiers
            ],
        }

        if obj.type == "MESH":
            bounding_box = self.get_aabb(obj)
            obj_info["world_bounding_box"] = bounding_box

        # Add material slots
        for slot in obj.material_slots:
            if slot.material:
                obj_info["materials"].append(slot.material.name)

        # Add mesh data if applicable
        if obj.type == "MESH" and obj.data:
            mesh = obj.data
            obj_info["mesh"] = {
                "vertices": len(mesh.vertices),
                "edges": len(mesh.edges),
                "polygons": len(mesh.polygons),
            }

        type_data = self._object_type_data(obj, sections, limit, offset)
        if type_data:
            obj_info["type_data"] = type_data

        return obj_info

    # The largest page one `type_data` list carries.
    _OBJECT_INFO_MAX_LIMIT = 1000
    # `coordinate_space` and `evaluated`, which every `type_data` block opens with: a block
    # holding nothing past them has nothing to report and is left out.
    _TYPE_DATA_HEADER_KEYS = 2

    @staticmethod
    def _attribute_records(data):
        return [
            {
                "name": attribute.name,
                "data_type": attribute.data_type,
                "domain": attribute.domain,
                "count": len(attribute.data),
            }
            for attribute in getattr(data, "attributes", ())
        ]

    def _object_type_data(self, obj, sections, limit, offset):
        """Return bounded, explicitly local-space native data and simulation state."""
        result = {"coordinate_space": "OBJECT_LOCAL", "evaluated": False}
        data = obj.data
        if data is not None and "ATTRIBUTES" in sections and hasattr(data, "attributes"):
            result["attributes"] = page_records(
                self._attribute_records(data), offset, limit, self._OBJECT_INFO_MAX_LIMIT
            )
        result.update(self._native_geometry_data(obj, data, sections, limit, offset))

        if "PARTICLES" in sections:
            systems = [
                {
                    "name": system.name,
                    "settings": system.settings.name if system.settings else None,
                    "particle_count": len(system.particles),
                    "seed": system.seed,
                }
                for system in getattr(obj, "particle_systems", ())
            ]
            if systems:
                result["particle_systems"] = page_records(systems, offset, limit, self._OBJECT_INFO_MAX_LIMIT)
        if "SOFT_BODY" in sections and getattr(obj, "soft_body", None) is not None:
            soft_body = obj.soft_body
            point_cache = soft_body.point_cache
            result["soft_body"] = {
                "goal": soft_body.settings.use_goal,
                "self_collision": soft_body.settings.use_self_collision,
                "cache": {
                    "frame_start": point_cache.frame_start,
                    "frame_end": point_cache.frame_end,
                    "is_baked": point_cache.is_baked,
                    "is_baking": point_cache.is_baking,
                },
            }
        if "DYNAMIC_PAINT" in sections:
            states = []
            for modifier in obj.modifiers:
                if modifier.type != "DYNAMIC_PAINT":
                    continue
                states.append(
                    {
                        "modifier": modifier.name,
                        "ui_type": modifier.ui_type,
                        "canvas_active": modifier.canvas_settings is not None,
                        "brush_active": modifier.brush_settings is not None,
                    }
                )
            if states:
                result["dynamic_paint"] = page_records(states, offset, limit, self._OBJECT_INFO_MAX_LIMIT)
        return result if len(result) > self._TYPE_DATA_HEADER_KEYS else {}

    def _native_geometry_data(self, obj, data, sections, limit, offset):
        """
        Return the one native-geometry block an object's type carries, under its `type_data` key.

        Args:
            obj: The object being inspected.
            data: `obj.data`.
            sections: The upper-cased `get_object_info` sections asked for.
            limit: The largest page each list returns.
            offset: Where each list's page starts.

        Returns:
            dict: One of `curve`, `curves`, `pointcloud`, `volume` or `grease_pencil`; empty
            for any other type, or when its section was not asked for.

        """
        result = {}
        if obj.type in {"CURVE", "SURFACE"} and "GEOMETRY" in sections:
            splines = []
            for index, spline in enumerate(data.splines):
                splines.append(
                    {
                        "index": index,
                        "type": spline.type,
                        "point_count_u": spline.point_count_u,
                        "point_count_v": spline.point_count_v,
                        "cyclic_u": spline.use_cyclic_u,
                        "cyclic_v": getattr(spline, "use_cyclic_v", False),
                        "order_u": getattr(spline, "order_u", None),
                        "order_v": getattr(spline, "order_v", None),
                        "resolution_u": spline.resolution_u,
                        "resolution_v": getattr(spline, "resolution_v", None),
                    }
                )
            result["curve"] = {
                "dimensions": data.dimensions,
                "resolution_u": data.resolution_u,
                "resolution_v": data.resolution_v,
                "bevel_depth": data.bevel_depth,
                "splines": page_records(splines, offset, limit, self._OBJECT_INFO_MAX_LIMIT),
            }
        elif obj.type == "CURVES" and "GEOMETRY" in sections:
            result["curves"] = {
                "point_count": len(data.points),
                "curve_count": len(data.curves),
                "surface": data.surface.name if getattr(data, "surface", None) else None,
            }
        elif obj.type == "POINTCLOUD" and "GEOMETRY" in sections:
            result["pointcloud"] = {"point_count": len(data.points)}
        elif obj.type == "VOLUME" and "VOLUME_GRIDS" in sections:
            grids = [
                {
                    "name": grid.name,
                    "data_type": grid.data_type,
                    "channels": grid.channels,
                    "voxel_size": list(grid.voxel_size),
                    "is_loaded": grid.is_loaded,
                }
                for grid in data.grids
            ]
            result["volume"] = {
                "filepath": data.filepath,
                "is_sequence": data.is_sequence,
                "frame_start": data.frame_start,
                "frame_duration": data.frame_duration,
                "grids": page_records(grids, offset, limit, self._OBJECT_INFO_MAX_LIMIT),
            }
        elif obj.type == "GREASEPENCIL" and "GREASE_PENCIL" in sections:
            layers = []
            for layer in data.layers:
                frames = list(layer.frames)
                layers.append(
                    {
                        "name": layer.name,
                        "frame_count": len(frames),
                        "frames": [
                            {
                                "frame_number": frame.frame_number,
                                "stroke_count": len(frame.drawing.strokes),
                                "point_count": len(frame.drawing.attributes["position"].data),
                            }
                            for frame in frames[:limit]
                        ],
                        "frames_truncated": len(frames) > limit,
                    }
                )
            result["grease_pencil"] = {"layers": page_records(layers, offset, limit, self._OBJECT_INFO_MAX_LIMIT)}
        return result

    _MESH_DATA_ELEMENT_TYPES = ("vertices", "edges", "faces", "loops")
    _MESH_DATA_MAX_LIMIT = 1000

    @staticmethod
    def _mesh_data_vertex(v):
        return {
            "index": v.index,
            "co": [v.co.x, v.co.y, v.co.z],
            "normal": [v.normal.x, v.normal.y, v.normal.z],
            "select": bool(v.select),
        }

    @staticmethod
    def _mesh_data_edge(e):
        return {
            "index": e.index,
            "vertices": list(e.vertices),
            "select": bool(e.select),
        }

    @staticmethod
    def _mesh_data_face(f):
        return {
            "index": f.index,
            "vertices": list(f.vertices),
            "normal": [f.normal.x, f.normal.y, f.normal.z],
            "select": bool(f.select),
            "material_index": f.material_index,
        }

    def get_mesh_data(self, object_name, element_type="vertices", limit=100, offset=0, selected_only=False):
        """
        Paginated inspection of a mesh's vertices/edges/faces/loops (indices, coords, normals, selection).

        Prerequisite for index-based edits: mesh_extrude/mesh_inset/mesh_bevel/
        mesh_bridge/mesh_subdivide take raw indices with no way to discover them
        otherwise, since get_object_info only reports element counts.

        Coordinates and normals come from the object's base mesh (`obj.data`)
        in local (object-space) coordinates - modifiers are not evaluated. To
        get world-space positions, transform by the object's `matrix_world`
        (see `get_object_info`).

        Args:
            object_name: Name of the Blender object to operate on.
            element_type: Value for element type.
            limit: Maximum number of items to return.
            offset: Zero-based starting position.
            selected_only: Value for selected only.

        Returns:
            Result produced by the operation.

        Raises:
            ValueError: If the operation cannot be completed.

        """
        if element_type not in self._MESH_DATA_ELEMENT_TYPES:
            raise ValueError(f"Invalid element_type: {element_type}. Must be one of {self._MESH_DATA_ELEMENT_TYPES}")
        obj = get_mesh_object(object_name)
        sync_from_editmode(obj)
        mesh = obj.data

        if element_type == "vertices":
            all_elements = mesh.vertices
            to_dict = self._mesh_data_vertex
        elif element_type == "edges":
            all_elements = mesh.edges
            to_dict = self._mesh_data_edge
        elif element_type == "faces":
            all_elements = mesh.polygons
            to_dict = self._mesh_data_face
        else:
            if selected_only:
                raise ValueError(
                    "selected_only is not supported for element_type='loops': "
                    "MeshLoop has no selection state of its own (use 'vertices', "
                    "'edges', or 'faces' instead)"
                )
            all_elements = mesh.loops
            face_of_loop = {}
            for face in mesh.polygons:
                for loop_index in face.loop_indices:
                    face_of_loop[loop_index] = face.index
            if hasattr(mesh, "calc_normals_split"):
                mesh.calc_normals_split()

            # Named and then assigned, so `to_dict` stays an ordinary variable: a
            # `def to_dict` here would declare the loop signature for the whole
            # function and reject the three bound methods above it.
            def _loop_to_dict(loop):
                normal = loop.normal
                return {
                    "index": loop.index,
                    "vertex_index": loop.vertex_index,
                    "edge_index": loop.edge_index,
                    "face_index": face_of_loop.get(loop.index),
                    "normal": [normal.x, normal.y, normal.z],
                }

            to_dict = _loop_to_dict

        total_unfiltered = len(all_elements)
        if selected_only:
            universe = [el for el in all_elements if el.select]
            total = len(universe)
            start, end, truncated, next_offset = paginate(total, offset, limit, self._MESH_DATA_MAX_LIMIT)
            page = universe[start:end]
        else:
            total = total_unfiltered
            start, end, truncated, next_offset = paginate(total, offset, limit, self._MESH_DATA_MAX_LIMIT)
            # islice avoids materializing the whole (possibly huge) collection
            # when the caller only asked for a small page of it.
            page = itertools.islice(all_elements, start, end)
        elements = [to_dict(el) for el in page]

        return {
            "name": obj.name,
            "element_type": element_type,
            "total": total,
            "total_unfiltered": total_unfiltered,
            "offset": start,
            "limit": limit,
            "returned_count": len(elements),
            "truncated": truncated,
            "next_offset": next_offset,
            "elements": elements,
        }
