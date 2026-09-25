"""Blender-main-thread handlers for cloth cache and component lifecycle."""

from __future__ import annotations

import contextlib
import json

import bpy

from ..rna_patch import get_object, patch_rna, restore_rna, validate_rna_value
from ..simulation_cache import require_cache_confirmation, set_cache_frame_range
from ._cache_helpers import (
    _all_cloth_caches,
    _cloth_cache_dependency_issues,
    _external_directory_evidence,
    _prospective_cache_identity,
    _run_point_cache_operator,
    _shared_cache_identity,
)
from ._ownership import _owned_component_records, _owned_membership_record
from .collisions import _affected_cloths
from .inspection_and_setup import (
    _cache_info,
    _get_cloth,
    _tag_update,
)

_POINT_CACHE_FIELDS = {
    "frame_start",
    "frame_end",
    "frame_step",
    "name",
    "index",
    "use_disk_cache",
    "use_external",
    "use_library_path",
    "filepath",
}


def _modifier_driver_paths(obj, modifier):
    with contextlib.suppress(Exception):
        prefix = modifier.path_from_id()
        animation = getattr(obj, "animation_data", None)
        return [curve.data_path for curve in getattr(animation, "drivers", ()) if curve.data_path.startswith(prefix)]
    return []


class ClothLifecycleHandlers:
    """Blender-main-thread handlers for cloth cache and component lifecycle."""

    def manage_cloth_cache(
        self,
        object_name,
        modifier_name,
        action="INSPECT",
        patch=None,
        confirm_bake=False,
        confirm_free=False,
        confirm_external_overwrite=False,
        max_bake_frames=250,
    ):
        obj, modifier = _get_cloth(object_name, modifier_name)
        cache = modifier.point_cache
        if action not in {"INSPECT", "CONFIGURE", "BAKE", "BAKE_FROM_CACHE", "FREE"}:
            raise ValueError(f"Unsupported cache action: {action}")
        if not 1 <= max_bake_frames <= 10_000:
            raise ValueError("max_bake_frames must be in [1, 10000]")
        patch = dict(patch or {})
        unknown = set(patch) - _POINT_CACHE_FIELDS
        if unknown:
            raise ValueError(f"Unsupported PointCache properties: {sorted(unknown)}")
        if action == "INSPECT" and patch:
            raise ValueError("INSPECT does not accept a configuration patch")
        if action == "CONFIGURE" and not patch:
            raise ValueError("CONFIGURE requires a nonempty PointCache patch")
        if action not in {"INSPECT", "CONFIGURE"} and patch:
            raise ValueError(f"{action} does not accept a configuration patch; configure first")

        before = _cache_info(cache)
        prospective = {
            "frame_start": patch.get("frame_start", cache.frame_start),
            "frame_end": patch.get("frame_end", cache.frame_end),
            "frame_step": patch.get("frame_step", cache.frame_step),
            "name": patch.get("name", cache.name),
            "index": patch.get("index", cache.index),
            "use_disk_cache": patch.get("use_disk_cache", cache.use_disk_cache),
            "use_external": patch.get("use_external", cache.use_external),
            "use_library_path": patch.get("use_library_path", cache.use_library_path),
            "filepath": patch.get("filepath", cache.filepath),
        }
        if prospective["frame_start"] > prospective["frame_end"]:
            raise ValueError("PointCache frame_start must be <= frame_end")
        for name, value in patch.items():
            validate_rna_value(cache, name, value)
        if prospective["use_external"] and not prospective["filepath"]:
            raise ValueError("External point caches require an explicit filepath")
        if prospective["use_disk_cache"] and not prospective["use_external"] and not bpy.data.filepath:
            raise ValueError("Internal disk caching requires the .blend file to be saved first")
        external = _external_directory_evidence(prospective["filepath"])
        if prospective["use_external"] and (not external["exists"] or not external["writable"]):
            raise ValueError(f"External cache directory must already exist and be writable: {external['resolved']}")
        identity = _prospective_cache_identity(cache, prospective)
        shared_with = []
        dependency_issues = _cloth_cache_dependency_issues(obj, modifier)
        if identity is not None:
            for other_obj, other_modifier, other_cache in _all_cloth_caches():
                if other_modifier == modifier:
                    continue
                if _shared_cache_identity(other_cache) == identity:
                    shared_with.append({"object": other_obj.name, "modifier": other_modifier.name})
        if shared_with and action != "INSPECT":
            raise ValueError(f"External cache identity is already used by {shared_with}")

        if action == "INSPECT":
            return {
                "changed_objects": [],
                "object": obj.name,
                "modifier": modifier.name,
                "action": action,
                "point_cache": before,
                "external_path": external,
                "shared_external_identity_with": shared_with,
                "dependency_issues": dependency_issues,
            }
        if cache.is_baking:
            raise ValueError("Point cache is currently baking")

        changes = {}
        if action == "CONFIGURE":
            if cache.is_baked:
                raise ValueError("Cannot configure a baked point cache; free the exact bake separately first")
            old_range = (cache.frame_start, cache.frame_end)
            scalar_patch = {name: value for name, value in patch.items() if name not in {"frame_start", "frame_end"}}
            try:
                changes = patch_rna(cache, scalar_patch, _POINT_CACHE_FIELDS)
                if "frame_start" in patch or "frame_end" in patch:
                    set_cache_frame_range(
                        cache,
                        prospective["frame_start"],
                        prospective["frame_end"],
                    )
                    changes["frame_start"] = {
                        "old": old_range[0],
                        "new": cache.frame_start,
                    }
                    changes["frame_end"] = {
                        "old": old_range[1],
                        "new": cache.frame_end,
                    }
                _tag_update(obj)
            except Exception:
                restore_rna(cache, changes)
                with contextlib.suppress(Exception):
                    set_cache_frame_range(cache, *old_range)
                raise
            return {
                "changed_objects": [obj.name],
                "object": obj.name,
                "modifier": modifier.name,
                "action": action,
                "changes": changes,
                "point_cache_before": before,
                "point_cache_after": _cache_info(cache),
                "external_path": _external_directory_evidence(cache.filepath),
                "warnings": ["Point-cache configuration changed; previously evaluated in-memory state is stale."],
            }

        frame_count = (cache.frame_end - cache.frame_start) // cache.frame_step + 1
        require_cache_confirmation(action, confirm_bake=confirm_bake, confirm_free=confirm_free)
        if action in {"BAKE", "BAKE_FROM_CACHE"}:
            if cache.is_baked:
                raise ValueError("Point cache is already baked")
            if dependency_issues:
                raise ValueError(f"Cloth cache dependencies are invalid: {dependency_issues}")
            if frame_count > max_bake_frames:
                raise ValueError(
                    f"Cache range contains {frame_count} steps, exceeding max_bake_frames={max_bake_frames}"
                )
            if action == "BAKE" and cache.use_external and external["entries"] and not confirm_external_overwrite:
                raise ValueError("External cache directory is not empty; confirm_external_overwrite=True is required")
            if action == "BAKE":
                _run_point_cache_operator(obj, cache, bpy.ops.ptcache.bake, bake=True)
            else:
                _run_point_cache_operator(obj, cache, bpy.ops.ptcache.bake_from_cache)
            if not cache.is_baked:
                raise RuntimeError(
                    f"{action} reported FINISHED but the exact point cache is not baked; "
                    f"state={json.dumps(_cache_info(cache))}"
                )
        else:
            if not cache.is_baked:
                raise ValueError("Point cache is not baked")
            if cache.use_external and external["entries"] and not confirm_external_overwrite:
                raise ValueError(
                    "Freeing an external bake may remove cache files; confirm_external_overwrite=True is also required"
                )
            _run_point_cache_operator(obj, cache, bpy.ops.ptcache.free_bake)
            if cache.is_baked:
                raise RuntimeError(
                    "FREE reported FINISHED but the exact point cache remains baked; "
                    f"state={json.dumps(_cache_info(cache))}"
                )
        return {
            "changed_objects": [obj.name],
            "object": obj.name,
            "modifier": modifier.name,
            "action": action,
            "frame_steps": frame_count,
            "operator_scope": "EXACT_CLOTH_POINT_CACHE",
            "point_cache_before": before,
            "point_cache_after": _cache_info(cache),
            "external_path": _external_directory_evidence(cache.filepath),
            "warnings": [
                "Bake operators run synchronously; this tool bounds frames but cannot interrupt Blender "
                "inside one frame solve."
            ],
        }

    def remove_cloth_components(
        self,
        object_name,
        component_type,
        modifier_name=None,
        collection_name=None,
        confirm_baked_removal=False,
        confirm_affected_bakes=False,
    ):
        obj = get_object(object_name)
        allowed = {
            "CLOTH_MODIFIER",
            "COLLISION_MODIFIER",
            "ATTACHMENT_MODIFIER",
            "COLLISION_COLLECTION_MEMBERSHIP",
        }
        if component_type not in allowed:
            raise ValueError(f"Unsupported component_type: {component_type}")
        if component_type == "COLLISION_COLLECTION_MEMBERSHIP":
            if modifier_name is not None:
                raise ValueError("modifier_name is not valid for collection membership removal")
            if not collection_name:
                raise ValueError("collection_name is required for collection membership removal")
            collection = bpy.data.collections.get(collection_name)
            if collection is None:
                raise ValueError(f"Collection not found: {collection_name}")
            if obj.name not in collection.objects:
                raise ValueError(f"Object '{obj.name}' is not directly linked to collection '{collection.name}'")
            ownership = _owned_membership_record(obj, collection.name)
            if ownership is None:
                raise ValueError("Collection membership is not marked as MCP-owned and will not be removed")
            affected = _affected_cloths(obj)
            baked = [
                {"object": cloth.name, "modifier": modifier.name}
                for cloth, modifier in affected
                if modifier.point_cache.is_baked
            ]
            if baked and not confirm_affected_bakes:
                raise ValueError(f"Collection membership affects baked cloth caches {baked}")
            serialized_ownership = obj[ownership["object_property"]]
            try:
                collection.objects.unlink(obj)
                del obj[ownership["object_property"]]
            except Exception:
                with contextlib.suppress(Exception):
                    if obj.name not in collection.objects:
                        collection.objects.link(obj)
                    obj[ownership["object_property"]] = serialized_ownership
                raise
            return {
                "changed_objects": [obj.name],
                "object": obj.name,
                "component_type": component_type,
                "removed": {"collection_membership": collection.name, "ownership": ownership},
                "affected_cloth_caches": [
                    {
                        "object": cloth.name,
                        "modifier": modifier.name,
                        "point_cache": _cache_info(modifier.point_cache),
                    }
                    for cloth, modifier in affected
                ],
                "retained": ["object", "other collection memberships", "modifiers", "vertex groups"],
            }

        if collection_name is not None:
            raise ValueError("collection_name is valid only for collection membership removal")
        if not modifier_name:
            raise ValueError("modifier_name is required for modifier removal")
        expected_type = {
            "CLOTH_MODIFIER": "CLOTH",
            "COLLISION_MODIFIER": "COLLISION",
        }.get(component_type)
        modifier = obj.modifiers.get(modifier_name)
        if modifier is None:
            raise ValueError(f"Modifier not found: {modifier_name}")
        if expected_type and modifier.type != expected_type:
            raise ValueError(f"Modifier '{modifier.name}' is {modifier.type}, not {expected_type}")
        ownership_role = {
            "CLOTH_MODIFIER": "cloth",
            "COLLISION_MODIFIER": "collider",
            "ATTACHMENT_MODIFIER": "attachment",
        }[component_type]
        ownership = next(
            (
                record
                for record in _owned_component_records(obj)
                if record.get("role") == ownership_role and record.get("modifier") == modifier.name
            ),
            None,
        )
        if component_type == "ATTACHMENT_MODIFIER":
            if modifier.type not in {"HOOK", "ARMATURE", "MESH_DEFORM", "SURFACE_DEFORM"}:
                raise ValueError(f"Modifier '{modifier.name}' is not a supported attachment type")
            if ownership is None:
                raise ValueError("Attachment modifier is not marked as MCP-owned and will not be removed")

        if component_type == "CLOTH_MODIFIER":
            affected = [(obj, modifier)]
            if modifier.point_cache.is_baked and not confirm_baked_removal:
                raise ValueError("Removing a baked Cloth modifier requires confirm_baked_removal=True")
        elif component_type == "COLLISION_MODIFIER":
            affected = _affected_cloths(obj)
        else:
            affected = [(obj, item) for item in obj.modifiers if item.type == "CLOTH"]
        baked_affected = [
            {"object": cloth.name, "modifier": cloth_modifier.name}
            for cloth, cloth_modifier in affected
            if cloth_modifier.point_cache.is_baked
            and not (component_type == "CLOTH_MODIFIER" and cloth_modifier == modifier)
        ]
        if baked_affected and not confirm_affected_bakes:
            raise ValueError(f"Removal affects baked cloth caches {baked_affected}")

        group_names = []
        cache_evidence = None
        if modifier.type == "CLOTH":
            group_names = [
                value
                for value in [
                    *[
                        getattr(modifier.settings, name, "")
                        for name in (
                            "vertex_group_mass",
                            "vertex_group_structural_stiffness",
                            "vertex_group_shear_stiffness",
                            "vertex_group_bending",
                            "vertex_group_shrink",
                            "vertex_group_pressure",
                            "vertex_group_intern",
                        )
                    ],
                    modifier.collision_settings.vertex_group_object_collisions,
                    modifier.collision_settings.vertex_group_self_collisions,
                ]
                if value
            ]
            cache_evidence = _cache_info(modifier.point_cache)
        elif hasattr(modifier, "vertex_group") and modifier.vertex_group:
            group_names = [modifier.vertex_group]
        drivers = _modifier_driver_paths(obj, modifier)
        downstream = [item.name for item in list(obj.modifiers)[list(obj.modifiers).index(modifier) + 1 :]]
        affected_cache_evidence = [
            {
                "object": cloth.name,
                "modifier": cloth_modifier.name,
                "point_cache": _cache_info(cloth_modifier.point_cache),
            }
            for cloth, cloth_modifier in affected
            if not (cloth == obj and cloth_modifier == modifier)
        ]
        ownership_value = obj.get(ownership["object_property"]) if ownership else None
        if ownership:
            del obj[ownership["object_property"]]
        try:
            obj.modifiers.remove(modifier)
        except Exception:
            if ownership and ownership_value is not None:
                obj[ownership["object_property"]] = ownership_value
            raise
        return {
            "changed_objects": [obj.name],
            "object": obj.name,
            "component_type": component_type,
            "removed": {
                "modifier": modifier_name,
                "modifier_type": expected_type or "ATTACHMENT",
                "ownership": ownership,
            },
            "preflight_dependencies": {
                "referenced_vertex_groups_retained": sorted(set(group_names)),
                "drivers_now_unresolved": drivers,
                "downstream_modifiers_retained": downstream,
                "point_cache_removed_with_modifier": cache_evidence,
                "external_cache_files_deleted": False,
            },
            "affected_cloth_caches": affected_cache_evidence,
            "retained": [
                "source object and mesh",
                "materials",
                "vertex groups",
                "control objects",
                "other modifiers",
                "external cache directories and files",
            ],
        }
