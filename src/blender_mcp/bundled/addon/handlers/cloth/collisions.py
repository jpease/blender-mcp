"""Blender-main-thread handlers for cloth collision configuration."""

from __future__ import annotations

import contextlib

import bpy

from ...helpers import preserve_mode_and_selection, set_active, sync_from_editmode
from ..rna_patch import get_object, patch_rna, restore_rna
from ._ownership import _tag_owned_component, _tag_owned_membership
from .inspection_and_setup import (
    _CLOTH_COLLISION_FIELDS,
    _COLLIDER_FIELDS,
    _DEFORMING_MODIFIERS,
    _TOPOLOGY_MODIFIERS,
    _animation_info,
    _cache_info,
    _collection_in_scene,
    _edge_lengths,
    _evaluated_counts,
    _get_cloth,
    _get_modifier,
    _max_keyed_location_delta,
    _object_scenes,
    _reject_baked,
    _tag_update,
)


def _affected_cloths(collider):
    affected = []
    for scene in bpy.data.scenes:
        if collider.name not in scene.objects:
            continue
        for obj in scene.objects:
            for modifier in obj.modifiers:
                if modifier.type != "CLOTH":
                    continue
                collision_settings = modifier.collision_settings
                if not collision_settings.use_collision:
                    continue
                collection = collision_settings.collection
                if collection is None or collider.name in collection.all_objects:
                    affected.append((obj, modifier))
    return affected


def _eligible_active_colliders(cloth_obj, collision_settings):
    if not collision_settings.use_collision:
        return []
    candidates = {}
    for scene in _object_scenes(cloth_obj):
        scoped = collision_settings.collection.all_objects if collision_settings.collection else scene.objects
        for candidate in scoped:
            if (
                any(modifier.type == "COLLISION" for modifier in candidate.modifiers)
                and candidate.collision
                and candidate.collision.use
            ):
                candidates[candidate.name] = candidate
    return [candidates[name] for name in sorted(candidates)]


def _collider_order_warnings(obj, collision_modifier):
    modifier_index = list(obj.modifiers).index(collision_modifier)
    downstream = [
        modifier.name
        for modifier in list(obj.modifiers)[modifier_index + 1 :]
        if modifier.type in _DEFORMING_MODIFIERS | _TOPOLOGY_MODIFIERS
    ]
    if not downstream:
        return []
    return [
        f"Deformation/topology modifiers after Collision may not be represented by the collision surface: {downstream}"
    ]


def _is_high_resolution_collider(cloth_obj, collider):
    return len(collider.data.polygons) > max(10_000, len(cloth_obj.data.polygons) * 4)


