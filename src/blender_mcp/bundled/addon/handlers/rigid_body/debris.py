# pyright: reportGeneralTypeIssues=false
"""Deterministic linked-mesh debris generation."""

import contextlib
import math
import random
import uuid

from itertools import combinations

import bpy
import mathutils

from .inspection_and_setup import (
    _BODY_FIELDS,
    _aabb_overlap,
    _add_rigid_body,
    _apply_patch,
    _body_info,
    _bounds,
    _collection_in_scene,
    _ensure_collection,
    _ensure_world,
    _evaluated_geometry,
    _mesh_volume,
    _prepare_cache_mutation,
    _scene,
    _validate_object_batch,
    _validate_rna_properties,
)


def _debris_region_bounds(scene, region):
    shape = region.get("shape")
    if shape == "BOX":
        minimum = region.get("minimum")
        maximum = region.get("maximum")
        if minimum is None or maximum is None or any(a >= b for a, b in zip(minimum, maximum, strict=True)):
            raise ValueError("BOX region requires finite minimum values below maximum values")
        return {"min": list(minimum), "max": list(maximum)}
    if shape == "SPHERE":
        center = region.get("center")
        radius = region.get("radius")
        if center is None or radius is None or not math.isfinite(radius) or radius <= 0:
            raise ValueError("SPHERE region requires a finite center and positive radius")
        return {
            "min": [component - radius for component in center],
            "max": [component + radius for component in center],
        }
    if shape != "COLLECTION_BOUNDS":
        raise ValueError(f"Unsupported debris region shape: {shape}")
    collection = bpy.data.collections.get(region.get("collection_name"))
    if collection is None or not _collection_in_scene(collection, scene):
        raise ValueError("COLLECTION_BOUNDS requires a collection linked to the requested scene")
    points = []
    for obj in collection.all_objects:
        if obj.name not in scene.objects:
            continue
        points.extend(obj.matrix_world @ mathutils.Vector(corner) for corner in obj.bound_box)
    result = _bounds(points)
    if result is None or any(dimension <= 1e-9 for dimension in result["dimensions"]):
        raise ValueError(f"Collection '{collection.name}' has no non-degenerate world bounds")
    return {"min": result["min"], "max": result["max"]}


def _random_point(generator, region, bounds):
    if region["shape"] != "SPHERE":
        return mathutils.Vector(
            tuple(generator.uniform(low, high) for low, high in zip(bounds["min"], bounds["max"], strict=True))
        )
    center = mathutils.Vector(region["center"])
    radius = float(region["radius"])
    while True:
        candidate = mathutils.Vector(tuple(generator.uniform(-1.0, 1.0) for _axis in range(3)))
        squared = candidate.length_squared
        if 1e-12 < squared <= 1.0:
            return center + candidate * (radius * generator.random() ** (1.0 / 3.0) / math.sqrt(squared))


def _weighted_source(generator, prepared_sources):
    pick = generator.uniform(0.0, sum(item[1] for item in prepared_sources))
    cumulative = 0.0
    for source, weight, volume in prepared_sources:
        cumulative += weight
        if pick <= cumulative:
            return source, volume
    source, _weight, volume = prepared_sources[-1]
    return source, volume


