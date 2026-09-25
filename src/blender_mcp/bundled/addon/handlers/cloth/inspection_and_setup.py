"""Blender-main-thread handlers for cloth simulation inspection and setup."""

from __future__ import annotations

import contextlib
import math
import statistics

from collections import Counter
from itertools import pairwise

import bpy

from ...helpers import paginate, sync_from_editmode
from ..rna_patch import get_object, patch_rna, read_fields, restore_rna, serialize, validate_rna_value
from ..simulation_cache import point_cache_info, set_cache_frame_range

_MCP_SCHEMA_VERSION = 1
_OWNERSHIP_PREFIX = "blendermcp_cloth"
_DEFORMING_MODIFIERS = {"ARMATURE", "HOOK", "LATTICE", "MESH_DEFORM", "SURFACE_DEFORM"}
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
_MATERIAL_FIELDS = {
    "mass",
    "air_damping",
    "bending_model",
    "tension_stiffness",
    "tension_stiffness_max",
    "compression_stiffness",
    "compression_stiffness_max",
    "shear_stiffness",
    "shear_stiffness_max",
    "bending_stiffness",
    "bending_stiffness_max",
    "tension_damping",
    "compression_damping",
    "shear_damping",
    "bending_damping",
}
_SOLVER_FIELDS = {"quality", "time_scale", "gravity", "voxel_cell_size"}
_PINNING_FIELDS = {
    "pin_stiffness",
    "goal_min",
    "goal_max",
    "goal_default",
    "goal_spring",
    "goal_friction",
}
_CLOTH_COLLISION_FIELDS = {
    "use_collision",
    "collision_quality",
    "distance_min",
    "impulse_clamp",
    "damping",
    "friction",
    "vertex_group_object_collisions",
    "use_self_collision",
    "self_distance_min",
    "self_friction",
    "self_impulse_clamp",
    "vertex_group_self_collisions",
}
_COLLIDER_FIELDS = {"use", "thickness_outer", "cloth_friction", "damping", "use_culling", "use_normal"}
_MATERIAL_PRESETS = {
    "COTTON": {
        "mass": 0.3,
        "tension_stiffness": 15.0,
        "compression_stiffness": 15.0,
        "shear_stiffness": 15.0,
        "bending_stiffness": 0.5,
        "tension_damping": 5.0,
        "compression_damping": 5.0,
        "shear_damping": 5.0,
        "air_damping": 1.0,
    },
    "SILK": {
        "mass": 0.15,
        "tension_stiffness": 5.0,
        "compression_stiffness": 5.0,
        "shear_stiffness": 5.0,
        "bending_stiffness": 0.05,
        "tension_damping": 0.0,
        "compression_damping": 0.0,
        "shear_damping": 0.0,
        "air_damping": 1.0,
    },
    "DENIM": {
        "mass": 1.0,
        "tension_stiffness": 40.0,
        "compression_stiffness": 40.0,
        "shear_stiffness": 40.0,
        "bending_stiffness": 10.0,
        "tension_damping": 25.0,
        "compression_damping": 25.0,
        "shear_damping": 25.0,
        "air_damping": 1.0,
    },
    "LEATHER": {
        "mass": 0.4,
        "tension_stiffness": 80.0,
        "compression_stiffness": 80.0,
        "shear_stiffness": 80.0,
        "bending_stiffness": 150.0,
        "tension_damping": 25.0,
        "compression_damping": 25.0,
        "shear_damping": 25.0,
        "air_damping": 1.0,
    },
    "RUBBER": {
        "mass": 3.0,
        "tension_stiffness": 15.0,
        "compression_stiffness": 15.0,
        "shear_stiffness": 15.0,
        "bending_stiffness": 25.0,
        "tension_damping": 25.0,
        "compression_damping": 25.0,
        "shear_damping": 25.0,
        "air_damping": 1.0,
    },
}
_WEIGHT_ROLES = {
    "PIN_MASS": ("settings", "vertex_group_mass"),
    "STRUCTURAL_STIFFNESS": ("settings", "vertex_group_structural_stiffness"),
    "SHEAR_STIFFNESS": ("settings", "vertex_group_shear_stiffness"),
    "BENDING_STIFFNESS": ("settings", "vertex_group_bending"),
    "SHRINK": ("settings", "vertex_group_shrink"),
    "PRESSURE": ("settings", "vertex_group_pressure"),
    "INTERNAL_SPRINGS": ("settings", "vertex_group_intern"),
    "OBJECT_COLLISION_EXCLUSION": ("collision_settings", "vertex_group_object_collisions"),
    "SELF_COLLISION_EXCLUSION": ("collision_settings", "vertex_group_self_collisions"),
}


