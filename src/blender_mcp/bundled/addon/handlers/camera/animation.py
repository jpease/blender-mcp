# pyright: reportGeneralTypeIssues=false, reportOptionalSubscript=false
"""Keyframing, interpolation styling, and time-based camera effects (focus pulls, dolly zoom, shake)."""

import math
import uuid

import bpy
import mathutils

from ..key_style import KeyStyle, style_point
from ._shared import (
    _camera,
    _ensure_collection,
    _finite_number,
    _frame,
    _matrix_close,
    _object,
    _required_name,
    _resolve_frame_from_time,
    _scene,
    _tag,
    _update_view_layer,
    _vector,
)

_OBJECT_CHANNELS = {
    "location": 3,
    "rotation_euler": 3,
    "rotation_quaternion": 4,
    "scale": 3,
}
_CAMERA_CHANNELS = {
    "lens",
    "ortho_scale",
    "shift_x",
    "shift_y",
    "clip_start",
    "clip_end",
}
_DOF_CHANNELS = {"focus_distance", "aperture_fstop"}
_CONSTRAINT_CHANNELS = {"influence", "offset_factor"}
# Blender's own `Camera.lens` soft range; a solved dolly-zoom pair outside it cannot be keyed.
_MIN_LENS_MM = 1
_MAX_LENS_MM = 10_000
# Below this squared length the camera and its subject are the same point to float precision,
# so the direction "away from the subject" is undefined rather than merely small.
_COINCIDENT_SQUARED_LENGTH = 1e-16
# Blender rejects a zero focus distance, and a subject exactly on the sensor plane is the
# only way to ask for one; focus a micrometre out instead of failing the pull.
_MIN_FOCUS_DISTANCE_M = 1e-6


def _animation_owner(record):
    obj = _object(record["object_name"])
    owner_kind = record.get("owner", "OBJECT")
    data_path = record["data_path"]
    constraint = None
    if owner_kind == "OBJECT":
        expected = _OBJECT_CHANNELS.get(data_path)
        if expected is None:
            raise ValueError(f"Unsupported object animation path: {data_path}")
        owner = obj
        fcurve_path = data_path
    elif owner_kind == "CAMERA_DATA":
        if obj.type != "CAMERA" or data_path not in _CAMERA_CHANNELS:
            raise ValueError(f"Unsupported camera-data animation path: {data_path}")
        owner = obj.data
        expected = 1
        fcurve_path = data_path
    elif owner_kind == "DOF":
        if obj.type != "CAMERA" or data_path not in _DOF_CHANNELS:
            raise ValueError(f"Unsupported depth-of-field animation path: {data_path}")
        owner = obj.data.dof
        expected = 1
        fcurve_path = owner.path_from_id(data_path)
    elif owner_kind == "CONSTRAINT":
        constraint_name = _required_name(record.get("constraint_name"), "constraint_name")
        constraint = obj.constraints.get(constraint_name)
        if constraint is None:
            raise ValueError(f"Constraint not found on '{obj.name}': {constraint_name}")
        if data_path not in _CONSTRAINT_CHANNELS or not hasattr(constraint, data_path):
            raise ValueError(f"Unsupported constraint animation path: {data_path}")
        owner = constraint
        expected = 1
        fcurve_path = constraint.path_from_id(data_path)
    else:
        raise ValueError(f"Unsupported animation owner: {owner_kind}")
    return obj, owner, expected, fcurve_path, constraint


def _normalized_key_value(value, expected, array_index, label):
    if isinstance(value, (list, tuple)):
        values = tuple(_finite_number(item, label) for item in value)
        if array_index is not None:
            raise ValueError(f"{label}: array_index requires a scalar value")
        if len(values) != expected:
            raise ValueError(f"{label}: expected {expected} values")
        return values
    scalar = _finite_number(value, label)
    if expected > 1 and array_index is None:
        raise ValueError(f"{label}: vector channels require a full vector or array_index")
    if array_index is not None and array_index >= expected:
        raise ValueError(f"{label}: array_index is outside the channel")
    return scalar


