"""Blender-main-thread handlers for cloth vertex-weight pinning."""

from __future__ import annotations

import contextlib

from ...helpers import sync_from_editmode
from ..rna_patch import finite, patch_rna
from .inspection_and_setup import (
    _DEFORMING_MODIFIERS,
    _PINNING_FIELDS,
    _WEIGHT_ROLES,
    _cache_info,
    _get_cloth,
    _reject_baked,
    _tag_update,
    _vertex_group_stats,
)

_MAX_WEIGHT_ASSIGNMENTS = 10_000


class ClothPinningHandlers:
    """Blender-main-thread handlers for cloth vertex-weight pinning."""

    def set_cloth_vertex_weights(self, object_name, modifier_name, role, group_name, assignments, operation="REPLACE"):
        obj, modifier = _get_cloth(object_name, modifier_name)
        sync_from_editmode(obj)
        _reject_baked([(obj, modifier)])
        if role not in _WEIGHT_ROLES:
            raise ValueError(f"Unknown cloth weight role: {role}")
        if operation not in {"REPLACE", "ADD", "SUBTRACT"}:
            raise ValueError("operation must be REPLACE, ADD, or SUBTRACT")
        if not assignments or len(assignments) > _MAX_WEIGHT_ASSIGNMENTS:
            raise ValueError(f"assignments must contain 1-{_MAX_WEIGHT_ASSIGNMENTS} entries")
        indices = [item["vertex_index"] for item in assignments]
        if len(set(indices)) != len(indices):
            raise ValueError("Each vertex_index may appear only once per request")
        total = len(obj.data.vertices)
        for item in assignments:
            index, weight = item["vertex_index"], item["weight"]
            if not 0 <= index < total:
                raise ValueError(f"Vertex index {index} out of range [0, {total - 1}]")
            finite(weight, "weight")
            if not 0.0 <= weight <= 1.0:
                raise ValueError(f"Weight for vertex {index} must be in [0, 1]")
        group = obj.vertex_groups.get(group_name)
        created = group is None
        if group is None:
            group = obj.vertex_groups.new(name=group_name)
        if group.lock_weight:
            if created:
                obj.vertex_groups.remove(group)
            raise ValueError(f"Vertex group '{group_name}' is locked")
        owner_name, property_name = _WEIGHT_ROLES[role]
        settings_owner = getattr(modifier, owner_name)
        old_reference = getattr(settings_owner, property_name)
        old_weights = {}
        for index in indices:
            try:
                old_weights[index] = float(group.weight(index))
            except RuntimeError:
                old_weights[index] = None
        try:
            for item in assignments:
                index, requested = item["vertex_index"], float(item["weight"])
                previous = old_weights[index] or 0.0
                if operation == "ADD":
                    requested = min(1.0, previous + requested)
                elif operation == "SUBTRACT":
                    requested = max(0.0, previous - requested)
                group.add([index], requested, "REPLACE")
            setattr(settings_owner, property_name, group.name)
            _tag_update(obj)
        except Exception:
            for index, previous in old_weights.items():
                with contextlib.suppress(Exception):
                    if previous is None:
                        group.remove([index])
                    else:
                        group.add([index], previous, "REPLACE")
            setattr(settings_owner, property_name, old_reference)
            if created:
                with contextlib.suppress(Exception):
                    obj.vertex_groups.remove(group)
            raise
        return {
            "changed_objects": [obj.name],
            "object": obj.name,
            "modifier": modifier.name,
            "role": role,
            "mapped_property": f"{owner_name}.{property_name}",
            "group": group.name,
            "group_created": created,
            "operation": operation,
            "changed_vertices": indices,
            "statistics": _vertex_group_stats(obj, group),
            "point_cache": _cache_info(modifier.point_cache),
            "warnings": [
                "Cloth weights changed and invalidate unbaked simulation state.",
                "If any topology-changing tool runs, query get_mesh_data again before reusing these indices.",
            ],
        }

    def configure_cloth_pinning(self, object_name, modifier_name, group_name, patch):
        obj, modifier = _get_cloth(object_name, modifier_name)
        _reject_baked([(obj, modifier)])
        group = obj.vertex_groups.get(group_name)
        if group is None:
            raise ValueError(f"Vertex group not found: {group_name}")
        if not patch:
            raise ValueError("Pinning patch cannot be empty")
        old_group = modifier.settings.vertex_group_mass
        changes = patch_rna(modifier.settings, patch, _PINNING_FIELDS)
        try:
            modifier.settings.vertex_group_mass = group.name
            _tag_update(obj)
        except Exception:
            modifier.settings.vertex_group_mass = old_group
            for name, values in changes.items():
                setattr(modifier.settings, name, values["old"])
            raise
        stats = _vertex_group_stats(obj, group)
        warnings = []
        if stats["nonzero"] == 0:
            warnings.append("The pin group has no nonzero weights; no vertices will be pinned.")
        elif stats["maximum"] < 0.5:
            warnings.append("All pin weights are below 0.5; the attachment boundary may be weak or oscillate.")
        if stats["nonzero"] == len(obj.data.vertices):
            warnings.append("Every vertex has a nonzero pin weight; little or no cloth motion may remain.")
        cloth_index = list(obj.modifiers).index(modifier)
        upstream = [item.name for item in list(obj.modifiers)[:cloth_index] if item.type in _DEFORMING_MODIFIERS]
        downstream = [item.name for item in list(obj.modifiers)[cloth_index + 1 :] if item.type in _DEFORMING_MODIFIERS]
        if downstream:
            warnings.append(
                f"Animation/deformation modifiers after Cloth cannot drive its pinned rest position: {downstream}"
            )
        return {
            "changed_objects": [obj.name],
            "object": obj.name,
            "modifier": modifier.name,
            "pin_group": {"old": old_group, "new": group.name},
            "changes": changes,
            "group_statistics": stats,
            "upstream_deformers": upstream,
            "downstream_deformers": downstream,
            "point_cache": _cache_info(modifier.point_cache),
            "warnings": warnings,
        }