def _get_modifier(obj, name, modifier_type):
    modifier = obj.modifiers.get(name)
    if modifier is None:
        raise ValueError(f"Modifier not found: {name} on '{obj.name}'")
    if modifier.type != modifier_type:
        raise ValueError(f"Modifier '{name}' on '{obj.name}' is {modifier.type}, not {modifier_type}")
    return modifier


def _get_cloth(object_name, modifier_name):
    obj = get_object(object_name, {"MESH"})
    return obj, _get_modifier(obj, modifier_name, "CLOTH")


def _cache_info(cache):
    return point_cache_info(cache)


def _reject_baked(modifiers):
    baked = [f"{obj.name}:{mod.name}" for obj, mod in modifiers if mod.point_cache.is_baked]
    if baked:
        raise ValueError("Cannot change a baked cloth cache. Free the exact bake separately first: " + ", ".join(baked))


def _tag_update(obj):
    with contextlib.suppress(Exception):
        obj.update_tag(refresh={"DATA"})
    bpy.context.view_layer.update()


def _modifier_info(obj, modifier):
    return {
        "name": modifier.name,
        "type": modifier.type,
        "index": list(obj.modifiers).index(modifier),
        "show_viewport": modifier.show_viewport,
        "show_render": modifier.show_render,
    }


def _native_transform(obj):
    if obj.rotation_mode == "QUATERNION":
        rotation = list(obj.rotation_quaternion)
    elif obj.rotation_mode == "AXIS_ANGLE":
        rotation = list(obj.rotation_axis_angle)
    else:
        rotation = list(obj.rotation_euler)
    world_location, world_rotation, world_scale = obj.matrix_world.decompose()
    return {
        "local": {
            "coordinate_space": "PARENT_LOCAL",
            "location": list(obj.location),
            "rotation_mode": obj.rotation_mode,
            "rotation": rotation,
            "scale": list(obj.scale),
        },
        "world": {
            "coordinate_space": "WORLD",
            "location": list(world_location),
            "rotation_quaternion": list(world_rotation),
            "scale": list(world_scale),
            "determinant": float(obj.matrix_world.to_3x3().determinant()),
        },
    }


def _scene_scope(scene_name, collection_name=None):
    scene = bpy.data.scenes.get(scene_name)
    if scene is None:
        raise ValueError(f"Scene not found: {scene_name}")
    if collection_name is None:
        return scene, list(scene.objects), None
    collection = bpy.data.collections.get(collection_name)
    if collection is None:
        raise ValueError(f"Collection not found: {collection_name}")
    if not _collection_in_scene(collection, scene):
        raise ValueError(f"Collection '{collection_name}' is not linked to scene '{scene_name}'")
    return scene, list(collection.all_objects), collection


def _collection_in_scene(collection, scene):
    pending = [scene.collection]
    while pending:
        current = pending.pop()
        if current == collection:
            return True
        pending.extend(current.children)
    return False


def _object_scenes(obj):
    return [scene for scene in bpy.data.scenes if obj.name in scene.objects]


def _scene_context_for_object(obj):
    scenes = _object_scenes(obj)
    if not scenes:
        raise ValueError(f"Object '{obj.name}' is not linked to a scene")
    scene = bpy.context.scene if bpy.context.scene in scenes else scenes[0]
    for layer in scene.view_layers:
        layer.update()
    view_layer = next((layer for layer in scene.view_layers if obj.name in layer.objects), None)
    if view_layer is None:
        raise ValueError(f"Object '{obj.name}' is excluded from every view layer in scene '{scene.name}'")
    return scene, view_layer


