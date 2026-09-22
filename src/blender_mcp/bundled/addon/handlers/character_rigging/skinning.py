# Inherited scope metrics: `bind_mesh_to_armature`, `set_skin_weights` and `clean_skin_weights`
# were over the branch/statement/local limits before this pass, and `scripts/lint_changed.py`
# attributes a whole-scope finding to any branch that writes inside the scope.
# Inherited findings, carried over with the code: every line in this module was moved verbatim out of
# `foundation.py`, where one file-wide pragma covered them, and `scripts/lint_changed.py` attributes a
# moved line to the branch that moved it. Suppressed, not fixed: restructuring the bodies would make a
# pure move unreviewable. They stay in the whole-tree backlog `just lint-all` reports.
# ruff: file-ignore[too-many-branches, too-many-locals, too-many-statements, magic-value-comparison, too-many-nested-blocks, undocumented-public-method, too-many-statements-in-try-clause]
"""
Binding a mesh to an armature, and editing the weights that result.

Vertex groups and armature modifiers are the mesh's half of a rig, and they are also state a
bone rename has to carry across an edit - which is why the group and modifier snapshots live
here, below `references`, instead of inside the transaction that uses them. Weight editing
sits with them because it is the same two datablocks under a different verb.
"""

# Blender's generated Python stubs widen many bpy collections to bpy_struct.
# pyright: reportArgumentType=false, reportGeneralTypeIssues=false

import bpy

from ...helpers import preserve_mode_and_selection, sync_from_editmode
from .primitives import _armature_object, _finite, _mesh_object, _unique_names
from .records import (
    _armature_modifier_info,
    _deform_names,
    _influence_histogram,
    _vertex_weight_map,
)


def _snapshot_groups(mesh_obj):
    sync_from_editmode(mesh_obj)
    groups = [{"name": group.name, "lock_weight": bool(group.lock_weight)} for group in mesh_obj.vertex_groups]
    weights = [_vertex_weight_map(mesh_obj, vertex) for vertex in mesh_obj.data.vertices]
    return groups, weights


def _restore_groups(mesh_obj, snapshot):
    groups, weights = snapshot
    for group in list(mesh_obj.vertex_groups):
        mesh_obj.vertex_groups.remove(group)
    for record in groups:
        group = mesh_obj.vertex_groups.new(name=record["name"])
        group.lock_weight = record["lock_weight"]
    for vertex_index, assignments in enumerate(weights):
        for name, weight in assignments.items():
            mesh_obj.vertex_groups[name].add([vertex_index], weight, "REPLACE")


def _snapshot_modifiers(mesh_obj):
    return [
        {
            "name": modifier.name,
            "type": modifier.type,
            "object": getattr(modifier, "object", None),
            "use_vertex_groups": getattr(modifier, "use_vertex_groups", None),
            "use_bone_envelopes": getattr(modifier, "use_bone_envelopes", None),
            "use_deform_preserve_volume": getattr(modifier, "use_deform_preserve_volume", None),
        }
        for modifier in mesh_obj.modifiers
    ]


def _restore_armature_modifiers(mesh_obj, records):
    for modifier in list(mesh_obj.modifiers):
        if modifier.type == "ARMATURE":
            mesh_obj.modifiers.remove(modifier)
    for index, record in enumerate(records):
        if record["type"] != "ARMATURE":
            continue
        modifier = mesh_obj.modifiers.new(name=record["name"], type="ARMATURE")
        for field in ("object", "use_vertex_groups", "use_bone_envelopes", "use_deform_preserve_volume"):
            if record[field] is not None:
                setattr(modifier, field, record[field])
        mesh_obj.modifiers.move(len(mesh_obj.modifiers) - 1, min(index, len(mesh_obj.modifiers) - 1))


