# Four handlers here bind more than fifteen locals: a framing or constraint call resolves a scene,
# a camera, a subject, a snapshot to restore and a solve result, and naming those is what makes
# them readable. Declared per file as `handlers/rendering.py` and `handlers/animation.py` do.
# ruff: file-ignore[too-many-locals]
# pyright: reportGeneralTypeIssues=false, reportOptionalSubscript=false
"""Aiming, camera-target management, object framing, and generic camera constraints."""

import uuid

from collections import Counter

import bpy
import mathutils

from ...helpers import deforming_meshes, render_exclusion_reason
from ._shared import (
    _CONSTRAINT_TYPES,
    _TARGETED_CONSTRAINTS,
    _TRACK_CONSTRAINTS,
    _add_constraint,
    _camera,
    _constraint_info,
    _ensure_collection,
    _finite_number,
    _look_quaternion,
    _matrix_close,
    _matrix_list,
    _new_empty,
    _object,
    _positive,
    _required_name,
    _restore_constraint,
    _scene,
    _snapshot_constraint,
    _target_world_point,
    _transform_info,
    _update_view_layer,
    _vector,
)

_COPY_CONSTRAINTS = {"COPY_LOCATION", "COPY_ROTATION"}
# The exact keys the server's BoneFrameTarget serialises. Anything else is a caller typo, and a
# silently ignored typo in a framing request is a close-up on the wrong thing.
_BONE_TARGET_FIELDS = {"object_name", "bone_name", "radius_m"}
# The camera projection each framing policy can solve for. MOVE_CAMERA and CHANGE_LENS both
# reason in perspective; only an orthographic camera has a scale to change.
_FRAMING_POLICY_TYPES = {
    "MOVE_CAMERA": "PERSP",
    "CHANGE_LENS": "PERSP",
    "CHANGE_ORTHO_SCALE": "ORTHO",
}
# Camera-local depth under which a point counts as being on or behind the camera plane, where a
# perspective divide stops meaning anything.
_MINIMUM_DEPTH_M = 1e-8
# A margin insets every side of the render frame, so at 0.9 there is no frame left to fit into.
_MAXIMUM_MARGIN = 0.9
# Squared distance under which two world points count as the same point. It is deliberately the
# same floor _look_quaternion applies to its aim direction, so a camera placement this handler
# accepts can never be rejected a line later by the aim it was computed for.
_COINCIDENT_DISTANCE_SQUARED = 1e-16
# How many meshes excluded_objects names, and how many named-but-hidden objects one warning lists.
# A production rig carries dozens of cages, proxies and widget meshes; naming every one would crowd
# the rest of the reply out of its budget, so the reply names this many and counts the rest.
_EXCLUDED_OBJECTS_LIMIT = 16


def _set_world_rotation(obj, rotation):
    location, _old_rotation, scale = obj.matrix_world.decompose()
    obj.matrix_world = mathutils.Matrix.LocRotScale(location, rotation, scale)


def _set_world_location(obj, location):
    # The translation twin of _set_world_rotation, and written the same way for the same reason:
    # assigning matrix_world makes Blender solve the local channel through the parent chain, so a
    # camera parented to a rig root, a crane arm, or a path follower still lands on the requested
    # world point. Assigning .location instead would be read in parent space and quietly put the
    # camera somewhere else entirely, which is precisely the failure this argument exists to avoid.
    _old_location, rotation, scale = obj.matrix_world.decompose()
    obj.matrix_world = mathutils.Matrix.LocRotScale(location, rotation, scale)


def _object_bound_points(objects, depsgraph):
    """World-space bound-box corners of every object as the depsgraph evaluates it, and each one's owner."""
    points, sources = [], []
    for obj in objects:
        evaluated = obj.evaluated_get(depsgraph)
        matrix = evaluated.matrix_world
        corners = [matrix @ mathutils.Vector(corner) for corner in evaluated.bound_box]
        points.extend(corners)
        sources.extend([obj.name] * len(corners))
    return points, sources


def _bounds_of(points):
    if not points:
        raise ValueError("The requested objects have no evaluable bounds")
    minimum = mathutils.Vector(tuple(min(point[index] for point in points) for index in range(3)))
    maximum = mathutils.Vector(tuple(max(point[index] for point in points) for index in range(3)))
    return minimum, maximum, (minimum + maximum) * 0.5


def _evaluated_bounds(objects):
    _update_view_layer()
    points, _sources = _object_bound_points(objects, bpy.context.evaluated_depsgraph_get())
    minimum, maximum, center = _bounds_of(points)
    return points, minimum, maximum, center


