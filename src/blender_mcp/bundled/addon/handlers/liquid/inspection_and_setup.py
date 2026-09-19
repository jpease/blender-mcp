# pyright: reportArgumentType=false, reportGeneralTypeIssues=false
# Blender RNA objects are dynamically generated; this module runs only inside Blender.
"""Blender-main-thread handlers for liquid domain inspection, setup, and validation."""

from __future__ import annotations

import contextlib
import math
import os
import pathlib
import uuid

from collections import Counter

import bpy
import mathutils

from ...helpers import paginate, sync_from_editmode
from ._geometry import _cube_geometry
from .manifest import read_manifest
from .manifest import register_objects as register_manifest_objects

_DOMAIN_FIELDS = {
    "resolution_max",
    "time_scale",
    "timesteps_min",
    "timesteps_max",
    "use_adaptive_timesteps",
    "cfl_condition",
    "simulation_method",
    "flip_ratio",
    "particle_randomness",
    "particle_number",
    "particle_min",
    "particle_max",
    "particle_radius",
    "particle_band_width",
    "use_fractions",
    "fractions_threshold",
    "fractions_distance",
    "delete_in_obstacle",
    "sys_particle_maximum",
}
# `use_flip_particles` is deliberately excluded from _DOMAIN_FIELDS. Verified against Blender 5.2.1:
# its RNA setter ignores the assigned value and *toggles* the FLIP particle system on every
# assignment (particle_systems count flips 1<->0 whether you write True or False), so routing it
# through the generic idempotent _patch_rna path would turn the feature off for a caller who asked
# for it to be on. _set_flip_particles below wraps it back into set-to-desired-state semantics.
_FLIP_PARTICLES_FIELD = "use_flip_particles"
_GAS_DOMAIN_FIELDS = {
    "resolution_max",
    "time_scale",
    "timesteps_min",
    "timesteps_max",
    "use_adaptive_timesteps",
    "cfl_condition",
    "vorticity",
    "burning_rate",
    "flame_smoke",
    "flame_vorticity",
    "use_noise",
    "noise_scale",
}
_FLOW_FIELDS = {
    "flow_behavior",
    "use_inflow",
    "use_plane_init",
    "surface_distance",
    "subframes",
    "use_initial_velocity",
    "velocity_coord",
    "velocity_factor",
    "velocity_normal",
    "velocity_random",
    "density_vertex_group",
}
# Stable identity for every object the liquid tools create or adopt. UUID and role are custom
# properties so a later rename cannot orphan a domain's dependencies; the same pair is mirrored into
# the cache-side manifest so a setup remains resolvable after a scene reload.
_LIQUID_UUID_PROPERTY = "blendermcp_liquid_uuid"
_LIQUID_ROLE_PROPERTY = "blendermcp_liquid_role"
_LIQUID_ROLES = frozenset(
    {
        "DOMAIN",
        "FLOW",
        "EFFECTOR",
        "GUIDE",
        # `create_liquid_proxy_rig` has always written these two exact strings to
        # `blendermcp_liquid_role`; they stay verbatim so existing scenes keep resolving.
        "FLOW_PROXY",
        "EFFECTOR_PROXY",
        "CONTAINER_VOLUME",
        "SPILL_VOLUME",
    }
)
# Real FluidFlowSettings properties that Blender only honours for particle-system smoke/fire
# emission. They are rejected with an explanation rather than silently allowed on a liquid surface.
_SMOKE_ONLY_FLOW_FIELDS = {"use_particle_size", "particle_size"}
_GAS_FLOW_FIELDS = _FLOW_FIELDS | {"density", "fuel_amount", "smoke_color", "temperature"}
_EFFECTOR_FIELDS = {
    "use_effector",
    "effector_type",
    "use_plane_init",
    "surface_distance",
    "subframes",
    "guide_mode",
    "velocity_factor",
}
_BOUNDARY_FIELDS = {
    "front": "use_collision_border_front",
    "back": "use_collision_border_back",
    "left": "use_collision_border_left",
    "right": "use_collision_border_right",
    "top": "use_collision_border_top",
    "bottom": "use_collision_border_bottom",
}
_CACHE_FLAGS = (
    "has_cache_baked_data",
    "has_cache_baked_noise",
    "has_cache_baked_mesh",
    "has_cache_baked_particles",
    "has_cache_baked_guide",
    "has_cache_baked_any",
    "is_cache_baking_data",
    "is_cache_baking_noise",
    "is_cache_baking_mesh",
    "is_cache_baking_particles",
    "is_cache_baking_guide",
    "is_cache_baking_any",
)
_CACHE_FIELDS = (
    "cache_directory",
    "cache_type",
    "cache_data_format",
    "cache_mesh_format",
    "cache_particle_format",
    "openvdb_cache_compress_type",
    "cache_frame_start",
    "cache_frame_end",
    "cache_frame_offset",
    "cache_frame_pause_data",
    "cache_frame_pause_noise",
    "cache_frame_pause_mesh",
    "cache_frame_pause_particles",
    "cache_frame_pause_guide",
    "cache_resumable",
)
_DOMAIN_INSPECTION_FIELDS = (
    _DOMAIN_FIELDS
    | {
        "domain_type",
        "use_mesh",
        "mesh_scale",
        "mesh_particle_radius",
        "use_speed_vectors",
        "use_spray_particles",
        "use_foam_particles",
        "use_bubble_particles",
        "use_tracer_particles",
        "particle_scale",
        "use_diffusion",
        "use_viscosity",
        "viscosity_base",
        "viscosity_exponent",
        "viscosity_value",
        "surface_tension",
        "use_guide",
        "guide_source",
        "guide_parent",
        "guide_alpha",
        "guide_beta",
        "guide_vel_factor",
    }
    | set(_CACHE_FIELDS)
    | set(_CACHE_FLAGS)
    | set(_BOUNDARY_FIELDS.values())
)
_TOPOLOGY_MODIFIERS = {
    "ARRAY",
    "BEVEL",
    "BOOLEAN",
    "BUILD",
    "DECIMATE",
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
        return {"id_type": type(value).__name__, "name": value.name}
    try:
        return [_serialize(item) for item in value]
    except TypeError:
        return str(value)


def _read_fields(owner, fields):
    return {name: _serialize(getattr(owner, name)) for name in sorted(fields) if hasattr(owner, name)}


def _get_object(name, types=None):
    obj = bpy.data.objects.get(name)
    if obj is None:
        raise ValueError(f"Object not found: {name}")
    if types and obj.type not in types:
        raise ValueError(f"Object '{name}' must be one of {sorted(types)} (type={obj.type})")
    return obj


def _get_scene(name):
    scene = bpy.data.scenes.get(name)
    if scene is None:
        raise ValueError(f"Scene not found: {name}")
    return scene


def _get_fluid_modifier(obj, modifier_name, role=None):
    modifier = obj.modifiers.get(modifier_name)
    if modifier is None:
        raise ValueError(f"Modifier not found: {modifier_name} on '{obj.name}'")
    if modifier.type != "FLUID":
        raise ValueError(f"Modifier '{modifier_name}' on '{obj.name}' is {modifier.type}, not FLUID")
    if role and modifier.fluid_type != role:
        raise ValueError(f"Fluid modifier '{modifier_name}' is {modifier.fluid_type}, not {role}")
    return modifier


def _get_domain(object_name, modifier_name=None, expected_domain_type="LIQUID"):
    obj = _get_object(object_name, {"MESH"})
    candidates = [
        modifier for modifier in obj.modifiers if modifier.type == "FLUID" and modifier.fluid_type == "DOMAIN"
    ]
    if modifier_name is not None:
        modifier = _get_fluid_modifier(obj, modifier_name, "DOMAIN")
    elif len(candidates) == 1:
        modifier = candidates[0]
    elif not candidates:
        raise ValueError(f"Object '{object_name}' has no fluid domain modifier")
    else:
        raise ValueError(f"Object '{object_name}' has multiple domain modifiers; provide modifier_name")
    settings = modifier.domain_settings
    if settings is None or settings.domain_type != expected_domain_type:
        raise ValueError(
            f"Modifier '{modifier.name}' on '{obj.name}' is not an initialized {expected_domain_type.lower()} domain"
        )
    return obj, modifier, settings


def _get_role(object_name, modifier_name, role):
    obj = _get_object(object_name, {"MESH"})
    modifier = _get_fluid_modifier(obj, modifier_name, role)
    settings = modifier.flow_settings if role == "FLOW" else modifier.effector_settings
    if settings is None:
        raise RuntimeError(f"Blender did not initialize {role.lower()} settings for '{obj.name}:{modifier.name}'")
    if role == "FLOW" and settings.flow_type != "LIQUID":
        raise ValueError(f"Flow '{obj.name}:{modifier.name}' is {settings.flow_type}, not LIQUID")
    return obj, modifier, settings


def _finite(value, label):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not math.isfinite(value):
        raise ValueError(f"{label} must be finite")
    if isinstance(value, (list, tuple)) and not all(
        isinstance(item, (int, float)) and math.isfinite(item) for item in value
    ):
        raise ValueError(f"{label} must contain only finite numbers")
    return value


def _rna_property(owner, name):
    prop = owner.bl_rna.properties.get(name)
    if prop is None or prop.is_readonly:
        raise ValueError(f"Blender {bpy.app.version_string} does not expose writable {type(owner).__name__}.{name}")
    return prop


def _validate_rna_value(owner, name, value):
    prop = _rna_property(owner, name)
    _finite(value, name)
    is_array = getattr(prop, "is_array", False)
    if prop.type in {"FLOAT", "INT"} and not is_array and not (prop.hard_min <= value <= prop.hard_max):
        raise ValueError(f"{name}={value} is outside Blender's RNA range [{prop.hard_min}, {prop.hard_max}]")
    if prop.type == "ENUM" and value not in {item.identifier for item in prop.enum_items}:
        raise ValueError(f"Invalid {name}: {value}")
    if is_array and len(value) != prop.array_length:
        raise ValueError(f"{name} must contain {prop.array_length} values")
    return value


def _patch_rna(owner, patch, allowed):
    patch = patch or {}
    unknown = set(patch) - allowed
    if unknown:
        raise ValueError(f"Unsupported properties: {sorted(unknown)}")
    validated = {name: _validate_rna_value(owner, name, value) for name, value in patch.items()}
    old = {name: _serialize(getattr(owner, name)) for name in validated}
    try:
        for name, value in validated.items():
            setattr(owner, name, value)
    except Exception:
        for name, value in old.items():
            with contextlib.suppress(Exception):
                setattr(owner, name, value)
        raise
    return {name: {"old": old[name], "new": _serialize(getattr(owner, name))} for name in validated}


def _restore_rna(owner, changes):
    for name, values in changes.items():
        with contextlib.suppress(Exception):
            setattr(owner, name, values["old"])


def _set_flip_particles(settings, desired):
    """
    Give ``use_flip_particles`` set-to-desired-state semantics instead of Blender's toggle.

    Verified against Blender 5.2.1: assigning this property ignores the assigned value and flips the
    FLIP particle system on or off, so writing ``True`` to an already-enabled domain disables it.
    Writing only when the current state differs makes the operation idempotent, which every other
    liquid patch in this module relies on.
    """
    desired = bool(desired)
    old = bool(getattr(settings, "use_flip_particles", False))
    if old != desired:
        settings.use_flip_particles = desired
        if bool(settings.use_flip_particles) != desired:
            raise ValueError(
                f"Blender did not accept use_flip_particles={desired}; it reads back as "
                f"{bool(settings.use_flip_particles)}"
            )
    return {"old": old, "new": bool(settings.use_flip_particles)}


def _reject_baked(settings):
    active = [name for name in _CACHE_FLAGS if getattr(settings, name, False)]
    if active:
        raise ValueError(
            "Cannot change a baked or baking liquid domain. Free the exact cache stages first: " + ", ".join(active)
        )


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
            "matrix": [list(row) for row in obj.matrix_world],
            "determinant": float(obj.matrix_world.to_3x3().determinant()),
        },
    }


