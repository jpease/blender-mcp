# ruff: file-ignore[too-many-branches, too-many-locals, undocumented-public-method]
"""Blender-side generic animation and layered Action handlers."""

import ast
import math
import re

import bpy

from .key_style import KeyStyle, style_point

_TARGET_COLLECTIONS = {
    "OBJECT": "objects",
    "SCENE": "scenes",
    "MATERIAL": "materials",
    "WORLD": "worlds",
    "CAMERA": "cameras",
    "LIGHT": "lights",
    "MESH": "meshes",
    "CURVE": "curves",
    "ARMATURE": "armatures",
    "SHAPE_KEYS": "shape_keys",
    "NODE_GROUP": "node_groups",
}
# Blender's `FModifierCycles.mode_before`/`mode_after` enum. REPEAT_OFFSET accumulates the
# curve's own start-to-end delta each repeat, which is what keeps a walking character walking
# instead of teleporting back to where the cycle started.
_CYCLE_MODES = frozenset({"NONE", "REPEAT", "REPEAT_OFFSET", "MIRROR"})
_NLA_TRACK_PROPERTIES = {"mute", "solo", "lock"}
_NLA_STRIP_PROPERTIES = {
    "frame_start",
    "frame_end",
    "action_frame_start",
    "action_frame_end",
    "blend_type",
    "extrapolation",
    "influence",
    "repeat",
    "scale",
    "mute",
}
# One edit addresses array_index 0..63, so a wider property could never be keyed whole.
_MAX_ARRAY_CHANNELS = 64
_DRIVER_TYPES = {"AVERAGE", "SUM", "SCRIPTED", "MIN", "MAX"}
_DRIVER_VARIABLE_TYPES = {"SINGLE_PROP", "TRANSFORMS"}
_DRIVER_TRANSFORM_TYPES = {
    "LOC_X",
    "LOC_Y",
    "LOC_Z",
    "ROT_X",
    "ROT_Y",
    "ROT_Z",
    "ROT_W",
    "SCALE_X",
    "SCALE_Y",
    "SCALE_Z",
    "SCALE_AVG",
}
_DRIVER_TRANSFORM_SPACES = {"WORLD_SPACE", "TRANSFORM_SPACE", "LOCAL_SPACE"}
_DRIVER_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SAFE_EXPRESSION_NODES = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.Name,
    # `ast.walk` yields each Name's `ctx` too, and in `mode="eval"` that is always Load: without
    # it every expression naming a variable - `frame` included - is refused.
    ast.Load,
    ast.Constant,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.UAdd,
    ast.USub,
)


