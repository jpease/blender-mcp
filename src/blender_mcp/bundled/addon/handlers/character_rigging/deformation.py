"""Blender handlers for weight transfer and B-Bone deformation settings."""

import contextlib

import bpy

from ...helpers import apply_modifier, sync_from_editmode
from .primitives import _armature_object, _finite, _mesh_object, _plain
from .skinning import _restore_groups, _snapshot_groups

_TRANSFER_MAPPINGS = {
    "TOPOLOGY",
    "NEAREST",
    "EDGE_NEAREST",
    "EDGEINTERP_NEAREST",
    "POLY_NEAREST",
    "POLYINTERP_NEAREST",
    "POLYINTERP_VNORPROJ",
}
_TRANSFER_MIX_MODES = {"REPLACE", "ABOVE_THRESHOLD", "BELOW_THRESHOLD", "MIX", "ADD", "SUB", "MUL"}
_BENDY_FIELDS = {
    "segments": "bbone_segments",
    "display_x": "bbone_x",
    "display_z": "bbone_z",
    "mapping_mode": "bbone_mapping_mode",
    "handle_type_start": "bbone_handle_type_start",
    "handle_type_end": "bbone_handle_type_end",
    "ease_in": "bbone_easein",
    "ease_out": "bbone_easeout",
    "curve_in_x": "bbone_curveinx",
    "curve_in_z": "bbone_curveinz",
    "curve_out_x": "bbone_curveoutx",
    "curve_out_z": "bbone_curveoutz",
    "roll_in": "bbone_rollin",
    "roll_out": "bbone_rollout",
    "scale_in": "bbone_scalein",
    "scale_out": "bbone_scaleout",
    "use_scale_easing": "use_scale_easing",
    "use_endroll_as_inroll": "use_endroll_as_inroll",
    "handle_use_ease_start": "bbone_handle_use_ease_start",
    "handle_use_ease_end": "bbone_handle_use_ease_end",
    "handle_use_scale_start": "bbone_handle_use_scale_start",
    "handle_use_scale_end": "bbone_handle_use_scale_end",
}


def _normalize_vertex_groups(mesh):
    for vertex in mesh.data.vertices:
        assignments = [(mesh.vertex_groups[item.group], float(item.weight)) for item in vertex.groups]
        editable = [(group, weight) for group, weight in assignments if not group.lock_weight]
        locked_sum = sum(weight for group, weight in assignments if group.lock_weight)
        editable_sum = sum(weight for _group, weight in editable)
        if locked_sum > 1.0 + 1e-8:
            raise ValueError(f"Locked weights exceed one on vertex {vertex.index}")
        if editable_sum <= 0:
            continue
        factor = max(0.0, 1.0 - locked_sum) / editable_sum
        for group, weight in editable:
            group.add([vertex.index], weight * factor, "REPLACE")


_DATA_TRANSFER_FIELDS = (
    "object",
    "use_vert_data",
    "data_types_verts",
    "vert_mapping",
    "layers_vgroup_select_src",
    "layers_vgroup_select_dst",
    "mix_mode",
    "mix_factor",
    "use_object_transform",
    "use_max_distance",
    "max_distance",
)


def _validate_transfer_options(
    source, target, mapping, source_groups, mix_mode, mix_factor, max_distance, commit, confirm_commit, normalize
):
    if mapping not in _TRANSFER_MAPPINGS:
        raise ValueError(f"Unsupported vertex mapping: {mapping}")
    if mapping == "TOPOLOGY" and len(source.data.vertices) != len(target.data.vertices):
        raise ValueError("TOPOLOGY mapping requires equal source and target vertex counts")
    if source_groups not in {"ALL", "DEFORM"}:
        raise ValueError("source_groups must be ALL or DEFORM")
    if mix_mode not in _TRANSFER_MIX_MODES:
        raise ValueError(f"Unsupported mix mode: {mix_mode}")
    mix_factor = _finite(mix_factor, "mix_factor")
    if not 0 <= mix_factor <= 1:
        raise ValueError("mix_factor must be in [0, 1]")
    if max_distance is not None and _finite(max_distance, "max_distance") <= 0:
        raise ValueError("max_distance must be positive")
    if commit and not confirm_commit:
        raise ValueError("confirm_commit=True is required to apply transferred weights")
    if normalize and not commit:
        raise ValueError("normalize=True requires commit=True because live weights are not yet committed")
    return mix_factor


