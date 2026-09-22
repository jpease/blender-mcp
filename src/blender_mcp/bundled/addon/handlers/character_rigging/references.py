# Inherited scope metrics: `_bone_dependencies`, `_rename_references`, `_remove_bone_references`
# and `_bone_reference_transaction` were over the branch limit before this pass, and
# `scripts/lint_changed.py` attributes a whole-scope finding to any branch that writes inside the
# scope.
# Inherited findings, carried over with the code: every line in this module was moved verbatim out of
# `foundation.py`, where one file-wide pragma covered them, and `scripts/lint_changed.py` attributes a
# moved line to the branch that moved it. Suppressed, not fixed: restructuring the bodies would make a
# pure move unreviewable. They stay in the whole-tree backlog `just lint-all` reports.
# ruff: file-ignore[too-many-branches, too-many-statements-in-try-clause]
"""
What else in the blend file names a bone, and keeping those names true across an edit.

Renaming or removing a bone is never local: constraints on other rigs, drivers, F-curve data
paths, vertex groups and armature modifiers all address bones by string, and Blender repairs
none of them. `_bone_reference_transaction` is the single place that takes the whole picture
before an edit-mode change and puts it back afterwards, so it sits apart from the structural
editing that calls it and from the records that only describe a rig.
"""

import contextlib

import bpy

# Imported under this file's existing private name so the three call sites below stay
# byte-identical: `_remove_bone_references` and `_bone_reference_transaction` already carry
# an inherited PLR0912 backlog, and rewriting a line inside either would transfer ownership
# of that finding to this change, which has nothing to do with branch count.
from ..action_assignment import action_fcurve_collections as _action_fcurve_collections
from .primitives import _bone_path_token
from .skinning import _restore_groups, _snapshot_groups


def _has_animation(armature_obj):
    for owner in _rig_animation_owners(armature_obj):
        animation = getattr(owner, "animation_data", None)
        if animation is not None and (
            getattr(animation, "action", None) is not None or len(animation.nla_tracks) or len(animation.drivers)
        ):
            return True
    return False


def _all_fcurves(owner):
    animation = getattr(owner, "animation_data", None)
    if animation is None:
        return []
    curves = list(animation.drivers)
    for action in _animation_actions(owner):
        for collection in _action_fcurve_collections(action):
            curves.extend(collection)
    return list({curve.as_pointer(): curve for curve in curves}.values())


def _animation_actions(owner):
    animation = getattr(owner, "animation_data", None)
    if animation is None:
        return []
    actions = []
    action = getattr(animation, "action", None)
    if action is not None:
        actions.append(action)
    for track in animation.nla_tracks:
        for strip in track.strips:
            if strip.action is not None:
                actions.append(strip.action)
    return list({action.as_pointer(): action for action in actions}.values())


def _armature_users(armature_obj):
    return [obj for obj in bpy.data.objects if obj.type == "ARMATURE" and obj.data == armature_obj.data]


def _rig_animation_owners(armature_obj):
    return [*_armature_users(armature_obj), armature_obj.data]


def _mesh_uses_armature_data(mesh_obj, armature_obj):
    users = set(_armature_users(armature_obj))
    return mesh_obj.parent in users or any(
        modifier.type == "ARMATURE" and modifier.object in users for modifier in mesh_obj.modifiers
    )


def _bone_dependencies(armature_obj, bone_name):
    dependencies = []
    users = _armature_users(armature_obj)
    for obj in bpy.data.objects:
        if obj.type == "MESH" and _mesh_uses_armature_data(obj, armature_obj):
            group = obj.vertex_groups.get(bone_name)
            if group is not None:
                dependencies.append({"kind": "VERTEX_GROUP", "object": obj.name, "name": group.name})
        for constraint in getattr(obj, "constraints", ()):
            if getattr(constraint, "target", None) in users and getattr(constraint, "subtarget", "") == bone_name:
                dependencies.append({"kind": "OBJECT_CONSTRAINT", "object": obj.name, "name": constraint.name})
    for user in users:
        for pose_bone in user.pose.bones:
            for constraint in pose_bone.constraints:
                if getattr(constraint, "target", None) in users and getattr(constraint, "subtarget", "") == bone_name:
                    dependencies.append(
                        {
                            "kind": "POSE_CONSTRAINT",
                            "object": user.name,
                            "bone": pose_bone.name,
                            "name": constraint.name,
                        }
                    )
    token = _bone_path_token(bone_name)
    for owner in _rig_animation_owners(armature_obj):
        for curve in _all_fcurves(owner):
            if token in curve.data_path:
                dependencies.append({"kind": "FCURVE", "owner": owner.name, "data_path": curve.data_path})
            driver = getattr(curve, "driver", None)
            for variable in getattr(driver, "variables", ()) if driver is not None else ():
                for target in variable.targets:
                    if target.id in users and target.bone_target == bone_name:
                        dependencies.append(
                            {"kind": "DRIVER_TARGET", "owner": owner.name, "data_path": curve.data_path}
                        )
    return dependencies