def _action_fcurves(animation):
    action = getattr(animation, "action", None)
    if action is None:
        return []
    slot = getattr(animation, "action_slot", None)
    layers = getattr(action, "layers", None)
    if slot is not None and layers is not None:
        curves = []
        for layer in layers:
            for strip in layer.strips:
                if getattr(strip, "type", None) != "KEYFRAME":
                    continue
                channelbag = strip.channelbag(slot)
                if channelbag is not None:
                    curves.extend(channelbag.fcurves)
        return curves
    return list(getattr(action, "fcurves", ()))


def _animation_info(obj):
    sources = []
    for label, owner in (("OBJECT", obj), ("DATA", getattr(obj, "data", None))):
        animation = getattr(owner, "animation_data", None)
        action = getattr(animation, "action", None)
        if action:
            fcurves = _action_fcurves(animation)
            sources.append(
                {
                    "owner": label,
                    "action": action.name,
                    "slot": getattr(getattr(animation, "action_slot", None), "identifier", None),
                    "fcurves": [curve.data_path for curve in fcurves[:100]],
                    "truncated": len(fcurves) > 100,
                }
            )
    shape_keys = getattr(getattr(obj, "data", None), "shape_keys", None)
    animation = getattr(shape_keys, "animation_data", None)
    action = getattr(animation, "action", None)
    if action:
        sources.append({"owner": "SHAPE_KEYS", "action": action.name})
    return sources


def _max_keyed_location_delta(obj):
    animation = getattr(obj, "animation_data", None)
    if getattr(animation, "action", None) is None:
        return None
    maximum = 0.0
    found = False
    for curve in _action_fcurves(animation):
        if curve.data_path != "location":
            continue
        values = sorted((float(point.co[0]), float(point.co[1])) for point in curve.keyframe_points)
        for previous, current in pairwise(values):
            frame_delta = current[0] - previous[0]
            if frame_delta > 0:
                maximum = max(maximum, abs(current[1] - previous[1]) / frame_delta)
                found = True
    return maximum if found else None


def _modifier_is_animated(obj, modifier):
    if modifier.type == "BUILD":
        return True
    with contextlib.suppress(Exception):
        modifier_path = modifier.path_from_id()
        animation = getattr(obj, "animation_data", None)
        curves = [*_action_fcurves(animation), *getattr(animation, "drivers", ())]
        return any(curve.data_path.startswith(modifier_path) for curve in curves)
    return False


def _vertex_group_stats(obj, group):
    weights = []
    assigned = 0
    for vertex in obj.data.vertices:
        entry = next((item for item in vertex.groups if item.group == group.index), None)
        if entry is not None:
            assigned += 1
            weights.append(float(entry.weight))
    return {
        "name": group.name,
        "locked": group.lock_weight,
        "assigned_vertices": assigned,
        "unassigned_vertices": len(obj.data.vertices) - assigned,
        "minimum": min(weights) if weights else None,
        "maximum": max(weights) if weights else None,
        "mean": statistics.fmean(weights) if weights else None,
        "nonzero": sum(weight > 0.0 for weight in weights),
    }


def _shape_keys(obj):
    keys = getattr(getattr(obj.data, "shape_keys", None), "key_blocks", None)
    if not keys:
        return []
    return [{"name": key.name, "value": float(key.value), "mute": key.mute} for key in keys]


def _evaluated_counts(obj):
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = obj.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh()
    try:
        return {
            "coordinate_space": "EVALUATED_OBJECT_LOCAL",
            "vertices": len(mesh.vertices),
            "edges": len(mesh.edges),
            "faces": len(mesh.polygons),
        }
    finally:
        evaluated.to_mesh_clear()


def _field_relationships(settings, scene):
    weights = getattr(settings, "effector_weights", None)
    if weights is None:
        return {"weights": {}, "collection": None, "effectors": []}
    collection = getattr(weights, "collection", None)
    candidates = collection.all_objects if collection else scene.objects
    effectors = []
    for obj in candidates:
        field = getattr(obj, "field", None)
        if field and getattr(field, "type", "NONE") != "NONE":
            effectors.append({"object": obj.name, "type": field.type})
    scalar_fields = {
        prop.identifier
        for prop in weights.bl_rna.properties
        if prop.identifier not in {"rna_type", "collection"} and prop.type in {"FLOAT", "INT", "BOOLEAN"}
    }
    return {
        "weights": read_fields(weights, scalar_fields),
        "collection": collection.name if collection else None,
        "effectors": effectors[:100],
        "truncated": len(effectors) > 100,
    }


