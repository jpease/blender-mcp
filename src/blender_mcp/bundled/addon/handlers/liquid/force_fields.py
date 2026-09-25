# pyright: reportGeneralTypeIssues=false
"""Liquid domain force-field and effector-weight configuration handlers."""

import contextlib

import bpy
import mathutils

from ...helpers import preserve_mode_and_selection
from ..rna_patch import finite, patch_rna, read_fields, restore_rna, validate_rna_value
from .inspection_and_setup import (
    _ensure_collection,
    _get_domain,
    _link_object,
    _reject_baked,
)

_WEIGHT_FIELDS = {
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
    "boid",
    "turbulence",
    "drag",
    "smokeflow",
}
_FIELD_FIELDS = {
    "type",
    "strength",
    "shape",
    "falloff_type",
    "noise",
    "seed",
    "use_min_distance",
    "distance_min",
    "use_max_distance",
    "distance_max",
}


def _field_snapshot(obj):
    return {
        "matrix_basis": obj.matrix_basis.copy(),
        "settings": read_fields(obj.field, _FIELD_FIELDS, typed_ids=True),
    }


def _create_force_field(scene, view_layer, collection, spec):
    with preserve_mode_and_selection(), bpy.context.temp_override(scene=scene, view_layer=view_layer):
        result = bpy.ops.object.effector_add(
            type=spec["field_type"],
            location=spec["location"],
            rotation=spec["rotation_euler"],
        )
        obj = bpy.context.active_object
    if "FINISHED" not in result or obj is None or obj.field is None:
        raise RuntimeError(f"Blender did not create force field '{spec['object_name']}': {sorted(result)}")
    obj.name = spec["object_name"]
    if obj.name not in collection.objects:
        collection.objects.link(obj)
    for linked_collection in list(obj.users_collection):
        if linked_collection != collection:
            linked_collection.objects.unlink(obj)
    return obj


def _restore_field(obj, snapshot):
    obj.matrix_basis = snapshot["matrix_basis"]
    for name, value in snapshot["settings"].items():
        with contextlib.suppress(Exception):
            setattr(obj.field, name, value)


