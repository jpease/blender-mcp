# Inherited scope metrics: `configure_armature_bones` and its neighbours were over the
# branch/statement/local limits before this pass, and `scripts/lint_changed.py` attributes a
# whole-scope finding to any branch that writes inside the scope.
# Inherited findings, carried over with the code: every line in this module was moved verbatim out of
# `foundation.py`, where one file-wide pragma covered them, and `scripts/lint_changed.py` attributes a
# moved line to the branch that moved it. Suppressed, not fixed: restructuring the bodies would make a
# pure move unreviewable. They stay in the whole-tree backlog `just lint-all` reports.
# ruff: file-ignore[too-many-branches, too-many-locals, too-many-statements, magic-value-comparison, too-many-nested-blocks, undocumented-public-method, too-many-statements-in-try-clause, fallible-context-manager]
"""
The armature itself: creating bones, editing them in edit mode, and mirroring them.

Edit mode is what shapes this module. `EditBone` exists only between `_enter_armature_edit`
and `_exit_object_mode`, every reference to a bone breaks the moment its name changes inside
that window, and a caller's bone list has to be checked for cycles and duplicates before any
of it is applied. Those three concerns are what this file owns; the handlers that only read a
rig are in `inspection`.
"""

# Blender's generated Python stubs widen many bpy collections to bpy_struct.
# pyright: reportArgumentType=false, reportGeneralTypeIssues=false

import contextlib

import bpy
import mathutils

from ...helpers import preserve_mode_and_selection, set_active
from .constraints import _copy_pose_constraint
from .primitives import (
    _POSE_FIELDS,
    _armature_object,
    _distance,
    _ensure_object_collection,
    _finite,
    _override_property_warning,
    _plain,
    _required_name,
    _unique_names,
    _vector,
    _vector_tuple,
)
from .records import _bone_collection_info, _transform_info
from .references import (
    _bone_dependencies,
    _bone_reference_transaction,
    _has_animation,
    _remove_bone_references,
    _rename_references,
)

_BONE_DATA_FIELDS = (
    "use_deform",
    "use_inherit_rotation",
    "inherit_scale",
    "use_local_location",
    "use_relative_parent",
    "use_envelope_multiply",
    "envelope_distance",
    "envelope_weight",
    "head_radius",
    "tail_radius",
)
_EDIT_BONE_COPY_FIELDS = (
    "roll",
    "use_connect",
    "use_deform",
    "use_inherit_rotation",
    "inherit_scale",
    "use_local_location",
    "use_relative_parent",
    "use_envelope_multiply",
    "envelope_distance",
    "envelope_weight",
    "head_radius",
    "tail_radius",
    "bbone_segments",
    "bbone_mapping_mode",
    "bbone_x",
    "bbone_z",
    "hide_select",
    "show_wire",
)


def _hierarchy_cycles(parent_by_name):
    cycles = []
    visited = set()
    active = []
    active_set = set()

    def visit(name):
        if name in active_set:
            start = active.index(name)
            cycles.append([*active[start:], name])
            return
        if name in visited:
            return
        active.append(name)
        active_set.add(name)
        parent = parent_by_name.get(name)
        if parent is not None:
            visit(parent)
        active.pop()
        active_set.remove(name)
        visited.add(name)

    for name in parent_by_name:
        visit(name)
    return cycles


def _validate_bone_specs(specs, collection_names):
    names = [_required_name(spec["name"], "bone.name") for spec in specs]
    _unique_names(names, "bone names")
    name_set = set(names)
    parents = {}
    for spec in specs:
        name = spec["name"]
        head = _vector_tuple(spec.get("head"), f"bone '{name}' head")
        tail = _vector_tuple(spec.get("tail"), f"bone '{name}' tail")
        if _distance(tail, head) <= 1e-8:
            raise ValueError(f"Bone '{name}' must have non-zero length")
        parent = spec.get("parent")
        if parent is not None and parent not in name_set:
            raise ValueError(f"Parent bone '{parent}' for '{name}' is not in the requested hierarchy")
        if parent == name:
            raise ValueError(f"Bone '{name}' cannot parent itself")
        parents[name] = parent
        missing = set(spec.get("collections", ())) - collection_names
        if missing:
            raise ValueError(f"Bone '{name}' references missing collections: {', '.join(sorted(missing))}")
    cycles = _hierarchy_cycles(parents)
    if cycles:
        raise ValueError(f"Bone hierarchy contains a cycle: {' -> '.join(cycles[0])}")
    by_name = {spec["name"]: spec for spec in specs}
    for spec in specs:
        if spec.get("use_connect") and spec.get("parent"):
            parent_tail = _vector_tuple(by_name[spec["parent"]]["tail"], "parent tail")
            if _distance(_vector_tuple(spec["head"], "connected head"), parent_tail) > 1e-6:
                raise ValueError(f"Connected bone '{spec['name']}' head must equal parent '{spec['parent']}' tail")


