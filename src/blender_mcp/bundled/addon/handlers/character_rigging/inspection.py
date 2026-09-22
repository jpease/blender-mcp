# Inherited scope metrics: `validate_character_rig` was over the branch/statement/local limits
# before this pass, and `scripts/lint_changed.py` attributes a whole-scope finding to any branch
# that writes inside the scope.
# Inherited findings, carried over with the code: every line in this module was moved verbatim out of
# `foundation.py`, where one file-wide pragma covered them, and `scripts/lint_changed.py` attributes a
# moved line to the branch that moved it. Suppressed, not fixed: restructuring the bodies would make a
# pure move unreviewable. They stay in the whole-tree backlog `just lint-all` reports.
# ruff: file-ignore[too-many-branches, too-many-locals, too-many-statements, magic-value-comparison, too-many-nested-blocks, undocumented-public-method]
"""
The read-only handlers: what a rig currently is, and whether it holds together.

`get_character_rig_info` and `get_skinning_info` page a rig's bones, groups and bindings;
`validate_character_rig` walks the same rig for the structural faults that only surface in a
shot - unweighted vertices, constraint cycles, bone paths animation still names after the
bone is gone. None of the three writes, which is what lets every other module in this package
be read as a write.
"""

import math
import re

from collections import Counter, defaultdict

import bpy

from ...helpers import paginate
from .constraints import _constraint_dependency_cycle
from .primitives import (
    _armature_object,
    _custom_properties,
    _finite,
    _mesh_object,
    _selected_bones,
    _unique_names,
    _validate_limit_offset,
)
from .records import (
    _animation_info,
    _bone_collection_info,
    _bone_info,
    _dependent_meshes,
    _issue,
    _pose_bone_info,
    _skinning_record,
    _transform_info,
)
from .references import _all_fcurves
from .structure import _hierarchy_cycles

_MAX_MEMBERSHIPS = 10_000_000
_BONE_PATH = re.compile(r'pose\.bones\["((?:[^"\\]|\\.)*)"\]')


