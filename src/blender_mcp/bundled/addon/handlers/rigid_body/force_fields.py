# pyright: reportGeneralTypeIssues=false
"""Rigid-body world force-field creation and configuration."""

import contextlib
import math
import uuid

import bpy
import mathutils

from ...helpers import preserve_mode_and_selection
from ..rna_patch import read_fields, serialize
from .inspection_and_setup import (
    _EFFECTOR_FIELDS,
    _apply_patch,
    _ensure_collection,
    _ensure_world,
    _prepare_cache_mutation,
    _restore_fields,
    _scene,
    _validate_rna_properties,
    _view_layer_for,
)

_FIELD_FIELDS = {
    "type",
    "strength",
    "flow",
    "noise",
    "seed",
    "shape",
    "falloff_type",
    "falloff_power",
    "distance_min",
    "distance_max",
    "apply_to_location",
    "apply_to_rotation",
    "use_min_distance",
    "use_max_distance",
}


def _finite_vector(value, label):
    if len(value) != 3 or any(
        isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item) for item in value
    ):
        raise ValueError(f"{label} must contain three finite numbers")


def _field_info(obj):
    return {
        "object": obj.name,
        "location_world": list(obj.matrix_world.translation),
        "rotation_mode": obj.rotation_mode,
        "settings": read_fields(obj.field, _FIELD_FIELDS),
        "collections": sorted(collection.name for collection in obj.users_collection),
        "animation": bool(obj.animation_data and (obj.animation_data.action or obj.animation_data.drivers)),
    }


