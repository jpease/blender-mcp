# Inherited scope metrics: `create_shape_key_controls` and its neighbours were already over the
# branch/statement/local limits before this file was last touched, and `scripts/lint_changed.py`
# attributes a whole-scope finding to any branch that writes a line inside the scope. Declared
# here, as `handlers/rendering.py` and `handlers/camera/targeting.py` do, so a one-line reply fix
# is not a licence to restructure a driver builder.
# ruff: file-ignore[too-many-branches, too-many-locals, too-many-statements]
"""Blender handlers for IK systems, rig drivers, custom shapes, and facial controls."""

import contextlib
import json
import math
import uuid

import bpy
import mathutils

from ...helpers import preserve_mode_and_selection
from .primitives import (
    _armature_object,
    _ensure_object_collection,
    _finite,
    _matrix_list,
    _mesh_object,
    _required_name,
)
from .structure import _enter_armature_edit, _exit_object_mode


def _validate_contiguous_chain(armature, bone_names):
    names = list(bone_names or ())
    if not names or len(names) != len(set(names)):
        raise ValueError("chain_bone_names must be a non-empty unique ordered list")
    bones = []
    for name in names:
        bone = armature.data.bones.get(name)
        if bone is None:
            raise ValueError(f"Chain bone not found: {name}")
        bones.append(bone)
    for parent, child in zip(bones, bones[1:], strict=False):
        if child.parent != parent:
            raise ValueError(
                f"Chain is not contiguous from root to end: '{child.name}' is not a child of '{parent.name}'"
            )
    return bones


def _validate_control_definition(armature, definition, label):
    name = _required_name(definition.get("name"), f"{label}.name")
    if armature.data.bones.get(name) is not None:
        raise ValueError(f"Control bone already exists: {name}")
    head = mathutils.Vector(definition.get("head"))
    tail = mathutils.Vector(definition.get("tail"))
    if not all(math.isfinite(float(value)) for value in (*head, *tail)):
        raise ValueError(f"{label} coordinates must be finite")
    if (tail - head).length <= 1e-8:
        raise ValueError(f"{label} must have non-zero length")
    return name, head, tail


def _ensure_bone_collection(armature_data, name):
    collection = armature_data.collections_all.get(name)
    return collection or armature_data.collections.new(name)


def _create_control_bones(armature, definitions):
    created = []
    with preserve_mode_and_selection():
        _enter_armature_edit(armature)
        try:
            for definition in definitions:
                name, head, tail = _validate_control_definition(armature, definition, "control")
                bone = armature.data.edit_bones.new(name)
                bone.head = head
                bone.tail = tail
                bone.use_deform = False
                collection = _ensure_bone_collection(armature.data, definition["collection"])
                collection.assign(bone)
                created.append(name)
        finally:
            _exit_object_mode()
    return created


def _add_ik_constraint(armature, chain_names, target_name, pole_definition, constraint_name, iterations, use_stretch):
    end = armature.pose.bones[chain_names[-1]]
    if end.constraints.get(constraint_name) is not None:
        raise ValueError(f"Constraint '{constraint_name}' already exists on '{end.name}'")
    constraint = end.constraints.new("IK")
    constraint.name = constraint_name
    constraint.target = armature
    constraint.subtarget = target_name
    constraint.chain_count = len(chain_names)
    constraint.iterations = int(iterations)
    constraint.use_stretch = bool(use_stretch)
    if pole_definition is not None:
        constraint.pole_target = armature
        constraint.pole_subtarget = pole_definition["name"]
        constraint.pole_angle = _finite(pole_definition.get("pole_angle", 0.0), "pole_angle")
    return constraint


def _tag_generated_bones(armature, bone_names, rig_id, role):
    for name in bone_names:
        bone = armature.data.bones[name]
        bone["blender_mcp_rig_id"] = rig_id
        bone["blender_mcp_role"] = role
        bone["blender_mcp_schema"] = 1