def _enter_armature_edit(obj):
    set_active(obj)
    result = bpy.ops.object.mode_set(mode="EDIT")
    if isinstance(result, (set, frozenset)) and "FINISHED" not in result:
        raise RuntimeError(f"Could not enter Edit Mode for armature '{obj.name}': {result}")


def _exit_object_mode():
    if bpy.context.mode != "OBJECT":
        result = bpy.ops.object.mode_set(mode="OBJECT")
        if isinstance(result, (set, frozenset)) and "FINISHED" not in result:
            raise RuntimeError(f"Could not return to Object Mode: {result}")


def _set_edit_bone_fields(edit_bone, spec):
    edit_bone.head = _vector(spec["head"], f"bone '{edit_bone.name}' head")
    edit_bone.tail = _vector(spec["tail"], f"bone '{edit_bone.name}' tail")
    for field in (
        "roll",
        "use_connect",
        "use_deform",
        "inherit_scale",
        "envelope_distance",
        "envelope_weight",
        "head_radius",
        "tail_radius",
    ):
        if field in spec:
            setattr(edit_bone, field, spec[field])


@contextlib.contextmanager
def _working_armature_data(armature_obj):
    """Edit a private copy, atomically swapping every user only after the body succeeds."""
    original = armature_obj.data
    if not getattr(original, "is_editable", True):
        raise ValueError(f"Armature data '{original.name}' is linked or otherwise not editable")
    original_name = original.name
    users = [obj for obj in bpy.data.objects if obj.data == original]
    working = original.copy()
    working.name = f"{original_name}.MCP Working"
    for obj in users:
        obj.data = working
    try:
        yield working, users
    except Exception:
        for obj in users:
            obj.data = original
        bpy.data.armatures.remove(working, do_unlink=True)
        raise
    else:
        bpy.data.armatures.remove(original, do_unlink=True)
        working.name = original_name


@contextlib.contextmanager
def _working_armature_with_references(armature_obj, operations):
    """Apply final reference outcomes around one atomic rest-data edit."""
    reference_outcomes = {bone.name: bone.name for bone in armature_obj.data.bones}
    for operation in operations:
        if operation["operation"] == "RENAME" and operation["reference_policy"] == "UPDATE":
            old_name = operation["bone_name"]
            for original_name, current_name in reference_outcomes.items():
                if current_name == old_name:
                    reference_outcomes[original_name] = operation["new_name"]
        elif operation["operation"] == "DELETE" and operation["reference_policy"] == "REMOVE_REFERENCES":
            deleted_name = operation["bone_name"]
            for original_name, current_name in reference_outcomes.items():
                if current_name == deleted_name:
                    reference_outcomes[original_name] = None
    reference_outcomes = {
        original_name: final_name
        for original_name, final_name in reference_outcomes.items()
        if final_name != original_name
    }
    with _bone_reference_transaction(armature_obj, reference_outcomes):
        affected = []
        for original_name, final_name in reference_outcomes.items():
            if final_name is None:
                affected.extend(_remove_bone_references(armature_obj, original_name))
        with _working_armature_data(armature_obj) as working:
            armature_data, users = working
            yield armature_data, users, affected
            for original_name, final_name in reference_outcomes.items():
                if final_name is not None:
                    affected.extend(_rename_references(armature_obj, original_name, final_name))