def _existing_transfer_modifier(target, modifier_name, destination_policy):
    existing = target.modifiers.get(modifier_name)
    if existing is not None and existing.type != "DATA_TRANSFER":
        raise ValueError(f"Modifier '{modifier_name}' exists and is not DATA_TRANSFER")
    if existing is not None and destination_policy == "ERROR":
        raise ValueError(f"Data Transfer modifier '{modifier_name}' already exists")
    if destination_policy not in {"ERROR", "UPDATE"}:
        raise ValueError("destination_policy must be ERROR or UPDATE")
    return existing


def _transfer_group_names(source, source_groups):
    selected_group_names = {group.name for group in source.vertex_groups}
    if source_groups == "DEFORM":
        armature_modifiers = [modifier for modifier in source.modifiers if modifier.type == "ARMATURE"]
        source_armature = next(
            (modifier.object for modifier in armature_modifiers if modifier.object is not None),
            source.parent if getattr(source.parent, "type", None) == "ARMATURE" else None,
        )
        if source_armature is None:
            raise ValueError("source_groups='DEFORM' requires an Armature modifier or armature parent")
        deform_names = {bone.name for bone in source_armature.data.bones if bone.use_deform}
        selected_group_names &= deform_names
        if not selected_group_names:
            raise ValueError("Source mesh has no vertex groups matching deform bones")
    return selected_group_names


def _configure_weight_transfer(
    target,
    modifier,
    source,
    group_names,
    mapping,
    source_groups,
    mix_mode,
    mix_factor,
    use_object_transform,
    max_distance,
):
    for group_name in sorted(group_names):
        if target.vertex_groups.get(group_name) is None:
            target.vertex_groups.new(name=group_name)
    modifier.object = source
    modifier.use_vert_data = True
    modifier.data_types_verts = {"VGROUP_WEIGHTS"}
    modifier.vert_mapping = mapping
    modifier.layers_vgroup_select_src = "ALL" if source_groups == "ALL" else "BONE_DEFORM"
    modifier.layers_vgroup_select_dst = "NAME"
    modifier.mix_mode = mix_mode
    modifier.mix_factor = mix_factor
    modifier.use_object_transform = bool(use_object_transform)
    modifier.use_max_distance = max_distance is not None
    if max_distance is not None:
        modifier.max_distance = max_distance


def _commit_weight_transfer(target, modifier, normalize):
    apply_modifier(target, modifier)
    if normalize:
        _normalize_vertex_groups(target)


def _undo_weight_transfer(target, groups_snapshot, modifier, created, previous):
    _restore_groups(target, groups_snapshot)
    if created and target.modifiers.get(modifier.name) is not None:
        target.modifiers.remove(modifier)
    elif previous is not None:
        for field, value in previous.items():
            with contextlib.suppress(Exception):
                setattr(modifier, field, value)