class RigInspectionHandlersMixin:
    """Report what a rig is and whether it holds together."""

    def get_character_rig_info(
        self,
        armature_object_name,
        bone_limit=100,
        bone_offset=0,
        bone_names=None,
        dependency_limit=100,
        dependency_offset=0,
        include_custom_properties=True,
    ):
        armature_obj = _armature_object(armature_object_name)
        bpy.context.view_layer.update()
        _validate_limit_offset(bone_limit, bone_offset, 500, "bone")
        _validate_limit_offset(dependency_limit, dependency_offset, 500, "dependency")
        bones = _selected_bones(armature_obj, bone_names)
        start, end, truncated, next_offset = paginate(len(bones), bone_offset, bone_limit, 500)
        dependencies = _dependent_meshes(armature_obj)
        dep_start, dep_end, dep_truncated, dep_next = paginate(
            len(dependencies), dependency_offset, dependency_limit, 500
        )
        return {
            "armature_object": armature_obj.name,
            "armature_data": armature_obj.data.name,
            "transforms": _transform_info(armature_obj),
            "pose_position": armature_obj.data.pose_position,
            "display": {
                "display_type": armature_obj.data.display_type,
                "show_axes": bool(armature_obj.data.show_axes),
                "axes_position": float(armature_obj.data.axes_position),
                "show_names": bool(armature_obj.data.show_names),
                "relation_line_position": armature_obj.data.relation_line_position,
                "show_bone_custom_shapes": bool(armature_obj.data.show_bone_custom_shapes),
                "show_bone_colors": bool(armature_obj.data.show_bone_colors),
                "show_in_front": bool(armature_obj.show_in_front),
            },
            "data_users": sorted(obj.name for obj in bpy.data.objects if obj.data == armature_obj.data),
            "bone_collections": {
                "items": [
                    _bone_collection_info(collection) for collection in list(armature_obj.data.collections_all)[:500]
                ],
                "total": len(armature_obj.data.collections_all),
                "truncated": len(armature_obj.data.collections_all) > 500,
            },
            "bones": {
                "items": [_bone_info(bone, include_custom_properties) for bone in bones[start:end]],
                "total": len(bones),
                "offset": start,
                "limit": bone_limit,
                "truncated": truncated,
                "next_offset": next_offset,
                "coordinate_space": "ARMATURE_LOCAL_REST",
            },
            "pose_bones": [
                _pose_bone_info(armature_obj, armature_obj.pose.bones[bone.name], include_custom_properties)
                for bone in bones[start:end]
            ],
            "animation": {
                "object": _animation_info(armature_obj),
                "armature_data": _animation_info(armature_obj.data),
            },
            "custom_properties": _custom_properties(armature_obj) if include_custom_properties else None,
            "armature_data_custom_properties": _custom_properties(armature_obj.data)
            if include_custom_properties
            else None,
            "dependent_meshes": {
                "items": dependencies[dep_start:dep_end],
                "total": len(dependencies),
                "offset": dep_start,
                "limit": dependency_limit,
                "truncated": dep_truncated,
                "next_offset": dep_next,
            },
        }

    def get_skinning_info(
        self,
        armature_object_name,
        mesh_object_names=None,
        influence_limit=4,
        normalization_tolerance=1e-4,
        weight_epsilon=1e-6,
        membership_limit=500,
        membership_offset=0,
    ):
        armature_obj = _armature_object(armature_object_name)
        _validate_limit_offset(membership_limit, membership_offset, 2_000, "membership")
        if not 1 <= int(influence_limit) <= 64:
            raise ValueError("influence_limit must be in [1, 64]")
        tolerance = _finite(normalization_tolerance, "normalization_tolerance")
        epsilon = _finite(weight_epsilon, "weight_epsilon")
        if not 0 <= tolerance <= 1 or not 0 <= epsilon <= 1:
            raise ValueError("normalization_tolerance and weight_epsilon must be in [0, 1]")
        if mesh_object_names is None:
            meshes = [_mesh_object(record["object"]) for record in _dependent_meshes(armature_obj)]
        else:
            _unique_names(mesh_object_names, "mesh object names")
            meshes = [_mesh_object(name) for name in mesh_object_names]
        records = [_skinning_record(mesh, armature_obj, influence_limit, tolerance, epsilon) for mesh in meshes]
        memberships = [membership for record in records for membership in record.pop("memberships")]
        for record in records:
            for key in (
                "unweighted_vertices",
                "excessive_influences",
                "non_normalized_vertices",
                "zero_or_near_zero_assignments",
            ):
                values = record[key]
                record[f"{key}_total"] = len(values)
                record[f"{key}_truncated"] = len(values) > membership_limit
                record[key] = values[:membership_limit]
        start, end, truncated, next_offset = paginate(
            len(memberships), membership_offset, membership_limit, _MAX_MEMBERSHIPS
        )
        return {
            "armature_object": armature_obj.name,
            "weight_source": "BASE_MESH_VERTEX_GROUPS",
            "evaluated_deformation_included": False,
            "meshes": records,
            "memberships": {
                "items": memberships[start:end],
                "total": len(memberships),
                "offset": start,
                "limit": membership_limit,
                "truncated": truncated,
                "next_offset": next_offset,
            },
        }

    def validate_character_rig(
        self,
        armature_object_names=None,
        mesh_object_names=None,
        frames=None,
        influence_limit=4,
        normalization_tolerance=1e-4,
        issue_limit=500,
        issue_offset=0,
    ):
        _validate_limit_offset(issue_limit, issue_offset, 2_000, "issue")
        if not 1 <= int(influence_limit) <= 64:
            raise ValueError("influence_limit must be in [1, 64]")
        tolerance = _finite(normalization_tolerance, "normalization_tolerance")
        if not 0 <= tolerance <= 1:
            raise ValueError("normalization_tolerance must be in [0, 1]")
        if armature_object_names is None:
            armatures = [obj for obj in bpy.data.objects if obj.type == "ARMATURE"]
        else:
            _unique_names(armature_object_names, "armature object names")
            armatures = [_armature_object(name) for name in armature_object_names]
        if mesh_object_names is None:
            armature_set = set(armatures)
            meshes = [
                obj
                for obj in bpy.data.objects
                if obj.type == "MESH"
                and (
                    obj.parent in armature_set
                    or any(
                        modifier.type == "ARMATURE" and modifier.object in armature_set for modifier in obj.modifiers
                    )
                )
            ]
        else:
            _unique_names(mesh_object_names, "mesh object names")
            meshes = [_mesh_object(name) for name in mesh_object_names]
        issues = []
        rig_ids = defaultdict(list)
        for armature_obj in armatures:
            rig_id = armature_obj.get("rig_id")
            if rig_id is not None:
                rig_ids[str(rig_id)].append(armature_obj.name)
            if any(abs(abs(float(value)) - 1.0) > 1e-5 for value in armature_obj.scale):
                severity = "ERROR" if any(float(value) < 0 for value in armature_obj.scale) else "WARNING"
                issues.append(
                    _issue(
                        "ARMATURE_NONUNIT_SCALE",
                        severity,
                        "Armature object scale is non-unit or negative",
                        object=armature_obj.name,
                        evidence={"scale": list(armature_obj.scale)},
                        remediation="Review and intentionally apply or preserve armature scale before delivery.",
                    )
                )
            collections = list(armature_obj.data.collections_all)
            if not collections:
                issues.append(
                    _issue(
                        "NO_BONE_COLLECTIONS",
                        "WARNING",
                        "Armature has no bone collections",
                        object=armature_obj.name,
                        remediation="Organize deform, mechanism, and control bones in named bone collections.",
                    )
                )
            for collection in collections:
                if not collection.bones:
                    issues.append(
                        _issue(
                            "EMPTY_BONE_COLLECTION",
                            "INFO",
                            "Bone collection is empty",
                            object=armature_obj.name,
                            bone_collection=collection.name,
                            remediation="Assign intended bones or remove the collection after confirmation.",
                        )
                    )
            parents = {bone.name: getattr(bone.parent, "name", None) for bone in armature_obj.data.bones}
            for cycle in _hierarchy_cycles(parents):
                issues.append(
                    _issue(
                        "BONE_HIERARCHY_CYCLE",
                        "ERROR",
                        "Bone hierarchy contains a cycle",
                        object=armature_obj.name,
                        evidence={"cycle": cycle},
                        remediation="Break the parent cycle in the rest hierarchy.",
                    )
                )
            for bone in armature_obj.data.bones:
                if bone.length <= 1e-8:
                    issues.append(
                        _issue(
                            "ZERO_LENGTH_BONE",
                            "ERROR",
                            "Bone has zero or near-zero rest length",
                            object=armature_obj.name,
                            bone=bone.name,
                            evidence={"length": float(bone.length)},
                            remediation="Move the head or tail in Edit Mode.",
                        )
                    )
                if (
                    bone.use_connect
                    and bone.parent is not None
                    and (bone.head_local - bone.parent.tail_local).length > 1e-6
                ):
                    issues.append(
                        _issue(
                            "CONNECTED_BONE_GAP",
                            "ERROR",
                            "Connected bone head does not match parent tail",
                            object=armature_obj.name,
                            bone=bone.name,
                            evidence={"gap": float((bone.head_local - bone.parent.tail_local).length)},
                            remediation="Snap the connected head to its parent tail.",
                        )
                    )
                pose_bone = armature_obj.pose.bones.get(bone.name)
                if pose_bone is None:
                    continue
                if pose_bone.custom_shape is not None and bpy.data.objects.get(pose_bone.custom_shape.name) is None:
                    issues.append(
                        _issue(
                            "INVALID_CUSTOM_SHAPE",
                            "ERROR",
                            "Pose bone custom shape is no longer a live object",
                            object=armature_obj.name,
                            bone=bone.name,
                            remediation="Assign an existing custom-shape object.",
                        )
                    )
                for constraint in pose_bone.constraints:
                    target = getattr(constraint, "target", None)
                    subtarget = getattr(constraint, "subtarget", "")
                    if not getattr(constraint, "is_valid", True):
                        issues.append(
                            _issue(
                                "INVALID_CONSTRAINT",
                                "ERROR",
                                "Blender reports that the pose constraint is invalid",
                                object=armature_obj.name,
                                bone=bone.name,
                                constraint=constraint.name,
                                remediation="Inspect its target, spaces, and dependency graph.",
                            )
                        )
                    if (
                        hasattr(constraint, "target")
                        and target is None
                        and constraint.type
                        not in {
                            "LIMIT_LOCATION",
                            "LIMIT_ROTATION",
                            "LIMIT_SCALE",
                        }
                    ):
                        issues.append(
                            _issue(
                                "MISSING_CONSTRAINT_TARGET",
                                "ERROR",
                                "Constraint has no target",
                                object=armature_obj.name,
                                bone=bone.name,
                                constraint=constraint.name,
                                remediation="Assign a valid target or remove the constraint.",
                            )
                        )
                    elif subtarget and (target.type != "ARMATURE" or target.data.bones.get(subtarget) is None):
                        issues.append(
                            _issue(
                                "MISSING_CONSTRAINT_SUBTARGET",
                                "ERROR",
                                "Constraint subtarget does not exist",
                                object=armature_obj.name,
                                bone=bone.name,
                                constraint=constraint.name,
                                evidence={"target": target.name, "subtarget": subtarget},
                                remediation="Choose an existing target bone.",
                            )
                        )
                    elif (
                        target == armature_obj
                        and subtarget
                        and _constraint_dependency_cycle(
                            armature_obj, bone.name, subtarget, ignored_constraint=constraint
                        )
                    ):
                        issues.append(
                            _issue(
                                "CONSTRAINT_DEPENDENCY_CYCLE",
                                "ERROR",
                                "Pose constraint participates in a bone dependency cycle",
                                object=armature_obj.name,
                                bone=bone.name,
                                constraint=constraint.name,
                                evidence={"subtarget": subtarget},
                                remediation="Remove or redirect one constraint edge in the cycle.",
                            )
                        )
                    if constraint.type == "IK":
                        if constraint.chain_count > 0:
                            available = 1
                            cursor = bone.parent
                            while cursor is not None:
                                available += 1
                                cursor = cursor.parent
                            if constraint.chain_count > available:
                                issues.append(
                                    _issue(
                                        "INVALID_IK_CHAIN_LENGTH",
                                        "ERROR",
                                        "IK chain_count exceeds the available parent chain",
                                        object=armature_obj.name,
                                        bone=bone.name,
                                        constraint=constraint.name,
                                        evidence={"chain_count": constraint.chain_count, "available": available},
                                        remediation="Reduce chain_count or extend the parent chain.",
                                    )
                                )
                        if (
                            constraint.pole_target is not None
                            and constraint.pole_target == armature_obj
                            and constraint.pole_subtarget == bone.name
                        ):
                            issues.append(
                                _issue(
                                    "INVALID_IK_POLE",
                                    "ERROR",
                                    "IK pole targets the constrained bone itself",
                                    object=armature_obj.name,
                                    bone=bone.name,
                                    constraint=constraint.name,
                                    remediation="Use a separate pole control.",
                                )
                            )
            for bone in armature_obj.data.bones:
                if not bone.name.endswith(".L"):
                    continue
                partner = armature_obj.data.bones.get(f"{bone.name[:-2]}.R")
                if partner is None:
                    continue
                expected_head = bone.head_local.copy()
                expected_tail = bone.tail_local.copy()
                expected_head.x *= -1
                expected_tail.x *= -1
                expected_z = bone.matrix_local.to_3x3().col[2].copy()
                expected_z.x *= -1
                partner_z = partner.matrix_local.to_3x3().col[2]
                geometry_error = max(
                    float((expected_head - partner.head_local).length),
                    float((expected_tail - partner.tail_local).length),
                )
                roll_alignment = float(expected_z.normalized().dot(partner_z.normalized()))
                if geometry_error > 1e-5 or roll_alignment < 0.9999:
                    issues.append(
                        _issue(
                            "INCONSISTENT_MIRROR_PAIR",
                            "WARNING",
                            "Left/right bone pair is not an X-reflected rest transform",
                            object=armature_obj.name,
                            bone=bone.name,
                            evidence={
                                "partner": partner.name,
                                "geometry_error": geometry_error,
                                "roll_axis_alignment": roll_alignment,
                            },
                            remediation="Mirror the pair from the authoritative side or review intentional asymmetry.",
                        )
                    )
            for owner in (armature_obj, armature_obj.data):
                for curve in _all_fcurves(owner):
                    for encoded_name in _BONE_PATH.findall(curve.data_path):
                        referenced_name = encoded_name.replace('\\"', '"').replace("\\\\", "\\")
                        if armature_obj.data.bones.get(referenced_name) is None:
                            issues.append(
                                _issue(
                                    "BROKEN_ANIMATION_BONE_PATH",
                                    "ERROR",
                                    "Animation or driver data path references a missing bone",
                                    object=armature_obj.name,
                                    bone=referenced_name,
                                    evidence={"owner": owner.name, "data_path": curve.data_path},
                                    remediation="Repair or remove the stale F-Curve/driver path.",
                                )
                            )
                    driver = getattr(curve, "driver", None)
                    for variable in getattr(driver, "variables", ()) if driver is not None else ():
                        for target in variable.targets:
                            if target.id is None:
                                issues.append(
                                    _issue(
                                        "BROKEN_DRIVER_TARGET",
                                        "ERROR",
                                        "Driver variable has no target ID",
                                        object=armature_obj.name,
                                        evidence={"owner": owner.name, "data_path": curve.data_path},
                                        remediation="Assign a live target or remove the driver variable.",
                                    )
                                )
                            elif target.bone_target and (
                                getattr(target.id, "type", None) != "ARMATURE"
                                or target.id.data.bones.get(target.bone_target) is None
                            ):
                                issues.append(
                                    _issue(
                                        "BROKEN_DRIVER_BONE_TARGET",
                                        "ERROR",
                                        "Driver variable references a missing bone target",
                                        object=armature_obj.name,
                                        bone=target.bone_target,
                                        evidence={
                                            "owner": owner.name,
                                            "data_path": curve.data_path,
                                            "target": getattr(target.id, "name", None),
                                        },
                                        remediation="Choose an existing armature bone target.",
                                    )
                                )
            users = [obj.name for obj in bpy.data.objects if obj.data == armature_obj.data]
            if len(users) > 1:
                issues.append(
                    _issue(
                        "SHARED_ARMATURE_DATA",
                        "WARNING",
                        "Armature datablock is shared by multiple objects",
                        object=armature_obj.name,
                        evidence={"users": users},
                        remediation="Confirm shared rest-data edits are intentional.",
                    )
                )
            animation = getattr(armature_obj, "animation_data", None)
            action = getattr(animation, "action", None) if animation else None
            if action is not None:
                action_users = [
                    obj.name
                    for obj in bpy.data.objects
                    if getattr(getattr(obj, "animation_data", None), "action", None) == action
                ]
                if len(action_users) > 1:
                    issues.append(
                        _issue(
                            "SHARED_ACTION",
                            "WARNING",
                            "Action is shared by multiple objects",
                            object=armature_obj.name,
                            evidence={"action": action.name, "users": action_users},
                            remediation="Confirm edits to the shared action are intentional.",
                        )
                    )
        for rig_id, names in rig_ids.items():
            if len(names) > 1:
                issues.append(
                    _issue(
                        "DUPLICATE_RIG_ID",
                        "ERROR",
                        "Multiple armature objects share the same rig_id",
                        evidence={"rig_id": rig_id, "objects": names},
                        remediation="Assign a unique rig_id to each independent rig.",
                    )
                )
        for mesh in meshes:
            if any(abs(abs(float(value)) - 1.0) > 1e-5 for value in mesh.scale):
                severity = "ERROR" if any(float(value) < 0 for value in mesh.scale) else "WARNING"
                issues.append(
                    _issue(
                        "MESH_NONUNIT_SCALE",
                        severity,
                        "Skinned mesh scale is non-unit or negative",
                        object=mesh.name,
                        evidence={"scale": list(mesh.scale)},
                        remediation="Review mesh scale before binding or export.",
                    )
                )
            modifiers = [
                (index, modifier) for index, modifier in enumerate(mesh.modifiers) if modifier.type == "ARMATURE"
            ]
            if not modifiers:
                issues.append(
                    _issue(
                        "MISSING_ARMATURE_MODIFIER",
                        "ERROR",
                        "Skinned mesh has no Armature modifier",
                        object=mesh.name,
                        remediation="Bind the mesh to an explicit armature.",
                    )
                )
                continue
            for index, modifier in modifiers:
                if modifier.object not in armatures:
                    issues.append(
                        _issue(
                            "WRONG_ARMATURE_TARGET",
                            "ERROR",
                            "Armature modifier targets an armature outside the validation scope",
                            object=mesh.name,
                            evidence={"modifier": modifier.name, "target": getattr(modifier.object, "name", None)},
                            remediation="Assign the intended armature or include it in validation scope.",
                        )
                    )
                    continue
                topology_indices = [
                    i for i, item in enumerate(mesh.modifiers) if item.type in {"SUBSURF", "REMESH", "NODES"}
                ]
                if topology_indices and index > min(topology_indices):
                    issues.append(
                        _issue(
                            "ARMATURE_MODIFIER_ORDER",
                            "WARNING",
                            "Armature modifier follows a topology-changing modifier",
                            object=mesh.name,
                            evidence={"modifier": modifier.name, "stack_index": index},
                            remediation="Review modifier order so weights address the intended topology.",
                        )
                    )
                skin = _skinning_record(mesh, modifier.object, influence_limit, tolerance, 1e-8)
                for vertex in skin["unweighted_vertices"]:
                    issues.append(
                        _issue(
                            "UNWEIGHTED_VERTEX",
                            "ERROR",
                            "Vertex has no positive deform-bone weight",
                            object=mesh.name,
                            vertex=vertex,
                            remediation="Assign and normalize deform weights.",
                        )
                    )
                for record in skin["non_normalized_vertices"]:
                    issues.append(
                        _issue(
                            "NON_NORMALIZED_VERTEX",
                            "WARNING",
                            "Deform weights do not sum to one",
                            object=mesh.name,
                            vertex=record["vertex"],
                            evidence={"sum": record["sum"]},
                            remediation="Normalize deform weights while respecting locked groups.",
                        )
                    )
                for record in skin["excessive_influences"]:
                    issues.append(
                        _issue(
                            "EXCESSIVE_INFLUENCES",
                            "WARNING",
                            "Vertex exceeds the configured deform influence limit",
                            object=mesh.name,
                            vertex=record["vertex"],
                            evidence={"count": record["count"], "limit": influence_limit},
                            remediation="Prune and normalize weights with a stable influence limit.",
                        )
                    )
                for group in skin["groups_for_missing_bones"]:
                    issues.append(
                        _issue(
                            "GROUP_FOR_MISSING_BONE",
                            "WARNING",
                            "Vertex group does not match any bone on the target armature",
                            object=mesh.name,
                            vertex_group=group,
                            remediation="Confirm it is non-deforming or remove it explicitly.",
                        )
                    )
                for group in skin["absent_deform_groups"]:
                    issues.append(
                        _issue(
                            "ABSENT_DEFORM_GROUP",
                            "INFO",
                            "Deform bone has no corresponding vertex group",
                            object=mesh.name,
                            bone=group,
                            remediation="Create the group if this bone should influence the mesh.",
                        )
                    )
        frame_records = []
        frames = list(frames or ())
        if len(frames) > 50:
            raise ValueError("At most 50 validation frames are allowed")
        scene = bpy.context.scene
        original_frame = scene.frame_current
        try:
            for frame in frames:
                scene.frame_set(int(frame))
                bpy.context.view_layer.update()
                for armature_obj in armatures:
                    invalid_bones = []
                    for pose_bone in armature_obj.pose.bones:
                        if any(not math.isfinite(float(value)) for row in pose_bone.matrix for value in row):
                            invalid_bones.append(pose_bone.name)
                    frame_records.append(
                        {"frame": int(frame), "armature": armature_obj.name, "finite_pose_matrices": not invalid_bones}
                    )
                    for bone_name in invalid_bones:
                        issues.append(
                            _issue(
                                "NONFINITE_EVALUATED_POSE",
                                "ERROR",
                                "Evaluated pose matrix contains a non-finite value",
                                object=armature_obj.name,
                                bone=bone_name,
                                frame=int(frame),
                                remediation="Inspect constraints, drivers, and keyed transforms at this frame.",
                            )
                        )
        finally:
            if frames:
                scene.frame_set(original_frame)
                bpy.context.view_layer.update()
        severity_order = {"ERROR": 0, "WARNING": 1, "INFO": 2}
        issues.sort(
            key=lambda item: (
                severity_order[item["severity"]],
                item["code"],
                item.get("object", ""),
                item.get("bone", ""),
                item.get("vertex", -1),
            )
        )
        start, end, truncated, next_offset = paginate(len(issues), issue_offset, issue_limit, 2_000)
        counts = Counter(item["severity"] for item in issues)
        return {
            "valid": counts["ERROR"] == 0,
            "summary": {"errors": counts["ERROR"], "warnings": counts["WARNING"], "info": counts["INFO"]},
            "issues": {
                "items": issues[start:end],
                "total": len(issues),
                "offset": start,
                "limit": issue_limit,
                "truncated": truncated,
                "next_offset": next_offset,
            },
            "evaluated_frames": frame_records,
            "scope": {
                "armatures": [obj.name for obj in armatures],
                "meshes": [obj.name for obj in meshes],
                "frames": frames,
            },
            "limitations": [
                "Structural validation does not certify artistic deformation or control behavior.",
                "Rest-pose changes made before this request cannot be inferred without an external baseline.",
                "IK pole quality is checked structurally; artistic pole placement still requires review.",
            ],
        }