def _edit_bone_specs(armature_obj):
    specs = []
    with preserve_mode_and_selection():
        _enter_armature_edit(armature_obj)
        try:
            for bone in armature_obj.data.edit_bones:
                specs.append(
                    {
                        "name": bone.name,
                        "head": list(bone.head),
                        "tail": list(bone.tail),
                        "roll": float(bone.roll),
                        "parent": getattr(bone.parent, "name", None),
                        "use_connect": bool(bone.use_connect),
                        "use_deform": bool(bone.use_deform),
                        "inherit_scale": bone.inherit_scale,
                        "envelope_distance": float(bone.envelope_distance),
                        "envelope_weight": float(bone.envelope_weight),
                        "head_radius": float(bone.head_radius),
                        "tail_radius": float(bone.tail_radius),
                        "collections": [collection.name for collection in bone.collections],
                    }
                )
        finally:
            _exit_object_mode()
    return specs


def _apply_patch_to_specs(specs, operations):
    final = {spec["name"]: dict(spec) for spec in specs}
    rename_map = {}
    deleted = []
    for operation in operations:
        kind = operation["operation"]
        if kind == "CREATE":
            name = operation["name"]
            if name in final:
                raise ValueError(f"Bone already exists: {name}")
            final[name] = {key: value for key, value in operation.items() if key != "operation"}
        elif kind == "RENAME":
            old = operation["bone_name"]
            new = operation["new_name"]
            if old not in final:
                raise ValueError(f"Bone not found: {old}")
            if new in final:
                raise ValueError(f"Bone name collision: {new}")
            spec = final.pop(old)
            spec["name"] = new
            final[new] = spec
            for child in final.values():
                if child.get("parent") == old:
                    child["parent"] = new
            rename_map[old] = new
        elif kind == "UPDATE":
            name = operation["bone_name"]
            if name not in final:
                raise ValueError(f"Bone not found: {name}")
            spec = final[name]
            if operation.get("clear_parent"):
                spec["parent"] = None
            for field in (
                "head",
                "tail",
                "roll",
                "parent",
                "use_connect",
                "use_deform",
                "inherit_scale",
                "envelope_distance",
                "envelope_weight",
                "head_radius",
                "tail_radius",
            ):
                if field in operation:
                    spec[field] = operation[field]
        elif kind == "DELETE":
            name = operation["bone_name"]
            if name not in final:
                raise ValueError(f"Bone not found: {name}")
            children = [child["name"] for child in final.values() if child.get("parent") == name]
            future_reparents = {
                item["bone_name"]
                for item in operations
                if item["operation"] == "UPDATE" and (item.get("clear_parent") or "parent" in item)
            }
            requested_deletions = {item["bone_name"] for item in operations if item["operation"] == "DELETE"}
            undealt = sorted(set(children) - future_reparents - requested_deletions)
            if undealt:
                raise ValueError(
                    f"Deleting bone '{name}' requires explicitly reparenting or deleting its children: "
                    f"{', '.join(undealt)}"
                )
            del final[name]
            deleted.append(name)
        else:
            raise ValueError(f"Unsupported bone operation: {kind}")
    return list(final.values()), rename_map, deleted


def _resolve_renamed(name, rename_map):
    seen = set()
    while name in rename_map and name not in seen:
        seen.add(name)
        name = rename_map[name]
    return name