def _action_fcurves(id_owner):
    animation = getattr(id_owner, "animation_data", None)
    action = getattr(animation, "action", None) if animation is not None else None
    if action is None:
        return None, ()
    if hasattr(action, "fcurves"):
        return action, action.fcurves
    slot = getattr(animation, "action_slot", None)
    curves = []
    for layer in getattr(action, "layers", ()):
        for strip in getattr(layer, "strips", ()):
            if getattr(strip, "type", None) != "KEYFRAME":
                continue
            channelbag = strip.channelbag(slot, ensure=False) if slot is not None else None
            if channelbag is not None:
                curves.extend(channelbag.fcurves)
    return action, curves


def _find_fcurve(id_owner, data_path, array_index):
    _action, curves = _action_fcurves(id_owner)
    for curve in curves:
        if curve.data_path == data_path and (array_index is None or curve.array_index == array_index):
            return curve
    return None


def _key_at(curve, frame):
    if curve is None:
        return None
    return next((point for point in curve.keyframe_points if abs(point.co[0] - frame) <= 1e-5), None)


def _set_key_style(id_owner, data_path, frame, array_indices, style):
    changed = []
    for index in array_indices:
        curve = _find_fcurve(id_owner, data_path, index)
        point = _key_at(curve, frame)
        if point is None:
            continue
        style_point(point, style)
        changed.append({"data_path": data_path, "array_index": index, "frame": frame})
    return changed


def _assign_and_key(record, resolved, style):
    obj, owner, expected, fcurve_path, constraint = resolved
    data_path = record["data_path"]
    array_index = record.get("array_index")
    value = record["_value"]
    frame = record["frame"]
    old = getattr(owner, data_path)
    if array_index is None:
        setattr(owner, data_path, value)
    else:
        old = old[array_index]
        getattr(owner, data_path)[array_index] = value
    kwargs = {"data_path": data_path, "frame": frame}
    if array_index is not None:
        kwargs["index"] = array_index
    if not owner.keyframe_insert(**kwargs):
        if array_index is None:
            setattr(owner, data_path, old)
        else:
            getattr(owner, data_path)[array_index] = old
        raise RuntimeError(f"Blender refused keyframe insertion for {obj.name}:{fcurve_path} at {frame}")
    id_owner = obj if constraint is not None else (obj.data if record["owner"] in {"CAMERA_DATA", "DOF"} else obj)
    indices = [array_index] if array_index is not None else list(range(expected))
    return _set_key_style(id_owner, fcurve_path, frame, indices, style)


def _subject_point(scene, object_name, point):
    if object_name is None:
        return _vector(point, "subject_point")
    target = _object(object_name, scene=scene)
    evaluated = target.evaluated_get(bpy.context.evaluated_depsgraph_get())
    return evaluated.matrix_world.translation.copy()


def _focus_depth(camera, point):
    local = camera.matrix_world.inverted_safe() @ point
    return -float(local.z)


def _set_interpolation_on_keys(id_owner, data_path, frames, style):
    action, curves = _action_fcurves(id_owner)
    if action is None:
        return []
    changed = []
    for curve in curves:
        if curve.data_path != data_path:
            continue
        for point in curve.keyframe_points:
            if any(abs(point.co[0] - frame) <= 1e-5 for frame in frames):
                style_point(point, style)
                changed.append({"data_path": data_path, "array_index": curve.array_index, "frame": point.co[0]})
    return changed


def _validated_focus_targets(scene, subjects):
    """
    Check that each end of a focus pull names exactly one resolvable subject.

    Args:
        scene: The scene the subjects are resolved in.
        subjects: `(label, object_name, point)` for each end of the pull.

    Raises:
        ValueError: If an end names neither or both forms, or names an object that is
            not in the scene.

    """
    for label, object_name, point in subjects:
        if (object_name is None) == (point is None):
            raise ValueError(f"Supply exactly one {label} subject or point")
        if object_name is not None:
            _object(object_name, scene=scene)
        else:
            _vector(point, f"{label}_point")