class ClothCollisionHandlers:
    """Blender-main-thread handlers for cloth collision configuration."""

    def _configure_collisions(self, obj, modifier, patch):
        patch = dict(patch or {})
        collection_name = patch.pop("collection_name", None)
        clear_collection = patch.pop("clear_collection", False)
        if collection_name and clear_collection:
            raise ValueError("collection_name and clear_collection cannot be combined")
        collection = None
        if collection_name:
            collection = bpy.data.collections.get(collection_name)
            if collection is None:
                raise ValueError(f"Collection not found: {collection_name}")
            if not any(_collection_in_scene(collection, scene) for scene in _object_scenes(obj)):
                raise ValueError(f"Collection '{collection_name}' is not linked to a scene containing '{obj.name}'")
        for field in ("vertex_group_object_collisions", "vertex_group_self_collisions"):
            if field in patch and patch[field] and obj.vertex_groups.get(patch[field]) is None:
                raise ValueError(f"Vertex group not found: {patch[field]}")
        for field in ("distance_min", "self_distance_min"):
            if field in patch and patch[field] <= 0:
                raise ValueError(f"{field} must be positive")
        old_collection = modifier.collision_settings.collection
        changes = patch_rna(modifier.collision_settings, patch, _CLOTH_COLLISION_FIELDS)
        try:
            if collection_name or clear_collection:
                modifier.collision_settings.collection = collection if collection_name else None
                changes["collection"] = {
                    "old": old_collection.name if old_collection else None,
                    "new": collection.name if collection else None,
                }
        except Exception:
            restore_rna(modifier.collision_settings, changes)
            modifier.collision_settings.collection = old_collection
            raise
        return changes

    def configure_cloth_collisions(self, object_name, modifier_name, patch):
        obj, modifier = _get_cloth(object_name, modifier_name)
        _reject_baked([(obj, modifier)])
        if not patch:
            raise ValueError("Collision patch cannot be empty")
        old_collection = modifier.collision_settings.collection
        changes = self._configure_collisions(obj, modifier, patch)
        try:
            _tag_update(obj)
        except Exception:
            restore_rna(modifier.collision_settings, changes)
            modifier.collision_settings.collection = old_collection
            raise
        edges = _edge_lengths(obj)
        warnings = []
        collision = modifier.collision_settings
        if edges["min"] and collision.distance_min > edges["min"] * 0.5:
            warnings.append("Object-collision distance exceeds half the smallest base edge and may separate violently.")
        if edges["min"] and collision.self_distance_min > edges["min"] * 0.5:
            warnings.append("Self-collision distance exceeds half the smallest base edge and may separate violently.")
        colliders = _eligible_active_colliders(obj, collision)
        outer_thicknesses = [float(collider.collision.thickness_outer) for collider in colliders]
        maximum_outer_thickness = max(outer_thicknesses, default=None)
        if (
            edges["min"]
            and maximum_outer_thickness is not None
            and collision.distance_min + maximum_outer_thickness > edges["min"]
        ):
            warnings.append(
                "Combined cloth distance and maximum collider outer thickness exceed the smallest base edge; "
                "inspect initial separation and contact stability."
            )
        keyed_motion = _max_keyed_location_delta(obj)
        if edges["min"] and keyed_motion and keyed_motion > edges["min"] * max(collision.collision_quality, 1):
            warnings.append(
                "Keyed object motion is large relative to the smallest base edge and collision quality; "
                "representative-frame tests may reveal tunneling."
            )
        return {
            "changed_objects": [obj.name],
            "object": obj.name,
            "modifier": modifier.name,
            "changes": changes,
            "scope": {
                "object_collision": collision.use_collision,
                "collection": collision.collection.name if collision.collection else None,
                "self_collision": collision.use_self_collision,
                "object_exclusion_group": collision.vertex_group_object_collisions,
                "self_exclusion_group": collision.vertex_group_self_collisions,
            },
            "distance_context": {
                "smallest_base_edge_object_local": edges["min"],
                "eligible_active_colliders": [collider.name for collider in colliders],
                "maximum_collider_outer_thickness": maximum_outer_thickness,
                "maximum_keyed_location_channel_units_per_frame": keyed_motion,
            },
            "point_cache": _cache_info(modifier.point_cache),
            "warnings": warnings,
        }

    def add_cloth_collider(
        self,
        object_name,
        modifier_name="Collision",
        existing_policy="ERROR",
        settings=None,
        registrations=None,
    ):
        obj = get_object(object_name, {"MESH", "CURVE"})
        if obj.type == "MESH":
            sync_from_editmode(obj)
        evaluated_geometry = _evaluated_counts(obj)
        if not evaluated_geometry["vertices"] or not evaluated_geometry["faces"]:
            raise ValueError(f"Collider '{object_name}' must evaluate to nonempty surface geometry")
        if existing_policy not in {"ERROR", "REUSE"}:
            raise ValueError("existing_policy must be ERROR or REUSE")
        registrations = registrations or []
        settings = dict(settings or {})
        if settings.get("use") is False:
            raise ValueError("add_cloth_collider requires CollisionSettings.use to remain enabled")
        settings["use"] = True
        resolved = []
        for item in registrations:
            cloth_obj, cloth_mod = _get_cloth(item["cloth_object_name"], item["cloth_modifier_name"])
            collection = bpy.data.collections.get(item["collection_name"])
            if collection is None:
                raise ValueError(f"Collection not found: {item['collection_name']}")
            if not any(_collection_in_scene(collection, scene) for scene in _object_scenes(cloth_obj)):
                raise ValueError(
                    f"Collection '{collection.name}' is not linked to a scene containing '{cloth_obj.name}'"
                )
            resolved.append((cloth_obj, cloth_mod, collection))
        _reject_baked([(cloth_obj, cloth_mod) for cloth_obj, cloth_mod, _collection in resolved])
        existing = obj.modifiers.get(modifier_name)
        created = False
        if existing:
            if existing.type != "COLLISION":
                raise ValueError(f"Modifier '{modifier_name}' already exists and is not Collision")
            if existing_policy == "ERROR":
                raise ValueError(f"Collision modifier '{modifier_name}' already exists on '{object_name}'")
            modifier = existing
        else:
            try:
                modifier = obj.modifiers.new(name=modifier_name, type="COLLISION")
                bpy.context.view_layer.update()
            except Exception as exc:
                failed = obj.modifiers.get(modifier_name)
                if failed is not None and failed.type == "COLLISION":
                    with contextlib.suppress(Exception):
                        obj.modifiers.remove(failed)
                with preserve_mode_and_selection():
                    set_active(obj)
                    result = bpy.ops.object.modifier_add(type="COLLISION")
                    if "FINISHED" not in result:
                        raise RuntimeError(
                            f"Blender Collision modifier operator did not finish: {sorted(result)}"
                        ) from exc
                    modifier = obj.modifiers[-1]
                    modifier.name = modifier_name
            created = True
        linked = []
        membership_ownership = []
        prior_collections = {}
        changes = {}
        ownership = None
        try:
            if obj.collision is None:
                raise RuntimeError("Blender did not initialize Object.collision")
            changes = patch_rna(obj.collision, settings, _COLLIDER_FIELDS)
            for cloth_obj, cloth_mod, collection in resolved:
                prior_collections[cloth_obj.name, cloth_mod.name] = cloth_mod.collision_settings.collection
                if obj.name not in collection.objects:
                    collection.objects.link(obj)
                    linked.append(collection)
                    membership_ownership.append(_tag_owned_membership(obj, collection))
                cloth_mod.collision_settings.collection = collection
            if created:
                ownership = _tag_owned_component(obj, modifier, "collider")
            _tag_update(obj)
        except Exception:
            for record in membership_ownership:
                with contextlib.suppress(Exception):
                    del obj[record["object_property"]]
            for collection in linked:
                with contextlib.suppress(Exception):
                    collection.objects.unlink(obj)
            for cloth_obj, cloth_mod, _collection in resolved:
                old = prior_collections.get((cloth_obj.name, cloth_mod.name))
                cloth_mod.collision_settings.collection = old
            if ownership is not None:
                with contextlib.suppress(Exception):
                    del obj[ownership["object_property"]]
            if created:
                with contextlib.suppress(Exception):
                    obj.modifiers.remove(modifier)
            elif obj.collision is not None:
                restore_rna(obj.collision, changes)
            raise
        return {
            "changed_objects": [obj.name, *sorted({cloth_obj.name for cloth_obj, _mod, _col in resolved})],
            "object": obj.name,
            "modifier": modifier.name,
            "created": created,
            "evaluated_geometry": evaluated_geometry,
            "modifier_index": list(obj.modifiers).index(modifier),
            "ownership": ownership,
            "membership_ownership": membership_ownership,
            "animation": _animation_info(obj),
            "settings_changes": changes,
            "registrations": [
                {
                    "cloth_object": cloth_obj.name,
                    "cloth_modifier": cloth_mod.name,
                    "collection": collection.name,
                }
                for cloth_obj, cloth_mod, collection in resolved
            ],
            "new_collection_memberships": [collection.name for collection in linked],
            "affected_cloth_caches": [_cache_info(mod.point_cache) for _cloth, mod in _affected_cloths(obj)],
            "warnings": [*self._scale_warnings(obj), *_collider_order_warnings(obj, modifier)],
        }

    def configure_cloth_collider(self, object_name, modifier_name, patch):
        obj = get_object(object_name, {"MESH", "CURVE"})
        modifier = _get_modifier(obj, modifier_name, "COLLISION")
        affected = _affected_cloths(obj)
        _reject_baked(affected)
        if not patch:
            raise ValueError("Collider patch cannot be empty")
        for field in ("thickness_outer", "cloth_friction", "damping"):
            if field in patch and patch[field] < 0:
                raise ValueError(f"{field} must be nonnegative")
        changes = patch_rna(obj.collision, patch, _COLLIDER_FIELDS)
        try:
            _tag_update(obj)
        except Exception:
            restore_rna(obj.collision, changes)
            raise
        warnings = [*self._scale_warnings(obj), *_collider_order_warnings(obj, modifier)]
        if (obj.collision.use_culling or obj.collision.use_normal) and obj.type == "MESH":
            zero_area = sum(poly.area <= 1e-12 for poly in obj.data.polygons)
            if zero_area:
                warnings.append(f"One-sided collision uses normals, but {zero_area} base faces have zero area.")
        return {
            "changed_objects": [obj.name],
            "object": obj.name,
            "modifier": modifier.name,
            "changes": changes,
            "affected_cloth_caches": [
                {"object": cloth.name, "modifier": cloth_mod.name, "point_cache": _cache_info(cloth_mod.point_cache)}
                for cloth, cloth_mod in affected
            ],
            "warnings": warnings,
        }
