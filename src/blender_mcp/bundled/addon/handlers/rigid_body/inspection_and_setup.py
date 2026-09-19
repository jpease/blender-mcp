# pyright: reportGeneralTypeIssues=false
# Blender RNA objects are dynamically generated; this module runs only inside Blender.
"""Blender-main-thread handlers for rigid-body inspection, setup, and validation."""

from __future__ import annotations

import contextlib
import math
import uuid

from collections import Counter
from itertools import combinations

import bmesh
import bpy
import mathutils

from ...helpers import paginate, preserve_mode_and_selection, set_active, sync_from_editmode
from ..simulation_cache import point_cache_info

_BODY_FIELDS = {
    "type",
    "enabled",
    "kinematic",
    "collision_shape",
    "mesh_source",
    "use_deform",
    "mass",
    "use_margin",
    "collision_margin",
    "friction",
    "restitution",
    "linear_damping",
    "angular_damping",
    "use_deactivation",
    "use_start_deactivated",
    "deactivate_linear_velocity",
    "deactivate_angular_velocity",
}
_WORLD_FIELDS = {"enabled", "time_scale", "substeps_per_frame", "solver_iterations", "use_split_impulse"}
_EFFECTOR_FIELDS = {
    "all",
    "gravity",
    "force",
    "vortex",
    "magnetic",
    "wind",
    "curve_guide",
    "texture",
    "harmonic",
    "charge",
    "lennardjones",
    "turbulence",
    "drag",
    "boid",
    "smokeflow",
}
_CONSTRAINT_COMMON_FIELDS = {
    "enabled",
    "disable_collisions",
    "use_breaking",
    "breaking_threshold",
    "use_override_solver_iterations",
    "solver_iterations",
}
_LAYER_PROFILES = {"ENVIRONMENT": {1}, "HERO": {2}, "DEBRIS": {3}, "RAGDOLL": {4}}
_SUPPORTED_BODY_TYPES = {"MESH"}
_TOPOLOGY_MODIFIERS = {
    "ARRAY",
    "BEVEL",
    "BOOLEAN",
    "BUILD",
    "DECIMATE",
    "EDGE_SPLIT",
    "MASK",
    "MIRROR",
    "NODES",
    "REMESH",
    "SCREW",
    "SKIN",
    "SOLIDIFY",
    "SUBSURF",
    "TRIANGULATE",
    "WELD",
}