def _bone_target_specs(bone_targets, scene):
    """
    Resolve bone targets to (armature object, bone name, radius) before anything is moved.

    Every refusal a bone target can earn — a missing object, an object that is not an armature, a
    bone the armature does not have — is raised here, so a bad target never reaches the solve and
    the camera is never touched on its way to being rejected.
    """
    specs = []
    for index, target in enumerate(bone_targets or []):
        label = f"bone_targets[{index}]"
        if not isinstance(target, dict):
            raise ValueError(f"{label} must be an object with object_name and bone_name")
        unsupported = set(target) - _BONE_TARGET_FIELDS
        if unsupported:
            raise ValueError(f"{label} has unsupported fields: {sorted(unsupported)}")
        object_name = _required_name(target.get("object_name"), f"{label}.object_name")
        bone_name = _required_name(target.get("bone_name"), f"{label}.bone_name")
        radius = _positive(target.get("radius_m", 0.0) or 0.0, f"{label}.radius_m", allow_zero=True)
        obj = _object(object_name, scene=scene)
        if obj.type != "ARMATURE":
            raise ValueError(f"{label} object '{object_name}' is not an armature (type={obj.type})")
        if obj.pose is None or obj.pose.bones.get(bone_name) is None:
            raise ValueError(f"{label} bone '{bone_name}' does not exist on armature '{object_name}'")
        specs.append((obj, bone_name, radius))
    if len({(obj.name, bone_name) for obj, bone_name, _radius in specs}) != len(specs):
        raise ValueError("bone_targets must not name the same bone on the same armature twice")
    return specs


def _padded_points(point, radius):
    """Expand a point into the corners of the axis-aligned cube of half-size radius around it."""
    if radius <= 0.0:
        return [point]
    return [
        point + mathutils.Vector((x, y, z))
        for x in (-radius, radius)
        for y in (-radius, radius)
        for z in (-radius, radius)
    ]


def _bone_target_points(specs, depsgraph):
    """
    World head and tail of each resolved bone, padded by its radius, their owner, and a record each.

    The head and tail are read from the depsgraph-evaluated armature, so a bone driven by
    constraints, drivers or an action reports where it actually is at the current frame rather
    than where its rest pose would put it. Every point is owned by the bone's armature object.
    """
    points, sources, records = [], [], []
    for obj, bone_name, radius in specs:
        evaluated = obj.evaluated_get(depsgraph)
        bone = evaluated.pose.bones[bone_name]
        matrix = evaluated.matrix_world
        head = matrix @ bone.head
        tail = matrix @ bone.tail
        segment = [*_padded_points(head, radius), *_padded_points(tail, radius)]
        points.extend(segment)
        sources.extend([obj.name] * len(segment))
        records.append(
            {
                "object_name": obj.name,
                "bone_name": bone_name,
                "radius_m": radius,
                "head_world": list(head),
                "tail_world": list(tail),
            }
        )
    return points, sources, records


def _armature_mesh_specs(armature_names, scene, *, include_hidden, named):
    """
    Resolve each armature name to the scene meshes it deforms that render, before anything is moved.

    Framing an armature object by name is not the same request: an armature's bound box is its
    bones, not the silhouette the camera sees, so a full-body frame has to expand to the deformed
    geometry. Every refusal that expansion can earn - a missing object, an object that is not an
    armature, a rig that deforms nothing in this scene, a rig whose every deformed mesh is left out
    of the render - is raised here, because a silently empty expansion is a frame on whatever else
    the caller happened to name.

    A rig binds more than its visible skin: collision cages, simulation proxies and helper meshes
    are armature-deformed too, and a proxy standing metres off the body would otherwise pull the
    frame wide around geometry the render never shows. Each mesh is therefore kept only when
    `render_exclusion_reason` finds nothing, unless `include_hidden` is set or the caller named
    the mesh in object_names; the rest are returned with the reason they were left out.

    Returns:
        list[tuple]: `(armature name, kept meshes, [(excluded mesh, reason code), ...])` per rig.

    """
    names = [_required_name(name, f"armature_names[{index}]") for index, name in enumerate(armature_names or [])]
    if len(set(names)) != len(names):
        raise ValueError("armature_names must not contain duplicates")
    specs = []
    for index, name in enumerate(names):
        label = f"armature_names[{index}]"
        armature = _object(name, scene=scene)
        if armature.type != "ARMATURE":
            raise ValueError(f"{label} object '{name}' is not an armature (type={armature.type})")
        meshes = [mesh for mesh, _binding, _enabled in deforming_meshes(armature, scene)]
        if not meshes:
            raise ValueError(
                f"{label} armature '{armature.name}' deforms no mesh in scene '{scene.name}'; "
                f"name the meshes in object_names, or a bone of '{armature.name}' in bone_targets"
            )
        kept, excluded = [], []
        for mesh in meshes:
            reason = None
            if not include_hidden and mesh.name not in named:
                reason = render_exclusion_reason(mesh, scene, armature=armature)
            if reason is None:
                kept.append(mesh)
            else:
                excluded.append((mesh, reason))
        if not kept:
            counts = Counter(reason for _mesh, reason in excluded)
            summary = ", ".join(f"{reason} x{count}" for reason, count in sorted(counts.items()))
            raise ValueError(
                f"{label} armature '{armature.name}' deforms {len(excluded)} mesh(es) in scene '{scene.name}', "
                f"and every one is left out of the render ({summary}); pass include_hidden=True to frame "
                f"them anyway, or name the meshes to frame in object_names"
            )
        specs.append((armature.name, kept, excluded))
    return specs