def _cloth_info(obj, modifier, scene):
    settings = modifier.settings
    collisions = modifier.collision_settings
    solver_result = modifier.solver_result
    serialized_solver_result = None
    if solver_result is not None:
        serialized_solver_result = read_fields(
            solver_result,
            {prop.identifier for prop in solver_result.bl_rna.properties if prop.identifier != "rna_type"},
        )
    return {
        **_modifier_info(obj, modifier),
        "settings": read_fields(
            settings,
            _MATERIAL_FIELDS
            | _SOLVER_FIELDS
            | _PINNING_FIELDS
            | {
                "vertex_group_mass",
                "vertex_group_structural_stiffness",
                "vertex_group_shear_stiffness",
                "vertex_group_bending",
                "vertex_group_shrink",
                "vertex_group_pressure",
                "vertex_group_intern",
                "use_sewing_springs",
                "sewing_force_max",
                "use_pressure",
                "uniform_pressure_force",
                "use_pressure_volume",
                "target_volume",
                "pressure_factor",
                "fluid_density",
                "use_internal_springs",
                "internal_spring_max_length",
                "internal_spring_max_diversion",
                "internal_spring_normal_check",
                "internal_tension_stiffness",
                "internal_compression_stiffness",
                "internal_tension_stiffness_max",
                "internal_compression_stiffness_max",
                "internal_friction",
                "rest_shape_key",
                "use_dynamic_mesh",
            },
        ),
        "collision_settings": {
            **read_fields(collisions, _CLOTH_COLLISION_FIELDS),
            "collection": collisions.collection.name if collisions.collection else None,
        },
        "field_relationships": _field_relationships(settings, scene),
        "point_cache": _cache_info(modifier.point_cache),
        "solver_status": "AVAILABLE" if solver_result is not None else "NOT_INITIALIZED",
        "solver_result": serialized_solver_result,
    }


def _collider_info(obj, modifier):
    settings = obj.collision
    all_fields = _COLLIDER_FIELDS | {
        "thickness_inner",
        "damping_factor",
        "friction_factor",
        "absorption",
        "permeability",
        "stickiness",
    }
    return {
        **_modifier_info(obj, modifier),
        "settings": read_fields(settings, all_fields),
        "non_cloth_field_applicability": {
            "thickness_inner": "soft_body_only",
            "damping_factor": "particle_only",
            "friction_factor": "particle_only",
            "permeability": "particle_only",
            "absorption": "force_effector",
            "stickiness": "not_documented_as_cloth_specific",
        },
    }


def _edge_lengths(obj):
    lengths = []
    for edge in obj.data.edges:
        a, b = edge.vertices
        lengths.append(float((obj.data.vertices[a].co - obj.data.vertices[b].co).length))
    if not lengths:
        return {"count": 0, "min": None, "max": None, "mean": None, "median": None, "ratio": None}
    minimum = min(lengths)
    maximum = max(lengths)
    return {
        "count": len(lengths),
        "min": minimum,
        "max": maximum,
        "mean": statistics.fmean(lengths),
        "median": statistics.median(lengths),
        "ratio": maximum / minimum if minimum > 0 else None,
    }


def _topology_summary(obj):
    edge_face_uses = Counter()
    for polygon in obj.data.polygons:
        vertices = list(polygon.vertices)
        for index, first in enumerate(vertices):
            second = vertices[(index + 1) % len(vertices)]
            edge_face_uses[tuple(sorted((first, second)))] += 1
    loose_edges = sum(edge_face_uses.get(tuple(sorted(edge.vertices)), 0) == 0 for edge in obj.data.edges)
    return {
        "triangles": sum(len(polygon.vertices) == 3 for polygon in obj.data.polygons),
        "quads": sum(len(polygon.vertices) == 4 for polygon in obj.data.polygons),
        "ngons": sum(len(polygon.vertices) > 4 for polygon in obj.data.polygons),
        "boundary_edges": sum(count == 1 for count in edge_face_uses.values()),
        "non_manifold_edges": sum(count != 2 for count in edge_face_uses.values()) + loose_edges,
        "loose_edges": loose_edges,
        "zero_area_faces": sum(polygon.area <= 1e-12 for polygon in obj.data.polygons),
    }