def _focus_depth_warnings(camera, depths):
    """
    Say when a focus plane sits behind the camera or outside its clip interval.

    Neither is an error - Blender focuses on the absolute depth either way - but both
    produce a pull that does not look focused, so they are reported rather than silently
    keyed.

    Args:
        camera: The camera whose clip interval the depths are judged against.
        depths: `(label, camera_space_depth)` for each end of the pull.

    Returns:
        list[str]: One notice per problem found, in the order the ends were given.

    """
    warnings = []
    for label, depth in depths:
        if depth <= 0:
            warnings.append(f"The {label} subject is behind the camera; focus uses the absolute camera-space depth.")
        distance = abs(depth)
        if distance < camera.data.clip_start or distance > camera.data.clip_end:
            warnings.append(f"The {label} focus plane is outside the camera clip interval.")
    return warnings


def _validated_dolly_optics(camera, start_distance, end_distance, reference_size, start_lens):
    """
    Check the dolly zoom's distances and solve its two focal lengths before anything moves.

    The lens pair is what the effect is: holding the subject's projected size while the
    camera travels means `lens_end/lens_start` equals `end_distance/start_distance`. A pair
    outside Blender's 1-10000 mm range cannot be keyed, so it is refused here rather than
    clamped into a shot that no longer holds the framing.

    Args:
        camera: The camera object, read for its current lens when start_lens is omitted.
        start_distance: Camera-to-subject distance at the start frame, in metres.
        end_distance: The same at the end frame.
        reference_size: The subject's real size the framing is held against, in metres.
        start_lens: The focal length to start from, or None to keep the camera's own.

    Returns:
        tuple: start_distance, end_distance, reference_size, lens_start and lens_end, each
        validated and coerced to float.

    Raises:
        ValueError: If a distance or the reference size is not positive, or if either
            solved focal length leaves Blender's range.

    """
    start_distance = _finite_number(start_distance, "start_distance")
    end_distance = _finite_number(end_distance, "end_distance")
    if start_distance <= 0 or end_distance <= 0:
        raise ValueError("Dolly-zoom distances must be positive")
    reference_size = _finite_number(reference_size, "subject_reference_size")
    if reference_size <= 0:
        raise ValueError("subject_reference_size must be positive")
    lens_start = camera.data.lens if start_lens is None else _finite_number(start_lens, "start_lens")
    lens_end = lens_start * end_distance / start_distance
    if not _MIN_LENS_MM <= lens_start <= _MAX_LENS_MM or not _MIN_LENS_MM <= lens_end <= _MAX_LENS_MM:
        raise ValueError(f"Solved focal lengths must remain within Blender's {_MIN_LENS_MM}-{_MAX_LENS_MM} mm range")
    return start_distance, end_distance, reference_size, lens_start, lens_end


def _dolly_zoom_sample(scene, camera, mover, sample, subject, projected_fraction):
    """
    Place and key one end of a dolly zoom, then measure what Blender actually evaluated.

    The requested camera position is written through `mover`, which may be a parent or rig
    empty, so constraints and parenting can land the camera somewhere else. That is measured
    rather than assumed: the record carries both the requested and the evaluated position.

    Args:
        scene: The scene, whose playhead this moves to the sample's frame.
        camera: The camera whose lens is keyed.
        mover: The object actually translated - the camera or something it hangs from.
        sample: `(frame, distance, lens)` for this end of the move.
        subject: `(subject_object_name, subject_point, reference_size)`.
        projected_fraction: Callable returning the subject's projected frame fraction for a
            distance and lens pair.

    Returns:
        tuple: The solution record for this frame, and the notices it raised.

    """
    frame_value, distance, lens = sample
    subject_object_name, subject_point, reference_size = subject
    scene.frame_set(frame_value)
    point = _subject_point(scene, subject_object_name, subject_point)
    camera_location = camera.matrix_world.translation.copy()
    away = camera_location - point
    if away.length_squared <= _COINCIDENT_SQUARED_LENGTH:
        # The camera sits on the subject, so "away from it" names no direction; fall back to
        # the camera's own backwards axis, which is where it would retreat to.
        away = -(camera.matrix_world.to_quaternion() @ mathutils.Vector((0.0, 0.0, -1.0)))
    away.normalize()
    desired_camera_location = point + away * distance
    mover.matrix_world.translation += desired_camera_location - camera_location
    mover.keyframe_insert(data_path="location", frame=frame_value)
    camera.data.lens = lens
    camera.data.keyframe_insert(data_path="lens", frame=frame_value)
    _update_view_layer()
    actual_camera_location = camera.matrix_world.translation.copy()
    actual_distance = (actual_camera_location - point).length
    warnings = []
    if not math.isclose(actual_distance, distance, rel_tol=1e-4, abs_tol=1e-4):
        warnings.append(
            f"At frame {frame_value}, constraints or parenting produced distance "
            f"{actual_distance:.6g} instead of {distance:.6g}."
        )
    if distance < camera.data.clip_start or distance > camera.data.clip_end:
        warnings.append(f"The subject distance at frame {frame_value} is outside the camera clip interval.")
    return {
        "frame": frame_value,
        "distance": distance,
        "lens": lens,
        "subject_reference_size": reference_size,
        "projected_frame_fraction": projected_fraction(distance, lens),
        "subject_point": list(point),
        "camera_location": list(desired_camera_location),
        "evaluated_camera_location": list(actual_camera_location),
        "evaluated_distance": actual_distance,
    }, warnings