def _rename_references(armature_obj, old_name, new_name):
    affected = []
    users = _armature_users(armature_obj)
    for obj in bpy.data.objects:
        if obj.type == "MESH" and _mesh_uses_armature_data(obj, armature_obj):
            group = obj.vertex_groups.get(old_name)
            if group is not None:
                group.name = new_name
                affected.append({"kind": "VERTEX_GROUP", "object": obj.name, "old": old_name, "new": group.name})
        for constraint in getattr(obj, "constraints", ()):
            if getattr(constraint, "target", None) in users and getattr(constraint, "subtarget", "") == old_name:
                constraint.subtarget = new_name
                affected.append({"kind": "OBJECT_CONSTRAINT", "object": obj.name, "name": constraint.name})
    for user in users:
        for pose_bone in user.pose.bones:
            for constraint in pose_bone.constraints:
                if getattr(constraint, "target", None) in users and getattr(constraint, "subtarget", "") == old_name:
                    constraint.subtarget = new_name
                    affected.append(
                        {
                            "kind": "POSE_CONSTRAINT",
                            "object": user.name,
                            "bone": pose_bone.name,
                            "name": constraint.name,
                        }
                    )
    old_token = _bone_path_token(old_name)
    new_token = _bone_path_token(new_name)
    for owner in _rig_animation_owners(armature_obj):
        for curve in _all_fcurves(owner):
            old_path = curve.data_path
            curve.data_path = old_path.replace(old_token, new_token)
            if curve.data_path != old_path:
                affected.append({"kind": "FCURVE", "owner": owner.name, "old": old_path, "new": curve.data_path})
            driver = getattr(curve, "driver", None)
            for variable in getattr(driver, "variables", ()) if driver is not None else ():
                for target in variable.targets:
                    if target.id in users and target.bone_target == old_name:
                        target.bone_target = new_name
                        affected.append({"kind": "DRIVER_TARGET", "owner": owner.name, "data_path": curve.data_path})
    return affected


def _remove_bone_references(armature_obj, bone_name):
    affected = []
    users = _armature_users(armature_obj)
    for obj in bpy.data.objects:
        if obj.type == "MESH" and _mesh_uses_armature_data(obj, armature_obj):
            group = obj.vertex_groups.get(bone_name)
            if group is not None:
                obj.vertex_groups.remove(group)
                affected.append({"kind": "VERTEX_GROUP", "object": obj.name, "name": bone_name})
        for constraint in getattr(obj, "constraints", ()):
            if getattr(constraint, "target", None) in users and getattr(constraint, "subtarget", "") == bone_name:
                constraint.subtarget = ""
                affected.append({"kind": "OBJECT_CONSTRAINT_SUBTARGET", "object": obj.name, "name": constraint.name})
    for user in users:
        for pose_bone in user.pose.bones:
            for constraint in pose_bone.constraints:
                if getattr(constraint, "target", None) in users and getattr(constraint, "subtarget", "") == bone_name:
                    constraint.subtarget = ""
                    affected.append(
                        {
                            "kind": "POSE_CONSTRAINT_SUBTARGET",
                            "object": user.name,
                            "bone": pose_bone.name,
                            "name": constraint.name,
                        }
                    )
    token = _bone_path_token(bone_name)
    for owner in _rig_animation_owners(armature_obj):
        animation = getattr(owner, "animation_data", None)
        if animation is None:
            continue
        for action in _animation_actions(owner):
            for collection in _action_fcurve_collections(action):
                for curve in list(collection):
                    if token in curve.data_path:
                        path = curve.data_path
                        collection.remove(curve)
                        affected.append(
                            {"kind": "FCURVE", "owner": owner.name, "action": action.name, "data_path": path}
                        )
        for curve in list(animation.drivers):
            if token in curve.data_path:
                path = curve.data_path
                animation.drivers.remove(curve)
                affected.append({"kind": "DRIVER", "owner": owner.name, "data_path": path})
                continue
            for variable in curve.driver.variables:
                for target in variable.targets:
                    if target.id in users and target.bone_target == bone_name:
                        target.bone_target = ""
                        affected.append(
                            {
                                "kind": "DRIVER_TARGET",
                                "owner": owner.name,
                                "data_path": curve.data_path,
                            }
                        )
    return affected


