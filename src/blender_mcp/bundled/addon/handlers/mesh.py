import bpy

from ..helpers import (
    apply_modifier,
    edit_mesh,
    get_mesh_object,
    mesh_counts,
    modifier_result,
    preserve_mode_and_selection,
    set_active,
)
from .texture import TextureHandlers

_SYMMETRIZE_DIRECTIONS = {
    "NEGATIVE_X",
    "POSITIVE_X",
    "NEGATIVE_Y",
    "POSITIVE_Y",
    "NEGATIVE_Z",
    "POSITIVE_Z",
}


class MeshHandlersMixin(TextureHandlers):
    """Provide handlers for creating and editing mesh objects."""

    # region Mesh editing handlers
    _PRIMITIVE_OPS = {
        "CUBE": lambda size, location, rotation: bpy.ops.mesh.primitive_cube_add(
            size=size, location=location, rotation=rotation
        ),
        "SPHERE": lambda size, location, rotation: bpy.ops.mesh.primitive_uv_sphere_add(
            radius=size, location=location, rotation=rotation
        ),
        "CYLINDER": lambda size, location, rotation: bpy.ops.mesh.primitive_cylinder_add(
            radius=size, depth=size * 2, location=location, rotation=rotation
        ),
        "CONE": lambda size, location, rotation: bpy.ops.mesh.primitive_cone_add(
            radius1=size, depth=size * 2, location=location, rotation=rotation
        ),
        "TORUS": lambda size, location, rotation: bpy.ops.mesh.primitive_torus_add(
            major_radius=size,
            minor_radius=size * 0.25,
            location=location,
            rotation=rotation,
        ),
        "PLANE": lambda size, location, rotation: bpy.ops.mesh.primitive_plane_add(
            size=size, location=location, rotation=rotation
        ),
        "CURVE": lambda size, location, rotation: bpy.ops.curve.primitive_bezier_curve_add(
            radius=size, location=location, rotation=rotation
        ),
    }

    def create_primitive(
        self,
        primitive_type,
        name=None,
        location=(0, 0, 0),
        rotation=(0, 0, 0),
        size=1.0,
        dimensions=None,
        purpose=None,
    ):
        """
        Create a mesh/curve primitive: cube, sphere, cylinder, cone, torus, plane, or curve.

        dimensions, if given, sets the object's world-space bounding box after
        creation (overriding size for footprint) so the same dimensions mean the
        same physical footprint across primitive types. purpose="blockout" tags
        the object as a placeholder proxy for later refinement.

        Args:
            primitive_type: Value for primitive type.
            name: Name to assign or look up.
            location: World-space location.
            rotation: Rotation value in radians.
            size: Size used to create or modify the object.
            dimensions: Value for dimensions.
            purpose: Value for purpose.

        Returns:
            Result produced by the operation.

        Raises:
            ValueError: If the operation cannot be completed.

        """
        ptype = str(primitive_type).upper()
        op = self._PRIMITIVE_OPS.get(ptype)
        if not op:
            raise ValueError(f"Unknown primitive_type: {primitive_type}. Must be one of {sorted(self._PRIMITIVE_OPS)}")
        if purpose is not None and purpose != "blockout":
            raise ValueError(f"Invalid purpose: {purpose}. Must be 'blockout' or omitted")
        with preserve_mode_and_selection():
            op(size, tuple(location), tuple(rotation))
            obj = bpy.context.active_object
            if obj is None:
                raise ValueError(f"Blender left no active object after adding primitive_type '{ptype}'")
        if name:
            obj.name = name
        if dimensions is not None:
            obj.dimensions = tuple(dimensions)
        if purpose == "blockout":
            obj["blockout"] = True
        result = {
            "name": obj.name,
            "type": obj.type,
            "location": [obj.location.x, obj.location.y, obj.location.z],
        }
        if obj.type == "MESH":
            result.update(mesh_counts(obj))
        if dimensions is not None:
            result["dimensions"] = [obj.dimensions.x, obj.dimensions.y, obj.dimensions.z]
            result["scale"] = [obj.scale.x, obj.scale.y, obj.scale.z]
        return result

    def mesh_extrude(self, object_name, offset=(0, 0, 1), face_indices=None):
        """
        Extrude the selected (or all) faces of a mesh by offset.

        Args:
            object_name: Name of the Blender object to operate on.
            offset: Zero-based starting position.
            face_indices: Indices of faces to operate on.

        Returns:
            Result produced by the operation.

        Raises:
            RuntimeError: If the operation cannot be completed.

        """
        obj = get_mesh_object(object_name)
        with edit_mesh(obj, face_indices=face_indices):
            result = bpy.ops.mesh.extrude_region_move(TRANSFORM_OT_translate={"value": tuple(offset)})
            if "FINISHED" not in result:
                raise RuntimeError(f"mesh.extrude_region_move did not finish (status: {result})")
        return {"name": obj.name, **mesh_counts(obj)}

    def mesh_inset(self, object_name, thickness=0.05, depth=0.0, face_indices=None):
        """
        Inset the selected (or all) faces of a mesh.

        Args:
            object_name: Name of the Blender object to operate on.
            thickness: Value for thickness.
            depth: Value for depth.
            face_indices: Indices of faces to operate on.

        Returns:
            Result produced by the operation.

        Raises:
            RuntimeError: If the operation cannot be completed.

        """
        obj = get_mesh_object(object_name)
        with edit_mesh(obj, face_indices=face_indices):
            result = bpy.ops.mesh.inset(thickness=thickness, depth=depth)
            if "FINISHED" not in result:
                raise RuntimeError(f"mesh.inset did not finish (status: {result})")
        return {"name": obj.name, **mesh_counts(obj)}

    def mesh_bevel(
        self,
        object_name,
        offset=0.05,
        segments=1,
        affect="EDGES",
        edge_indices=None,
        vertex_indices=None,
    ):
        """
        Bevel the selected (or all) edges/vertices of a mesh.

        Args:
            object_name: Name of the Blender object to operate on.
            offset: Zero-based starting position.
            segments: Value for segments.
            affect: Value for affect.
            edge_indices: Indices of edges to operate on.
            vertex_indices: Indices of vertices to operate on.

        Returns:
            Result produced by the operation.

        Raises:
            RuntimeError: If the operation cannot be completed.

        """
        obj = get_mesh_object(object_name)
        with edit_mesh(obj, vert_indices=vertex_indices, edge_indices=edge_indices):
            result = bpy.ops.mesh.bevel(offset=offset, segments=segments, affect=affect)
            if "FINISHED" not in result:
                raise RuntimeError(f"mesh.bevel did not finish (status: {result})")
        return {"name": obj.name, **mesh_counts(obj)}

    def mesh_symmetrize(self, object_name, direction="NEGATIVE_X"):
        """
        Symmetrize a mesh across an axis, mirroring one half of the geometry onto the other.

        Args:
            object_name: Name of the Blender object to operate on.
            direction: Value for direction.

        Returns:
            Result produced by the operation.

        Raises:
            ValueError: If the operation cannot be completed.
            RuntimeError: If the operation cannot be completed.

        """
        direction = str(direction).upper()
        if direction not in _SYMMETRIZE_DIRECTIONS:
            raise ValueError(f"Invalid direction: {direction}. Must be one of {sorted(_SYMMETRIZE_DIRECTIONS)}")
        obj = get_mesh_object(object_name)
        with edit_mesh(obj):
            result = bpy.ops.mesh.symmetrize(direction=direction)
            if "FINISHED" not in result:
                raise RuntimeError(f"mesh.symmetrize did not finish (status: {result})")
        return {"name": obj.name, **mesh_counts(obj)}

    def mesh_boolean(self, object_name, cutter_object_name, operation="DIFFERENCE", keep_cutter=True):
        """
        Apply a boolean modifier between two mesh objects, deleting the cutter unless keep_cutter.

        Args:
            object_name: Name of the Blender object to operate on.
            cutter_object_name: Name of the cutter object.
            operation: Value for operation.
            keep_cutter: Whether to p cutter.

        Returns:
            Result produced by the operation.

        Raises:
            ValueError: If the operation cannot be completed.

        """
        operation = str(operation).upper()
        if operation not in {"UNION", "DIFFERENCE", "INTERSECT"}:
            raise ValueError(f"Invalid operation: {operation}. Must be one of UNION, DIFFERENCE, INTERSECT")
        if object_name == cutter_object_name:
            raise ValueError(f"cutter_object_name must differ from object_name (both are '{object_name}')")
        obj = get_mesh_object(object_name)
        cutter = get_mesh_object(cutter_object_name)
        mod = obj.modifiers.new(name="Boolean", type="BOOLEAN")
        mod.object = cutter
        mod.operation = operation
        apply_modifier(obj, mod)
        if not keep_cutter:
            bpy.data.objects.remove(cutter, do_unlink=True)
        return {"name": obj.name, **mesh_counts(obj)}

    def mesh_subdivide(self, object_name, cuts=1, face_indices=None):
        """
        Subdivide the selected (or all) faces of a mesh.

        Args:
            object_name: Name of the Blender object to operate on.
            cuts: Value for cuts.
            face_indices: Indices of faces to operate on.

        Returns:
            Result produced by the operation.

        Raises:
            RuntimeError: If the operation cannot be completed.

        """
        obj = get_mesh_object(object_name)
        with edit_mesh(obj, face_indices=face_indices):
            result = bpy.ops.mesh.subdivide(number_cuts=cuts)
            if "FINISHED" not in result:
                raise RuntimeError(f"mesh.subdivide did not finish (status: {result})")
        return {"name": obj.name, **mesh_counts(obj)}

    def mesh_remesh(self, object_name, voxel_size=0.1):
        """
        Voxel-remesh a mesh object, rebuilding its topology at the given voxel size.

        Args:
            object_name: Name of the Blender object to operate on.
            voxel_size: Value for voxel size.

        Returns:
            Result produced by the operation.

        Raises:
            RuntimeError: If the operation cannot be completed.

        """
        obj = get_mesh_object(object_name)
        obj.data.remesh_voxel_size = voxel_size
        with preserve_mode_and_selection():
            set_active(obj)
            result = bpy.ops.object.voxel_remesh()
        if "FINISHED" not in result:
            raise RuntimeError(f"object.voxel_remesh did not finish (status: {result})")
        return {"name": obj.name, **mesh_counts(obj)}

    def mesh_solidify(self, object_name, thickness=0.01, apply=False):
        """
        Add thickness to a mesh's surface via a Solidify modifier.

        Args:
            object_name: Name of the Blender object to operate on.
            thickness: Value for thickness.
            apply: Value for apply.

        Returns:
            Result produced by the operation.

        """
        obj = get_mesh_object(object_name)
        mod = obj.modifiers.new(name="Solidify", type="SOLIDIFY")
        mod.thickness = thickness
        if apply:
            apply_modifier(obj, mod)
        return {"name": obj.name, **modifier_result(obj, mod, apply)}

    def clear_materials(self, object_names):
        """
        Remove all material slots from the given objects.

        Args:
            object_names: Names of Blender objects to operate on.

        Returns:
            Result produced by the operation.

        Raises:
            ValueError: If the operation cannot be completed.

        """
        if not object_names:
            raise ValueError("At least one object name is required")
        objs = []
        for name in object_names:
            obj = bpy.data.objects.get(name)
            if not obj:
                raise ValueError(f"Object not found: {name}")
            objs.append(obj)
        for obj in objs:
            if obj.data is not None and hasattr(obj.data, "materials"):
                obj.data.materials.clear()
        return {"names": [obj.name for obj in objs]}

    def clear_vertex_groups(self, object_name):
        """
        Remove all vertex groups from a mesh object.

        Args:
            object_name: Name of the Blender object to operate on.

        Returns:
            Result produced by the operation.

        """
        obj = get_mesh_object(object_name)
        obj.vertex_groups.clear()
        return {"name": obj.name}

    def clear_edge_marks(self, object_name):
        """
        Clear sharp, seam, and freestyle edge marks on a mesh object.

        Args:
            object_name: Name of the Blender object to operate on.

        Returns:
            Result produced by the operation.

        """
        obj = get_mesh_object(object_name)
        for edge in obj.data.edges:
            edge.use_edge_sharp = False
            edge.use_seam = False
            edge.use_freestyle_mark = False
        return {"name": obj.name}

    # endregion