def _projected_fraction(camera, framing_axis, reference_size):
    """
    Build the function that says how much of the frame the subject fills.

    The camera's current lens and field of view are read once, here, because the dolly zoom
    then changes both: sampling them inside the loop would measure the shot being built
    rather than the one it started from.

    Args:
        camera: The camera whose current lens and field of view set the reference framing.
        framing_axis: HORIZONTAL or VERTICAL - which field of view the fraction is of.
        reference_size: The subject's real size, in metres.

    Returns:
        Callable: `(distance, lens) -> fraction of the frame the subject spans`.

    """
    current_lens = camera.data.lens
    current_angle = camera.data.angle_x if framing_axis == "HORIZONTAL" else camera.data.angle_y
    current_tangent = math.tan(current_angle / 2)

    def projected_fraction(distance, lens):
        half_frame = distance * current_tangent * current_lens / lens
        return reference_size / (2 * half_frame)

    return projected_fraction


def _solve_dolly_samples(scene, camera, mover, samples, subject, projected_fraction):
    """
    Place, key and measure both ends of a dolly zoom.

    Args:
        scene: The scene being keyed; its playhead moves to each sample's frame.
        camera: The camera whose lens is keyed.
        mover: The object actually translated.
        samples: `(frame, distance, lens)` per end.
        subject: `(subject_object_name, subject_point, reference_size)`.
        projected_fraction: `_projected_fraction`'s callable.

    Returns:
        dict: solutions, one record per end, and warnings, every notice they raised.

    """
    solutions = []
    warnings = []
    for sample in samples:
        solution, sample_warnings = _dolly_zoom_sample(scene, camera, mover, sample, subject, projected_fraction)
        solutions.append(solution)
        warnings.extend(sample_warnings)
    return {"solutions": solutions, "warnings": warnings}


def _focus_pull_samples(scene, camera, ends):
    """
    Read each end's subject position and camera-space depth at that end's own frame.

    The playhead has to move: a subject that is itself animated, or parented to something
    that is, sits somewhere different at the two ends, and focusing both on the current
    frame's position is the bug this exists to avoid.

    Args:
        scene: The scene whose playhead is moved to each end's frame.
        camera: The camera the depth is measured in front of.
        ends: `(label, frame, subject_object_name, subject_point)` per end of the pull.

    Returns:
        list[dict]: One record per end, in order: label, frame, world (the subject's
        world-space position at that frame) and depth (its camera-space depth, negative
        behind the camera).

    """
    samples = []
    for label, frame_value, subject_object_name, subject_point in ends:
        scene.frame_set(frame_value)
        world = _subject_point(scene, subject_object_name, subject_point)
        samples.append({"label": label, "frame": frame_value, "world": world, "depth": _focus_depth(camera, world)})
    return samples