def _framing_objects(objects, armature_specs):
    """List the explicitly named objects, then every armature-deformed mesh, each counted once."""
    combined = list(objects)
    seen = {obj.name for obj in combined}
    for _armature_name, meshes, _excluded in armature_specs:
        for mesh in meshes:
            if mesh.name in seen:
                continue
            seen.add(mesh.name)
            combined.append(mesh)
    return combined


def _excluded_records(armature_specs, framed):
    """
    Name every deformed mesh the render-visibility rule left out of the frame, once, with its reason.

    A mesh two rigs deform is listed once, and one that reached the frame by another route - named
    in object_names, or kept through a second rig - is not listed at all: it was framed.
    """
    framed_names = {obj.name for obj in framed}
    reasons = {}
    for _armature_name, _meshes, excluded in armature_specs:
        for mesh, reason in excluded:
            if mesh.name not in framed_names:
                reasons.setdefault(mesh.name, reason)
    return [{"object": name, "reason": reason} for name, reason in reasons.items()]


def _named_render_warnings(objects, scene):
    """
    Warn once about every object named in object_names that the render-visibility rule would drop.

    A named object is always framed - the caller asked for it by name - but a hidden one is the
    usual cause of a frame far wider than the shot, so the reply says which ones and why.
    """
    hidden = []
    for obj in objects:
        reason = render_exclusion_reason(obj, scene)
        if reason is not None:
            hidden.append(f"'{obj.name}' ({reason})")
    if not hidden:
        return []
    listed = ", ".join(hidden[:_EXCLUDED_OBJECTS_LIMIT])
    more = f" and {len(hidden) - _EXCLUDED_OBJECTS_LIMIT} more" if len(hidden) > _EXCLUDED_OBJECTS_LIMIT else ""
    return [
        f"object_names names {len(hidden)} object(s) the render does not show, framed anyway because they "
        f"were named: {listed}{more}; drop them from object_names to frame only what the camera renders"
    ]


def _framing_points(objects, bone_specs):
    """
    Every world point a framing solve must contain, the object owning each, and the bone records.

    A bone target's points are owned by its armature, so the owners name what pushed the bounds out.
    """
    _update_view_layer()
    depsgraph = bpy.context.evaluated_depsgraph_get()
    points, sources = _object_bound_points(objects, depsgraph)
    bone_points, bone_sources, bone_records = _bone_target_points(bone_specs, depsgraph)
    points.extend(bone_points)
    sources.extend(bone_sources)
    minimum, maximum, center = _bounds_of(points)
    return points, sources, minimum, maximum, center, bone_records


def _margin_limits(camera_data, scene, margin):
    frame = camera_data.view_frame(scene=scene)
    perspective = camera_data.type != "ORTHO"
    if perspective:
        xs = [point.x / -point.z for point in frame]
        ys = [point.y / -point.z for point in frame]
    else:
        xs = [point.x for point in frame]
        ys = [point.y for point in frame]
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)
    inset = margin
    return (
        xmin + (xmax - xmin) * inset,
        xmax - (xmax - xmin) * inset,
        ymin + (ymax - ymin) * inset,
        ymax - (ymax - ymin) * inset,
    )


def _frame_contains(local_points, limits, *, perspective):
    xmin, xmax, ymin, ymax = limits
    x_epsilon = max((xmax - xmin) * 1e-6, 1e-9)
    y_epsilon = max((ymax - ymin) * 1e-6, 1e-9)
    for point in local_points:
        if perspective:
            depth = -point.z
            if depth <= _MINIMUM_DEPTH_M:
                return False
            x, y = point.x / depth, point.y / depth
        else:
            x, y = point.x, point.y
        if x < xmin - x_epsilon or x > xmax + x_epsilon or y < ymin - y_epsilon or y > ymax + y_epsilon:
            return False
    return True