def _required_name(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def _target(spec):
    if not isinstance(spec, dict):
        raise ValueError("target must be an object")
    target_type = str(spec.get("type", "")).upper()
    collection_name = _TARGET_COLLECTIONS.get(target_type)
    if collection_name is None:
        raise ValueError(f"Unsupported animation target type: {target_type}")
    name = _required_name(spec.get("name"), "target.name")
    owner = getattr(bpy.data, collection_name).get(name)
    if owner is None:
        raise ValueError(f"{target_type} animation target not found: {name}")
    return owner, target_type


def _animation_data(owner, *, create=False):
    data = owner.animation_data_create() if create else owner.animation_data
    if create and data is None:
        raise ValueError(f"Animation data is unavailable for {owner.name}")
    return data


def _action_slot(action, owner, *, create=False):
    data = owner.animation_data
    if data and data.action == action and data.action_slot is not None:
        return data.action_slot
    matching = [slot for slot in action.slots if slot.target_id_type == owner.id_type]
    named = [slot for slot in matching if slot.name_display == owner.name]
    if len(named) == 1:
        return named[0]
    if len(matching) == 1:
        return matching[0]
    if matching and not create:
        raise ValueError(f"Action {action.name} has multiple suitable slots; assign the intended slot in Blender")
    if matching:
        raise ValueError(f"Action {action.name} has multiple suitable slots for {owner.id_type}")
    if not create:
        return None
    return action.slots.new(owner.id_type, owner.name)


def _cycle_slot_handle(action, owner, slot_identifier):
    """
    Pick which of an action's slots a cycle operation applies to.

    Args:
        action: The action being made cyclic.
        owner: The ID the action drives.
        slot_identifier: An explicit slot `identifier`, or None to resolve one.

    Returns:
        tuple: The slot's handle, and its identifier for the reply.

    Raises:
        ValueError: If the named slot is not in this action, or none can be resolved.

    """
    if slot_identifier is not None:
        slot = next((item for item in action.slots if item.identifier == slot_identifier), None)
        if slot is None:
            raise ValueError(f"Action {action.name} has no slot with identifier {slot_identifier}")
        return slot.handle, slot.identifier
    slot = _action_slot(action, owner)
    if slot is None:
        raise ValueError(f"Action {action.name} has no slot for {owner.name}; assign it before making it cyclic")
    return slot.handle, slot.identifier


def _assign_action(owner, action, *, replace_active):
    data = _animation_data(owner, create=True)
    current = data.action
    if current is not None and current != action and not replace_active:
        raise ValueError(
            f"{owner.name} already uses Action {current.name}; set replace_active=True to replace the assignment"
        )
    slot = _action_slot(action, owner, create=True)
    data.action = action
    data.action_slot = slot
    return slot


def _channelbag(action, slot, *, create=False):
    for layer in action.layers:
        for strip in layer.strips:
            for bag in getattr(strip, "channelbags", ()):
                if bag.slot_handle == slot.handle:
                    return bag
    if not create:
        return None
    if not action.layers:
        layer = action.layers.new("MCP Layer")
    else:
        layer = action.layers[0]
    if not layer.strips:
        strip = layer.strips.new(type="KEYFRAME")
    else:
        strip = layer.strips[0]
    return strip.channelbags.new(slot)


def _iter_fcurves(action, slot_handle=None):
    for layer in action.layers:
        for strip in layer.strips:
            for bag in getattr(strip, "channelbags", ()):
                if slot_handle is not None and bag.slot_handle != slot_handle:
                    continue
                for fcurve in bag.fcurves:
                    yield bag, fcurve


def _keyframe_info(action, slot_handle):
    records = []
    for _bag, fcurve in _iter_fcurves(action, slot_handle):
        for point in fcurve.keyframe_points:
            records.append(
                {
                    "data_path": fcurve.data_path,
                    "array_index": fcurve.array_index,
                    "frame": point.co[0],
                    "value": point.co[1],
                    "interpolation": point.interpolation,
                    "group": fcurve.group.name if fcurve.group else None,
                }
            )
    records.sort(key=lambda item: (item["frame"], item["data_path"], item["array_index"]))
    return records


def _driver_info(data):
    if data is None:
        return []
    records = []
    for fcurve in data.drivers:
        driver = fcurve.driver
        records.append(
            {
                "data_path": fcurve.data_path,
                "array_index": fcurve.array_index,
                "type": driver.type,
                "expression": driver.expression if driver.type == "SCRIPTED" else None,
                "muted": fcurve.mute,
                "valid": driver.is_valid,
                "variables": [
                    {
                        "name": variable.name,
                        "type": variable.type,
                        "targets": [
                            {
                                "id_type": target.id_type,
                                "id": target.id.name if target.id else None,
                                "data_path": target.data_path,
                            }
                            for target in variable.targets
                        ],
                    }
                    for variable in driver.variables
                ],
            }
        )
    return records


def _nla_info(data):
    if data is None:
        return []
    return [
        {
            "name": track.name,
            "mute": track.mute,
            "solo": track.is_solo,
            "lock": track.lock,
            "strips": [
                {
                    "name": strip.name,
                    "action": strip.action.name if strip.action else None,
                    "frame_start": strip.frame_start,
                    "frame_end": strip.frame_end,
                    "action_frame_start": strip.action_frame_start,
                    "action_frame_end": strip.action_frame_end,
                    "blend_type": strip.blend_type,
                    "extrapolation": strip.extrapolation,
                    "influence": strip.influence,
                    "repeat": strip.repeat,
                    "scale": strip.scale,
                    "mute": strip.mute,
                }
                for strip in track.strips
            ],
        }
        for track in data.nla_tracks
    ]


def _reduce_samples(samples, tolerance):
    """Ramer-Douglas-Peucker reduction for one scalar F-Curve."""
    if tolerance <= 0 or len(samples) <= 2:
        return samples, 0.0

    kept = {0, len(samples) - 1}

    def visit(start, end):
        frame_a, value_a = samples[start]
        frame_b, value_b = samples[end]
        worst_error = -1.0
        worst_index = None
        span = frame_b - frame_a
        for index in range(start + 1, end):
            frame, value = samples[index]
            factor = (frame - frame_a) / span if span else 0.0
            error = abs(value - (value_a + (value_b - value_a) * factor))
            if error > worst_error:
                worst_error = error
                worst_index = index
        if worst_index is not None and worst_error > tolerance:
            kept.add(worst_index)
            visit(start, worst_index)
            visit(worst_index, end)

    visit(0, len(samples) - 1)
    reduced = [samples[index] for index in sorted(kept)]
    maximum_error = 0.0
    for (frame_a, value_a), (frame_b, value_b) in zip(reduced, reduced[1:], strict=False):
        span = frame_b - frame_a
        for frame, value in samples:
            if frame_a < frame < frame_b:
                factor = (frame - frame_a) / span if span else 0.0
                maximum_error = max(maximum_error, abs(value - (value_a + (value_b - value_a) * factor)))
    return reduced, maximum_error


def _matrix_channels(matrix, rotation_mode):
    location, quaternion, scale = matrix.decompose()
    if rotation_mode == "QUATERNION":
        rotation_path = "rotation_quaternion"
        rotation = tuple(quaternion)
    elif rotation_mode == "AXIS_ANGLE":
        axis, angle = quaternion.to_axis_angle()
        rotation_path = "rotation_axis_angle"
        rotation = (angle, *axis)
    else:
        rotation_path = "rotation_euler"
        rotation = tuple(quaternion.to_euler(rotation_mode))
    return {"location": tuple(location), rotation_path: rotation, "scale": tuple(scale)}


def _append_transform_samples(channels, owner, matrix, transforms, frame, prefix=""):
    values = _matrix_channels(matrix, owner.rotation_mode)
    selected = set(transforms)
    paths = []
    if "LOCATION" in selected:
        paths.append("location")
    if "ROTATION" in selected:
        paths.append(next(name for name in values if name.startswith("rotation_")))
    if "SCALE" in selected:
        paths.append("scale")
    for path in paths:
        data_path = f"{prefix}{path}"
        for index, value in enumerate(values[path]):
            channels.setdefault((data_path, index), []).append((frame, float(value)))


def _path_tail(data_path):
    """
    Split a data path into its owner path and the final segment it addresses.

    `rpartition(".")` cannot do this: a subscript key may itself contain dots,
    so `pose.bones["hand_ik.L"]["IK_FK"]` splits inside the bone name and the
    custom property becomes unreachable. This scans instead, ignoring dots that
    sit inside quotes or brackets.

    Args:
        data_path: The RNA data path to split.

    Returns:
        tuple: `(owner_path, key, is_custom)`. `key` names an RNA property when
        `is_custom` is False and a custom (ID) property when it is True.
        `owner_path` is empty when the property sits on the target itself.

    Raises:
        ValueError: If quotes or brackets are unbalanced, the path ends in an
            array subscript (that belongs in `array_index`), or the final
            segment is neither an identifier nor a string key.

    """
    quote = None
    escaped = False
    depth = 0
    last_dot = -1
    subscript_start = -1
    for position, character in enumerate(data_path):
        if quote is not None:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
        elif character in {'"', "'"}:
            quote = character
        elif character == "[":
            if depth == 0:
                subscript_start = position
            depth += 1
        elif character == "]":
            depth -= 1
            if depth < 0:
                raise ValueError(f"Unbalanced brackets in data_path: {data_path!r}")
        elif character == "." and depth == 0:
            last_dot = position
            # An attribute after a subscript starts a fresh segment, so the
            # bracket that opened the previous one is no longer the tail.
            subscript_start = -1
    if quote is not None or depth:
        raise ValueError(f"Unbalanced quotes or brackets in data_path: {data_path!r}")
    if subscript_start >= 0 and data_path.endswith("]"):
        try:
            key = ast.literal_eval(data_path[subscript_start + 1 : -1])
        except Exception as exc:
            raise ValueError(f"Unsupported subscript in data_path {data_path!r}: {exc}") from exc
        if isinstance(key, str):
            return data_path[:subscript_start], key, True
        if isinstance(key, int) and not isinstance(key, bool):
            raise ValueError(f"data_path {data_path!r} must not end in an array index; use array_index instead")
        raise ValueError(f"Unsupported subscript in data_path: {data_path!r}")
    name = data_path[last_dot + 1 :]
    if not name.isidentifier():
        raise ValueError(f'data_path {data_path!r} must end in an RNA property identifier or a ["custom property"] key')
    return (data_path[:last_dot] if last_dot >= 0 else ""), name, False


def _custom_property(property_owner, key, data_path):
    """
    Read one animatable custom (ID) property addressed by a `["key"]` subscript.

    Rig controls - IK/FK switches, visibility sliders - are custom properties,
    and Blender keys them through the same F-Curve API as RNA properties. Only
    `bpy_struct` carries them: `id_properties_ensure` is absent on collections,
    which is what keeps `pose.bones["Hand"]` (a collection member, not a
    property) from being mistaken for one.

    Args:
        property_owner: The struct the subscript resolves against.
        key: The custom-property key.
        data_path: The full path, for error messages.

    Returns:
        tuple: `(array_length, value)`, with `array_length` 0 for a scalar.

    Raises:
        ValueError: If the owner cannot hold custom properties, the key is
            absent, or its value cannot drive an F-Curve.

    """
    ensure = getattr(property_owner, "id_properties_ensure", None)
    if ensure is None:
        raise ValueError(f"data_path does not address a custom property holder: {data_path}")
    group = ensure()
    if key not in group:
        raise ValueError(f"Custom property not found: {data_path}")
    value = group[key]
    return _custom_array_length(value, data_path), value


def _custom_array_length(value, data_path):
    """
    Classify a custom property's value as a count of F-Curve channels.

    Args:
        value: The custom property's current value.
        data_path: The full path, for error messages.

    Returns:
        int: 0 for a scalar, or the component count of a numeric array.

    Raises:
        ValueError: If the value cannot drive an F-Curve - a string, a nested
            group, or an array wider than one edit may address.

    """
    if isinstance(value, (bool, int, float)):
        return 0
    if not isinstance(value, (str, bytes)):
        try:
            items = list(value)
        except TypeError:
            items = None
        if items and all(isinstance(item, (bool, int, float)) for item in items):
            if len(items) > _MAX_ARRAY_CHANNELS:
                raise ValueError(
                    f"Custom property arrays wider than {_MAX_ARRAY_CHANNELS} channels are unsupported: {data_path}"
                )
            return len(items)
    raise ValueError(f"Custom property is not animatable: {data_path}")


def _resolve_property(owner, data_path):
    data_path = _required_name(data_path, "data_path")
    owner_path, key, is_custom = _path_tail(data_path)
    if owner_path:
        try:
            property_owner = owner.path_resolve(owner_path)
        except Exception as exc:
            raise ValueError(f"Invalid data_path owner {owner_path!r}: {exc}") from exc
    else:
        property_owner = owner
    if is_custom:
        return _custom_property(property_owner, key, data_path)
    properties = getattr(getattr(property_owner, "bl_rna", None), "properties", None)
    rna_property = properties.get(key) if properties is not None else None
    if rna_property is None:
        raise ValueError(f"RNA property not found: {data_path}")
    if rna_property.is_readonly:
        raise ValueError(f"RNA property is read-only: {data_path}")
    if not rna_property.is_animatable:
        raise ValueError(f"RNA property is not animatable: {data_path}")
    value = getattr(property_owner, key)
    array_length = getattr(rna_property, "array_length", 0)
    return array_length, value


def _expanded_edit(owner, edit):
    if not isinstance(edit, dict):
        raise ValueError("Each keyframe edit must be an object")
    operation = str(edit.get("operation", "UPSERT")).upper()
    if operation not in {"UPSERT", "REMOVE"}:
        raise ValueError(f"Unsupported keyframe operation: {operation}")
    data_path = _required_name(edit.get("data_path"), "data_path")
    frame = edit.get("frame")
    if isinstance(frame, bool) or not isinstance(frame, (int, float)) or not math.isfinite(frame):
        raise ValueError("frame must be a finite number")
    if not -1_000_000 <= frame <= 1_000_000:
        raise ValueError("frame must be between -1000000 and 1000000")
    index = edit.get("array_index", -1)
    if isinstance(index, bool) or not isinstance(index, int) or not -1 <= index <= 63:
        raise ValueError("array_index must be an integer from -1 to 63")
    style = KeyStyle(
        str(edit.get("interpolation", "BEZIER")).upper(),
        str(edit.get("handle_left", "AUTO_CLAMPED")).upper(),
        str(edit.get("handle_right", "AUTO_CLAMPED")).upper(),
        str(edit["easing"]).upper() if edit.get("easing") is not None else None,
    )
    style.validate()
    array_length, _current = _resolve_property(owner, data_path)
    if index >= 0 and (not array_length or index >= array_length):
        raise ValueError(f"array_index {index} is invalid for {data_path} (length {array_length})")
    value = edit.get("value")
    if operation == "REMOVE":
        if value is not None:
            raise ValueError("REMOVE does not accept value")
        indices = range(array_length) if index == -1 and array_length else [0 if index == -1 else index]
        return [(operation, data_path, item, float(frame), None, style, edit.get("group")) for item in indices]
    if value is None:
        raise ValueError("UPSERT requires value")
    if index == -1 and array_length:
        if not isinstance(value, (list, tuple)) or len(value) != array_length:
            raise ValueError(f"{data_path} requires exactly {array_length} values when array_index=-1")
        values = list(value)
        indices = range(array_length)
    else:
        if isinstance(value, (list, tuple)):
            raise ValueError("A scalar value is required for one animation channel")
        values = [value]
        indices = [0 if index == -1 else index]
    expanded = []
    for channel_index, channel_value in zip(indices, values, strict=True):
        if isinstance(channel_value, bool) or not isinstance(channel_value, (int, float)):
            raise ValueError("Keyframe values must be numeric")
        channel_value = float(channel_value)
        if not math.isfinite(channel_value):
            raise ValueError("Keyframe values must be finite")
        expanded.append((operation, data_path, channel_index, float(frame), channel_value, style, edit.get("group")))
    return expanded


def _find_key(fcurve, frame):
    return next((point for point in fcurve.keyframe_points if abs(point.co[0] - frame) <= 1e-6), None)


def _driver_fcurve(owner, data_path, index):
    data = owner.animation_data
    if data is None:
        return None
    return next(
        (fcurve for fcurve in data.drivers if fcurve.data_path == data_path and fcurve.array_index == index),
        None,
    )


def _safe_expression(expression, variable_names):
    if not isinstance(expression, str) or not expression or len(expression) > 256:
        raise ValueError("expression must contain between 1 and 256 characters")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ValueError("expression must be valid arithmetic syntax") from exc
    for node in ast.walk(tree):
        if not isinstance(node, _SAFE_EXPRESSION_NODES):
            raise ValueError("expression may contain only arithmetic, numeric constants, variables, and frame")
        if isinstance(node, ast.Name) and node.id not in variable_names | {"frame"}:
            raise ValueError(f"expression references undeclared variable: {node.id}")
        if isinstance(node, ast.Constant) and (
            isinstance(node.value, bool) or not isinstance(node.value, (int, float))
        ):
            raise ValueError("expression constants must be numeric")
    return expression


def _prepare_driver_variables(variables):
    if not isinstance(variables, list) or len(variables) > 64:
        raise ValueError("variables must be a list with at most 64 records")
    prepared = []
    names = set()
    for spec in variables:
        if not isinstance(spec, dict):
            raise ValueError("Each driver variable must be an object")
        name = _required_name(spec.get("name"), "variable.name")
        if not _DRIVER_NAME_RE.fullmatch(name) or name == "frame":
            raise ValueError("Driver variable names must be identifiers other than 'frame'")
        if name in names:
            raise ValueError(f"Duplicate driver variable name: {name}")
        names.add(name)
        variable_type = str(spec.get("type", "")).upper()
        if variable_type not in _DRIVER_VARIABLE_TYPES:
            raise ValueError(f"Unsupported driver variable type: {variable_type}")
        source, source_type = _target(spec.get("target"))
        if variable_type == "SINGLE_PROP":
            source_path = _required_name(spec.get("data_path"), "variable.data_path")
            try:
                source.path_resolve(source_path)
            except Exception as exc:
                raise ValueError(f"Invalid source data_path {source_path!r}: {exc}") from exc
            prepared.append((name, variable_type, source, source_type, source_path, None, None, None))
        else:
            if source_type != "OBJECT":
                raise ValueError("TRANSFORMS variables require an OBJECT target")
            transform_type = str(spec.get("transform_type", "")).upper()
            transform_space = str(spec.get("transform_space", "WORLD_SPACE")).upper()
            if transform_type not in _DRIVER_TRANSFORM_TYPES:
                raise ValueError(f"Unsupported driver transform_type: {transform_type}")
            if transform_space not in _DRIVER_TRANSFORM_SPACES:
                raise ValueError(f"Unsupported driver transform_space: {transform_space}")
            bone_target = spec.get("bone_target") or ""
            if bone_target and (source.type != "ARMATURE" or source.data.bones.get(bone_target) is None):
                raise ValueError(f"Armature bone not found: {source.name}/{bone_target}")
            prepared.append(
                (name, variable_type, source, source_type, None, bone_target, transform_type, transform_space)
            )
    return prepared


def _replace_driver_variables(driver, prepared):
    while driver.variables:
        driver.variables.remove(driver.variables[0])
    for name, variable_type, source, source_type, data_path, bone_target, transform_type, transform_space in prepared:
        variable = driver.variables.new()
        variable.name = name
        variable.type = variable_type
        target = variable.targets[0]
        if variable_type == "SINGLE_PROP":
            # Writable only for SINGLE_PROP: a TRANSFORMS target is always an Object, and Blender
            # exposes its `id_type` read-only, so assigning it raises AttributeError.
            target.id_type = source_type
            target.id = source
            target.data_path = data_path
        else:
            target.id = source
            target.bone_target = bone_target
            target.transform_type = transform_type
            target.transform_space = transform_space


def _driver_result(owner, fcurve):
    driver = fcurve.driver
    return {
        "target": owner.name,
        "data_path": fcurve.data_path,
        "array_index": fcurve.array_index,
        "type": driver.type,
        "expression": driver.expression if driver.type == "SCRIPTED" else None,
        "mute": fcurve.mute,
        "variables": [variable.name for variable in driver.variables],
        "changed_resources": [owner.name],
    }


def _nla_track(data, name):
    return next((track for track in data.nla_tracks if track.name == name), None)


def _nla_strip(track, name):
    return next((strip for strip in track.strips if strip.name == name), None)


def _validate_nla_patch(patch, allowed, label):
    if not isinstance(patch, dict) or not patch:
        raise ValueError(f"{label} must be a non-empty object")
    unknown = sorted(set(patch) - allowed)
    if unknown:
        raise ValueError(f"Unsupported {label} settings: {unknown}")
    for name, value in patch.items():
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"{name} must be finite")
    return patch