def _key_focus_distance(camera, samples, style):
    """
    Focus the camera by keying `dof.focus_distance` itself, with no helper object.

    Args:
        camera: The camera whose depth-of-field is keyed.
        samples: `(frame, camera_space_depth)` for each end of the pull.
        style: The `KeyStyle` the keys written here are shaped with.

    Returns:
        list[dict]: The styled keys, as `_set_interpolation_on_keys` reports them.

    Raises:
        RuntimeError: If Blender refuses a focus-distance key, which would leave the pull
            holding one end and not the other.

    """
    camera.data.dof.use_dof = True
    camera.data.dof.focus_object = None
    path = camera.data.dof.path_from_id("focus_distance")
    for frame_value, depth in samples:
        # A subject behind the camera has negative depth; Blender focuses on the distance,
        # and zero is not a legal focus distance.
        camera.data.dof.focus_distance = max(abs(depth), _MIN_FOCUS_DISTANCE_M)
        if not camera.data.dof.keyframe_insert(data_path="focus_distance", frame=frame_value):
            raise RuntimeError(f"Blender refused the focus-distance key at frame {frame_value}")
    return _set_interpolation_on_keys(camera.data, path, tuple(frame for frame, _depth in samples), style)


def _key_focus_control(camera, collection, control_name, samples, style):
    """
    Focus the camera by animating a tagged Empty it tracks, which an artist can then re-aim.

    Args:
        camera: The camera whose depth-of-field is pointed at the control.
        collection: The collection the control is linked into.
        control_name: The Empty's name.
        samples: `(frame, world_point)` for each end of the pull.
        style: The `KeyStyle` the keys written here are shaped with.

    Returns:
        tuple: The created focus-control object and its styled keys.

    """
    focus_control = bpy.data.objects.new(control_name, None)
    collection.objects.link(focus_control)
    _tag(focus_control, str(uuid.uuid4()), "focus_pull")
    focus_control.empty_display_type = "SPHERE"
    for frame_value, world_point in samples:
        focus_control.location = world_point
        focus_control.keyframe_insert(data_path="location", frame=frame_value)
    changed_keys = _set_interpolation_on_keys(
        focus_control, "location", tuple(frame for frame, _point in samples), style
    )
    camera.data.dof.use_dof = True
    camera.data.dof.focus_object = focus_control
    return focus_control, changed_keys