def _world_bounds(obj, evaluated=True):
    target = obj.evaluated_get(bpy.context.evaluated_depsgraph_get()) if evaluated else obj
    corners = [target.matrix_world @ mathutils.Vector(corner) for corner in target.bound_box]
    minimum = [min(float(point[axis]) for point in corners) for axis in range(3)]
    maximum = [max(float(point[axis]) for point in corners) for axis in range(3)]
    return {
        "coordinate_space": "WORLD",
        "minimum": minimum,
        "maximum": maximum,
        "dimensions": [maximum[axis] - minimum[axis] for axis in range(3)],
    }


def _domain_bounds(obj, settings):
    """
    Return the domain container bounds and, once baked, the evaluated liquid-mesh bounds.

    A liquid DOMAIN modifier replaces the object's evaluated mesh with the generated liquid surface
    once data/mesh caching exists, so ``evaluated_get`` no longer describes the container - it
    describes the fluid itself. Domain-shape checks (containment, cell sizing, scale) must use the
    base (``evaluated=False``) bounds; only report the evaluated bounds when a bake actually exists.
    """
    baked = any(getattr(settings, name, False) for name in ("has_cache_baked_data", "has_cache_baked_mesh"))
    return {
        "base_domain_bounds": _world_bounds(obj, evaluated=False),
        "evaluated_liquid_bounds": _world_bounds(obj, evaluated=True) if baked else None,
    }


def _bounds_overlap(first, second, tolerance=0.0):
    return all(
        first["minimum"][axis] <= second["maximum"][axis] + tolerance
        and first["maximum"][axis] + tolerance >= second["minimum"][axis]
        for axis in range(3)
    )


def _bounds_contains(outer, inner, tolerance=0.0):
    return all(
        outer["minimum"][axis] - tolerance <= inner["minimum"][axis]
        and inner["maximum"][axis] <= outer["maximum"][axis] + tolerance
        for axis in range(3)
    )


def _evaluated_counts(obj):
    evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    mesh = evaluated.to_mesh()
    try:
        return {"vertices": len(mesh.vertices), "edges": len(mesh.edges), "faces": len(mesh.polygons)}
    finally:
        evaluated.to_mesh_clear()


def _modifier_info(obj, modifier):
    return {
        "name": modifier.name,
        "type": modifier.type,
        "fluid_type": modifier.fluid_type,
        "index": list(obj.modifiers).index(modifier),
        "show_viewport": modifier.show_viewport,
        "show_render": modifier.show_render,
    }


def _animation_info(obj):
    result = []
    for label, owner in (("OBJECT", obj), ("DATA", getattr(obj, "data", None))):
        animation = getattr(owner, "animation_data", None)
        action = getattr(animation, "action", None)
        if action is not None:
            result.append({"owner": label, "action": action.name})
        drivers = getattr(animation, "drivers", ()) if animation is not None else ()
        if drivers:
            result.append({"owner": label, "drivers": [curve.data_path for curve in list(drivers)[:100]]})
    return result


def _domain_info(obj, modifier, settings):
    runtime = {}
    for name in ("cell_size", "domain_resolution"):
        with contextlib.suppress(Exception):
            runtime[name] = _serialize(getattr(settings, name))
    fields = _read_fields(settings, _DOMAIN_INSPECTION_FIELDS)
    if any(
        getattr(settings, name, False)
        for name in ("use_spray_particles", "use_foam_particles", "use_bubble_particles", "use_tracer_particles")
    ):
        fields["cache_particle_format"] = _serialize(settings.cache_particle_format)
    identity = _liquid_object_identity(obj)
    return {
        "object": obj.name,
        "domain_uuid": identity["uuid"],
        "role": identity["role"],
        "owned_objects": _owned_object_registry(settings),
        "modifier": _modifier_info(obj, modifier),
        "transform": _native_transform(obj),
        **_domain_bounds(obj, settings),
        "animation": _animation_info(obj),
        "settings": fields,
        "scope": {
            "flow_collection": settings.fluid_group.name if settings.fluid_group else None,
            "effector_collection": settings.effector_group.name if settings.effector_group else None,
            "force_collection": settings.force_collection.name if settings.force_collection else None,
        },
        "runtime": runtime,
    }


def _flow_info(obj, modifier, settings):
    return {
        "object": obj.name,
        "modifier": _modifier_info(obj, modifier),
        "settings": {
            "flow_type": settings.flow_type,
            "flow_source": settings.flow_source,
            **_read_fields(settings, _FLOW_FIELDS),
        },
    }


def _effector_info(obj, modifier, settings):
    return {
        "object": obj.name,
        "modifier": _modifier_info(obj, modifier),
        "settings": _read_fields(settings, _EFFECTOR_FIELDS),
    }


def _scene_has_collection(scene, collection):
    return collection == scene.collection or collection in scene.collection.children_recursive


def _object_in_collection(obj, collection):
    return collection is not None and obj.name in collection.all_objects


def _domain_dependencies(scene, domain_obj, settings):
    records = []
    roles = (("FLOW", settings.fluid_group), ("EFFECTOR", settings.effector_group))
    for role, collection in roles:
        role_objects = set()
        for obj in sorted(scene.objects, key=lambda item: item.name):
            modifiers = [mod for mod in obj.modifiers if mod.type == "FLUID" and mod.fluid_type == role]
            if modifiers:
                role_objects.add(obj.name)
            for modifier in modifiers:
                role_settings = modifier.flow_settings if role == "FLOW" else modifier.effector_settings
                liquid_compatible = role != "FLOW" or (
                    role_settings is not None and role_settings.flow_type == "LIQUID"
                )
                records.append(
                    {
                        "domain": domain_obj.name,
                        "kind": role.lower(),
                        "object": obj.name,
                        **_liquid_object_identity(obj),
                        "modifier": modifier.name,
                        "collection": collection.name if collection else None,
                        "in_scope": collection is None or _object_in_collection(obj, collection),
                        "liquid_compatible": liquid_compatible,
                        "missing_settings": role_settings is None,
                    }
                )
        if collection is not None:
            for obj in sorted(collection.all_objects, key=lambda item: item.name):
                if obj.name not in role_objects:
                    records.append(
                        {
                            "domain": domain_obj.name,
                            "kind": role.lower(),
                            "object": obj.name,
                            **_liquid_object_identity(obj),
                            "modifier": None,
                            "collection": collection.name,
                            "in_scope": True,
                            "liquid_compatible": False if role == "FLOW" else None,
                            "missing_settings": True,
                            "missing_role_modifier": True,
                        }
                    )
    collection = settings.force_collection
    candidates = collection.all_objects if collection else scene.objects
    for obj in sorted(candidates, key=lambda item: item.name):
        field = getattr(obj, "field", None)
        if field is not None and getattr(field, "type", "NONE") != "NONE":
            records.append(
                {
                    "domain": domain_obj.name,
                    "kind": "force",
                    "object": obj.name,
                    "field_type": field.type,
                    "collection": collection.name if collection else None,
                    "in_scope": True,
                }
            )
    return records


def _ensure_collection(scene, name):
    collection = bpy.data.collections.get(name)
    created = False
    linked = False
    if collection is None:
        collection = bpy.data.collections.new(name)
        created = True
    if not _scene_has_collection(scene, collection):
        scene.collection.children.link(collection)
        linked = True
    return collection, created, linked


def _link_object(collection, obj):
    if obj.name in collection.objects:
        return False
    collection.objects.link(obj)
    return True


def _validate_dimensions(values, label):
    _finite(values, label)
    if len(values) != 3 or any(value <= 0 for value in values):
        raise ValueError(f"{label} must contain three positive finite values")


def _ensure_liquid_uuid(obj):
    """Return the object's stable liquid UUID, assigning one the first time it's needed."""
    existing = obj.get(_LIQUID_UUID_PROPERTY)
    if isinstance(existing, str) and existing:
        return existing
    new_uuid = str(uuid.uuid4())
    obj[_LIQUID_UUID_PROPERTY] = new_uuid
    return new_uuid


def _tag_liquid_object(obj, role):
    """
    Give a liquid-owned object a stable UUID and role, returning a rollback token.

    ``ObjectState`` does not snapshot custom properties, so a caller that fails after tagging must
    pass the returned token to :func:`_untag_liquid_object` from its own rollback path.
    """
    if role not in _LIQUID_ROLES:
        raise ValueError(f"Unknown liquid object role: {role}")
    had_uuid = isinstance(obj.get(_LIQUID_UUID_PROPERTY), str) and bool(obj.get(_LIQUID_UUID_PROPERTY))
    previous_role = obj.get(_LIQUID_ROLE_PROPERTY)
    object_uuid = _ensure_liquid_uuid(obj)
    obj[_LIQUID_ROLE_PROPERTY] = role
    return {
        "object": obj,
        "uuid": object_uuid,
        "role": role,
        "had_uuid": had_uuid,
        "previous_role": previous_role,
    }


def _untag_liquid_object(token):
    """Undo a :func:`_tag_liquid_object` call, best effort."""
    if not token:
        return
    obj = token["object"]
    with contextlib.suppress(Exception):
        if not token["had_uuid"]:
            del obj[_LIQUID_UUID_PROPERTY]  # pyright: ignore[reportArgumentType]
    with contextlib.suppress(Exception):
        if token["previous_role"] is None:
            del obj[_LIQUID_ROLE_PROPERTY]  # pyright: ignore[reportArgumentType]
        else:
            obj[_LIQUID_ROLE_PROPERTY] = token["previous_role"]


def _liquid_object_identity(obj):
    """Report an object's recorded liquid UUID and role without assigning either."""
    recorded_uuid = obj.get(_LIQUID_UUID_PROPERTY)
    recorded_role = obj.get(_LIQUID_ROLE_PROPERTY)
    return {
        "uuid": recorded_uuid if isinstance(recorded_uuid, str) and recorded_uuid else None,
        "role": recorded_role if isinstance(recorded_role, str) and recorded_role else None,
    }


def _find_object_by_liquid_uuid(object_uuid):
    """
    Resolve an object by its recorded liquid UUID, which survives renames as names do not.

    Objects are scanned rather than read from the cache-side manifest because the manifest stores
    names only as a human-readable convenience; the scene remains the authority on identity.
    """
    if not isinstance(object_uuid, str) or not object_uuid:
        raise ValueError("object_uuid must be a non-empty string")
    matches = [obj for obj in bpy.data.objects if obj.get(_LIQUID_UUID_PROPERTY) == object_uuid]
    if not matches:
        raise ValueError(f"No object carries liquid UUID '{object_uuid}'")
    if len(matches) > 1:
        raise ValueError(
            f"Liquid UUID '{object_uuid}' is recorded on {len(matches)} objects "
            f"({', '.join(sorted(item.name for item in matches))}); the scene is inconsistent"
        )
    return matches[0]


