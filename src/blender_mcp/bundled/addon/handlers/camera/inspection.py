# pyright: reportGeneralTypeIssues=false, reportOptionalSubscript=false
"""Read-only camera-rig inspection and structural/evaluated-transform validation."""

import bpy

from ...helpers import paginate
from ._shared import (
    _MAX_RIG_DESCENDANTS,
    _TARGETED_CONSTRAINTS,
    _action_records,
    _bounded_int,
    _camera_settings,
    _constraint_info,
    _descendants,
    _driver_records,
    _frame,
    _object,
    _parent_hierarchy,
    _rig_metadata,
    _scene,
    _transform_info,
)

_MAX_ANIMATION_RECORDS = 5_000


def _finding(severity, code, obj, prop, message, remediation, frame=None):
    result = {
        "severity": severity,
        "code": code,
        "object": getattr(obj, "name", obj),
        "property": prop,
        "message": message,
        "remediation": remediation,
    }
    if frame is not None:
        result["frame"] = frame
    return result


class _InspectionMixin:
    """Provide read-only camera-rig inspection and validation handlers."""

    def get_camera_rig_info(
        self,
        scene_name,
        object_name,
        descendant_depth=4,
        child_limit=50,
        child_offset=0,
        animation_limit=100,
        animation_offset=0,
    ):
        scene = _scene(scene_name)
        root = _object(object_name, scene=scene)
        descendant_depth = _bounded_int(descendant_depth, "descendant_depth", 0, 12)
        child_limit = _bounded_int(child_limit, "child_limit", 1, 200)
        child_offset = _bounded_int(child_offset, "child_offset", 0, _MAX_RIG_DESCENDANTS - 1)
        animation_limit = _bounded_int(animation_limit, "animation_limit", 1, 500)
        animation_offset = _bounded_int(animation_offset, "animation_offset", 0, _MAX_ANIMATION_RECORDS - 1)
        descendants, descendants_capped = _descendants(root, descendant_depth)
        child_start, child_end, child_truncated, child_next = paginate(len(descendants), child_offset, child_limit, 200)
        members = []
        for obj, depth in descendants[child_start:child_end]:
            members.append(
                {
                    "name": obj.name,
                    "type": obj.type,
                    "depth": depth,
                    "parent": obj.parent.name if obj.parent else None,
                    "constraints": [_constraint_info(item) for item in obj.constraints],
                    "drivers": _driver_records(obj),
                    "rig_metadata": _rig_metadata(obj),
                }
            )
        animation = _action_records("OBJECT", root, _MAX_ANIMATION_RECORDS)
        if root.type == "CAMERA":
            animation.extend(_action_records("CAMERA_DATA", root.data, _MAX_ANIMATION_RECORDS - len(animation)))
        for descendant, _depth in descendants:
            remaining = _MAX_ANIMATION_RECORDS - len(animation)
            if remaining <= 0:
                break
            animation.extend(_action_records(descendant.name, descendant, remaining))
            if descendant.type == "CAMERA":
                remaining = _MAX_ANIMATION_RECORDS - len(animation)
                if remaining <= 0:
                    break
                animation.extend(_action_records(f"{descendant.name}:CAMERA_DATA", descendant.data, remaining))
        animation_capped = len(animation) >= _MAX_ANIMATION_RECORDS
        anim_start, anim_end, anim_truncated, anim_next = paginate(
            len(animation), animation_offset, animation_limit, 500
        )
        inspected_camera_names = {obj.name for obj, _depth in [(root, 0), *descendants] if obj.type == "CAMERA"}
        markers = [
            {"name": marker.name, "frame": marker.frame, "camera": marker.camera.name}
            for marker in scene.timeline_markers
            if marker.camera is not None and marker.camera.name in inspected_camera_names
        ]
        render = scene.render
        result = {
            "scene": scene.name,
            "object": root.name,
            "object_type": root.type,
            "parent_hierarchy": _parent_hierarchy(root),
            "transform": _transform_info(root),
            "constraints": [_constraint_info(item) for item in root.constraints],
            "drivers": _driver_records(root),
            "rig_metadata": _rig_metadata(root),
            "active_scene_camera": scene.camera == root,
            "camera_markers": markers,
            "render_gate": {
                "resolution_x": render.resolution_x,
                "resolution_y": render.resolution_y,
                "resolution_percentage": render.resolution_percentage,
                "pixel_aspect_x": render.pixel_aspect_x,
                "pixel_aspect_y": render.pixel_aspect_y,
                "display_aspect": (render.resolution_x * render.pixel_aspect_x)
                / (render.resolution_y * render.pixel_aspect_y),
            },
            "children": members,
            "children_total": len(descendants),
            "children_returned_count": len(members),
            "children_truncated": child_truncated or descendants_capped,
            "children_next_offset": child_next,
            "children_scan_capped": descendants_capped,
            "animation": animation[anim_start:anim_end],
            "animation_total": len(animation),
            "animation_returned_count": anim_end - anim_start,
            "animation_truncated": anim_truncated or animation_capped,
            "animation_next_offset": anim_next,
            "animation_scan_capped": animation_capped,
        }
        if root.type == "CAMERA":
            result["camera_data"] = root.data.name
            result["camera"] = _camera_settings(root.data)
        return result

    def validate_camera_rig(self, scene_name, object_names=None, sample_frames=None):
        scene = _scene(scene_name)
        frames = list(sample_frames or [scene.frame_current])
        if len(frames) > 24:
            raise ValueError("sample_frames exceeds the 24-frame safety limit")
        frames = list(dict.fromkeys(_frame(value, "sample_frames") for value in frames))
        if object_names is None:
            objects = [obj for obj in scene.objects if obj.type == "CAMERA" or obj.get("mcp_camera_rig_id") is not None]
            if len(objects) > 500:
                raise ValueError("Scene camera-rig scope exceeds 500 objects; provide an explicit object_names subset")
        else:
            if len(object_names) > 500 or len(set(object_names)) != len(object_names):
                raise ValueError("object_names must be unique and contain at most 500 names")
            objects = [_object(name, scene=scene) for name in object_names]
        findings = []
        if scene.camera is None:
            findings.append(
                _finding(
                    "WARNING",
                    "MISSING_SCENE_CAMERA",
                    scene.name,
                    "scene.camera",
                    "The scene has no active camera.",
                    "Assign a camera before rendering the shot.",
                )
            )
        elif scene.camera.type != "CAMERA":
            findings.append(
                _finding(
                    "ERROR",
                    "INVALID_SCENE_CAMERA",
                    scene.camera,
                    "scene.camera",
                    "The active scene object is not a camera.",
                    "Assign a camera object.",
                )
            )
        roots_by_id = {}
        roles_by_id = {}
        for obj in objects:
            rig_id = obj.get("mcp_camera_rig_id")
            if rig_id:
                roles_by_id.setdefault(rig_id, {}).setdefault(obj.get("mcp_camera_role"), []).append(obj)
                if obj.parent is None:
                    roots_by_id.setdefault(rig_id, []).append(obj)
            seen = set()
            parent = obj
            while parent is not None:
                if parent in seen:
                    findings.append(
                        _finding(
                            "ERROR", "PARENT_CYCLE", obj, "parent", "Parent cycle detected.", "Break the parent cycle."
                        )
                    )
                    break
                seen.add(parent)
                parent = parent.parent
            parent = obj.parent
            if parent is not None:
                scale = parent.matrix_world.to_scale()
                if any(value < 0 for value in scale):
                    findings.append(
                        _finding(
                            "WARNING",
                            "NEGATIVE_PARENT_SCALE",
                            obj,
                            "parent.scale",
                            "A parent has negative scale.",
                            "Use positive parent scale or verify evaluated camera orientation.",
                        )
                    )
                if max(scale) - min(scale) > 1e-5:
                    findings.append(
                        _finding(
                            "WARNING",
                            "NONUNIFORM_PARENT_SCALE",
                            obj,
                            "parent.scale",
                            "A parent has nonuniform scale.",
                            "Verify constraints and camera transforms under evaluated scale.",
                        )
                    )
            for constraint in obj.constraints:
                if not constraint.is_valid:
                    findings.append(
                        _finding(
                            "ERROR",
                            "INVALID_CONSTRAINT",
                            obj,
                            f'constraints["{constraint.name}"]',
                            "Constraint reports invalid state.",
                            "Repair or remove the broken constraint.",
                        )
                    )
                if constraint.type in _TARGETED_CONSTRAINTS and getattr(constraint, "target", None) is None:
                    findings.append(
                        _finding(
                            "ERROR",
                            "MISSING_CONSTRAINT_TARGET",
                            obj,
                            f'constraints["{constraint.name}"].target',
                            "Constraint target is missing.",
                            "Assign an explicit compatible target.",
                        )
                    )
                if (
                    constraint.type == "FOLLOW_PATH"
                    and getattr(getattr(constraint, "target", None), "type", None) != "CURVE"
                ):
                    findings.append(
                        _finding(
                            "ERROR",
                            "INVALID_PATH_TARGET",
                            obj,
                            f'constraints["{constraint.name}"].target',
                            "Follow Path target is not a curve.",
                            "Assign a curve object.",
                        )
                    )
            animation = getattr(obj, "animation_data", None)
            for curve in getattr(animation, "drivers", ()) if animation is not None else ():
                for variable in curve.driver.variables:
                    if any(target.id is None for target in variable.targets):
                        findings.append(
                            _finding(
                                "ERROR",
                                "BROKEN_DRIVER_TARGET",
                                obj,
                                curve.data_path,
                                "Driver variable has a missing ID target.",
                                "Repair the driver variable target.",
                            )
                        )
            if obj.type == "CAMERA":
                data = obj.data
                if data.clip_start <= 0 or data.clip_start >= data.clip_end:
                    findings.append(
                        _finding(
                            "ERROR",
                            "INVALID_CLIP_RANGE",
                            obj,
                            "data.clip_start",
                            "Camera clipping planes are invalid.",
                            "Set 0 < clip_start < clip_end.",
                        )
                    )
                if data.lens < 5 or data.lens > 500:
                    findings.append(
                        _finding(
                            "WARNING",
                            "EXTREME_LENS",
                            obj,
                            "data.lens",
                            "Camera lens is outside the typical 5-500 mm production range.",
                            "Confirm the extreme focal length is intentional.",
                        )
                    )
                if data.sensor_width <= 0 or data.sensor_height <= 0:
                    findings.append(
                        _finding(
                            "ERROR",
                            "INVALID_SENSOR",
                            obj,
                            "data.sensor_width",
                            "Camera sensor dimensions are invalid.",
                            "Set positive sensor dimensions.",
                        )
                    )
                if getattr(data, "users", 1) > 1:
                    findings.append(
                        _finding(
                            "INFO",
                            "SHARED_CAMERA_DATA",
                            obj,
                            "data",
                            "Camera datablock is shared by multiple objects.",
                            "Confirm linked optics are intentional.",
                        )
                    )
        for rig_id, roots in roots_by_id.items():
            if len(roots) != 1:
                findings.append(
                    _finding(
                        "WARNING",
                        "DUPLICATE_RIG_ROOT",
                        rig_id,
                        "mcp_camera_rig_id",
                        f"Rig has {len(roots)} root objects.",
                        "Keep one tagged root per rig ID.",
                    )
                )
        for rig_id, roles in roles_by_id.items():
            for role, members in roles.items():
                if role and len(members) > 1 and role in {"root", "camera", "target"}:
                    findings.append(
                        _finding(
                            "WARNING",
                            "DUPLICATE_RIG_ROLE",
                            rig_id,
                            str(role),
                            f"Rig has {len(members)} members with role '{role}'.",
                            "Assign unique primary roles.",
                        )
                    )
        markers_by_frame = {}
        for marker in scene.timeline_markers:
            if marker.camera is not None:
                markers_by_frame.setdefault(marker.frame, []).append(marker)
        for frame_value, markers in markers_by_frame.items():
            if len(markers) > 1:
                findings.append(
                    _finding(
                        "WARNING",
                        "OVERLAPPING_CAMERA_MARKERS",
                        scene.name,
                        "timeline_markers",
                        f"Multiple camera markers exist at frame {frame_value}.",
                        "Keep one editorial camera binding per frame.",
                        frame_value,
                    )
                )
        original_frame = scene.frame_current
        original_subframe = scene.frame_subframe
        try:
            for frame_value in frames:
                scene.frame_set(frame_value)
                depsgraph = bpy.context.evaluated_depsgraph_get()
                for camera in (obj for obj in objects if obj.type == "CAMERA"):
                    evaluated = camera.evaluated_get(depsgraph)
                    for constraint in camera.constraints:
                        target = getattr(constraint, "target", None)
                        if target is None or constraint.type not in {"TRACK_TO", "DAMPED_TRACK", "LOCKED_TRACK"}:
                            continue
                        local = (
                            evaluated.matrix_world.inverted_safe()
                            @ target.evaluated_get(depsgraph).matrix_world.translation
                        )
                        if local.z >= 0:
                            findings.append(
                                _finding(
                                    "WARNING",
                                    "AIM_TARGET_BEHIND_CAMERA",
                                    camera,
                                    f'constraints["{constraint.name}"].target',
                                    "Aim target evaluates behind the camera.",
                                    "Move the target in front of camera local -Z or inspect the constraint axes.",
                                    frame_value,
                                )
                            )
                    focus = camera.data.dof.focus_object
                    if focus is not None:
                        local = (
                            evaluated.matrix_world.inverted_safe()
                            @ focus.evaluated_get(depsgraph).matrix_world.translation
                        )
                        if local.z >= 0:
                            findings.append(
                                _finding(
                                    "WARNING",
                                    "FOCUS_TARGET_BEHIND_CAMERA",
                                    camera,
                                    "data.dof.focus_object",
                                    "Focus target evaluates behind the camera.",
                                    "Move the focus target in front of camera local -Z.",
                                    frame_value,
                                )
                            )
        finally:
            scene.frame_set(original_frame, subframe=original_subframe)
        findings.sort(
            key=lambda item: (
                {"ERROR": 0, "WARNING": 1, "INFO": 2}[item["severity"]],
                item["code"],
                str(item["object"]),
            )
        )
        return {
            "scene": scene.name,
            "objects_checked": [obj.name for obj in objects],
            "sampled_frames": frames,
            "findings": findings,
            "summary": {
                severity.lower(): sum(item["severity"] == severity for item in findings)
                for severity in ("ERROR", "WARNING", "INFO")
            },
            "verification": "Structural and evaluated-transform checks only; visual correctness was not inferred.",
        }
