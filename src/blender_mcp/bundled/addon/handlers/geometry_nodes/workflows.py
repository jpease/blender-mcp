"""Reusable Geometry Nodes workflow builders for production modeling tasks."""

# ruff: file-ignore[line-too-long]

import math
import os
import tempfile

from typing import Any

import bpy

from ._shared import (
    BUILDER_KEY,
    ROLE_KEY,
    add_interface_socket,
    evaluated_summary,
    group_dependencies,
    initialize_group,
    link,
    require_object,
    set_input,
)


def _role(node, role: str):
    """Tag and label a generated node with a stable workflow role."""
    node[ROLE_KEY] = role
    node.label = role.replace("_", " ").title()
    return node


def _new_node(group, bl_idname: str, role: str, location: tuple[float, float]):
    """Create one runtime-verified builder node with stable role metadata."""
    if getattr(bpy.types, bl_idname, None) is None:
        raise ValueError(f"Required node type is unavailable in this Blender runtime: {bl_idname}")
    node = group.nodes.new(bl_idname)
    node.name = role
    node.location = location
    return _role(node, role)


def _group_io(group):
    """Resolve the builder group's input and active output nodes."""
    input_node = next(node for node in group.nodes if node.bl_idname == "NodeGroupInput")
    output_node = next(node for node in group.nodes if node.bl_idname == "NodeGroupOutput" and node.is_active_output)
    return input_node, output_node


def _add_input(
    group,
    name: str,
    socket_type: str,
    default: Any,
    *,
    description: str,
    min_value: float | int | None = None,
    max_value: float | int | None = None,
):
    """Add one documented builder input and return the concrete Group Input output."""
    spec = {
        "name": name,
        "direction": "INPUT",
        "socket_type": socket_type,
        "default_value": default,
        "description": description,
        "min_value": min_value,
        "max_value": max_value,
    }
    item = add_interface_socket(group, spec)
    input_node, _output_node = _group_io(group)
    socket = next(socket for socket in input_node.outputs if socket.identifier == item.identifier)
    return socket, item


def _expose(
    group,
    node,
    input_name: str,
    interface_name: str,
    socket_type: str,
    default: Any,
    description: str,
    *,
    min_value: float | int | None = None,
    max_value: float | int | None = None,
    occurrence: int = 0,
):
    """Expose one generated node input as a documented group control."""
    output, item = _add_input(
        group,
        interface_name,
        socket_type,
        default,
        description=description,
        min_value=min_value,
        max_value=max_value,
    )
    target = [socket for socket in node.inputs if socket.name == input_name][occurrence]
    group.links.new(output, target)
    return item


def _set_source(node, source_type: str, source_name: str) -> None:
    """Assign one explicit object or collection source to its info node."""
    if source_type == "OBJECT":
        source = bpy.data.objects.get(source_name)
        if source is None:
            raise ValueError(f"Source object not found: {source_name}")
        set_input(node, "Object", source)
    else:
        source = bpy.data.collections.get(source_name)
        if source is None:
            raise ValueError(f"Source collection not found: {source_name}")
        set_input(node, "Collection", source)


def _source_node(
    group,
    source_type: str,
    source_name: str,
    *,
    transform_space: str,
    role: str = "instance_source",
    location=(-350, -250),
):
    """
    Create an Object Info or Collection Info node with an explicit dependency.

    `transform_space` has no default because the two readings answer different questions.
    RELATIVE reads the source where it sits in the world, expressed in the modifier object's
    local space: a cutter, a path, a pivot or a proximity target the builder promises to follow.
    ORIGINAL reads the source's own local geometry and ignores where it sits: a shape the graph
    places itself, such as an instance or a profile cross-section.
    """
    bl_idname = "GeometryNodeObjectInfo" if source_type == "OBJECT" else "GeometryNodeCollectionInfo"
    node = _new_node(group, bl_idname, role, location)
    node.transform_space = transform_space
    _set_source(node, source_type, source_name)
    return node


def _prepare_builder(object_name: str, group_name: str, builder: str, purpose: str):
    """Create a new tagged modifier group and validate the target before graph mutation."""
    obj = require_object(object_name)
    group, created = initialize_group(
        group_name,
        purpose=purpose,
        execution_role="MODIFIER",
        geometry_types=[obj.type if obj.type != "GREASEPENCIL" else "GREASE_PENCIL"],
        collision_policy="ERROR",
    )
    if not created:
        raise ValueError(f"Builder group already exists: {group_name}")
    group[BUILDER_KEY] = builder
    for link_item in list(group.links):
        group.links.remove(link_item)
    return obj, group


def _attach_builder(obj, group, modifier_name: str | None = None):
    """Attach a completed builder group only after graph construction succeeds."""
    requested = modifier_name or group.name
    if obj.modifiers.get(requested) is not None:
        raise ValueError(f"Modifier already exists on '{obj.name}': {requested}")
    modifier = obj.modifiers.new(name=requested, type="NODES")
    modifier.node_group = group
    bpy.context.view_layer.update()
    return modifier


def _instance_transform_chain(group, instance, location=(180, 100), rotation=(0.0, 0.0, 0.0)):
    """Add reusable post-instance rotation, scale, and translation controls."""
    rotate = _new_node(group, "GeometryNodeRotateInstances", "rotate_instances", location)
    scale = _new_node(group, "GeometryNodeScaleInstances", "scale_instances", (location[0] + 200, location[1]))
    translate = _new_node(
        group, "GeometryNodeTranslateInstances", "translate_instances", (location[0] + 400, location[1])
    )
    link(group, instance, "Instances", rotate, "Instances")
    link(group, rotate, "Instances", scale, "Instances")
    link(group, scale, "Instances", translate, "Instances")
    _expose(
        group,
        rotate,
        "Rotation",
        "Instance Rotation",
        "NodeSocketRotation",
        rotation,
        "Additional local rotation applied to every generated instance",
    )
    _expose(
        group,
        scale,
        "Scale",
        "Instance Scale",
        "NodeSocketVector",
        (1.0, 1.0, 1.0),
        "Additional component-wise scale applied to every generated instance",
    )
    _expose(
        group,
        translate,
        "Translation",
        "Instance Translation",
        "NodeSocketVector",
        (0.0, 0.0, 0.0),
        "Additional local translation applied to every generated instance",
    )
    return translate, {
        "rotate_instances": rotate,
        "scale_instances": scale,
        "translate_instances": translate,
    }


def _curve_to_mesh(group, curve_node, profile: tuple[Any, str], fill_caps: bool, nodes: dict[str, Any]):
    """
    Sweep `profile` along `curve_node`'s curve, scaled by that curve's radius, and register both nodes.

    Curve to Mesh no longer scales its profile by the radius attribute on its own; its Scale
    field does, so a radius set upstream reaches the mesh only through the Radius field wired here.
    """
    profile_node, profile_output = profile
    curve_to_mesh = _new_node(group, "GeometryNodeCurveToMesh", "curve_to_mesh", (260, 100))
    _expose(
        group,
        curve_to_mesh,
        "Fill Caps",
        "Fill Caps",
        "NodeSocketBool",
        fill_caps,
        "Create end faces on open curve profiles",
    )
    link(group, curve_node, "Curve", curve_to_mesh, "Curve")
    link(group, profile_node, profile_output, curve_to_mesh, "Profile Curve")
    radius_field = _new_node(group, "GeometryNodeInputRadius", "radius_field", (60, -60))
    link(group, radius_field, "Radius", curve_to_mesh, "Scale")
    nodes.update({"curve_to_mesh": curve_to_mesh, "radius_field": radius_field})
    return curve_to_mesh


def _finish_builder(obj, group, modifier, node_map: dict[str, Any], *, warnings=None, estimated_instances=None):
    """Return the common inspectable builder result."""
    return {
        "object": obj.name,
        "node_group": group.name,
        "modifier": modifier.name,
        "builder": group.get(BUILDER_KEY),
        "node_map": {role: node.name for role, node in node_map.items()},
        "dependencies": group_dependencies(group),
        "estimated_instance_count": estimated_instances,
        "evaluated": evaluated_summary(obj),
        "warnings": warnings or [],
        "changed_objects": [obj.name],
        "changed_resources": [group.name],
    }


def _remove_builder_on_error(group) -> None:
    """Remove a partially built group; its modifier is never attached before completion."""
    if group is not None and group.name in bpy.data.node_groups:
        bpy.data.node_groups.remove(group, do_unlink=True)