def _patch_nla(owner, patch, mapping=None):
    mapping = mapping or {}
    previous = {}
    try:
        for name, value in patch.items():
            property_name = mapping.get(name, name)
            previous[property_name] = getattr(owner, property_name)
            setattr(owner, property_name, value)
    except Exception:
        for name, value in previous.items():
            setattr(owner, name, value)
        raise
    return previous


def _sampled_bake_channels(obj, target, frames, transforms, bone_names):
    """
    Walk the frame range once, reading the evaluated values the bake will key.

    Every sample comes from the dependency graph, so constraints, drivers and parenting are
    already resolved into the numbers recorded here.

    Args:
        obj: The object being baked.
        target: The bake target record, read for `space` and `properties`.
        frames: The frames to sample, in order.
        transforms: LOCATION/ROTATION/SCALE channels to record, if any.
        bone_names: Pose bones to record instead of the object itself, if any.

    Returns:
        tuple: `{(data_path, array_index): [(frame, value), ...]}` and the per-channel
        tolerances the caller's reduction should use.

    Raises:
        ValueError: If a property channel names array indices the property does not have.

    """
    scene = bpy.context.scene
    channels = {}
    channel_tolerances = {}
    for frame in frames:
        scene.frame_set(frame)
        evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
        if transforms and not bone_names:
            matrix = evaluated.matrix_world if target.get("space") == "WORLD" else evaluated.matrix_basis
            _append_transform_samples(channels, obj, matrix, transforms, frame)
        for bone_name in bone_names:
            evaluated_bone = evaluated.pose.bones[bone_name]
            matrix = evaluated_bone.matrix if target.get("space") in {"WORLD", "POSE"} else evaluated_bone.matrix_basis
            pose_bone = obj.pose.bones[bone_name]
            _append_transform_samples(channels, pose_bone, matrix, transforms, frame, f"{pose_bone.path_from_id()}.")
        for channel in target.get("properties", []):
            array_length, value = _resolve_property(evaluated, channel["data_path"])
            indices = channel.get("array_indices")
            if array_length:
                indices = indices or list(range(array_length))
                invalid = [index for index in indices if index < 0 or index >= array_length]
                if invalid:
                    raise ValueError(f"Invalid array indices for {channel['data_path']}: {invalid}")
                for index in indices:
                    channels.setdefault((channel["data_path"], index), []).append((frame, float(value[index])))
                    channel_tolerances[channel["data_path"], index] = channel.get("tolerance", 0.0)
            else:
                channels.setdefault((channel["data_path"], 0), []).append((frame, float(value)))
                channel_tolerances[channel["data_path"], 0] = channel.get("tolerance", 0.0)
    return channels, channel_tolerances