def _references_any_bone(curve, armature_users, bone_names):
    if any(_bone_path_token(name) in curve.data_path for name in bone_names):
        return True
    driver = getattr(curve, "driver", None)
    return any(
        target.id in armature_users and target.bone_target in bone_names
        for variable in getattr(driver, "variables", ())
        if driver is not None
        for target in variable.targets
    )


def _restore_drivers(owner, backup_holder):
    animation = getattr(owner, "animation_data", None)
    if animation is not None:
        for curve in list(animation.drivers):
            animation.drivers.remove(curve)
    backup_animation = getattr(backup_holder, "animation_data", None)
    if backup_animation is None or not len(backup_animation.drivers):
        return
    owner_animation = owner.animation_data_create()
    for curve in backup_animation.drivers:
        owner_animation.drivers.from_existing(src_driver=curve)


@contextlib.contextmanager
def _bone_reference_transaction(armature_obj, bone_names):
    """Rollback every external reference edited by rename/delete operations."""
    names = set(bone_names)
    users = set(_armature_users(armature_obj))
    group_snapshots = []
    constraint_snapshots = []
    action_copies = []
    driver_backups = []
    try:
        for obj in bpy.data.objects:
            if (
                obj.type == "MESH"
                and _mesh_uses_armature_data(obj, armature_obj)
                and any(obj.vertex_groups.get(name) is not None for name in names)
            ):
                group_snapshots.append((obj, _snapshot_groups(obj)))
            for constraint in getattr(obj, "constraints", ()):
                if getattr(constraint, "target", None) in users and constraint.subtarget in names:
                    constraint_snapshots.append((constraint, constraint.subtarget))
        for user in users:
            for pose_bone in user.pose.bones:
                for constraint in pose_bone.constraints:
                    if getattr(constraint, "target", None) in users and constraint.subtarget in names:
                        constraint_snapshots.append((constraint, constraint.subtarget))

        owners = _rig_animation_owners(armature_obj)
        actions = {
            action.as_pointer(): action
            for owner in owners
            for action in _animation_actions(owner)
            if any(
                any(_bone_path_token(name) in curve.data_path for name in names)
                for collection in _action_fcurve_collections(action)
                for curve in collection
            )
        }
        for action in actions.values():
            original_name = action.name
            backup = action.copy()
            action_copies.append((action, backup, original_name))

        for owner in owners:
            animation = getattr(owner, "animation_data", None)
            if animation is None or not any(_references_any_bone(curve, users, names) for curve in animation.drivers):
                continue
            backup_holder = bpy.data.objects.new(f"{owner.name}.MCP Driver Backup", None)
            backup_animation = backup_holder.animation_data_create()
            for curve in animation.drivers:
                backup_animation.drivers.from_existing(src_driver=curve)
            driver_backups.append((owner, backup_holder))
        yield
    except Exception:
        for owner, backup_holder in reversed(driver_backups):
            _restore_drivers(owner, backup_holder)
        for action, backup, original_name in reversed(action_copies):
            action.user_remap(backup)
            if bpy.data.actions.get(action.name) is action:
                bpy.data.actions.remove(action, do_unlink=True)
            backup.name = original_name
        for constraint, subtarget in reversed(constraint_snapshots):
            constraint.subtarget = subtarget
        for mesh, snapshot in reversed(group_snapshots):
            _restore_groups(mesh, snapshot)
        raise
    else:
        for _action, backup, _original_name in action_copies:
            if bpy.data.actions.get(backup.name) is backup:
                bpy.data.actions.remove(backup, do_unlink=True)
    finally:
        for _owner, backup_holder in driver_backups:
            if bpy.data.objects.get(backup_holder.name) is backup_holder:
                bpy.data.objects.remove(backup_holder, do_unlink=True)