def _serialize(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if hasattr(value, "name"):
        return value.name
    if hasattr(value, "to_list"):
        return value.to_list()
    if isinstance(value, (list, tuple)) or hasattr(value, "__iter__"):
        with contextlib.suppress(TypeError):
            return [_serialize(item) for item in value]
    return str(value)


def _bvh_class():
    from mathutils.bvhtree import BVHTree

    return BVHTree


def _read_fields(owner, fields):
    return {name: _serialize(getattr(owner, name)) for name in sorted(fields) if hasattr(owner, name)}


def _scene(name):
    scene = bpy.data.scenes.get(name)
    if scene is None:
        raise ValueError(f"Scene not found: {name}")
    return scene


def _object(name, types=None):
    obj = bpy.data.objects.get(name)
    if obj is None:
        raise ValueError(f"Object not found: {name}")
    if types and obj.type not in types:
        raise ValueError(f"Object '{name}' must be one of {sorted(types)} (type={obj.type})")
    return obj


def _collection_in_scene(collection, scene):
    pending = [scene.collection]
    while pending:
        current = pending.pop()
        if current == collection:
            return True
        pending.extend(current.children)
    return False


def _ensure_collection(scene, name):
    if not name:
        raise ValueError("Collection names must be non-empty")
    collection = bpy.data.collections.get(name)
    created = collection is None
    if collection is None:
        collection = bpy.data.collections.new(name)
        scene.collection.children.link(collection)
    elif not _collection_in_scene(collection, scene):
        raise ValueError(
            f"Collection '{name}' exists but is not linked to scene '{scene.name}'; use a collision-safe name"
        )
    return collection, created


def _preflight_collection_name(scene, name):
    collection = bpy.data.collections.get(name)
    if collection is not None and not _collection_in_scene(collection, scene):
        raise ValueError(
            f"Collection '{name}' exists but is not linked to scene '{scene.name}'; use a collision-safe name"
        )


def _view_layer_for(scene, obj=None):
    for layer in scene.view_layers:
        layer.update()
        if obj is None or obj.name in layer.objects:
            return layer
    target = f" containing '{obj.name}'" if obj else ""
    raise ValueError(f"Scene '{scene.name}' has no usable view layer{target}")


def _scene_depsgraph(scene):
    view_layer = _view_layer_for(scene)
    with bpy.context.temp_override(scene=scene, view_layer=view_layer):
        return bpy.context.evaluated_depsgraph_get()


def _depsgraph_for_object(obj):
    scenes = [scene for scene in bpy.data.scenes if obj.name in scene.objects]
    if not scenes:
        raise ValueError(f"Object '{obj.name}' is not linked to a scene")
    scene = bpy.context.scene if bpy.context.scene in scenes else scenes[0]
    return _scene_depsgraph(scene)


def _require_finished(result, operation):
    if "FINISHED" not in result:
        raise RuntimeError(f"{operation} did not finish: {sorted(result)}")


def _run_world_operator(scene, operator):
    view_layer = _view_layer_for(scene)
    with bpy.context.temp_override(scene=scene, view_layer=view_layer):
        result = operator()
    _require_finished(result, operator.idname())


def _run_object_operator(scene, obj, operator, **kwargs):
    view_layer = _view_layer_for(scene, obj)
    with bpy.context.temp_override(scene=scene, view_layer=view_layer), preserve_mode_and_selection():
        set_active(obj)
        result = operator(**kwargs)
    _require_finished(result, operator.idname())


def _ensure_world(scene):
    if scene.rigidbody_world is None:
        _run_world_operator(scene, bpy.ops.rigidbody.world_add)
    if scene.rigidbody_world is None:
        raise RuntimeError(f"Blender reported FINISHED but did not create a rigid-body world for '{scene.name}'")
    return scene.rigidbody_world


def _cache_info(cache):
    return point_cache_info(cache)


def _free_world_bake(scene, world):
    view_layer = _view_layer_for(scene)
    override = {"scene": scene, "view_layer": view_layer, "point_cache": world.point_cache}
    with bpy.context.temp_override(**override):
        result = bpy.ops.ptcache.free_bake()
    _require_finished(result, "bpy.ops.ptcache.free_bake")
    if world.point_cache.is_baked:
        raise RuntimeError("Rigid-body point cache remains baked after free_bake reported FINISHED")


def _prepare_cache_mutation(scene, world, confirm_delete_baked_cache):
    cache = world.point_cache
    if cache.is_baking:
        raise ValueError("Rigid-body point cache is currently baking")
    if not cache.is_baked:
        return False
    if not confirm_delete_baked_cache:
        raise ValueError("Cannot change a baked rigid-body world; set confirm_delete_baked_cache=True to free it")
    _free_world_bake(scene, world)
    return True


def _validate_rna_properties(properties, patch, allowed):
    unknown = set(patch) - allowed
    if unknown:
        raise ValueError(f"Unsupported properties: {sorted(unknown)}")
    prepared = {}
    for name, value in patch.items():
        prop = properties.get(name)
        if prop is None or prop.is_readonly:
            raise ValueError(f"Property '{name}' is unavailable or read-only in this Blender build")
        if prop.type == "ENUM":
            choices = {item.identifier for item in prop.enum_items}
            if not isinstance(value, str) or value not in choices:
                raise ValueError(f"{name} must be one of {sorted(choices)}")
        elif prop.type == "BOOLEAN":
            if not isinstance(value, bool):
                raise ValueError(f"{name} must be a boolean")
        elif prop.type == "STRING":
            if not isinstance(value, str):
                raise ValueError(f"{name} must be a string")
        elif prop.type == "INT":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{name} must be an integer")
            if value < prop.hard_min or value > prop.hard_max:
                raise ValueError(f"{name}={value} is outside Blender's RNA range [{prop.hard_min}, {prop.hard_max}]")
        elif prop.type == "FLOAT":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{name} must be a finite number")
            if value < prop.hard_min or value > prop.hard_max:
                raise ValueError(f"{name}={value} is outside Blender's RNA range [{prop.hard_min}, {prop.hard_max}]")
        prepared[name] = value
    return prepared


def _validate_rna_patch(owner, patch, allowed):
    return _validate_rna_properties(owner.bl_rna.properties, patch, allowed)


def _apply_patch(owner, patch, allowed):
    prepared = _validate_rna_patch(owner, patch, allowed)
    changes = {}
    for name, value in prepared.items():
        old = _serialize(getattr(owner, name))
        if old == _serialize(value):
            continue
        setattr(owner, name, value)
        changes[name] = {"old": old, "new": _serialize(getattr(owner, name))}
    return changes


def _restore_fields(owner, snapshot):
    for name, value in snapshot.items():
        with contextlib.suppress(Exception):
            setattr(owner, name, value)


def _set_cache_range(cache, patch):
    start = int(patch.get("frame_start", cache.frame_start))
    end = int(patch.get("frame_end", cache.frame_end))
    step = int(patch.get("frame_step", cache.frame_step))
    if start > end:
        raise ValueError("Rigid-body cache frame_start must not exceed frame_end")
    if not 1 <= step <= 1000:
        raise ValueError("Rigid-body cache frame_step must be in [1, 1000]")
    if start > cache.frame_end:
        cache.frame_end = end
        cache.frame_start = start
    else:
        cache.frame_start = start
        cache.frame_end = end
    cache.frame_step = step
    if (cache.frame_start, cache.frame_end, cache.frame_step) != (start, end, step):
        raise RuntimeError("Blender did not retain the requested rigid-body cache range")


def _native_transform(obj):
    local_location, local_rotation, local_scale = obj.matrix_basis.decompose()
    world_location, world_rotation, world_scale = obj.matrix_world.decompose()
    return {
        "local": {
            "coordinate_space": "PARENT_LOCAL",
            "location": list(local_location),
            "rotation_quaternion_wxyz": list(local_rotation),
            "scale": list(local_scale),
            "matrix": [list(row) for row in obj.matrix_basis],
        },
        "world": {
            "coordinate_space": "WORLD",
            "location": list(world_location),
            "rotation_quaternion_wxyz": list(world_rotation),
            "scale": list(world_scale),
            "matrix": [list(row) for row in obj.matrix_world],
        },
        "rotation_mode": obj.rotation_mode,
    }


def _animation_info(obj):
    animation = obj.animation_data
    action = animation.action if animation else None
    paths = sorted({curve.data_path for curve in _action_fcurves(action)}) if action else []
    return {
        "animated": bool(action or (animation and animation.drivers)),
        "action": action.name if action else None,
        "fcurve_paths": paths[:100],
        "fcurve_paths_truncated": len(paths) > 100,
        "drivers": len(animation.drivers) if animation else 0,
    }


def _action_fcurves(action):
    """Return legacy or layered action F-curves on Blender 5.1+."""
    if action is None:
        return []
    legacy = getattr(action, "fcurves", None)
    if legacy is not None:
        return list(legacy)
    return [
        curve
        for layer in action.layers
        for strip in layer.strips
        for channelbag in getattr(strip, "channelbags", ())
        for curve in channelbag.fcurves
    ]


def _clear_action_fcurves(action):
    """Remove all F-curves from a legacy or layered action."""
    legacy = getattr(action, "fcurves", None)
    if legacy is not None:
        for curve in list(legacy):
            legacy.remove(curve)
        return
    for layer in action.layers:
        for strip in layer.strips:
            for channelbag in getattr(strip, "channelbags", ()):
                for curve in list(channelbag.fcurves):
                    channelbag.fcurves.remove(curve)


def _bounds(points):
    if not points:
        return None
    minimum = [min(point[index] for point in points) for index in range(3)]
    maximum = [max(point[index] for point in points) for index in range(3)]
    return {
        "min": minimum,
        "max": maximum,
        "dimensions": [maximum[index] - minimum[index] for index in range(3)],
        "center": [(maximum[index] + minimum[index]) * 0.5 for index in range(3)],
    }


def _evaluated_geometry(obj):
    if obj.type != "MESH":
        return {"vertices": None, "edges": None, "faces": None, "triangles": None, "bounds_world": None}
    depsgraph = _depsgraph_for_object(obj)
    evaluated = obj.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh()
    try:
        mesh.calc_loop_triangles()
        world_points = [evaluated.matrix_world @ vertex.co for vertex in mesh.vertices]
        return {
            "vertices": len(mesh.vertices),
            "edges": len(mesh.edges),
            "faces": len(mesh.polygons),
            "triangles": len(mesh.loop_triangles),
            "bounds_world": _bounds(world_points),
        }
    finally:
        evaluated.to_mesh_clear()


def _body_info(obj):
    body = obj.rigid_body
    if body is None:
        return None
    return {
        **_read_fields(body, _BODY_FIELDS),
        "collision_collections": [i + 1 for i, flag in enumerate(body.collision_collections) if flag],
    }


def _object_info(obj):
    if obj.type == "MESH":
        sync_from_editmode(obj)
    evaluated = _evaluated_geometry(obj)
    scale = [float(value) for value in obj.scale]
    topology_modifiers = [modifier.name for modifier in obj.modifiers if modifier.type in _TOPOLOGY_MODIFIERS]
    ambiguity = []
    if any(abs(abs(value) - 1.0) > 1e-5 for value in scale):
        ambiguity.append("object scale is not applied")
    if topology_modifiers:
        ambiguity.append(f"topology-changing modifiers are live: {topology_modifiers}")
    return {
        "object": obj.name,
        "object_type": obj.type,
        "data_type": type(obj.data).__name__ if obj.data else None,
        "rigid_body": _body_info(obj),
        "transform": _native_transform(obj),
        "dimensions_world_aligned": list(obj.dimensions),
        "parent": obj.parent.name if obj.parent else None,
        "collections": sorted(collection.name for collection in obj.users_collection),
        "animation": _animation_info(obj),
        "mesh_data_shared": bool(obj.data and obj.data.users > 1),
        "base_geometry": (
            {"vertices": len(obj.data.vertices), "edges": len(obj.data.edges), "faces": len(obj.data.polygons)}
            if obj.type == "MESH"
            else None
        ),
        "evaluated_geometry": evaluated,
        "collision_representation_warnings": ambiguity,
    }


def _constraint_flat_info(constraint):
    fields = _CONSTRAINT_COMMON_FIELDS | {
        "type",
        "spring_type",
        "use_limit_lin_x",
        "use_limit_lin_y",
        "use_limit_lin_z",
        "use_limit_ang_x",
        "use_limit_ang_y",
        "use_limit_ang_z",
        "use_spring_x",
        "use_spring_y",
        "use_spring_z",
        "use_spring_ang_x",
        "use_spring_ang_y",
        "use_spring_ang_z",
        "limit_lin_x_lower",
        "limit_lin_x_upper",
        "limit_lin_y_lower",
        "limit_lin_y_upper",
        "limit_lin_z_lower",
        "limit_lin_z_upper",
        "limit_ang_x_lower",
        "limit_ang_x_upper",
        "limit_ang_y_lower",
        "limit_ang_y_upper",
        "limit_ang_z_lower",
        "limit_ang_z_upper",
        "spring_stiffness_x",
        "spring_stiffness_y",
        "spring_stiffness_z",
        "spring_stiffness_ang_x",
        "spring_stiffness_ang_y",
        "spring_stiffness_ang_z",
        "spring_damping_x",
        "spring_damping_y",
        "spring_damping_z",
        "spring_damping_ang_x",
        "spring_damping_ang_y",
        "spring_damping_ang_z",
        "motor_lin_target_velocity",
        "motor_lin_max_impulse",
        "motor_ang_target_velocity",
        "motor_ang_max_impulse",
        "use_motor_lin",
        "use_motor_ang",
    }
    return {
        **_read_fields(constraint, fields),
        "object1": constraint.object1.name if constraint.object1 else None,
        "object2": constraint.object2.name if constraint.object2 else None,
    }


def _constraint_info(obj, world):
    constraint = obj.rigid_body_constraint
    endpoints = [constraint.object1, constraint.object2] if constraint else []
    issues = []
    if constraint:
        if any(endpoint is None for endpoint in endpoints):
            issues.append("missing endpoint")
        if endpoints[0] is not None and endpoints[0] == endpoints[1]:
            issues.append("same-object endpoints")
        for endpoint in endpoints:
            if endpoint is not None and endpoint.rigid_body is None:
                issues.append(f"endpoint '{endpoint.name}' has no rigid body")
    return {
        "object": obj.name,
        "transform": _native_transform(obj),
        "constraint": _constraint_flat_info(constraint) if constraint else None,
        "collections": sorted(collection.name for collection in obj.users_collection),
        "in_world_constraint_collection": bool(world and world.constraints and obj.name in world.constraints.objects),
        "issues": issues,
    }


def _validate_object_batch(scene, names, require_body=False):
    if len(set(names)) != len(names):
        raise ValueError("Object names must be unique")
    objects = []
    for name in names:
        obj = _object(name)
        if obj.name not in scene.objects:
            raise ValueError(f"Object '{name}' is not linked to scene '{scene.name}'")
        if obj.library is not None or not obj.is_editable:
            raise ValueError(f"Object '{name}' is not editable")
        if require_body and obj.rigid_body is None:
            raise ValueError(f"Object '{name}' has no rigid-body settings")
        if any(not math.isfinite(float(value)) for row in obj.matrix_world for value in row):
            raise ValueError(f"Object '{name}' has a non-finite world transform")
        objects.append(obj)
    return objects


def _validate_body_semantics(body, patch):
    prospective_shape = patch.get("collision_shape", body.collision_shape)
    prospective_deform = patch.get("use_deform", body.use_deform)
    if prospective_deform and prospective_shape != "MESH":
        raise ValueError("use_deform=True requires collision_shape='MESH'")


def _body_snapshot(body):
    return {name: getattr(body, name) for name in _BODY_FIELDS if hasattr(body, name)} | {
        "collision_collections": tuple(body.collision_collections)
    }


def _constraint_axis_fields(axis_name, settings, spring):
    kind, axis = axis_name.split("_", 1)
    angle = kind == "angular"
    affix = f"ang_{axis}" if angle else f"lin_{axis}"
    patch = {}
    if "use_limit" in settings:
        patch[f"use_limit_{affix}"] = settings["use_limit"]
    if "lower" in settings:
        patch[f"limit_{affix}_lower"] = settings["lower"]
    if "upper" in settings:
        patch[f"limit_{affix}_upper"] = settings["upper"]
    if spring:
        spring_affix = f"ang_{axis}" if angle else axis
        if "use_spring" in settings:
            patch[f"use_spring_{spring_affix}"] = settings["use_spring"]
        if "stiffness" in settings:
            patch[f"spring_stiffness_{spring_affix}"] = settings["stiffness"]
        if "damping" in settings:
            patch[f"spring_damping_{spring_affix}"] = settings["damping"]
    return patch


def _flatten_constraint_config(constraint, configuration):
    constraint_type = configuration["type"]
    if constraint.type != constraint_type:
        raise ValueError(
            f"Configuration type {constraint_type} does not match existing constraint type {constraint.type}"
        )
    patch = {name: value for name, value in configuration.items() if name in _CONSTRAINT_COMMON_FIELDS}
    if "spring_type" in configuration:
        patch["spring_type"] = configuration["spring_type"]
    for axis_name in ("linear_x", "linear_y", "linear_z", "angular_x", "angular_y", "angular_z"):
        settings = configuration.get(axis_name)
        if settings:
            patch.update(_constraint_axis_fields(axis_name, settings, constraint_type == "GENERIC_SPRING"))
    for public_name, prefix in (("linear_motor", "lin"), ("angular_motor", "ang")):
        settings = configuration.get(public_name)
        if not settings:
            continue
        if "enabled" in settings:
            patch[f"use_motor_{prefix}"] = settings["enabled"]
        if "target_velocity" in settings:
            patch[f"motor_{prefix}_target_velocity"] = settings["target_velocity"]
        if "max_impulse" in settings:
            patch[f"motor_{prefix}_max_impulse"] = settings["max_impulse"]
    for affix in ("lin_x", "lin_y", "lin_z", "ang_x", "ang_y", "ang_z"):
        lower_name = f"limit_{affix}_lower"
        upper_name = f"limit_{affix}_upper"
        lower = patch.get(lower_name, getattr(constraint, lower_name, 0.0))
        upper = patch.get(upper_name, getattr(constraint, upper_name, 0.0))
        if lower > upper:
            raise ValueError(f"{lower_name} must not exceed {upper_name}")
    return _validate_rna_patch(
        constraint,
        patch,
        {prop.identifier for prop in constraint.bl_rna.properties if prop.identifier != "rna_type"},
    )


def _active_degrees_of_freedom(constraint_type):
    return {
        "FIXED": [],
        "POINT": ["ANGULAR_X", "ANGULAR_Y", "ANGULAR_Z"],
        "HINGE": ["ANGULAR_Z"],
        "SLIDER": ["LINEAR_X"],
        "PISTON": ["LINEAR_X", "ANGULAR_X"],
        "GENERIC": ["CONFIGURED_LINEAR_AND_ANGULAR_AXES"],
        "GENERIC_SPRING": ["CONFIGURED_LINEAR_AND_ANGULAR_AXES"],
        "MOTOR": ["ANGULAR_X", "LINEAR_X"],
    }[constraint_type]


def _add_rigid_body(scene, obj, body_type):
    _run_object_operator(scene, obj, bpy.ops.rigidbody.object_add, type=body_type)
    if obj.rigid_body is None:
        raise RuntimeError(f"Blender did not attach rigid-body settings to '{obj.name}'")


def _remove_rigid_body(scene, obj):
    if obj.rigid_body is not None:
        _run_object_operator(scene, obj, bpy.ops.rigidbody.object_remove)


def _world_member_objects(scene, world):
    return sorted(
        [
            obj
            for obj in (world.collection.objects if world and world.collection else ())
            if obj.name in scene.objects and obj.rigid_body is not None
        ],
        key=lambda item: item.name,
    )


def _world_constraint_objects(scene, world):
    return sorted(
        [
            obj
            for obj in (world.constraints.objects if world and world.constraints else ())
            if obj.name in scene.objects and obj.rigid_body_constraint is not None
        ],
        key=lambda item: item.name,
    )


def _evaluated_mesh_payload(obj, target_space_object=None):
    depsgraph = _depsgraph_for_object(obj)
    evaluated = obj.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh()
    try:
        matrix = mathutils.Matrix.Identity(4)
        if target_space_object is not None:
            matrix = target_space_object.matrix_world.inverted() @ evaluated.matrix_world
        vertices = [matrix @ vertex.co for vertex in mesh.vertices]
        faces = [tuple(polygon.vertices) for polygon in mesh.polygons]
        return vertices, faces
    finally:
        evaluated.to_mesh_clear()


def _box_mesh(name, bounds):
    minimum, maximum = bounds["min"], bounds["max"]
    vertices = [
        (x, y, z)
        for x, y, z in (
            (minimum[0], minimum[1], minimum[2]),
            (maximum[0], minimum[1], minimum[2]),
            (maximum[0], maximum[1], minimum[2]),
            (minimum[0], maximum[1], minimum[2]),
            (minimum[0], minimum[1], maximum[2]),
            (maximum[0], minimum[1], maximum[2]),
            (maximum[0], maximum[1], maximum[2]),
            (minimum[0], maximum[1], maximum[2]),
        )
    ]
    faces = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(vertices, [], faces)
    mesh.update()
    return mesh


def _primitive_proxy_mesh(name, approximation, bounds):
    center = mathutils.Vector(bounds["center"])
    dimensions = mathutils.Vector(bounds["dimensions"])
    bm = bmesh.new()
    try:
        if approximation == "SPHERE":
            radius = max(dimensions) * 0.5
            bmesh.ops.create_icosphere(
                bm,
                subdivisions=2,
                radius=radius,
                matrix=mathutils.Matrix.Translation(tuple(center)),
            )
        else:
            axis_index = max(range(3), key=lambda index: dimensions[index])
            radius = max(dimensions[(axis_index + 1) % 3], dimensions[(axis_index + 2) % 3]) * 0.5
            depth = dimensions[axis_index]
            if approximation == "CAPSULE":
                cylinder_depth = max(0.0, depth - 2.0 * radius)
                bmesh.ops.create_icosphere(bm, subdivisions=2, radius=radius)
                for vertex in list(bm.verts):
                    if vertex.co.z > 0:
                        vertex.co.z += cylinder_depth * 0.5
                    elif vertex.co.z < 0:
                        vertex.co.z -= cylinder_depth * 0.5
            else:
                bmesh.ops.create_cone(bm, cap_ends=True, segments=16, radius1=radius, radius2=radius, depth=depth)
            if axis_index == 0:
                rotation = mathutils.Matrix.Rotation(math.pi / 2.0, 4, "Y")
            elif axis_index == 1:
                rotation = mathutils.Matrix.Rotation(math.pi / 2.0, 4, "X")
            else:
                rotation = mathutils.Matrix.Identity(4)
            bmesh.ops.transform(
                bm,
                matrix=mathutils.Matrix.Translation(tuple(center)) @ rotation,
                verts=list(bm.verts),
            )
        mesh = bpy.data.meshes.new(name)
        bm.to_mesh(mesh)
        mesh.update()
        return mesh
    finally:
        bm.free()


def _convex_hull_mesh(name, vertices):
    bm = bmesh.new()
    try:
        verts = [bm.verts.new(vertex) for vertex in vertices]
        result = bmesh.ops.convex_hull(bm, input=verts, use_existing_faces=False)
        interior = list(
            {id(item): item for item in result.get("geom_interior", []) + result.get("geom_unused", [])}.values()
        )
        if interior:
            bmesh.ops.delete(bm, geom=interior, context="VERTS")
        mesh = bpy.data.meshes.new(name)
        bm.to_mesh(mesh)
        mesh.update()
        return mesh
    finally:
        bm.free()


def _mesh_volume(obj):
    depsgraph = _depsgraph_for_object(obj)
    evaluated = obj.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh()
    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        non_manifold = sum(not edge.is_manifold for edge in list(bm.edges))
        if non_manifold:
            raise ValueError(f"Mesh '{obj.name}' is not closed/manifold ({non_manifold} non-manifold edges)")
        local_volume = abs(float(bm.calc_volume(signed=True)))
        determinant = float(evaluated.matrix_world.to_3x3().determinant())
        volume = local_volume * abs(determinant)
        if volume <= 1e-12:
            raise ValueError(f"Mesh '{obj.name}' has near-zero evaluated world volume")
        return volume, determinant
    finally:
        bm.free()
        evaluated.to_mesh_clear()


def _aabb_overlap(first, second):
    return all(
        first["min"][axis] < second["max"][axis] and second["min"][axis] < first["max"][axis] for axis in range(3)
    )


def _world_bvh(obj, depsgraph):
    evaluated = obj.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh()
    try:
        vertices = [evaluated.matrix_world @ vertex.co for vertex in mesh.vertices]
        polygons = [tuple(poly.vertices) for poly in mesh.polygons]
        return _bvh_class().FromPolygons(vertices, polygons, all_triangles=False, epsilon=0.0)
    finally:
        evaluated.to_mesh_clear()


class RigidBodyInspectionAndSetupHandlers:
    """Provide rigid-body inspection, authoring, and validation handlers."""

    def get_rigid_body_scene_info(
        self,
        scene_name,
        member_limit=100,
        member_offset=0,
        constraint_limit=100,
        constraint_offset=0,
    ):
        scene = _scene(scene_name)
        world = scene.rigidbody_world
        if world is None:
            return {
                "scene": scene.name,
                "frame_observed": scene.frame_current,
                "use_gravity": scene.use_gravity,
                "gravity": list(scene.gravity),
                "world": None,
                "members": [],
                "member_page": {"total": 0, "offset": 0, "returned_count": 0, "truncated": False, "next_offset": None},
                "constraints": [],
                "constraint_page": {
                    "total": 0,
                    "offset": 0,
                    "returned_count": 0,
                    "truncated": False,
                    "next_offset": None,
                },
                "objects_with_rigid_bodies_outside_world_collection": sorted(
                    obj.name for obj in scene.objects if obj.rigid_body is not None
                ),
                "constraints_outside_world_collection": sorted(
                    obj.name for obj in scene.objects if obj.rigid_body_constraint is not None
                ),
                "non_rigid_objects_in_world_collection": [],
                "non_constraint_objects_in_constraint_collection": [],
            }
        members = _world_member_objects(scene, world)
        constraints = _world_constraint_objects(scene, world)
        m_start, m_end, m_truncated, m_next = paginate(len(members), member_offset, member_limit, 500)
        c_start, c_end, c_truncated, c_next = paginate(len(constraints), constraint_offset, constraint_limit, 500)
        member_names = {obj.name for obj in members}
        constraint_names = {obj.name for obj in constraints}
        bodies = [obj for obj in scene.objects if obj.rigid_body is not None]
        constraint_components = [obj for obj in scene.objects if obj.rigid_body_constraint is not None]
        non_body_members = sorted(
            obj.name for obj in (world.collection.objects if world.collection else ()) if obj.rigid_body is None
        )
        non_constraint_members = sorted(
            obj.name
            for obj in (world.constraints.objects if world.constraints else ())
            if obj.rigid_body_constraint is None
        )
        type_counts = Counter(obj.rigid_body.type for obj in members if obj.rigid_body)
        shape_counts = Counter(obj.rigid_body.collision_shape for obj in members if obj.rigid_body)
        enabled_counts = Counter(
            "enabled" if obj.rigid_body.enabled else "disabled" for obj in members if obj.rigid_body
        )
        return {
            "scene": scene.name,
            "frame_observed": scene.frame_current,
            "use_gravity": scene.use_gravity,
            "gravity": list(scene.gravity),
            "world": {
                **_read_fields(world, _WORLD_FIELDS),
                "body_collection": world.collection.name if world.collection else None,
                "constraint_collection": world.constraints.name if world.constraints else None,
                "effector_weights": {
                    **_read_fields(world.effector_weights, _EFFECTOR_FIELDS),
                    "collection": world.effector_weights.collection.name if world.effector_weights.collection else None,
                },
                "point_cache": _cache_info(world.point_cache),
            },
            "member_counts": {
                "total": len(members),
                "by_type": dict(sorted(type_counts.items())),
                "by_collision_shape": dict(sorted(shape_counts.items())),
                "by_enabled_state": dict(sorted(enabled_counts.items())),
            },
            "members": [
                {
                    "object": obj.name,
                    "type": obj.rigid_body.type,
                    "enabled": obj.rigid_body.enabled,
                    "collision_shape": obj.rigid_body.collision_shape,
                }
                for obj in members[m_start:m_end]
            ],
            "member_page": {
                "total": len(members),
                "offset": m_start,
                "returned_count": m_end - m_start,
                "truncated": m_truncated,
                "next_offset": m_next,
            },
            "constraints": [
                {
                    "object": obj.name,
                    "type": obj.rigid_body_constraint.type,
                    "enabled": obj.rigid_body_constraint.enabled,
                }
                for obj in constraints[c_start:c_end]
            ],
            "constraint_page": {
                "total": len(constraints),
                "offset": c_start,
                "returned_count": c_end - c_start,
                "truncated": c_truncated,
                "next_offset": c_next,
            },
            "objects_with_rigid_bodies_outside_world_collection": sorted(
                obj.name for obj in bodies if obj.name not in member_names
            ),
            "constraints_outside_world_collection": sorted(
                obj.name for obj in constraint_components if obj.name not in constraint_names
            ),
            "non_rigid_objects_in_world_collection": non_body_members,
            "non_constraint_objects_in_constraint_collection": non_constraint_members,
        }

    def get_rigid_body_object_info(self, object_names):
        if not object_names or len(object_names) > 100:
            raise ValueError("object_names must contain 1-100 names")
        if len(set(object_names)) != len(object_names):
            raise ValueError("object_names must be unique")
        return {"objects": [_object_info(_object(name)) for name in object_names]}

    def get_rigid_body_constraint_info(self, scene_name, constraint_object_names=None, limit=100, offset=0):
        scene = _scene(scene_name)
        world = scene.rigidbody_world
        if constraint_object_names is None:
            candidates = sorted(
                [obj for obj in scene.objects if obj.rigid_body_constraint is not None], key=lambda item: item.name
            )
        else:
            if len(set(constraint_object_names)) != len(constraint_object_names):
                raise ValueError("constraint_object_names must be unique")
            candidates = [_object(name) for name in constraint_object_names]
            missing = [obj.name for obj in candidates if obj.rigid_body_constraint is None]
            if missing:
                raise ValueError(f"Objects have no rigid-body constraint settings: {missing}")
            outside = [obj.name for obj in candidates if obj.name not in scene.objects]
            if outside:
                raise ValueError(f"Constraint objects are outside scene '{scene.name}': {outside}")
        start, end, truncated, next_offset = paginate(len(candidates), offset, limit, 500)
        return {
            "scene": scene.name,
            "constraints": [_constraint_info(obj, world) for obj in candidates[start:end]],
            "page": {
                "total": len(candidates),
                "offset": start,
                "returned_count": end - start,
                "truncated": truncated,
                "next_offset": next_offset,
            },
        }

    def configure_rigid_body_world(
        self,
        scene_name,
        body_collection_name="RigidBodyWorld",
        constraint_collection_name="RigidBodyConstraints",
        world=None,
        gravity=None,
        use_gravity=None,
        cache=None,
        effector_weights=None,
        confirm_reassign_populated_collections=False,
        confirm_delete_baked_cache=False,
    ):
        scene = _scene(scene_name)
        world_patch = world or {}
        cache_patch = cache or {}
        weights_patch = dict(effector_weights or {})
        if gravity is not None and (len(gravity) != 3 or not all(math.isfinite(v) for v in gravity)):
            raise ValueError("gravity must contain three finite values")
        _preflight_collection_name(scene, body_collection_name)
        _preflight_collection_name(scene, constraint_collection_name)
        _validate_rna_properties(bpy.types.RigidBodyWorld.bl_rna.properties, world_patch, _WORLD_FIELDS)
        collection_name = weights_patch.pop("collection_name", None)
        clear_collection = weights_patch.pop("clear_collection", False)
        _validate_rna_properties(
            bpy.types.EffectorWeights.bl_rna.properties,
            weights_patch,
            _EFFECTOR_FIELDS,
        )
        effector_collection = None
        if collection_name:
            effector_collection = bpy.data.collections.get(collection_name)
            if effector_collection is None or not _collection_in_scene(effector_collection, scene):
                raise ValueError(f"Effector collection '{collection_name}' is not linked to scene '{scene.name}'")
        if ("frame_start" in cache_patch) != ("frame_end" in cache_patch):
            raise ValueError("Rigid-body cache frame_start and frame_end must be supplied together")
        if "frame_start" in cache_patch and cache_patch["frame_start"] > cache_patch["frame_end"]:
            raise ValueError("Rigid-body cache frame_start must not exceed frame_end")
        created_world = scene.rigidbody_world is None
        rigid_world = _ensure_world(scene)
        body_collection, body_created = _ensure_collection(scene, body_collection_name)
        constraint_collection, constraint_created = _ensure_collection(scene, constraint_collection_name)
        invalid_body_members = [obj.name for obj in body_collection.objects if obj.rigid_body is None]
        invalid_constraint_members = [
            obj.name for obj in constraint_collection.objects if obj.rigid_body_constraint is None
        ]
        if invalid_body_members:
            raise ValueError(f"Requested body collection contains objects without rigid bodies: {invalid_body_members}")
        if invalid_constraint_members:
            raise ValueError(
                "Requested constraint collection contains objects without rigid-body constraints: "
                f"{invalid_constraint_members}"
            )
        for current, requested, label in (
            (rigid_world.collection, body_collection, "body"),
            (rigid_world.constraints, constraint_collection, "constraint"),
        ):
            if (
                current is not None
                and current != requested
                and len(current.objects)
                and not confirm_reassign_populated_collections
            ):
                raise ValueError(
                    f"Refusing to replace populated rigid-body {label} collection '{current.name}'; "
                    "set confirm_reassign_populated_collections=True"
                )
        prospective_start = cache_patch.get("frame_start", rigid_world.point_cache.frame_start)
        prospective_end = cache_patch.get("frame_end", rigid_world.point_cache.frame_end)
        if prospective_start > prospective_end:
            raise ValueError("Rigid-body cache frame_start must not exceed frame_end")
        changed_cache = _prepare_cache_mutation(scene, rigid_world, confirm_delete_baked_cache)
        old = {
            "world": {name: getattr(rigid_world, name) for name in _WORLD_FIELDS},
            "body_collection": rigid_world.collection,
            "constraint_collection": rigid_world.constraints,
            "gravity": tuple(scene.gravity),
            "use_gravity": scene.use_gravity,
            "cache": {
                name: getattr(rigid_world.point_cache, name) for name in ("frame_start", "frame_end", "frame_step")
            },
            "weights": {name: getattr(rigid_world.effector_weights, name) for name in _EFFECTOR_FIELDS},
            "effector_collection": rigid_world.effector_weights.collection,
        }
        try:
            rigid_world.collection = body_collection
            rigid_world.constraints = constraint_collection
            world_changes = _apply_patch(rigid_world, world_patch, _WORLD_FIELDS)
            if gravity is not None:
                scene.gravity = gravity
            if use_gravity is not None:
                scene.use_gravity = use_gravity
            _set_cache_range(rigid_world.point_cache, cache_patch)
            weight_changes = _apply_patch(rigid_world.effector_weights, weights_patch, _EFFECTOR_FIELDS)
            if collection_name is not None or clear_collection:
                rigid_world.effector_weights.collection = effector_collection if collection_name else None
        except Exception:
            _restore_fields(rigid_world, old["world"])
            rigid_world.collection = old["body_collection"]
            rigid_world.constraints = old["constraint_collection"]
            scene.gravity = old["gravity"]
            scene.use_gravity = old["use_gravity"]
            _set_cache_range(rigid_world.point_cache, old["cache"])
            _restore_fields(rigid_world.effector_weights, old["weights"])
            rigid_world.effector_weights.collection = old["effector_collection"]
            raise
        scene_changed = bool(
            created_world
            or world_changes
            or weight_changes
            or tuple(scene.gravity) != old["gravity"]
            or scene.use_gravity != old["use_gravity"]
            or rigid_world.collection != old["body_collection"]
            or rigid_world.constraints != old["constraint_collection"]
            or rigid_world.effector_weights.collection != old["effector_collection"]
            or any(getattr(rigid_world.point_cache, name) != value for name, value in old["cache"].items())
        )
        warnings = []
        if changed_cache:
            warnings.append("The protected rigid-body bake was explicitly freed before changing world settings.")
        elif scene_changed and not created_world:
            warnings.append(
                "Rigid-body world settings changed; previously evaluated in-memory cache state may be stale."
            )
        changed_resources = [scene.name] if scene_changed else []
        changed_resources.extend(
            name
            for name, created in (
                (body_collection.name, body_created),
                (constraint_collection.name, constraint_created),
            )
            if created
        )
        return {
            "changed_resources": changed_resources,
            "scene": scene.name,
            "created_world": created_world,
            "created_collections": [
                name
                for name, created in (
                    (body_collection.name, body_created),
                    (constraint_collection.name, constraint_created),
                )
                if created
            ],
            "world_changes": world_changes,
            "effector_weight_changes": weight_changes,
            "world": self.get_rigid_body_scene_info(scene.name)["world"],
            "warnings": warnings,
        }

    def add_rigid_bodies(
        self,
        scene_name,
        object_names,
        body_type,
        settings=None,
        source_settings_object_name=None,
        world_collection_name=None,
        existing_policy="ERROR",
        confirm_delete_baked_cache=False,
    ):
        scene = _scene(scene_name)
        objects = _validate_object_batch(scene, object_names)
        unsupported = [obj.name for obj in objects if obj.type not in _SUPPORTED_BODY_TYPES]
        if unsupported:
            raise ValueError(f"Rigid-body creation currently supports mesh objects only: {unsupported}")
        for obj in objects:
            sync_from_editmode(obj)
            if not obj.data.vertices:
                raise ValueError(f"Mesh '{obj.name}' has no vertices")
        if body_type not in {"ACTIVE", "PASSIVE"}:
            raise ValueError("body_type must be ACTIVE or PASSIVE")
        if existing_policy not in {"ERROR", "REUSE"}:
            raise ValueError("existing_policy must be ERROR or REUSE")
        existing = [obj.name for obj in objects if obj.rigid_body is not None]
        if existing and existing_policy == "ERROR":
            raise ValueError(f"Objects already have rigid-body settings: {existing}")
        if world_collection_name:
            _preflight_collection_name(scene, world_collection_name)
        source_patch = {}
        if source_settings_object_name:
            source = _object(source_settings_object_name)
            if source.rigid_body is None:
                raise ValueError(f"Source settings object '{source.name}' has no rigid body")
            source_patch = _read_fields(source.rigid_body, _BODY_FIELDS - {"type"})
        explicit = dict(settings or {})
        if explicit.get("type", body_type) != body_type:
            raise ValueError("settings.type must match body_type")
        explicit.pop("type", None)
        combined = {**source_patch, **explicit, "type": body_type}
        _validate_rna_properties(bpy.types.RigidBodyObject.bl_rna.properties, combined, _BODY_FIELDS)
        if combined.get("use_deform") and combined.get("collision_shape", "CONVEX_HULL") != "MESH":
            raise ValueError("use_deform=True requires collision_shape='MESH'")
        rigid_world = _ensure_world(scene)
        target_collection = rigid_world.collection
        if world_collection_name:
            target_collection, _created = _ensure_collection(scene, world_collection_name)
            if (
                rigid_world.collection is not None
                and rigid_world.collection != target_collection
                and len(rigid_world.collection.objects)
            ):
                raise ValueError(
                    f"World already uses populated body collection '{rigid_world.collection.name}', "
                    f"not '{target_collection.name}'"
                )
        if target_collection is None:
            target_collection, _created = _ensure_collection(scene, world_collection_name or "RigidBodyWorld")
        invalid_members = [obj.name for obj in target_collection.objects if obj.rigid_body is None]
        if invalid_members:
            raise ValueError(f"World body collection contains objects without rigid bodies: {invalid_members}")
        for obj in objects:
            body_for_validation = obj.rigid_body
            if body_for_validation is not None:
                _validate_body_semantics(body_for_validation, combined)
                _validate_rna_patch(body_for_validation, combined, _BODY_FIELDS)
        changed_cache = _prepare_cache_mutation(scene, rigid_world, confirm_delete_baked_cache)
        snapshots = {obj.name: _body_snapshot(obj.rigid_body) for obj in objects if obj.rigid_body is not None}
        created = []
        try:
            rigid_world.collection = target_collection
            for obj in objects:
                if obj.rigid_body is None:
                    _add_rigid_body(scene, obj, body_type)
                    created.append(obj)
                if obj.name not in target_collection.objects:
                    target_collection.objects.link(obj)
                _validate_body_semantics(obj.rigid_body, combined)
                _apply_patch(obj.rigid_body, combined, _BODY_FIELDS)
        except Exception:
            for obj in reversed(created):
                with contextlib.suppress(Exception):
                    _remove_rigid_body(scene, obj)
            for name, snapshot in snapshots.items():
                body = bpy.data.objects.get(name).rigid_body
                layers = snapshot.pop("collision_collections")
                _restore_fields(body, snapshot)
                body.collision_collections = layers
            raise
        warnings = []
        if changed_cache:
            warnings.append("The protected rigid-body bake was explicitly freed before adding bodies.")
        if body_type == "ACTIVE" and combined.get("collision_shape", "CONVEX_HULL") == "MESH":
            warnings.append("Active MESH collision is expensive and may be unstable; prefer a convex proxy.")
        return {
            "changed_objects": [obj.name for obj in objects],
            "scene": scene.name,
            "body_collection": target_collection.name,
            "created": [obj.name for obj in created],
            "reused": [obj.name for obj in objects if obj not in created],
            "bodies": [{"object": obj.name, "settings": _body_info(obj)} for obj in objects],
            "warnings": warnings,
        }

    def configure_rigid_bodies(self, scene_name, targets, confirm_delete_baked_cache=False):
        scene = _scene(scene_name)
        if not targets or len(targets) > 500:
            raise ValueError("targets must contain 1-500 records")
        if any(not target.get("settings") for target in targets):
            raise ValueError("Every target settings patch must contain at least one property")
        objects = _validate_object_batch(scene, [item["object_name"] for item in targets], require_body=True)
        resolved = []
        for obj, target in zip(objects, targets, strict=True):
            patch = target["settings"]
            _validate_body_semantics(obj.rigid_body, patch)
            prepared = _validate_rna_patch(obj.rigid_body, patch, _BODY_FIELDS)
            resolved.append((obj, prepared, _body_snapshot(obj.rigid_body)))
        world = _ensure_world(scene)
        changed_cache = _prepare_cache_mutation(scene, world, confirm_delete_baked_cache)
        records = []
        try:
            for obj, patch, _snapshot in resolved:
                records.append({"object": obj.name, "changes": _apply_patch(obj.rigid_body, patch, _BODY_FIELDS)})
        except Exception:
            for obj, _patch, snapshot in resolved:
                layers = snapshot.pop("collision_collections")
                _restore_fields(obj.rigid_body, snapshot)
                obj.rigid_body.collision_collections = layers
            raise
        warnings = ["Rigid-body settings changed; unprotected evaluated cache state is stale."]
        if changed_cache:
            warnings[0] = "The protected rigid-body bake was explicitly freed before changing body settings."
        return {"changed_objects": [obj.name for obj in objects], "changes": records, "warnings": warnings}

    def set_rigid_body_mass(
        self,
        scene_name,
        assignments,
        target_total_mass=None,
        confirm_delete_baked_cache=False,
    ):
        scene = _scene(scene_name)
        objects = _validate_object_batch(
            scene, [assignment["object_name"] for assignment in assignments], require_body=True
        )
        proposed = []
        warnings = []
        for obj, assignment in zip(objects, assignments, strict=True):
            if ("mass" in assignment) == ("density" in assignment):
                raise ValueError(f"Mass assignment for '{obj.name}' needs exactly one of mass or density")
            if "mass" in assignment:
                mass = float(assignment["mass"])
                volume = None
                density = None
            else:
                if obj.type != "MESH":
                    raise ValueError(f"Density-derived mass requires a mesh: '{obj.name}'")
                volume, determinant = _mesh_volume(obj)
                density = float(assignment["density"])
                mass = density * volume
                if determinant < 0:
                    warnings.append(
                        f"'{obj.name}' has a negative world transform determinant; absolute volume was used."
                    )
            if not math.isfinite(mass) or mass < 0.001:
                raise ValueError(f"Calculated mass for '{obj.name}' must be finite and at least 0.001 kg")
            proposed.append({"object": obj, "mass": mass, "volume": volume, "density": density})
        if target_total_mass is not None:
            total = sum(item["mass"] for item in proposed)
            if total <= 0:
                raise ValueError("Cannot normalize a zero total mass")
            factor = float(target_total_mass) / total
            for item in proposed:
                item["mass"] *= factor
                if item["mass"] < 0.001:
                    raise ValueError("target_total_mass would put an object below Blender's 0.001 kg minimum")
        world = _ensure_world(scene)
        changed_cache = _prepare_cache_mutation(scene, world, confirm_delete_baked_cache)
        old = {item["object"].name: item["object"].rigid_body.mass for item in proposed}
        records = []
        try:
            for item in proposed:
                obj = item["object"]
                obj.rigid_body.mass = item["mass"]
                records.append(
                    {
                        "object": obj.name,
                        "old_mass": old[obj.name],
                        "new_mass": obj.rigid_body.mass,
                        "density": item["density"],
                        "evaluated_world_volume": item["volume"],
                    }
                )
        except Exception:
            for item in proposed:
                item["object"].rigid_body.mass = old[item["object"].name]
            raise
        warnings.append("Mass changes invalidate unprotected evaluated rigid-body cache state.")
        if changed_cache:
            warnings.append("The protected rigid-body bake was explicitly freed.")
        return {"changed_objects": [obj.name for obj in objects], "assignments": records, "warnings": warnings}

    def set_rigid_body_collision_layers(
        self,
        scene_name,
        targets,
        policy="REPLACE",
        confirm_delete_baked_cache=False,
    ):
        if policy not in {"REPLACE", "ADD", "REMOVE"}:
            raise ValueError("policy must be REPLACE, ADD, or REMOVE")
        scene = _scene(scene_name)
        objects = _validate_object_batch(scene, [target["object_name"] for target in targets], require_body=True)
        resolved = []
        for obj, target in zip(objects, targets, strict=True):
            has_layers = "layers" in target
            has_profile = "profile" in target
            if has_layers == has_profile:
                raise ValueError(f"Layer target '{obj.name}' needs exactly one of layers or profile")
            layers = set(target.get("layers", _LAYER_PROFILES.get(target.get("profile"), set())))
            if not layers or any(layer < 1 or layer > 20 for layer in layers):
                raise ValueError(f"Layers for '{obj.name}' must be unique values in [1, 20]")
            current = {index + 1 for index, flag in enumerate(obj.rigid_body.collision_collections) if flag}
            result = layers if policy == "REPLACE" else current | layers if policy == "ADD" else current - layers
            if not result:
                raise ValueError(f"Collision-layer operation would leave '{obj.name}' on no layers")
            resolved.append((obj, current, result))
        world = _ensure_world(scene)
        changed_cache = _prepare_cache_mutation(scene, world, confirm_delete_baked_cache)
        try:
            for obj, _current, result in resolved:
                obj.rigid_body.collision_collections = tuple(index + 1 in result for index in range(20))
        except Exception:
            for obj, current, _result in resolved:
                obj.rigid_body.collision_collections = tuple(index + 1 in current for index in range(20))
            raise
        disconnected = [
            [first.name, second.name]
            for (first, _a, first_layers), (second, _b, second_layers) in combinations(resolved, 2)
            if not first_layers & second_layers
        ]
        warnings = ["Collision-layer changes invalidate unprotected evaluated rigid-body cache state."]
        if changed_cache:
            warnings.append("The protected rigid-body bake was explicitly freed.")
        return {
            "changed_objects": [obj.name for obj in objects],
            "policy": policy,
            "layers": [{"object": obj.name, "old": sorted(old), "new": sorted(new)} for obj, old, new in resolved],
            "disconnected_target_pairs": disconnected,
            "warnings": warnings,
        }

    def create_rigid_body_collision_proxy(
        self,
        scene_name,
        source_object_name,
        proxy_name,
        collection_name,
        approximation,
        body_type,
        low_resolution_source_name=None,
        drive_render_object="NONE",
        hide_from_render=True,
        settings=None,
        confirm_delete_baked_cache=False,
    ):
        scene = _scene(scene_name)
        source = _validate_object_batch(scene, [source_object_name])[0]
        if source.type != "MESH":
            raise ValueError("Collision proxy sources must be mesh objects")
        if source.rigid_body is not None:
            raise ValueError(
                f"Source '{source.name}' already has rigid-body settings; "
                "remove or explicitly retain them before proxying"
            )
        if bpy.data.objects.get(proxy_name) is not None:
            raise ValueError(f"Object already exists: {proxy_name}")
        _preflight_collection_name(scene, collection_name)
        if body_type == "ACTIVE" and drive_render_object == "NONE":
            raise ValueError("Active proxies must drive the render object")
        if drive_render_object not in {"NONE", "PARENT", "COPY_TRANSFORMS"}:
            raise ValueError("drive_render_object must be NONE, PARENT, or COPY_TRANSFORMS")
        expected_shape = "CONVEX_HULL" if approximation in {"CONVEX_HULL", "LOW_RES_SOURCE"} else approximation
        if settings and settings.get("type", body_type) != body_type:
            raise ValueError("settings.type must match body_type")
        if settings and settings.get("collision_shape", expected_shape) != expected_shape:
            raise ValueError("settings.collision_shape must match the selected proxy approximation")
        vertices, _faces = _evaluated_mesh_payload(source, source)
        if not vertices:
            raise ValueError(f"Source mesh '{source.name}' has no evaluated vertices")
        source_bounds = _bounds(vertices)
        if source_bounds is None:
            raise RuntimeError("Evaluated source bounds could not be calculated")
        if any(dimension <= 1e-9 for dimension in source_bounds["dimensions"]):
            raise ValueError(f"Source '{source.name}' must have non-zero evaluated extent on every local axis")
        low_payload = None
        if approximation == "LOW_RES_SOURCE":
            low = _validate_object_batch(scene, [low_resolution_source_name])[0]
            if low.type != "MESH":
                raise ValueError("LOW_RES_SOURCE must name a mesh object")
            low_payload = _evaluated_mesh_payload(low, source)
        elif approximation not in {"BOX", "SPHERE", "CAPSULE", "CYLINDER", "CONVEX_HULL"}:
            raise ValueError(f"Unsupported proxy approximation: {approximation}")
        world = _ensure_world(scene)
        changed_cache = _prepare_cache_mutation(scene, world, confirm_delete_baked_cache)
        if approximation == "BOX":
            mesh = _box_mesh(f"{proxy_name} Mesh", source_bounds)
            collision_shape = "BOX"
        elif approximation in {"SPHERE", "CAPSULE", "CYLINDER"}:
            mesh = _primitive_proxy_mesh(f"{proxy_name} Mesh", approximation, source_bounds)
            collision_shape = approximation
        elif approximation == "CONVEX_HULL":
            mesh = _convex_hull_mesh(f"{proxy_name} Mesh", vertices)
            collision_shape = "CONVEX_HULL"
        else:
            if low_payload is None:
                raise RuntimeError("LOW_RES_SOURCE geometry was not prepared")
            low_vertices, low_faces = low_payload
            mesh = bpy.data.meshes.new(f"{proxy_name} Mesh")
            mesh.from_pydata(low_vertices, [], low_faces)
            mesh.update()
            collision_shape = "CONVEX_HULL"
        collection, _created = _ensure_collection(scene, collection_name)
        proxy = bpy.data.objects.new(proxy_name, mesh)
        collection.objects.link(proxy)
        proxy.matrix_world = source.matrix_world.copy()
        proxy.hide_render = bool(hide_from_render)
        proxy.display_type = "WIRE"
        rig_id = uuid.uuid4().hex
        _add_rigid_body(scene, proxy, body_type)
        body_patch = {**(settings or {}), "type": body_type, "collision_shape": collision_shape}
        _validate_body_semantics(proxy.rigid_body, body_patch)
        _apply_patch(proxy.rigid_body, body_patch, _BODY_FIELDS)
        driver = None
        if drive_render_object == "COPY_TRANSFORMS":
            constraint = source.constraints.new("COPY_TRANSFORMS")
            constraint.name = f"MCP Rigid Body Proxy {rig_id[:8]}"
            constraint.target = proxy
            driver = {"type": "COPY_TRANSFORMS", "name": constraint.name}
        elif drive_render_object == "PARENT":
            source_world = source.matrix_world.copy()
            source.parent = proxy
            source.matrix_world = source_world
            driver = {"type": "PARENT", "object": proxy.name}
        proxy["blendermcp_rigid_body_rig_id"] = rig_id
        proxy["blendermcp_rigid_body_role"] = "collision_proxy"
        proxy["blendermcp_rigid_body_source"] = source.name
        source[f"blendermcp_rigid_body_proxy_{rig_id}"] = proxy.name
        proxy_vertices = [vertex.co.copy() for vertex in mesh.vertices]
        proxy_bounds = _bounds(proxy_vertices)
        if proxy_bounds is None:
            raise RuntimeError("Created collision proxy has no vertices")
        source_volume = math.prod(source_bounds["dimensions"])
        proxy_volume = math.prod(proxy_bounds["dimensions"])
        return {
            "changed_objects": [source.name, proxy.name] if driver else [proxy.name],
            "proxy": proxy.name,
            "source": source.name,
            "collection": collection.name,
            "rig_id": rig_id,
            "approximation": approximation,
            "collision_shape": collision_shape,
            "driver": driver,
            "source_bounds_object_local": source_bounds,
            "proxy_bounds_object_local": proxy_bounds,
            "bounds_volume_ratio": proxy_volume / source_volume if source_volume > 1e-12 else None,
            "rigid_body": _body_info(proxy),
            "warnings": [
                "The visible source geometry was preserved; the proxy remains a separate editable object.",
                *(["The protected rigid-body bake was explicitly freed."] if changed_cache else []),
            ],
        }

    def create_rigid_body_constraint(
        self,
        scene_name,
        name,
        object1_name,
        object2_name,
        transform,
        configuration,
        collection_name=None,
        confirm_delete_baked_cache=False,
    ):
        scene = _scene(scene_name)
        if bpy.data.objects.get(name) is not None:
            raise ValueError(f"Object already exists: {name}")
        first, second = _validate_object_batch(scene, [object1_name, object2_name], require_body=True)
        if first == second:
            raise ValueError("Rigid-body constraint endpoints must be distinct")
        if collection_name:
            _preflight_collection_name(scene, collection_name)
        world = _ensure_world(scene)
        constraint_collection_name = collection_name or (
            world.constraints.name if world.constraints else "RigidBodyConstraints"
        )
        collection, _created = _ensure_collection(scene, constraint_collection_name)
        if world.constraints is not None and world.constraints != collection and len(world.constraints.objects):
            raise ValueError(f"World already uses populated constraint collection '{world.constraints.name}'")
        invalid_members = [obj.name for obj in collection.objects if obj.rigid_body_constraint is None]
        if invalid_members:
            raise ValueError(
                f"Constraint collection contains objects without rigid-body constraints: {invalid_members}"
            )
        changed_cache = _prepare_cache_mutation(scene, world, confirm_delete_baked_cache)
        empty = bpy.data.objects.new(name, None)
        # Linking directly to RigidBodyWorld.constraints creates the component
        # implicitly in Blender 5.1+.  Link to the scene root first so the
        # documented constraint_add operator remains the sole creator.
        scene.collection.objects.link(empty)
        empty.empty_display_type = "ARROWS"
        empty.location = transform["location"]
        empty.rotation_mode = "QUATERNION"
        if "rotation_quaternion" in transform:
            quaternion = mathutils.Quaternion(transform["rotation_quaternion"])
            quaternion.normalize()
        elif "axis" in transform:
            axis = mathutils.Vector(transform["axis"])
            if axis.length <= 1e-12:
                raise ValueError("Constraint axis must be non-zero")
            axis.normalize()
            local_axis = mathutils.Vector((0.0, 0.0, 1.0) if configuration["type"] == "HINGE" else (1.0, 0.0, 0.0))
            quaternion = local_axis.rotation_difference(axis)
        else:
            quaternion = mathutils.Quaternion((1.0, 0.0, 0.0, 0.0))
        empty.rotation_quaternion = quaternion
        world.constraints = collection
        _run_object_operator(scene, empty, bpy.ops.rigidbody.constraint_add, type=configuration["type"])
        if empty.name in scene.collection.objects and empty.name in collection.objects:
            scene.collection.objects.unlink(empty)
        constraint = empty.rigid_body_constraint
        if constraint is None:
            raise RuntimeError("Blender reported FINISHED but created no rigid-body constraint")
        constraint.object1 = first
        constraint.object2 = second
        flat = _flatten_constraint_config(constraint, configuration)
        changes = _apply_patch(
            constraint,
            flat,
            {prop.identifier for prop in constraint.bl_rna.properties if prop.identifier != "rna_type"},
        )
        rig_id = uuid.uuid4().hex
        empty["blendermcp_rigid_body_rig_id"] = rig_id
        empty["blendermcp_rigid_body_role"] = "constraint"
        return {
            "changed_objects": [empty.name],
            "constraint_object": empty.name,
            "collection": collection.name,
            "rig_id": rig_id,
            "constraint": _constraint_flat_info(constraint),
            "changes": changes,
            "active_degrees_of_freedom": _active_degrees_of_freedom(constraint.type),
            "axis_convention": "LOCAL_Z" if constraint.type == "HINGE" else "LOCAL_X",
            "warnings": ["The protected rigid-body bake was explicitly freed."] if changed_cache else [],
        }

    def configure_rigid_body_constraint(
        self,
        scene_name,
        constraint_object_name,
        configuration,
        object1_name=None,
        object2_name=None,
        confirm_delete_baked_cache=False,
    ):
        scene = _scene(scene_name)
        obj = _validate_object_batch(scene, [constraint_object_name])[0]
        constraint = obj.rigid_body_constraint
        if constraint is None:
            raise ValueError(f"Object '{obj.name}' has no rigid-body constraint")
        if set(configuration) == {"type"} and object1_name is None and object2_name is None:
            raise ValueError("Provide at least one constraint setting or endpoint change")
        first = _object(object1_name) if object1_name else constraint.object1
        second = _object(object2_name) if object2_name else constraint.object2
        if first is None or second is None:
            raise ValueError("Both rigid-body constraint endpoints are required")
        if first == second:
            raise ValueError("Rigid-body constraint endpoints must be distinct")
        for endpoint in (first, second):
            if endpoint.name not in scene.objects or endpoint.rigid_body is None:
                raise ValueError(f"Endpoint '{endpoint.name}' must be a rigid body in scene '{scene.name}'")
        flat = _flatten_constraint_config(constraint, configuration)
        world = _ensure_world(scene)
        changed_cache = _prepare_cache_mutation(scene, world, confirm_delete_baked_cache)
        old = {name: getattr(constraint, name) for name in flat}
        old_endpoints = (constraint.object1, constraint.object2)
        try:
            constraint.object1 = first
            constraint.object2 = second
            changes = _apply_patch(
                constraint,
                flat,
                {prop.identifier for prop in constraint.bl_rna.properties if prop.identifier != "rna_type"},
            )
        except Exception:
            constraint.object1, constraint.object2 = old_endpoints
            _restore_fields(constraint, old)
            raise
        warnings = ["Constraint changes invalidate unprotected evaluated rigid-body cache state."]
        if changed_cache:
            warnings.append("The protected rigid-body bake was explicitly freed.")
        return {
            "changed_objects": [obj.name],
            "constraint_object": obj.name,
            "endpoint_changes": {
                "object1": {"old": old_endpoints[0].name if old_endpoints[0] else None, "new": first.name},
                "object2": {"old": old_endpoints[1].name if old_endpoints[1] else None, "new": second.name},
            },
            "changes": changes,
            "constraint": _constraint_flat_info(constraint),
            "active_degrees_of_freedom": _active_degrees_of_freedom(constraint.type),
            "warnings": warnings,
        }

    def validate_rigid_body_setup(
        self,
        scene_name,
        object_names=None,
        max_findings=200,
        collision_pair_limit=64,
        evaluated_triangle_limit=250000,
    ):
        scene = _scene(scene_name)
        world = scene.rigidbody_world
        findings = []
        omitted = 0

        def add(severity, code, subject, evidence, remediation):
            nonlocal omitted
            if len(findings) >= max_findings:
                omitted += 1
                return
            findings.append(
                {
                    "severity": severity,
                    "code": code,
                    "object_or_constraint": subject,
                    "evidence": evidence,
                    "remediation": remediation,
                }
            )

        if world is None:
            add("ERROR", "MISSING_WORLD", None, {"scene": scene.name}, "Configure a rigid-body world first.")
            return {
                "scene": scene.name,
                "bodies_checked": 0,
                "constraints_checked": 0,
                "findings": findings,
                "summary": {"errors": 1, "warnings": 0, "info": 0},
                "truncated": False,
                "omitted_findings": 0,
            }
        if world.point_cache.frame_start > world.point_cache.frame_end:
            add(
                "ERROR",
                "INVALID_CACHE_RANGE",
                None,
                _cache_info(world.point_cache),
                "Configure a cache range whose start is not after its end.",
            )
        if world.point_cache.is_baking:
            add("ERROR", "CACHE_BAKING", None, _cache_info(world.point_cache), "Wait for the active bake to finish.")
        elif world.point_cache.is_outdated:
            add(
                "WARNING",
                "CACHE_OUTDATED",
                None,
                _cache_info(world.point_cache),
                "Re-evaluate or rebake after setup changes.",
            )
        elif world.point_cache.is_baked:
            add(
                "INFO",
                "CACHE_BAKED",
                None,
                _cache_info(world.point_cache),
                "Free the bake explicitly before editing physics.",
            )
        scoped = [obj for obj in scene.objects if obj.rigid_body is not None]
        if object_names is not None:
            requested = set(object_names)
            if len(requested) != len(object_names):
                raise ValueError("object_names must be unique")
            missing = requested - {obj.name for obj in scoped}
            if missing:
                raise ValueError(f"Requested objects are missing or have no rigid body: {sorted(missing)}")
            scoped = [obj for obj in scoped if obj.name in requested]
        member_names = {obj.name for obj in _world_member_objects(scene, world)}
        geometry = {}
        masses = []
        for obj in scoped:
            body = obj.rigid_body
            if obj.name not in member_names:
                add(
                    "ERROR",
                    "BODY_OUTSIDE_WORLD_COLLECTION",
                    obj.name,
                    {"world_collection": world.collection.name if world.collection else None},
                    "Link the object to the rigid-body world's body collection.",
                )
            scale = [float(value) for value in obj.scale]
            if any(not math.isfinite(value) or abs(value) <= 1e-12 for value in scale):
                add("ERROR", "INVALID_SCALE", obj.name, scale, "Repair zero or non-finite scale before simulation.")
            elif max(abs(value) for value in scale) / min(abs(value) for value in scale) > 10.0:
                add("WARNING", "EXTREME_NONUNIFORM_SCALE", obj.name, scale, "Use a collision proxy or normalize scale.")
            elif any(abs(abs(value) - 1.0) > 1e-4 for value in scale):
                add(
                    "INFO",
                    "UNAPPLIED_SCALE",
                    obj.name,
                    scale,
                    "Confirm scale-sensitive collision margins intentionally.",
                )
            if not math.isfinite(body.mass) or body.mass < 0.001:
                add("ERROR", "INVALID_MASS", obj.name, body.mass, "Assign a finite mass of at least 0.001 kg.")
            if body.type == "ACTIVE":
                masses.append((obj.name, float(body.mass)))
                if body.collision_shape == "MESH":
                    add(
                        "WARNING",
                        "ACTIVE_CONCAVE_MESH",
                        obj.name,
                        {"collision_shape": body.collision_shape},
                        "Prefer CONVEX_HULL or a dedicated low-resolution proxy.",
                    )
            if body.use_deform and body.collision_shape != "MESH":
                add(
                    "ERROR",
                    "DEFORM_WITH_NON_MESH_SHAPE",
                    obj.name,
                    {"collision_shape": body.collision_shape},
                    "Disable deformation or use a MESH collision shape deliberately.",
                )
            if _animation_info(obj)["animated"] and body.type == "ACTIVE" and not body.kinematic:
                add(
                    "WARNING",
                    "ANIMATED_ACTIVE_NOT_KINEMATIC",
                    obj.name,
                    {"kinematic": False},
                    "Use intentional kinematic control and keyframed handoff for authored motion.",
                )
            info = _evaluated_geometry(obj)
            geometry[obj.name] = info
            if info["triangles"] and info["triangles"] > evaluated_triangle_limit:
                add(
                    "WARNING",
                    "EXCESSIVE_COLLISION_COMPLEXITY",
                    obj.name,
                    {"triangles": info["triangles"], "limit": evaluated_triangle_limit},
                    "Use a simpler collision proxy.",
                )
            diagonal = math.sqrt(
                sum(value * value for value in (info["bounds_world"] or {"dimensions": [0, 0, 0]})["dimensions"])
            )
            if body.use_margin and diagonal > 0 and body.collision_margin > diagonal * 0.2:
                add(
                    "WARNING",
                    "LARGE_COLLISION_MARGIN",
                    obj.name,
                    {"margin": body.collision_margin, "bounds_diagonal": diagonal},
                    "Reduce the margin relative to the collision proxy size.",
                )
        if len(masses) > 1:
            positive = [mass for _name, mass in masses if mass > 0]
            ratio = max(positive) / min(positive)
            if ratio > 1000:
                add(
                    "WARNING",
                    "EXTREME_MASS_RATIO",
                    None,
                    {
                        "ratio": ratio,
                        "lightest": min(masses, key=lambda item: item[1]),
                        "heaviest": max(masses, key=lambda item: item[1]),
                    },
                    "Reduce interacting mass ratios or increase solver quality.",
                )
        constraints = [obj for obj in scene.objects if obj.rigid_body_constraint is not None]
        constraint_members = {obj.name for obj in _world_constraint_objects(scene, world)}
        disabled_pairs = set()
        for obj in constraints:
            constraint = obj.rigid_body_constraint
            if obj.name not in constraint_members:
                add(
                    "ERROR",
                    "CONSTRAINT_OUTSIDE_WORLD_COLLECTION",
                    obj.name,
                    {"world_collection": world.constraints.name if world.constraints else None},
                    "Link the constraint to the world's constraint collection.",
                )
            endpoints = (constraint.object1, constraint.object2)
            if any(endpoint is None for endpoint in endpoints):
                add(
                    "ERROR",
                    "MISSING_CONSTRAINT_ENDPOINT",
                    obj.name,
                    _constraint_flat_info(constraint),
                    "Assign both endpoints.",
                )
                continue
            if endpoints[0] == endpoints[1]:
                add("ERROR", "SAME_CONSTRAINT_ENDPOINT", obj.name, endpoints[0].name, "Assign two distinct bodies.")
            for endpoint in endpoints:
                if endpoint.rigid_body is None:
                    add(
                        "ERROR",
                        "NON_RIGID_ENDPOINT",
                        obj.name,
                        {"endpoint": endpoint.name},
                        "Add rigid-body settings to the endpoint.",
                    )
            if constraint.disable_collisions:
                disabled_pairs.add(frozenset((endpoints[0].name, endpoints[1].name)))
            for affix in ("lin_x", "lin_y", "lin_z", "ang_x", "ang_y", "ang_z"):
                if getattr(constraint, f"use_limit_{affix}") and getattr(constraint, f"limit_{affix}_lower") > getattr(
                    constraint, f"limit_{affix}_upper"
                ):
                    add(
                        "ERROR",
                        "INVALID_CONSTRAINT_LIMIT",
                        obj.name,
                        {"axis": affix},
                        "Set the lower limit less than or equal to the upper limit.",
                    )
        pair_count = 0
        pair_limit_reached = False
        depsgraph = _scene_depsgraph(scene)
        for first, second in combinations(scoped, 2):
            if pair_count >= collision_pair_limit:
                pair_limit_reached = True
                break
            first_layers = {i for i, flag in enumerate(first.rigid_body.collision_collections) if flag}
            second_layers = {i for i, flag in enumerate(second.rigid_body.collision_collections) if flag}
            if not first_layers & second_layers:
                add(
                    "INFO",
                    "DISCONNECTED_COLLISION_LAYERS",
                    f"{first.name}|{second.name}",
                    {"first": sorted(i + 1 for i in first_layers), "second": sorted(i + 1 for i in second_layers)},
                    "Confirm these bodies are intentionally unable to collide.",
                )
                continue
            if frozenset((first.name, second.name)) in disabled_pairs:
                continue
            first_bounds = geometry[first.name]["bounds_world"]
            second_bounds = geometry[second.name]["bounds_world"]
            if not first_bounds or not second_bounds or not _aabb_overlap(first_bounds, second_bounds):
                continue
            pair_count += 1
            if (
                geometry[first.name]["triangles"] <= evaluated_triangle_limit
                and geometry[second.name]["triangles"] <= evaluated_triangle_limit
            ):
                first_bvh = _world_bvh(first, depsgraph)
                second_bvh = _world_bvh(second, depsgraph)
                overlaps = first_bvh.overlap(second_bvh)
                if overlaps:
                    add(
                        "WARNING",
                        (
                            "INITIAL_INTERPENETRATION"
                            if scene.frame_current == world.point_cache.frame_start
                            else "CURRENT_FRAME_INTERPENETRATION"
                        ),
                        f"{first.name}|{second.name}",
                        {
                            "frame": scene.frame_current,
                            "cache_start": world.point_cache.frame_start,
                            "overlapping_face_pairs": len(overlaps),
                            "sample": overlaps[:10],
                        },
                        "Separate the initial collision geometry or reduce margins before simulation.",
                    )
        if len(scoped) > 50 and world.substeps_per_frame < 10:
            add(
                "WARNING",
                "LOW_SUBSTEPS_FOR_BODY_COUNT",
                None,
                {"bodies": len(scoped), "substeps_per_frame": world.substeps_per_frame},
                "Test with more substeps and compare stability/performance.",
            )
        if constraints and world.solver_iterations < 10:
            add(
                "WARNING",
                "LOW_SOLVER_ITERATIONS_FOR_CONSTRAINTS",
                None,
                {"constraints": len(constraints), "solver_iterations": world.solver_iterations},
                "Test with more solver iterations and compare constraint drift.",
            )
        counts = Counter(item["severity"] for item in findings)
        return {
            "scene": scene.name,
            "frame_observed": scene.frame_current,
            "bodies_checked": len(scoped),
            "constraints_checked": len(constraints),
            "collision_pairs_narrow_phase_checked": pair_count,
            "collision_pair_limit_reached": pair_limit_reached,
            "findings": findings,
            "summary": {"errors": counts["ERROR"], "warnings": counts["WARNING"], "info": counts["INFO"]},
            "truncated": omitted > 0,
            "omitted_findings": omitted,
            "disclaimer": "Static preflight only; physical correctness requires bounded simulation sampling.",
        }
