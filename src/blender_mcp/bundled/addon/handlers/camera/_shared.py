# pyright: reportGeneralTypeIssues=false, reportOptionalSubscript=false
"""Validation, lookup, serialization, and constraint helpers shared by two or more camera topic modules."""

import math

import bpy
import mathutils

from ...helpers import rotation_as_native_list

_CAMERA_OPTICS = {
    "lens",
    "ortho_scale",
    "sensor_width",
    "sensor_height",
    "sensor_fit",
    "shift_x",
    "shift_y",
    "clip_start",
    "clip_end",
    "panorama_type",
}
_CAMERA_DISPLAY = {
    "passepartout_alpha",
    "show_passepartout",
    "show_safe_areas",
    "show_name",
    "show_limits",
    "show_mist",
    "show_composition_center",
    "show_composition_center_diagonal",
    "show_composition_golden",
    "show_composition_golden_tria_a",
    "show_composition_golden_tria_b",
    "show_composition_harmony_tri_a",
    "show_composition_harmony_tri_b",
    "show_composition_thirds",
}
_TRACK_CONSTRAINTS = {"TRACK_TO", "DAMPED_TRACK", "LOCKED_TRACK"}
_CONSTRAINT_TYPES = {
    "TRACK_TO",
    "DAMPED_TRACK",
    "LOCKED_TRACK",
    "FOLLOW_PATH",
    "CHILD_OF",
    "COPY_LOCATION",
    "COPY_ROTATION",
    "COPY_TRANSFORMS",
    "LIMIT_LOCATION",
    "LIMIT_ROTATION",
    "LIMIT_SCALE",
}
_TARGETED_CONSTRAINTS = _CONSTRAINT_TYPES - {"LIMIT_LOCATION", "LIMIT_ROTATION", "LIMIT_SCALE"}
_RIG_SCHEMA_VERSION = 1
_MAX_RIG_DESCENDANTS = 2_000
_MIN_FRAME = -1_048_574
_MAX_FRAME = 1_048_574


def _finite_number(value, label):
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{label} must be finite")
    return value


def _vector(value, label):
    if value is None or len(value) != 3:
        raise ValueError(f"{label} must contain exactly three numbers")
    return mathutils.Vector(
        tuple(_finite_number(component, f"{label}[{index}]") for index, component in enumerate(value))
    )


def _positive(value, label, *, allow_zero=False):
    value = _finite_number(value, label)
    if value < 0 if allow_zero else value <= 0:
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{label} must be {qualifier}")
    return value


