"""Blender-main-thread handlers for cloth render-surface modifier stacks."""

from __future__ import annotations

import contextlib

from ...helpers import sync_from_editmode
from ..rna_patch import patch_rna
from ._deform_binding import _bind_corrective_smooth, _move_modifier_immediately_after, _unbind_corrective_smooth
from ._geometry_sampling import _evaluated_geometry_evidence
from ._ownership import _tag_owned_component
from .inspection_and_setup import (
    _edge_lengths,
    _get_cloth,
    _modifier_info,
    _scene_context_for_object,
    _tag_update,
)

_CORRECTIVE_SMOOTH_FIELDS = {
    "factor",
    "iterations",
    "scale",
    "rest_source",
    "smooth_type",
    "use_only_smooth",
    "use_pin_boundary",
    "vertex_group",
}
_SUBDIVISION_FIELDS = {
    "levels",
    "render_levels",
    "quality",
    "subdivision_type",
    "uv_smooth",
    "use_creases",
}
_SOLIDIFY_FIELDS = {
    "thickness",
    "offset",
    "material_offset",
    "material_offset_rim",
    "use_even_offset",
    "use_quality_normals",
    "use_rim",
}
_WEIGHTED_NORMAL_FIELDS = {"weight", "mode", "thresh", "keep_sharp", "use_face_influence"}