def _owned_object_registry(settings):
    """Report the manifest's UUID -> {name, role} registry for a domain, or None when unavailable."""
    directory = getattr(settings, "cache_directory", None)
    if not directory:
        return None
    try:
        resolved = _resolved_cache_path(directory)
    except ValueError:
        return None
    manifest = read_manifest(resolved)
    if not manifest:
        return None
    objects = manifest.get("objects")
    return objects if isinstance(objects, dict) else None


def _register_owned_objects(domain_settings, domain_uuid, entries):
    """
    Record owned (uuid, name, role) triples in the domain's cache-side manifest, best effort.

    The manifest is bookkeeping written outside ``mutation_transaction``; a missing or read-only
    cache directory must never fail an otherwise successful scene mutation, so failures are absorbed
    and reported as an unwritten manifest instead.
    """
    directory = getattr(domain_settings, "cache_directory", None)
    if not directory or not domain_uuid:
        return None
    try:
        resolved = _resolved_cache_path(directory)
    except ValueError:
        return None
    if not os.path.isdir(resolved):
        return None
    return register_manifest_objects(resolved, domain_uuid, entries)


def _resolved_cache_path(path):
    if not isinstance(path, str) or not path.strip():
        raise ValueError("cache_directory must be an explicit nonempty path")
    return os.path.normcase(os.path.normpath(bpy.path.abspath(path)))


def _check_unique_cache_path(settings, path):
    wanted = _resolved_cache_path(path)
    for obj in bpy.data.objects:
        for modifier in obj.modifiers:
            if modifier.type != "FLUID" or modifier.fluid_type != "DOMAIN":
                continue
            other = modifier.domain_settings
            if other is None or other is settings or not other.cache_directory:
                continue
            if _resolved_cache_path(other.cache_directory) == wanted:
                raise ValueError(f"Cache directory is already used by liquid domain '{obj.name}:{modifier.name}'")
    return wanted


def _topology_from_mesh(mesh):
    edge_faces = Counter()
    for polygon in mesh.polygons:
        vertices = list(polygon.vertices)
        for index, first in enumerate(vertices):
            edge_faces[tuple(sorted((first, vertices[(index + 1) % len(vertices)])))] += 1
    mesh_edges = [tuple(sorted(edge.vertices)) for edge in mesh.edges]
    boundary = sum(edge_faces.get(edge, 0) == 1 for edge in mesh_edges)
    non_manifold = sum(edge_faces.get(edge, 0) != 2 for edge in mesh_edges)
    return {"boundary_edges": boundary, "non_manifold_edges": non_manifold}


def _mesh_topology(obj):
    return _topology_from_mesh(obj.data)


def _evaluated_mesh_topology(obj):
    """
    Return topology counts for the mesh reaching the fluid modifier, not the base mesh.

    Boolean/array/mirror modifier stacks change the effective closed/open shape a flow or
    effector actually presents to Mantaflow; the base mesh can read manifold while the
    evaluated result does not, or vice versa.
    """
    evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    mesh = evaluated.to_mesh()
    try:
        return _topology_from_mesh(mesh)
    finally:
        evaluated.to_mesh_clear()


def _flow_normal_orientation(obj):
    """
    Sample evaluated face normals and report the fraction pointing toward the mesh centroid.

    Blender's liquid emission expects outward-facing normals; a majority-inward mesh (flipped
    normals, or geometry built inside-out) emits incorrectly with no other visible symptom.
    """
    evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    mesh = evaluated.to_mesh()
    try:
        if not mesh.polygons or not mesh.vertices:
            return None
        world_matrix = evaluated.matrix_world
        normal_matrix = world_matrix.to_3x3().inverted_safe().transposed()
        centroid = mathutils.Vector((0.0, 0.0, 0.0))
        for vertex in mesh.vertices:
            centroid += world_matrix @ vertex.co
        centroid /= len(mesh.vertices)
        sampled = 0
        inward = 0
        for polygon in mesh.polygons:
            outward = (world_matrix @ polygon.center) - centroid
            if outward.length_squared <= 1e-12:
                continue
            normal_world = normal_matrix @ polygon.normal
            if normal_world.length_squared <= 1e-12:
                continue
            sampled += 1
            if normal_world.dot(outward) < 0:
                inward += 1
        if sampled == 0:
            return None
        return {"faces_sampled": sampled, "inward_faces": inward, "inward_fraction": inward / sampled}
    finally:
        evaluated.to_mesh_clear()


def _wall_thickness_samples(obj, cell_size, max_samples=64):
    """
    Cast rays inward from sampled evaluated faces and return the measured wall thickness.

    ``BVHTree.FromObject`` builds its tree in the object's local space (vertex positions are
    used as-is, never multiplied by ``matrix_world``), so rays are cast in local space and hits
    are mapped back through ``matrix_world`` to measure thickness in world units.
    """
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = obj.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh()
    try:
        polygons = list(mesh.polygons)
        if not polygons:
            return []
        bvh = mathutils.bvhtree.BVHTree.FromObject(obj, depsgraph, deform=True, cage=False, epsilon=0.0)
        world_matrix = evaluated.matrix_world
        inverse = world_matrix.inverted_safe()
        inverse_3x3 = inverse.to_3x3()
        normal_matrix = world_matrix.to_3x3().inverted_safe().transposed()
        step = max(1, math.ceil(len(polygons) / max_samples))
        epsilon = max(cell_size * 1e-3, 1e-6)
        thicknesses = []
        for index in range(0, len(polygons), step):
            polygon = polygons[index]
            normal_world = normal_matrix @ polygon.normal
            if normal_world.length_squared <= 1e-12:
                continue
            normal_world.normalize()
            origin_world = world_matrix @ polygon.center
            start_world = origin_world - normal_world * epsilon
            start_local = inverse @ start_world
            direction_local = (inverse_3x3 @ -normal_world).normalized()
            location, _hit_normal, _hit_index, _distance = bvh.ray_cast(start_local, direction_local)
            if location is not None:
                hit_world = world_matrix @ location
                thicknesses.append((hit_world - start_world).length)
        return thicknesses
    finally:
        evaluated.to_mesh_clear()


def _warning_for_effector(obj):
    warnings = ["Mantaflow collision is one-way; liquid forces are not applied back to rigid bodies."]
    if obj.type == "MESH":
        counts = _evaluated_counts(obj)
        if counts["faces"] > 100_000:
            warnings.append("High evaluated collider face count; consider a dedicated low-resolution proxy.")
        bounds = _world_bounds(obj)
        dimensions = sorted(bounds["dimensions"])
        if dimensions[-1] > 0 and dimensions[0] / dimensions[-1] < 0.01:
            warnings.append("Very thin collision geometry may need plane initialization, more subframes, or a proxy.")
    if _animation_info(obj):
        warnings.append("Animated effectors need representative-frame collision review and may require more subframes.")
    return warnings