def _mesh_level_set_grid(obj, voxel_size):
    """Create an OpenVDB level set from the object's evaluated local-space mesh."""
    try:
        import numpy
        import openvdb
    except ImportError as exc:
        raise ValueError("OPENVDB delivery requires Blender's bundled openvdb and numpy modules") from exc
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = obj.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh()
    try:
        if not mesh.vertices or not mesh.polygons:
            raise ValueError("MESH OpenVDB delivery requires evaluated vertices and faces")
        mesh.calc_loop_triangles()
        points = numpy.asarray([tuple(vertex.co) for vertex in mesh.vertices], dtype=numpy.float32)
        triangles = numpy.asarray([tuple(triangle.vertices) for triangle in mesh.loop_triangles], dtype=numpy.uint32)
        return openvdb.FloatGrid.createLevelSetFromPolygons(
            points,
            triangles=triangles,
            transform=openvdb.createLinearTransform(voxel_size),
        )
    finally:
        evaluated.to_mesh_clear()


def _points_level_set_grid(obj, voxel_size, radius):
    """Create a union of OpenVDB level-set spheres from object-local points."""
    try:
        import openvdb
    except ImportError as exc:
        raise ValueError("OPENVDB delivery requires Blender's bundled openvdb module") from exc
    if obj.type == "POINTCLOUD":
        positions = [tuple(point.co) for point in obj.data.points]
    elif obj.type == "MESH":
        positions = [tuple(vertex.co) for vertex in obj.data.vertices]
    else:
        raise ValueError("POINTS OpenVDB delivery requires a MESH or POINTCLOUD object")
    if not positions:
        raise ValueError("POINTS OpenVDB delivery requires at least one point")
    if len(positions) > 100_000:
        raise ValueError("POINTS OpenVDB delivery is limited to 100000 source points")
    grid = openvdb.createLevelSetSphere(radius, positions[0], voxel_size)
    for position in positions[1:]:
        sphere = openvdb.createLevelSetSphere(radius, position, voxel_size)
        grid.combine(sphere, min)
    return grid


def _cube_fog_grid(voxel_size, radius, density):
    """Create a constant-density OpenVDB cube matching the live Volume Cube graph."""
    try:
        import openvdb
    except ImportError as exc:
        raise ValueError("OPENVDB delivery requires Blender's bundled openvdb module") from exc
    extent = max(1, math.ceil(radius / voxel_size))
    grid = openvdb.FloatGrid(0.0)
    grid.transform = openvdb.createLinearTransform(voxel_size)
    grid.fill((-extent, -extent, -extent), (extent, extent, extent), density, active=True)
    grid.gridClass = openvdb.GridClass.FOG_VOLUME
    return grid


def _level_set_to_fog(grid, density):
    """Convert a signed-distance grid to constant density inside its surface."""
    import openvdb

    grid.signedFloodFill()
    for value in grid.iterAllValues():
        inside = value.value < 0.0
        value.value = density if inside else 0.0
        value.active = inside
    grid.gridClass = openvdb.GridClass.FOG_VOLUME
    return grid


def _write_openvdb_delivery(obj, source, path, grid_name, density, voxel_size, radius):
    """Write one named grid atomically through OpenVDB, never Blender's Volume RNA."""
    try:
        import openvdb
    except ImportError as exc:
        raise ValueError("OPENVDB delivery requires Blender's bundled openvdb module") from exc
    if source == "MESH":
        grid = _level_set_to_fog(_mesh_level_set_grid(obj, voxel_size), density)
    elif source == "POINTS":
        grid = _level_set_to_fog(_points_level_set_grid(obj, voxel_size, radius), density)
    else:
        grid = _cube_fog_grid(voxel_size, radius, density)
    grid.name = grid_name
    grid_evidence = {
        "grid": grid.name,
        "grid_class": "FOG_VOLUME",
        "active_voxels": grid.activeVoxelCount(),
    }
    handle = tempfile.NamedTemporaryFile(dir=os.path.dirname(path), suffix=".vdb", delete=False)
    temporary_path = handle.name
    handle.close()
    try:
        openvdb.write(temporary_path, grid)
        os.replace(temporary_path, path)
    finally:
        if os.path.exists(temporary_path):
            os.unlink(temporary_path)
    return grid_evidence


def _create_vdb_backed_object(source_obj, group_name, path, material):
    """Create a native Volume object backed by an already-written VDB file."""
    name = f"{group_name} Volume"
    if bpy.data.objects.get(name) is not None:
        raise ValueError(f"OpenVDB delivery object already exists: {name}")
    data = bpy.data.volumes.new(name)
    delivery_obj = None
    try:
        data.filepath = path
        if material is not None:
            data.materials.append(material)
        delivery_obj = bpy.data.objects.new(name, data)
        collection = source_obj.users_collection[0] if source_obj.users_collection else bpy.context.collection
        collection.objects.link(delivery_obj)
        delivery_obj.matrix_world = source_obj.matrix_world.copy()
        return delivery_obj, data
    except Exception:
        if delivery_obj is not None:
            bpy.data.objects.remove(delivery_obj, do_unlink=True)
        if bpy.data.volumes.get(data.name) is not None:
            bpy.data.volumes.remove(data)
        raise