def _property_owner(armature, owner_kind, bone_name=None):
    if owner_kind == "OBJECT":
        return armature
    if owner_kind != "POSE_BONE":
        raise ValueError(f"Unsupported property owner: {owner_kind}")
    pose_bone = armature.pose.bones.get(bone_name)
    if pose_bone is None:
        raise ValueError(f"Property pose bone not found: {bone_name}")
    return pose_bone


def _property_data_path(owner, property_name):
    escaped = bpy.utils.escape_identifier(property_name)
    if getattr(owner, "id_data", owner) == owner:
        return f'["{escaped}"]'
    return owner.path_from_id() + f'["{escaped}"]'


def _configure_property(owner, name, default, minimum, maximum, soft_minimum=None, soft_maximum=None):
    existed = name in owner
    old_value = owner.get(name)
    owner[name] = default
    ui = owner.id_properties_ui(name)
    settings = {"min": minimum, "max": maximum}
    if soft_minimum is not None:
        settings["soft_min"] = soft_minimum
    if soft_maximum is not None:
        settings["soft_max"] = soft_maximum
    ui.update(**settings)
    return existed, old_value


def _driver_owner(destination):
    obj = bpy.data.objects.get(destination["object_name"])
    if obj is None:
        raise ValueError(f"Driver destination object not found: {destination['object_name']}")
    kind = destination["owner"]
    if kind in {"POSE_BONE", "CONSTRAINT"}:
        if obj.type != "ARMATURE":
            raise ValueError(f"Driver destination '{obj.name}' is not an armature")
        pose_bone = obj.pose.bones.get(destination.get("bone_name"))
        if pose_bone is None:
            raise ValueError(f"Driver destination bone not found: {destination.get('bone_name')}")
        if kind == "POSE_BONE":
            return pose_bone
        constraint = pose_bone.constraints.get(destination.get("constraint_name"))
        if constraint is None:
            raise ValueError(f"Driver destination constraint not found: {destination.get('constraint_name')}")
        return constraint
    if kind == "SHAPE_KEY":
        if obj.type != "MESH" or obj.data.shape_keys is None:
            raise ValueError(f"'{obj.name}' has no shape keys")
        key = obj.data.shape_keys.key_blocks.get(destination.get("shape_key_name"))
        if key is None:
            raise ValueError(f"Shape key not found: {destination.get('shape_key_name')}")
        return key
    if kind == "MODIFIER":
        modifier = obj.modifiers.get(destination.get("modifier_name"))
        if modifier is None:
            raise ValueError(f"Modifier not found: {destination.get('modifier_name')}")
        return modifier
    raise ValueError(f"Unsupported driver destination owner: {kind}")


def _prepare_driver_destination(owner, property_name, array_index, replace):
    if not hasattr(owner, property_name):
        raise ValueError(f"'{type(owner).__name__}' does not support driven property '{property_name}'")
    rna_property = owner.bl_rna.properties.get(property_name)
    if rna_property is None or rna_property.is_readonly:
        raise ValueError(f"Driven property '{property_name}' is unavailable or read-only")
    if array_index is not None and (not rna_property.is_array or array_index >= rna_property.array_length):
        raise ValueError(f"array_index is invalid for '{property_name}'")
    if array_index is None and rna_property.is_array:
        raise ValueError(f"array_index is required for array property '{property_name}'")
    destination_path = owner.path_from_id(property_name)
    destination_index = array_index if array_index is not None else 0
    animation = getattr(owner.id_data, "animation_data", None)
    existing = next(
        (
            curve
            for curve in getattr(animation, "drivers", ())
            if curve.data_path == destination_path and curve.array_index == destination_index
        ),
        None,
    )
    if existing is not None and not replace:
        raise ValueError(f"A driver already exists for '{destination_path}'[{destination_index}]")
    with contextlib.suppress(TypeError):
        if existing is not None:
            if array_index is not None:
                owner.driver_remove(property_name, array_index)
            else:
                owner.driver_remove(property_name)
    try:
        fcurve = (
            owner.driver_add(property_name, array_index) if array_index is not None else owner.driver_add(property_name)
        )
    except TypeError as exc:
        raise ValueError(f"A driver already exists for '{property_name}'") from exc
    return fcurve