def _limiting_axis(local_points, sources, limits, *, perspective):
    """
    Name the frame axis the fit is limited by, and the objects owning its two outermost points.

    The owners are what a caller needs when a solve comes back wider than the shot: the object at
    either end of the tight axis is the one pushing the frame out. A point within a millionth of
    the axis's span of an end counts as on it, so two objects sharing an edge are both named.
    """
    xmin, xmax, ymin, ymax = limits
    x_center, y_center = (xmin + xmax) * 0.5, (ymin + ymax) * 0.5
    x_half, y_half = max((xmax - xmin) * 0.5, 1e-12), max((ymax - ymin) * 0.5, 1e-12)
    projected = []
    for point in local_points:
        depth = -point.z
        projected.append((point.x / depth, point.y / depth) if perspective else (point.x, point.y))
    x_score = max(abs(x - x_center) / x_half for x, _y in projected)
    y_score = max(abs(y - y_center) / y_half for _x, y in projected)
    axis = 0 if x_score >= y_score else 1
    coordinates = [pair[axis] for pair in projected]
    low, high = min(coordinates), max(coordinates)
    tolerance = max((high - low) * 1e-6, 1e-9)
    owners = [
        source
        for source, coordinate in zip(sources, coordinates, strict=True)
        if coordinate <= low + tolerance or coordinate >= high - tolerance
    ]
    return ("HORIZONTAL" if axis == 0 else "VERTICAL"), list(dict.fromkeys(owners))


def _binary_smallest_fit(predicate, low, high):
    for _attempt in range(32):
        if predicate(high):
            break
        high *= 2
    else:
        raise ValueError("Unable to solve a framing distance or scale within a finite range")
    for _iteration in range(60):
        middle = (low + high) * 0.5
        if predicate(middle):
            high = middle
        else:
            low = middle
    return high


def _validated_framing_request(
    scene_name, camera_name, object_names, bone_targets, armature_names, margin, policy, include_hidden
):
    """Resolve and check every framing argument before the camera is touched."""
    scene = _scene(scene_name)
    camera = _camera(camera_name, scene=scene)
    object_names = list(object_names or [])
    if not object_names and not bone_targets and not armature_names:
        raise ValueError("Supply at least one of object_names, bone_targets or armature_names; all were empty")
    if len(set(object_names)) != len(object_names):
        raise ValueError("object_names must not contain duplicates")
    if not isinstance(include_hidden, bool):
        raise ValueError(f"include_hidden must be true or false, not {include_hidden!r}")
    objects = [_object(name, scene=scene) for name in object_names]
    bone_specs = _bone_target_specs(bone_targets, scene)
    armature_specs = _armature_mesh_specs(armature_names, scene, include_hidden=include_hidden, named=set(object_names))
    margin = _finite_number(margin, "margin")
    if not 0 <= margin < _MAXIMUM_MARGIN:
        raise ValueError(f"margin must be in [0, {_MAXIMUM_MARGIN})")
    policy = str(policy).upper()
    if policy not in _FRAMING_POLICY_TYPES:
        raise ValueError(f"Unsupported framing policy: {policy}")
    required_type = _FRAMING_POLICY_TYPES[policy]
    if camera.data.type != required_type:
        raise ValueError(f"{policy} framing requires a {required_type} camera")
    return scene, camera, objects, bone_specs, armature_specs, margin, policy


def _solve_move_camera(camera, scene, points, center, rotation, scale, margin, span):
    """Slide the camera back along its aim until everything fits, leaving its optics alone."""
    inverse_rotation = rotation.conjugated()
    centered = [inverse_rotation @ (point - center) for point in points]
    limits = _margin_limits(camera.data, scene, margin)

    def distance_fits(distance):
        local = [mathutils.Vector((point.x, point.y, point.z - distance)) for point in centered]
        return _frame_contains(local, limits, perspective=True)

    initial_high = max(span, camera.data.clip_start * 2, 1.0)
    distance = _binary_smallest_fit(distance_fits, camera.data.clip_start, initial_high) * 1.00001
    solved_location = center + (rotation @ mathutils.Vector((0.0, 0.0, distance)))
    camera.matrix_world = mathutils.Matrix.LocRotScale(solved_location, rotation, scale)
    return {"distance": distance, "lens": camera.data.lens}


