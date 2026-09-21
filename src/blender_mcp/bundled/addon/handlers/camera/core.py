# pyright: reportGeneralTypeIssues=false, reportOptionalSubscript=false
"""Camera object lifecycle: creation, optics/display configuration, scene-camera assignment, and depth of field."""

import uuid

import bpy
import mathutils

from ._shared import (
    _CAMERA_DISPLAY,
    _CAMERA_OPTICS,
    _DOF_FIELDS,
    _MAX_FRAME,
    _MIN_FRAME,
    _bounded_int,
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
        look_at_object_name=None,
        look_at_point=None,
        optics=None,
        make_active=False,
    ):
        scene = _scene(scene_name)
        _required_name(name, "name")
        orientation_count = sum(
            value is not None for value in (rotation_euler, rotation_quaternion, look_at_object_name, look_at_point)
        )
        if orientation_count > 1:
            raise ValueError("Supply only one camera orientation source")
        world_location = _vector(location, "location")
        look_target = None
        if look_at_object_name is not None:
            _update_view_layer()
            look_target = _object(look_at_object_name, scene=scene).matrix_world.translation.copy()
        elif look_at_point is not None:
            look_target = _vector(look_at_point, "look_at_point")
        if rotation_euler is not None:
            rotation_euler = _vector(rotation_euler, "rotation_euler")
        if rotation_quaternion is not None:
            if len(rotation_quaternion) != 4:
                raise ValueError("rotation_quaternion must contain [w, x, y, z]")
            values = tuple(_finite_number(value, "rotation_quaternion") for value in rotation_quaternion)
            quaternion = mathutils.Quaternion(values)
            if quaternion.length_squared <= 1e-16:
                raise ValueError("rotation_quaternion must not be zero-length")
            quaternion.normalize()
        else:
            quaternion = None

        if optics and optics.get("projection") not in {None, projection}:
            raise ValueError("projection conflicts with optics.projection; supply projection in only one place")
        collection = _ensure_collection(scene, collection_name)
        data = bpy.data.cameras.new(f"{name} Data")
        obj = bpy.data.objects.new(name, data)
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
        elif look_target is not None:
            obj.rotation_mode = "QUATERNION"
            obj.rotation_quaternion = _look_quaternion(world_location, look_target)
        if make_active:
            scene.camera = obj
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
            marker_frame = _bounded_int(marker_frame, "marker_frame", _MIN_FRAME, _MAX_FRAME)
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
        patch = patch or {}
        if not patch and focus_object_name is None and focus_distance is None and focus_point is None:
            raise ValueError("Provide at least one depth-of-field or focus change")
        for field in ("aperture_fstop", "aperture_ratio"):
            if field in patch:
                _positive(patch[field], field)
        if "aperture_blades" in patch:
            _bounded_int(patch["aperture_blades"], "aperture_blades", 0, 16)
        if "aperture_rotation" in patch:
            _finite_number(patch["aperture_rotation"], "aperture_rotation")
        existing_target = bpy.data.objects.get(focus_target_name) if focus_target_name else None
        existing_target_matrix = existing_target.matrix_world.copy() if existing_target is not None else None
        if existing_target is not None:
            if not reuse_focus_target:
                raise ValueError(f"Focus target '{focus_target_name}' exists; set reuse_focus_target=true")
            if existing_target.type != "EMPTY" or existing_target.get("mcp_camera_role") != "focus_target":
                raise ValueError(f"Object '{focus_target_name}' is not a tagged MCP focus target")
        old_patch = {field: getattr(dof, field) for field in patch}
        old_focus_object = dof.focus_object
        old_focus_distance = dof.focus_distance
        created_target = None
        try:
            old, new = _patch_values(dof, patch, _DOF_FIELDS)
            if point is not None:
                if existing_target is not None:
                    existing_target.matrix_world.translation = point
                    focus_object = existing_target
                else:
                    collection = _ensure_collection(scene, focus_collection_name)
                    created_target = _new_empty(
                        collection,
                        focus_target_name,
                        point,
                        str(uuid.uuid4()),
                        "focus_target",
                        display_type="SPHERE",
                    )
                    focus_object = created_target
            if focus_object is not None:
                dof.focus_object = focus_object
            elif focus_distance is not None:
                dof.focus_object = None
                dof.focus_distance = focus_distance
        except Exception:
            for field, value in old_patch.items():
                setattr(dof, field, value)
            dof.focus_object = old_focus_object
            dof.focus_distance = old_focus_distance
            if existing_target is not None and existing_target_matrix is not None:
                existing_target.matrix_world = existing_target_matrix
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
                "focus_object": getattr(old_focus_object, "name", None),
                "focus_distance": old_focus_distance,
            },
            "new": {
                **new,
                "focus_object": getattr(dof.focus_object, "name", None),
                "focus_distance": dof.focus_distance,
            },
            "focus_intent": "OBJECT" if dof.focus_object else "DISTANCE",
            "changed_objects": changed,
            "changed_resources": [camera.data.name],
            "warnings": ["Depth-of-field appearance depends on the render engine and sampling settings."],
        }