class _AnimationMixin:
    """Provide keyframing, interpolation styling, focus-pull, dolly-zoom, and camera-shake handlers."""

    def keyframe_camera_rig(
        self,
        keyframes,
        policy="REPLACE",
        interpolation="BEZIER",
        handle_left="AUTO_CLAMPED",
        handle_right="AUTO_CLAMPED",
    ):
        if not isinstance(keyframes, list) or not 1 <= len(keyframes) <= 500:
            raise ValueError("keyframes must contain between 1 and 500 records")
        if policy not in {"REPLACE", "INSERT_ONLY"}:
            raise ValueError("policy must be REPLACE or INSERT_ONLY")
        style = KeyStyle(interpolation, handle_left, handle_right)
        style.validate()
        prepared = []
        seen = set()
        scene_cache = {}
        for index, source in enumerate(keyframes):
            record = dict(source)
            scene_name = record.get("scene_name")
            scene = scene_cache.get(scene_name)
            if scene is None:
                scene = _scene(scene_name) if scene_name else bpy.context.scene
                scene_cache[scene_name] = scene
            record["frame"] = _resolve_frame_from_time(
                record.get("frame"), record.get("at_seconds"), f"keyframes[{index}]", scene
            )
            record["_policy"] = policy
            resolved = _animation_owner(record)
            record["_value"] = _normalized_key_value(
                record.get("value"), resolved[2], record.get("array_index"), f"keyframes[{index}].value"
            )
            identity = (
                record["object_name"],
                record.get("owner", "OBJECT"),
                record.get("constraint_name"),
                record["data_path"],
                record.get("array_index"),
                record["frame"],
            )
            if identity in seen:
                raise ValueError(f"Duplicate keyframe destination at record {index}")
            seen.add(identity)
            id_owner = resolved[0].data if record.get("owner") in {"CAMERA_DATA", "DOF"} else resolved[0]
            indices = [record.get("array_index")] if record.get("array_index") is not None else range(resolved[2])
            if policy == "INSERT_ONLY" and any(
                _key_at(_find_fcurve(id_owner, resolved[3], array_index), record["frame"]) is not None
                for array_index in indices
            ):
                raise ValueError(f"A key already exists at record {index}; INSERT_ONLY made no changes")
            prepared.append((record, resolved))

        changed_keys = []
        for record, resolved in prepared:
            changed_keys.extend(_assign_and_key(record, resolved, style))
        changed_objects = list(dict.fromkeys(record["object_name"] for record, _resolved in prepared))
        actions = sorted(
            {
                action.name
                for record, resolved in prepared
                for action in [
                    _action_fcurves(resolved[0].data if record["owner"] in {"CAMERA_DATA", "DOF"} else resolved[0])[0]
                ]
                if action is not None
            }
        )
        return {
            "keyframes": changed_keys,
            "actions": actions,
            "policy": policy,
            "changed_objects": changed_objects,
            "changed_resources": actions,
        }

    def set_camera_interpolation(
        self,
        object_name,
        owner,
        data_path,
        frame_start,
        frame_end,
        array_index=None,
        interpolation="BEZIER",
        handle_left="AUTO_CLAMPED",
        handle_right="AUTO_CLAMPED",
        easing=None,
    ):
        style = KeyStyle(interpolation, handle_left, handle_right, easing)
        style.validate()
        start = _frame(frame_start, "frame_start")
        end = _frame(frame_end, "frame_end")
        if start > end:
            raise ValueError("frame_start must be less than or equal to frame_end")
        obj = _object(object_name)
        if owner == "OBJECT":
            if data_path not in _OBJECT_CHANNELS:
                raise ValueError(f"Unsupported object animation path: {data_path}")
            id_owner = obj
        elif owner == "CAMERA_DATA":
            if obj.type != "CAMERA" or data_path not in _CAMERA_CHANNELS | {f"dof.{path}" for path in _DOF_CHANNELS}:
                raise ValueError(f"Unsupported camera animation path: {data_path}")
            id_owner = obj.data
        else:
            raise ValueError("owner must be OBJECT or CAMERA_DATA")
        action, curves = _action_fcurves(id_owner)
        if action is None:
            raise ValueError(f"{object_name} has no action for {owner}")
        matched_curves = []
        changed = []
        for curve in curves:
            if curve.data_path != data_path or (array_index is not None and curve.array_index != array_index):
                continue
            matched_curves.append({"data_path": curve.data_path, "array_index": curve.array_index})
            for point in curve.keyframe_points:
                if start <= point.co[0] <= end:
                    style_point(point, style)
                    changed.append({"array_index": curve.array_index, "frame": float(point.co[0])})
        if not matched_curves:
            raise ValueError("No matching animation curves were found")
        return {
            "object": obj.name,
            "owner": owner,
            "action": action.name,
            "matched_curves": matched_curves,
            "changed_keys": changed,
            "changed_objects": [obj.name] if changed else [],
            "changed_resources": [action.name] if changed else [],
        }

    def create_focus_pull(
        self,
        scene_name,
        camera_name,
        start_frame=None,
        end_frame=None,
        start_at_seconds=None,
        end_at_seconds=None,
        start_subject_name=None,
        start_point=None,
        end_subject_name=None,
        end_point=None,
        mode="DISTANCE",
        interpolation="BEZIER",
        focus_control_name="MCP Focus Pull",
        collection_name="MCP Camera Controls",
    ):
        scene = _scene(scene_name)
        camera = _camera(camera_name, scene=scene)
        start = _resolve_frame_from_time(start_frame, start_at_seconds, "start", scene)
        end = _resolve_frame_from_time(end_frame, end_at_seconds, "end", scene)
        if start >= end:
            raise ValueError("start must be less than end")
        if mode not in {"DISTANCE", "FOCUS_CONTROL"}:
            raise ValueError("mode must be DISTANCE or FOCUS_CONTROL")
        style = KeyStyle(interpolation)
        style.validate()
        _validated_focus_targets(
            scene,
            (("start", start_subject_name, start_point), ("end", end_subject_name, end_point)),
        )
        if mode == "FOCUS_CONTROL":
            _required_name(focus_control_name, "focus_control_name")
            collection = _ensure_collection(scene, collection_name)
        else:
            collection = None

        original_frame = scene.frame_current
        original_subframe = scene.frame_subframe
        try:
            samples = _focus_pull_samples(
                scene,
                camera,
                (("start", start, start_subject_name, start_point), ("end", end, end_subject_name, end_point)),
            )
            warnings = _focus_depth_warnings(camera, [(item["label"], item["depth"]) for item in samples])
            if mode == "DISTANCE":
                changed_keys = _key_focus_distance(camera, [(item["frame"], item["depth"]) for item in samples], style)
                focus_control = None
            else:
                focus_control, changed_keys = _key_focus_control(
                    camera,
                    collection,
                    focus_control_name,
                    [(item["frame"], item["world"]) for item in samples],
                    style,
                )
        finally:
            scene.frame_set(original_frame, subframe=original_subframe)
        action, _curves = _action_fcurves(camera.data if mode == "DISTANCE" else focus_control)
        return {
            "camera": camera.name,
            "mode": mode,
            **{
                item["label"]: {
                    "frame": item["frame"],
                    "point": list(item["world"]),
                    "camera_space_depth": item["depth"],
                }
                for item in samples
            },
            "focus_control": getattr(focus_control, "name", None),
            "changed_keys": changed_keys,
            "warnings": warnings,
            "changed_objects": [camera.name] + ([focus_control.name] if focus_control is not None else []),
            "changed_resources": [camera.data.name, *([action.name] if action is not None else [])],
        }

    def create_dolly_zoom(
        self,
        scene_name,
        camera_name,
        movement_object_name,
        start_frame=None,
        end_frame=None,
        start_at_seconds=None,
        end_at_seconds=None,
        start_distance=None,
        end_distance=None,
        subject_object_name=None,
        subject_point=None,
        subject_reference_size=1.0,
        start_lens=None,
        framing_axis="VERTICAL",
        interpolation="LINEAR",
    ):
        scene = _scene(scene_name)
        camera = _camera(camera_name, scene=scene)
        mover = _object(movement_object_name, scene=scene)
        if camera.data.type != "PERSP":
            raise ValueError("Dolly zoom requires a perspective camera")
        if framing_axis not in {"HORIZONTAL", "VERTICAL"}:
            raise ValueError("framing_axis must be HORIZONTAL or VERTICAL")
        start = _resolve_frame_from_time(start_frame, start_at_seconds, "start", scene)
        end = _resolve_frame_from_time(end_frame, end_at_seconds, "end", scene)
        if start >= end:
            raise ValueError("start must be less than end")
        if (subject_object_name is None) == (subject_point is None):
            raise ValueError("Supply exactly one subject object or point")
        if subject_object_name is not None:
            _object(subject_object_name, scene=scene)
        else:
            _vector(subject_point, "subject_point")
        start_distance, end_distance, subject_reference_size, lens_start, lens_end = _validated_dolly_optics(
            camera, start_distance, end_distance, subject_reference_size, start_lens
        )
        style = KeyStyle(interpolation)
        style.validate()
        original_frame = scene.frame_current
        original_subframe = scene.frame_subframe
        try:
            solved = _solve_dolly_samples(
                scene,
                camera,
                mover,
                ((start, start_distance, lens_start), (end, end_distance, lens_end)),
                (subject_object_name, subject_point, subject_reference_size),
                _projected_fraction(camera, framing_axis, subject_reference_size),
            )
            changed_keys = [
                *_set_interpolation_on_keys(mover, "location", (start, end), style),
                *_set_interpolation_on_keys(camera.data, "lens", (start, end), style),
            ]
        finally:
            scene.frame_set(original_frame, subframe=original_subframe)
        return {
            "camera": camera.name,
            "movement_object": mover.name,
            "framing_axis": framing_axis,
            "solutions": solved["solutions"],
            "changed_keys": changed_keys,
            "approximation": "Preserves the lens-to-camera-space-distance ratio for the subject reference point.",
            "warnings": solved["warnings"],
            "changed_objects": list(dict.fromkeys([camera.name, mover.name])),
            "changed_resources": [
                camera.data.name,
                *[
                    action.name
                    for action in (_action_fcurves(mover)[0], _action_fcurves(camera.data)[0])
                    if action is not None
                ],
            ],
        }

    def add_camera_shake(
        self,
        scene_name,
        camera_name,
        collection_name,
        control_name,
        frame_start=None,
        frame_end=None,
        frame_start_at_seconds=None,
        frame_end_at_seconds=None,
        translation_strength=(0.02, 0.02, 0.01),
        rotation_strength=(0.01, 0.01, 0.02),
        noise_scale=12.0,
        phase=0.0,
        depth=1,
        influence=1.0,
    ):
        scene = _scene(scene_name)
        camera = _camera(camera_name, scene=scene)
        collection = _ensure_collection(scene, collection_name)
        _required_name(control_name, "control_name")
        start = _resolve_frame_from_time(frame_start, frame_start_at_seconds, "frame_start", scene)
        end = _resolve_frame_from_time(frame_end, frame_end_at_seconds, "frame_end", scene)
        if start >= end:
            raise ValueError("frame_start must be less than frame_end")
        translation = _vector(translation_strength, "translation_strength")
        rotation = _vector(rotation_strength, "rotation_strength")
        if any(value < 0 for value in (*translation, *rotation)):
            raise ValueError("Shake strength components must be non-negative")
        if not any((*translation, *rotation)):
            raise ValueError("At least one shake strength component must be non-zero")
        noise_scale = _finite_number(noise_scale, "noise_scale")
        phase = _finite_number(phase, "phase")
        influence = _finite_number(influence, "influence")
        if noise_scale <= 0 or not 0 <= influence <= 1 or isinstance(depth, bool) or not 0 <= int(depth) <= 8:
            raise ValueError("Invalid noise scale, influence, or depth")

        camera_world = camera.matrix_world.copy()
        original_parent = camera.parent
        original_parent_inverse = camera.matrix_parent_inverse.copy()
        original_basis = camera.matrix_basis.copy()
        control = bpy.data.objects.new(control_name, None)
        collection.objects.link(control)
        control.empty_display_type = "CUBE"
        control.empty_display_size = 0.35
        control.parent = original_parent
        control.matrix_parent_inverse = original_parent_inverse
        control.matrix_basis = mathutils.Matrix.Identity(4)
        _tag(control, str(uuid.uuid4()), "shake_control")
        camera.parent = control
        camera.matrix_parent_inverse = mathutils.Matrix.Identity(4)
        camera.matrix_basis = original_basis
        if not _matrix_close(camera.matrix_world, camera_world):
            camera.matrix_world = camera_world
        control.rotation_mode = "XYZ"
        for path in ("location", "rotation_euler"):
            control.keyframe_insert(data_path=path, frame=start)
            control.keyframe_insert(data_path=path, frame=end)
        action, curves = _action_fcurves(control)
        if action is None:
            raise RuntimeError("Blender did not create an action for the shake control")
        modifiers = []
        strengths = {"location": translation, "rotation_euler": rotation}
        for curve in curves:
            if curve.data_path not in strengths:
                continue
            strength = strengths[curve.data_path][curve.array_index]
            if strength == 0:
                continue
            modifier = curve.modifiers.new(type="NOISE")
            modifier.strength = strength
            modifier.scale = noise_scale
            modifier.phase = phase + curve.array_index * 17.0 + (100.0 if curve.data_path == "rotation_euler" else 0.0)
            modifier.depth = int(depth)
            modifier.blend_type = "ADD"
            modifier.influence = influence
            modifier.use_restricted_range = True
            modifier.frame_start = start
            modifier.frame_end = end
            modifiers.append(
                {
                    "data_path": curve.data_path,
                    "array_index": curve.array_index,
                    "strength": strength,
                    "phase": modifier.phase,
                }
            )
        return {
            "camera": camera.name,
            "control": control.name,
            "action": action.name,
            "noise_modifiers": modifiers,
            "disable": f"Mute action '{action.name}' or set each Noise modifier influence to 0.",
            "changed_objects": [control.name, camera.name],
            "changed_resources": [action.name],
        }
