"""Blender-main-thread handlers for cloth material and solver configuration."""

from __future__ import annotations

import bpy

from .inspection_and_setup import (
    _MATERIAL_FIELDS,
    _MATERIAL_PRESETS,
    _SOLVER_FIELDS,
    _cache_info,
    _edge_lengths,
    _get_cloth,
    _max_keyed_location_delta,
    _mesh_scale_context,
    _patch_rna,
    _reject_baked,
    _restore_rna,
    _tag_update,
)


class ClothMaterialAndSolverHandlers:
    """Blender-main-thread handlers for cloth material and solver configuration."""

    def _scale_warnings(self, obj):
        absolute = [abs(value) for value in obj.scale]
        warnings = []
        if max(absolute) / min(absolute) > 1.01:
            warnings.append(
                "Nonuniform object scale makes cloth thickness, mass, and collision distances scale-sensitive."
            )
        if obj.matrix_world.to_3x3().determinant() < 0:
            warnings.append("Negative world-transform determinant can invert cloth/collider orientation assumptions.")
        return warnings

    def _configure_material(self, obj, modifier, patch, preset):
        if _mesh_scale_context(obj)["scene_unit_scale_length"] <= 0:
            raise ValueError("Scene unit scale must be positive before configuring cloth material behavior")
        values = {}
        if preset:
            if preset not in _MATERIAL_PRESETS:
                raise ValueError(f"Unknown material preset: {preset}")
            values.update(_MATERIAL_PRESETS[preset])
        values.update(patch or {})
        if not values:
            return {}
        changes = _patch_rna(modifier.settings, values, _MATERIAL_FIELDS)
        return changes

    def configure_cloth_material(self, object_name, modifier_name, patch=None, preset=None):
        obj, modifier = _get_cloth(object_name, modifier_name)
        _reject_baked([(obj, modifier)])
        if not patch and not preset:
            raise ValueError("Provide a material patch or preset")
        scale_context = _mesh_scale_context(obj)
        if scale_context["scene_unit_scale_length"] <= 0:
            raise ValueError("Scene unit scale must be positive before configuring cloth material behavior")
        changes = self._configure_material(obj, modifier, patch, preset)
        try:
            _tag_update(obj)
        except Exception:
            _restore_rna(modifier.settings, changes)
            raise
        warnings = self._scale_warnings(obj)
        if scale_context["base_surface_area_object_local_squared"] <= 0:
            warnings.append("The base mesh has no face area; material density cannot be assessed.")
        max_group_map = {
            "tension_stiffness_max": "vertex_group_structural_stiffness",
            "compression_stiffness_max": "vertex_group_structural_stiffness",
            "shear_stiffness_max": "vertex_group_shear_stiffness",
            "bending_stiffness_max": "vertex_group_bending",
        }
        for field, group_field in max_group_map.items():
            if field in changes and not getattr(modifier.settings, group_field):
                warnings.append(f"{field} has no effect until {group_field} references a populated vertex group.")
        return {
            "changed_objects": [obj.name],
            "object": obj.name,
            "modifier": modifier.name,
            "preset": preset,
            "preset_version": "blender-5.1" if preset else None,
            "changes": changes,
            "point_cache": _cache_info(modifier.point_cache),
            "scale_and_density_context": scale_context,
            "warnings": warnings,
        }

    def configure_cloth_solver(self, object_name, modifier_name, patch):
        obj, modifier = _get_cloth(object_name, modifier_name)
        _reject_baked([(obj, modifier)])
        if not patch:
            raise ValueError("Solver patch cannot be empty")
        if "time_scale" in patch and patch["time_scale"] <= 0:
            raise ValueError("time_scale must be positive")
        if "voxel_cell_size" in patch and patch["voxel_cell_size"] <= 0:
            raise ValueError("voxel_cell_size must be positive")
        old_quality = modifier.settings.quality
        changes = _patch_rna(modifier.settings, patch, _SOLVER_FIELDS)
        try:
            _tag_update(obj)
        except Exception:
            _restore_rna(modifier.settings, changes)
            raise
        edge = _edge_lengths(obj)
        frame_count = modifier.point_cache.frame_end - modifier.point_cache.frame_start + 1
        ratio = modifier.settings.quality / max(old_quality, 1)
        keyed_motion = _max_keyed_location_delta(obj)
        effective_motion = keyed_motion * modifier.settings.time_scale if keyed_motion is not None else None
        warnings = self._scale_warnings(obj)
        if edge["min"] and effective_motion and effective_motion > edge["min"] * max(modifier.settings.quality, 1):
            warnings.append(
                "Keyed object motion is large relative to the smallest base edge and solver quality; "
                "test representative contact frames for tunneling."
            )
        return {
            "changed_objects": [obj.name],
            "object": obj.name,
            "modifier": modifier.name,
            "changes": changes,
            "scene_fps": bpy.context.scene.render.fps / bpy.context.scene.render.fps_base,
            "cache_frame_count": frame_count,
            "smallest_base_edge_local": edge["min"],
            "maximum_keyed_location_channel_units_per_frame": keyed_motion,
            "time_scaled_keyed_motion_per_frame": effective_motion,
            "estimated_quality_cost_multiplier": ratio,
            "point_cache": _cache_info(modifier.point_cache),
            "warnings": warnings,
        }