class RigidBodyForceFieldHandlers:
    """Author bounded force fields and patch rigid-body world effector weights."""

    def configure_rigid_body_force_fields(
        self,
        scene_name,
        force_collection_name,
        fields,
        create_collection=False,
        weights=None,
        confirm_delete_baked_cache=False,
    ):
        scene = _scene(scene_name)
        weights = dict(weights or {})
        if not fields and not weights:
            raise ValueError("Provide at least one force field or an effector-weights patch")
        if len(fields) > 64 or len({item["object_name"] for item in fields}) != len(fields):
            raise ValueError("fields must contain at most 64 unique object names")
        collection = bpy.data.collections.get(force_collection_name)
        if collection is None:
            if not create_collection:
                raise ValueError(f"Collection not found: {force_collection_name}")
            collection, _created = _ensure_collection(scene, force_collection_name)
        elif collection != scene.collection and collection not in scene.collection.children_recursive:
            raise ValueError(f"Collection '{collection.name}' is not linked to scene '{scene.name}'")
        view_layer = _view_layer_for(scene)
        allowed_types = {"FORCE", "WIND", "VORTEX", "TURBULENCE", "DRAG", "HARMONIC"}
        resolved = []
        for spec in fields:
            field_type = spec["field_type"]
            if field_type not in allowed_types:
                raise ValueError(f"Unsupported rigid-body force field type: {field_type}")
            _finite_vector(spec.get("location", (0.0, 0.0, 0.0)), "location")
            _finite_vector(spec.get("rotation_euler", (0.0, 0.0, 0.0)), "rotation_euler")
            obj = bpy.data.objects.get(spec["object_name"])
            if obj is None and not spec.get("create_if_missing"):
                raise ValueError(f"Force-field object not found: {spec['object_name']}")
            if obj is not None and obj.name not in scene.objects:
                raise ValueError(f"Force-field object '{obj.name}' is not linked to scene '{scene.name}'")
            if obj is not None and obj.field is None:
                raise ValueError(f"Object '{obj.name}' has no FieldSettings")
            patch = {"type": field_type}
            for name in _FIELD_FIELDS - {"type", "use_min_distance", "use_max_distance"}:
                if name in spec:
                    patch[name] = spec[name]
            if "distance_min" in patch:
                patch["use_min_distance"] = True
            if "distance_max" in patch:
                patch["use_max_distance"] = True
            if obj is not None:
                _validate_rna_properties(obj.field.bl_rna.properties, patch, _FIELD_FIELDS)
            resolved.append((obj, spec, patch))
        world = _ensure_world(scene)
        cache_freed = _prepare_cache_mutation(scene, world, confirm_delete_baked_cache)
        collection_name = weights.pop("collection_name", None)
        clear_collection = weights.pop("clear_collection", False)
        if collection_name and clear_collection:
            raise ValueError("collection_name and clear_collection are mutually exclusive")
        weight_collection = None
        if collection_name:
            weight_collection = bpy.data.collections.get(collection_name)
            if weight_collection is None or (
                weight_collection != scene.collection and weight_collection not in scene.collection.children_recursive
            ):
                raise ValueError(f"Effector collection '{collection_name}' is not linked to scene '{scene.name}'")
        _validate_rna_properties(world.effector_weights.bl_rna.properties, weights, _EFFECTOR_FIELDS)
        created = []
        linked = []
        snapshots = []
        old_weights = {name: getattr(world.effector_weights, name) for name in weights}
        old_weight_collection = world.effector_weights.collection
        rig_id = uuid.uuid4().hex
        try:
            for existing, spec, patch in resolved:
                obj = existing
                if obj is None:
                    with bpy.context.temp_override(scene=scene, view_layer=view_layer), preserve_mode_and_selection():
                        result = bpy.ops.object.effector_add(
                            type=spec["field_type"],
                            location=spec.get("location", (0.0, 0.0, 0.0)),
                            rotation=spec.get("rotation_euler", (0.0, 0.0, 0.0)),
                        )
                        obj = bpy.context.active_object
                    if "FINISHED" not in result or obj is None or obj.field is None:
                        raise RuntimeError(
                            f"Blender did not create force field '{spec['object_name']}': {sorted(result)}"
                        )
                    obj.name = spec["object_name"]
                    created.append(obj)
                else:
                    snapshots.append((obj, obj.matrix_world.copy(), {name: getattr(obj.field, name) for name in patch}))
                    obj.matrix_world = mathutils.Matrix.LocRotScale(
                        spec.get("location", tuple(obj.matrix_world.translation)),
                        mathutils.Euler(spec.get("rotation_euler", tuple(obj.rotation_euler))).to_quaternion(),
                        obj.matrix_world.to_scale(),
                    )
                if obj.name not in collection.objects:
                    collection.objects.link(obj)
                    linked.append(obj)
                for other in list(obj.users_collection):
                    if other != collection and obj in created:
                        other.objects.unlink(obj)
                _apply_patch(obj.field, patch, _FIELD_FIELDS)
                obj["blendermcp_rigid_body_rig_id"] = rig_id
                obj["blendermcp_rigid_body_role"] = "force_field"
                obj["blendermcp_rigid_body_schema"] = 1
            weight_changes = _apply_patch(world.effector_weights, weights, _EFFECTOR_FIELDS)
            if collection_name or clear_collection:
                world.effector_weights.collection = weight_collection if collection_name else None
        except Exception:
            for obj in reversed(created):
                with contextlib.suppress(Exception):
                    bpy.data.objects.remove(obj, do_unlink=True)
            for obj, matrix, settings in snapshots:
                obj.matrix_world = matrix
                _restore_fields(obj.field, settings)
            for obj in linked:
                if obj not in created and obj.name in collection.objects:
                    with contextlib.suppress(Exception):
                        collection.objects.unlink(obj)
            _restore_fields(world.effector_weights, old_weights)
            world.effector_weights.collection = old_weight_collection
            raise
        field_objects = [bpy.data.objects[spec["object_name"]] for _obj, spec, _patch in resolved]
        return {
            "changed_objects": [obj.name for obj in field_objects],
            "changed_resources": [scene.name],
            "rig_id": rig_id,
            "force_collection": collection.name,
            "created": [obj.name for obj in created],
            "fields": [_field_info(obj) for obj in field_objects],
            "effector_weight_changes": weight_changes,
            "effector_collection": serialize(world.effector_weights.collection),
            "cache_freed": cache_freed,
        }
