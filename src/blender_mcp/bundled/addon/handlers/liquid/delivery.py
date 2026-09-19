# pyright: reportOptionalSubscript=false, reportUnhashable=false
"""Blender-main-thread handlers for liquid proxy, variant, delivery, and scale workflows."""

import contextlib
import math
import os
import tempfile
import time
import uuid

from itertools import pairwise

import bmesh
import bpy
import mathutils

from ...helpers import preserve_mode_and_selection
from ._geometry import _RIM_AXES
from .inspection_and_setup import (
    _CACHE_FLAGS,
    _LIQUID_UUID_PROPERTY,
    _check_unique_cache_path,
    _ensure_collection,
    _ensure_liquid_uuid,
    _get_domain,
    _get_object,
    _get_scene,
    _liquid_object_identity,
    _read_fields,
    _register_owned_objects,
    _resolved_cache_path,
    _tag_liquid_object,
    _wall_thickness_samples,
    _world_bounds,
)
from .simulation import _cache_directory_evidence, _cache_state, _evaluated_output

_SCHEMA_VERSION = 1
_UNIT_METERS = {"METERS": 1.0, "CENTIMETERS": 0.01, "MILLIMETERS": 0.001}
_DEFORMING_MODIFIERS = {"ARMATURE", "CAST", "CURVE", "DISPLACE", "LATTICE", "MESH_DEFORM", "NODES", "WARP", "WAVE"}
_SPEED_ATTRIBUTE_NAMES = {"velocity", "fluid_velocity", "vel"}
_HOLLOW_CONTAINER_CAP_FRACTION = 0.08
_HOLLOW_CONTAINER_MIN_ALIGNMENT = 0.3


def _validate_name(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 63:
        raise ValueError(f"{label} must be a nonempty Blender ID name of at most 63 characters")


def _scene_view_layer(scene, objects):
    view_layer = next((layer for layer in scene.view_layers if all(obj.name in layer.objects for obj in objects)), None)
    if view_layer is None:
        raise ValueError(f"No view layer in scene '{scene.name}' contains every required object")
    return view_layer


def _local_bounds(obj):
    points = [mathutils.Vector(corner) for corner in obj.bound_box]
    minimum = mathutils.Vector(tuple(min(point[axis] for point in points) for axis in range(3)))
    maximum = mathutils.Vector(tuple(max(point[axis] for point in points) for axis in range(3)))
    dimensions = maximum - minimum
    if any(not math.isfinite(float(value)) or value <= 0.0 for value in dimensions):
        raise ValueError(f"Object '{obj.name}' has invalid local bounds for proxy generation")
    return (minimum + maximum) * 0.5, dimensions


def _box_mesh(name, center, dimensions):
    half = dimensions * 0.5
    x0, y0, z0 = center - half
    x1, y1, z1 = center + half
    vertices = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0)]
    vertices.extend([(x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)])
    faces = [(0, 1, 2, 3), (4, 7, 6, 5), (0, 4, 5, 1), (1, 5, 6, 2), (2, 6, 7, 3), (4, 0, 3, 7)]
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(vertices, [], faces)
    mesh.update()
    return mesh


def _capsule_mesh(name, center, dimensions):
    radius = max(min(float(dimensions.x), float(dimensions.y)) * 0.5, 1e-6)
    half_cylinder = max(float(dimensions.z) * 0.5 - radius, 0.0)
    mesh = bpy.data.meshes.new(name)
    bm = bmesh.new()
    try:
        result = bmesh.ops.create_uvsphere(bm, u_segments=16, v_segments=8, radius=1.0)
        for vertex in result["verts"]:
            unit_z = float(vertex.co.z)
            vertex.co.x = center.x + vertex.co.x * radius
            vertex.co.y = center.y + vertex.co.y * radius
            vertex.co.z = center.z + unit_z * radius + math.copysign(half_cylinder, unit_z)
        bm.to_mesh(mesh)
        mesh.update()
    finally:
        bm.free()
    return mesh


def _evaluated_mesh_copy(source, name):
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = source.evaluated_get(depsgraph)
    mesh = bpy.data.meshes.new_from_object(evaluated, depsgraph=depsgraph)
    mesh.name = name
    return mesh


def _convex_hull_mesh(source, name):
    mesh = _evaluated_mesh_copy(source, name)
    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        if len(bm.verts) < 4:
            raise ValueError(f"Object '{source.name}' needs at least four vertices for a convex hull")
        result = bmesh.ops.convex_hull(bm, input=list(bm.verts), use_existing_faces=False)
        interior = [
            item
            for item in result.get("geom_interior", ())
            if isinstance(item, (bmesh.types.BMVert, bmesh.types.BMEdge, bmesh.types.BMFace))
        ]
        if interior:
            bmesh.ops.delete(bm, geom=interior, context="VERTS")
        bm.to_mesh(mesh)
        mesh.update()
    finally:
        bm.free()
    return mesh


def _hollow_container_geometry(source, name, rim_axis):
    """
    Build an open-topped shell mesh from the source's evaluated geometry.

    Removes the cap facing ``rim_axis`` (the pour opening) and returns the mesh plus the
    vertex indices of the opposite cap, for weighting a distinct bottom thickness through a
    Solidify vertex group. Indices are only read *after* ``index_update()`` following the cap
    deletion, since ``bmesh.ops.delete`` does not guarantee surviving vertices keep their index.
    """
    axis_index, sign = _RIM_AXES[rim_axis]
    mesh = _evaluated_mesh_copy(source, name)
    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        bm.normal_update()
        coordinates = [vertex.co[axis_index] for vertex in bm.verts]  # pyright: ignore[reportGeneralTypeIssues]
        if not coordinates:
            raise ValueError(f"Object '{source.name}' has no evaluated geometry for a hollow-container proxy")
        axis_minimum, axis_maximum = min(coordinates), max(coordinates)
        axis_range = axis_maximum - axis_minimum
        if axis_range <= 1e-9:
            raise ValueError(f"Object '{source.name}' is degenerate along rim_axis={rim_axis}")
        opening_direction = mathutils.Vector((0.0, 0.0, 0.0))
        opening_direction[axis_index] = sign
        # The opening (pour cap) sits at whichever extreme opening_direction points toward; the
        # kept cap (for bottom-thickness weighting) sits at the opposite extreme.
        opening_extreme = axis_maximum if sign > 0 else axis_minimum
        base_extreme = axis_minimum if sign > 0 else axis_maximum
        band = _HOLLOW_CONTAINER_CAP_FRACTION * axis_range

        def cap_faces(direction, extreme):
            faces = []
            for face in bm.faces:  # pyright: ignore[reportGeneralTypeIssues]
                aligned = face.normal.dot(direction) > _HOLLOW_CONTAINER_MIN_ALIGNMENT
                in_band = all(abs(vertex.co[axis_index] - extreme) <= band for vertex in face.verts)
                if aligned and in_band:
                    faces.append(face)
            return faces

        top_faces = cap_faces(opening_direction, opening_extreme)
        if not top_faces:
            raise ValueError(
                f"Could not detect a pour-opening cap on '{source.name}' for rim_axis={rim_axis}; "
                "try a different rim_axis or use SUPPLIED geometry"
            )
        bmesh.ops.delete(bm, geom=top_faces, context="FACES")
        bm.verts.index_update()
        bm.faces.index_update()
        bottom_faces = cap_faces(-opening_direction, base_extreme)
        bottom_vertex_indices = sorted({vertex.index for face in bottom_faces for vertex in face.verts})
        bm.to_mesh(mesh)
        mesh.update()
    finally:
        bm.free()
    return mesh, bottom_vertex_indices