class LiquidInspectionAndSetupHandlers:
    """Provide production-oriented Mantaflow liquid inspection and setup handlers."""

    def get_liquid_simulation_info(
        self,
        scene_name=None,
        domain_object_name=None,
        domain_uuid=None,
        domain_limit=25,
        domain_offset=0,
        dependency_limit=100,
        dependency_offset=0,
    ):
        if scene_name is None and domain_object_name is None and domain_uuid is None:
            raise ValueError("Provide scene_name, domain_object_name, domain_uuid, or a combination")
        resolved_by_uuid = None
        if domain_uuid is not None:
            resolved_by_uuid = _find_object_by_liquid_uuid(domain_uuid)
            if domain_object_name is not None and resolved_by_uuid.name != domain_object_name:
                raise ValueError(
                    f"domain_uuid '{domain_uuid}' resolves to '{resolved_by_uuid.name}', not '{domain_object_name}'"
                )
            domain_object_name = resolved_by_uuid.name
        domain_filter = resolved_by_uuid or (_get_object(domain_object_name, {"MESH"}) if domain_object_name else None)
        if scene_name is not None:
            scene = _get_scene(scene_name)
            if domain_filter is not None and domain_filter.name not in scene.objects:
                raise ValueError(f"Domain '{domain_object_name}' is not linked to scene '{scene_name}'")
        else:
            scene = next((item for item in bpy.data.scenes if domain_filter.name in item.objects), None)
            if scene is None:
                raise ValueError(f"Domain '{domain_object_name}' is not linked to a scene")
        candidates = []
        for obj in sorted(scene.objects, key=lambda item: item.name):
            if domain_filter is not None and obj != domain_filter:
                continue
            for modifier in obj.modifiers:
                if modifier.type == "FLUID" and modifier.fluid_type == "DOMAIN":
                    settings = modifier.domain_settings
                    if settings is not None and settings.domain_type == "LIQUID":
                        candidates.append((obj, modifier, settings))
        start, end, truncated, next_offset = paginate(len(candidates), domain_offset, domain_limit, 100)
        page = candidates[start:end]
        dependencies = []
        for obj, _modifier, settings in page:
            dependencies.extend(_domain_dependencies(scene, obj, settings))
        dependency_start, dependency_end, dependency_truncated, dependency_next = paginate(
            len(dependencies), dependency_offset, dependency_limit, 1000
        )
        return {
            "scene": scene.name,
            "frame_observed": scene.frame_current,
            "resolved_by": "domain_uuid" if resolved_by_uuid is not None else "object_name",
            "domains": [_domain_info(*item) for item in page],
            "domain_page": {
                "total": len(candidates),
                "offset": start,
                "returned_count": len(page),
                "truncated": truncated,
                "next_offset": next_offset,
            },
            "dependencies": dependencies[dependency_start:dependency_end],
            "dependency_page": {
                "total": len(dependencies),
                "offset": dependency_start,
                "returned_count": dependency_end - dependency_start,
                "truncated": dependency_truncated,
                "next_offset": dependency_next,
            },
        }

    def get_fluid_object_info(self, object_name):
        obj = _get_object(object_name)
        if obj.type == "MESH":
            sync_from_editmode(obj)
        fluid = [modifier for modifier in obj.modifiers if modifier.type == "FLUID" and modifier.fluid_type != "NONE"]
        if not fluid:
            raise ValueError(f"Object '{object_name}' has no active fluid role")
        is_domain = any(modifier.fluid_type == "DOMAIN" for modifier in fluid)
        result = {
            "object": obj.name,
            "object_type": obj.type,
            "data_type": type(obj.data).__name__ if obj.data else None,
            "collections": sorted(collection.name for collection in obj.users_collection),
            "transform": _native_transform(obj),
            "dimensions_world_aligned": list(obj.dimensions),
            # A DOMAIN modifier's evaluated mesh is the generated liquid surface once baked, not the
            # container; use the base (unevaluated) bounds here and see domains[].evaluated_liquid_bounds.
            "bounds": _world_bounds(obj, evaluated=not is_domain),
            "animation": _animation_info(obj),
            "modifier_stack": [_modifier_info(obj, modifier) for modifier in obj.modifiers],
            "domains": [],
            "flows": [],
            "effectors": [],
        }
        if obj.type == "MESH":
            result["base_geometry"] = {
                "coordinate_space": "OBJECT_LOCAL",
                "vertices": len(obj.data.vertices),
                "edges": len(obj.data.edges),
                "faces": len(obj.data.polygons),
                "topology": _mesh_topology(obj),
            }
            result["evaluated_geometry"] = _evaluated_counts(obj)
        for modifier in fluid:
            if modifier.fluid_type == "DOMAIN" and modifier.domain_settings is not None:
                result["domains"].append(_domain_info(obj, modifier, modifier.domain_settings))
            elif modifier.fluid_type == "FLOW" and modifier.flow_settings is not None:
                result["flows"].append(_flow_info(obj, modifier, modifier.flow_settings))
            elif modifier.fluid_type == "EFFECTOR" and modifier.effector_settings is not None:
                result["effectors"].append(_effector_info(obj, modifier, modifier.effector_settings))
        return result

    def create_liquid_domain(
        self,
        scene_name,
        cache_directory,
        object_name=None,
        new_object_name="Liquid Domain",
        collection_name=None,
        dimensions=(4.0, 4.0, 4.0),
        location=(0.0, 0.0, 0.0),
        modifier_name="Liquid Domain",
        flow_collection_name=None,
        effector_collection_name=None,
        cache_type="REPLAY",
        cache_frame_start=1,
        cache_frame_end=250,
        resolution_max=64,
        simulation_method="FLIP",
        time_scale=1.0,
        use_adaptive_timesteps=True,
        timesteps_min=1,
        timesteps_max=4,
        cfl_condition=4.0,
    ):
        scene = _get_scene(scene_name)
        _validate_dimensions(dimensions, "dimensions")
        _finite(location, "location")
        if len(location) != 3:
            raise ValueError("location must contain three finite values")
        if cache_frame_start > cache_frame_end:
            raise ValueError("cache_frame_start must be <= cache_frame_end")
        if timesteps_min > timesteps_max:
            raise ValueError("timesteps_min must be <= timesteps_max")
        if cache_type not in {"REPLAY", "MODULAR", "ALL"}:
            raise ValueError("cache_type must be REPLAY, MODULAR, or ALL")
        created_object = object_name is None
        linked_collections = []
        old_collection_tags = []
        old_tag = None
        old_uuid = None
        old_role = None
        domain_uuid = None
        if created_object:
            target_collection_name = collection_name or "Liquid Domains"
            target_collection, _created, linked = _ensure_collection(scene, target_collection_name)
            if linked:
                linked_collections.append(target_collection)
            mesh = bpy.data.meshes.new(f"{new_object_name} Mesh")
            vertices, faces = _cube_geometry(dimensions)
            mesh.from_pydata(vertices, [], faces)
            mesh.update()
            obj = bpy.data.objects.new(new_object_name, mesh)
            target_collection.objects.link(obj)
            obj.location = location
        else:
            obj = _get_object(object_name, {"MESH"})
            if obj.name not in scene.objects:
                raise ValueError(f"Object '{object_name}' is not linked to scene '{scene_name}'")
            sync_from_editmode(obj)
            if not obj.data.vertices or not obj.data.polygons:
                raise ValueError(f"Domain mesh '{object_name}' must contain vertices and faces")
            if any(not math.isfinite(float(value)) or float(value) == 0.0 for value in obj.scale):
                raise ValueError(f"Domain mesh '{object_name}' has zero or non-finite scale")
            old_tag = obj.get("blendermcp_liquid_domain")
            old_uuid = obj.get(_LIQUID_UUID_PROPERTY)
            old_role = obj.get(_LIQUID_ROLE_PROPERTY)
        if any(mod.type == "FLUID" and mod.fluid_type == "DOMAIN" for mod in obj.modifiers):
            raise ValueError(f"Object '{obj.name}' already has a fluid domain modifier")
        modifier = None
        domain_changes = {}
        old_scope = None
        try:
            modifier = obj.modifiers.new(name=modifier_name, type="FLUID")
            modifier.fluid_type = "DOMAIN"
            bpy.context.view_layer.update()
            settings = modifier.domain_settings
            if settings is None:
                raise RuntimeError("Blender did not initialize FluidDomainSettings after dependency-graph update")
            settings.domain_type = "LIQUID"
            bpy.context.view_layer.update()
            resolved_cache_path = _check_unique_cache_path(settings, cache_directory)
            # Directory creation is idempotent and not part of the rollback below: a
            # bake-ready empty directory left behind after a failed domain setup is
            # harmless, unlike partially-written cache files.
            pathlib.Path(resolved_cache_path).mkdir(parents=True, exist_ok=True)
            flow_name = flow_collection_name or f"{obj.name} Flows"
            effector_name = effector_collection_name or f"{obj.name} Effectors"
            flow_collection, _created, flow_linked = _ensure_collection(scene, flow_name)
            effector_collection, _created, effector_linked = _ensure_collection(scene, effector_name)
            if flow_linked:
                linked_collections.append(flow_collection)
            if effector_linked:
                linked_collections.append(effector_collection)
            for collection in (flow_collection, effector_collection):
                current_owner = collection.get("blendermcp_liquid_domain")
                if current_owner not in {None, obj.name}:
                    raise ValueError(
                        f"Collection '{collection.name}' is owned by liquid domain '{current_owner}', not '{obj.name}'"
                    )
                key = "blendermcp_liquid_domain"
                old_collection_tags.append((collection, key, key in collection, collection.get(key)))
                collection[key] = obj.name
            old_scope = (settings.fluid_group, settings.effector_group)
            settings.fluid_group = flow_collection
            settings.effector_group = effector_collection
            initial = {
                "resolution_max": resolution_max,
                "time_scale": time_scale,
                "timesteps_min": timesteps_min,
                "timesteps_max": timesteps_max,
                "use_adaptive_timesteps": use_adaptive_timesteps,
                "cfl_condition": cfl_condition,
                "simulation_method": simulation_method,
            }
            domain_changes = _patch_rna(settings, initial, _DOMAIN_FIELDS)
            for name, value in (
                ("cache_directory", cache_directory),
                ("cache_type", cache_type),
                ("cache_frame_start", cache_frame_start),
                ("cache_frame_end", cache_frame_end),
            ):
                _validate_rna_value(settings, name, value)
            settings.cache_directory = cache_directory
            settings.cache_type = cache_type
            if cache_frame_start > settings.cache_frame_end:
                settings.cache_frame_end = cache_frame_end
                settings.cache_frame_start = cache_frame_start
            else:
                settings.cache_frame_start = cache_frame_start
                settings.cache_frame_end = cache_frame_end
            obj["blendermcp_liquid_domain"] = 1
            domain_uuid = _tag_liquid_object(obj, "DOMAIN")["uuid"]
            bpy.context.view_layer.update()
        except Exception:
            if domain_changes and modifier is not None and modifier.domain_settings is not None:
                _restore_rna(modifier.domain_settings, domain_changes)
            if old_scope and modifier is not None and modifier.domain_settings is not None:
                modifier.domain_settings.fluid_group, modifier.domain_settings.effector_group = old_scope
            if not created_object:
                if old_tag is None:
                    with contextlib.suppress(Exception):
                        del obj["blendermcp_liquid_domain"]  # pyright: ignore[reportArgumentType]
                else:
                    obj["blendermcp_liquid_domain"] = old_tag
                if old_uuid is None:
                    with contextlib.suppress(Exception):
                        del obj[_LIQUID_UUID_PROPERTY]  # pyright: ignore[reportArgumentType]
                else:
                    obj[_LIQUID_UUID_PROPERTY] = old_uuid
                if old_role is None:
                    with contextlib.suppress(Exception):
                        del obj[_LIQUID_ROLE_PROPERTY]  # pyright: ignore[reportArgumentType]
                else:
                    obj[_LIQUID_ROLE_PROPERTY] = old_role
                with contextlib.suppress(Exception):
                    if modifier is not None:
                        obj.modifiers.remove(modifier)
            for collection in linked_collections:
                with contextlib.suppress(Exception):
                    scene.collection.children.unlink(collection)
            for collection, key, existed, value in reversed(old_collection_tags):
                with contextlib.suppress(Exception):
                    if existed:
                        collection[key] = value
                    else:
                        del collection[key]
            raise
        return {
            "changed_objects": [obj.name],
            "object": obj.name,
            "created_object": created_object,
            "modifier": modifier.name,
            "domain": _domain_info(obj, modifier, settings),
            "cache_directory_resolved": _resolved_cache_path(cache_directory),
            "retained_live_modifier": True,
            "domain_uuid": domain_uuid,
            "manifest_registered": _register_owned_objects(settings, domain_uuid, [(domain_uuid, obj.name, "DOMAIN")])
            is not None,
            "ownership": {
                "domain_property": "blendermcp_liquid_domain",
                "uuid_property": _LIQUID_UUID_PROPERTY,
                "role_property": _LIQUID_ROLE_PROPERTY,
                "flow_collection": flow_collection.name,
                "effector_collection": effector_collection.name,
            },
            "warnings": []
            if all(abs(float(scale) - 1.0) <= 1e-6 for scale in obj.scale)
            else ["Existing domain has non-unit scale; solver cell sizing and distances are scale-sensitive."],
        }

    def fit_liquid_domain(
        self,
        scene_name,
        source_object_names,
        collider_object_names=None,
        domain_object_name=None,
        new_domain_name="Liquid Domain",
        cache_directory=None,
        collection_name=None,
        modifier_name="Liquid Domain",
        padding=(0.25, 0.25, 0.25),
        expected_travel=(0.0, 0.0, 0.0),
        splash_height=0.0,
        sample_frame_start=None,
        sample_frame_end=None,
        sample_frame_step=1,
        open_boundaries=None,
    ):
        scene = _get_scene(scene_name)
        if not source_object_names:
            raise ValueError("source_object_names cannot be empty")
        _finite(padding, "padding")
        _finite(expected_travel, "expected_travel")
        if len(padding) != 3 or any(value < 0 for value in padding):
            raise ValueError("padding must contain three non-negative values")
        if len(expected_travel) != 3:
            raise ValueError("expected_travel must contain three finite values")
        if splash_height < 0 or not math.isfinite(splash_height):
            raise ValueError("splash_height must be finite and non-negative")
        objects = []
        for name in [*source_object_names, *(collider_object_names or [])]:
            obj = _get_object(name, {"MESH"})
            if obj.name not in scene.objects:
                raise ValueError(f"Object '{name}' is not linked to scene '{scene_name}'")
            objects.append(obj)
        start = scene.frame_current if sample_frame_start is None else int(sample_frame_start)
        end = start if sample_frame_end is None else int(sample_frame_end)
        if end < start:
            raise ValueError("sample_frame_end must be >= sample_frame_start")
        if sample_frame_step < 1:
            raise ValueError("sample_frame_step must be >= 1")
        frames = list(range(start, end + 1, sample_frame_step))
        if frames[-1] != end:
            frames.append(end)
        if len(frames) > 32:
            raise ValueError("At most 32 frames may be sampled; increase sample_frame_step or narrow the range")
        original_frame = scene.frame_current
        union_min = [math.inf, math.inf, math.inf]
        union_max = [-math.inf, -math.inf, -math.inf]
        try:
            for frame in frames:
                scene.frame_set(frame)
                bpy.context.view_layer.update()
                for obj in objects:
                    bounds = _world_bounds(obj)
                    union_min = [min(union_min[axis], bounds["minimum"][axis]) for axis in range(3)]
                    union_max = [max(union_max[axis], bounds["maximum"][axis]) for axis in range(3)]
        finally:
            scene.frame_set(original_frame)
            bpy.context.view_layer.update()
        for axis, travel in enumerate(expected_travel):
            if travel < 0:
                union_min[axis] += travel
            else:
                union_max[axis] += travel
        gravity = [float(value) for value in scene.gravity]
        gravity_length = math.sqrt(sum(value**2 for value in gravity))
        if gravity_length > 1e-12:
            splash_vector = [-value / gravity_length * splash_height for value in gravity]
        else:
            splash_vector = [0.0, 0.0, splash_height]
        for axis, travel in enumerate(splash_vector):
            if travel < 0:
                union_min[axis] += travel
            else:
                union_max[axis] += travel
        minimum = [union_min[axis] - padding[axis] for axis in range(3)]
        maximum = [union_max[axis] + padding[axis] for axis in range(3)]
        dimensions = [maximum[axis] - minimum[axis] for axis in range(3)]
        center = [(minimum[axis] + maximum[axis]) * 0.5 for axis in range(3)]
        created = domain_object_name is None
        if created:
            if not cache_directory:
                raise ValueError("cache_directory is required when fit_liquid_domain creates a domain")
            created_result = self.create_liquid_domain(
                scene_name=scene_name,
                cache_directory=cache_directory,
                new_object_name=new_domain_name,
                collection_name=collection_name,
                dimensions=dimensions,
                location=center,
                modifier_name=modifier_name,
            )
            domain_object_name = created_result["object"]
        domain_obj, domain_modifier, settings = _get_domain(domain_object_name, modifier_name if not created else None)
        _reject_baked(settings)
        if domain_obj.data.users > 1:
            raise ValueError("Cannot refit a domain whose mesh datablock is shared; make its mesh single-user first")
        if not created:
            inverse = domain_obj.matrix_world.inverted_safe()
            corners = [
                inverse @ mathutils.Vector((x, y, z))
                for x in (minimum[0], maximum[0])
                for y in (minimum[1], maximum[1])
                for z in (minimum[2], maximum[2])
            ]
            vertices = [corners[index] for index in (0, 4, 6, 2, 1, 5, 7, 3)]
            _unused, faces = _cube_geometry((1.0, 1.0, 1.0))
            domain_obj.data.clear_geometry()
            domain_obj.data.from_pydata(vertices, [], faces)
            domain_obj.data.update()
            bpy.context.view_layer.update()
        cell_size = max(dimensions) / settings.resolution_max
        return {
            "changed_objects": [domain_obj.name],
            "domain": domain_obj.name,
            "modifier": domain_modifier.name,
            "created": created,
            "sampled_frames": frames,
            "world_bounds": {"coordinate_space": "WORLD", "minimum": minimum, "maximum": maximum},
            "dimensions": dimensions,
            "estimated_cell_size": cell_size,
            "limiting_axis": "XYZ"[dimensions.index(max(dimensions))],
            "gravity_vector_world": gravity,
            "splash_allowance_world": splash_vector,
            "open_boundaries_considered": open_boundaries or [],
            "warnings": []
            if not open_boundaries
            else ["Open boundaries were reported but do not reduce fitted safety padding."],
        }

    def configure_liquid_solver(self, domain_object_name, modifier_name, patch):
        obj, modifier, settings = _get_domain(domain_object_name, modifier_name)
        _reject_baked(settings)
        if not patch:
            raise ValueError("Solver patch cannot be empty")
        prospective_min = patch.get("timesteps_min", settings.timesteps_min)
        prospective_max = patch.get("timesteps_max", settings.timesteps_max)
        if prospective_min > prospective_max:
            raise ValueError("timesteps_min must be <= timesteps_max")
        particle_min = patch.get("particle_min", settings.particle_min)
        particle_max = patch.get("particle_max", settings.particle_max)
        if particle_min > particle_max:
            raise ValueError("particle_min must be <= particle_max")
        flip_particles = patch.get(_FLIP_PARTICLES_FIELD)
        rna_patch = {name: value for name, value in patch.items() if name != _FLIP_PARTICLES_FIELD}
        changes = _patch_rna(settings, rna_patch, _DOMAIN_FIELDS)
        flip_change = None
        try:
            if flip_particles is not None:
                flip_change = _set_flip_particles(settings, flip_particles)
            bpy.context.view_layer.update()
        except Exception:
            # _restore_rna assigns old values back, which would *toggle* use_flip_particles rather
            # than restore it, so that field is rolled back through its own idempotent setter.
            _restore_rna(settings, changes)
            if flip_change is not None:
                with contextlib.suppress(Exception):
                    _set_flip_particles(settings, flip_change["old"])
            raise
        if flip_change is not None and flip_change["old"] != flip_change["new"]:
            changes[_FLIP_PARTICLES_FIELD] = flip_change
        estimate = self.estimate_liquid_resources(domain_object_name, modifier_name)
        return {
            "changed_objects": [obj.name],
            "object": obj.name,
            "modifier": modifier.name,
            "changes": changes,
            "estimated_grid": estimate["estimated_grid"],
            "invalidated_cache_stages": ["DATA", "MESH", "PARTICLES", "GUIDES"],
        }

    def _domain_collection_for_role(self, domain_settings, role):
        collection = domain_settings.fluid_group if role == "FLOW" else domain_settings.effector_group
        if collection is None:
            raise ValueError(
                f"Domain has no explicit {'flow' if role == 'FLOW' else 'effector'} collection; configure scope first"
            )
        return collection

    def _prepare_fluid_role(
        self,
        object_name,
        domain_object_name,
        modifier_name,
        existing_policy,
        role,
        domain_type="LIQUID",
    ):
        """Validate and stage a flow/effector modifier without configuring its RNA settings."""
        obj = _get_object(object_name, {"MESH"})
        sync_from_editmode(obj)
        if not obj.data.vertices or not obj.data.polygons:
            raise ValueError(f"{role.title()} mesh '{object_name}' must contain vertices and faces")
        domain_obj, domain_modifier, domain = _get_domain(
            domain_object_name,
            expected_domain_type=domain_type,
        )
        _reject_baked(domain)
        if obj == domain_obj:
            raise ValueError(f"A {domain_type.lower()} domain cannot also be its own {role.lower()}")
        if existing_policy not in {"ERROR", "REUSE"}:
            raise ValueError("existing_policy must be ERROR or REUSE")
        collection = self._domain_collection_for_role(domain, role)
        modifier = obj.modifiers.get(modifier_name)
        created = modifier is None
        if modifier is not None:
            if existing_policy == "ERROR":
                raise ValueError(f"Modifier '{modifier_name}' already exists on '{object_name}'")
            modifier = _get_fluid_modifier(obj, modifier_name, role)
        else:
            modifier = obj.modifiers.new(name=modifier_name, type="FLUID")
        return obj, domain_obj, domain_modifier, domain, collection, modifier, created

    @staticmethod
    def _initialize_fluid_role(modifier, role, created):
        """Initialize and return Blender's role-specific fluid settings."""
        if created:
            modifier.fluid_type = role
            bpy.context.view_layer.update()
        settings = getattr(modifier, f"{role.lower()}_settings")
        if settings is None:
            raise RuntimeError(f"Blender did not initialize Fluid{role.title()}Settings")
        return settings

    @staticmethod
    def _rollback_fluid_role(obj, modifier, role, changes, collection, linked, created, identity=None):
        """Undo a partially configured flow/effector component."""
        _untag_liquid_object(identity)
        settings = getattr(modifier, f"{role.lower()}_settings")
        if changes and settings is not None:
            _restore_rna(settings, changes)
        if linked:
            with contextlib.suppress(Exception):
                collection.objects.unlink(obj)
        if created:
            with contextlib.suppress(Exception):
                obj.modifiers.remove(modifier)

    def add_liquid_flow(
        self,
        object_name,
        domain_object_name,
        modifier_name="Liquid Flow",
        existing_policy="ERROR",
        behavior="GEOMETRY",
        settings=None,
    ):
        obj, domain_obj, domain_modifier, domain, collection, modifier, created = self._prepare_fluid_role(
            object_name,
            domain_object_name,
            modifier_name,
            existing_policy,
            "FLOW",
        )
        linked = False
        changes = {}
        identity = None
        try:
            flow = self._initialize_fluid_role(modifier, "FLOW", created)
            if not created and flow.flow_type != "LIQUID":
                raise ValueError("REUSE requires an existing LIQUID flow")
            if created:
                flow.flow_type = "LIQUID"
            patch = dict(settings or {})
            patch["flow_behavior"] = behavior
            changes = self._configure_flow_settings(obj, flow, patch)
            linked = _link_object(collection, obj)
            identity = _tag_liquid_object(obj, "FLOW")
            bpy.context.view_layer.update()
        except Exception:
            self._rollback_fluid_role(obj, modifier, "FLOW", changes, collection, linked, created, identity)
            raise
        domain_uuid = _ensure_liquid_uuid(domain_obj)
        manifest = _register_owned_objects(domain, domain_uuid, [(identity["uuid"], obj.name, "FLOW")])
        return {
            "changed_objects": [obj.name, domain_obj.name],
            "object": obj.name,
            "modifier": modifier.name,
            "created": created,
            "domain": domain_obj.name,
            "domain_modifier": domain_modifier.name,
            "domain_uuid": domain_uuid,
            "object_uuid": identity["uuid"],
            "manifest_registered": manifest is not None,
            "flow_collection": collection.name,
            "collection_membership_added": linked,
            "changes": changes,
            "invalidated_cache_stages": ["DATA", "MESH", "PARTICLES"],
        }

    @staticmethod
    def _configure_flow_settings(obj, flow, patch):
        if not patch:
            raise ValueError("Flow patch cannot be empty")
        group = patch.get("density_vertex_group")
        if group and obj.vertex_groups.get(group) is None:
            raise ValueError(f"Vertex group not found on '{obj.name}': {group}")
        smoke_only = sorted(_SMOKE_ONLY_FLOW_FIELDS & set(patch))
        if smoke_only:
            raise ValueError(
                f"{', '.join(smoke_only)} only affects particle-system smoke/fire emission and is ignored "
                "for a LIQUID flow; use surface_distance, subframes, or use_plane_init instead"
            )
        effective_behavior = patch.get("flow_behavior", flow.flow_behavior)
        if effective_behavior == "GEOMETRY" and "use_inflow" in patch:
            raise ValueError(
                "use_inflow ('Use Flow') has no effect for GEOMETRY flow behavior; it only applies to INFLOW or OUTFLOW"
            )
        return _patch_rna(flow, patch, _FLOW_FIELDS)

    def configure_liquid_flow(self, object_name, modifier_name, domain_object_name, patch):
        obj, modifier, flow = _get_role(object_name, modifier_name, "FLOW")
        domain_obj, _domain_modifier, domain = _get_domain(domain_object_name)
        _reject_baked(domain)
        collection = self._domain_collection_for_role(domain, "FLOW")
        if not _object_in_collection(obj, collection):
            raise ValueError(f"Flow '{object_name}' is not a member of domain collection '{collection.name}'")
        changes = self._configure_flow_settings(obj, flow, patch)
        try:
            bpy.context.view_layer.update()
        except Exception:
            _restore_rna(flow, changes)
            raise
        return {
            "changed_objects": [obj.name, domain_obj.name],
            "object": obj.name,
            "modifier": modifier.name,
            "domain": domain_obj.name,
            "changes": changes,
            "velocity_coordinate_space": "WORLD axes; source motion is scaled separately by velocity_factor",
            "invalidated_cache_stages": ["DATA", "MESH", "PARTICLES"],
        }

    def add_liquid_effector(
        self,
        object_name,
        domain_object_name,
        modifier_name="Liquid Effector",
        existing_policy="ERROR",
        effector_type="COLLISION",
        settings=None,
    ):
        obj, domain_obj, domain_modifier, domain, collection, modifier, created = self._prepare_fluid_role(
            object_name,
            domain_object_name,
            modifier_name,
            existing_policy,
            "EFFECTOR",
        )
        linked = False
        changes = {}
        identity = None
        try:
            effector = self._initialize_fluid_role(modifier, "EFFECTOR", created)
            patch = dict(settings or {})
            patch["effector_type"] = effector_type
            changes = _patch_rna(effector, patch, _EFFECTOR_FIELDS)
            linked = _link_object(collection, obj)
            identity = _tag_liquid_object(obj, "GUIDE" if effector_type == "GUIDE" else "EFFECTOR")
            bpy.context.view_layer.update()
        except Exception:
            self._rollback_fluid_role(obj, modifier, "EFFECTOR", changes, collection, linked, created, identity)
            raise
        domain_uuid = _ensure_liquid_uuid(domain_obj)
        manifest = _register_owned_objects(domain, domain_uuid, [(identity["uuid"], obj.name, identity["role"])])
        return {
            "changed_objects": [obj.name, domain_obj.name],
            "object": obj.name,
            "modifier": modifier.name,
            "created": created,
            "domain": domain_obj.name,
            "domain_modifier": domain_modifier.name,
            "domain_uuid": domain_uuid,
            "object_uuid": identity["uuid"],
            "object_role": identity["role"],
            "manifest_registered": manifest is not None,
            "effector_collection": collection.name,
            "collection_membership_added": linked,
            "changes": changes,
            "invalidated_cache_stages": ["DATA", "MESH", "PARTICLES", "GUIDES"],
            "warnings": _warning_for_effector(obj),
        }

    def configure_liquid_effector(self, object_name, modifier_name, domain_object_name, patch):
        obj, modifier, effector = _get_role(object_name, modifier_name, "EFFECTOR")
        domain_obj, _domain_modifier, domain = _get_domain(domain_object_name)
        _reject_baked(domain)
        collection = self._domain_collection_for_role(domain, "EFFECTOR")
        if not _object_in_collection(obj, collection):
            raise ValueError(f"Effector '{object_name}' is not a member of domain collection '{collection.name}'")
        if not patch:
            raise ValueError("Effector patch cannot be empty")
        changes = _patch_rna(effector, patch, _EFFECTOR_FIELDS)
        try:
            bpy.context.view_layer.update()
        except Exception:
            _restore_rna(effector, changes)
            raise
        warnings = _warning_for_effector(obj)
        upstream = [
            mod.name
            for mod in list(obj.modifiers)[: list(obj.modifiers).index(modifier)]
            if mod.type in _TOPOLOGY_MODIFIERS
        ]
        if upstream:
            warnings.append(
                f"Topology-changing modifiers before the effector may require sampled validation: {upstream}"
            )
        return {
            "changed_objects": [obj.name, domain_obj.name],
            "object": obj.name,
            "modifier": modifier.name,
            "domain": domain_obj.name,
            "changes": changes,
            "invalidated_cache_stages": ["DATA", "MESH", "PARTICLES", "GUIDES"],
            "warnings": warnings,
        }

    def configure_liquid_scope_and_boundaries(
        self,
        domain_object_name,
        modifier_name,
        flow_collection_name=None,
        effector_collection_name=None,
        force_collection_name=None,
        clear_flow_collection=False,
        clear_effector_collection=False,
        clear_force_collection=False,
        create_missing_collections=False,
        boundaries=None,
    ):
        obj, modifier, settings = _get_domain(domain_object_name, modifier_name)
        _reject_baked(settings)
        specifications = (
            ("fluid_group", flow_collection_name, clear_flow_collection),
            ("effector_group", effector_collection_name, clear_effector_collection),
            ("force_collection", force_collection_name, clear_force_collection),
        )
        if not boundaries and not any(name is not None or clear for _field, name, clear in specifications):
            raise ValueError("Provide at least one collection or boundary change")
        scene = next((item for item in bpy.data.scenes if obj.name in item.objects), None)
        if scene is None:
            raise ValueError(f"Domain '{obj.name}' is not linked to a scene")
        resolved = {}
        created_links = []
        for field, name, clear in specifications:
            if name is not None and clear:
                raise ValueError(f"Cannot both set and clear {field}")
            if name is None:
                resolved[field] = None if clear else getattr(settings, field)
                continue
            collection = bpy.data.collections.get(name)
            if collection is None:
                if not create_missing_collections:
                    raise ValueError(f"Collection not found: {name}")
                collection, _created, linked = _ensure_collection(scene, name)
                if linked:
                    created_links.append(collection)
            elif not _scene_has_collection(scene, collection):
                raise ValueError(f"Collection '{name}' is not linked to scene '{scene.name}'")
            resolved[field] = collection
        boundary_patch = {
            _BOUNDARY_FIELDS[name]: value for name, value in (boundaries or {}).items() if name in _BOUNDARY_FIELDS
        }
        unknown = set(boundaries or {}) - set(_BOUNDARY_FIELDS)
        if unknown:
            raise ValueError(f"Unsupported boundary faces: {sorted(unknown)}")
        boundary_changes = {}
        old_collections = {field: getattr(settings, field) for field, _name, _clear in specifications}
        try:
            boundary_changes = _patch_rna(settings, boundary_patch, set(_BOUNDARY_FIELDS.values()))
            for field, value in resolved.items():
                setattr(settings, field, value)
            bpy.context.view_layer.update()
        except Exception:
            _restore_rna(settings, boundary_changes)
            for field, value in old_collections.items():
                with contextlib.suppress(Exception):
                    setattr(settings, field, value)
            for collection in created_links:
                with contextlib.suppress(Exception):
                    scene.collection.children.unlink(collection)
            raise
        domain_bounds = _world_bounds(obj, evaluated=False)
        outside = {"flows": [], "effectors": []}
        for label, collection in (("flows", settings.fluid_group), ("effectors", settings.effector_group)):
            if collection is None:
                continue
            for member in collection.all_objects:
                if not _bounds_overlap(domain_bounds, _world_bounds(member)):
                    outside[label].append(member.name)
        collection_changes = {
            field: {
                "old": old_collections[field].name if old_collections[field] else None,
                "new": getattr(settings, field).name if getattr(settings, field) else None,
            }
            for field, _name, _clear in specifications
            if old_collections[field] != getattr(settings, field)
        }
        return {
            "changed_objects": [obj.name],
            "object": obj.name,
            "modifier": modifier.name,
            "collection_changes": collection_changes,
            "boundary_changes": boundary_changes,
            "domain_local_face_mapping": {
                "front": "-Y",
                "back": "+Y",
                "left": "-X",
                "right": "+X",
                "top": "+Z",
                "bottom": "-Z",
            },
            "domain_world_matrix": [list(row) for row in obj.matrix_world],
            "out_of_bounds_members": outside,
            "invalidated_cache_stages": ["DATA", "MESH", "PARTICLES", "GUIDES"],
        }

    def estimate_liquid_resources(self, domain_object_name, modifier_name):
        obj, modifier, settings = _get_domain(domain_object_name, modifier_name)
        bounds = _world_bounds(obj, evaluated=False)
        dimensions = bounds["dimensions"]
        longest = max(dimensions)
        if longest <= 0:
            raise ValueError("Domain has zero world-space extent")
        cell_size = longest / settings.resolution_max
        cells = [max(1, int(math.ceil(value / cell_size))) for value in dimensions]
        base_cells = math.prod(cells)
        frames = max(0, settings.cache_frame_end - settings.cache_frame_start + 1)
        mesh_multiplier = settings.mesh_scale**3 if settings.use_mesh else 0
        secondary_count = sum(
            bool(getattr(settings, name, False))
            for name in ("use_spray_particles", "use_foam_particles", "use_bubble_particles", "use_tracer_particles")
        )
        particle_factor = max(1.0, float(settings.particle_number))
        relative_per_frame = base_cells * (1.0 + particle_factor * 0.35 + mesh_multiplier * 0.2 + secondary_count * 0.3)
        runtime = {}
        for name in ("cell_size", "domain_resolution"):
            with contextlib.suppress(Exception):
                runtime[name] = _serialize(getattr(settings, name))
        preview_resolution = max(16, min(64, settings.resolution_max // 2))
        return {
            "domain": obj.name,
            "modifier": modifier.name,
            "world_dimensions": dimensions,
            "estimated_grid": {
                "resolution_max": settings.resolution_max,
                "limiting_axis": "XYZ"[dimensions.index(longest)],
                "cell_size": cell_size,
                "cells_xyz": cells,
                "base_cell_count": base_cells,
            },
            "runtime_values": runtime,
            "frame_count": frames,
            "mesh": {"enabled": settings.use_mesh, "scale": settings.mesh_scale},
            "particles": {
                "number_factor": settings.particle_number,
                "minimum_per_cell": settings.particle_min,
                "maximum_per_cell": settings.particle_max,
                "secondary_types_enabled": secondary_count,
            },
            "cache": {"type": settings.cache_type, "data_format": settings.cache_data_format},
            "relative_cost_index": relative_per_frame * frames,
            "recommendations": {
                "preview_resolution_max": preview_resolution,
                "final_resolution_max": settings.resolution_max,
                "note": (
                    "Relative cost is conservative; occupancy, motion, compression, hardware, and solver behavior "
                    "dominate actual memory, disk, and bake time."
                ),
            },
        }

    def validate_liquid_setup(self, scene_name, domain_object_names=None, max_findings=200):
        scene = _get_scene(scene_name)
        if not 1 <= int(max_findings) <= 1000:
            raise ValueError("max_findings must be in [1, 1000]")
        requested = set(domain_object_names or [])
        if requested:
            missing = sorted(requested - {obj.name for obj in scene.objects})
            if missing:
                raise ValueError(f"Domain objects are not linked to scene '{scene.name}': {missing}")
        domains = []
        for obj in scene.objects:
            if requested and obj.name not in requested:
                continue
            for modifier in obj.modifiers:
                if (
                    modifier.type == "FLUID"
                    and modifier.fluid_type == "DOMAIN"
                    and modifier.domain_settings
                    and modifier.domain_settings.domain_type == "LIQUID"
                ):
                    domains.append((obj, modifier, modifier.domain_settings))
        if requested:
            found = {obj.name for obj, _modifier, _settings in domains}
            wrong = sorted(requested - found)
            if wrong:
                raise ValueError(f"Objects are not initialized liquid domains: {wrong}")
        findings = []

        def add(severity, code, message, *, obj=None, prop=None, evidence=None, remediation=None):
            findings.append(
                {
                    "severity": severity,
                    "code": code,
                    "object": obj,
                    "property": prop,
                    "frame": scene.frame_current,
                    "message": message,
                    "evidence": evidence,
                    "remediation": remediation,
                }
            )

        cache_paths = {}
        domain_bounds = {}
        for obj, modifier, settings in domains:
            bounds = _world_bounds(obj, evaluated=False)
            domain_bounds[obj.name] = bounds
            cell_size = max(bounds["dimensions"]) / settings.resolution_max
            scale = [float(value) for value in obj.scale]
            if any(value <= 0 for value in scale):
                add("ERROR", "INVALID_DOMAIN_SCALE", "Domain has zero or negative scale.", obj=obj.name, evidence=scale)
            elif max(scale) / min(scale) > 1.01:
                add(
                    "WARNING",
                    "NONUNIFORM_DOMAIN_SCALE",
                    "Domain scale is nonuniform; cell sizing is scale-sensitive.",
                    obj=obj.name,
                    evidence=scale,
                    remediation="Use a unit-scale box with dimensions baked into its mesh before baking.",
                )
            if min(bounds["dimensions"]) <= 1e-6:
                add(
                    "ERROR", "ZERO_DOMAIN_EXTENT", "Domain has a zero-size world extent.", obj=obj.name, evidence=bounds
                )
            if settings.mesh_concave_lower > settings.mesh_concave_upper:
                add(
                    "ERROR",
                    "INVERTED_MESH_CONCAVITY",
                    "mesh_concave_lower is greater than mesh_concave_upper.",
                    obj=obj.name,
                    prop="mesh_concave_lower",
                    evidence={
                        "mesh_concave_lower": settings.mesh_concave_lower,
                        "mesh_concave_upper": settings.mesh_concave_upper,
                    },
                    remediation="Set mesh_concave_lower <= mesh_concave_upper.",
                )
            if settings.cache_frame_start > settings.cache_frame_end:
                add(
                    "ERROR",
                    "INVALID_FRAME_RANGE",
                    "Cache start is after cache end.",
                    obj=obj.name,
                    evidence=[settings.cache_frame_start, settings.cache_frame_end],
                )
            path = _resolved_cache_path(settings.cache_directory) if settings.cache_directory else ""
            if not path:
                add("ERROR", "MISSING_CACHE_PATH", "Domain has no explicit cache directory.", obj=obj.name)
            else:
                cache_paths.setdefault(path, []).append(f"{obj.name}:{modifier.name}")
                parent = path if os.path.isdir(path) else os.path.dirname(path)
                if not parent or not os.path.isdir(parent):
                    add(
                        "ERROR",
                        "INVALID_CACHE_PARENT",
                        "Cache directory parent does not exist.",
                        obj=obj.name,
                        evidence=path,
                        remediation="Choose an explicit cache path under an existing writable directory.",
                    )
                elif not os.access(parent, os.W_OK):
                    add(
                        "ERROR",
                        "CACHE_NOT_WRITABLE",
                        "Cache directory parent is not writable.",
                        obj=obj.name,
                        evidence=parent,
                    )
            baked = [name for name in _CACHE_FLAGS if name.startswith("has_") and getattr(settings, name, False)]
            baking = [name for name in _CACHE_FLAGS if name.startswith("is_") and getattr(settings, name, False)]
            if baking:
                add(
                    "WARNING", "ACTIVE_BAKE", "A liquid cache stage is currently baking.", obj=obj.name, evidence=baking
                )
            if settings.has_cache_baked_mesh and not settings.has_cache_baked_data:
                add(
                    "ERROR",
                    "PARTIAL_CACHE_ORDER",
                    "Mesh cache exists without its data cache prerequisite.",
                    obj=obj.name,
                )
            if baked and settings.cache_type == "REPLAY":
                add(
                    "WARNING",
                    "REPLAY_BAKE_STATE",
                    "Replay domain reports baked stages; inspect for stale partial data.",
                    obj=obj.name,
                    evidence=baked,
                )
            if not settings.use_mesh:
                add(
                    "WARNING",
                    "MESH_DISABLED",
                    "Liquid surface meshing is disabled for render delivery.",
                    obj=obj.name,
                    remediation="Enable mesh generation before the intentional data/mesh bake.",
                )
            if not settings.use_speed_vectors:
                add(
                    "INFO",
                    "SPEED_VECTORS_DISABLED",
                    "Mesh speed vectors are disabled; motion-blur/export velocity will be unavailable after baking.",
                    obj=obj.name,
                )
            dependencies = _domain_dependencies(scene, obj, settings)
            for record in dependencies:
                if record["kind"] in {"flow", "effector"} and record["missing_settings"]:
                    add(
                        "ERROR",
                        "UNINITIALIZED_ROLE",
                        "Fluid modifier settings are unavailable.",
                        obj=record["object"],
                        evidence=record,
                    )
                if record["kind"] == "flow" and not record["missing_settings"] and not record["liquid_compatible"]:
                    add(
                        "ERROR",
                        "NON_LIQUID_FLOW",
                        "Scoped flow is not configured for liquid.",
                        obj=record["object"],
                        evidence=record,
                    )
                if not record.get("in_scope", True):
                    add(
                        "WARNING",
                        "OUTSIDE_SCOPE",
                        "Fluid role exists in the scene but is outside this domain's explicit collection.",
                        obj=record["object"],
                        evidence=record,
                    )
            for role, collection in (("FLOW", settings.fluid_group), ("EFFECTOR", settings.effector_group)):
                if collection is None:
                    add(
                        "WARNING",
                        f"UNBOUNDED_{role}_SCOPE",
                        f"Domain has scene-wide {role.lower()} scope.",
                        obj=obj.name,
                        remediation="Assign an explicit collection for deterministic production scope.",
                    )
                    candidates = scene.objects
                else:
                    candidates = collection.all_objects
                for member in candidates:
                    role_modifiers = [mod for mod in member.modifiers if mod.type == "FLUID" and mod.fluid_type == role]
                    if not role_modifiers:
                        if collection is not None:
                            add(
                                "WARNING",
                                "COLLECTION_MEMBER_WITHOUT_ROLE",
                                f"Object is in the domain {role.lower()} collection without a matching modifier.",
                                obj=member.name,
                                evidence={"domain": obj.name, "collection": collection.name},
                            )
                        continue
                    member_bounds = _world_bounds(member)
                    member_scale = [float(value) for value in member.scale]
                    if any(value <= 0 for value in member_scale):
                        add(
                            "ERROR",
                            "INVALID_MEMBER_SCALE",
                            f"{role.title()} has zero or negative scale.",
                            obj=member.name,
                            evidence=member_scale,
                        )
                    elif max(member_scale) / min(member_scale) > 1.01:
                        add(
                            "WARNING",
                            "NONUNIFORM_MEMBER_SCALE",
                            f"{role.title()} scale is nonuniform; its mesh shape may not read as intended.",
                            obj=member.name,
                            evidence=member_scale,
                        )
                    member_world_scale = list(member.matrix_world.decompose()[2])
                    if any(
                        abs(float(member_world_scale[axis]) - member_scale[axis]) > max(1e-4, member_scale[axis] * 0.01)
                        for axis in range(3)
                    ):
                        add(
                            "WARNING",
                            "UNAPPLIED_MEMBER_TRANSFORM",
                            f"{role.title()}'s effective world scale differs from its own local scale, indicating "
                            "an un-applied parent or delta transform.",
                            obj=member.name,
                            evidence={
                                "local_scale": member_scale,
                                "world_scale": [float(value) for value in member_world_scale],
                            },
                            remediation="Apply Object > Apply > Scale (or the parent's transform) before baking.",
                        )
                    if not _bounds_overlap(bounds, member_bounds):
                        add(
                            "ERROR" if role == "FLOW" else "WARNING",
                            "ROLE_OUTSIDE_DOMAIN",
                            f"{role.title()} does not overlap the domain at the observed frame.",
                            obj=member.name,
                            evidence={"domain": obj.name, "bounds": member_bounds},
                        )
                    elif role == "FLOW" and not _bounds_contains(bounds, member_bounds):
                        add(
                            "WARNING",
                            "FLOW_CLIPPED",
                            "Flow bounds cross the domain boundary.",
                            obj=member.name,
                            evidence={"domain": obj.name, "flow_bounds": member_bounds},
                        )
                    if role == "FLOW":
                        minimum_feature = min(member_bounds["dimensions"])
                        if minimum_feature < cell_size * 2:
                            add(
                                "WARNING",
                                "FLOW_BELOW_GRID_SCALE",
                                "The flow's thinnest world extent spans fewer than two estimated cells.",
                                obj=member.name,
                                evidence={"minimum_feature": minimum_feature, "estimated_cell_size": cell_size},
                                remediation="Tighten the domain, increase resolution, or use a thicker source proxy.",
                            )
                    if member.type == "MESH":
                        topology = _evaluated_mesh_topology(member)
                        flow_settings = role_modifiers[0].flow_settings if role == "FLOW" else None
                        plane = bool(flow_settings and flow_settings.use_plane_init)
                        effector_settings = role_modifiers[0].effector_settings if role == "EFFECTOR" else None
                        plane = plane or bool(effector_settings and effector_settings.use_plane_init)
                        if topology["non_manifold_edges"] and not plane:
                            add(
                                "WARNING",
                                "NON_MANIFOLD_FLUID_GEOMETRY",
                                f"Closed {role.lower()} geometry is non-manifold once modifiers are evaluated.",
                                obj=member.name,
                                evidence=topology,
                                remediation=(
                                    "Repair the mesh or explicitly enable plane initialization when appropriate."
                                ),
                            )
                        if role == "FLOW":
                            orientation = _flow_normal_orientation(member)
                            if orientation and orientation["inward_fraction"] > 0.5:
                                add(
                                    "WARNING",
                                    "FLOW_NORMALS_LIKELY_INWARD",
                                    "Most evaluated face normals point toward the flow object's own centroid.",
                                    obj=member.name,
                                    evidence=orientation,
                                    remediation="Recalculate or flip normals so they face outward before emission.",
                                )
                        if role == "EFFECTOR":
                            thicknesses = _wall_thickness_samples(member, cell_size)
                            if thicknesses and min(thicknesses) < cell_size * 1.5:
                                add(
                                    "WARNING",
                                    "THIN_EFFECTOR_WALL",
                                    "Effector wall thickness is under 1.5 estimated cells at a sampled point; "
                                    "liquid may leak through.",
                                    obj=member.name,
                                    evidence={
                                        "minimum_sampled_thickness": min(thicknesses),
                                        "estimated_cell_size": cell_size,
                                        "samples": len(thicknesses),
                                    },
                                    remediation="Thicken the collider wall or increase domain resolution.",
                                )
                    topology_modifiers = [mod.name for mod in member.modifiers if mod.type in _TOPOLOGY_MODIFIERS]
                    if topology_modifiers and _animation_info(member):
                        add(
                            "WARNING",
                            "ANIMATED_TOPOLOGY_RISK",
                            "Animated fluid input has topology-changing modifiers.",
                            obj=member.name,
                            evidence=topology_modifiers,
                        )
                    for role_modifier in role_modifiers:
                        role_settings = (
                            role_modifier.flow_settings if role == "FLOW" else role_modifier.effector_settings
                        )
                        if role_settings and _animation_info(member) and role_settings.subframes == 0:
                            add(
                                "WARNING",
                                "NO_MOTION_SUBFRAMES",
                                "Animated fluid input has zero motion subframes.",
                                obj=member.name,
                                prop="subframes",
                            )
                        if role == "FLOW" and role_settings and role_settings.flow_behavior == "OUTFLOW":
                            near_border = any(
                                abs(member_bounds["minimum"][axis] - bounds["minimum"][axis]) <= cell_size * 2
                                or abs(bounds["maximum"][axis] - member_bounds["maximum"][axis]) <= cell_size * 2
                                for axis in range(3)
                            )
                            if not near_border:
                                add(
                                    "INFO",
                                    "OUTFLOW_AWAY_FROM_BOUNDARY",
                                    "Outflow is not near a domain boundary; verify that this is intentional.",
                                    obj=member.name,
                                )
                        if role == "FLOW" and role_settings and role_settings.use_initial_velocity:
                            velocity = math.sqrt(sum(float(value) ** 2 for value in role_settings.velocity_coord))
                            max_cell_travel = settings.cfl_condition * settings.timesteps_max
                            if cell_size > 0 and velocity * settings.time_scale / cell_size > max_cell_travel:
                                add(
                                    "WARNING",
                                    "VELOCITY_EXCEEDS_TIMESTEP_BUDGET",
                                    "Configured initial velocity exceeds the estimated CFL/time-step budget.",
                                    obj=member.name,
                                    evidence={
                                        "velocity": velocity,
                                        "cell_size": cell_size,
                                        "cfl_condition": settings.cfl_condition,
                                        "timesteps_max": settings.timesteps_max,
                                    },
                                    remediation=(
                                        "Increase motion subframes/time steps or reduce velocity after preview testing."
                                    ),
                                )
            if (
                settings.fluid_group
                and any(
                    mod.flow_settings and mod.flow_settings.flow_behavior == "INFLOW"
                    for member in settings.fluid_group.all_objects
                    for mod in member.modifiers
                    if mod.type == "FLUID" and mod.fluid_type == "FLOW"
                )
                and all(getattr(settings, field) for field in _BOUNDARY_FIELDS.values())
            ):
                add(
                    "INFO",
                    "CLOSED_INFLOW_DOMAIN",
                    "A continuous inflow is used with all domain boundaries closed.",
                    obj=obj.name,
                    remediation="Confirm an outflow or sufficient capacity exists for the shot.",
                )
            estimate = self.estimate_liquid_resources(obj.name, modifier.name)
            if estimate["estimated_grid"]["base_cell_count"] > 250_000_000:
                add(
                    "WARNING",
                    "VERY_LARGE_GRID",
                    "Estimated base grid is exceptionally large.",
                    obj=obj.name,
                    evidence=estimate["estimated_grid"],
                    remediation="Fit the domain more tightly or validate at a preview resolution first.",
                )
        for path, owners in cache_paths.items():
            if len(owners) > 1:
                add(
                    "ERROR",
                    "SHARED_CACHE_PATH",
                    "Multiple liquid domains share one cache directory.",
                    evidence={"path": path, "domains": owners},
                    remediation="Assign one unique cache directory per domain/variant.",
                )
        for index, (first_obj, _first_modifier, _first_settings) in enumerate(domains):
            for second_obj, _second_modifier, _second_settings in domains[index + 1 :]:
                if _bounds_overlap(domain_bounds[first_obj.name], domain_bounds[second_obj.name]):
                    add(
                        "ERROR",
                        "OVERLAPPING_DOMAINS",
                        "Liquid domains overlap at the observed frame.",
                        evidence={"first": first_obj.name, "second": second_obj.name},
                    )
        severity_order = {"ERROR": 0, "WARNING": 1, "INFO": 2}
        findings.sort(key=lambda item: (severity_order[item["severity"]], item["object"] or "", item["code"]))
        truncated = len(findings) > max_findings
        returned = findings[:max_findings]
        return {
            "scene": scene.name,
            "frame_observed": scene.frame_current,
            "domains_checked": [obj.name for obj, _modifier, _settings in domains],
            "summary": dict(Counter(item["severity"] for item in findings)),
            "findings": returned,
            "truncated": truncated,
            "total_findings": len(findings),
            "passed": not any(item["severity"] == "ERROR" for item in findings),
            "limitations": [
                (
                    "This structural preflight does not advance frames, initialize a bake, or assess visual liquid "
                    "quality."
                ),
                (
                    "Fast-motion and animated-topology findings are conservative; review representative cached "
                    "frames in Blender."
                ),
            ],
        }

    def inspect_fluid_simulation(self, domain_type, scene_name=None, domain_object_name=None, limit=25, offset=0):
        """Canonical bounded inspection for either Mantaflow domain type."""
        domain_type = str(domain_type).upper()
        if domain_type not in {"LIQUID", "GAS"}:
            raise ValueError("domain_type must be LIQUID or GAS")
        scene = _get_scene(scene_name)
        if domain_object_name:
            obj, modifier, settings = _get_domain(domain_object_name, expected_domain_type=domain_type)
            if obj.name not in scene.objects:
                raise ValueError(f"Domain '{obj.name}' is not linked to scene '{scene.name}'")
            domains = [(obj, modifier, settings)]
        else:
            domains = []
            for obj in scene.objects:
                for modifier in obj.modifiers:
                    settings = getattr(modifier, "domain_settings", None)
                    if modifier.type == "FLUID" and modifier.fluid_type == "DOMAIN" and settings is not None:
                        if settings.domain_type == domain_type:
                            domains.append((obj, modifier, settings))
        domains.sort(key=lambda record: (record[0].name, record[1].name))
        start, end, truncated, next_offset = paginate(len(domains), offset, limit, 100)
        return {
            "scene": scene.name,
            "domain_type": domain_type,
            "total": len(domains),
            "offset": start,
            "limit": limit,
            "returned_count": end - start,
            "truncated": truncated,
            "next_offset": next_offset,
            "domains": [_domain_info(*record) for record in domains[start:end]],
        }

    def create_fluid_domain(self, domain_type, **kwargs):
        """Compatibility-preserving canonical domain constructor."""
        domain_type = str(domain_type).upper()
        if domain_type not in {"LIQUID", "GAS"}:
            raise ValueError("domain_type must be LIQUID or GAS")
        if domain_type == "LIQUID":
            result = self.create_liquid_domain(**kwargs)
            result["domain_type"] = domain_type
            return result
        result = self.create_liquid_domain(**kwargs)
        obj = _get_object(result["object"], {"MESH"})
        modifier = _get_fluid_modifier(obj, result["modifier"], "DOMAIN")
        settings = modifier.domain_settings
        settings.domain_type = "GAS"
        obj["blendermcp_fluid_domain_type"] = "GAS"
        bpy.context.view_layer.update()
        result["domain_type"] = "GAS"
        result["domain"] = _domain_info(obj, modifier, settings)
        result["warnings"] = [
            *result.get("warnings", []),
            "Gas domain created through the shared fluid core; configure smoke/fire fields before baking.",
        ]
        return result

    def configure_fluid_solver(self, domain_type, domain_object_name, modifier_name, patch):
        domain_type = str(domain_type).upper()
        if domain_type == "LIQUID":
            return self.configure_liquid_solver(domain_object_name, modifier_name, patch)
        obj, modifier, settings = _get_domain(domain_object_name, modifier_name, "GAS")
        _reject_baked(settings)
        if not patch:
            raise ValueError("Solver patch cannot be empty")
        liquid_only = sorted(set(patch) - _GAS_DOMAIN_FIELDS)
        if liquid_only:
            raise ValueError(f"Unsupported GAS solver properties: {liquid_only}")
        changes = _patch_rna(settings, patch, _GAS_DOMAIN_FIELDS)
        try:
            bpy.context.view_layer.update()
        except Exception:
            _restore_rna(settings, changes)
            raise
        return {
            "changed_objects": [obj.name],
            "object": obj.name,
            "modifier": modifier.name,
            "domain_type": "GAS",
            "changes": changes,
            "invalidated_cache_stages": ["DATA", "NOISE"],
        }

    def add_fluid_flow(
        self,
        domain_type,
        object_name,
        domain_object_name,
        modifier_name="Fluid Flow",
        existing_policy="ERROR",
        behavior="GEOMETRY",
        gas_flow_type="SMOKE",
        settings=None,
    ):
        domain_type = str(domain_type).upper()
        if domain_type == "LIQUID":
            liquid_settings = dict(settings or {})
            unsupported = sorted(set(liquid_settings) - _FLOW_FIELDS)
            if unsupported:
                raise ValueError(f"Unsupported LIQUID flow settings: {unsupported}")
            return self.add_liquid_flow(
                object_name,
                domain_object_name,
                modifier_name,
                existing_policy,
                behavior,
                liquid_settings,
            )
        if domain_type != "GAS":
            raise ValueError("domain_type must be LIQUID or GAS")
        obj, domain_obj, _domain_modifier, _domain, collection, modifier, created = self._prepare_fluid_role(
            object_name,
            domain_object_name,
            modifier_name,
            existing_policy,
            "FLOW",
            "GAS",
        )
        linked = False
        changes = {}
        try:
            flow = self._initialize_fluid_role(modifier, "FLOW", created)
            if not created and flow.flow_type not in {"SMOKE", "FIRE", "BOTH"}:
                raise ValueError("REUSE requires an existing gas flow")
            flow.flow_type = gas_flow_type
            patch = dict(settings or {})
            patch["flow_behavior"] = behavior
            changes = _patch_rna(flow, patch, _GAS_FLOW_FIELDS)
            linked = _link_object(collection, obj)
            bpy.context.view_layer.update()
        except Exception:
            self._rollback_fluid_role(obj, modifier, "FLOW", changes, collection, linked, created)
            raise
        return {
            "changed_objects": [obj.name, domain_obj.name],
            "object": obj.name,
            "modifier": modifier.name,
            "domain": domain_obj.name,
            "domain_type": "GAS",
            "flow_type": gas_flow_type,
            "changes": changes,
            "retained_live_modifier": True,
        }

    def add_fluid_effector(
        self,
        domain_type,
        object_name,
        domain_object_name,
        modifier_name="Fluid Effector",
        existing_policy="ERROR",
        effector_type="COLLISION",
        settings=None,
    ):
        domain_type = str(domain_type).upper()
        if domain_type == "LIQUID":
            return self.add_liquid_effector(
                object_name,
                domain_object_name,
                modifier_name,
                existing_policy,
                effector_type,
                settings,
            )
        if domain_type != "GAS":
            raise ValueError("domain_type must be LIQUID or GAS")
        obj, domain_obj, domain_modifier, _domain, collection, modifier, created = self._prepare_fluid_role(
            object_name,
            domain_object_name,
            modifier_name,
            existing_policy,
            "EFFECTOR",
            "GAS",
        )
        linked = False
        changes = {}
        try:
            effector = self._initialize_fluid_role(modifier, "EFFECTOR", created)
            patch = dict(settings or {})
            patch["effector_type"] = effector_type
            changes = _patch_rna(effector, patch, _EFFECTOR_FIELDS)
            linked = _link_object(collection, obj)
            bpy.context.view_layer.update()
        except Exception:
            self._rollback_fluid_role(obj, modifier, "EFFECTOR", changes, collection, linked, created)
            raise
        return {
            "changed_objects": [obj.name, domain_obj.name],
            "object": obj.name,
            "modifier": modifier.name,
            "created": created,
            "domain": domain_obj.name,
            "domain_modifier": domain_modifier.name,
            "domain_type": "GAS",
            "effector_collection": collection.name,
            "collection_membership_added": linked,
            "changes": changes,
            "invalidated_cache_stages": ["DATA", "NOISE"],
            "warnings": _warning_for_effector(obj),
        }