class LiquidForceFieldHandlers:
    """Attach and configure Blender force fields that influence a liquid domain."""

    def configure_liquid_force_fields(
        self,
        scene_name,
        domain_object_name,
        modifier_name,
        fields,
        force_collection_name,
        create_collection=False,
        weights=None,
    ):
        scene = bpy.data.scenes.get(scene_name)
        if scene is None:
            raise ValueError(f"Scene not found: {scene_name}")
        domain_obj, modifier, domain = _get_domain(domain_object_name, modifier_name)
        if domain_obj.name not in scene.objects:
            raise ValueError(f"Domain '{domain_obj.name}' is not linked to scene '{scene.name}'")
        _reject_baked(domain)
        if not fields and not weights:
            raise ValueError("Provide at least one force field or an effector-weights patch")
        if len(fields) > 64 or len({item["object_name"] for item in fields}) != len(fields):
            raise ValueError("fields must contain at most 64 unique object names")
        collection = bpy.data.collections.get(force_collection_name)
        collection_created_link = False
        if collection is None:
            if not create_collection:
                raise ValueError(f"Collection not found: {force_collection_name}")
            collection, _created, collection_created_link = _ensure_collection(scene, force_collection_name)
        elif collection != scene.collection and collection not in scene.collection.children_recursive:
            raise ValueError(f"Collection '{collection.name}' is not linked to scene '{scene.name}'")
        view_layer = next((layer for layer in scene.view_layers if domain_obj.name in layer.objects), None)
        if view_layer is None:
            raise ValueError(f"Domain '{domain_obj.name}' is excluded from every view layer in scene '{scene.name}'")
        resolved = []
        for spec in fields:
            name = spec["object_name"]
            obj = bpy.data.objects.get(name)
            if obj is None and not spec.get("create_if_missing"):
                raise ValueError(f"Force-field object not found: {name}")
            if obj is not None and obj.name not in scene.objects:
                raise ValueError(f"Force-field object '{name}' is not linked to scene '{scene.name}'")
            if obj is not None and obj.field is None:
                raise ValueError(
                    f"Object '{name}' has no FieldSettings; use a Blender force-field object or choose a new name "
                    "with create_if_missing=True"
                )
            for vector_name in ("location", "rotation_euler"):
                vector = spec[vector_name]
                finite(vector, vector_name)
                if len(vector) != 3:
                    raise ValueError(f"{vector_name} must contain three finite values")
            prospective = {
                "type": spec["field_type"],
                **{name: spec[name] for name in _FIELD_FIELDS if name in spec and name != "type"},
            }
            if obj is not None:
                for name, value in prospective.items():
                    validate_rna_value(obj.field, name, value)
            resolved.append((obj, spec, prospective))
        weight_changes = {}
        linked_objects = []
        created_objects = []
        snapshots = []
        old_collection = domain.force_collection
        try:
            for resolved_obj, spec, prospective in resolved:
                obj = resolved_obj
                if obj is None:
                    obj = _create_force_field(scene, view_layer, collection, spec)
                    created_objects.append(obj)
                else:
                    snapshots.append((obj, _field_snapshot(obj)))
                    if _link_object(collection, obj):
                        linked_objects.append(obj)
                world_scale = obj.matrix_world.to_scale()
                obj.matrix_world = mathutils.Matrix.LocRotScale(
                    spec["location"],
                    mathutils.Euler(spec["rotation_euler"]).to_quaternion(),
                    tuple(world_scale),
                )
                field_patch = prospective if resolved_obj is not None else {**prospective, "type": spec["field_type"]}
                patch_rna(obj.field, field_patch, _FIELD_FIELDS)
                obj["blendermcp_liquid_force"] = domain_obj.name
            domain.force_collection = collection
            if weights:
                weight_changes = patch_rna(domain.effector_weights, weights, _WEIGHT_FIELDS)
            bpy.context.view_layer.update()
        except Exception:
            restore_rna(domain.effector_weights, weight_changes)
            domain.force_collection = old_collection
            for obj, snapshot in reversed(snapshots):
                with contextlib.suppress(Exception):
                    _restore_field(obj, snapshot)
            for obj in linked_objects:
                with contextlib.suppress(Exception):
                    collection.objects.unlink(obj)
            for obj in reversed(created_objects):
                with contextlib.suppress(Exception):
                    bpy.data.objects.remove(obj, do_unlink=True)
            if collection_created_link:
                with contextlib.suppress(Exception):
                    scene.collection.children.unlink(collection)
            raise
        force_info = [
            {
                "object": obj.name,
                "created": obj in created_objects,
                "field": read_fields(obj.field, _FIELD_FIELDS, typed_ids=True),
                "coordinate_space": "WORLD",
                "world_location": list(obj.matrix_world.translation),
                "world_rotation_quaternion": list(obj.matrix_world.to_quaternion()),
            }
            for obj, _spec, _prospective in [
                (bpy.data.objects.get(spec["object_name"]), spec, prospective) for _obj, spec, prospective in resolved
            ]
        ]
        return {
            "changed_objects": [domain_obj.name, *[item["object"] for item in force_info]],
            "domain": domain_obj.name,
            "modifier": modifier.name,
            "force_collection": collection.name,
            "force_fields": force_info,
            "effector_weight_changes": weight_changes,
            "scene_gravity_world": list(scene.gravity),
            "domain_gravity_multiplier": float(domain.effector_weights.gravity),
            "invalidated_cache_stages": ["DATA", "MESH", "PARTICLES"],
            "warnings": [
                "Force influence depends on domain resolution and time steps.",
                "Mantaflow does not provide bidirectional rigid-body coupling.",
            ],
        }
