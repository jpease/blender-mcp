# Two handlers here configure a datablock they have just created or patched from inside the try
# that owns undoing it, and the configuration is the part that can raise: shortening the clause
# would put a write outside its own rollback, which is the defect these clauses exist to prevent.
# Declared per file as `handlers/character_rigging/references.py` does.
# ruff: file-ignore[too-many-statements-in-try-clause]
# pyright: reportGeneralTypeIssues=false, reportOptionalSubscript=false
"""Camera object lifecycle: creation, optics/display configuration, scene-camera assignment, and depth of field."""

import uuid

import bpy
import mathutils

from ...helpers import MAX_FRAME, MIN_FRAME, bounded_int
from ._shared import (
    _CAMERA_DISPLAY,
    _CAMERA_OPTICS,
    _DOF_FIELDS,
    _camera,
    _camera_cut_map,
    _camera_settings,
    _ensure_collection,
    _finite_number,
    _look_quaternion,
    _new_empty,
    _object,
    _patch_values,
    _positive,
    _required_name,
    _retroactive_cut_warnings,
    _scene,
    _target_world_point,
    _transform_info,
    _update_view_layer,
    _validate_display,
    _vector,
)


def _validate_optics(data, patch):
    patch = dict(patch or {})
    if "projection" in patch:
        patch["type"] = patch.pop("projection")
    allowed = _CAMERA_OPTICS | {"type"}
    unknown = set(patch) - allowed
    if unknown:
        raise ValueError(f"Unsupported camera optical fields: {sorted(unknown)}")
    for field in ("lens", "ortho_scale", "sensor_width", "sensor_height", "clip_start", "clip_end"):
        if field in patch:
            _positive(patch[field], field)
    for field in ("shift_x", "shift_y"):
        if field in patch:
            _finite_number(patch[field], field)
    projection = patch.get("type", data.type)
    if projection not in {"PERSP", "ORTHO", "PANO"}:
        raise ValueError("projection must be PERSP, ORTHO, or PANO")
    clip_start = patch.get("clip_start", data.clip_start)
    clip_end = patch.get("clip_end", data.clip_end)
    if clip_start >= clip_end:
        raise ValueError("clip_start must be less than clip_end")
    if "panorama_type" in patch and projection != "PANO":
        raise ValueError("panorama_type requires projection='PANO'")
    return patch


# A quaternion is four components, and one this short names no rotation: `normalize()` on it
# divides by ~zero. Mirrors `_look_quaternion`'s own guard in `camera/_shared.py`.
_QUATERNION_COMPONENTS = 4
_DEGENERATE_QUATERNION_LENGTH_SQUARED = 1e-16

_DOF_ENGINE_NOTICE = "Depth-of-field appearance depends on the render engine and sampling settings."
# A focus intent on a camera with `use_dof` off renders exactly like no focus intent at all, and
# every other field of this reply says the change landed - which it did, on a switch nothing is
# reading. `create_focus_pull` turns the switch on for its caller; this tool does not, because a
# camera deliberately left sharp must stay sharp, so the inconsistency is stated rather than
# resolved by guessing which one the caller meant.
_DOF_DISABLED_WARNING = (
    "Focus was set, but this camera's use_dof is off, so the render shows no depth of field at all. "
    'Pass patch={"use_dof": true} to enable it.'
)


def _validated_dof_patch(patch):
    """
    Check a depth-of-field patch's numeric fields before any of them is written.

    Args:
        patch: The caller's raw patch, possibly None.

    Returns:
        dict: The patch, defaulted to empty.

    Raises:
        ValueError: If a field is out of its range, leaving the camera untouched.

    """
    patch = patch or {}
    for field in ("aperture_fstop", "aperture_ratio"):
        if field in patch:
            _positive(patch[field], field)
    if "aperture_blades" in patch:
        bounded_int("aperture_blades", patch["aperture_blades"], 0, 16)
    if "aperture_rotation" in patch:
        _finite_number(patch["aperture_rotation"], "aperture_rotation")
    return patch