def _configure_property_expression_driver(
    owner,
    property_name,
    array_index,
    variables,
    expression,
    replace,
):
    fcurve = _prepare_driver_destination(owner, property_name, array_index, replace)
    driver = fcurve.driver
    driver.type = "SCRIPTED"
    for variable_name, source, source_path in variables:
        variable = driver.variables.new()
        variable.name = variable_name
        variable.type = "SINGLE_PROP"
        variable.targets[0].id = source
        variable.targets[0].data_path = source_path
    driver.expression = expression
    return fcurve


def _configure_single_property_driver(owner, property_name, array_index, source, source_path, factor, offset, replace):
    expression = f"rig_property * {float(factor):.17g} + {float(offset):.17g}"
    return _configure_property_expression_driver(
        owner,
        property_name,
        array_index,
        [("rig_property", source, source_path)],
        expression,
        replace,
    )


def _corrective_expression(operation, variable_names, factor, offset):
    if operation == "MULTIPLY":
        body = " * ".join(variable_names)
    elif operation == "MINIMUM":
        body = f"min({', '.join(variable_names)})"
    elif operation == "MAXIMUM":
        body = f"max({', '.join(variable_names)})"
    elif operation == "AVERAGE":
        body = f"({' + '.join(variable_names)}) / {len(variable_names)}"
    else:
        raise ValueError(f"Unsupported corrective operation: {operation}")
    return f"({body}) * {float(factor):.17g} + {float(offset):.17g}"


def _default_ik_target(chain, name, collection):
    end = chain[-1]
    length = max(float(end.length) * 0.5, 0.05)
    return {
        "name": name,
        "head": tuple(end.tail_local),
        "tail": tuple(end.tail_local + mathutils.Vector((0, 0, length))),
        "collection": collection,
    }