class ArmatureStructureHandlersMixin:
    """Create armatures and edit their bones."""

    def create_armature(self, name, collection_name, bones=None, world_transform=None, display=None):
        _required_name(name, "name")
        collection = _ensure_object_collection(collection_name)
        if bpy.data.objects.get(name) is not None or bpy.data.armatures.get(name) is not None:
            raise ValueError(f"Armature object or datablock already exists: {name}")
        bones = list(bones or ())
        display = dict(display or {})
        world_transform = dict(world_transform or {})
        requested_collections = {collection_name for spec in bones for collection_name in spec.get("collections", ())}
        # Initial bone collection names belong to the new armature and are created by this request.
        collection_names = requested_collections
        _validate_bone_specs(bones, collection_names)
        location = _vector(world_transform.get("location", (0, 0, 0)), "world_transform.location")
        scale = _vector(world_transform.get("scale", (1, 1, 1)), "world_transform.scale")
        if any(abs(value) <= 1e-12 for value in scale):
            raise ValueError("world_transform.scale components must be non-zero")
        quaternion_values = world_transform.get("rotation_quaternion", (1, 0, 0, 0))
        if len(quaternion_values) != 4:
            raise ValueError("world_transform.rotation_quaternion must contain four values [w, x, y, z]")
        quaternion_values = tuple(_finite(value, "rotation_quaternion") for value in quaternion_values)
        if sum(value * value for value in quaternion_values) <= 1e-16:
            raise ValueError("world_transform.rotation_quaternion must be non-zero")
        quaternion = mathutils.Quaternion(quaternion_values)
        quaternion.normalize()

        armature_data = bpy.data.armatures.new(name)
        armature_obj = bpy.data.objects.new(name, armature_data)
        try:
            collection.objects.link(armature_obj)
            armature_obj.matrix_world = mathutils.Matrix.LocRotScale(location, quaternion, scale)
            armature_data.pose_position = display.get("pose_position", "POSE")
            armature_data.display_type = display.get("display_type", "OCTAHEDRAL")
            armature_data.show_axes = bool(display.get("show_axes", False))
            armature_data.show_names = bool(display.get("show_names", True))
            armature_data.axes_position = _finite(display.get("axes_position", 0.0), "axes_position")
            armature_data.relation_line_position = display.get("relation_line_position", "TAIL")
            armature_data.show_bone_custom_shapes = bool(display.get("show_bone_custom_shapes", True))
            armature_data.show_bone_colors = bool(display.get("show_bone_colors", True))
            armature_obj.show_in_front = bool(display.get("show_in_front", True))
            for bone_collection_name in sorted(requested_collections):
                armature_data.collections.new(bone_collection_name)
            if bones:
                with preserve_mode_and_selection():
                    _enter_armature_edit(armature_obj)
                    try:
                        created = {}
                        for spec in bones:
                            edit_bone = armature_data.edit_bones.new(spec["name"])
                            _set_edit_bone_fields(edit_bone, spec)
                            created[spec["name"]] = edit_bone
                        for spec in bones:
                            edit_bone = created[spec["name"]]
                            parent_name = spec.get("parent")
                            if parent_name is not None:
                                edit_bone.parent = created[parent_name]
                                edit_bone.use_connect = bool(spec.get("use_connect", False))
                            for bone_collection_name in spec.get("collections", ()):
                                armature_data.collections_all[bone_collection_name].assign(edit_bone)
                    finally:
                        _exit_object_mode()
        except Exception:
            if bpy.data.objects.get(armature_obj.name) is armature_obj:
                bpy.data.objects.remove(armature_obj, do_unlink=True)
            if bpy.data.armatures.get(armature_data.name) is armature_data:
                bpy.data.armatures.remove(armature_data, do_unlink=True)
            raise
        return {
            "armature_object": armature_obj.name,
            "armature_data": armature_data.name,
            "collection": collection.name,
            "bones": [bone.name for bone in armature_data.bones],
            "bone_collections": [item.name for item in armature_data.collections_all],
            "transforms": _transform_info(armature_obj),
            "changed_objects": [armature_obj.name],
            "changed_resources": [armature_data.name],
        }

    def patch_armature_bones(self, armature_object_name, operations, confirm_animated_rest_changes=False):
        armature_obj = _armature_object(armature_object_name)
        operations = list(operations or ())
        if not operations:
            raise ValueError("At least one bone operation is required")
        if len(operations) > 1_000:
            raise ValueError("At most 1000 bone operations are allowed")
        if _has_animation(armature_obj) and not confirm_animated_rest_changes:
            raise ValueError("confirm_animated_rest_changes=True is required because this rig has animation")
        existing_specs = _edit_bone_specs(armature_obj)
        final_specs, rename_map, deleted = _apply_patch_to_specs(existing_specs, operations)
        _validate_bone_specs(final_specs, {collection.name for collection in armature_obj.data.collections_all})
        final_names = {spec["name"] for spec in final_specs}
        for operation in operations:
            alignment_name = operation.get("align_orientation_bone")
            if alignment_name is not None and _resolve_renamed(alignment_name, rename_map) not in final_names:
                raise ValueError(f"Alignment bone not found in final hierarchy: {alignment_name}")
        dependencies = {}
        for operation in operations:
            if operation["operation"] not in {"RENAME", "DELETE"}:
                continue
            name = operation["bone_name"]
            found = _bone_dependencies(armature_obj, name)
            dependencies[name] = found
            policy = operation["reference_policy"]
            if found and policy == "ERROR":
                raise ValueError(f"Bone '{name}' has references; use an explicit update/removal policy: {found[:10]}")
        # Bone names are evaluated in request order, which also makes chained renames deterministic.
        changed_users = []
        with _working_armature_with_references(armature_obj, operations) as (
            armature_data,
            users,
            affected_dependencies,
        ):
            changed_users = [obj.name for obj in users]
            with preserve_mode_and_selection():
                _enter_armature_edit(armature_obj)
                try:
                    for operation in operations:
                        kind = operation["operation"]
                        if kind == "CREATE":
                            edit_bone = armature_data.edit_bones.new(operation["name"])
                            _set_edit_bone_fields(edit_bone, operation)
                        elif kind == "RENAME":
                            armature_data.edit_bones[operation["bone_name"]].name = operation["new_name"]
                        elif kind == "UPDATE":
                            edit_bone = armature_data.edit_bones[operation["bone_name"]]
                            for field in (
                                "head",
                                "tail",
                                "roll",
                                "use_connect",
                                "use_deform",
                                "inherit_scale",
                                "envelope_distance",
                                "envelope_weight",
                                "head_radius",
                                "tail_radius",
                            ):
                                if field not in operation:
                                    continue
                                value = operation[field]
                                if field in {"head", "tail"}:
                                    value = _vector(value, f"{edit_bone.name}.{field}")
                                setattr(edit_bone, field, value)
                        elif kind == "DELETE":
                            armature_data.edit_bones.remove(armature_data.edit_bones[operation["bone_name"]])
                    for spec in final_specs:
                        edit_bone = armature_data.edit_bones[spec["name"]]
                        parent_name = spec.get("parent")
                        edit_bone.parent = armature_data.edit_bones.get(parent_name) if parent_name else None
                        edit_bone.use_connect = bool(spec.get("use_connect", False) and parent_name)
                    for operation in operations:
                        if operation["operation"] != "UPDATE":
                            continue
                        edit_bone = armature_data.edit_bones[_resolve_renamed(operation["bone_name"], rename_map)]
                        if "align_roll_vector" in operation:
                            vector = _vector(operation["align_roll_vector"], "align_roll_vector")
                            if vector.length <= 1e-8:
                                raise ValueError("align_roll_vector must be non-zero")
                            edit_bone.align_roll(vector)
                        if "align_orientation_bone" in operation:
                            alignment_name = _resolve_renamed(operation["align_orientation_bone"], rename_map)
                            edit_bone.align_orientation(armature_data.edit_bones[alignment_name])
                    for operation in operations:
                        if operation["operation"] != "CREATE":
                            continue
                        edit_bone = armature_data.edit_bones[_resolve_renamed(operation["name"], rename_map)]
                        for collection_name in operation.get("collections", ()):
                            armature_data.collections_all[collection_name].assign(edit_bone)
                finally:
                    _exit_object_mode()
        changed_objects = set(changed_users)
        changed_objects.update(
            record["object"] for record in affected_dependencies if isinstance(record.get("object"), str)
        )
        return {
            "armature_object": armature_obj.name,
            "armature_data": armature_obj.data.name,
            "operations_applied": len(operations),
            "bone_names": [bone.name for bone in armature_obj.data.bones],
            "renamed_bones": rename_map,
            "deleted_bones": deleted,
            "dependencies_before": dependencies,
            "affected_dependencies": affected_dependencies,
            "data_users_changed": changed_users,
            "changed_objects": sorted(changed_objects),
            "changed_resources": [armature_obj.data.name],
            "warnings": ["Rest-pose edits can invalidate authored deformation and animation."]
            if _has_animation(armature_obj)
            else [],
        }

    def mirror_armature_bones(
        self,
        armature_object_name,
        bone_names,
        axis="X",
        source_token=".L",
        target_token=".R",
        mirror_constraints=False,
    ):
        armature_obj = _armature_object(armature_object_name)
        names = list(bone_names or ())
        if not names:
            raise ValueError("At least one source bone is required")
        _unique_names(names, "source bone names")
        if axis not in {"X", "Y", "Z"}:
            raise ValueError("axis must be X, Y, or Z")
        if not source_token or source_token == target_token:
            raise ValueError("source_token must be non-empty and differ from target_token")
        source_bones = {}
        name_map = {}
        for name in names:
            bone = armature_obj.data.bones.get(name)
            if bone is None:
                raise ValueError(f"Bone not found: {name}")
            if source_token not in name:
                raise ValueError(f"Bone '{name}' does not contain source token '{source_token}'")
            target_name = name.replace(source_token, target_token)
            if target_name == name or armature_obj.data.bones.get(target_name) is not None:
                raise ValueError(f"Mirrored bone name collides or is unchanged: {target_name}")
            source_bones[name] = bone
            name_map[name] = target_name
        _unique_names(list(name_map.values()), "mirrored bone names")
        ambiguous = []
        axis_index = {"X": 0, "Y": 1, "Z": 2}[axis]
        for name, bone in source_bones.items():
            if abs(bone.head_local[axis_index]) <= 1e-7 and abs(bone.tail_local[axis_index]) <= 1e-7:
                ambiguous.append(name)
        changed_users = []
        with _working_armature_data(armature_obj) as (armature_data, users):
            changed_users = [obj.name for obj in users]
            with preserve_mode_and_selection():
                _enter_armature_edit(armature_obj)
                try:
                    created = {}
                    # Parent sources are created first regardless of the caller's order.
                    pending = set(names)
                    while pending:
                        progressed = False
                        for name in list(pending):
                            source = armature_data.edit_bones[name]
                            if source.parent and source.parent.name in pending:
                                continue
                            target = armature_data.edit_bones.new(name_map[name])
                            target.head = source.head.copy()
                            target.tail = source.tail.copy()
                            target.head[axis_index] *= -1
                            target.tail[axis_index] *= -1
                            reflected_z = source.z_axis.copy()
                            reflected_z[axis_index] *= -1
                            target.align_roll(reflected_z)
                            for field in _EDIT_BONE_COPY_FIELDS:
                                if field not in {"roll", "use_connect"} and hasattr(source, field):
                                    setattr(target, field, getattr(source, field))
                            if source.parent is not None:
                                target.parent = created.get(
                                    source.parent.name,
                                    armature_data.edit_bones.get(name_map.get(source.parent.name, "")),
                                )
                                if target.parent is None:
                                    target.parent = source.parent
                                target.use_connect = bool(source.use_connect and source.parent.name in name_map)
                            for collection in source.collections:
                                armature_data.collections_all[collection.name].assign(target)
                            created[name] = target
                            pending.remove(name)
                            progressed = True
                        if not progressed:
                            raise RuntimeError("Could not resolve mirrored bone parent order")
                finally:
                    _exit_object_mode()
            mirrored_constraints = []
            if mirror_constraints:
                for source_name, target_name in name_map.items():
                    source_pose = armature_obj.pose.bones[source_name]
                    target_pose = armature_obj.pose.bones[target_name]
                    for constraint in source_pose.constraints:
                        copied = _copy_pose_constraint(constraint, target_pose, armature_obj, name_map)
                        mirrored_constraints.append({"bone": target_name, "constraint": copied.name})
        return {
            "armature_object": armature_obj.name,
            "axis": axis,
            "source_to_target": name_map,
            "mirrored_constraints": mirrored_constraints,
            "centerline_ambiguities": ambiguous,
            "data_users_changed": changed_users,
            "changed_objects": changed_users,
            "changed_resources": [armature_obj.data.name],
            "warnings": [f"Source bones on the {axis} center plane produce overlapping mirrored geometry: {ambiguous}"]
            if ambiguous
            else [],
        }

    def manage_bone_collections(self, armature_object_name, operations):
        armature_obj = _armature_object(armature_object_name)
        operations = list(operations or ())
        if not operations:
            raise ValueError("At least one collection operation is required")
        # Simulate names and parents before the first mutation.
        names = {collection.name for collection in armature_obj.data.collections_all}
        parents = {
            collection.name: getattr(collection.parent, "name", None)
            for collection in armature_obj.data.collections_all
        }
        for operation in operations:
            kind = operation["operation"]
            name = operation["name"]
            if kind == "CREATE":
                if name in names and operation.get("existing_policy", "ERROR") == "ERROR":
                    raise ValueError(f"Bone collection already exists: {name}")
                names.add(name)
                parents.setdefault(name, operation.get("parent"))
            elif name not in names:
                raise ValueError(f"Bone collection not found: {name}")
            if kind == "RENAME":
                new_name = operation["new_name"]
                if new_name in names:
                    raise ValueError(f"Bone collection name collision: {new_name}")
                names.remove(name)
                names.add(new_name)
                parents[new_name] = parents.pop(name)
                parents = {key: new_name if value == name else value for key, value in parents.items()}
            elif kind == "CONFIGURE":
                if operation.get("clear_parent"):
                    parents[name] = None
                elif "parent" in operation:
                    parents[name] = operation["parent"]
            elif kind == "REMOVE":
                if not operation.get("confirm_destructive"):
                    raise ValueError(f"confirm_destructive=True is required to remove collection '{name}'")
                removed_parent = parents[name]
                names.remove(name)
                parents.pop(name, None)
                parents = {key: removed_parent if value == name else value for key, value in parents.items()}
            if kind in {"ASSIGN", "UNASSIGN"}:
                if operation.get("replace_memberships") and not operation.get("confirm_destructive"):
                    raise ValueError("confirm_destructive=True is required when replace_memberships=True")
                if kind == "UNASSIGN" and not operation.get("confirm_destructive"):
                    raise ValueError("confirm_destructive=True is required to unassign bone memberships")
                missing_bones = [bone for bone in operation["bone_names"] if armature_obj.data.bones.get(bone) is None]
                if missing_bones:
                    raise ValueError(f"Unknown bones for collection '{name}': {missing_bones}")
        for name, parent in parents.items():
            if parent is not None and parent not in names:
                raise ValueError(f"Collection '{name}' references missing parent '{parent}'")
        cycles = _hierarchy_cycles(parents)
        if cycles:
            raise ValueError(f"Bone collection hierarchy contains a cycle: {' -> '.join(cycles[0])}")
        displaced = []
        changed_users = []
        with _working_armature_data(armature_obj) as (armature_data, users):
            changed_users = [obj.name for obj in users]
            for operation in operations:
                if operation["operation"] == "CREATE" and armature_data.collections_all.get(operation["name"]) is None:
                    armature_data.collections.new(operation["name"])
            for operation in operations:
                kind = operation["operation"]
                name = operation["name"]
                collection = armature_data.collections_all.get(name)
                if kind == "CREATE":
                    parent_name = operation.get("parent")
                    collection.parent = armature_data.collections_all.get(parent_name) if parent_name else None
                    collection.is_visible = bool(operation.get("is_visible", True))
                    collection.is_solo = bool(operation.get("is_solo", False))
                elif kind == "RENAME":
                    collection.name = operation["new_name"]
                elif kind == "CONFIGURE":
                    if operation.get("clear_parent"):
                        collection.parent = None
                    elif "parent" in operation:
                        collection.parent = armature_data.collections_all[operation["parent"]]
                    for field in ("is_visible", "is_solo"):
                        if field in operation:
                            setattr(collection, field, operation[field])
                    if "position" in operation:
                        siblings = [
                            item
                            for item in armature_data.collections_all
                            if item.parent == collection.parent and item != collection
                        ]
                        destination_position = min(operation["position"], len(siblings))
                        if siblings:
                            destination = (
                                siblings[destination_position].index
                                if destination_position < len(siblings)
                                else siblings[-1].index
                            )
                            armature_data.collections.move(collection.index, destination)
                elif kind in {"ASSIGN", "UNASSIGN"}:
                    for bone_name in operation["bone_names"]:
                        bone = armature_data.bones[bone_name]
                        if kind == "ASSIGN" and operation.get("replace_memberships"):
                            for prior in list(bone.collections):
                                prior.unassign(bone)
                                displaced.append({"bone": bone_name, "collection": prior.name})
                        (collection.assign if kind == "ASSIGN" else collection.unassign)(bone)
                elif kind == "REMOVE":
                    displaced.extend({"bone": bone.name, "collection": name} for bone in collection.bones)
                    armature_data.collections.remove(collection)
        return {
            "armature_object": armature_obj.name,
            "collections": [_bone_collection_info(item) for item in armature_obj.data.collections_all],
            "displaced_memberships": displaced,
            "data_users_changed": changed_users,
            "changed_objects": changed_users,
            "changed_resources": [armature_obj.data.name],
        }

    def configure_armature_bones(self, armature_object_name, bone_patches=None, pose_bone_patches=None):
        armature_obj = _armature_object(armature_object_name)
        bone_patches = list(bone_patches or ())
        pose_bone_patches = list(pose_bone_patches or ())
        if not bone_patches and not pose_bone_patches:
            raise ValueError("At least one bone or pose-bone patch is required")
        _unique_names([item["bone_name"] for item in bone_patches], "bone patch targets")
        _unique_names([item["bone_name"] for item in pose_bone_patches], "pose-bone patch targets")
        for patch in [*bone_patches, *pose_bone_patches]:
            if armature_obj.data.bones.get(patch["bone_name"]) is None:
                raise ValueError(f"Bone not found: {patch['bone_name']}")
        for patch in pose_bone_patches:
            for axis in "xyz":
                minimum = patch.get(f"ik_min_{axis}")
                maximum = patch.get(f"ik_max_{axis}")
                if minimum is not None and maximum is not None and minimum > maximum:
                    raise ValueError(f"ik_min_{axis} must not exceed ik_max_{axis} on '{patch['bone_name']}'")
            for field in ("ik_stiffness_x", "ik_stiffness_y", "ik_stiffness_z"):
                if field in patch and not 0 <= _finite(patch[field], field) <= 0.99:
                    raise ValueError(f"{field} must be in [0, 0.99]")
        old_values = []
        try:
            for patch in bone_patches:
                bone = armature_obj.data.bones[patch["bone_name"]]
                for field in _BONE_DATA_FIELDS:
                    if field in patch:
                        old_values.append((bone, field, getattr(bone, field)))
                        setattr(bone, field, patch[field])
            for patch in pose_bone_patches:
                pose_bone = armature_obj.pose.bones[patch["bone_name"]]
                for field in _POSE_FIELDS:
                    if field in patch:
                        old = getattr(pose_bone, field)
                        old_values.append((pose_bone, field, old.copy() if hasattr(old, "copy") else old))
                        setattr(pose_bone, field, patch[field])
                if "custom_properties" in patch:
                    for key, value in patch["custom_properties"].items():
                        prior = pose_bone.get(key, None)
                        existed = key in pose_bone
                        old_values.append((pose_bone, ("custom_property", key, existed), prior))
                        pose_bone[key] = value
        except Exception:
            for owner, field, value in reversed(old_values):
                if isinstance(field, tuple):
                    _, key, existed = field
                    if existed:
                        owner[key] = value
                    elif key in owner:
                        del owner[key]
                else:
                    setattr(owner, field, value)
            raise
        changes = []
        for owner, field, old in old_values:
            if isinstance(field, tuple):
                key = field[1]
                new = owner[key]
                field_name = f"custom_properties.{key}"
            else:
                new = getattr(owner, field)
                field_name = field
            changes.append({"bone": owner.name, "field": field_name, "old": _plain(old), "new": _plain(new)})
        warning = _override_property_warning(
            armature_obj,
            [patch["bone_name"] for patch in pose_bone_patches if patch.get("custom_properties")],
        )
        return {
            "armature_object": armature_obj.name,
            "changes": changes,
            "warnings": [warning] if warning else [],
            "changed_objects": [armature_obj.name],
            "changed_resources": [armature_obj.data.name] if bone_patches else [],
        }