def _solve_change_lens(camera, scene, points, location, rotation, margin):
    """Widen the lens to the longest focal length that still contains everything, in place."""
    inverse_rotation = rotation.conjugated()
    local_points = [inverse_rotation @ (point - location) for point in points]
    if any(point.z >= -_MINIMUM_DEPTH_M for point in local_points):
        raise ValueError("Cannot change lens because at least one bound is on or behind the camera plane")
    minimum_lens = max(float(camera.data.bl_rna.properties["lens"].hard_min), 0.01)
    camera.data.lens = minimum_lens
    if not _frame_contains(local_points, _margin_limits(camera.data, scene, margin), perspective=True):
        raise ValueError("Objects do not fit even at the camera's minimum supported lens")
    # `hard_max` is the float maximum; 60 halvings from there never reach a real focal length.
    low, high = minimum_lens, float(camera.data.bl_rna.properties["lens"].soft_max)
    for _iteration in range(60):
        middle = (low + high) * 0.5
        camera.data.lens = middle
        if _frame_contains(local_points, _margin_limits(camera.data, scene, margin), perspective=True):
            low = middle
        else:
            high = middle
    low = max(minimum_lens, low * 0.99999)
    camera.data.lens = low
    _set_world_rotation(camera, rotation)
    return {"distance": -sum(point.z for point in local_points) / len(local_points), "lens": low}


def _solve_ortho_scale(camera, scene, points, location, rotation, margin, span, original_ortho_scale):
    """Shrink the orthographic width to the smallest that still contains everything."""
    inverse_rotation = rotation.conjugated()
    local_points = [inverse_rotation @ (point - location) for point in points]

    def scale_fits(scale_value):
        camera.data.ortho_scale = scale_value
        return _frame_contains(local_points, _margin_limits(camera.data, scene, margin), perspective=False)

    ortho_scale = _binary_smallest_fit(scale_fits, 1e-6, max(original_ortho_scale, span, 1.0)) * 1.00001
    camera.data.ortho_scale = ortho_scale
    _set_world_rotation(camera, rotation)
    return {"ortho_scale": ortho_scale}


def _verify_framing(camera, scene, points, sources, margin):
    """
    Re-measure the camera state a solve assigned; name the limiting axis and the objects at its ends.

    The solve reasons about the camera it is about to write; this reads the camera Blender
    actually evaluated, so a constraint that overrode the assignment is caught here rather than
    reported as a successful framing.
    """
    _update_view_layer()
    limits = _margin_limits(camera.data, scene, margin)
    local_points = [camera.matrix_world.inverted() @ point for point in points]
    perspective = camera.data.type == "PERSP"
    if not _frame_contains(local_points, limits, perspective=perspective):
        raise ValueError(
            "The assigned camera state does not frame the request; active constraints may be overriding it"
        )
    return _limiting_axis(local_points, sources, limits, perspective=perspective)