def _has_deformation(obj):
    shape_keys = getattr(getattr(obj, "data", None), "shape_keys", None)
    return bool(
        (shape_keys and (shape_keys.animation_data or len(shape_keys.key_blocks) > 1))
        or any(modifier.type in _DEFORMING_MODIFIERS for modifier in obj.modifiers)
    )


def _matrix_error(first, second):
    return max(abs(float(first[row][column]) - float(second[row][column])) for row in range(4) for column in range(4))


def _fluid_modifier(obj, role):
    return next(
        (modifier for modifier in obj.modifiers if modifier.type == "FLUID" and modifier.fluid_type == role),
        None,
    )


def _dependency_objects(settings):
    groups = {
        "FLOW": getattr(settings, "fluid_group", None),
        "EFFECTOR": getattr(settings, "effector_group", None),
        "FORCE": getattr(settings, "force_collection", None),
    }
    records = []
    seen = set()
    for role, collection in groups.items():
        if collection is None:
            continue
        for obj in collection.all_objects:
            key = int(getattr(obj, "session_uid", id(obj)))
            if key in seen:
                continue
            seen.add(key)
            records.append((obj, role, collection))
    guide_parent = getattr(settings, "guide_parent", None)
    if guide_parent is not None and int(getattr(guide_parent, "session_uid", id(guide_parent))) not in seen:
        records.append((guide_parent, "GUIDE_DOMAIN", None))
    return records


def _copy_animation(source, duplicate, policy):
    if policy == "NONE":
        duplicate.animation_data_clear()
        return None
    animation = getattr(source, "animation_data", None)
    if animation is None or animation.action is None:
        return None
    duplicate.animation_data_create()
    duplicate.animation_data.action = animation.action.copy() if policy == "COPY" else animation.action
    return duplicate.animation_data.action.name


def _copy_materials(obj, material_map):
    if obj.data is None or not hasattr(obj.data, "materials"):
        return []
    copied = []
    for index, material in enumerate(list(obj.data.materials)):
        if material is None:
            continue
        key = int(getattr(material, "session_uid", id(material)))
        replacement = material_map.get(key)
        if replacement is None:
            replacement = material.copy()
            replacement.name = f"{material.name} Variant"
            material_map[key] = replacement
        obj.data.materials[index] = replacement
        copied.append(replacement.name)
    return copied


def _collection_name(source, suffix):
    base = f"{source.name} {suffix}".strip()
    return base[:63]


def _modifier_order(obj):
    return [modifier.name for modifier in obj.modifiers]


def _restore_modifier_order(obj, order):
    for target_index, name in enumerate(order):
        modifier = obj.modifiers.get(name)
        if modifier is not None:
            current = list(obj.modifiers).index(modifier)
            if current != target_index:
                obj.modifiers.move(current, target_index)


def _post_fluid_modifier(obj, fluid, name, modifier_type, existing_policy):
    existing = obj.modifiers.get(name)
    if existing is not None:
        if existing.type != modifier_type:
            raise ValueError(f"Modifier '{name}' exists with type {existing.type}, expected {modifier_type}")
        if existing_policy != "REUSE":
            raise ValueError(f"Modifier already exists: {name}")
        modifier = existing
        created = False
    else:
        modifier = obj.modifiers.new(name=name, type=modifier_type)
        created = True
    fluid_index = list(obj.modifiers).index(fluid)
    current = list(obj.modifiers).index(modifier)
    if current <= fluid_index:
        obj.modifiers.move(current, fluid_index + 1)
    return modifier, created


def _speed_attributes(obj):
    evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    mesh = evaluated.to_mesh()
    try:
        names = [attribute.name for attribute in getattr(mesh, "attributes", ())]
        return {"available": any(name.lower() in _SPEED_ATTRIBUTE_NAMES for name in names), "attributes": names[:100]}
    finally:
        evaluated.to_mesh_clear()


def _set_scene_frame_range(scene, start, end, step):
    if start > scene.frame_end:
        scene.frame_end = end
        scene.frame_start = start
    else:
        scene.frame_start = start
        scene.frame_end = end
    scene.frame_step = step


def _validate_axes(forward, up):
    if forward.removeprefix("NEGATIVE_") == up.removeprefix("NEGATIVE_"):
        raise ValueError("forward_axis and up_axis must refer to different axes")


def _mesh_cost(obj):
    evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    mesh = evaluated.to_mesh()
    try:
        triangles = sum(max(1, len(polygon.vertices) - 2) for polygon in mesh.polygons)
        return {"vertices": len(mesh.vertices), "faces": len(mesh.polygons), "triangles": triangles}
    finally:
        evaluated.to_mesh_clear()


def _bounds_volume(bounds):
    dimensions = bounds["dimensions"]
    return max(float(dimensions[0]), 0.0) * max(float(dimensions[1]), 0.0) * max(float(dimensions[2]), 0.0)