def _write_baked_curves(bag, channels, channel_tolerances, transform_tolerance, style):
    """
    Reduce each sampled channel and write it as one styled F-Curve.

    Args:
        bag: The channelbag the curves are created in.
        channels: `_sampled_bake_channels`' samples.
        channel_tolerances: Per-channel reduction tolerances.
        transform_tolerance: The tolerance for channels with none of their own.
        style: The `KeyStyle` applied to every key.

    Returns:
        dict: key_count, curves (one record per channel) and max_reconstruction_error - the
        worst distance between a reduced curve and the samples it replaced.

    """
    key_count = 0
    maximum_error = 0.0
    curve_records = []
    for (data_path, index), samples in channels.items():
        reduced, error = _reduce_samples(samples, channel_tolerances.get((data_path, index), transform_tolerance))
        maximum_error = max(maximum_error, error)
        fcurve = bag.fcurves.new(data_path, index=index)
        for key_frame, value in reduced:
            key = fcurve.keyframe_points.insert(key_frame, value, options={"FAST"})
            style_point(key, style)
        fcurve.update()
        key_count += len(reduced)
        curve_records.append(
            {
                "data_path": data_path,
                "array_index": index,
                "sample_count": len(samples),
                "key_count": len(reduced),
                "max_reconstruction_error": error,
            }
        )
    return {"key_count": key_count, "curves": curve_records, "max_reconstruction_error": maximum_error}