class DeformationHandlersMixin:
    """Transfer skin weights and configure editable B-Bone deformation."""

    def transfer_skin_weights(
        self,
        source_mesh_name,
        target_mesh_name,
        modifier_name="Rig Weight Transfer",
        mapping="POLYINTERP_NEAREST",
        source_groups="DEFORM",
        mix_mode="REPLACE",
        mix_factor=1.0,
        use_object_transform=True,
        max_distance=None,
        destination_policy="ERROR",
        commit=False,
        confirm_commit=False,
        normalize=False,
    ):
        source = _mesh_object(source_mesh_name)
        target = _mesh_object(target_mesh_name)
        if source == target:
            raise ValueError("Source and target meshes must differ")
        sync_from_editmode(source)
        sync_from_editmode(target)
        mix_factor = _validate_transfer_options(
            source,
            target,
            mapping,
            source_groups,
            mix_mode,
            mix_factor,
            max_distance,
            commit,
            confirm_commit,
            normalize,
        )
        existing = _existing_transfer_modifier(target, modifier_name, destination_policy)
        selected_group_names = _transfer_group_names(source, source_groups)
        locked = sorted(
            group.name for group in target.vertex_groups if group.lock_weight and group.name in selected_group_names
        )
        if locked:
            raise ValueError(f"Cannot transfer over locked target groups: {locked}")

        groups_snapshot = _snapshot_groups(target)
        created = existing is None
        modifier = existing or target.modifiers.new(name=modifier_name, type="DATA_TRANSFER")
        previous = None if created else {field: getattr(modifier, field) for field in _DATA_TRANSFER_FIELDS}
        try:
            _configure_weight_transfer(
                target,
                modifier,
                source,
                selected_group_names,
                mapping,
                source_groups,
                mix_mode,
                mix_factor,
                use_object_transform,
                max_distance,
            )
            if commit:
                _commit_weight_transfer(target, modifier, normalize)
        except Exception:
            _undo_weight_transfer(target, groups_snapshot, modifier, created, previous)
            raise
        return {
            "source_mesh": source.name,
            "target_mesh": target.name,
            "mapping": mapping,
            "committed": bool(commit),
            "modifier": None if commit else modifier.name,
            "locked_groups_preserved": locked,
            "changed_objects": [target.name],
            "retained_live_dependencies": [] if commit else [modifier.name],
        }

    def configure_bendy_bones(self, armature_object_name, patches):
        armature = _armature_object(armature_object_name)
        patches = list(patches or ())
        if not patches:
            raise ValueError("At least one B-Bone patch is required")
        names = [patch.get("bone_name") for patch in patches]
        if len(names) != len(set(names)):
            raise ValueError("Each bone may be patched only once per request")
        prepared = []
        for patch in patches:
            bone = armature.data.bones.get(patch.get("bone_name"))
            pose_bone = armature.pose.bones.get(patch.get("bone_name"))
            if bone is None or pose_bone is None:
                raise ValueError(f"Bone not found: {patch.get('bone_name')}")
            values = {}
            for supplied, rna_name in _BENDY_FIELDS.items():
                if supplied in patch:
                    value = patch[supplied]
                    if not hasattr(bone, rna_name):
                        raise ValueError(f"Blender does not support B-Bone field '{rna_name}'")
                    values[rna_name] = value
            for side in ("start", "end"):
                handle_name = patch.get(f"custom_handle_{side}")
                if handle_name is not None:
                    handle = armature.data.bones.get(handle_name)
                    if handle is None:
                        raise ValueError(f"Custom B-Bone handle not found: {handle_name}")
                    if handle == bone:
                        raise ValueError(f"Bone '{bone.name}' cannot be its own custom handle")
                    values[f"bbone_custom_handle_{side}"] = handle
            prepared.append((bone, pose_bone, values))
        snapshots = []
        for bone, _pose, values in prepared:
            snapshot = {}
            for name in values:
                value = getattr(bone, name)
                snapshot[name] = value.copy() if hasattr(value, "copy") else value
            snapshots.append((bone, snapshot))
        changes = []
        try:
            for bone, pose_bone, values in prepared:
                before = {name: _plain(getattr(bone, name)) for name in values}
                for name, value in values.items():
                    setattr(bone, name, value)
                bpy.context.view_layer.update()
                changes.append(
                    {
                        "bone": bone.name,
                        "old": before,
                        "new": {name: _plain(getattr(bone, name)) for name in values},
                        "segment_matrices": [
                            [list(row) for row in pose_bone.bbone_segment_matrix(index, rest=False)]
                            for index in range(min(int(bone.bbone_segments) + 1, 33))
                        ],
                    }
                )
        except Exception:
            for bone, values in snapshots:
                for name, value in values.items():
                    setattr(bone, name, value)
            raise
        return {"armature_object": armature.name, "changes": changes, "changed_objects": [armature.name]}