def _reusable_focus_target(focus_target_name, reuse_focus_target):
    """
    Resolve a named focus target that already exists, refusing to take one over implicitly.

    Args:
        focus_target_name: The target name the caller supplied, or None.
        reuse_focus_target: Whether the caller consented to reusing an existing object.

    Returns:
        The existing object, or None when the name is free or unset.

    Raises:
        ValueError: If the name is taken without consent, or by something this tool did not make.

    """
    existing = bpy.data.objects.get(focus_target_name) if focus_target_name else None
    if existing is None:
        return None
    if not reuse_focus_target:
        raise ValueError(f"Focus target '{focus_target_name}' exists; set reuse_focus_target=true")
    if existing.type != "EMPTY" or existing.get("mcp_camera_role") != "focus_target":
        raise ValueError(f"Object '{focus_target_name}' is not a tagged MCP focus target")
    return existing


def _focus_target_for_point(scene, collection_name, existing_target, focus_target_name, point):
    """
    Put the focus Empty a world-space focus point needs, reusing the caller's or making one.

    Args:
        scene: The scene the collection is resolved in.
        collection_name: Where a newly created target is linked.
        existing_target: The target `_reusable_focus_target` approved, or None.
        focus_target_name: The name a new target takes.
        point: The world-space point to focus on.

    Returns:
        tuple: The object to focus on, and the object this call created, or None if it reused one.
        The caller removes the second on failure, which is why reuse must not report one.

    """
    if existing_target is not None:
        existing_target.matrix_world.translation = point
        return existing_target, None
    collection = _ensure_collection(scene, collection_name)
    created = _new_empty(collection, focus_target_name, point, str(uuid.uuid4()), "focus_target", display_type="SPHERE")
    return created, created


def _validated_quaternion(values):
    """
    Turn a caller's `[w, x, y, z]` into a unit quaternion, or say why it is not one.

    Args:
        values: The four components as the client sent them.

    Returns:
        mathutils.Quaternion: The same rotation, normalized.

    Raises:
        ValueError: If there are not four finite components, or they name no rotation.

    """
    if len(values) != _QUATERNION_COMPONENTS:
        raise ValueError("rotation_quaternion must contain [w, x, y, z]")
    quaternion = mathutils.Quaternion(tuple(_finite_number(value, "rotation_quaternion") for value in values))
    if quaternion.length_squared <= _DEGENERATE_QUATERNION_LENGTH_SQUARED:
        raise ValueError("rotation_quaternion must not be zero-length")
    quaternion.normalize()
    return quaternion


def _resolved_aim_target(scene, target_object_name, target_point, target_bone_name):
    """
    Read where a new camera should look, from whichever of the two target forms was given.

    Args:
        scene: The scene a `target_object_name` is resolved in.
        target_object_name: An object to look at, or None.
        target_point: A world point to look at, or None.
        target_bone_name: A bone on that object to look at instead of its origin, or None.

    Returns:
        mathutils.Vector | None: The world point to aim at, or None when neither was given.

    Raises:
        ValueError: If the named object or bone does not exist, or the point is not three finite
        numbers.

    """
    if target_object_name is not None:
        # The object's evaluated world position, so a constrained or animated target - or a posed
        # bone on it - aims at where it actually is rather than at its unevaluated origin.
        _update_view_layer()
        return _target_world_point(_object(target_object_name, scene=scene), target_bone_name)
    if target_point is not None:
        return _vector(target_point, "target_point")
    return None


def _resolved_camera_orientation(
    scene, rotation_euler, rotation_quaternion, target_object_name, target_point, target_bone_name
):
    """
    Reduce the four ways a new camera can be oriented to the values the write needs.

    Args:
        scene: The scene a `target_object_name` is resolved in.
        rotation_euler: An explicit XYZ triple, or None.
        rotation_quaternion: An explicit `[w, x, y, z]`, or None.
        target_object_name: An object to look at, or None.
        target_point: A world point to look at, or None.
        target_bone_name: A bone on `target_object_name` to look at, or None. This qualifies that
            object rather than competing with it, so it is not one of the exclusive sources.

    Returns:
        tuple: `(rotation_euler, quaternion, aim_target)`, each None unless the caller named
        that source; at most one is ever set.

    Raises:
        ValueError: If more than one source is named, or a named one is unusable.

    """
    sources = (rotation_euler, rotation_quaternion, target_object_name, target_point)
    if sum(value is not None for value in sources) > 1:
        raise ValueError("Supply only one camera orientation source")
    if target_bone_name is not None and target_object_name is None:
        raise ValueError("target_bone_name requires target_object_name")
    return (
        None if rotation_euler is None else _vector(rotation_euler, "rotation_euler"),
        None if rotation_quaternion is None else _validated_quaternion(rotation_quaternion),
        _resolved_aim_target(scene, target_object_name, target_point, target_bone_name),
    )