class _TargetingMixin:
    """Provide camera aiming, camera-target, object-framing, and constraint handlers."""

    def point_camera_at(
        self,
        scene_name,
        camera_name,
        target_object_name=None,
        target_point=None,
        target_bone_name=None,
        camera_location=None,
    ):
        scene = _scene(scene_name)
        camera = _camera(camera_name, scene=scene)
        if (target_object_name is None) == (target_point is None):
            raise ValueError("Supply exactly one of target_object_name or target_point")
        if target_bone_name and target_object_name is None:
            raise ValueError("target_bone_name requires target_object_name")
        target = _object(target_object_name, scene=scene) if target_object_name is not None else None
        point = (
            _target_world_point(target, target_bone_name)
            if target is not None
            else _vector(target_point, "target_point")
        )
        placement = _vector(camera_location, "camera_location") if camera_location is not None else None
        if placement is not None:
            if (point - placement).length_squared <= _COINCIDENT_DISTANCE_SQUARED:
                # _look_quaternion refuses a zero-length aim direction as well, but it would only do
                # so after the camera had already been moved, and it cannot name the coordinates that
                # collided. Refusing here leaves the camera untouched and hands the caller both
                # points, which is what they need to pick a different vantage.
                raise ValueError(
                    f"camera_location {list(placement)} and the aim target {list(point)} are the same "
                    "world position; a camera cannot look at the point it occupies"
                )
            # _camera() has already run a view-layer update, so the parent chain is evaluated and the
            # world-space write below resolves against a current parent matrix. Blender's matrix_world
            # setter also stores what it is given before deriving the local channel, so the aim reads
            # the new origin back on the next line without a second update.
            _set_world_location(camera, placement)
        _set_world_rotation(camera, _look_quaternion(camera.matrix_world.translation, point))
        return {
            "camera": camera.name,
            "target_object": target.name if target else None,
            "target_point": list(point),
            "transform": _transform_info(camera),
            "changed_objects": [camera.name],
        }

    def create_camera_target(
        self,
        scene_name,
        collection_name,
        name,
        location=None,
        target_object_name=None,
        use_evaluated_bounds_center=True,
        reuse=False,
        camera_names=None,
        constraint_type="DAMPED_TRACK",
    ):
        scene = _scene(scene_name)
        _required_name(name, "name")
        if (location is None) == (target_object_name is None):
            raise ValueError("Supply exactly one of location or target_object_name")
        source = _object(target_object_name, scene=scene) if target_object_name is not None else None
        camera_names = camera_names or []
        if len(set(camera_names)) != len(camera_names):
            raise ValueError("camera_names must not contain duplicates")
        cameras = [_camera(camera_name, scene=scene) for camera_name in camera_names]
        if constraint_type not in _TRACK_CONSTRAINTS:
            raise ValueError(f"Unsupported tracking constraint: {constraint_type}")
        for camera in cameras:
            existing_constraint = camera.constraints.get(f"MCP Aim: {name}")
            if existing_constraint is not None and existing_constraint.type != constraint_type:
                raise ValueError(f"Constraint 'MCP Aim: {name}' on '{camera.name}' has type {existing_constraint.type}")
        if source is not None and use_evaluated_bounds_center:
            _points, _minimum, _maximum, target_location = _evaluated_bounds([source])
        elif source is not None:
            _update_view_layer()
            target_location = source.matrix_world.translation.copy()
        else:
            target_location = _vector(location, "location")
        existing = bpy.data.objects.get(name)
        target_matrix = existing.matrix_world.copy() if existing is not None else None
        constraint_name = f"MCP Aim: {name}"
        constraint_snapshots = [(camera, _snapshot_constraint(camera, constraint_name)) for camera in cameras]
        created = False
        if existing is not None:
            if not reuse:
                raise ValueError(f"Object '{name}' already exists; set reuse=true only for a tagged target")
            if existing.type != "EMPTY" or existing.get("mcp_camera_role") != "target":
                raise ValueError(f"Object '{name}' is not an Empty tagged as an MCP camera target")
            if existing.name not in scene.objects:
                raise ValueError(f"Existing target '{name}' is not linked to scene '{scene.name}'")
            target = existing
        else:
            collection = _ensure_collection(scene, collection_name)
            target = _new_empty(
                collection,
                name,
                target_location,
                str(uuid.uuid4()),
                "target",
                display_type="SPHERE",
            )
            created = True
        constraints = []
        try:
            target.matrix_world.translation = target_location
            for camera in cameras:
                constraint = _add_constraint(
                    camera,
                    target,
                    name=constraint_name,
                    constraint_type=constraint_type,
                )
                constraints.append({"camera": camera.name, **_constraint_info(constraint)})
        except Exception:
            for camera, snapshot in constraint_snapshots:
                _restore_constraint(camera, constraint_name, snapshot)
            if created:
                bpy.data.objects.remove(target, do_unlink=True)  # pyright: ignore[reportArgumentType]
            elif target_matrix is not None:
                target.matrix_world = target_matrix
            raise
        changed = [target.name, *(camera.name for camera in cameras)]
        return {
            "target": target.name,
            "created": created,
            "location_world": list(target.matrix_world.translation),
            "source_object": source.name if source else None,
            "constraints": constraints,
            "changed_objects": changed,
        }

    def frame_camera_on_objects(
        self,
        scene_name,
        camera_name,
        object_names=None,
        bone_targets=None,
        armature_names=None,
        margin=0.1,
        policy="MOVE_CAMERA",
        aim_at_center=True,
        include_hidden=False,
    ):
        scene, camera, objects, bone_specs, armature_specs, margin, policy = _validated_framing_request(
            scene_name, camera_name, object_names, bone_targets, armature_names, margin, policy, include_hidden
        )
        framed = _framing_objects(objects, armature_specs)
        excluded = _excluded_records(armature_specs, framed)
        warnings = [] if include_hidden else _named_render_warnings(objects, scene)
        points, sources, minimum, maximum, center, bone_records = _framing_points(framed, bone_specs)
        restore = (camera.matrix_world.copy(), camera.data.lens, camera.data.ortho_scale)
        location, rotation, scale = restore[0].decompose()
        if aim_at_center:
            rotation = _look_quaternion(location, center)
        span = (maximum - minimum).length
        try:
            if policy == "MOVE_CAMERA":
                result = _solve_move_camera(camera, scene, points, center, rotation, scale, margin, span)
            elif policy == "CHANGE_LENS":
                result = _solve_change_lens(camera, scene, points, location, rotation, margin)
            else:
                result = _solve_ortho_scale(camera, scene, points, location, rotation, margin, span, restore[2])
            limiting_axis, limiting_objects = _verify_framing(camera, scene, points, sources, margin)
        except Exception:
            camera.matrix_world, camera.data.lens, camera.data.ortho_scale = restore
            raise
        return {
            "camera": camera.name,
            "objects": [obj.name for obj in objects],
            "bone_targets": bone_records,
            "armature_meshes": {
                name: sorted(mesh.name for mesh in meshes) for name, meshes, _excluded in armature_specs
            },
            "framed_objects": [obj.name for obj in framed],
            "excluded_objects": excluded[:_EXCLUDED_OBJECTS_LIMIT],
            "excluded_total": len(excluded),
            "policy": policy,
            "margin": margin,
            "bounds_world": {"min": list(minimum), "max": list(maximum)},
            "target_point_world": list(center),
            "limiting_axis": limiting_axis,
            "limiting_objects": limiting_objects,
            "transform": _transform_info(camera),
            **result,
            "warnings": warnings,
            "changed_objects": [camera.name],
            "changed_resources": [camera.data.name] if policy != "MOVE_CAMERA" else [],
        }

    def add_camera_constraint(
        self,
        scene_name,
        owner_name,
        constraint_name,
        constraint_type,
        target_name=None,
        subtarget=None,
        influence=1.0,
        owner_space="WORLD",
        target_space="WORLD",
        stack_index=-1,
        preserve_transform=True,
        track_axis="TRACK_NEGATIVE_Z",
        up_axis="UP_Y",
        lock_axis="LOCK_Y",
        forward_axis="FORWARD_X",
        use_curve_follow=True,
        use_fixed_location=True,
        offset_factor=0.0,
        use_x=True,
        use_y=True,
        use_z=True,
        invert_x=False,
        invert_y=False,
        invert_z=False,
        minimum=None,
        maximum=None,
    ):
        scene = _scene(scene_name)
        owner = _object(owner_name, scene=scene)
        _required_name(constraint_name, "constraint_name")
        if constraint_type not in _CONSTRAINT_TYPES:
            raise ValueError(f"Unsupported camera constraint: {constraint_type}")
        valid_spaces = {"WORLD", "CUSTOM", "POSE", "LOCAL_WITH_PARENT", "LOCAL"}
        if owner_space not in valid_spaces or target_space not in valid_spaces:
            raise ValueError("Unsupported owner_space or target_space")
        if track_axis not in {
            "TRACK_X",
            "TRACK_Y",
            "TRACK_Z",
            "TRACK_NEGATIVE_X",
            "TRACK_NEGATIVE_Y",
            "TRACK_NEGATIVE_Z",
        }:
            raise ValueError("Unsupported track_axis")
        if up_axis not in {"UP_X", "UP_Y", "UP_Z"} or lock_axis not in {"LOCK_X", "LOCK_Y", "LOCK_Z"}:
            raise ValueError("Unsupported up_axis or lock_axis")
        if forward_axis not in {
            "FORWARD_X",
            "FORWARD_Y",
            "FORWARD_Z",
            "TRACK_NEGATIVE_X",
            "TRACK_NEGATIVE_Y",
            "TRACK_NEGATIVE_Z",
        }:
            raise ValueError("Unsupported forward_axis")
        if isinstance(stack_index, bool) or int(stack_index) != stack_index or int(stack_index) < -1:
            raise ValueError("stack_index must be an integer greater than or equal to -1")
        offset_factor = _finite_number(offset_factor, "offset_factor")
        if not 0 <= offset_factor <= 1:
            raise ValueError("offset_factor must be between 0 and 1")
        targeted = constraint_type in _TARGETED_CONSTRAINTS
        if targeted != (target_name is not None):
            raise ValueError("The target requirement does not match the constraint type")
        target = _object(target_name, scene=scene) if target_name is not None else None
        if constraint_type == "FOLLOW_PATH" and target.type != "CURVE":
            raise ValueError("FOLLOW_PATH requires a curve target")
        if subtarget:
            bones = getattr(getattr(target, "data", None), "bones", None)
            if target.type != "ARMATURE" or bones is None or bones.get(subtarget) is None:
                raise ValueError(f"Bone subtarget not found: {subtarget}")
        if constraint_type.startswith("LIMIT_") and minimum is None and maximum is None:
            raise ValueError("Limit constraints require minimum and/or maximum")
        minimum_vector = _vector(minimum, "minimum") if minimum is not None else None
        maximum_vector = _vector(maximum, "maximum") if maximum is not None else None
        if (
            minimum_vector is not None
            and maximum_vector is not None
            and any(lower > upper for lower, upper in zip(minimum_vector, maximum_vector, strict=True))
        ):
            raise ValueError("Each minimum component must be less than or equal to maximum")
        influence = _finite_number(influence, "influence")
        if not 0 <= influence <= 1:
            raise ValueError("influence must be between 0 and 1")
        existing = owner.constraints.get(constraint_name)
        if existing is not None and existing.type != constraint_type:
            raise ValueError(f"Constraint '{constraint_name}' already has type {existing.type}")
        before_matrix = owner.matrix_world.copy()
        constraint = existing or owner.constraints.new(type=constraint_type)
        if existing is None:
            constraint.name = constraint_name
        fields: dict[str, object] = {"influence": influence}
        if target is not None:
            fields.update({"target": target, "subtarget": subtarget or ""})
        if constraint_type in {"TRACK_TO", "DAMPED_TRACK", "LOCKED_TRACK"}:
            fields["track_axis"] = track_axis
        if constraint_type == "TRACK_TO":
            fields["up_axis"] = up_axis
        if constraint_type == "LOCKED_TRACK":
            fields["lock_axis"] = lock_axis
        if constraint_type == "FOLLOW_PATH":
            fields.update(
                {
                    "forward_axis": forward_axis,
                    "up_axis": up_axis,
                    "use_curve_follow": use_curve_follow,
                    "use_fixed_location": use_fixed_location,
                    "offset_factor": offset_factor,
                }
            )
        if constraint_type in _COPY_CONSTRAINTS:
            fields.update(
                {
                    "use_x": use_x,
                    "use_y": use_y,
                    "use_z": use_z,
                    "invert_x": invert_x,
                    "invert_y": invert_y,
                    "invert_z": invert_z,
                }
            )
        for field in ("owner_space", "target_space"):
            if hasattr(constraint, field):
                fields[field] = owner_space if field == "owner_space" else target_space
        if constraint_type.startswith("LIMIT_"):
            axes = ("x", "y", "z")
            for index, axis in enumerate(axes):
                if minimum_vector is not None:
                    fields[f"use_min_{axis}"] = True
                    fields[f"min_{axis}"] = minimum_vector[index]
                if maximum_vector is not None:
                    fields[f"use_max_{axis}"] = True
                    fields[f"max_{axis}"] = maximum_vector[index]
        unsupported = [field for field in fields if not hasattr(constraint, field)]
        if unsupported:
            if existing is None:
                owner.constraints.remove(constraint)
            raise ValueError(f"Constraint {constraint_type} does not support fields: {unsupported}")
        old_fields = {field: getattr(constraint, field) for field in fields}
        old_index = list(owner.constraints).index(constraint)
        old_inverse = constraint.inverse_matrix.copy() if hasattr(constraint, "inverse_matrix") else None
        try:
            for field, value in fields.items():
                setattr(constraint, field, value)
            if constraint_type == "CHILD_OF" and preserve_transform:
                constraint.inverse_matrix = target.matrix_world.inverted_safe() @ before_matrix
            if stack_index >= 0:
                source_index = list(owner.constraints).index(constraint)
                owner.constraints.move(source_index, min(int(stack_index), len(owner.constraints) - 1))
            if preserve_transform and constraint_type != "CHILD_OF":
                owner.matrix_world = before_matrix
        except Exception:
            owner.matrix_world = before_matrix
            if existing is None:
                owner.constraints.remove(constraint)
            else:
                for field, value in old_fields.items():
                    setattr(constraint, field, value)
                if old_inverse is not None:
                    constraint.inverse_matrix = old_inverse
                owner.constraints.move(list(owner.constraints).index(constraint), old_index)
            raise
        _update_view_layer()
        evaluated = owner.evaluated_get(bpy.context.evaluated_depsgraph_get()).matrix_world.copy()
        return {
            "owner": owner.name,
            "constraint": _constraint_info(constraint),
            "created": existing is None,
            "assigned_world_matrix": _matrix_list(owner.matrix_world),
            "evaluated_world_matrix": _matrix_list(evaluated),
            "evaluated_transform_changed": not _matrix_close(before_matrix, evaluated),
            "changed_objects": [owner.name],
        }