class RigidBodyDebrisHandlers:
    """Create bounded, repeatable debris fields from reusable source meshes."""

    def create_rigid_body_debris_field(
        self,
        scene_name,
        field_name,
        sources,
        count,
        seed,
        region,
        density,
        transform_range,
        collection_name="Rigid Body Debris",
        collision_shape="CONVEX_HULL",
        collision_layers=(3,),
        start_deactivated=True,
        settings=None,
        confirm_delete_baked_cache=False,
    ):
        scene = _scene(scene_name)
        if not field_name or not isinstance(seed, int) or isinstance(seed, bool):
            raise ValueError("field_name must be non-empty and seed must be an integer")
        if not 1 <= count <= 500:
            raise ValueError("count must be in [1, 500]")
        if not math.isfinite(density) or density <= 0:
            raise ValueError("density must be finite and positive")
        source_names = [item.get("object_name") for item in sources]
        if not source_names or len(source_names) > 32 or len(source_names) != len(set(source_names)):
            raise ValueError("sources must contain 1-32 unique object names")
        source_objects = _validate_object_batch(scene, source_names)
        if any(obj.type != "MESH" for obj in source_objects):
            raise ValueError("Debris sources must be mesh objects")
        by_name = {obj.name: obj for obj in source_objects}
        prepared_sources = []
        for source_spec in sources:
            weight = source_spec.get("weight", 1.0)
            if not math.isfinite(weight) or weight <= 0:
                raise ValueError("Every debris source weight must be finite and positive")
            source = by_name[source_spec["object_name"]]
            volume, _determinant = _mesh_volume(source)
            prepared_sources.append((source, weight, volume))
        minimum_scale = transform_range.get("uniform_scale_min", 1.0)
        maximum_scale = transform_range.get("uniform_scale_max", 1.0)
        rotation_min = transform_range.get("rotation_min_radians", (0.0, 0.0, 0.0))
        rotation_max = transform_range.get("rotation_max_radians", (0.0, 0.0, 0.0))
        if not 0 < minimum_scale <= maximum_scale or any(
            low > high for low, high in zip(rotation_min, rotation_max, strict=True)
        ):
            raise ValueError("Debris transform ranges are invalid")
        mass_limits = [
            (density * volume * minimum_scale**3, density * volume * maximum_scale**3)
            for _source, _weight, volume in prepared_sources
        ]
        if any(low < 0.001 or high > 10_000.0 for low, high in mass_limits):
            raise ValueError("Density and scale can produce mass outside Blender's [0.001, 10000] range")
        layers = set(collision_layers)
        if not layers or len(layers) != len(collision_layers) or any(not 1 <= layer <= 20 for layer in layers):
            raise ValueError("collision_layers must contain unique values in [1, 20]")
        allowed_shapes = {"BOX", "SPHERE", "CAPSULE", "CYLINDER", "CONE", "CONVEX_HULL"}
        if collision_shape not in allowed_shapes:
            raise ValueError(f"Unsupported debris collision shape: {collision_shape}")
        patch = dict(settings or {})
        if patch.get("type", "ACTIVE") != "ACTIVE" or "mass" in patch:
            raise ValueError("Debris settings cannot override ACTIVE type or density-derived mass")
        if patch.get("collision_shape", collision_shape) != collision_shape:
            raise ValueError("settings.collision_shape must match collision_shape")
        patch.update(
            {
                "type": "ACTIVE",
                "collision_shape": collision_shape,
                "use_deactivation": bool(start_deactivated or patch.get("use_deactivation")),
                "use_start_deactivated": bool(start_deactivated),
            }
        )
        _validate_rna_properties(bpy.types.RigidBodyObject.bl_rna.properties, patch, _BODY_FIELDS)
        bounds = _debris_region_bounds(scene, region)
        names = [f"{field_name} {index:03d}" for index in range(1, count + 1)]
        conflicts = [name for name in names if bpy.data.objects.get(name) is not None]
        if conflicts:
            raise ValueError(f"Debris object names already exist: {conflicts[:10]}")
        world = _ensure_world(scene)
        cache_freed = _prepare_cache_mutation(scene, world, confirm_delete_baked_cache)
        collection, _created = _ensure_collection(scene, collection_name)
        generator = random.Random(seed)
        rig_id = uuid.uuid4().hex
        created = []
        records = []
        try:
            for name in names:
                source, source_volume = _weighted_source(generator, prepared_sources)
                scale_factor = generator.uniform(minimum_scale, maximum_scale)
                obj = bpy.data.objects.new(name, source.data)
                collection.objects.link(obj)
                obj.location = _random_point(generator, region, bounds)
                obj.rotation_mode = "XYZ"
                obj.rotation_euler = tuple(
                    generator.uniform(low, high) for low, high in zip(rotation_min, rotation_max, strict=True)
                )
                obj.scale = tuple(component * scale_factor for component in source.scale)
                _add_rigid_body(scene, obj, "ACTIVE")
                _apply_patch(obj.rigid_body, patch, _BODY_FIELDS)
                obj.rigid_body.mass = density * source_volume * scale_factor**3
                obj.rigid_body.collision_collections = tuple(layer in layers for layer in range(1, 21))
                if world.collection is not None and obj.name not in world.collection.objects:
                    world.collection.objects.link(obj)
                obj["blendermcp_rigid_body_rig_id"] = rig_id
                obj["blendermcp_rigid_body_role"] = "debris"
                obj["blendermcp_rigid_body_source"] = source.name
                obj["blendermcp_rigid_body_schema"] = 1
                created.append(obj)
                records.append(
                    {
                        "object": obj.name,
                        "source": source.name,
                        "location_world": list(obj.location),
                        "rotation_euler_xyz_radians": list(obj.rotation_euler),
                        "uniform_scale_factor": scale_factor,
                        "mass": float(obj.rigid_body.mass),
                    }
                )
        except Exception:
            for obj in reversed(created):
                with contextlib.suppress(Exception):
                    bpy.data.objects.remove(obj, do_unlink=True)
            raise
        evaluated_bounds = {obj.name: _evaluated_geometry(obj)["bounds_world"] for obj in created}
        overlaps = []
        truncated = False
        for first, second in combinations(created, 2):
            if len(overlaps) >= 256:
                truncated = True
                break
            first_bounds = evaluated_bounds[first.name]
            second_bounds = evaluated_bounds[second.name]
            if first_bounds and second_bounds and _aabb_overlap(first_bounds, second_bounds):
                overlaps.append([first.name, second.name])
        return {
            "changed_objects": names,
            "field": field_name,
            "rig_id": rig_id,
            "collection": collection.name,
            "seed": seed,
            "count": len(created),
            "source_mapping": records,
            "total_mass": sum(record["mass"] for record in records),
            "collision_layers": sorted(layers),
            "initial_aabb_overlap_candidates": overlaps,
            "overlap_results_truncated": truncated,
            "rigid_body_template": _body_info(created[0]),
            "linked_mesh_data": True,
            "cache_freed": cache_freed,
            "warnings": [
                "AABB overlap candidates are conservative and are not Bullet contact results.",
                *(["The protected rigid-body bake was explicitly freed."] if cache_freed else []),
            ],
        }