class GeometryNodesWorkflowHandlersMixin:
    """Build inspectable Geometry Nodes systems for common production workflows."""

    def create_procedural_scatter(
        self,
        object_name,
        group_name,
        source_type,
        source_name,
        distribution="SURFACE_RANDOM",
        density=10.0,
        distance_min=0.1,
        seed=0,
        scale_min=1.0,
        scale_max=1.0,
        mask_attribute=None,
        include_original=True,
        realize_instances=False,
        output_type="INSTANCES",
        density_attribute="mcp_scatter_density",
        selection_attribute="mcp_scatter_selection",
        orientation="NORMAL",
        orientation_offset=(0.0, 0.0, 0.0),
        guide_length=1.0,
        source_collection_policy="PICK_INSTANCE",
        **_unused,
    ):
        obj = require_object(object_name)
        source_object = bpy.data.objects.get(source_name) if source_type == "OBJECT" and source_name else None
        source_collection = (
            bpy.data.collections.get(source_name) if source_type == "COLLECTION" and source_name else None
        )
        if output_type == "INSTANCES" and source_object is None and source_collection is None:
            raise ValueError(f"Scatter source not found: {source_name}")
        group = None
        try:
            obj, group = _prepare_builder(object_name, group_name, "SCATTER", "procedural scatter")
            group_input, group_output = _group_io(group)
            nodes = {}
            if distribution == "VOLUME":
                mesh_to_volume = _new_node(group, "GeometryNodeMeshToVolume", "mesh_to_volume", (-450, 100))
                distribute = _new_node(group, "GeometryNodeDistributePointsInVolume", "point_distribution", (-200, 100))
                link(group, group_input, "Geometry", mesh_to_volume, "Mesh")
                link(group, mesh_to_volume, "Volume", distribute, "Volume")
                _expose(
                    group,
                    distribute,
                    "Density",
                    "Density",
                    "NodeSocketFloat",
                    density,
                    "Points per unit volume",
                    min_value=0.0,
                )
                _expose(group, distribute, "Seed", "Seed", "NodeSocketInt", seed, "Deterministic random seed")
                nodes["mesh_to_volume"] = mesh_to_volume
            else:
                distribute = _new_node(group, "GeometryNodeDistributePointsOnFaces", "point_distribution", (-200, 100))
                distribute.distribute_method = "POISSON" if distribution == "SURFACE_POISSON" else "RANDOM"
                link(group, group_input, "Geometry", distribute, "Mesh")
                density_input = "Density" if distribution == "SURFACE_RANDOM" else "Density Max"
                _expose(
                    group,
                    distribute,
                    density_input,
                    "Density",
                    "NodeSocketFloat",
                    density,
                    "Points per unit surface area",
                    min_value=0.0,
                )
                _expose(
                    group,
                    distribute,
                    "Distance Min",
                    "Distance Min",
                    "NodeSocketFloat",
                    distance_min,
                    "Minimum Poisson point separation",
                    min_value=0.000001,
                )
                _expose(group, distribute, "Seed", "Seed", "NodeSocketInt", seed, "Deterministic random seed")
                if mask_attribute:
                    mask = _new_node(group, "GeometryNodeInputNamedAttribute", "density_mask", (-450, -50))
                    mask.data_type = "FLOAT"
                    set_input(mask, "Name", mask_attribute)
                    target_name = "Density" if distribution == "SURFACE_RANDOM" else "Density Factor"
                    link(group, mask, "Attribute", distribute, target_name)
                    nodes["density_mask"] = mask
            point_geometry = distribute
            point_output = "Points"
            if density_attribute:
                store_density = _new_node(group, "GeometryNodeStoreNamedAttribute", "store_density", (-20, 40))
                store_density.data_type = "FLOAT"
                store_density.domain = "POINT"
                set_input(store_density, "Name", density_attribute)
                set_input(store_density, "Value", density)
                link(group, point_geometry, point_output, store_density, "Geometry")
                point_geometry, point_output = store_density, "Geometry"
                nodes["store_density"] = store_density
            if selection_attribute:
                store_selection = _new_node(group, "GeometryNodeStoreNamedAttribute", "store_selection", (20, 0))
                store_selection.data_type = "BOOLEAN"
                store_selection.domain = "POINT"
                set_input(store_selection, "Name", selection_attribute)
                set_input(store_selection, "Value", True)
                link(group, point_geometry, point_output, store_selection, "Geometry")
                point_geometry, point_output = store_selection, "Geometry"
                nodes["store_selection"] = store_selection

            if output_type == "POINTS":
                terminal, terminal_output = point_geometry, point_output
                source = instance = random_scale = None
            else:
                instance = _new_node(group, "GeometryNodeInstanceOnPoints", "instance_on_points", (180, 100))
                link(group, point_geometry, point_output, instance, "Points")
                if output_type == "HAIR_CURVES":
                    source = _new_node(group, "GeometryNodeCurvePrimitiveLine", "guide_curve", (-40, -220))
                    set_input(source, "Start", (0.0, 0.0, 0.0))
                    set_input(source, "End", (0.0, 0.0, guide_length))
                    source_value = None
                    link(group, source, "Curve", instance, "Instance")
                else:
                    # Each instance is the source's own shape; where the source sits is not the scatter's.
                    source = _source_node(group, source_type, source_name, transform_space="ORIGINAL")
                    source_value = source_object if source_type == "OBJECT" else source_collection
                    _expose(
                        group,
                        source,
                        "Object" if source_type == "OBJECT" else "Collection",
                        "Source",
                        "NodeSocketObject" if source_type == "OBJECT" else "NodeSocketCollection",
                        source_value,
                        "Object or collection instanced at each generated point",
                    )
                    if source_type == "COLLECTION":
                        separate = source_collection_policy in {"PICK_INSTANCE", "SEPARATE_CHILDREN"}
                        set_input(source, "Separate Children", separate)
                        set_input(source, "Reset Children", source_collection_policy == "SEPARATE_CHILDREN")
                        set_input(instance, "Pick Instance", separate)
                    link(group, source, "Geometry" if source_type == "OBJECT" else "Instances", instance, "Instance")
            random_scale = _new_node(group, "FunctionNodeRandomValue", "random_scale", (-180, -180))
            random_scale.data_type = "FLOAT"
            _expose(
                group,
                random_scale,
                "Min",
                "Scale Min",
                "NodeSocketFloat",
                scale_min,
                "Minimum uniform instance scale",
                min_value=0.0,
            )
            _expose(
                group,
                random_scale,
                "Max",
                "Scale Max",
                "NodeSocketFloat",
                scale_max,
                "Maximum uniform instance scale",
                min_value=0.0,
            )
            _expose(
                group,
                random_scale,
                "Seed",
                "Scale Seed",
                "NodeSocketInt",
                seed,
                "Deterministic scale-randomization seed",
            )
            if output_type != "POINTS":
                link(group, random_scale, "Value", instance, "Scale")
                if orientation == "NORMAL":
                    link(group, distribute, "Rotation", instance, "Rotation")
                elif orientation == "RANDOM":
                    random_rotation = _new_node(group, "FunctionNodeRandomValue", "random_rotation", (-20, -120))
                    random_rotation.data_type = "FLOAT_VECTOR"
                    set_input(random_rotation, "Min", (-math.pi, -math.pi, -math.pi))
                    set_input(random_rotation, "Max", (math.pi, math.pi, math.pi))
                    link(group, random_rotation, "Value", instance, "Rotation")
                    nodes["random_rotation"] = random_rotation
                transformed, transform_nodes = _instance_transform_chain(
                    group, instance, (300, 100), rotation=orientation_offset
                )
                realize = _new_node(group, "GeometryNodeRealizeInstances", "realize_instances", (500, 30))
                link(group, transformed, "Instances", realize, "Geometry")
                if output_type == "HAIR_CURVES":
                    realization_switch = realize
                    terminal, terminal_output = realize, "Geometry"
                else:
                    realization_switch = _new_node(group, "GeometryNodeSwitch", "realization_policy", (680, 30))
                    realization_switch.input_type = "GEOMETRY"
                    link(group, transformed, "Instances", realization_switch, "False")
                    link(group, realize, "Geometry", realization_switch, "True")
                    _expose(
                        group,
                        realization_switch,
                        "Switch",
                        "Realize Instances",
                        "NodeSocketBool",
                        realize_instances,
                        "Convert instances to unique geometry before graph output",
                    )
                    terminal, terminal_output = realization_switch, "Output"
                nodes.update({**transform_nodes, "realize_instances": realize})
                if output_type == "INSTANCES":
                    nodes["realization_policy"] = realization_switch
            join = _new_node(group, "GeometryNodeJoinGeometry", "join_original", (620, 30))
            link(group, group_input, "Geometry", join, "Geometry")
            link(group, terminal, terminal_output, join, "Geometry")
            passthrough_switch = _new_node(group, "GeometryNodeSwitch", "original_geometry_policy", (820, 30))
            passthrough_switch.input_type = "GEOMETRY"
            link(group, terminal, terminal_output, passthrough_switch, "False")
            link(group, join, "Geometry", passthrough_switch, "True")
            _expose(
                group,
                passthrough_switch,
                "Switch",
                "Include Original",
                "NodeSocketBool",
                include_original,
                "Join the source surface with scattered instances",
            )
            link(group, passthrough_switch, "Output", group_output, "Geometry")
            nodes.update(
                {
                    "join_original": join,
                    "original_geometry_policy": passthrough_switch,
                }
            )
            nodes["point_distribution"] = distribute
            if source is not None:
                nodes["instance_source"] = source
            if instance is not None:
                nodes["instance_on_points"] = instance
                nodes["random_scale"] = random_scale
            group["blender_mcp_scatter_output"] = output_type
            group["blender_mcp_density_attribute"] = density_attribute or ""
            group["blender_mcp_selection_attribute"] = selection_attribute or ""
            group["blender_mcp_orientation"] = orientation
            group["blender_mcp_source_collection_policy"] = source_collection_policy
            modifier = _attach_builder(obj, group)
            estimate = None
            if obj.type == "MESH" and distribution != "VOLUME":
                area = sum(polygon.area for polygon in obj.data.polygons)
                estimate = int(area * density) if distribution == "SURFACE_RANDOM" else None
            result = _finish_builder(obj, group, modifier, nodes, estimated_instances=estimate)
            result["output_type"] = output_type
            result["attributes"] = [name for name in (density_attribute, selection_attribute) if name]
            result["estimated_curve_count"] = estimate if output_type == "HAIR_CURVES" else None
            return result
        except Exception:
            _remove_builder_on_error(group)
            raise

    def create_curve_generator(
        self,
        object_name,
        group_name,
        curve_object_name=None,
        profile_object_name=None,
        radius=0.05,
        resolution=32,
        trim_start=0.0,
        trim_end=1.0,
        fill_caps=True,
        material_name=None,
        **_unused,
    ):
        group = None
        try:
            obj, group = _prepare_builder(object_name, group_name, "CURVE_GENERATOR", "curve-driven mesh generator")
            group_input, group_output = _group_io(group)
            nodes = {}
            source = group_input
            source_output = "Geometry"
            if curve_object_name:
                curve_obj = bpy.data.objects.get(curve_object_name)
                if curve_obj is None or curve_obj.type != "CURVE":
                    raise ValueError(f"Curve object not found or not CURVE: {curve_object_name}")
                # The path is followed where it sits in the world, not where its local data says.
                source = _source_node(
                    group,
                    "OBJECT",
                    curve_obj.name,
                    transform_space="RELATIVE",
                    role="curve_source",
                    location=(-650, 100),
                )
                _expose(
                    group,
                    source,
                    "Object",
                    "Curve Source",
                    "NodeSocketObject",
                    curve_obj,
                    "Editable curve object used as the generator path",
                )
                source_output = "Geometry"
                nodes["curve_source"] = source
            resample = _new_node(group, "GeometryNodeResampleCurve", "resample_curve", (-400, 100))
            set_input(resample, "Count", resolution)
            _expose(
                group, resample, "Count", "Resolution", "NodeSocketInt", resolution, "Curve sample count", min_value=2
            )
            trim = _new_node(group, "GeometryNodeTrimCurve", "trim_curve", (-180, 100))
            trim.mode = "FACTOR"
            _expose(
                group,
                trim,
                "Start",
                "Trim Start",
                "NodeSocketFloat",
                trim_start,
                "Normalized trim start",
                min_value=0.0,
                max_value=1.0,
            )
            _expose(
                group,
                trim,
                "End",
                "Trim End",
                "NodeSocketFloat",
                trim_end,
                "Normalized trim end",
                min_value=0.0,
                max_value=1.0,
            )
            cyclic = _new_node(group, "GeometryNodeSetSplineCyclic", "set_cyclic", (-20, 100))
            _expose(
                group,
                cyclic,
                "Cyclic",
                "Cyclic",
                "NodeSocketBool",
                False,
                "Close every selected spline before meshing",
            )
            tilt = _new_node(group, "GeometryNodeSetCurveTilt", "set_tilt", (170, 100))
            _expose(
                group,
                tilt,
                "Tilt",
                "Tilt",
                "NodeSocketFloat",
                0.0,
                "Profile tilt in radians along the curve",
            )
            radius_node = _new_node(group, "GeometryNodeSetCurveRadius", "set_radius", (360, 100))
            _expose(
                group,
                radius_node,
                "Radius",
                "Radius",
                "NodeSocketFloat",
                radius,
                "Generated curve radius",
                min_value=0.000001,
            )
            link(group, source, source_output, resample, "Curve")
            link(group, resample, "Curve", trim, "Curve")
            link(group, trim, "Curve", cyclic, "Curve")
            link(group, cyclic, "Curve", tilt, "Curve")
            link(group, tilt, "Curve", radius_node, "Curve")
            if profile_object_name:
                profile_obj = bpy.data.objects.get(profile_object_name)
                if profile_obj is None or profile_obj.type != "CURVE":
                    raise ValueError(f"Profile object not found or not CURVE: {profile_object_name}")
                # A cross-section in its own space: its world placement would offset the tube off the path.
                profile = _source_node(
                    group,
                    "OBJECT",
                    profile_obj.name,
                    transform_space="ORIGINAL",
                    role="profile_source",
                    location=(-50, -160),
                )
                _expose(
                    group,
                    profile,
                    "Object",
                    "Profile",
                    "NodeSocketObject",
                    profile_obj,
                    "Curve object used as the bevel profile",
                )
                profile_output = "Geometry"
            else:
                profile = _new_node(group, "GeometryNodeCurvePrimitiveCircle", "profile_curve", (-50, -160))
                set_input(profile, "Resolution", max(3, min(resolution, 512)))
                set_input(profile, "Radius", 1.0)
                profile_output = "Curve"
            curve_to_mesh = _curve_to_mesh(group, radius_node, (profile, profile_output), fill_caps, nodes)
            terminal = curve_to_mesh
            if material_name:
                material = bpy.data.materials.get(material_name)
                if material is None:
                    raise ValueError(f"Material not found: {material_name}")
                set_material = _new_node(group, "GeometryNodeSetMaterial", "set_material", (470, 100))
                _expose(
                    group,
                    set_material,
                    "Material",
                    "Material",
                    "NodeSocketMaterial",
                    material,
                    "Material assigned to generated faces",
                )
                link(group, curve_to_mesh, "Mesh", set_material, "Geometry")
                terminal = set_material
                nodes["set_material"] = set_material
            link(
                group,
                terminal,
                "Geometry" if terminal.bl_idname == "GeometryNodeSetMaterial" else "Mesh",
                group_output,
                "Geometry",
            )
            nodes.update(
                {
                    "resample_curve": resample,
                    "trim_curve": trim,
                    "set_cyclic": cyclic,
                    "set_tilt": tilt,
                    "set_radius": radius_node,
                    "profile": profile,
                }
            )
            modifier = _attach_builder(obj, group)
            result = _finish_builder(obj, group, modifier, nodes)
            result["coordinate_space"] = (
                "The path curve is read where it sits in the world, expressed in the modifier object's local "
                "space; a profile curve is read in its own local space."
            )
            return result
        except Exception:
            _remove_builder_on_error(group)
            raise

    def create_procedural_array(
        self,
        object_name,
        group_name,
        source_name,
        layout,
        count=5,
        count_y=1,
        spacing=(1.0, 0.0, 0.0),
        angular_span=math.tau,
        endpoint_policy="EXCLUDE_END",
        pivot_object_name=None,
        curve_object_name=None,
        realize_instances=False,
        **_unused,
    ):
        group = None
        try:
            source_obj = bpy.data.objects.get(source_name)
            if source_obj is None:
                raise ValueError(f"Array source object not found: {source_name}")
            obj, group = _prepare_builder(object_name, group_name, "ARRAY", f"{layout.lower()} procedural array")
            _group_input, group_output = _group_io(group)
            nodes = {}
            if layout == "LINEAR":
                points = _new_node(group, "GeometryNodeMeshLine", "layout_points", (-350, 100))
                points.mode = "OFFSET"
                _expose(
                    group, points, "Count", "Count", "NodeSocketInt", count, "Number of array instances", min_value=1
                )
                _expose(
                    group,
                    points,
                    "Offset",
                    "Spacing",
                    "NodeSocketVector",
                    spacing,
                    "Object-space offset between instances",
                )
                points_output = "Mesh"
            elif layout == "GRID":
                points = _new_node(group, "GeometryNodeMeshGrid", "layout_points", (-350, 100))
                _expose(
                    group,
                    points,
                    "Size X",
                    "Extent X",
                    "NodeSocketFloat",
                    abs(spacing[0]) * max(0, count - 1),
                    "Total object-space X extent",
                    min_value=0.0,
                )
                _expose(
                    group,
                    points,
                    "Size Y",
                    "Extent Y",
                    "NodeSocketFloat",
                    abs(spacing[1] or spacing[0]) * max(0, count_y - 1),
                    "Total object-space Y extent",
                    min_value=0.0,
                )
                _expose(
                    group,
                    points,
                    "Vertices X",
                    "Count X",
                    "NodeSocketInt",
                    count,
                    "Number of X-axis instances",
                    min_value=1,
                )
                _expose(
                    group,
                    points,
                    "Vertices Y",
                    "Count Y",
                    "NodeSocketInt",
                    count_y,
                    "Number of Y-axis instances",
                    min_value=1,
                )
                points_output = "Mesh"
            elif layout == "RADIAL":
                base_points = _new_node(group, "GeometryNodeMeshLine", "index_points", (-800, 100))
                base_points.mode = "OFFSET"
                count_item = _expose(
                    group,
                    base_points,
                    "Count",
                    "Count",
                    "NodeSocketInt",
                    count,
                    "Number of radial instances",
                    min_value=1,
                )
                set_input(base_points, "Offset", (0.0, 0.0, 0.0))
                index = _new_node(group, "GeometryNodeInputIndex", "instance_index", (-800, -120))
                divide = _new_node(group, "ShaderNodeMath", "normalized_index", (-580, -120))
                divide.operation = "DIVIDE"
                link(group, index, "Index", divide, "Value", to_occurrence=0)
                count_socket = next(
                    socket for socket in _group_io(group)[0].outputs if socket.identifier == count_item.identifier
                )
                if endpoint_policy == "INCLUDE_BOTH":
                    subtract = _new_node(group, "ShaderNodeMath", "endpoint_denominator", (-790, -250))
                    subtract.operation = "SUBTRACT"
                    set_input(subtract, "Value", 1.0, 1)
                    maximum = _new_node(group, "ShaderNodeMath", "safe_denominator", (-610, -250))
                    maximum.operation = "MAXIMUM"
                    set_input(maximum, "Value", 1.0, 1)
                    group.links.new(count_socket, next(socket for socket in subtract.inputs if socket.name == "Value"))
                    link(group, subtract, "Value", maximum, "Value", to_occurrence=0)
                    link(group, maximum, "Value", divide, "Value", to_occurrence=1)
                    nodes.update({"endpoint_denominator": subtract, "safe_denominator": maximum})
                else:
                    group.links.new(count_socket, [socket for socket in divide.inputs if socket.name == "Value"][1])
                multiply = _new_node(group, "ShaderNodeMath", "angle", (-380, -120))
                multiply.operation = "MULTIPLY"
                _expose(
                    group,
                    multiply,
                    "Value",
                    "Angular Span",
                    "NodeSocketFloat",
                    angular_span,
                    "Total radial angle in radians",
                    occurrence=1,
                )
                link(group, divide, "Value", multiply, "Value", to_occurrence=0)
                cosine = _new_node(group, "ShaderNodeMath", "cosine", (-180, -40))
                cosine.operation = "COSINE"
                sine = _new_node(group, "ShaderNodeMath", "sine", (-180, -180))
                sine.operation = "SINE"
                link(group, multiply, "Value", cosine, "Value")
                link(group, multiply, "Value", sine, "Value")
                radius = max(abs(spacing[0]), abs(spacing[1]), abs(spacing[2]), 1.0)
                scale_x = _new_node(group, "ShaderNodeMath", "radius_x", (20, -40))
                scale_y = _new_node(group, "ShaderNodeMath", "radius_y", (20, -180))
                scale_x.operation = "MULTIPLY"
                scale_y.operation = "MULTIPLY"
                _expose(
                    group,
                    scale_x,
                    "Value",
                    "Radius",
                    "NodeSocketFloat",
                    radius,
                    "Object-space radial distance",
                    min_value=0.0,
                    occurrence=1,
                )
                link(group, cosine, "Value", scale_x, "Value", to_occurrence=0)
                link(group, sine, "Value", scale_y, "Value", to_occurrence=0)
                combine_position = _new_node(group, "ShaderNodeCombineXYZ", "radial_position", (220, -100))
                link(group, scale_x, "Value", combine_position, "X")
                link(group, scale_y, "Value", combine_position, "Y")
                final_position = combine_position
                final_position_output = "Vector"
                if pivot_object_name:
                    pivot = bpy.data.objects.get(pivot_object_name)
                    if pivot is None:
                        raise ValueError(f"Pivot object not found: {pivot_object_name}")
                    # Its Location output then names the pivot's world position in this object's space.
                    pivot_info = _source_node(
                        group, "OBJECT", pivot.name, transform_space="RELATIVE", role="pivot_source", location=(0, -350)
                    )
                    _expose(
                        group,
                        pivot_info,
                        "Object",
                        "Pivot",
                        "NodeSocketObject",
                        pivot,
                        "Object whose world location is the radial pivot",
                    )
                    add_pivot = _new_node(group, "ShaderNodeVectorMath", "add_pivot", (430, -100))
                    add_pivot.operation = "ADD"
                    link(group, combine_position, "Vector", add_pivot, "Vector", to_occurrence=0)
                    link(group, pivot_info, "Location", add_pivot, "Vector", to_occurrence=1)
                    final_position = add_pivot
                    final_position_output = "Vector"
                    nodes.update({"pivot_source": pivot_info, "add_pivot": add_pivot})
                set_position = _new_node(group, "GeometryNodeSetPosition", "layout_points", (650, 100))
                link(group, base_points, "Mesh", set_position, "Geometry")
                link(group, final_position, final_position_output, set_position, "Position")
                rotation = _new_node(group, "ShaderNodeCombineXYZ", "radial_rotation", (630, -160))
                link(group, multiply, "Value", rotation, "Z")
                points = set_position
                points_output = "Geometry"
                nodes.update(
                    {
                        "index_points": base_points,
                        "instance_index": index,
                        "normalized_index": divide,
                        "angle": multiply,
                        "radial_position": combine_position,
                        "radial_rotation": rotation,
                    }
                )
            else:
                curve = bpy.data.objects.get(curve_object_name) if curve_object_name else None
                if curve is None or curve.type != "CURVE":
                    raise ValueError("CURVE layout requires curve_object_name naming a curve object")
                # The array follows the curve where it sits in the world, not where its local data says.
                curve_info = _source_node(
                    group, "OBJECT", curve.name, transform_space="RELATIVE", role="curve_source", location=(-550, 100)
                )
                _expose(
                    group,
                    curve_info,
                    "Object",
                    "Curve",
                    "NodeSocketObject",
                    curve,
                    "Curve object followed by the array",
                )
                points = _new_node(group, "GeometryNodeCurveToPoints", "layout_points", (-300, 100))
                points.mode = "COUNT"
                _expose(
                    group, points, "Count", "Count", "NodeSocketInt", count, "Number of curve instances", min_value=1
                )
                link(group, curve_info, "Geometry", points, "Curve")
                points_output = "Points"
                nodes["curve_source"] = curve_info
            # Each copy is the source's own shape; the layout above decides where it goes.
            source = _source_node(group, "OBJECT", source_name, transform_space="ORIGINAL", location=(-300, -180))
            _expose(group, source, "Object", "Source", "NodeSocketObject", source_obj, "Object instanced by the array")
            instance = _new_node(group, "GeometryNodeInstanceOnPoints", "instance_on_points", (0, 100))
            link(group, points, points_output, instance, "Points")
            link(group, source, "Geometry", instance, "Instance")
            if layout == "RADIAL":
                link(group, nodes["radial_rotation"], "Vector", instance, "Rotation")
            elif layout == "CURVE" and "Rotation" in [socket.name for socket in points.outputs]:
                link(group, points, "Rotation", instance, "Rotation")
            terminal, transform_nodes = _instance_transform_chain(group, instance, (200, 100))
            nodes.update(transform_nodes)
            if realize_instances:
                realize = _new_node(group, "GeometryNodeRealizeInstances", "realize_instances", (240, 100))
                link(group, terminal, "Instances", realize, "Geometry")
                terminal = realize
                nodes["realize_instances"] = realize
            link(
                group,
                terminal,
                "Geometry" if terminal.bl_idname == "GeometryNodeRealizeInstances" else "Instances",
                group_output,
                "Geometry",
            )
            nodes.update({"layout_points": points, "instance_source": source, "instance_on_points": instance})
            modifier = _attach_builder(obj, group)
            estimate = count * count_y if layout == "GRID" else count
            warnings = []
            if layout == "RADIAL":
                group["blender_mcp_endpoint_policy"] = endpoint_policy
            return _finish_builder(obj, group, modifier, nodes, warnings=warnings, estimated_instances=estimate)
        except Exception:
            _remove_builder_on_error(group)
            raise

    def create_surface_paneling(
        self,
        object_name,
        group_name,
        source_collection_name=None,
        panel_scale=0.9,
        depth=0.05,
        normal_offset=0.0,
        seed=0,
        mask_attribute=None,
        realize_instances=False,
        **_unused,
    ):
        group = None
        try:
            obj, group = _prepare_builder(object_name, group_name, "SURFACE_PANELING", "surface paneling")
            group_input, group_output = _group_io(group)
            offset_geometry = _new_node(group, "GeometryNodeSetPosition", "normal_offset", (-650, 100))
            normal = _new_node(group, "GeometryNodeInputNormal", "surface_normal", (-650, -100))
            offset_vector = _new_node(group, "ShaderNodeVectorMath", "normal_offset_vector", (-430, -100))
            offset_vector.operation = "SCALE"
            _expose(
                group,
                offset_vector,
                "Scale",
                "Normal Offset",
                "NodeSocketFloat",
                normal_offset,
                "Signed panel offset along each face normal",
            )
            link(group, group_input, "Geometry", offset_geometry, "Geometry")
            link(group, normal, "Normal", offset_vector, "Vector")
            link(group, offset_vector, "Vector", offset_geometry, "Offset")
            points = _new_node(group, "GeometryNodeMeshToPoints", "face_points", (-300, 100))
            points.mode = "FACES"
            link(group, offset_geometry, "Geometry", points, "Mesh")
            if mask_attribute:
                mask = _new_node(group, "GeometryNodeInputNamedAttribute", "panel_mask", (-500, 280))
                mask.data_type = "BOOLEAN"
                set_input(mask, "Name", mask_attribute)
                link(group, mask, "Attribute", points, "Selection")
            store_id = _new_node(group, "GeometryNodeStoreNamedAttribute", "store_panel_id", (-100, 100))
            store_id.data_type = "INT"
            store_id.domain = "POINT"
            set_input(store_id, "Name", "panel_id")
            index = _new_node(group, "GeometryNodeInputIndex", "panel_id", (-300, -60))
            link(group, points, "Points", store_id, "Geometry")
            link(group, index, "Index", store_id, "Value")
            nodes = {
                "normal_offset": offset_geometry,
                "surface_normal": normal,
                "normal_offset_vector": offset_vector,
                "face_points": points,
                "store_panel_id": store_id,
                "panel_id": index,
            }
            if mask_attribute:
                nodes["panel_mask"] = mask
            if source_collection_name:
                # Each panel variant is its own shape; the face points decide where it goes.
                source = _source_node(
                    group, "COLLECTION", source_collection_name, transform_space="ORIGINAL", location=(-300, -160)
                )
                source_output = "Instances"
                source_collection = bpy.data.collections[source_collection_name]
                _expose(
                    group,
                    source,
                    "Collection",
                    "Panel Collection",
                    "NodeSocketCollection",
                    source_collection,
                    "Collection containing panel variants",
                )
            else:
                source = _new_node(group, "GeometryNodeMeshCube", "default_panel", (-300, -160))
                set_input(source, "Size", (panel_scale, panel_scale, max(depth, 1e-6)))
                source_output = "Mesh"
            instance = _new_node(group, "GeometryNodeInstanceOnPoints", "instance_on_faces", (0, 100))
            panel_size = _new_node(group, "ShaderNodeCombineXYZ", "panel_scale", (-100, -180))
            _expose(
                group,
                panel_size,
                "X",
                "Panel Scale",
                "NodeSocketFloat",
                panel_scale,
                "Panel width and height scale",
                min_value=0.000001,
            )
            scale_item = next(item for item in group.interface.items_tree if item.name == "Panel Scale")
            scale_socket = next(socket for socket in group_input.outputs if socket.identifier == scale_item.identifier)
            group.links.new(scale_socket, next(socket for socket in panel_size.inputs if socket.name == "Y"))
            _expose(
                group,
                panel_size,
                "Z",
                "Depth",
                "NodeSocketFloat",
                max(depth, 1e-6),
                "Panel depth scale",
                min_value=0.000001,
            )
            link(group, store_id, "Geometry", instance, "Points")
            link(group, source, source_output, instance, "Instance")
            if source_collection_name:
                set_input(instance, "Pick Instance", True)
                random_index = _new_node(group, "FunctionNodeRandomValue", "panel_picker", (-80, -430))
                random_index.data_type = "INT"
                set_input(random_index, "Min", 0)
                set_input(random_index, "Max", max(0, len(source_collection.objects) - 1))
                _expose(
                    group,
                    random_index,
                    "Seed",
                    "Seed",
                    "NodeSocketInt",
                    seed,
                    "Deterministic panel-variant seed",
                )
                link(group, random_index, "Value", instance, "Instance Index")
                nodes["panel_picker"] = random_index
            link(group, panel_size, "Vector", instance, "Scale")
            align = _new_node(group, "FunctionNodeAlignEulerToVector", "align_to_normal", (-100, -340))
            align.axis = "Z"
            link(group, normal, "Normal", align, "Vector")
            link(group, align, "Rotation", instance, "Rotation")
            nodes.update({"panel_scale": panel_size, "align_to_normal": align})
            terminal, transform_nodes = _instance_transform_chain(group, instance, (180, 100))
            nodes.update(transform_nodes)
            if realize_instances:
                realize = _new_node(group, "GeometryNodeRealizeInstances", "realize_instances", (230, 100))
                link(group, terminal, "Instances", realize, "Geometry")
                terminal = realize
                nodes["realize_instances"] = realize
            link(
                group,
                terminal,
                "Geometry" if terminal.bl_idname == "GeometryNodeRealizeInstances" else "Instances",
                group_output,
                "Geometry",
            )
            nodes.update({"panel_source": source, "instance_on_faces": instance})
            group["blender_mcp_seed"] = seed
            modifier = _attach_builder(obj, group)
            estimate = len(obj.data.polygons) if obj.type == "MESH" else None
            return _finish_builder(obj, group, modifier, nodes, estimated_instances=estimate)
        except Exception:
            _remove_builder_on_error(group)
            raise

    def create_procedural_boolean(
        self,
        object_name,
        group_name,
        cutter_source,
        cutter_name,
        operation="DIFFERENCE",
        solver="EXACT",
        include_cutters=False,
        **_unused,
    ):
        group = None
        try:
            obj, group = _prepare_builder(object_name, group_name, "BOOLEAN", "live multi-cutter boolean")
            group_input, group_output = _group_io(group)
            # A cutter cuts where it sits in the world; its local data alone would cut at this object's origin.
            source = _source_node(group, cutter_source, cutter_name, transform_space="RELATIVE", location=(-400, -100))
            source_value = (
                bpy.data.objects.get(cutter_name)
                if cutter_source == "OBJECT"
                else bpy.data.collections.get(cutter_name)
            )
            _expose(
                group,
                source,
                "Object" if cutter_source == "OBJECT" else "Collection",
                "Cutters",
                "NodeSocketObject" if cutter_source == "OBJECT" else "NodeSocketCollection",
                source_value,
                "Object or collection providing live Boolean cutters",
            )
            cutter_terminal = source
            cutter_output = "Geometry" if cutter_source == "OBJECT" else "Instances"
            nodes = {"cutter_source": source}
            if cutter_source == "COLLECTION":
                realize = _new_node(group, "GeometryNodeRealizeInstances", "realize_cutters", (-180, -100))
                link(group, source, "Instances", realize, "Geometry")
                cutter_terminal = realize
                cutter_output = "Geometry"
                nodes["realize_cutters"] = realize
            boolean = _new_node(group, "GeometryNodeMeshBoolean", "mesh_boolean", (80, 100))
            boolean.operation = operation
            boolean.solver = solver
            link(group, group_input, "Geometry", boolean, "Mesh 1")
            link(group, cutter_terminal, cutter_output, boolean, "Mesh 2")
            join = _new_node(group, "GeometryNodeJoinGeometry", "debug_join_cutters", (320, 100))
            link(group, boolean, "Mesh", join, "Geometry")
            link(group, cutter_terminal, cutter_output, join, "Geometry")
            debug_switch = _new_node(group, "GeometryNodeSwitch", "cutter_display_policy", (530, 100))
            debug_switch.input_type = "GEOMETRY"
            link(group, boolean, "Mesh", debug_switch, "False")
            link(group, join, "Geometry", debug_switch, "True")
            _expose(
                group,
                debug_switch,
                "Switch",
                "Include Cutters",
                "NodeSocketBool",
                include_cutters,
                "Include cutter geometry in the modifier output for debugging",
            )
            link(group, debug_switch, "Output", group_output, "Geometry")
            nodes.update(
                {
                    "mesh_boolean": boolean,
                    "debug_join_cutters": join,
                    "cutter_display_policy": debug_switch,
                }
            )
            modifier = _attach_builder(obj, group)
            return _finish_builder(obj, group, modifier, nodes)
        except Exception:
            _remove_builder_on_error(group)
            raise

    def create_procedural_deformer(
        self,
        object_name,
        group_name,
        template,
        strength=0.1,
        scale=1.0,
        axis="Z",
        coordinate_space="OBJECT",
        seed=0,
        target_object_name=None,
        mask_attribute=None,
        **_unused,
    ):
        if coordinate_space != "OBJECT":
            raise ValueError(
                "WORLD-space deformation requires an explicit transform-reference contract; use OBJECT or patch the graph with Transform nodes"
            )
        group = None
        try:
            obj, group = _prepare_builder(object_name, group_name, "DEFORMER", f"{template.lower()} field deformer")
            group_input, group_output = _group_io(group)
            set_position = _new_node(group, "GeometryNodeSetPosition", "set_position", (280, 100))
            link(group, group_input, "Geometry", set_position, "Geometry")
            nodes = {"set_position": set_position}
            if template == "NOISE_DISPLACEMENT":
                position = _new_node(group, "GeometryNodeInputPosition", "position", (-500, -100))
                noise = _new_node(group, "ShaderNodeTexNoise", "noise_field", (-280, -100))
                noise.noise_dimensions = "4D"
                normal = _new_node(group, "GeometryNodeInputNormal", "normal", (-280, -280))
                field_strength = _new_node(group, "ShaderNodeMath", "field_strength", (-40, -100))
                field_strength.operation = "MULTIPLY"
                multiply = _new_node(group, "ShaderNodeVectorMath", "scale_offset", (170, -100))
                multiply.operation = "SCALE"
                _expose(
                    group, noise, "Scale", "Scale", "NodeSocketFloat", scale, "Noise feature scale", min_value=0.000001
                )
                _expose(
                    group, noise, "W", "Seed", "NodeSocketFloat", float(seed), "Deterministic fourth noise dimension"
                )
                _expose(
                    group,
                    field_strength,
                    "Value",
                    "Strength",
                    "NodeSocketFloat",
                    strength,
                    "Signed displacement strength",
                    occurrence=1,
                )
                link(group, position, "Position", noise, "Vector")
                link(group, noise, "Factor", field_strength, "Value", to_occurrence=0)
                link(group, normal, "Normal", multiply, "Vector")
                link(group, field_strength, "Value", multiply, "Scale")
                link(group, multiply, "Vector", set_position, "Offset")
                nodes.update(
                    {
                        "position": position,
                        "noise_field": noise,
                        "normal": normal,
                        "field_strength": field_strength,
                        "scale_offset": multiply,
                    }
                )
            elif template in {"MASK_OFFSET", "PROXIMITY_PUSH"}:
                normal = _new_node(group, "GeometryNodeInputNormal", "normal", (-300, -180))
                factor = _new_node(group, "ShaderNodeMath", "field_strength", (-80, -80))
                factor.operation = "MULTIPLY"
                _expose(
                    group,
                    factor,
                    "Value",
                    "Strength",
                    "NodeSocketFloat",
                    strength,
                    "Signed normal offset",
                    occurrence=1,
                )
                if template == "MASK_OFFSET":
                    if not mask_attribute:
                        raise ValueError("MASK_OFFSET requires mask_attribute")
                    field = _new_node(group, "GeometryNodeInputNamedAttribute", "mask_attribute", (-300, 0))
                    field.data_type = "FLOAT"
                    set_input(field, "Name", mask_attribute)
                    link(group, field, "Attribute", factor, "Value", to_occurrence=0)
                else:
                    target = bpy.data.objects.get(target_object_name) if target_object_name else None
                    if target is None:
                        raise ValueError("PROXIMITY_PUSH requires target_object_name")
                    # Distance is measured to the target where it sits in the world.
                    target_info = _source_node(
                        group,
                        "OBJECT",
                        target.name,
                        transform_space="RELATIVE",
                        role="target_source",
                        location=(-520, 0),
                    )
                    proximity = _new_node(group, "GeometryNodeProximity", "proximity", (-300, 0))
                    map_range = _new_node(group, "ShaderNodeMapRange", "falloff", (-100, 0))
                    set_input(map_range, "From Min", 0.0)
                    set_input(map_range, "From Max", scale)
                    set_input(map_range, "To Min", 1.0)
                    set_input(map_range, "To Max", 0.0)
                    link(group, target_info, "Geometry", proximity, "Geometry")
                    link(group, proximity, "Distance", map_range, "Value")
                    link(group, map_range, "Result", factor, "Value", to_occurrence=0)
                    nodes.update({"target_source": target_info, "proximity": proximity, "falloff": map_range})
                vector_math = _new_node(group, "ShaderNodeVectorMath", "deformation_field", (120, -80))
                vector_math.operation = "SCALE"
                link(group, normal, "Normal", vector_math, "Vector")
                link(group, factor, "Value", vector_math, "Scale")
                link(group, vector_math, "Vector", set_position, "Offset")
                nodes.update({"normal": normal, "field_strength": factor, "deformation_field": vector_math})
            elif template == "TWIST":
                position = _new_node(group, "GeometryNodeInputPosition", "position", (-550, -100))
                separate = _new_node(group, "ShaderNodeSeparateXYZ", "axis_coordinate", (-350, -100))
                angle = _new_node(group, "ShaderNodeMath", "twist_angle", (-150, -100))
                angle.operation = "MULTIPLY"
                rotate = _new_node(group, "ShaderNodeVectorRotate", "twist_rotation", (40, -100))
                rotate.rotation_type = "AXIS_ANGLE"
                subtract = _new_node(group, "ShaderNodeVectorMath", "deformation_field", (240, -100))
                subtract.operation = "SUBTRACT"
                axis_vector = {"X": (1.0, 0.0, 0.0), "Y": (0.0, 1.0, 0.0), "Z": (0.0, 0.0, 1.0)}[axis]
                set_input(rotate, "Axis", axis_vector)
                _expose(
                    group,
                    angle,
                    "Value",
                    "Strength",
                    "NodeSocketFloat",
                    strength,
                    "Twist radians per local-space unit",
                    occurrence=1,
                )
                link(group, position, "Position", separate, "Vector")
                link(group, separate, axis, angle, "Value", to_occurrence=0)
                link(group, position, "Position", rotate, "Vector")
                link(group, angle, "Value", rotate, "Angle")
                link(group, rotate, "Vector", subtract, "Vector", to_occurrence=0)
                link(group, position, "Position", subtract, "Vector", to_occurrence=1)
                link(group, subtract, "Vector", set_position, "Offset")
                nodes.update(
                    {
                        "position": position,
                        "axis_coordinate": separate,
                        "twist_angle": angle,
                        "twist_rotation": rotate,
                        "deformation_field": subtract,
                    }
                )
            else:
                position = _new_node(group, "GeometryNodeInputPosition", "position", (-550, -100))
                separate = _new_node(group, "ShaderNodeSeparateXYZ", "axis_coordinate", (-350, -100))
                factor = _new_node(group, "ShaderNodeMapRange", "taper_factor", (-150, -100))
                set_input(factor, "From Min", -scale)
                set_input(factor, "From Max", scale)
                set_input(factor, "To Min", 1.0 - strength)
                set_input(factor, "To Max", 1.0 + strength)
                combine = _new_node(group, "ShaderNodeCombineXYZ", "taper_scale", (40, -100))
                for component in "XYZ":
                    if component == axis:
                        set_input(combine, component, 1.0)
                    else:
                        link(group, factor, "Result", combine, component)
                multiply = _new_node(group, "ShaderNodeVectorMath", "scaled_position", (220, -100))
                multiply.operation = "MULTIPLY"
                subtract = _new_node(group, "ShaderNodeVectorMath", "deformation_field", (420, -100))
                subtract.operation = "SUBTRACT"
                link(group, position, "Position", separate, "Vector")
                link(group, separate, axis, factor, "Value")
                link(group, position, "Position", multiply, "Vector", to_occurrence=0)
                link(group, combine, "Vector", multiply, "Vector", to_occurrence=1)
                link(group, multiply, "Vector", subtract, "Vector", to_occurrence=0)
                link(group, position, "Position", subtract, "Vector", to_occurrence=1)
                link(group, subtract, "Vector", set_position, "Offset")
                nodes.update(
                    {
                        "position": position,
                        "axis_coordinate": separate,
                        "taper_factor": factor,
                        "taper_scale": combine,
                        "scaled_position": multiply,
                        "deformation_field": subtract,
                    }
                )
            if mask_attribute and template != "MASK_OFFSET":
                group["blender_mcp_mask_attribute"] = mask_attribute
            if target_object_name and template != "PROXIMITY_PUSH":
                target = bpy.data.objects.get(target_object_name)
                if target is None:
                    raise ValueError(f"Target object not found: {target_object_name}")
                group["blender_mcp_target_object"] = target.name
            group["blender_mcp_axis"] = axis
            group["blender_mcp_coordinate_space"] = coordinate_space
            link(group, set_position, "Geometry", group_output, "Geometry")
            modifier = _attach_builder(obj, group)
            return _finish_builder(obj, group, modifier, nodes)
        except Exception:
            _remove_builder_on_error(group)
            raise

    def create_volume_generator(
        self,
        object_name,
        group_name,
        source="MESH",
        output_type="VOLUME",
        density=1.0,
        voxel_size=0.1,
        radius=0.5,
        threshold=0.1,
        material_name=None,
        density_grid_name="density",
        delivery="LIVE_GRAPH",
        output_path=None,
        confirm_write=False,
        confirm_overwrite=False,
        **_unused,
    ):
        group = None
        modifier = None
        delivery_obj = None
        delivery_data = None
        # `{}` not None: it is only ever merged into the OPENVDB result with `**`,
        # and both the write and the merge sit behind the same delivery check.
        grid_evidence = {}
        try:
            if source not in {"MESH", "POINTS", "CUBE"}:
                raise ValueError("source must be MESH, POINTS, or CUBE")
            if output_type not in {"VOLUME", "MESH"}:
                raise ValueError("output_type must be VOLUME or MESH")
            if delivery not in {"LIVE_GRAPH", "OPENVDB"}:
                raise ValueError("delivery must be LIVE_GRAPH or OPENVDB")
            if density < 0 or voxel_size <= 0 or radius <= 0:
                raise ValueError("Require density >= 0, voxel_size > 0, and radius > 0")
            if not isinstance(density_grid_name, str) or not density_grid_name.strip():
                raise ValueError("density_grid_name must be a non-empty string")
            if delivery == "OPENVDB":
                if output_type != "VOLUME":
                    raise ValueError("OPENVDB delivery requires output_type=VOLUME")
                if not confirm_write or not output_path:
                    raise ValueError("OPENVDB delivery requires output_path and confirm_write=True")
                path = os.path.abspath(bpy.path.abspath(output_path))
                if os.path.splitext(path)[1].lower() != ".vdb":
                    raise ValueError("OpenVDB output_path must use the .vdb extension")
                directory = os.path.dirname(path)
                if not os.path.isdir(directory):
                    raise ValueError(f"OpenVDB output directory does not exist: {directory}")
                if os.path.exists(path) and not confirm_overwrite:
                    raise ValueError("OpenVDB output already exists; set confirm_overwrite=True to replace it")
            else:
                path = None
            obj, group = _prepare_builder(object_name, group_name, "VOLUME", "static procedural volume generator")
            if obj.modifiers.get(group.name) is not None:
                raise ValueError(f"Modifier already exists on '{obj.name}': {group.name}")
            if delivery == "OPENVDB" and bpy.data.objects.get(f"{group.name} Volume") is not None:
                raise ValueError(f"OpenVDB delivery object already exists: {group.name} Volume")
            group_input, group_output = _group_io(group)
            if source == "MESH":
                volume = _new_node(group, "GeometryNodeMeshToVolume", "volume_generator", (0, 100))
                _expose(
                    group, volume, "Density", "Density", "NodeSocketFloat", density, "Volume density", min_value=0.0
                )
                _expose(
                    group,
                    volume,
                    "Voxel Size",
                    "Voxel Size",
                    "NodeSocketFloat",
                    voxel_size,
                    "Volume voxel edge length",
                    min_value=0.000001,
                )
                link(group, group_input, "Geometry", volume, "Mesh")
            elif source == "POINTS":
                volume = _new_node(group, "GeometryNodePointsToVolume", "volume_generator", (0, 100))
                _expose(
                    group, volume, "Density", "Density", "NodeSocketFloat", density, "Volume density", min_value=0.0
                )
                _expose(
                    group,
                    volume,
                    "Voxel Size",
                    "Voxel Size",
                    "NodeSocketFloat",
                    voxel_size,
                    "Volume voxel edge length",
                    min_value=0.000001,
                )
                _expose(
                    group,
                    volume,
                    "Radius",
                    "Radius",
                    "NodeSocketFloat",
                    radius,
                    "Radius contributed by each point",
                    min_value=0.000001,
                )
                link(group, group_input, "Geometry", volume, "Points")
            else:
                volume = _new_node(group, "GeometryNodeVolumeCube", "volume_generator", (0, 100))
                _expose(
                    group,
                    volume,
                    "Density",
                    "Density",
                    "NodeSocketFloat",
                    density,
                    "Uniform volume density",
                    min_value=0.0,
                )
                resolution = max(1, min(512, math.ceil(2 * radius / voxel_size)))
                set_input(volume, "Min", (-radius, -radius, -radius))
                set_input(volume, "Max", (radius, radius, radius))
                for axis_name in ("Resolution X", "Resolution Y", "Resolution Z"):
                    set_input(volume, axis_name, resolution)
            terminal = volume
            terminal_output = "Volume"
            nodes = {"volume_generator": volume}
            if density_grid_name != "density":
                get_grid = _new_node(group, "GeometryNodeGetNamedGrid", "get_density_grid", (180, -120))
                get_grid.data_type = "FLOAT"
                set_input(get_grid, "Name", "density")
                link(group, volume, "Volume", get_grid, "Volume")
                store_grid = _new_node(group, "GeometryNodeStoreNamedGrid", "store_density_grid", (300, 100))
                store_grid.data_type = "FLOAT"
                set_input(store_grid, "Name", density_grid_name)
                link(group, volume, "Volume", store_grid, "Volume")
                link(group, get_grid, "Grid", store_grid, "Grid")
                terminal = store_grid
                terminal_output = "Volume"
                nodes.update({"get_density_grid": get_grid, "store_density_grid": store_grid})
            if output_type == "MESH":
                convert = _new_node(group, "GeometryNodeVolumeToMesh", "volume_to_mesh", (230, 100))
                _expose(
                    group,
                    convert,
                    "Threshold",
                    "Threshold",
                    "NodeSocketFloat",
                    threshold,
                    "Density isosurface threshold",
                    min_value=0.0,
                )
                _expose(
                    group,
                    convert,
                    "Voxel Size",
                    "Mesh Voxel Size",
                    "NodeSocketFloat",
                    voxel_size,
                    "Output mesh voxel edge length",
                    min_value=0.000001,
                )
                link(group, terminal, terminal_output, convert, "Volume")
                terminal = convert
                terminal_output = "Mesh"
                nodes["volume_to_mesh"] = convert
            if material_name:
                material = bpy.data.materials.get(material_name)
                if material is None:
                    raise ValueError(f"Material not found: {material_name}")
                set_material = _new_node(group, "GeometryNodeSetMaterial", "set_material", (460, 100))
                _expose(
                    group,
                    set_material,
                    "Material",
                    "Material",
                    "NodeSocketMaterial",
                    material,
                    "Material assigned to the generated geometry",
                )
                link(group, terminal, terminal_output, set_material, "Geometry")
                terminal = set_material
                terminal_output = "Geometry"
                nodes["set_material"] = set_material
            link(group, terminal, terminal_output, group_output, "Geometry")
            if delivery == "OPENVDB":
                grid_evidence = _write_openvdb_delivery(
                    obj, source, path, density_grid_name, density, voxel_size, radius
                )
            modifier = _attach_builder(obj, group)
            bounds = evaluated_summary(obj).get("world_bounds", {})
            minimum, maximum = bounds.get("min"), bounds.get("max")
            estimate = None
            if minimum and maximum:
                estimate = math.prod(max(1, math.ceil((maximum[i] - minimum[i]) / voxel_size)) for i in range(3))
            warnings = []
            if estimate and estimate > 256**3:
                warnings.append(f"Estimated voxel grid {estimate:,} cells is expensive; increase voxel_size.")
            result = _finish_builder(obj, group, modifier, nodes, warnings=warnings)
            result["estimated_voxel_cells"] = estimate
            result["simulation"] = False
            result["output_type"] = output_type
            result["grids"] = [{"name": density_grid_name, "data_type": "FLOAT"}]
            result["delivery"] = delivery
            if delivery == "OPENVDB":
                material = bpy.data.materials.get(material_name) if material_name else None
                delivery_obj, delivery_data = _create_vdb_backed_object(obj, group.name, path, material)
                result["openvdb"] = {
                    "path": path,
                    "bytes": os.path.getsize(path),
                    "grid": density_grid_name,
                    "object": delivery_obj.name,
                    "data": delivery_data.name,
                    "writer": "OPENVDB_PYTHON",
                    **grid_evidence,
                }
                result["changed_objects"].append(delivery_obj.name)
                result["changed_resources"].append(delivery_data.name)
            return result
        except Exception:
            if delivery_obj is not None:
                bpy.data.objects.remove(delivery_obj, do_unlink=True)
            if delivery_data is not None and bpy.data.volumes.get(delivery_data.name) is not None:
                bpy.data.volumes.remove(delivery_data)
            if modifier is not None and modifier.name in obj.modifiers:
                obj.modifiers.remove(modifier)
            _remove_builder_on_error(group)
            raise