class LiquidDeliveryHandlers:
    """Provide production liquid proxy, variant, render, export, and performance handlers."""

    def create_liquid_proxy_rig(
        self,
        scene_name,
        source_object_name,
        proxy_object_name,
        domain_object_name,
        domain_modifier_name,
        role,
        geometry="BOX",
        driver="COPY_TRANSFORMS",
        collection_name="Liquid Proxies",
        modifier_name="Liquid Proxy",
        existing_policy="ERROR",
        decimate_ratio=0.2,
        wall_thickness=0.05,
        bottom_thickness=None,
        rim_axis="Z",
        allow_deforming_proxy=False,
        flow_settings=None,
        effector_settings=None,
        validation_frames=None,
    ):
        scene = _get_scene(scene_name)
        source = _get_object(source_object_name, {"MESH"})
        domain, _domain_modifier, domain_settings = _get_domain(domain_object_name, domain_modifier_name)
        if source.name not in scene.objects or domain.name not in scene.objects:
            raise ValueError("Source and domain must be linked to the explicit scene")
        if role not in {"FLOW", "EFFECTOR"} or geometry not in {
            "BOX",
            "CAPSULE",
            "CONVEX_HULL",
            "DECIMATED",
            "HOLLOW_CONTAINER",
            "SUPPLIED",
        }:
            raise ValueError("Unsupported proxy role or geometry")
        if driver not in {"COPY_TRANSFORMS", "PARENT"}:
            raise ValueError("driver must be COPY_TRANSFORMS or PARENT")
        if (flow_settings and role != "FLOW") or (effector_settings and role != "EFFECTOR"):
            raise ValueError("Flow/effector settings must match the selected proxy role")
        if geometry == "HOLLOW_CONTAINER":
            if rim_axis not in _RIM_AXES:
                raise ValueError(f"rim_axis must be one of {sorted(_RIM_AXES)}")
            if not isinstance(wall_thickness, (int, float)) or isinstance(wall_thickness, bool) or wall_thickness <= 0:
                raise ValueError("wall_thickness must be a positive number")
            if bottom_thickness is not None and (
                not isinstance(bottom_thickness, (int, float))
                or isinstance(bottom_thickness, bool)
                or bottom_thickness <= 0
            ):
                raise ValueError("bottom_thickness must be a positive number")
        frames = sorted({int(frame) for frame in (validation_frames or [scene.frame_current])})
        if not frames or len(frames) > 12:
            raise ValueError("validation_frames must contain 1-12 unique frames")
        if source.name == proxy_object_name:
            raise ValueError("Source and proxy must be distinct objects")
        _validate_name(proxy_object_name, "proxy_object_name")
        _validate_name(collection_name, "collection_name")
        existing = bpy.data.objects.get(proxy_object_name)
        if geometry == "SUPPLIED":
            if existing is None or existing.type != "MESH":
                raise ValueError("SUPPLIED requires proxy_object_name to identify an existing mesh")
            proxy = existing
            if proxy.name not in scene.objects:
                raise ValueError("Supplied proxy must be linked to the explicit scene")
            if _fluid_modifier(proxy, role) and existing_policy != "REUSE":
                raise ValueError(f"Supplied proxy '{proxy.name}' already has a {role} fluid modifier")
            if _has_deformation(proxy) and not allow_deforming_proxy:
                raise ValueError("A deforming supplied proxy requires allow_deforming_proxy=True")
            created = False
        else:
            if existing is not None:
                raise ValueError(f"Proxy object already exists: {proxy_object_name}")
            center, dimensions = _local_bounds(source)
            bottom_vertex_indices = []
            if geometry == "BOX":
                mesh = _box_mesh(f"{proxy_object_name} Mesh", center, dimensions)
            elif geometry == "CAPSULE":
                mesh = _capsule_mesh(f"{proxy_object_name} Mesh", center, dimensions)
            elif geometry == "CONVEX_HULL":
                mesh = _convex_hull_mesh(source, f"{proxy_object_name} Mesh")
            elif geometry == "HOLLOW_CONTAINER":
                mesh, bottom_vertex_indices = _hollow_container_geometry(source, f"{proxy_object_name} Mesh", rim_axis)
            else:
                mesh = _evaluated_mesh_copy(source, f"{proxy_object_name} Mesh")
            proxy = bpy.data.objects.new(proxy_object_name, mesh)
            created = True
        collection, _collection_created, _linked = _ensure_collection(scene, collection_name)
        if proxy.name not in collection.objects:
            collection.objects.link(proxy)
        original_frame = scene.frame_current
        original_subframe = scene.frame_subframe
        old_parent = proxy.parent
        old_parent_inverse = proxy.matrix_parent_inverse.copy()
        old_basis = proxy.matrix_basis.copy()
        old_matrix = proxy.matrix_world.copy()
        old_hide_render = proxy.hide_render
        old_display_type = proxy.display_type
        created_constraint = None
        created_decimate = None
        created_solidify = None
        created_vertex_group = None
        tag_keys = (
            "blendermcp_liquid_simulation_id",
            "blendermcp_liquid_role",
            "blendermcp_liquid_source",
            _LIQUID_UUID_PROPERTY,
        )
        old_tags = {key: (key in proxy, proxy.get(key)) for key in tag_keys}
        simulation_id = domain.get("blendermcp_liquid_simulation_id") or uuid.uuid4().hex
        try:
            proxy.matrix_world = source.matrix_world.copy()
            if driver == "COPY_TRANSFORMS":
                created_constraint = proxy.constraints.new(type="COPY_TRANSFORMS")
                created_constraint.name = "Liquid Proxy Follow"
                created_constraint.target = source
                created_constraint.owner_space = "WORLD"
                created_constraint.target_space = "WORLD"
            else:
                proxy.parent = source
                proxy.matrix_parent_inverse = mathutils.Matrix.Identity(4)
                proxy.matrix_basis = mathutils.Matrix.Identity(4)
            if geometry == "DECIMATED":
                created_decimate = proxy.modifiers.new(name="Liquid Proxy Decimate", type="DECIMATE")
                created_decimate.ratio = decimate_ratio
            elif geometry == "HOLLOW_CONTAINER":
                created_solidify = proxy.modifiers.new(name="Liquid Proxy Shell", type="SOLIDIFY")
                created_solidify.offset = -1.0
                created_solidify.use_rim = True
                resolved_bottom_thickness = wall_thickness if bottom_thickness is None else bottom_thickness
                created_solidify.thickness = resolved_bottom_thickness
                if bottom_vertex_indices and not math.isclose(resolved_bottom_thickness, wall_thickness, rel_tol=1e-9):
                    created_vertex_group = proxy.vertex_groups.new(name="Liquid Proxy Bottom")
                    created_vertex_group.add(bottom_vertex_indices, 1.0, "REPLACE")
                    created_solidify.vertex_group = created_vertex_group.name
                    created_solidify.thickness_vertex_group = wall_thickness / resolved_bottom_thickness
            if role == "FLOW":
                record = self.add_liquid_flow(
                    proxy.name,
                    domain.name,
                    behavior=(flow_settings or {}).get("behavior", "GEOMETRY"),
                    modifier_name=modifier_name,
                    existing_policy=existing_policy,
                    settings={
                        key: value
                        for key, value in {
                            "use_inflow": (flow_settings or {}).get("use_inflow"),
                            "subframes": (flow_settings or {}).get("subframes", 0),
                            "surface_distance": (flow_settings or {}).get("surface_distance", 1.5),
                            "use_initial_velocity": (flow_settings or {}).get("use_initial_velocity", False),
                            "velocity_factor": (flow_settings or {}).get("velocity_factor", 1.0),
                        }.items()
                        if value is not None
                    },
                )
            else:
                record = self.add_liquid_effector(
                    proxy.name,
                    domain.name,
                    modifier_name=modifier_name,
                    existing_policy=existing_policy,
                    settings={
                        "subframes": (effector_settings or {}).get("subframes", 0),
                        "surface_distance": (effector_settings or {}).get("surface_distance", 0.001),
                        "use_plane_init": (effector_settings or {}).get("use_plane_init", False),
                    },
                )
            proxy.hide_render = True
            proxy.display_type = "WIRE"
            proxy["blendermcp_liquid_simulation_id"] = simulation_id
            # add_liquid_flow/add_liquid_effector already tagged the proxy with the plain FLOW/EFFECTOR
            # role; re-tagging narrows it to the proxy role while keeping the same UUID.
            proxy_uuid = _tag_liquid_object(proxy, f"{role}_PROXY")["uuid"]
            proxy["blendermcp_liquid_source"] = source.name
            evidence = []
            for frame in frames:
                scene.frame_set(frame)
                bpy.context.view_layer.update()
                evidence.append(
                    {"frame": frame, "maximum_matrix_error": _matrix_error(proxy.matrix_world, source.matrix_world)}
                )
            maximum_error = max(item["maximum_matrix_error"] for item in evidence)
            if maximum_error > 1e-5:
                raise RuntimeError(f"Proxy transform validation failed; maximum matrix error={maximum_error:g}")
        except Exception:
            if not created:
                with contextlib.suppress(Exception):
                    if created_constraint is not None:
                        proxy.constraints.remove(created_constraint)
                    if created_decimate is not None:
                        proxy.modifiers.remove(created_decimate)
                    if created_solidify is not None:
                        proxy.modifiers.remove(created_solidify)
                    if created_vertex_group is not None:
                        proxy.vertex_groups.remove(created_vertex_group)
                    proxy.parent = old_parent
                    proxy.matrix_parent_inverse = old_parent_inverse
                    proxy.matrix_basis = old_basis
                    proxy.matrix_world = old_matrix
                    proxy.hide_render = old_hide_render
                    proxy.display_type = old_display_type
                    for key, (existed, value) in old_tags.items():
                        if existed:
                            proxy[key] = value
                        elif key in proxy:
                            del proxy[key]
            raise
        finally:
            scene.frame_set(original_frame, subframe=original_subframe)
        warnings = ["Mantaflow interaction is one-way; liquid does not apply forces back to the source asset."]
        if _has_deformation(source) and geometry != "SUPPLIED":
            warnings.append(
                "The source deforms, but this generated proxy follows transforms only; "
                "use a supplied deforming proxy if needed."
            )
        if geometry == "HOLLOW_CONTAINER":
            if bottom_thickness is not None and not bottom_vertex_indices:
                warnings.append(
                    "Could not detect a distinct bottom cap; bottom_thickness was ignored and "
                    "wall_thickness was applied uniformly."
                )
            domain_bounds = _world_bounds(domain, evaluated=False)
            cell_size = max(domain_bounds["dimensions"]) / domain_settings.resolution_max
            thicknesses = _wall_thickness_samples(proxy, cell_size)
            if thicknesses and min(thicknesses) < cell_size * 1.5:
                warnings.append(
                    "Hollow-container wall thickness is under 1.5 estimated domain cells at a sampled point "
                    f"(min={min(thicknesses):.4g}, cell_size={cell_size:.4g}); liquid may leak through. "
                    "Increase wall_thickness/bottom_thickness or domain resolution."
                )
        domain_uuid = _ensure_liquid_uuid(domain)
        manifest = _register_owned_objects(domain_settings, domain_uuid, [(proxy_uuid, proxy.name, f"{role}_PROXY")])
        return {
            "changed_objects": [proxy.name, domain.name],
            "source": source.name,
            "proxy": proxy.name,
            "created_proxy": created,
            "geometry": geometry,
            "driver": driver,
            "role": role,
            "fluid_modifier": record["modifier"],
            "collection": collection.name,
            "simulation_id": simulation_id,
            "domain_uuid": domain_uuid,
            "proxy_uuid": proxy_uuid,
            "proxy_role": f"{role}_PROXY",
            "manifest_registered": manifest is not None,
            "transform_validation": evidence,
            "source_preserved": True,
            "proxy_render_hidden": True,
            "warnings": warnings,
        }

    def duplicate_liquid_setup_variant(
        self,
        source_domain_object_name,
        source_domain_modifier_name,
        variant_domain_object_name,
        variant_collection_name,
        name_suffix,
        cache_directory,
        mesh_data_policy="COPY",
        material_policy="LINK",
        animation_policy="COPY",
        activation_policy="DISABLE_VARIANT",
    ):
        source, source_modifier, source_settings = _get_domain(source_domain_object_name, source_domain_modifier_name)
        if any(getattr(source_settings, flag, False) for flag in _CACHE_FLAGS if flag.startswith("is_cache_baking")):
            raise ValueError("Cannot duplicate a liquid setup while its cache is baking")
        if mesh_data_policy not in {"COPY", "LINK"} or material_policy not in {"COPY", "LINK"}:
            raise ValueError("Mesh/material policies must be COPY or LINK")
        if animation_policy not in {"COPY", "LINK", "NONE"}:
            raise ValueError("animation_policy must be COPY, LINK, or NONE")
        if activation_policy not in {"DISABLE_SOURCE", "DISABLE_VARIANT"}:
            raise ValueError("activation_policy must disable exactly one domain")
        if material_policy == "COPY" and mesh_data_policy == "LINK":
            raise ValueError("material_policy=COPY requires mesh_data_policy=COPY to avoid editing source slots")
        for value, label in (
            (variant_domain_object_name, "variant_domain_object_name"),
            (variant_collection_name, "variant_collection_name"),
            (name_suffix, "name_suffix"),
        ):
            _validate_name(value, label)
        if bpy.data.objects.get(variant_domain_object_name) is not None:
            raise ValueError(f"Variant domain already exists: {variant_domain_object_name}")
        if bpy.data.collections.get(variant_collection_name) is not None:
            raise ValueError(f"Variant collection already exists: {variant_collection_name}")
        resolved_cache = _resolved_cache_path(cache_directory)
        if (
            not os.path.isabs(resolved_cache)
            or not os.path.isdir(resolved_cache)
            or not os.access(resolved_cache, os.W_OK)
        ):
            raise ValueError("cache_directory must resolve to an existing writable absolute directory")
        with os.scandir(resolved_cache) as entries:
            if next(entries, None) is not None:
                raise ValueError("Variant cache_directory must be empty")
        _check_unique_cache_path(source_settings, cache_directory)
        dependencies = _dependency_objects(source_settings)
        originals = [source, *[record[0] for record in dependencies]]
        names = {source: variant_domain_object_name}
        for obj in originals[1:]:
            names[obj] = _collection_name(obj, name_suffix)
        if len(set(names.values())) != len(names) or any(bpy.data.objects.get(name) for name in names.values()):
            raise ValueError("Variant object names collide with each other or existing scene objects")
        scene = next((candidate for candidate in bpy.data.scenes if source.name in candidate.objects), None)
        if scene is None:
            raise ValueError("Source domain is not linked to a scene")
        collection = bpy.data.collections.new(variant_collection_name)
        scene.collection.children.link(collection)
        role_collections = {}
        for role, source_collection in {
            "FLOW": getattr(source_settings, "fluid_group", None),
            "EFFECTOR": getattr(source_settings, "effector_group", None),
            "FORCE": getattr(source_settings, "force_collection", None),
        }.items():
            if source_collection is not None:
                child = bpy.data.collections.new(_collection_name(source_collection, name_suffix))
                collection.children.link(child)
                role_collections[role] = child
        mapping = {}
        material_map = {}
        animation_records = {}
        variant_uuids = {}
        simulation_id = uuid.uuid4().hex
        source_visibility = (source_modifier.show_viewport, source_modifier.show_render)
        try:
            for original in originals:
                duplicate = original.copy()
                duplicate.name = names[original]
                if original.data is not None and mesh_data_policy == "COPY":
                    duplicate.data = original.data.copy()
                    duplicate.data.name = f"{duplicate.name} Data"
                collection.objects.link(duplicate)
                mapping[original] = duplicate
                action = _copy_animation(original, duplicate, animation_policy)
                if action:
                    animation_records[duplicate.name] = action
                if material_policy == "COPY":
                    _copy_materials(duplicate, material_map)
                duplicate["blendermcp_liquid_simulation_id"] = simulation_id
                duplicate["blendermcp_liquid_variant_source"] = original.name
                duplicate["blendermcp_liquid_schema_version"] = _SCHEMA_VERSION
                # `Object.copy()` carries custom properties across, so each duplicate would otherwise
                # share its original's liquid UUID. Re-issue a fresh identity, keeping the recorded role.
                if _liquid_object_identity(original)["uuid"]:
                    duplicate[_LIQUID_UUID_PROPERTY] = uuid.uuid4().hex
                    variant_uuids[duplicate] = duplicate[_LIQUID_UUID_PROPERTY]
            variant = mapping[source]
            variant_modifier = variant.modifiers.get(source_modifier.name)
            if variant_modifier is None or variant_modifier.type != "FLUID" or variant_modifier.fluid_type != "DOMAIN":
                raise RuntimeError("Copied object did not retain the liquid domain modifier")
            settings = variant_modifier.domain_settings
            if settings is None:
                bpy.context.view_layer.update()
                settings = variant_modifier.domain_settings
            if settings is None:
                raise RuntimeError("Copied domain settings are unavailable")
            settings.cache_directory = cache_directory
            for role, _source_collection in {
                "FLOW": getattr(source_settings, "fluid_group", None),
                "EFFECTOR": getattr(source_settings, "effector_group", None),
                "FORCE": getattr(source_settings, "force_collection", None),
            }.items():
                target = role_collections.get(role)
                if target is None:
                    continue
                for original, member_role, _collection in dependencies:
                    if member_role == role:
                        target.objects.link(mapping[original])
                if role == "FLOW":
                    settings.fluid_group = target
                elif role == "EFFECTOR":
                    settings.effector_group = target
                else:
                    settings.force_collection = target
                target["blendermcp_liquid_simulation_id"] = simulation_id
                target["blendermcp_liquid_role"] = role
            guide_parent = getattr(source_settings, "guide_parent", None)
            if guide_parent is not None:
                settings.guide_parent = mapping.get(guide_parent)
                if settings.guide_parent is None:
                    raise RuntimeError("Guide-parent domain was not duplicated")
            if activation_policy == "DISABLE_SOURCE":
                source_modifier.show_viewport = False
                source_modifier.show_render = False
                disabled = source.name
            else:
                variant_modifier.show_viewport = False
                variant_modifier.show_render = False
                disabled = variant.name
            bpy.context.view_layer.update()
            active = [flag for flag in _CACHE_FLAGS if bool(getattr(settings, flag, False))]
            if active:
                raise RuntimeError(f"Variant unexpectedly inherited active cache state: {active}")
            if _resolved_cache_path(settings.cache_directory) == _resolved_cache_path(source_settings.cache_directory):
                raise RuntimeError("Variant and source resolve to the same cache directory")
        except Exception:
            source_modifier.show_viewport, source_modifier.show_render = source_visibility
            raise
        variant_uuid = variant_uuids.get(variant) or _ensure_liquid_uuid(variant)
        manifest = _register_owned_objects(
            settings,
            variant_uuid,
            [
                (object_uuid, duplicate.name, _liquid_object_identity(duplicate)["role"] or "UNKNOWN")
                for duplicate, object_uuid in variant_uuids.items()
            ],
        )
        return {
            "changed_objects": [item.name for item in mapping.values()]
            + ([source.name] if disabled == source.name else []),
            "source_domain": source.name,
            "variant_domain": variant.name,
            "variant_modifier": variant_modifier.name,
            "variant_collection": collection.name,
            "simulation_id": simulation_id,
            "variant_domain_uuid": variant_uuid,
            "variant_object_uuids": {duplicate.name: value for duplicate, value in variant_uuids.items()},
            "manifest_registered": manifest is not None,
            "object_mapping": {original.name: duplicate.name for original, duplicate in mapping.items()},
            "role_collections": {role: item.name for role, item in role_collections.items()},
            "animation_actions": animation_records,
            "policies": {
                "mesh_data": mesh_data_policy,
                "materials": material_policy,
                "animation": animation_policy,
                "activation": activation_policy,
            },
            "disabled_domain": disabled,
            "cache": _cache_state(settings),
            "cache_directory_resolved": resolved_cache,
            "source_cache_preserved": True,
            "warnings": [
                "Variant dependencies are independent objects; linked mesh/material policies still share "
                "named datablocks."
            ],
        }

    def prepare_liquid_render_mesh(
        self,
        domain_object_name,
        modifier_name,
        finish,
        output_policy="REQUIRE_BAKED",
        material_name=None,
        material_assignment="KEEP",
        material_slot_index=None,
        subdivision_modifier_name="Liquid Render Subdivision",
        smooth_modifier_name="Liquid Render Smooth",
        laplacian_modifier_name="Liquid Render Laplacian",
        existing_policy="ERROR",
        delivery_object_name=None,
    ):
        obj, fluid, settings = _get_domain(domain_object_name, modifier_name)
        if not settings.use_mesh:
            raise ValueError("Liquid mesh output is disabled; configure and bake the mesh stage first")
        if output_policy == "REQUIRE_BAKED" and not settings.has_cache_baked_mesh:
            raise ValueError("REQUIRE_BAKED needs a completed liquid mesh cache")
        if output_policy == "ALLOW_REPLAY" and settings.cache_type != "REPLAY" and not settings.has_cache_baked_mesh:
            raise ValueError("ALLOW_REPLAY requires REPLAY mode or an already baked mesh")
        before = _evaluated_output(obj)
        base_counts = {
            "vertices": len(obj.data.vertices),
            "edges": len(obj.data.edges),
            "faces": len(obj.data.polygons),
        }
        if (
            output_policy == "ALLOW_REPLAY"
            and not settings.has_cache_baked_mesh
            and before["vertices"] == base_counts["vertices"]
            and before["faces"] == base_counts["faces"]
        ):
            raise ValueError("No evaluated replay liquid surface is observable at the current frame")
        if material_assignment not in {"KEEP", "APPEND", "REPLACE_SLOT"}:
            raise ValueError("Unsupported material assignment policy")
        material = bpy.data.materials.get(material_name) if material_name else None
        if material_name and material is None:
            raise ValueError(f"Material not found: {material_name}")
        if material_assignment != "KEEP" and material is None:
            raise ValueError("A material_name is required when assigning a material")
        if material_assignment == "REPLACE_SLOT" and (
            material_slot_index is None or material_slot_index >= len(obj.material_slots)
        ):
            raise ValueError("material_slot_index must identify an existing slot")
        if material_assignment != "REPLACE_SLOT" and material_slot_index is not None:
            raise ValueError("material_slot_index is valid only for REPLACE_SLOT")
        if existing_policy not in {"ERROR", "REUSE"}:
            raise ValueError("existing_policy must be ERROR or REUSE")
        requested = {
            subdivision_modifier_name: "SUBSURF" if finish.get("subdivision_levels") is not None else None,
            smooth_modifier_name: "SMOOTH" if finish.get("smooth_factor") is not None else None,
            laplacian_modifier_name: "LAPLACIANSMOOTH" if finish.get("laplacian_lambda") is not None else None,
        }
        for name, modifier_type in requested.items():
            if modifier_type and obj.modifiers.get(name) is not None and existing_policy == "ERROR":
                raise ValueError(f"Render modifier already exists: {name}")
        if delivery_object_name and bpy.data.objects.get(delivery_object_name) is not None:
            raise ValueError(f"Delivery object already exists: {delivery_object_name}")
        original_order = _modifier_order(obj)
        old_smooth = [polygon.use_smooth for polygon in obj.data.polygons]
        old_hide_render = obj.hide_render
        speed_before = _speed_attributes(obj)
        snapshots = {}
        created = []
        delivery = None
        appended_material = False
        replaced_material = None
        try:
            if finish.get("smooth_shading", True):
                for polygon in obj.data.polygons:
                    polygon.use_smooth = True
            if finish.get("subdivision_levels") is not None:
                modifier, was_created = _post_fluid_modifier(
                    obj, fluid, subdivision_modifier_name, "SUBSURF", existing_policy
                )
                snapshots[modifier.name] = (modifier.levels, modifier.render_levels)
                modifier.levels = finish["subdivision_levels"]
                modifier.render_levels = finish.get("subdivision_render_levels", finish["subdivision_levels"])
                created.extend([modifier.name] if was_created else [])
            if finish.get("smooth_factor") is not None:
                modifier, was_created = _post_fluid_modifier(
                    obj, fluid, smooth_modifier_name, "SMOOTH", existing_policy
                )
                snapshots[modifier.name] = (modifier.factor, modifier.iterations)
                modifier.factor = finish["smooth_factor"]
                modifier.iterations = finish.get("smooth_iterations", 2)
                created.extend([modifier.name] if was_created else [])
            if finish.get("laplacian_lambda") is not None:
                modifier, was_created = _post_fluid_modifier(
                    obj, fluid, laplacian_modifier_name, "LAPLACIANSMOOTH", existing_policy
                )
                snapshots[modifier.name] = (modifier.lambda_factor, modifier.iterations)
                modifier.lambda_factor = finish["laplacian_lambda"]
                modifier.iterations = finish.get("laplacian_iterations", 2)
                created.extend([modifier.name] if was_created else [])
            if material_assignment == "APPEND":
                obj.data.materials.append(material)
                appended_material = True
            elif material_assignment == "REPLACE_SLOT":
                replaced_material = obj.material_slots[material_slot_index].material
                obj.material_slots[material_slot_index].material = material
            obj.hide_render = False
            bpy.context.view_layer.update()
            speed_after = _speed_attributes(obj)
            if delivery_object_name:
                evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
                mesh = bpy.data.meshes.new_from_object(evaluated, depsgraph=bpy.context.evaluated_depsgraph_get())
                mesh.name = f"{delivery_object_name} Mesh"
                delivery = bpy.data.objects.new(delivery_object_name, mesh)
                target_collection = obj.users_collection[0] if obj.users_collection else bpy.context.scene.collection
                target_collection.objects.link(delivery)
                delivery.matrix_world = obj.matrix_world.copy()
                delivery["blendermcp_liquid_delivery_source"] = obj.name
                delivery["blendermcp_liquid_delivery_frame"] = bpy.context.scene.frame_current
            after = _evaluated_output(obj)
        except Exception:
            for polygon, value in zip(obj.data.polygons, old_smooth, strict=False):
                polygon.use_smooth = value
            obj.hide_render = old_hide_render
            if appended_material:
                obj.data.materials.pop(index=len(obj.data.materials) - 1)
            if replaced_material is not None:
                obj.material_slots[material_slot_index].material = replaced_material
            for name, values in snapshots.items():
                modifier = obj.modifiers.get(name)
                if modifier is None or name in created:
                    continue
                if modifier.type == "SUBSURF":
                    modifier.levels, modifier.render_levels = values
                elif modifier.type == "SMOOTH":
                    modifier.factor, modifier.iterations = values
                else:
                    modifier.lambda_factor, modifier.iterations = values
            _restore_modifier_order(obj, original_order)
            raise
        warnings = []
        if created and speed_before["available"]:
            warnings.append(
                "Post-fluid modifiers may interpolate or discard velocity attributes; verify motion blur downstream."
            )
        if speed_before["available"] and not speed_after["available"]:
            warnings.append("A recognized velocity attribute disappeared after render finishing.")
        if delivery is not None:
            warnings.append(
                "The delivery object is a static evaluated copy at the reported frame, not a live simulation."
            )
        return {
            "changed_objects": [obj.name] + ([delivery.name] if delivery else []),
            "domain": obj.name,
            "fluid_modifier": fluid.name,
            "output_policy": output_policy,
            "base_counts": base_counts,
            "evaluated_before": before,
            "evaluated_after": after,
            "modifier_order": _modifier_order(obj),
            "created_modifiers": created,
            "material_slots": [slot.material.name if slot.material else None for slot in obj.material_slots],
            "speed_vectors": {"before": speed_before, "after": speed_after},
            "delivery_object": delivery.name if delivery else None,
            "source_cache_preserved": True,
            "warnings": warnings,
        }

    def export_liquid_simulation(
        self,
        scene_name,
        domain_object_name,
        modifier_name,
        filepath,
        file_format,
        frame_start,
        frame_end,
        frame_step=1,
        coordinate_space="WORLD",
        units="SCENE",
        forward_axis="NEGATIVE_Z",
        up_axis="Y",
        include_surface=True,
        include_secondary_particles=False,
        include_materials=True,
        include_velocity_attributes=True,
        overwrite=False,
        max_frames=500,
    ):
        scene = _get_scene(scene_name)
        obj, _modifier, settings = _get_domain(domain_object_name, modifier_name)
        if obj.name not in scene.objects:
            raise ValueError("Domain must be linked to the explicit scene")
        if file_format not in {"ALEMBIC", "USD"} or coordinate_space not in {"WORLD", "LOCAL"}:
            raise ValueError("Unsupported export format or coordinate space")
        if units not in {"SCENE", *_UNIT_METERS}:
            raise ValueError("Unsupported units")
        _validate_axes(forward_axis, up_axis)
        if not include_surface and not include_secondary_particles:
            raise ValueError("Select the surface, secondary particles, or both")
        if include_surface and (not settings.use_mesh or not settings.has_cache_baked_mesh):
            raise ValueError("Surface export requires an enabled, baked liquid mesh stage")
        if include_secondary_particles and not settings.has_cache_baked_particles:
            raise ValueError("Secondary-particle export requires a baked particle stage")
        if frame_start > frame_end or frame_step <= 0:
            raise ValueError("frame_start must be <= frame_end and frame_step must be positive")
        frame_count = (frame_end - frame_start) // frame_step + 1
        if not 1 <= frame_count <= max_frames <= 2_000:
            raise ValueError("Export frame count exceeds the configured bound")
        if file_format == "ALEMBIC" and frame_step != 1:
            raise ValueError("Blender 5.1 Alembic export does not expose a frame-step option")
        if file_format == "ALEMBIC" and include_secondary_particles and not include_surface:
            raise ValueError("Blender 5.1 Alembic cannot exclude the selected domain mesh from particles-only export")
        if file_format == "ALEMBIC" and (forward_axis, up_axis) != ("NEGATIVE_Z", "Y"):
            raise ValueError("Blender 5.1 Alembic export uses fixed NEGATIVE_Z forward and Y up axes")
        resolved = os.path.abspath(bpy.path.abspath(filepath))
        extension = os.path.splitext(resolved)[1].lower()
        allowed = {"ALEMBIC": {".abc"}, "USD": {".usd", ".usda", ".usdc"}}[file_format]
        if extension not in allowed:
            raise ValueError(f"{file_format} filepath must use one of {sorted(allowed)}")
        parent = os.path.dirname(resolved)
        if not os.path.isdir(parent) or not os.access(parent, os.W_OK):
            raise ValueError("Export parent directory must exist and be writable")
        if os.path.exists(resolved) and not overwrite:
            raise ValueError("Export path exists; set overwrite=True to replace it")
        view_layer = _scene_view_layer(scene, [obj])
        original_frame = scene.frame_current
        original_subframe = scene.frame_subframe
        original_range = (scene.frame_start, scene.frame_end, scene.frame_step)
        descriptor, temporary = tempfile.mkstemp(prefix=".blendermcp-liquid-", suffix=extension, dir=parent)
        os.close(descriptor)
        os.unlink(temporary)
        try:
            _set_scene_frame_range(scene, frame_start, frame_end, frame_step)
            scene.frame_set(frame_start)
            with bpy.context.temp_override(scene=scene, view_layer=view_layer), preserve_mode_and_selection():
                for selected in list(bpy.context.selected_objects):
                    selected.select_set(False)
                obj.select_set(True)
                view_layer.objects.active = obj
                if file_format == "ALEMBIC":
                    scene_scale = float(scene.unit_settings.scale_length) or 1.0
                    target_scale = scene_scale if units == "SCENE" else _UNIT_METERS[units]
                    result = bpy.ops.wm.alembic_export(
                        filepath=temporary,
                        start=frame_start,
                        end=frame_end,
                        selected=True,
                        flatten=coordinate_space == "WORLD",
                        uvs=True,
                        normals=True,
                        vcolors=True,
                        global_scale=scene_scale / target_scale,
                        export_particles=include_secondary_particles,
                        export_custom_properties=include_velocity_attributes,
                        as_background_job=False,
                        evaluation_mode="RENDER",
                        init_scene_frame_range=False,
                    )
                else:
                    target_meters = (
                        float(scene.unit_settings.scale_length) or 1.0 if units == "SCENE" else _UNIT_METERS[units]
                    )
                    result = bpy.ops.wm.usd_export(
                        filepath=temporary,
                        selected_objects_only=True,
                        export_animation=frame_count > 1,
                        export_meshes=include_surface,
                        export_points=include_secondary_particles,
                        export_uvmaps=True,
                        export_mesh_colors=True,
                        export_normals=True,
                        export_materials=include_materials,
                        export_custom_properties=include_velocity_attributes,
                        evaluation_mode="RENDER",
                        convert_orientation=True,
                        export_global_forward_selection=forward_axis,
                        export_global_up_selection=up_axis,
                        convert_scene_units="CUSTOM",
                        meters_per_unit=target_meters,
                        merge_parent_xform=coordinate_space == "WORLD",
                    )
            if "FINISHED" not in result:
                raise RuntimeError(f"{file_format} exporter did not finish: {sorted(result)}")
            if not os.path.isfile(temporary) or os.path.getsize(temporary) <= 0:
                raise RuntimeError(f"{file_format} exporter did not write a nonempty file")
            os.replace(temporary, resolved)
            temporary = None
        finally:
            _set_scene_frame_range(scene, *original_range)
            scene.frame_set(original_frame, subframe=original_subframe)
            view_layer.update()
            if temporary and os.path.exists(temporary):
                with contextlib.suppress(OSError):
                    os.unlink(temporary)
        speed = _speed_attributes(obj) if include_velocity_attributes else {"available": False, "attributes": []}
        warnings = []
        if include_velocity_attributes and not speed["available"]:
            warnings.append(
                "No recognized evaluated velocity attribute was found; the export cannot preserve missing speed data."
            )
        if include_secondary_particles:
            warnings.append(
                "Secondary-particle support is exporter-dependent; verify point/particle roles in the destination DCC."
            )
        if file_format == "ALEMBIC" and include_materials:
            warnings.append("Alembic does not preserve Blender material node networks.")
        return {
            "changed_objects": [],
            "changed_resources": [resolved],
            "filepath": resolved,
            "bytes": os.path.getsize(resolved),
            "format": file_format,
            "domain": obj.name,
            "frame_range": {"start": frame_start, "end": frame_end, "step": frame_step, "count": frame_count},
            "components": {"surface": include_surface, "secondary_particles": include_secondary_particles},
            "coordinate_space": coordinate_space,
            "units": units,
            "axes": {"forward": forward_axis, "up": up_axis},
            "speed_vectors": speed,
            "source_cache_preserved": True,
            "warnings": warnings,
        }

    def analyze_liquid_performance(
        self,
        domain_object_name,
        modifier_name,
        frames=None,
        measure_replay_evaluation=False,
        timeout_seconds=30.0,
        max_dependency_objects=200,
        max_cache_entries=10_000,
    ):
        obj, modifier, settings = _get_domain(domain_object_name, modifier_name)
        dependencies = _dependency_objects(settings)
        if len(dependencies) > max_dependency_objects:
            raise ValueError(
                f"Domain has {len(dependencies)} dependencies, "
                f"exceeding max_dependency_objects={max_dependency_objects}"
            )
        resource = self.estimate_liquid_resources(obj.name, modifier.name)
        domain_bounds = _world_bounds(obj, evaluated=False)
        domain_volume = _bounds_volume(domain_bounds)
        records = []
        total_triangles = 0
        for dependency, role, collection in dependencies:
            cost = _mesh_cost(dependency) if dependency.type == "MESH" else None
            if cost:
                total_triangles += cost["triangles"]
            records.append(
                {
                    "object": dependency.name,
                    "role": role,
                    "collection": collection.name if collection else None,
                    "evaluated_geometry": cost,
                    "animated": bool(dependency.animation_data and dependency.animation_data.action),
                    "deforming": _has_deformation(dependency) if dependency.type == "MESH" else False,
                    "fluid": [
                        {
                            "modifier": fluid.name,
                            "type": fluid.fluid_type,
                            "subframes": getattr(
                                fluid.flow_settings if fluid.fluid_type == "FLOW" else fluid.effector_settings,
                                "subframes",
                                None,
                            ),
                        }
                        for fluid in dependency.modifiers
                        if fluid.type == "FLUID" and fluid.fluid_type in {"FLOW", "EFFECTOR"}
                    ],
                }
            )
        flow_bounds = [
            _world_bounds(item) for item, role, _collection in dependencies if role == "FLOW" and item.type == "MESH"
        ]
        if flow_bounds:
            minimum = [min(bounds["minimum"][axis] for bounds in flow_bounds) for axis in range(3)]
            maximum = [max(bounds["maximum"][axis] for bounds in flow_bounds) for axis in range(3)]
            occupied = {
                "minimum": minimum,
                "maximum": maximum,
                "dimensions": [maximum[axis] - minimum[axis] for axis in range(3)],
            }
            utilization = min(_bounds_volume(occupied) / domain_volume, 1.0) if domain_volume else None
        else:
            occupied = None
            utilization = 0.0 if domain_volume else None
        cache = _cache_directory_evidence(settings.cache_directory, max_entries=max_cache_entries)
        normalized_frames = sorted({int(frame) for frame in (frames or [])})
        if len(normalized_frames) > 12:
            raise ValueError("At most 12 unique performance frames may be evaluated")
        if measure_replay_evaluation and not normalized_frames:
            raise ValueError("Measured evaluation requires at least one explicit frame")
        if normalized_frames and not measure_replay_evaluation:
            raise ValueError("frames are accepted only when measure_replay_evaluation=True")
        if measure_replay_evaluation and settings.cache_type != "REPLAY" and not settings.has_cache_baked_data:
            raise ValueError("Measured evaluation requires REPLAY mode or existing baked data")
        scene = bpy.context.scene
        original_frame = scene.frame_current
        original_subframe = scene.frame_subframe
        timings = []
        positions_by_frame = []
        started = time.monotonic()
        try:
            for frame in normalized_frames:
                if time.monotonic() - started > timeout_seconds:
                    raise TimeoutError("Liquid performance sampling exceeded timeout_seconds")
                before = time.perf_counter()
                scene.frame_set(frame)
                bpy.context.view_layer.update()
                output = _evaluated_output(obj)
                positions_by_frame.append(
                    {
                        "frame": frame,
                        "positions": {
                            dependency.name: [float(value) for value in dependency.matrix_world.translation]
                            for dependency, _role, _collection in dependencies
                        },
                    }
                )
                timings.append({"frame": frame, "evaluation_seconds": time.perf_counter() - before, "output": output})
        finally:
            scene.frame_set(original_frame, subframe=original_subframe)
        movement = []
        cell_size = float(resource["estimated_grid"]["cell_size"])
        for dependency, role, _collection in dependencies:
            samples = []
            for previous, current in pairwise(positions_by_frame):
                frame_delta = current["frame"] - previous["frame"]
                if frame_delta <= 0:
                    continue
                start = mathutils.Vector(previous["positions"][dependency.name])
                end = mathutils.Vector(current["positions"][dependency.name])
                distance_per_frame = float((end - start).length) / frame_delta
                samples.append(
                    {
                        "frames": [previous["frame"], current["frame"]],
                        "world_distance_per_frame": distance_per_frame,
                        "cells_per_frame": distance_per_frame / cell_size if cell_size else None,
                    }
                )
            if samples:
                fluid = _fluid_modifier(dependency, role) if role in {"FLOW", "EFFECTOR"} else None
                fluid_settings = None
                if fluid is not None:
                    fluid_settings = fluid.flow_settings if role == "FLOW" else fluid.effector_settings
                subframes = int(getattr(fluid_settings, "subframes", 0))
                maximum = max(sample["cells_per_frame"] for sample in samples)
                movement.append(
                    {
                        "object": dependency.name,
                        "role": role,
                        "subframes": subframes,
                        "maximum_cells_per_frame": maximum,
                        "maximum_cells_per_subframe": maximum / (subframes + 1),
                        "samples": samples,
                    }
                )
        findings = []
        estimated_cells = resource["estimated_grid"]["base_cell_count"]
        if utilization is not None and utilization < 0.1:
            findings.append({"severity": "WARNING", "code": "LOW_DOMAIN_UTILIZATION", "evidence": utilization})
        if settings.mesh_scale > 2:
            findings.append({"severity": "WARNING", "code": "HIGH_MESH_SCALE", "evidence": settings.mesh_scale})
        secondary = [
            name
            for name in ("use_spray_particles", "use_foam_particles", "use_bubble_particles", "use_tracer_particles")
            if getattr(settings, name, False)
        ]
        if len(secondary) >= 3:
            findings.append({"severity": "INFO", "code": "MANY_SECONDARY_PARTICLE_TYPES", "evidence": secondary})
        deforming = [record["object"] for record in records if record["deforming"]]
        if deforming:
            findings.append({"severity": "WARNING", "code": "DEFORMING_DEPENDENCIES", "evidence": deforming})
        fast = [record for record in movement if record["maximum_cells_per_subframe"] > settings.cfl_condition]
        if fast:
            findings.append({"severity": "WARNING", "code": "SOURCE_SPEED_EXCEEDS_CFL", "evidence": fast})
        if cache["scan_truncated"]:
            findings.append({"severity": "WARNING", "code": "CACHE_SCAN_TRUNCATED", "evidence": max_cache_entries})
        return {
            "changed_objects": [obj.name] if measure_replay_evaluation else [],
            "domain": obj.name,
            "modifier": modifier.name,
            "estimated_grid": resource["estimated_grid"],
            "relative_cost_index": resource["relative_cost_index"],
            "domain_bounds": domain_bounds,
            "flow_bounds_union": occupied,
            "domain_volume_utilization": utilization,
            "dependencies": records,
            "dependency_evaluated_triangles": total_triangles,
            "solver": _read_fields(
                settings,
                {
                    "resolution_max",
                    "timesteps_min",
                    "timesteps_max",
                    "cfl_condition",
                    "particle_min",
                    "particle_max",
                    "mesh_scale",
                    "use_viscosity",
                    "use_diffusion",
                    "use_guide",
                },
            ),
            "secondary_particle_types": secondary,
            "cache": {"state": _cache_state(settings), "directory": cache},
            "measured_evaluation": {
                "performed": measure_replay_evaluation,
                "frames": timings,
                "dependency_movement": movement,
                "cache_effect": "May populate REPLAY data" if measure_replay_evaluation else "None",
            },
            "findings": findings,
            "limits": {
                "dependency_objects": max_dependency_objects,
                "cache_entries": max_cache_entries,
                "measured_frames": 12,
            },
            "claims": {
                "estimated_cells": estimated_cells,
                "exact_peak_memory": None,
                "exact_remaining_time": None,
                "timings": "Measured only for this bounded run and hardware.",
            },
        }