class ClothRenderSurfaceHandlers:
    """Blender-main-thread handlers for cloth render-surface modifier stacks."""

    def prepare_cloth_render_surface(
        self,
        object_name,
        cloth_modifier_name,
        corrective_smooth=None,
        subdivision=None,
        solidify=None,
        weighted_normal=None,
        corrective_smooth_name="Cloth Corrective Smooth",
        subdivision_name="Cloth Render Subdivision",
        solidify_name="Cloth Render Thickness",
        weighted_normal_name="Cloth Weighted Normal",
        existing_policy="ERROR",
        rest_frame=1,
    ):
        obj, cloth_modifier = _get_cloth(object_name, cloth_modifier_name)
        sync_from_editmode(obj)
        scene, view_layer = _scene_context_for_object(obj)
        original_frame = scene.frame_current
        original_subframe = scene.frame_subframe
        if existing_policy not in {"ERROR", "REUSE"}:
            raise ValueError("existing_policy must be ERROR or REUSE")
        requests = [
            ("CORRECTIVE_SMOOTH", corrective_smooth_name, corrective_smooth, _CORRECTIVE_SMOOTH_FIELDS),
            ("SUBSURF", subdivision_name, subdivision, _SUBDIVISION_FIELDS),
            ("SOLIDIFY", solidify_name, solidify, _SOLIDIFY_FIELDS),
            ("WEIGHTED_NORMAL", weighted_normal_name, weighted_normal, _WEIGHTED_NORMAL_FIELDS),
        ]
        requested = [item for item in requests if item[2] is not None]
        if not requested:
            raise ValueError("At least one render-finishing patch is required")
        if len({name for _kind, name, _patch, _fields in requested}) != len(requested):
            raise ValueError("Requested render modifier names must be unique")
        for _kind, _name, patch, _fields in requested:
            group_name = patch.get("vertex_group") if patch else None
            if group_name and obj.vertex_groups.get(group_name) is None:
                raise ValueError(f"Vertex group not found: {group_name}")
        if corrective_smooth and corrective_smooth.get("iterations", 0) > 200:
            raise ValueError("Corrective Smooth iterations are limited to 200")
        if subdivision:
            for field in ("levels", "render_levels"):
                if subdivision.get(field, 0) > 6:
                    raise ValueError(f"{field} is limited to 6 for bounded evaluated geometry")
        if solidify:
            if solidify.get("thickness") == 0:
                raise ValueError("Solidify thickness must be nonzero")
            material_count = len(obj.material_slots)
            for field in ("material_offset", "material_offset_rim"):
                offset = int(solidify.get(field, 0) or 0)
                if offset and not material_count:
                    raise ValueError(f"{field} requires at least one material slot")
                if offset and any(
                    not 0 <= polygon.material_index + offset < material_count for polygon in obj.data.polygons
                ):
                    raise ValueError(f"{field} would resolve outside the object's material slots")
        before = _evaluated_geometry_evidence(obj)
        created = []
        reused = []
        ownership = []
        snapshots = []
        bound_during_request = []
        try:
            preceding = cloth_modifier
            records = []
            for modifier_type, modifier_name, patch, allowed in requested:
                modifier = obj.modifiers.get(modifier_name)
                if modifier is not None:
                    if modifier.type != modifier_type:
                        raise ValueError(f"Modifier '{modifier_name}' is {modifier.type}, not {modifier_type}")
                    if existing_policy == "ERROR":
                        raise ValueError(f"Modifier '{modifier_name}' already exists")
                    if cloth_modifier.point_cache.is_baked and list(obj.modifiers).index(modifier) < list(
                        obj.modifiers
                    ).index(cloth_modifier):
                        raise ValueError("Cannot move an upstream finishing modifier across a baked Cloth modifier")
                    snapshots.append(
                        (
                            modifier,
                            list(obj.modifiers).index(modifier),
                            {name: getattr(modifier, name) for name in allowed},
                        )
                    )
                    reused.append(modifier.name)
                else:
                    modifier = obj.modifiers.new(name=modifier_name, type=modifier_type)
                    created.append(modifier)
                    ownership.append(_tag_owned_component(obj, modifier, "render_finish"))
                changes = patch_rna(modifier, patch, allowed)
                _move_modifier_immediately_after(obj, modifier, preceding)
                if modifier_type == "CORRECTIVE_SMOOTH" and modifier.rest_source == "BIND":
                    scene.frame_set(rest_frame)
                    view_layer.update()
                    was_bound = modifier.is_bind
                    _bind_corrective_smooth(obj, modifier)
                    if not was_bound:
                        bound_during_request.append(modifier)
                preceding = modifier
                records.append({"modifier": _modifier_info(obj, modifier), "changes": changes})
            _tag_update(obj)
            after = _evaluated_geometry_evidence(obj)
        except Exception:
            for modifier in reversed(bound_during_request):
                with contextlib.suppress(Exception):
                    _unbind_corrective_smooth(obj, modifier)
            for record in reversed(ownership):
                with contextlib.suppress(Exception):
                    del obj[record["object_property"]]
            for modifier in reversed(created):
                with contextlib.suppress(Exception):
                    obj.modifiers.remove(modifier)
            for modifier, index, values in reversed(snapshots):
                for name, value in values.items():
                    with contextlib.suppress(Exception):
                        setattr(modifier, name, value)
                with contextlib.suppress(Exception):
                    obj.modifiers.move(list(obj.modifiers).index(modifier), index)
            raise
        finally:
            scene.frame_set(original_frame, subframe=original_subframe)
            view_layer.update()
        warnings = []
        if solidify is not None:
            thickness = abs(float(solidify.get("thickness", getattr(obj.modifiers[solidify_name], "thickness", 0))))
            edges = _edge_lengths(obj)
            if edges["median"] and thickness > edges["median"]:
                warnings.append("Solidify thickness exceeds the median simulation edge length and may self-intersect.")
            collision_distance = float(cloth_modifier.collision_settings.distance_min)
            if thickness > collision_distance * 2:
                warnings.append("Solidify thickness is more than twice the cloth object-collision distance.")
        if subdivision is not None and int(subdivision.get("render_levels", 0) or 0) > 3:
            warnings.append("Render subdivision above level 3 can multiply evaluated cloth surface cost sharply.")
        return {
            "changed_objects": [obj.name],
            "object": obj.name,
            "cloth_modifier": cloth_modifier.name,
            "modifiers": records,
            "created_modifiers": [modifier.name for modifier in created],
            "reused_modifiers": reused,
            "modifier_stack": [_modifier_info(obj, modifier) for modifier in obj.modifiers],
            "geometry": {"before": before, "after": after},
            "base_mesh_preserved": True,
            "uv_layers_preserved": [layer.name for layer in obj.data.uv_layers],
            "material_slots_preserved": len(obj.material_slots),
            "motion_blur_configuration_changed": False,
            "rest_frame": rest_frame,
            "ownership": ownership,
            "warnings": warnings,
        }