def _required_name(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _bounded_int(value, label, minimum, maximum):
    if isinstance(value, bool):
        raise ValueError(f"{label} must be an integer")
    integer = int(value)
    if integer != value or not minimum <= integer <= maximum:
        raise ValueError(f"{label} must be an integer in [{minimum}, {maximum}]")
    return integer


def _frame(value, label):
    if isinstance(value, bool) or int(value) != value or not _MIN_FRAME <= int(value) <= _MAX_FRAME:
        raise ValueError(f"{label} must be an integer in [{_MIN_FRAME}, {_MAX_FRAME}]")
    return int(value)


def _scene_fps(scene):
    return scene.render.fps / scene.render.fps_base if scene.render.fps_base else float(scene.render.fps)


def _resolve_frame_from_time(frame, at_seconds, label, scene):
    if (frame is None) == (at_seconds is None):
        raise ValueError(f"{label} must supply exactly one of frame or at_seconds")
    if frame is not None:
        return _frame(frame, f"{label}")
    seconds = _finite_number(at_seconds, f"{label}.at_seconds")
    fps = _scene_fps(scene)
    resolved = _frame(scene.frame_start + seconds * fps, f"{label}.at_seconds")
    return resolved


def _scene(name):
    scene = bpy.data.scenes.get(name)
    if scene is None:
        raise ValueError(f"Scene not found: {name}")
    return scene


def _object(name, *, scene=None):
    obj = bpy.data.objects.get(name)
    if obj is None:
        raise ValueError(f"Object not found: {name}")
    if scene is not None and obj.name not in scene.objects:
        raise ValueError(f"Object '{name}' is not linked to scene '{scene.name}'")
    return obj


def _camera(name, *, scene=None):
    obj = _object(name, scene=scene)
    if obj.type != "CAMERA" or obj.data is None:
        raise ValueError(f"Object '{name}' is not a camera (type={obj.type})")
    _update_view_layer()
    return obj


def _ensure_collection(scene, name):
    if not isinstance(name, str) or not name.strip():
        raise ValueError("collection_name must be a non-empty string")
    collection = bpy.data.collections.get(name)
    if collection is None:
        collection = bpy.data.collections.new(name)
    if collection.name not in scene.collection.children:
        scene.collection.children.link(collection)
    return collection


def _plain(value):
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if hasattr(value, "name"):
        return value.name
    try:
        return list(value)
    except TypeError:
        return str(value)


def _update_view_layer():
    view_layer = getattr(bpy.context, "view_layer", None)
    if view_layer is not None:
        view_layer.update()


def _matrix_list(matrix):
    return [[float(value) for value in row] for row in matrix]


def _matrix_close(first, second, tolerance=1e-5):
    return all(abs(first[row][column] - second[row][column]) <= tolerance for row in range(4) for column in range(4))


def _transform_info(obj):
    _update_view_layer()
    world_location, world_rotation, world_scale = obj.matrix_world.decompose()
    return {
        "local": {
            "location": list(obj.location),
            "rotation_mode": obj.rotation_mode,
            "rotation": rotation_as_native_list(obj),
            "scale": list(obj.scale),
            "matrix": _matrix_list(obj.matrix_basis),
        },
        "world": {
            "location": list(world_location),
            "rotation_quaternion": list(world_rotation),
            "scale": list(world_scale),
            "matrix": _matrix_list(obj.matrix_world),
        },
    }


def _constraint_info(constraint):
    result = {
        "name": constraint.name,
        "type": constraint.type,
        "influence": float(constraint.influence),
        "mute": bool(constraint.mute),
        "is_valid": bool(constraint.is_valid),
        "owner_space": getattr(constraint, "owner_space", None),
        "target_space": getattr(constraint, "target_space", None),
    }
    for field in (
        "target",
        "subtarget",
        "track_axis",
        "up_axis",
        "lock_axis",
        "forward_axis",
        "use_curve_follow",
        "use_fixed_location",
        "offset_factor",
    ):
        if hasattr(constraint, field):
            result[field] = _plain(getattr(constraint, field))
    return result


def _driver_records(obj):
    animation = getattr(obj, "animation_data", None)
    records = []
    for driver_curve in getattr(animation, "drivers", ()) if animation is not None else ():
        driver = driver_curve.driver
        variables = []
        for variable in driver.variables:
            variables.append(
                {
                    "name": variable.name,
                    "type": variable.type,
                    "targets": [
                        {
                            "id": getattr(target.id, "name", None),
                            "data_path": target.data_path,
                            "bone_target": target.bone_target,
                            "transform_type": target.transform_type,
                            "transform_space": target.transform_space,
                        }
                        for target in variable.targets
                    ],
                }
            )
        records.append(
            {
                "data_path": driver_curve.data_path,
                "array_index": driver_curve.array_index,
                "expression": driver.expression,
                "type": driver.type,
                "variables": variables,
            }
        )
    return records


def _action_records(owner_label, owner, max_records=None):
    if max_records is not None and max_records <= 0:
        return []
    animation = getattr(owner, "animation_data", None)
    action = getattr(animation, "action", None) if animation is not None else None
    if action is None:
        return []
    records = []
    curves = getattr(action, "fcurves", ())
    for curve in curves:
        if max_records is not None and len(records) >= max_records:
            break
        records.append(
            {
                "owner": owner_label,
                "action": action.name,
                "data_path": curve.data_path,
                "array_index": curve.array_index,
                "keyframes": len(curve.keyframe_points),
            }
        )
    if not records:
        records.append(
            {
                "owner": owner_label,
                "action": action.name,
                "frame_range": list(action.frame_range),
                "layered": bool(getattr(action, "is_action_layered", False)),
            }
        )
    return records


def _camera_settings(data):
    fields = ["type", *_CAMERA_OPTICS, *_CAMERA_DISPLAY]
    settings = {field: _plain(getattr(data, field)) for field in fields if hasattr(data, field)}
    dof = data.dof
    settings["dof"] = {
        field: _plain(getattr(dof, field))
        for field in [*_DOF_FIELDS, "focus_object", "focus_distance"]
        if hasattr(dof, field)
    }
    return settings


_DOF_FIELDS = {
    "use_dof",
    "aperture_fstop",
    "aperture_blades",
    "aperture_rotation",
    "aperture_ratio",
}


def _tag(obj, rig_id, role):
    obj["mcp_camera_rig_id"] = rig_id
    obj["mcp_camera_role"] = role
    obj["mcp_camera_schema_version"] = _RIG_SCHEMA_VERSION
    obj["mcp_camera_owner"] = "blender-mcp"


def _rig_metadata(obj):
    # Blender ID datablocks expose custom properties through keys(), but are not
    # themselves iterable like a normal mapping.
    return {
        key: _plain(obj[key])
        for key in obj.keys()  # ruff: ignore[in-dict-keys]
        if key.startswith("mcp_camera_")
    }


def _descendants(root, max_depth):
    found = []
    frontier = [(root, 0)]
    while frontier:
        parent, depth = frontier.pop(0)
        if depth >= max_depth:
            continue
        for child in sorted(parent.children, key=lambda item: item.name):
            if len(found) >= _MAX_RIG_DESCENDANTS:
                return found, True
            found.append((child, depth + 1))
            frontier.append((child, depth + 1))
    return found, False


def _parent_hierarchy(obj):
    names = []
    parent = obj.parent
    while parent is not None:
        names.append(parent.name)
        parent = parent.parent
    return names


def _look_quaternion(origin, target):
    direction = target - origin
    if direction.length_squared <= 1e-16:
        raise ValueError("Camera and aim target cannot occupy the same world position")
    return direction.to_track_quat("-Z", "Y")


def _patch_values(owner, patch, allowed):
    patch = patch or {}
    unknown = set(patch) - allowed
    if unknown:
        raise ValueError(f"Unsupported fields: {sorted(unknown)}")
    missing = [field for field in patch if not hasattr(owner, field)]
    if missing:
        raise ValueError(f"Running Blender does not support fields: {missing}")
    old = {field: _plain(getattr(owner, field)) for field in patch}
    assigned = []
    try:
        for field, value in patch.items():
            setattr(owner, field, value)
            assigned.append(field)
    except Exception:
        for field in reversed(assigned):
            setattr(owner, field, old[field])
        raise
    new = {field: _plain(getattr(owner, field)) for field in patch}
    return old, new


def _validate_display(patch):
    patch = dict(patch or {})
    unknown = set(patch) - _CAMERA_DISPLAY
    if unknown:
        raise ValueError(f"Unsupported camera display fields: {sorted(unknown)}")
    if "passepartout_alpha" in patch:
        alpha = _finite_number(patch["passepartout_alpha"], "passepartout_alpha")
        if not 0 <= alpha <= 1:
            raise ValueError("passepartout_alpha must be between 0 and 1")
    return patch


def _add_constraint(
    camera,
    target,
    *,
    name,
    constraint_type,
    track_axis="TRACK_NEGATIVE_Z",
    up_axis="UP_Y",
    lock_axis="LOCK_Y",
    influence=1.0,
    owner_space="WORLD",
    target_space="WORLD",
    stack_index=-1,
    subtarget=None,
):
    if constraint_type not in _TRACK_CONSTRAINTS:
        raise ValueError(f"Unsupported tracking constraint: {constraint_type}")
    _required_name(name, "constraint_name")
    influence = _finite_number(influence, "influence")
    if not 0 <= influence <= 1:
        raise ValueError("influence must be between 0 and 1")
    if subtarget:
        bones = getattr(getattr(target, "data", None), "bones", None)
        if target.type != "ARMATURE" or bones is None or bones.get(subtarget) is None:
            raise ValueError(f"Bone subtarget '{subtarget}' does not exist on armature target '{target.name}'")
    existing = camera.constraints.get(name)
    if existing is not None and existing.type != constraint_type:
        raise ValueError(f"Constraint '{name}' already exists with type {existing.type}; use a different stable name")
    constraint = existing or camera.constraints.new(type=constraint_type)
    created = existing is None
    if created:
        constraint.name = name
    old = {}
    fields = {
        "target": target,
        "subtarget": subtarget or "",
        "track_axis": track_axis,
        "influence": influence,
        "owner_space": owner_space,
        "target_space": target_space,
    }
    if constraint_type == "TRACK_TO":
        fields["up_axis"] = up_axis
    if constraint_type == "LOCKED_TRACK":
        fields["lock_axis"] = lock_axis
    fields = {field: value for field, value in fields.items() if hasattr(constraint, field)}
    try:
        for field in fields:
            old[field] = getattr(constraint, field)
        for field, value in fields.items():
            setattr(constraint, field, value)
        if stack_index >= 0:
            source = list(camera.constraints).index(constraint)
            destination = min(stack_index, len(camera.constraints) - 1)
            camera.constraints.move(source, destination)
    except Exception:
        if created:
            camera.constraints.remove(constraint)
        else:
            for field, value in old.items():
                setattr(constraint, field, value)
        raise
    return constraint


def _snapshot_constraint(owner, name):
    constraint = owner.constraints.get(name)
    if constraint is None:
        return None
    fields = {}
    for field in (
        "target",
        "subtarget",
        "track_axis",
        "up_axis",
        "lock_axis",
        "influence",
        "owner_space",
        "target_space",
    ):
        if hasattr(constraint, field):
            fields[field] = getattr(constraint, field)
    return {"type": constraint.type, "index": list(owner.constraints).index(constraint), "fields": fields}


def _restore_constraint(owner, name, snapshot):
    constraint = owner.constraints.get(name)
    if snapshot is None:
        if constraint is not None:
            owner.constraints.remove(constraint)
        return
    if constraint is None or constraint.type != snapshot["type"]:
        if constraint is not None:
            owner.constraints.remove(constraint)
        constraint = owner.constraints.new(type=snapshot["type"])
        constraint.name = name
    for field, value in snapshot["fields"].items():
        setattr(constraint, field, value)
    owner.constraints.move(list(owner.constraints).index(constraint), snapshot["index"])


def _new_empty(collection, name, location, rig_id, role, *, display_type="PLAIN_AXES", size=0.5):
    obj = bpy.data.objects.new(name, None)
    collection.objects.link(obj)
    obj.location = location
    obj.empty_display_type = display_type
    obj.empty_display_size = size
    _tag(obj, rig_id, role)
    return obj
