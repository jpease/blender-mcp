"""Blender-main-thread handlers for cloth attachments."""

from __future__ import annotations

import contextlib
import statistics

from ...helpers import sync_from_editmode
from ..rna_patch import get_object, validate_rna_value
from ._deform_binding import (
    _attachment_target_matrix,
    _bind_deform_modifier,
    _move_modifier_immediately_before,
    _restore_attachment_modifier,
    _snapshot_attachment_modifier,
)
from ._geometry_sampling import _evaluated_world_vertices
from ._ownership import _tag_owned_component
from .inspection_and_setup import (
    _cache_info,
    _get_cloth,
    _modifier_info,
    _reject_baked,
    _scene_context_for_object,
    _tag_update,
    _vertex_group_stats,
)


class ClothAttachmentHandlers:
    """Blender-main-thread handlers for cloth attachments."""

    def create_cloth_attachment(
        self,
        cloth_object_name,
        cloth_modifier_name,
        pin_group_name,
        target_object_name,
        attachment_type="HOOK",
        attachment_modifier_name="Cloth Attachment",
        bone_name=None,
        rest_frame=1,
        existing_policy="ERROR",
        bind=True,
    ):
        cloth, cloth_modifier = _get_cloth(cloth_object_name, cloth_modifier_name)
        sync_from_editmode(cloth)
        _reject_baked([(cloth, cloth_modifier)])
        if existing_policy not in {"ERROR", "REUSE"}:
            raise ValueError("existing_policy must be ERROR or REUSE")
        if attachment_type not in {"HOOK", "ARMATURE", "MESH_DEFORM", "SURFACE_DEFORM"}:
            raise ValueError(f"Unsupported attachment_type: {attachment_type}")
        pin_group = cloth.vertex_groups.get(pin_group_name)
        if pin_group is None:
            raise ValueError(f"Pin vertex group not found: {pin_group_name}")
        pin_stats = _vertex_group_stats(cloth, pin_group)
        if not pin_stats["nonzero"]:
            raise ValueError(f"Pin vertex group '{pin_group_name}' has no nonzero weights")
        if cloth_modifier.settings.vertex_group_mass != pin_group_name:
            raise ValueError(
                f"Cloth pin group is '{cloth_modifier.settings.vertex_group_mass}', not '{pin_group_name}'; "
                "configure pinning explicitly before creating the attachment"
            )
        target = get_object(target_object_name)
        if target == cloth:
            raise ValueError("Attachment target must differ from the cloth object")
        if abs(float(cloth.matrix_world.determinant())) <= 1e-12:
            raise ValueError(f"Cloth object '{cloth.name}' has a singular world transform")
        if attachment_type == "ARMATURE" and target.type != "ARMATURE":
            raise ValueError("ARMATURE attachments require an armature target")
        if attachment_type in {"MESH_DEFORM", "SURFACE_DEFORM"} and target.type != "MESH":
            raise ValueError(f"{attachment_type} attachments require a mesh target")
        if bone_name and attachment_type != "HOOK":
            raise ValueError("bone_name is supported only by HOOK attachments")
        if bone_name and target.type != "ARMATURE":
            raise ValueError("A bone-targeted Hook requires an armature target")
        if bone_name and target.data.bones.get(bone_name) is None:
            raise ValueError(f"Bone not found: {bone_name}")
        scene, view_layer = _scene_context_for_object(cloth)
        if target.name not in scene.objects:
            raise ValueError(f"Attachment target '{target.name}' is not linked to cloth scene '{scene.name}'")
        if attachment_type in {"MESH_DEFORM", "SURFACE_DEFORM"}:
            evaluated_target = target.evaluated_get(view_layer.depsgraph)
            target_mesh = evaluated_target.to_mesh()
            try:
                if not target_mesh.vertices or not target_mesh.polygons:
                    raise ValueError(f"Attachment target '{target.name}' must evaluate to a nonempty surface")
            finally:
                evaluated_target.to_mesh_clear()
        validate_rna_value(scene, "frame_current", rest_frame)

        existing = cloth.modifiers.get(attachment_modifier_name)
        created = False
        if existing is not None:
            if existing.type != attachment_type:
                raise ValueError(f"Modifier '{attachment_modifier_name}' is {existing.type}, not {attachment_type}")
            if existing_policy == "ERROR":
                raise ValueError(f"Attachment modifier already exists: {attachment_modifier_name}")
            modifier = existing
        else:
            modifier = cloth.modifiers.new(name=attachment_modifier_name, type=attachment_type)
            created = True
        original_index = list(cloth.modifiers).index(modifier)
        snapshot = None if created else _snapshot_attachment_modifier(modifier)
        was_bound = bool(getattr(modifier, "is_bound", False))
        if was_bound and attachment_type == "MESH_DEFORM" and modifier.object != target:
            raise ValueError("Cannot retarget an already-bound Mesh Deform modifier")
        if was_bound and attachment_type == "SURFACE_DEFORM" and modifier.target != target:
            raise ValueError("Cannot retarget an already-bound Surface Deform modifier")

        original_frame = scene.frame_current
        original_subframe = scene.frame_subframe
        ownership = None
        before = None
        try:
            scene.frame_set(rest_frame)
            view_layer.update()
            before = _evaluated_world_vertices(cloth, 10_000, view_layer.depsgraph)
            if attachment_type == "HOOK":
                modifier.object = target
                modifier.subtarget = bone_name or ""
                modifier.vertex_group = pin_group_name
                target_matrix = _attachment_target_matrix(target, bone_name)
                modifier.matrix_inverse = target_matrix.inverted() @ cloth.matrix_world
                modifier.center = cloth.matrix_world.inverted() @ target_matrix.translation
            elif attachment_type == "ARMATURE":
                modifier.object = target
                modifier.vertex_group = pin_group_name
                modifier.use_vertex_groups = True
            elif attachment_type == "MESH_DEFORM":
                modifier.object = target
                modifier.vertex_group = pin_group_name
            else:
                modifier.target = target
                modifier.vertex_group = pin_group_name
            _move_modifier_immediately_before(cloth, modifier, cloth_modifier)
            _tag_update(cloth)
            if attachment_type in {"MESH_DEFORM", "SURFACE_DEFORM"} and bind and not modifier.is_bound:
                _bind_deform_modifier(cloth, modifier)
            if created:
                ownership = _tag_owned_component(cloth, modifier, "attachment")
            _tag_update(cloth)
            after = _evaluated_world_vertices(cloth, 10_000, view_layer.depsgraph)
        except Exception:
            if ownership is not None:
                with contextlib.suppress(Exception):
                    del cloth[ownership["object_property"]]
            if not created and getattr(modifier, "is_bound", False) and not was_bound:
                with contextlib.suppress(Exception):
                    _bind_deform_modifier(cloth, modifier)
            if created:
                with contextlib.suppress(Exception):
                    cloth.modifiers.remove(modifier)
            else:
                _restore_attachment_modifier(modifier, snapshot)
                with contextlib.suppress(Exception):
                    cloth.modifiers.move(list(cloth.modifiers).index(modifier), original_index)
            raise
        finally:
            scene.frame_set(original_frame, subframe=original_subframe)
            view_layer.update()

        rest_displacements = None
        if before["total"] == after["total"] and before["indices"] == after["indices"]:
            displacement_records = []
            for vertex_index, old, new in zip(before["indices"], before["positions"], after["positions"], strict=True):
                try:
                    weight = pin_group.weight(vertex_index)
                except RuntimeError:
                    weight = 0.0
                displacement_records.append((weight, float((new - old).length)))
            displacements = [distance for _weight, distance in displacement_records]
            pinned = [distance for weight, distance in displacement_records if weight > 0]
            unpinned = [distance for weight, distance in displacement_records if weight <= 0]
            rest_displacements = {
                "sampled_vertices": len(displacements),
                "maximum_world": max(displacements, default=0.0),
                "mean_world": statistics.fmean(displacements) if displacements else 0.0,
                "pinned_maximum_world": max(pinned, default=0.0),
                "unpinned_maximum_world": max(unpinned, default=0.0),
                "topology_matched": True,
            }
        return {
            "changed_objects": [cloth.name],
            "cloth_object": cloth.name,
            "cloth_modifier": cloth_modifier.name,
            "attachment": _modifier_info(cloth, modifier),
            "attachment_type": attachment_type,
            "target_object": target.name,
            "target_bone": bone_name,
            "pin_group": pin_stats,
            "created": created,
            "bound": getattr(modifier, "is_bound", None),
            "rest_frame": rest_frame,
            "rest_frame_displacement_check": rest_displacements,
            "ownership": ownership,
            "point_cache": _cache_info(cloth_modifier.point_cache),
            "warnings": [
                "Attachment input changed and invalidates unbaked simulation state.",
                "The rest-frame displacement check is sampled and does not prove behavior at animated frames.",
            ],
        }