class _CoreMixin:
    """Provide camera creation, optics/display patching, scene-camera assignment, and DOF handlers."""

    def create_camera(
        self,
        scene_name,
        collection_name,
        name,
        projection="PERSP",
        location=(0.0, 0.0, 0.0),
        rotation_euler=None,
        rotation_quaternion=None,
        target_object_name=None,
        target_point=None,
        target_bone_name=None,
        optics=None,
        make_active=False,
    ):
        scene = _scene(scene_name)
        _required_name(name, "name")
        world_location = _vector(location, "location")
        rotation_euler, quaternion, aim_target = _resolved_camera_orientation(
            scene, rotation_euler, rotation_quaternion, target_object_name, target_point, target_bone_name
        )

        if optics and optics.get("projection") not in {None, projection}:
            raise ValueError("projection conflicts with optics.projection; supply projection in only one place")
        collection = _ensure_collection(scene, collection_name)
        data = bpy.data.cameras.new(f"{name} Data")
        obj = bpy.data.objects.new(name, data)
        try:
            collection.objects.link(obj)
            patch = {"projection": projection, **(optics or {})}
            validated = _validate_optics(data, patch)
            _patch_values(data, validated, _CAMERA_OPTICS | {"type"})
            obj.location = world_location
            if rotation_euler is not None:
                obj.rotation_mode = "XYZ"
                obj.rotation_euler = rotation_euler
            elif quaternion is not None:
                obj.rotation_mode = "QUATERNION"
                obj.rotation_quaternion = quaternion
            elif aim_target is not None:
                obj.rotation_mode = "QUATERNION"
                obj.rotation_quaternion = _look_quaternion(world_location, aim_target)
            if make_active:
                scene.camera = obj
        except Exception:
            # Optics and the aim are only checkable against the camera they are being written to,
            # so both can still refuse after the datablocks exist - a panorama_type on a
            # non-PANO projection, or a camera placed on the point it was told to look at. A
            # dispatched call would be unwound by the mutation transaction, but a direct caller
            # (the real-Blender smoke scripts) and the invalidated-transaction path have nothing
            # to unwind with, and the refusal would leave a half-built camera plus an orphan
            # `<name> Data` behind under a name the next attempt can no longer use cleanly.
            bpy.data.objects.remove(obj, do_unlink=True)
            bpy.data.cameras.remove(data, do_unlink=True)
            raise
        return {
            "object": obj.name,
            "camera_data": data.name,
            "collection": collection.name,
            "scene": scene.name,
            "active_scene_camera": scene.camera == obj,
            "transform": _transform_info(obj),
            "settings": _camera_settings(data),
            "changed_objects": [obj.name],
            "changed_resources": [data.name],
        }

    def configure_camera(self, camera_name, optics=None, display=None):
        camera = _camera(camera_name)
        if not optics and not display:
            raise ValueError("Provide at least one optics or display field to change")
        optics_patch = _validate_optics(camera.data, optics)
        display_patch = _validate_display(display)
        old_optics, new_optics = _patch_values(camera.data, optics_patch, _CAMERA_OPTICS | {"type"})
        try:
            old_display, new_display = _patch_values(camera.data, display_patch, _CAMERA_DISPLAY)
        except Exception:
            for field, value in old_optics.items():
                setattr(camera.data, field, value)
            raise
        return {
            "camera": camera.name,
            "camera_data": camera.data.name,
            "old": {**old_optics, **old_display},
            "new": {**new_optics, **new_display},
            "changed_objects": [camera.name],
            "changed_resources": [camera.data.name],
        }

    def set_scene_camera(
        self,
        scene_name,
        camera_name,
        marker_name=None,
        marker_frame=None,
        replace_marker=False,
    ):
        scene = _scene(scene_name)
        camera = _camera(camera_name, scene=scene)
        if (marker_name is None) != (marker_frame is None):
            raise ValueError("marker_name and marker_frame must be supplied together")
        marker = None
        marker_created = False
        marker_old = None
        if marker_name is not None:
            assert marker_frame is not None
            if not marker_name.strip():
                raise ValueError("marker_name must be non-empty")
            marker_frame = bounded_int("marker_frame", marker_frame, MIN_FRAME, MAX_FRAME)
            by_name = scene.timeline_markers.get(marker_name)
            at_frame = [item for item in scene.timeline_markers if item.frame == marker_frame]
            marker = by_name or (at_frame[0] if at_frame else None)
            if marker is not None:
                conflicts = (
                    marker.name != marker_name
                    or marker.frame != marker_frame
                    or (marker.camera is not None and marker.camera is not camera)
                )
                if conflicts and not replace_marker:
                    raise ValueError(
                        f"Marker collision at name '{marker_name}' or frame {marker_frame}; "
                        "set replace_marker=true to replace"
                    )
                marker_old = (marker.name, marker.frame, marker.camera)
            else:
                marker = scene.timeline_markers.new(marker_name, frame=marker_frame)
                marker_created = True
        previous = scene.camera
        try:
            scene.camera = camera
            if marker is not None:
                marker.name = marker_name
                marker.frame = marker_frame
                marker.camera = camera
        except Exception:
            scene.camera = previous
            if marker_created:
                scene.timeline_markers.remove(marker)
            elif marker is not None and marker_old is not None:
                marker.name, marker.frame, marker.camera = marker_old
            raise
        return {
            "scene": scene.name,
            "previous_camera": previous.name if previous else None,
            "camera": camera.name,
            "marker": ({"name": marker.name, "frame": marker.frame, "camera": marker.camera.name} if marker else None),
            # Reported whether or not this call made a marker: assigning scene.camera while the
            # timeline's earliest camera marker sits after frame_start changes nothing at all.
            "warnings": _retroactive_cut_warnings(scene.frame_start, _camera_cut_map(scene)),
            "changed_objects": [],
        }

    def configure_camera_dof(
        self,
        scene_name,
        camera_name,
        patch,
        focus_object_name=None,
        focus_distance=None,
        focus_point=None,
        focus_target_name=None,
        focus_collection_name="MCP Camera Controls",
        reuse_focus_target=False,
    ):
        scene = _scene(scene_name)
        camera = _camera(camera_name, scene=scene)
        if sum(value is not None for value in (focus_object_name, focus_distance, focus_point)) > 1:
            raise ValueError("Supply at most one focus intent")
        focus_object = _object(focus_object_name, scene=scene) if focus_object_name else None
        if focus_distance is not None:
            focus_distance = _positive(focus_distance, "focus_distance")
        point = _vector(focus_point, "focus_point") if focus_point is not None else None
        if point is not None and not focus_target_name:
            raise ValueError("focus_target_name is required for a focus point")
        dof = camera.data.dof
        patch = _validated_dof_patch(patch)
        if not patch and focus_object_name is None and focus_distance is None and focus_point is None:
            raise ValueError("Provide at least one depth-of-field or focus change")
        existing_target = _reusable_focus_target(focus_target_name, reuse_focus_target)
        before = {
            "patch": {field: getattr(dof, field) for field in patch},
            "focus_object": dof.focus_object,
            "focus_distance": dof.focus_distance,
            "target_matrix": existing_target.matrix_world.copy() if existing_target is not None else None,
        }
        created_target = None
        try:
            old, new = _patch_values(dof, patch, _DOF_FIELDS)
            if point is not None:
                focus_object, created_target = _focus_target_for_point(
                    scene, focus_collection_name, existing_target, focus_target_name, point
                )
            if focus_object is not None:
                dof.focus_object = focus_object
            elif focus_distance is not None:
                dof.focus_object = None
                dof.focus_distance = focus_distance
        except Exception:
            for field, value in before["patch"].items():
                setattr(dof, field, value)
            dof.focus_object = before["focus_object"]
            dof.focus_distance = before["focus_distance"]
            if existing_target is not None and before["target_matrix"] is not None:
                existing_target.matrix_world = before["target_matrix"]
            if created_target is not None:
                bpy.data.objects.remove(created_target, do_unlink=True)
            raise
        changed = [camera.name]
        if created_target:
            changed.append(created_target.name)
        return {
            "camera": camera.name,
            "camera_data": camera.data.name,
            "old": {
                **old,
                "focus_object": getattr(before["focus_object"], "name", None),
                "focus_distance": before["focus_distance"],
            },
            "new": {
                **new,
                "focus_object": getattr(dof.focus_object, "name", None),
                "focus_distance": dof.focus_distance,
            },
            "focus_intent": "OBJECT" if dof.focus_object else "DISTANCE",
            "changed_objects": changed,
            "changed_resources": [camera.data.name],
            "warnings": [_DOF_ENGINE_NOTICE, *([] if dof.use_dof else [_DOF_DISABLED_WARNING])],
        }