class SkinningHandlersMixin:
    """Bind meshes to armatures and edit the weights that result."""

    def bind_mesh_to_armature(
        self,
        armature_object_name,
        mesh_object_names,
        method="EMPTY_GROUPS",
        modifier_name="Armature",
        existing_modifier_policy="REUSE",
        parent_meshes=False,
        preserve_volume=False,
        modifier_index=None,
        replacement_policy="PRESERVE",
        confirm_replace_weights=False,
    ):
        armature_obj = _armature_object(armature_object_name)
        names = list(mesh_object_names or ())
        if not names:
            raise ValueError("At least one mesh object is required")
        _unique_names(names, "mesh object names")
        meshes = [_mesh_object(name) for name in names]
        if method not in {"EMPTY_GROUPS", "AUTOMATIC", "ENVELOPES", "EXISTING_WEIGHTS"}:
            raise ValueError(f"Unsupported binding method: {method}")
        if replacement_policy == "REPLACE" and not confirm_replace_weights:
            raise ValueError("confirm_replace_weights=True is required when replacement_policy='REPLACE'")
        deform_names = sorted(_deform_names(armature_obj))
        for mesh in meshes:
            if parent_meshes:
                ancestor = armature_obj.parent
                while ancestor is not None:
                    if ancestor == mesh:
                        raise ValueError(f"Parenting '{mesh.name}' to '{armature_obj.name}' would create a cycle")
                    ancestor = ancestor.parent
            conflicts = [
                modifier.name
                for modifier in mesh.modifiers
                if modifier.name == modifier_name and modifier.type != "ARMATURE"
            ]
            if conflicts:
                raise ValueError(f"Modifier '{modifier_name}' on '{mesh.name}' is not an Armature modifier")
            if mesh.modifiers.get(modifier_name) is not None and existing_modifier_policy == "ERROR":
                raise ValueError(f"Modifier '{modifier_name}' already exists on '{mesh.name}'")
            existing_deform_groups = [name for name in deform_names if mesh.vertex_groups.get(name) is not None]
            if method == "EXISTING_WEIGHTS" and not existing_deform_groups:
                raise ValueError(f"'{mesh.name}' has no vertex groups matching deform bones on '{armature_obj.name}'")
            if replacement_policy == "REPLACE":
                locked = [name for name in existing_deform_groups if mesh.vertex_groups[name].lock_weight]
                if locked:
                    raise ValueError(f"Cannot replace locked deform groups on '{mesh.name}': {locked}")
            if method in {"AUTOMATIC", "ENVELOPES"} and replacement_policy == "PRESERVE":
                existing = sorted(existing_deform_groups)
                if existing:
                    raise ValueError(
                        f"{method} would replace existing deform weights on '{mesh.name}': {existing[:20]}; "
                        "use replacement_policy='REPLACE' with confirmation"
                    )
        snapshots = {
            mesh.name: {
                "groups": _snapshot_groups(mesh),
                "modifiers": _snapshot_modifiers(mesh),
                "modifier_pointers": {modifier.as_pointer() for modifier in mesh.modifiers},
                "parent": mesh.parent,
                "parent_type": mesh.parent_type,
                "parent_inverse": mesh.matrix_parent_inverse.copy(),
                "world": mesh.matrix_world.copy(),
            }
            for mesh in meshes
        }
        try:
            if replacement_policy == "REPLACE":
                for mesh in meshes:
                    for name in deform_names:
                        group = mesh.vertex_groups.get(name)
                        if group is not None:
                            mesh.vertex_groups.remove(group)
            if method in {"AUTOMATIC", "ENVELOPES"}:
                with preserve_mode_and_selection():
                    bpy.ops.object.select_all(action="DESELECT")
                    for mesh in meshes:
                        mesh.select_set(True)
                    armature_obj.select_set(True)
                    bpy.context.view_layer.objects.active = armature_obj
                    operator_type = "ARMATURE_AUTO" if method == "AUTOMATIC" else "ARMATURE_ENVELOPE"
                    result = bpy.ops.object.parent_set(type=operator_type, keep_transform=True)
                    if not isinstance(result, (set, frozenset)) or "FINISHED" not in result:
                        raise RuntimeError(f"Automatic binding operator did not finish: {result}")
            else:
                for mesh in meshes:
                    if method == "EMPTY_GROUPS":
                        for name in deform_names:
                            if mesh.vertex_groups.get(name) is None:
                                mesh.vertex_groups.new(name=name)
            for mesh in meshes:
                modifier = mesh.modifiers.get(modifier_name)
                if modifier is not None and modifier.type != "ARMATURE":
                    raise ValueError(f"Modifier '{modifier_name}' on '{mesh.name}' is not an Armature modifier")
                new_matching = [
                    item
                    for item in mesh.modifiers
                    if item.type == "ARMATURE"
                    and item.object == armature_obj
                    and item.as_pointer() not in snapshots[mesh.name]["modifier_pointers"]
                ]
                if modifier is None and method in {"AUTOMATIC", "ENVELOPES"} and new_matching:
                    modifier = new_matching[-1]
                if modifier is None:
                    modifier = mesh.modifiers.new(name=modifier_name, type="ARMATURE")
                for duplicate in new_matching:
                    if duplicate != modifier:
                        mesh.modifiers.remove(duplicate)
                modifier.name = modifier_name
                modifier.object = armature_obj
                modifier.use_vertex_groups = method != "ENVELOPES"
                modifier.use_bone_envelopes = method == "ENVELOPES"
                modifier.use_deform_preserve_volume = bool(preserve_volume)
                if modifier_index is not None:
                    mesh.modifiers.move(
                        list(mesh.modifiers).index(modifier), min(int(modifier_index), len(mesh.modifiers) - 1)
                    )
                world = mesh.matrix_world.copy()
                if parent_meshes:
                    mesh.parent = armature_obj
                    mesh.parent_type = "OBJECT"
                    mesh.matrix_parent_inverse = armature_obj.matrix_world.inverted()
                    mesh.matrix_world = world
                elif method in {"AUTOMATIC", "ENVELOPES"}:
                    # parent_set is the documented weighting operation.
                    # Undo only its parenting side effect when omitted.
                    mesh.parent = snapshots[mesh.name]["parent"]
                    mesh.parent_type = snapshots[mesh.name]["parent_type"]
                    mesh.matrix_parent_inverse = snapshots[mesh.name]["parent_inverse"]
                    mesh.matrix_world = world
        except Exception:
            for mesh in meshes:
                snapshot = snapshots[mesh.name]
                _restore_groups(mesh, snapshot["groups"])
                _restore_armature_modifiers(mesh, snapshot["modifiers"])
                mesh.parent = snapshot["parent"]
                mesh.parent_type = snapshot["parent_type"]
                mesh.matrix_parent_inverse = snapshot["parent_inverse"]
                mesh.matrix_world = snapshot["world"]
            raise
        bindings = []
        for mesh in meshes:
            modifier = mesh.modifiers.get(modifier_name)
            bindings.append(
                {
                    "mesh": mesh.name,
                    "method": method,
                    "parent": getattr(mesh.parent, "name", None),
                    "modifier": _armature_modifier_info(modifier),
                    "deform_groups": sorted(name for name in deform_names if mesh.vertex_groups.get(name) is not None),
                }
            )
        return {
            "armature_object": armature_obj.name,
            "bindings": bindings,
            "changed_objects": [mesh.name for mesh in meshes],
        }

    def set_skin_weights(self, assignments=None, normalized_vertices=None):
        assignments = list(assignments or ())
        normalized_vertices = list(normalized_vertices or ())
        if not assignments and not normalized_vertices:
            raise ValueError("At least one assignment or normalized vertex payload is required")
        meshes = {}
        for item in [*assignments, *normalized_vertices]:
            mesh = meshes.setdefault(item["mesh_object_name"], _mesh_object(item["mesh_object_name"]))
            sync_from_editmode(mesh)
        # Complete preflight, including locks and indices, before the first write.
        for item in assignments:
            mesh = meshes[item["mesh_object_name"]]
            group = mesh.vertex_groups.get(item["group_name"])
            if group is None and not item.get("create_missing_group"):
                raise ValueError(f"Vertex group '{item['group_name']}' not found on '{mesh.name}'")
            if group is not None and group.lock_weight:
                raise ValueError(f"Vertex group '{group.name}' on '{mesh.name}' is locked")
            invalid = [index for index in item["vertex_indices"] if not 0 <= index < len(mesh.data.vertices)]
            if invalid:
                raise ValueError(f"Vertex indices out of range on '{mesh.name}': {invalid[:20]}")
            weight = _finite(item["weight"], "weight")
            if not 0 <= weight <= 1:
                raise ValueError("weight must be in [0, 1]")
        for item in normalized_vertices:
            mesh = meshes[item["mesh_object_name"]]
            index = item["vertex_index"]
            if not 0 <= index < len(mesh.data.vertices):
                raise ValueError(f"Vertex index {index} out of range on '{mesh.name}'")
            if abs(sum(_finite(weight, "weight") for weight in item["weights"].values()) - 1.0) > 1e-6:
                raise ValueError(f"Normalized weights for '{mesh.name}' vertex {index} must sum to 1")
            for name in item["weights"]:
                group = mesh.vertex_groups.get(name)
                if group is None and not item.get("create_missing_groups"):
                    raise ValueError(f"Vertex group '{name}' not found on '{mesh.name}'")
                if group is not None and group.lock_weight:
                    current = _vertex_weight_map(mesh, mesh.data.vertices[index]).get(name, 0.0)
                    if abs(current - item["weights"][name]) > 1e-8:
                        raise ValueError(f"Normalized payload would alter locked group '{name}' on '{mesh.name}'")
            current = _vertex_weight_map(mesh, mesh.data.vertices[index])
            omitted_locked = [
                group.name
                for group in mesh.vertex_groups
                if group.lock_weight and current.get(group.name, 0.0) > 0 and group.name not in item["weights"]
            ]
            if omitted_locked:
                raise ValueError(f"Normalized payload omits locked assignments on '{mesh.name}': {omitted_locked}")
        snapshots = {name: _snapshot_groups(mesh) for name, mesh in meshes.items()}
        changes = []
        try:
            for item in assignments:
                mesh = meshes[item["mesh_object_name"]]
                group = mesh.vertex_groups.get(item["group_name"])
                if group is None:
                    group = mesh.vertex_groups.new(name=item["group_name"])
                indices = item["vertex_indices"]
                weight = float(item["weight"])
                mode = item.get("mode", "REPLACE")
                previous = [
                    {
                        "vertex": index,
                        "weight": _vertex_weight_map(mesh, mesh.data.vertices[index]).get(group.name),
                    }
                    for index in indices[:100]
                ]
                if mode == "REPLACE" and weight == 0:
                    group.remove(indices)
                else:
                    group.add(indices, weight, mode)
                changes.append(
                    {
                        "mesh": mesh.name,
                        "group": group.name,
                        "vertices": len(indices),
                        "mode": mode,
                        "weight": weight,
                        "previous": previous,
                        "previous_truncated": len(indices) > len(previous),
                    }
                )
            for item in normalized_vertices:
                mesh = meshes[item["mesh_object_name"]]
                index = item["vertex_index"]
                desired = item["weights"]
                previous = _vertex_weight_map(mesh, mesh.data.vertices[index])
                for name in desired:
                    if mesh.vertex_groups.get(name) is None:
                        mesh.vertex_groups.new(name=name)
                for group in mesh.vertex_groups:
                    if not group.lock_weight:
                        group.remove([index])
                for name, weight in desired.items():
                    group = mesh.vertex_groups[name]
                    if not group.lock_weight and weight > 0:
                        group.add([index], weight, "REPLACE")
                changes.append(
                    {
                        "mesh": mesh.name,
                        "vertex": index,
                        "previous_weights": previous,
                        "normalized_weights": desired,
                    }
                )
        except Exception:
            for name, mesh in meshes.items():
                _restore_groups(mesh, snapshots[name])
            raise
        changed_meshes = []
        for name, mesh in meshes.items():
            before_groups, before_weights = snapshots[name]
            current_groups = [
                {"name": group.name, "lock_weight": bool(group.lock_weight)} for group in mesh.vertex_groups
            ]
            current_weights = [_vertex_weight_map(mesh, vertex) for vertex in mesh.data.vertices]
            if before_groups != current_groups or before_weights != current_weights:
                changed_meshes.append(name)
        return {
            "changes": changes,
            "changed_objects": sorted(changed_meshes),
            "warnings": ["Refresh topology indices before reusing this payload after any topology-changing operation."],
        }

    def clean_skin_weights(
        self,
        mesh_object_name,
        armature_object_name=None,
        vertex_indices=None,
        threshold=1e-4,
        influence_limit=4,
        normalize="DEFORM",
        protected_group_names=None,
        remove_orphan_groups=False,
        confirm_remove_orphan_groups=False,
    ):
        mesh = _mesh_object(mesh_object_name)
        sync_from_editmode(mesh)
        armature_obj = _armature_object(armature_object_name) if armature_object_name else None
        threshold = _finite(threshold, "threshold")
        if not 0 <= threshold <= 1:
            raise ValueError("threshold must be in [0, 1]")
        if influence_limit is not None and not 1 <= int(influence_limit) <= 64:
            raise ValueError("influence_limit must be in [1, 64]")
        if normalize not in {"NONE", "ALL", "DEFORM"}:
            raise ValueError("normalize must be NONE, ALL, or DEFORM")
        if normalize == "DEFORM" and armature_obj is None:
            raise ValueError("armature_object_name is required when normalize='DEFORM'")
        if remove_orphan_groups and not confirm_remove_orphan_groups:
            raise ValueError("confirm_remove_orphan_groups=True is required to remove orphan groups")
        protected_names = set(protected_group_names or ())
        unknown_protected = protected_names - {group.name for group in mesh.vertex_groups}
        if unknown_protected:
            raise ValueError(f"Protected groups not found on '{mesh.name}': {sorted(unknown_protected)}")
        locked_names = {group.name for group in mesh.vertex_groups if group.lock_weight}
        protected_names |= locked_names
        deform_names = _deform_names(armature_obj) if armature_obj else set()
        included_names = (
            {group.name for group in mesh.vertex_groups}
            if normalize == "ALL"
            else deform_names
            if normalize == "DEFORM"
            else {group.name for group in mesh.vertex_groups}
        )
        if vertex_indices is None:
            indices = list(range(len(mesh.data.vertices)))
        else:
            indices = list(vertex_indices)
            _unique_names(indices, "vertex indices")
            invalid = [index for index in indices if not 0 <= index < len(mesh.data.vertices)]
            if invalid:
                raise ValueError(f"Vertex indices out of range on '{mesh.name}': {invalid[:20]}")
        before_maps = [_vertex_weight_map(mesh, vertex) for vertex in mesh.data.vertices]
        proposals = {}
        removed_assignments = []
        for index in indices:
            before = before_maps[index]
            proposed = dict(before)
            candidates = [name for name in included_names if name not in protected_names]
            for name in candidates:
                weight = proposed.get(name, 0.0)
                if weight <= threshold:
                    if name in proposed:
                        removed_assignments.append(
                            {"vertex": index, "group": name, "weight": weight, "reason": "THRESHOLD"}
                        )
                    proposed.pop(name, None)
            if influence_limit is not None:
                protected_influences = [
                    name for name in protected_names & included_names if proposed.get(name, 0.0) > 0
                ]
                if len(protected_influences) > influence_limit:
                    raise ValueError(
                        f"Vertex {index} has {len(protected_influences)} protected influences, "
                        f"above limit {influence_limit}"
                    )
                editable = sorted(
                    ((name, proposed.get(name, 0.0)) for name in candidates if proposed.get(name, 0.0) > 0),
                    key=lambda item: (-item[1], item[0]),
                )
                keep = {name for name, _weight in editable[: influence_limit - len(protected_influences)]}
                for name, weight in editable:
                    if name not in keep:
                        proposed.pop(name, None)
                        removed_assignments.append(
                            {"vertex": index, "group": name, "weight": weight, "reason": "INFLUENCE_LIMIT"}
                        )
            if normalize != "NONE":
                locked_sum = sum(proposed.get(name, 0.0) for name in protected_names & included_names)
                if locked_sum > 1.0 + 1e-8:
                    raise ValueError(
                        f"Protected weights sum to {locked_sum:.6g} on vertex {index}, so normalization is impossible"
                    )
                editable_names = [name for name in candidates if proposed.get(name, 0.0) > 0]
                editable_sum = sum(proposed[name] for name in editable_names)
                target = max(0.0, 1.0 - locked_sum)
                if editable_names and editable_sum > 0:
                    factor = target / editable_sum
                    for name in editable_names:
                        proposed[name] *= factor
            proposals[index] = proposed
        snapshot = _snapshot_groups(mesh)
        orphan_names = []
        if remove_orphan_groups:
            if armature_obj is None:
                raise ValueError("armature_object_name is required to identify orphan groups")
            orphan_names = sorted(
                group.name
                for group in mesh.vertex_groups
                if group.name not in deform_names and group.name not in protected_names
            )
        before_histogram = _influence_histogram([before_maps[index] for index in indices], included_names)
        try:
            for index, proposed in proposals.items():
                before = before_maps[index]
                touched = (set(before) | set(proposed)) & included_names - protected_names
                for name in touched:
                    group = mesh.vertex_groups.get(name)
                    if group is not None:
                        group.remove([index])
                for name in touched:
                    weight = proposed.get(name, 0.0)
                    group = mesh.vertex_groups.get(name)
                    if group is not None and weight > 0:
                        group.add([index], weight, "REPLACE")
            for name in orphan_names:
                group = mesh.vertex_groups.get(name)
                if group is not None:
                    mesh.vertex_groups.remove(group)
        except Exception:
            _restore_groups(mesh, snapshot)
            raise
        after_maps = [_vertex_weight_map(mesh, vertex) for vertex in mesh.data.vertices]
        changed_vertices = [index for index in indices if before_maps[index] != after_maps[index]]
        residual_unweighted = [
            index
            for index in indices
            if not any(name in included_names and weight > 0 for name, weight in after_maps[index].items())
        ]
        detail_limit = 2_000
        return {
            "mesh_object": mesh.name,
            "changed_vertices": changed_vertices[:detail_limit],
            "changed_vertex_count": len(changed_vertices),
            "changed_vertices_truncated": len(changed_vertices) > detail_limit,
            "removed_assignments": removed_assignments[:detail_limit],
            "removed_assignment_count": len(removed_assignments),
            "removed_assignments_truncated": len(removed_assignments) > detail_limit,
            "removed_orphan_groups": orphan_names,
            "before_influence_histogram": before_histogram,
            "after_influence_histogram": _influence_histogram([after_maps[index] for index in indices], included_names),
            "residual_unweighted_vertices": residual_unweighted[:detail_limit],
            "residual_unweighted_count": len(residual_unweighted),
            "residual_unweighted_truncated": len(residual_unweighted) > detail_limit,
            "untouched_protected_groups": sorted(protected_names),
            "changed_objects": [mesh.name] if changed_vertices or orphan_names else [],
        }