def _resolved_edit_action(owner, action_name, replace_active, allow_shared):
    """
    Pick, create or reuse the layered Action one keyframe edit writes into.

    An Action this call creates is removed again on every refusal below it, because a
    refused edit that leaves a new empty Action assigned to the target reads, at save time,
    exactly like the animation the caller asked for.

    Args:
        owner: The ID being keyed.
        action_name: The Action to create or reuse, or None to edit whatever drives `owner`.
        replace_active: Whether assigning may displace the Action already there.
        allow_shared: Whether an Action with other users may be edited in place.

    Returns:
        tuple: The Action and the slot within it this owner's keys belong to.

    Raises:
        ValueError: If no Action is named and none is active, if the Action is shared and
            `allow_shared` is not set, or if it is a legacy (non-layered) Action.

    """
    data = _animation_data(owner, create=True)
    current = data.action
    created_action = False
    if action_name:
        action_name = _required_name(action_name, "action_name")
        selected = bpy.data.actions.get(action_name)
        if selected is None:
            selected = bpy.data.actions.new(action_name)
            created_action = True
    elif current is not None:
        selected = current
    else:
        raise ValueError("Target has no active Action; provide action_name to create or assign one")
    try:
        if selected.users - (1 if current == selected else 0) > 0 and not allow_shared:
            raise ValueError(
                f"Action {selected.name} has {selected.users} users; set allow_shared_action=True to edit it in place"
            )
        if not selected.is_action_layered:
            raise ValueError(f"Action {selected.name} is legacy; convert or duplicate it to a layered Action first")
        if action_name:
            return selected, _assign_action(owner, selected, replace_active=replace_active)
        return selected, _action_slot(selected, owner, create=True)
    except Exception:
        if created_action:
            bpy.data.actions.remove(selected)
        raise