def _mesh_scale_context(obj):
    scenes = _object_scenes(obj)
    scene = scenes[0] if scenes else bpy.context.scene
    scale_length = float(scene.unit_settings.scale_length)
    local_area = sum(float(polygon.area) for polygon in obj.data.polygons)
    return {
        "scene": scene.name,
        "scene_unit_scale_length": scale_length,
        "base_surface_area_object_local_squared": local_area,
        "base_vertices_per_local_area": len(obj.data.vertices) / local_area if local_area > 0 else None,
        "edge_lengths_object_local": _edge_lengths(obj),
    }


class ClothInspectionAndSetupHandlers:
    """Blender-main-thread handlers for cloth simulation inspection and setup."""

    def get_cloth_simulation_info(
        self,
        scene_name,
        collection_name=None,
        object_limit=25,
        object_offset=0,
        dependency_limit=100,
        dependency_offset=0,
    ):
        scene, scope_objects, collection = _scene_scope(scene_name, collection_name)
        candidates = [
            obj
            for obj in sorted(scope_objects, key=lambda item: item.name)
            if any(modifier.type in {"CLOTH", "COLLISION"} for modifier in obj.modifiers)
        ]
        o_start, o_end, o_truncated, o_next = paginate(len(candidates), object_offset, object_limit, 200)
        records = []
        dependencies = []
        for obj in candidates[o_start:o_end]:
            cloth = [modifier for modifier in obj.modifiers if modifier.type == "CLOTH"]
            collision = [modifier for modifier in obj.modifiers if modifier.type == "COLLISION"]
            record = {
                "object": obj.name,
                "type": obj.type,
                "collections": sorted(item.name for item in obj.users_collection),
                "transform": _native_transform(obj),
                "animation": _animation_info(obj),
                "cloth": [_cloth_info(obj, modifier, scene) for modifier in cloth],
                "colliders": [_collider_info(obj, modifier) for modifier in collision],
            }
            records.append(record)
            for modifier in cloth:
                settings = modifier.settings
                collisions = modifier.collision_settings
                names = {
                    "vertex_group": [
                        getattr(settings, name, "")
                        for name in (
                            "vertex_group_mass",
                            "vertex_group_structural_stiffness",
                            "vertex_group_shear_stiffness",
                            "vertex_group_bending",
                            "vertex_group_shrink",
                            "vertex_group_pressure",
                            "vertex_group_intern",
                        )
                    ]
                    + [
                        getattr(collisions, "vertex_group_object_collisions", ""),
                        getattr(collisions, "vertex_group_self_collisions", ""),
                    ],
                    "collision_collection": [collisions.collection.name] if collisions.collection else [],
                    "effector_collection": (
                        [settings.effector_weights.collection.name] if settings.effector_weights.collection else []
                    ),
                }
                for kind, values in names.items():
                    for value in values:
                        if value:
                            if kind == "vertex_group":
                                exists = obj.vertex_groups.get(value) is not None
                            elif kind == "collision_collection":
                                exists = _collection_in_scene(collisions.collection, scene)
                            else:
                                exists = _collection_in_scene(settings.effector_weights.collection, scene)
                            dependencies.append(
                                {
                                    "cloth_object": obj.name,
                                    "cloth_modifier": modifier.name,
                                    "kind": kind,
                                    "name": value,
                                    "missing": not exists,
                                }
                            )
                rest_shape_key = serialize(getattr(settings, "rest_shape_key", ""))
                if rest_shape_key:
                    shape_keys = getattr(getattr(obj.data, "shape_keys", None), "key_blocks", None)
                    dependencies.append(
                        {
                            "cloth_object": obj.name,
                            "cloth_modifier": modifier.name,
                            "kind": "rest_shape_key",
                            "name": rest_shape_key,
                            "missing": shape_keys is None or shape_keys.get(rest_shape_key) is None,
                        }
                    )
                collision_candidates = collisions.collection.all_objects if collisions.collection else scene.objects
                for candidate in collision_candidates:
                    if any(item.type == "COLLISION" for item in candidate.modifiers):
                        collider_enabled = bool(candidate.collision and candidate.collision.use)
                        dependencies.append(
                            {
                                "cloth_object": obj.name,
                                "cloth_modifier": modifier.name,
                                "kind": "eligible_collider",
                                "name": candidate.name,
                                "excluded_by_collection": False,
                                "cloth_collision_enabled": collisions.use_collision,
                                "relationship": "ACTIVE"
                                if collisions.use_collision and collider_enabled
                                else "IN_SCOPE_BUT_DISABLED",
                            }
                        )

        d_start, d_end, d_truncated, d_next = paginate(len(dependencies), dependency_offset, dependency_limit, 1000)
        return {
            "scene": scene.name,
            "collection": collection.name if collection else None,
            "frame_observed": scene.frame_current,
            "objects": records,
            "object_page": {
                "total": len(candidates),
                "offset": o_start,
                "returned_count": len(records),
                "truncated": o_truncated,
                "next_offset": o_next,
            },
            "dependencies": dependencies[d_start:d_end],
            "dependency_page": {
                "total": len(dependencies),
                "offset": d_start,
                "returned_count": d_end - d_start,
                "truncated": d_truncated,
                "next_offset": d_next,
            },
        }

    def get_cloth_object_info(self, object_name, vertex_group_limit=50, vertex_group_offset=0):
        obj = get_object(object_name)
        if obj.type == "MESH":
            sync_from_editmode(obj)
        relevant = [modifier for modifier in obj.modifiers if modifier.type in {"CLOTH", "COLLISION"}]
        if not relevant:
            raise ValueError(f"Object '{object_name}' has no Cloth or Collision modifier")
        scene = next((scene for scene in bpy.data.scenes if obj.name in scene.objects), bpy.context.scene)
        groups = list(obj.vertex_groups) if obj.type == "MESH" else []
        start, end, truncated, next_offset = paginate(len(groups), vertex_group_offset, vertex_group_limit, 500)
        result = {
            "object": obj.name,
            "object_type": obj.type,
            "data_type": type(obj.data).__name__ if obj.data else None,
            "collections": sorted(collection.name for collection in obj.users_collection),
            "transform": _native_transform(obj),
            "dimensions_world_aligned": list(obj.dimensions),
            "animation": _animation_info(obj),
            "modifier_stack": [_modifier_info(obj, modifier) for modifier in obj.modifiers],
            "shape_keys": _shape_keys(obj) if obj.type == "MESH" else [],
            "cloth": [_cloth_info(obj, modifier, scene) for modifier in relevant if modifier.type == "CLOTH"],
            "colliders": [_collider_info(obj, modifier) for modifier in relevant if modifier.type == "COLLISION"],
            "vertex_groups": [_vertex_group_stats(obj, group) for group in groups[start:end]],
            "vertex_group_page": {
                "total": len(groups),
                "offset": start,
                "returned_count": end - start,
                "truncated": truncated,
                "next_offset": next_offset,
            },
        }
        if obj.type == "MESH":
            result["base_geometry"] = {
                "coordinate_space": "OBJECT_LOCAL",
                "vertices": len(obj.data.vertices),
                "edges": len(obj.data.edges),
                "faces": len(obj.data.polygons),
                "edge_lengths": _edge_lengths(obj),
                "topology": _topology_summary(obj),
            }
            result["evaluated_geometry"] = _evaluated_counts(obj)
        return result

    def add_cloth_simulation(
        self,
        object_name,
        modifier_name="Cloth",
        modifier_index=None,
        existing_policy="ERROR",
        cache_frame_start=1,
        cache_frame_end=250,
        collision_collection_name=None,
        preset=None,
        material=None,
        solver=None,
        collisions=None,
    ):
        from ._ownership import _tag_owned_component

        obj = get_object(object_name, {"MESH"})
        sync_from_editmode(obj)
        if not obj.data.vertices or not obj.data.edges:
            raise ValueError(f"Mesh '{object_name}' must have nonempty vertices and edges")
        if any(not math.isfinite(value) or value == 0 for value in obj.scale):
            raise ValueError(f"Mesh '{object_name}' has an invalid zero or non-finite scale")
        if existing_policy not in {"ERROR", "REUSE"}:
            raise ValueError("existing_policy must be ERROR or REUSE")
        if cache_frame_start > cache_frame_end:
            raise ValueError("cache_frame_start must be <= cache_frame_end")
        collection = None
        if collision_collection_name:
            collection = bpy.data.collections.get(collision_collection_name)
            if collection is None:
                raise ValueError(f"Collection not found: {collision_collection_name}")
            if not any(_collection_in_scene(collection, scene) for scene in _object_scenes(obj)):
                raise ValueError(
                    f"Collection '{collision_collection_name}' is not linked to a scene containing '{object_name}'"
                )
        existing = obj.modifiers.get(modifier_name)
        created = False
        if existing:
            if existing.type != "CLOTH":
                raise ValueError(f"Modifier '{modifier_name}' already exists and is not Cloth")
            if existing_policy == "ERROR":
                raise ValueError(f"Cloth modifier '{modifier_name}' already exists on '{object_name}'")
            modifier = existing
            _reject_baked([(obj, modifier)])
        else:
            duplicate_cloth = [item.name for item in obj.modifiers if item.type == "CLOTH"]
            if duplicate_cloth:
                raise ValueError(
                    f"Object '{object_name}' already has Cloth modifiers {duplicate_cloth}; reuse one by exact name"
                )
            modifier = obj.modifiers.new(name=modifier_name, type="CLOTH")
            created = True
            bpy.context.view_layer.update()
        original_index = list(obj.modifiers).index(modifier)
        old_cache_range = (modifier.point_cache.frame_start, modifier.point_cache.frame_end)
        old_collision_collection = modifier.collision_settings.collection
        material_changes = {}
        solver_changes = {}
        collision_changes = {}
        ownership = None
        try:
            if modifier.settings is None or modifier.collision_settings is None or modifier.point_cache is None:
                raise RuntimeError("Blender did not initialize Cloth settings after dependency-graph update")
            if modifier_index is not None:
                if not 0 <= modifier_index < len(obj.modifiers):
                    raise ValueError(f"modifier_index must be in [0, {len(obj.modifiers) - 1}]")
                obj.modifiers.move(list(obj.modifiers).index(modifier), modifier_index)
            material_changes = self._configure_material(obj, modifier, material, preset)
            solver_changes = patch_rna(modifier.settings, solver or {}, _SOLVER_FIELDS)
            collision_patch = dict(collisions or {})
            if collision_collection_name:
                collision_patch["collection_name"] = collision_collection_name
            collision_changes = self._configure_collisions(obj, modifier, collision_patch)
            cache = modifier.point_cache
            for name, value in (("frame_start", cache_frame_start), ("frame_end", cache_frame_end)):
                validate_rna_value(cache, name, value)
            set_cache_frame_range(cache, cache_frame_start, cache_frame_end)
            if collection is not None:
                modifier.collision_settings.collection = collection
            if created:
                ownership = _tag_owned_component(obj, modifier, "cloth")
            _tag_update(obj)
        except Exception:
            if ownership is not None:
                with contextlib.suppress(Exception):
                    del obj[ownership["object_property"]]
            if created:
                with contextlib.suppress(Exception):
                    obj.modifiers.remove(modifier)
            elif not created:
                restore_rna(modifier.settings, material_changes)
                restore_rna(modifier.settings, solver_changes)
                restore_rna(modifier.collision_settings, collision_changes)
                modifier.collision_settings.collection = old_collision_collection
                set_cache_frame_range(modifier.point_cache, *old_cache_range)
                with contextlib.suppress(Exception):
                    obj.modifiers.move(list(obj.modifiers).index(modifier), original_index)
            raise
        return {
            "changed_objects": [obj.name],
            "object": obj.name,
            "modifier": modifier.name,
            "created": created,
            "modifier_index": list(obj.modifiers).index(modifier),
            "material_changes": material_changes,
            "solver_changes": solver_changes,
            "collision_changes": collision_changes,
            "point_cache": _cache_info(modifier.point_cache),
            "collision_collection": modifier.collision_settings.collection.name
            if modifier.collision_settings.collection
            else None,
            "retained_live_dependencies": True,
            "ownership": ownership,
            "scale_and_density_context": _mesh_scale_context(obj),
            "warnings": self._scale_warnings(obj),
        }