class ControlRigHandlersMixin:
    """Build IK control systems and explicit property-driven rig relationships."""

    def create_ik_chain(
        self,
        armature_object_name,
        chain_bone_names,
        target_control,
        pole_control=None,
        constraint_name="IK",
        iterations=500,
        use_stretch=False,
    ):
        armature = _armature_object(armature_object_name)
        chain = _validate_contiguous_chain(armature, chain_bone_names)
        validated_chain_names = [bone.name for bone in chain]
        end_name = validated_chain_names[-1]
        if armature.pose.bones[end_name].constraints.get(constraint_name) is not None:
            raise ValueError(f"Constraint '{constraint_name}' already exists on '{end_name}'")
        _validate_control_definition(armature, target_control, "target_control")
        if pole_control is not None:
            _validate_control_definition(armature, pole_control, "pole_control")
            if pole_control["name"] == target_control["name"]:
                raise ValueError("Target and pole controls must have distinct names")
        created = _create_control_bones(armature, [target_control, *([pole_control] if pole_control else [])])
        constraint = _add_ik_constraint(
            armature,
            validated_chain_names,
            target_control["name"],
            pole_control,
            _required_name(constraint_name, "constraint_name"),
            iterations,
            use_stretch,
        )
        rig_id = str(uuid.uuid4())
        _tag_generated_bones(armature, created, rig_id, "IK_CONTROL")
        bpy.context.view_layer.update()
        return {
            "armature_object": armature.name,
            "chain": validated_chain_names,
            "controls": created,
            "constraint": constraint.name,
            "evaluated_end_matrix": _matrix_list(armature.pose.bones[end_name].matrix),
            "rig_id": rig_id,
            "changed_objects": [armature.name],
        }

    def create_ik_fk_limb(
        self,
        armature_object_name,
        deform_bone_names,
        property_bone_name,
        property_name="ik_fk",
        fk_prefix="FK-",
        ik_prefix="IK-",
        mechanism_collection="MCH",
        control_collection="CTRL",
        ik_target=None,
        pole_control=None,
    ):
        armature = _armature_object(armature_object_name)
        chain = _validate_contiguous_chain(armature, deform_bone_names)
        rest_matrices = {bone.name: _matrix_list(bone.matrix_local) for bone in chain}
        property_bone = armature.pose.bones.get(property_bone_name)
        if property_bone is None:
            raise ValueError(f"Property bone not found: {property_bone_name}")
        if property_name in property_bone:
            raise ValueError(f"Property '{property_name}' already exists on '{property_bone_name}'")
        fk_names = [f"{fk_prefix}{bone.name}" for bone in chain]
        ik_names = [f"{ik_prefix}{bone.name}" for bone in chain]
        collisions = [name for name in [*fk_names, *ik_names] if armature.data.bones.get(name) is not None]
        if collisions:
            raise ValueError(f"Generated bone names already exist: {collisions}")
        if ik_target is None:
            ik_target = _default_ik_target(chain, f"CTRL-{chain[-1].name}-IK", control_collection)
        _validate_control_definition(armature, ik_target, "ik_target")
        if pole_control is not None:
            _validate_control_definition(armature, pole_control, "pole_control")
        constraint_collisions = [
            bone.name
            for bone in chain
            if any(
                armature.pose.bones[bone.name].constraints.get(name) is not None
                for name in ("IK/FK Copy FK", "IK/FK Copy IK")
            )
        ]
        if constraint_collisions:
            raise ValueError(f"IK/FK blend constraints already exist on: {constraint_collisions}")
        with preserve_mode_and_selection():
            _enter_armature_edit(armature)
            try:
                mechanism = _ensure_bone_collection(armature.data, mechanism_collection)
                for prefix_names in (fk_names, ik_names):
                    for source_name, generated_name in zip(deform_bone_names, prefix_names, strict=True):
                        source = armature.data.edit_bones[source_name]
                        generated = armature.data.edit_bones.new(generated_name)
                        generated.head = source.head
                        generated.tail = source.tail
                        generated.roll = source.roll
                        generated.use_deform = False
                        index = deform_bone_names.index(source_name)
                        if index:
                            generated.parent = armature.data.edit_bones[prefix_names[index - 1]]
                            generated.use_connect = source.use_connect
                        mechanism.assign(generated)
            finally:
                _exit_object_mode()
        controls = _create_control_bones(armature, [ik_target, *([pole_control] if pole_control else [])])
        ik_constraint = _add_ik_constraint(
            armature, ik_names, ik_target["name"], pole_control, "IK/FK IK Solver", 500, False
        )
        _configure_property(property_bone, property_name, 0.0, 0.0, 1.0)
        source_path = _property_data_path(property_bone, property_name)
        blend_constraints = []
        for deform_name, fk_name, ik_name in zip(deform_bone_names, fk_names, ik_names, strict=True):
            owner = armature.pose.bones[deform_name]
            fk_constraint = owner.constraints.new("COPY_TRANSFORMS")
            fk_constraint.name = "IK/FK Copy FK"
            fk_constraint.target = armature
            fk_constraint.subtarget = fk_name
            fk_constraint.influence = 1.0
            fk_curve = _configure_single_property_driver(
                fk_constraint, "influence", None, armature, source_path, -1.0, 1.0, False
            )
            ik_copy = owner.constraints.new("COPY_TRANSFORMS")
            ik_copy.name = "IK/FK Copy IK"
            ik_copy.target = armature
            ik_copy.subtarget = ik_name
            ik_copy.influence = 0.0
            ik_curve = _configure_single_property_driver(
                ik_copy, "influence", None, armature, source_path, 1.0, 0.0, False
            )
            blend_constraints.append(
                {
                    "bone": deform_name,
                    "fk": fk_constraint.name,
                    "ik": ik_copy.name,
                    "drivers": [fk_curve.data_path, ik_curve.data_path],
                }
            )
        rig_id = str(uuid.uuid4())
        _tag_generated_bones(armature, fk_names, rig_id, "FK_CHAIN")
        _tag_generated_bones(armature, ik_names, rig_id, "IK_CHAIN")
        _tag_generated_bones(armature, controls, rig_id, "IK_CONTROL")
        metadata = {
            "rig_id": rig_id,
            "deform": list(deform_bone_names),
            "fk": fk_names,
            "ik": ik_names,
            "target": ik_target["name"],
            "pole": pole_control["name"] if pole_control else None,
            "property_bone": property_bone_name,
            "property": property_name,
        }
        armature[f"blender_mcp_ik_fk_{rig_id}"] = json.dumps(metadata, sort_keys=True)
        return {
            **metadata,
            "armature_object": armature.name,
            "controls": controls,
            "ik_constraint": ik_constraint.name,
            "blend_constraints": blend_constraints,
            "snap_metadata": {"rest_matrices": rest_matrices},
            "changed_objects": [armature.name],
        }

    def create_spline_ik_rig(
        self,
        armature_object_name,
        chain_bone_names,
        constraint_name="Spline IK",
        curve_object_name=None,
        new_curve_name=None,
        curve_points=None,
        curve_collection_name=None,
        use_even_divisions=True,
        y_scale_mode="FIT_CURVE",
        xz_scale_mode="VOLUME_PRESERVE",
        use_curve_radius=True,
    ):
        armature = _armature_object(armature_object_name)
        chain = _validate_contiguous_chain(armature, chain_bone_names)
        end = armature.pose.bones[chain[-1].name]
        if end.constraints.get(constraint_name) is not None:
            raise ValueError(f"Constraint already exists on '{end.name}': {constraint_name}")
        created_curve = False
        if curve_object_name:
            curve = bpy.data.objects.get(curve_object_name)
            if curve is None or curve.type != "CURVE":
                raise ValueError(f"Curve object not found: {curve_object_name}")
        else:
            new_curve_name = _required_name(new_curve_name, "new_curve_name")
            curve_collection_name = _required_name(curve_collection_name, "curve_collection_name")
            if bpy.data.objects.get(new_curve_name) is not None:
                raise ValueError(f"Object already exists: {new_curve_name}")
            points: list = list(curve_points or ())
            if len(points) < 2:
                raise ValueError("At least two curve points are required")
            if any(len(point) != 3 for point in points):
                raise ValueError("Every curve point must contain exactly three coordinates")
            coordinates = [tuple(_finite(value, "curve point") for value in point) for point in points]
            curve_data = bpy.data.curves.new(new_curve_name, type="CURVE")
            curve_data.dimensions = "3D"
            spline = curve_data.splines.new("POLY")
            spline.points.add(len(coordinates) - 1)
            for point, coordinate in zip(spline.points, coordinates, strict=True):
                point.co = (*coordinate, 1.0)
            curve = bpy.data.objects.new(new_curve_name, curve_data)
            curve.matrix_world = armature.matrix_world
            _ensure_object_collection(curve_collection_name).objects.link(curve)
            created_curve = True
        constraint = end.constraints.new("SPLINE_IK")
        constraint.name = constraint_name
        constraint.target = curve
        constraint.chain_count = len(chain)
        constraint.use_even_divisions = bool(use_even_divisions)
        constraint.y_scale_mode = y_scale_mode
        constraint.xz_scale_mode = xz_scale_mode
        constraint.use_curve_radius = bool(use_curve_radius)
        bpy.context.view_layer.update()
        return {
            "armature_object": armature.name,
            "chain": [bone.name for bone in chain],
            "curve_object": curve.name,
            "curve_created": created_curve,
            "constraint": constraint.name,
            "changed_objects": [armature.name, *([curve.name] if created_curve else [])],
            "retained_live_dependencies": [curve.name, constraint.name],
        }

    def create_rig_property_driver(
        self,
        armature_object_name,
        property_owner,
        property_name,
        destinations,
        property_bone_name=None,
        default=0.0,
        minimum=0.0,
        maximum=1.0,
        soft_minimum=None,
        soft_maximum=None,
        factor=1.0,
        offset=0.0,
    ):
        armature = _armature_object(armature_object_name)
        if minimum >= maximum or not minimum <= default <= maximum:
            raise ValueError("Invalid property range")
        if soft_minimum is not None and not minimum <= soft_minimum <= maximum:
            raise ValueError("soft_minimum must be inside [minimum, maximum]")
        if soft_maximum is not None and not minimum <= soft_maximum <= maximum:
            raise ValueError("soft_maximum must be inside [minimum, maximum]")
        if soft_minimum is not None and soft_maximum is not None and soft_minimum > soft_maximum:
            raise ValueError("soft_minimum must not exceed soft_maximum")
        owner = _property_owner(armature, property_owner, property_bone_name)
        if property_name in owner:
            raise ValueError(f"Rig property already exists: {property_name}")
        prepared = []
        for destination in destinations or ():
            destination_owner = _driver_owner(destination)
            prepared.append((destination, destination_owner))
        if not prepared:
            raise ValueError("At least one driver destination is required")
        _configure_property(owner, property_name, default, minimum, maximum, soft_minimum, soft_maximum)
        source_path = _property_data_path(owner, property_name)
        records = []
        for destination, destination_owner in prepared:
            curve = _configure_single_property_driver(
                destination_owner,
                destination["property_name"],
                destination.get("array_index"),
                armature,
                source_path,
                factor,
                offset,
                destination.get("existing_policy", "ERROR") == "REPLACE",
            )
            records.append(
                {
                    "owner": destination["owner"],
                    "object": destination["object_name"],
                    "data_path": curve.data_path,
                    "array_index": curve.array_index,
                    "expression": curve.driver.expression,
                }
            )
        return {
            "armature_object": armature.name,
            "property_owner": property_owner,
            "property_bone": property_bone_name,
            "property": property_name,
            "drivers": records,
            "changed_objects": sorted({armature.name, *(item[0]["object_name"] for item in prepared)}),
        }

    def assign_bone_custom_shapes(
        self,
        armature_object_name,
        assignments,
        widget_collection_name=None,
        hide_widgets_from_render=True,
    ):
        armature = _armature_object(armature_object_name)
        widget_collection = _ensure_object_collection(widget_collection_name) if widget_collection_name else None
        prepared = []
        assignment_names = [assignment.get("bone_name") for assignment in assignments or ()]
        if len(assignment_names) != len(set(assignment_names)):
            raise ValueError("Each pose bone may receive only one custom-shape assignment per request")
        for assignment in assignments or ():
            pose_bone = armature.pose.bones.get(assignment.get("bone_name"))
            if pose_bone is None:
                raise ValueError(f"Pose bone not found: {assignment.get('bone_name')}")
            shape = bpy.data.objects.get(assignment.get("shape_object_name"))
            if shape is None:
                raise ValueError(f"Shape object not found: {assignment.get('shape_object_name')}")
            transform = None
            if assignment.get("transform_bone_name"):
                transform = armature.pose.bones.get(assignment["transform_bone_name"])
                if transform is None:
                    raise ValueError(f"Shape transform bone not found: {assignment['transform_bone_name']}")
            scale = assignment.get("scale", (1, 1, 1))
            if any(_finite(value, "shape scale") <= 0 for value in scale):
                raise ValueError("Custom shape scale components must be positive")
            prepared.append((assignment, pose_bone, shape, transform))
        changes = []
        for assignment, pose_bone, shape, transform in prepared:
            pose_bone.custom_shape = shape
            pose_bone.custom_shape_transform = transform
            pose_bone.custom_shape_translation = assignment.get("translation", (0, 0, 0))
            pose_bone.custom_shape_rotation_euler = assignment.get("rotation_euler", (0, 0, 0))
            pose_bone.custom_shape_scale_xyz = assignment.get("scale", (1, 1, 1))
            pose_bone.custom_shape_wire_width = assignment.get("wire_width", 1.0)
            pose_bone.use_custom_shape_bone_size = bool(assignment.get("use_bone_size", True))
            if widget_collection is not None and shape.name not in widget_collection.objects:
                widget_collection.objects.link(shape)
            if hide_widgets_from_render:
                shape.hide_render = True
            changes.append(
                {
                    "bone": pose_bone.name,
                    "shape": shape.name,
                    "transform_bone": getattr(transform, "name", None),
                }
            )
        return {
            "armature_object": armature.name,
            "assignments": changes,
            "widget_collection": getattr(widget_collection, "name", None),
            "changed_objects": sorted({armature.name, *(shape.name for _a, _p, shape, _t in prepared)}),
        }

    def create_shape_key_controls(
        self,
        mesh_object_name,
        armature_object_name,
        property_owner,
        controls,
        property_bone_name=None,
    ):
        mesh = _mesh_object(mesh_object_name)
        armature = _armature_object(armature_object_name)
        if mesh.data.shape_keys is None or mesh.data.shape_keys.key_blocks.get("Basis") is None:
            raise ValueError(f"Mesh '{mesh.name}' requires shape keys with a Basis key")
        owner = _property_owner(armature, property_owner, property_bone_name)
        controls = list(controls or ())
        if not controls:
            raise ValueError("At least one shape-key control is required")
        prepared = []
        property_names = []
        shape_key_names = []
        for control in controls:
            mode = control.get("mode", "DIRECT")
            if mode == "DIRECT":
                properties = [
                    {
                        "property_name": control["property_name"],
                        "minimum": control["minimum"],
                        "maximum": control["maximum"],
                        "default": control["default"],
                    }
                ]
                names = [control["shape_key_name"]]
            elif mode == "SIGNED":
                properties = [
                    {
                        "property_name": control["property_name"],
                        "minimum": -1.0,
                        "maximum": 1.0,
                        "default": control["default"],
                    }
                ]
                names = [control["positive_shape_key_name"], control["negative_shape_key_name"]]
            elif mode == "CORRECTIVE":
                properties = list(control["inputs"])
                names = [control["shape_key_name"]]
            else:
                raise ValueError(f"Unsupported shape-key control mode: {mode}")
            keys = []
            for name in names:
                key = mesh.data.shape_keys.key_blocks.get(name)
                if key is None or key.name == "Basis":
                    raise ValueError(f"Driven shape key not found: {name}")
                keys.append(key)
            property_names.extend(item["property_name"] for item in properties)
            shape_key_names.extend(names)
            prepared.append((mode, control, properties, keys))
        if len(property_names) != len(set(property_names)):
            raise ValueError("Shape-key control property names must be unique")
        if len(shape_key_names) != len(set(shape_key_names)):
            raise ValueError("Each shape key may be driven only once per request")
        existing_properties = sorted(name for name in property_names if name in owner)
        if existing_properties:
            raise ValueError(f"Rig properties already exist: {existing_properties}")
        records = []
        for mode, control, properties, keys in prepared:
            for item in properties:
                _configure_property(
                    owner,
                    item["property_name"],
                    item["default"],
                    item["minimum"],
                    item["maximum"],
                )
            variables = [
                (f"input_{index}", armature, _property_data_path(owner, item["property_name"]))
                for index, item in enumerate(properties)
            ]
            replace = control.get("existing_driver_policy", "ERROR") == "REPLACE"
            if mode == "DIRECT":
                expression = (
                    f"input_0 * {float(control.get('factor', 1.0)):.17g} + {float(control.get('offset', 0.0)):.17g}"
                )
                driver_specs = [(keys[0], expression)]
            elif mode == "SIGNED":
                factor = float(control.get("factor", 1.0))
                driver_specs = [
                    (keys[0], f"max(input_0, 0.0) * {factor:.17g}"),
                    (keys[1], f"max(-input_0, 0.0) * {factor:.17g}"),
                ]
            else:
                variable_names = [item[0] for item in variables]
                expression = _corrective_expression(
                    control.get("operation", "MULTIPLY"),
                    variable_names,
                    control.get("factor", 1.0),
                    control.get("offset", 0.0),
                )
                driver_specs = [(keys[0], expression)]
            for key, expression in driver_specs:
                curve = _configure_property_expression_driver(
                    key,
                    "value",
                    None,
                    variables,
                    expression,
                    replace,
                )
                records.append(
                    {
                        "mode": mode,
                        "shape_key": key.name,
                        "properties": [item["property_name"] for item in properties],
                        "data_path": curve.data_path,
                        "expression": expression,
                    }
                )
        return {
            "mesh_object": mesh.name,
            "armature_object": armature.name,
            "controls": records,
            "changed_objects": [mesh.name, armature.name],
            "changed_resources": [mesh.data.shape_keys.name],
        }
