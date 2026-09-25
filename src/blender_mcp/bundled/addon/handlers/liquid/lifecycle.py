# pyright: reportGeneralTypeIssues=false
"""Precise removal of liquid fluid modifiers and MCP-owned helpers."""

import bpy

from ..rna_patch import get_object
from .simulation import _active_cache_flags, _cache_directory_evidence, _cache_state


class LiquidLifecycleHandlers:
    """Remove explicitly selected liquid fluid components with production safeguards."""

    def remove_fluid_components(self, targets, accept_orphaned_cache=False):
        if not targets or len(targets) > 64:
            raise ValueError("targets must contain 1-64 records")
        identities = [(item["object_name"], item["modifier_name"]) for item in targets]
        if len(set(identities)) != len(identities):
            raise ValueError("targets contain duplicate object/modifier pairs")
        resolved = []
        helper_names = set()
        for record in targets:
            obj = get_object(record["object_name"])
            modifier = obj.modifiers.get(record["modifier_name"])
            if modifier is None or modifier.type != "FLUID" or modifier.fluid_type == "NONE":
                raise ValueError(f"Active fluid modifier not found: {obj.name}:{record['modifier_name']}")
            cache = None
            if modifier.fluid_type == "DOMAIN":
                settings = modifier.domain_settings
                if settings is None:
                    raise ValueError(f"Domain settings are unavailable: {obj.name}:{modifier.name}")
                active = _active_cache_flags(settings)
                if active and not accept_orphaned_cache:
                    raise ValueError(
                        f"Domain '{obj.name}:{modifier.name}' has baked/baking cache state {active}; free it first "
                        "or explicitly accept orphaning"
                    )
                cache = {
                    "state": _cache_state(settings),
                    "directory": _cache_directory_evidence(settings.cache_directory),
                }
                if cache["directory"]["files_scanned"] and not accept_orphaned_cache:
                    raise ValueError(
                        f"Domain '{obj.name}:{modifier.name}' has files in its cache directory; free the cache "
                        "first or explicitly accept orphaning"
                    )
            if record.get("remove_owned_helper_object"):
                if obj.get("blendermcp_liquid_helper") is None:
                    raise ValueError(f"Object '{obj.name}' is not tagged as an MCP-owned liquid helper")
                if len(obj.modifiers) != 1:
                    raise ValueError("Owned helper removal requires the fluid modifier to be its only modifier")
                helper_names.add(obj.name)
            resolved.append((obj, modifier, record, cache))
        removed = []
        for obj, modifier, record, cache in resolved:
            info = {
                "object": obj.name,
                "modifier": modifier.name,
                "fluid_type": modifier.fluid_type,
                "cache_removed_with_modifier": cache,
                "external_cache_files_deleted": False,
                "helper_object_removed": bool(record.get("remove_owned_helper_object")),
            }
            obj.modifiers.remove(modifier)
            removed.append(info)
        for name in helper_names:
            helper = bpy.data.objects.get(name)
            if helper is not None:
                bpy.data.objects.remove(helper, do_unlink=True)  # pyright: ignore[reportArgumentType]
        changed = [record["object"] for record in removed]
        return {
            "changed_objects": changed,
            "removed": removed,
            "retained": [
                "non-helper source/render objects",
                "mesh datablocks",
                "materials",
                "other modifiers",
                "collections",
                "external cache directories and files",
            ],
            "recovery": "Use Blender Undo for modifier/helper recovery; external cache files were not touched.",
            "warnings": (
                ["One or more domain modifiers were removed while cache paths/files may remain orphaned."]
                if accept_orphaned_cache and any(item["cache_removed_with_modifier"] for item in removed)
                else []
            ),
        }