class AnimationHandlersMixin:
    """Expose generic animation inspection, Actions, and keyframes."""

    def inspect_animation(self, target, offset=0, limit=200):
        owner, target_type = _target(target)
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer from 1 to 1000")
        data = _animation_data(owner)
        action = data.action if data else None
        slot = data.action_slot if data and action else None
        keys = _keyframe_info(action, slot.handle) if action and slot else []
        page = keys[offset : offset + limit]
        return {
            "target": {"type": target_type, "name": owner.name},
            "action": {
                "name": action.name,
                "users": action.users,
                "is_layered": action.is_action_layered,
                "slot": slot.identifier if slot else None,
                "slots": [
                    {"identifier": item.identifier, "target_id_type": item.target_id_type, "name": item.name_display}
                    for item in action.slots
                ],
                "frame_range": list(action.curve_frame_range),
            }
            if action
            else None,
            "keyframes": page,
            "total_keyframes": len(keys),
            "offset": offset,
            "limit": limit,
            "truncated": offset + len(page) < len(keys),
            "next_offset": offset + len(page) if offset + len(page) < len(keys) else None,
            "drivers": _driver_info(data),
            "nla_tracks": _nla_info(data),
        }

    def manage_animation_action(
        self,
        target,
        action,
        action_name=None,
        source_action_name=None,
        replace_active=False,
    ):
        owner, _target_type = _target(target)
        operation = str(action).upper()
        if operation not in {"CREATE", "ASSIGN", "DUPLICATE", "UNASSIGN"}:
            raise ValueError(f"Unsupported action operation: {operation}")
        data = _animation_data(owner, create=operation != "UNASSIGN")
        if operation == "UNASSIGN":
            if data is None or data.action is None:
                return {"target": owner.name, "action": None, "changed_resources": []}
            if action_name and data.action.name != action_name:
                raise ValueError(f"Active Action is {data.action.name}, not {action_name}")
            previous = data.action.name
            data.action = None
            return {"target": owner.name, "unassigned": previous, "changed_resources": [owner.name, previous]}

        action_name = _required_name(action_name, "action_name")
        if operation == "CREATE":
            if bpy.data.actions.get(action_name) is not None:
                raise ValueError(f"Action already exists: {action_name}")
            selected = bpy.data.actions.new(action_name)
        elif operation == "ASSIGN":
            selected = bpy.data.actions.get(action_name)
            if selected is None:
                raise ValueError(f"Action not found: {action_name}")
        else:
            source_name = _required_name(source_action_name, "source_action_name")
            source = bpy.data.actions.get(source_name)
            if source is None:
                raise ValueError(f"Source Action not found: {source_name}")
            if bpy.data.actions.get(action_name) is not None:
                raise ValueError(f"Action already exists: {action_name}")
            selected = source.copy()
            selected.name = action_name
        if not selected.is_action_layered:
            if operation in {"CREATE", "DUPLICATE"}:
                bpy.data.actions.remove(selected)
            raise ValueError(f"Action {selected.name} is legacy and cannot be assigned by this tool")
        try:
            slot = _assign_action(owner, selected, replace_active=replace_active)
        except Exception:
            if operation in {"CREATE", "DUPLICATE"}:
                bpy.data.actions.remove(selected)
            raise
        return {
            "target": owner.name,
            "action": selected.name,
            "slot": slot.identifier,
            "created": operation in {"CREATE", "DUPLICATE"},
            "changed_resources": [owner.name, selected.name],
        }

    def edit_keyframes(
        self,
        target,
        edits,
        action_name=None,
        replace_active_action=False,
        allow_shared_action=False,
    ):
        owner, _target_type = _target(target)
        if not isinstance(edits, list) or not 1 <= len(edits) <= 1000:
            raise ValueError("edits must contain between 1 and 1000 records")
        expanded = [item for edit in edits for item in _expanded_edit(owner, edit)]
        selected, slot = _resolved_edit_action(owner, action_name, replace_active_action, allow_shared_action)
        bag = _channelbag(selected, slot, create=True)
        changed = []
        for operation, data_path, index, frame, value, style, group in expanded:
            fcurve = bag.fcurves.find(data_path, index=index)
            key = _find_key(fcurve, frame) if fcurve else None
            if operation == "REMOVE":
                if key is None:
                    continue
                fcurve.keyframe_points.remove(key, fast=True)
                if not fcurve.keyframe_points:
                    bag.fcurves.remove(fcurve)
                else:
                    fcurve.update()
                changed.append({"operation": operation, "data_path": data_path, "array_index": index, "frame": frame})
                continue
            if fcurve is None:
                fcurve = bag.fcurves.new(data_path, index=index, group_name=group or "")
            if key is None:
                key = fcurve.keyframe_points.insert(frame, value, options={"FAST"})
            else:
                key.co[1] = value
            style_point(key, style)
            fcurve.update()
            changed.append(
                {
                    "operation": operation,
                    "data_path": data_path,
                    "array_index": index,
                    "frame": frame,
                    "value": value,
                }
            )
        return {
            "target": owner.name,
            "action": selected.name,
            "slot": slot.identifier,
            "changed_keyframes": changed,
            "changed_resources": [owner.name, selected.name],
        }

    def bake_evaluated_animation(
        self,
        target,
        frame_start,
        frame_end,
        frame_step=1,
        action_name="Evaluated Bake",
        interpolation="LINEAR",
        handle_left="AUTO_CLAMPED",
        handle_right="AUTO_CLAMPED",
        easing=None,
        transform_tolerance=0.0,
        confirm_bake=False,
    ):
        if not confirm_bake:
            raise ValueError("confirm_bake=True is required")
        style = KeyStyle(interpolation, handle_left, handle_right, easing)
        style.validate()
        object_name = _required_name(target.get("object_name"), "target.object_name")
        obj = bpy.data.objects.get(object_name)
        if obj is None:
            raise ValueError(f"Object not found: {object_name}")
        if bpy.data.actions.get(action_name) is not None:
            raise ValueError(f"Action already exists: {action_name}")
        if frame_end < frame_start or frame_step < 1:
            raise ValueError("Require frame_end >= frame_start and frame_step >= 1")
        frames = list(range(frame_start, frame_end + 1, frame_step))
        if len(frames) > 100_000:
            raise ValueError("Bake range exceeds the 100000-sample safety limit")
        transforms = target.get("transforms", [])
        bone_names = target.get("bone_names", [])
        if bone_names and obj.type != "ARMATURE":
            raise ValueError("bone_names require an ARMATURE object")
        missing_bones = [name for name in bone_names if obj.pose.bones.get(name) is None]
        if missing_bones:
            raise ValueError(f"Pose bones not found: {missing_bones}")
        for channel in target.get("properties", []):
            _resolve_property(obj, channel["data_path"])

        scene = bpy.context.scene
        original_frame = scene.frame_current
        try:
            channels, channel_tolerances = _sampled_bake_channels(obj, target, frames, transforms, bone_names)
        finally:
            scene.frame_set(original_frame)

        action = bpy.data.actions.new(_required_name(action_name, "action_name"))
        try:
            slot = _assign_action(obj, action, replace_active=True)
            written = _write_baked_curves(
                _channelbag(action, slot, create=True),
                channels,
                channel_tolerances,
                transform_tolerance,
                style,
            )
        except Exception:
            bpy.data.actions.remove(action)
            raise
        return {
            "object": obj.name,
            "action": action.name,
            "slot": slot.identifier,
            "frame_range": [frame_start, frame_end, frame_step],
            "sampled_key_count": sum(len(samples) for samples in channels.values()),
            "key_count": written["key_count"],
            "curves": written["curves"],
            "max_reconstruction_error": written["max_reconstruction_error"],
            "sample_space": target.get("space", "LOCAL"),
            "new_non_shared_action": action.users <= 1,
            "warnings": [
                "Constraints remain live; mute or remove them before using baked transforms as final unconstrained motion."
            ]
            if transforms
            else [],
            "changed_objects": [obj.name],
            "changed_resources": [action.name],
        }

    def set_action_cycle(
        self,
        target,
        action_name,
        operation="SET",
        mode_before="REPEAT_OFFSET",
        mode_after="REPEAT_OFFSET",
        cycles_before=0,
        cycles_after=0,
        data_path_prefix=None,
        action_slot_identifier=None,
    ):
        """Add, update or remove the Cycles F-Modifier on an action slot's curves."""
        owner, _target_type = _target(target)
        operation = str(operation).upper()
        if operation not in {"SET", "REMOVE"}:
            raise ValueError("operation must be SET or REMOVE")
        for label, mode in (("mode_before", mode_before), ("mode_after", mode_after)):
            if mode not in _CYCLE_MODES:
                raise ValueError(f"{label} must be one of {sorted(_CYCLE_MODES)}")
        action = bpy.data.actions.get(_required_name(action_name, "action_name"))
        if action is None:
            raise ValueError(f"Action not found: {action_name}")
        slot_handle, slot_identifier = _cycle_slot_handle(action, owner, action_slot_identifier)
        curves = [curve for _bag, curve in _iter_fcurves(action, slot_handle)]
        if not curves:
            raise ValueError(f"Action {action.name} has no F-Curves in slot {slot_identifier}")
        selected = [
            curve for curve in curves if data_path_prefix is None or curve.data_path.startswith(data_path_prefix)
        ]
        if not selected:
            # A prefix that names nothing is a typo, not a finished job: reported as success it
            # reads as "made cyclic", and the caller only finds out when playback does not loop.
            # A selected curve that simply has no modifier to REMOVE is a different thing, and
            # stays a success below.
            raise ValueError(
                f"Action {action.name} has no F-Curve in slot {slot_identifier} whose data_path starts with "
                f"{data_path_prefix!r}"
            )
        records = []
        for curve in selected:
            modifier = next((item for item in curve.modifiers if item.type == "CYCLES"), None)
            if operation == "REMOVE":
                if modifier is None:
                    continue
                curve.modifiers.remove(modifier)
            else:
                if modifier is None:
                    modifier = curve.modifiers.new(type="CYCLES")
                modifier.mode_before = mode_before
                modifier.mode_after = mode_after
                # Blender spells "forever" as zero cycles, in both directions.
                modifier.cycles_before = int(cycles_before)
                modifier.cycles_after = int(cycles_after)
            records.append(
                {
                    "data_path": curve.data_path,
                    "array_index": curve.array_index,
                    "mode_before": mode_before if operation == "SET" else None,
                    "mode_after": mode_after if operation == "SET" else None,
                }
            )
        return {
            "action": action.name,
            "action_slot": slot_identifier,
            "operation": operation,
            "curve_count": len(records),
            "modifiers": records,
            "changed_resources": [action.name],
        }

    def manage_nla_tracks(
        self,
        target,
        action,
        track_name,
        strip_name=None,
        action_name=None,
        frame_start=None,
        track_patch=None,
        strip_patch=None,
        confirm_remove=False,
    ):
        owner, _target_type = _target(target)
        operation = str(action).upper()
        allowed = {"CREATE_TRACK", "ADD_STRIP", "PATCH_TRACK", "PATCH_STRIP", "REMOVE_STRIP", "REMOVE_TRACK"}
        if operation not in allowed:
            raise ValueError(f"Unsupported NLA operation: {operation}")
        track_name = _required_name(track_name, "track_name")
        data = _animation_data(owner, create=operation in {"CREATE_TRACK", "ADD_STRIP"})
        track = _nla_track(data, track_name) if data else None

        if operation == "CREATE_TRACK":
            if track is not None:
                raise ValueError(f"NLA track already exists: {track_name}")
            track = data.nla_tracks.new()
            track.name = track_name
            if track_patch:
                patch = _validate_nla_patch(track_patch, _NLA_TRACK_PROPERTIES, "track_patch")
                _patch_nla(track, patch, {"solo": "is_solo"})
            return {"target": owner.name, "track": track.name, "created": True, "changed_resources": [owner.name]}

        if track is None:
            raise ValueError(f"NLA track not found: {track_name}")
        if operation == "PATCH_TRACK":
            patch = _validate_nla_patch(track_patch, _NLA_TRACK_PROPERTIES, "track_patch")
            _patch_nla(track, patch, {"solo": "is_solo"})
        elif operation == "ADD_STRIP":
            strip_name = _required_name(strip_name, "strip_name")
            action_name = _required_name(action_name, "action_name")
            if _nla_strip(track, strip_name) is not None:
                raise ValueError(f"NLA strip already exists in {track_name}: {strip_name}")
            action_data = bpy.data.actions.get(action_name)
            if action_data is None:
                raise ValueError(f"Action not found: {action_name}")
            if not action_data.is_action_layered:
                raise ValueError(f"Action {action_name} is legacy and cannot be added by this tool")
            if (
                isinstance(frame_start, bool)
                or not isinstance(frame_start, (int, float))
                or not math.isfinite(frame_start)
            ):
                raise ValueError("frame_start must be a finite number")
            slot = _action_slot(action_data, owner, create=True)
            strip = track.strips.new(strip_name, math.floor(frame_start), action_data)
            try:
                strip.action_slot = slot
                strip.frame_start = float(frame_start)
                if strip_patch:
                    patch = _validate_nla_patch(strip_patch, _NLA_STRIP_PROPERTIES, "strip_patch")
                    resulting_start = patch.get("frame_start", strip.frame_start)
                    resulting_end = patch.get("frame_end", strip.frame_end)
                    if resulting_end <= resulting_start:
                        raise ValueError("Resulting frame_end must be greater than frame_start")
                    _patch_nla(strip, patch)
            except Exception:
                track.strips.remove(strip)
                raise
            return {
                "target": owner.name,
                "track": track.name,
                "strip": strip.name,
                "action": action_data.name,
                "changed_resources": [owner.name, action_data.name],
            }
        elif operation == "PATCH_STRIP":
            strip_name = _required_name(strip_name, "strip_name")
            strip = _nla_strip(track, strip_name)
            if strip is None:
                raise ValueError(f"NLA strip not found in {track_name}: {strip_name}")
            patch = _validate_nla_patch(strip_patch, _NLA_STRIP_PROPERTIES, "strip_patch")
            resulting_start = patch.get("frame_start", strip.frame_start)
            resulting_end = patch.get("frame_end", strip.frame_end)
            action_start = patch.get("action_frame_start", strip.action_frame_start)
            action_end = patch.get("action_frame_end", strip.action_frame_end)
            if resulting_end <= resulting_start:
                raise ValueError("Resulting frame_end must be greater than frame_start")
            if action_end <= action_start:
                raise ValueError("Resulting action_frame_end must be greater than action_frame_start")
            _patch_nla(strip, patch)
        elif operation == "REMOVE_STRIP":
            if not confirm_remove:
                raise ValueError("confirm_remove=True is required")
            strip_name = _required_name(strip_name, "strip_name")
            strip = _nla_strip(track, strip_name)
            if strip is None:
                raise ValueError(f"NLA strip not found in {track_name}: {strip_name}")
            track.strips.remove(strip)
            return {
                "target": owner.name,
                "track": track.name,
                "removed_strip": strip_name,
                "changed_resources": [owner.name],
            }
        else:
            if not confirm_remove:
                raise ValueError("confirm_remove=True is required")
            data.nla_tracks.remove(track)
            return {"target": owner.name, "removed_track": track_name, "changed_resources": [owner.name]}
        return {"target": owner.name, "track": _nla_info(data), "changed_resources": [owner.name]}

    def manage_animation_driver(
        self,
        target,
        action,
        data_path,
        array_index=-1,
        driver_type=None,
        expression=None,
        variables=None,
        mute=None,
        confirm_remove=False,
    ):
        owner, _target_type = _target(target)
        operation = str(action).upper()
        if operation not in {"ADD", "PATCH", "REMOVE"}:
            raise ValueError(f"Unsupported driver operation: {operation}")
        data_path = _required_name(data_path, "data_path")
        array_length, _value = _resolve_property(owner, data_path)
        if isinstance(array_index, bool) or not isinstance(array_index, int) or not -1 <= array_index <= 63:
            raise ValueError("array_index must be an integer from -1 to 63")
        if array_length:
            if array_index < 0 or array_index >= array_length:
                raise ValueError(f"array_index must select one channel of {data_path} (length {array_length})")
            normalized_index = array_index
        else:
            if array_index not in {-1, 0}:
                raise ValueError(f"array_index is invalid for scalar property {data_path}")
            normalized_index = 0
        fcurve = _driver_fcurve(owner, data_path, normalized_index)
        if operation == "REMOVE":
            if not confirm_remove:
                raise ValueError("confirm_remove=True is required")
            if fcurve is None:
                raise ValueError(f"Driver not found: {data_path}[{normalized_index}]")
            if not owner.driver_remove(data_path, normalized_index if array_length else -1):
                raise RuntimeError(f"Blender did not remove driver: {data_path}[{normalized_index}]")
            return {
                "target": owner.name,
                "removed": {"data_path": data_path, "array_index": normalized_index},
                "changed_resources": [owner.name],
            }
        if operation == "ADD" and fcurve is not None:
            raise ValueError(f"Driver already exists: {data_path}[{normalized_index}]")
        if operation == "PATCH" and fcurve is None:
            raise ValueError(f"Driver not found: {data_path}[{normalized_index}]")
        prepared = _prepare_driver_variables(variables) if variables is not None else None
        requested_type = str(driver_type).upper() if driver_type is not None else None
        if requested_type is not None and requested_type not in _DRIVER_TYPES:
            raise ValueError(f"Unsupported driver_type: {requested_type}")
        resulting_type = requested_type or fcurve.driver.type
        if expression is not None:
            if resulting_type != "SCRIPTED":
                raise ValueError("expression is valid only for a SCRIPTED driver")
            expression = _safe_expression(expression, {item[0] for item in prepared or []})
        if resulting_type == "SCRIPTED" and operation == "ADD" and expression is None:
            raise ValueError("SCRIPTED ADD requires expression")
        if operation == "ADD":
            try:
                fcurve = owner.driver_add(data_path, normalized_index if array_length else -1)
            except Exception as exc:
                raise ValueError(f"Could not add driver for {data_path}[{normalized_index}]: {exc}") from exc
        try:
            if requested_type is not None:
                fcurve.driver.type = requested_type
            if prepared is not None:
                _replace_driver_variables(fcurve.driver, prepared)
            if expression is not None:
                fcurve.driver.expression = expression
            if mute is not None:
                fcurve.mute = bool(mute)
        except Exception:
            if operation == "ADD":
                owner.driver_remove(data_path, normalized_index if array_length else -1)
            raise
        return _driver_result(owner, fcurve)
