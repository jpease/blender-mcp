# pyright: reportGeneralTypeIssues=false, reportOptionalSubscript=false
"""Editorial camera markers (camera cuts) and render-gate/safe-area/guide configuration."""

from ._shared import (
    _camera,
    _camera_cut_map,
    _finite_number,
    _frame,
    _patch_values,
    _plain,
    _required_name,
    _retroactive_cut_warnings,
    _scene,
    _validate_display,
)

_CAMERA_GUIDES = {
    "show_safe_areas",
    "show_composition_center",
    "show_composition_center_diagonal",
    "show_composition_golden",
    "show_composition_golden_tria_a",
    "show_composition_golden_tria_b",
    "show_composition_harmony_tri_a",
    "show_composition_harmony_tri_b",
    "show_composition_thirds",
}


def _prepared_marker_edits(scene, action, edits, replace_existing):
    """
    Validate every requested marker edit before the first one is applied.

    Every refusal in here is raised while the timeline is still untouched, so a request
    naming one bad marker among twenty leaves the scene as it was rather than half-cut.

    Args:
        scene: Scene holding the timeline markers.
        action: CREATE, UPDATE or REMOVE; LIST never reaches here.
        edits: The requested marker edits, already bounded by the caller.
        replace_existing: Whether CREATE may take over a marker that already exists.

    Returns:
        tuple[list[str], list[tuple]]: The edited names in request order, and one
        `(name, existing_marker, frame, camera)` per edit, with frame and camera None
        for REMOVE.

    Raises:
        ValueError: If a name repeats, a marker is missing or already present, or a
            CREATE omits the frame or camera it cannot infer.

    """
    names = []
    prepared = []
    for index, edit in enumerate(edits):
        name = _required_name(edit.get("name"), f"markers[{index}].name")
        if name in names:
            raise ValueError(f"Duplicate marker name in request: {name}")
        names.append(name)
        existing = scene.timeline_markers.get(name)
        if action == "CREATE":
            if edit.get("frame") is None or edit.get("camera_name") is None:
                raise ValueError("CREATE requires frame and camera_name for every marker")
            if existing is not None and not replace_existing:
                raise ValueError(f"Marker already exists: {name}")
        elif existing is None:
            raise ValueError(f"Marker not found: {name}")
        if action == "REMOVE":
            prepared.append((name, existing, None, None))
            continue
        frame = _frame(edit.get("frame", existing.frame if existing else None), f"markers[{index}].frame")
        camera_name = edit.get("camera_name", getattr(existing.camera, "name", None) if existing else None)
        if camera_name is None:
            raise ValueError(f"markers[{index}].camera_name is required")
        prepared.append((name, existing, frame, _camera(camera_name, scene=scene)))
    return names, prepared


class _ShotsMixin:
    """Provide editorial camera-marker and render-gate/safe-area/guide handlers."""

    def create_camera_markers(self, scene_name, action, markers=None, replace_existing=False):
        scene = _scene(scene_name)
        edits = list(markers or [])
        if action not in {"LIST", "CREATE", "UPDATE", "REMOVE"}:
            raise ValueError("action must be LIST, CREATE, UPDATE, or REMOVE")
        if action == "LIST":
            if edits:
                raise ValueError("LIST does not accept marker edits")
            cuts = _camera_cut_map(scene)
            return {
                "action": action,
                "camera_cuts": cuts,
                "warnings": _retroactive_cut_warnings(scene.frame_start, cuts),
                "changed_objects": [],
            }
        if not edits or len(edits) > 200:
            raise ValueError("A mutating marker request requires 1 to 200 edits")
        names, prepared = _prepared_marker_edits(scene, action, edits, replace_existing)
        changed_cameras = []
        for name, existing, frame, camera in prepared:
            if action == "REMOVE":
                scene.timeline_markers.remove(existing)
                continue
            marker = existing or scene.timeline_markers.new(name, frame=frame)
            marker.frame = frame
            marker.camera = camera
            changed_cameras.append(camera.name)
        cuts = _camera_cut_map(scene)
        return {
            "action": action,
            "edited_markers": names,
            "camera_cuts": cuts,
            "warnings": _retroactive_cut_warnings(scene.frame_start, cuts),
            "changed_objects": list(dict.fromkeys(changed_cameras)),
            "changed_resources": [scene.name],
        }

    def configure_camera_render_gate(
        self,
        scene_name,
        camera_name=None,
        render=None,
        border=None,
        safe_areas=None,
        guides=None,
    ):
        scene = _scene(scene_name)
        camera = _camera(camera_name, scene=scene) if camera_name is not None else None
        render_patch = dict(render or {})
        border_patch = dict(border or {})
        safe_patch = dict(safe_areas or {})
        guide_patch = dict(guides or {})
        if not any((render_patch, border_patch, safe_patch, guide_patch)):
            raise ValueError("Provide at least one render-gate field")
        render_allowed = {"resolution_x", "resolution_y", "resolution_percentage", "pixel_aspect_x", "pixel_aspect_y"}
        border_map = {
            "use_border": "use_border",
            "use_crop_to_border": "use_crop_to_border",
            "min_x": "border_min_x",
            "max_x": "border_max_x",
            "min_y": "border_min_y",
            "max_y": "border_max_y",
        }
        if set(render_patch) - render_allowed or set(border_patch) - set(border_map):
            raise ValueError("Unsupported render-gate field")
        for field in ("resolution_x", "resolution_y"):
            if field in render_patch and (
                isinstance(render_patch[field], bool)
                or int(render_patch[field]) != render_patch[field]
                or not 4 <= int(render_patch[field]) <= 65_536
            ):
                raise ValueError(f"{field} must be an integer between 4 and 65536")
        if "resolution_percentage" in render_patch and (
            isinstance(render_patch["resolution_percentage"], bool)
            or int(render_patch["resolution_percentage"]) != render_patch["resolution_percentage"]
            or not 1 <= int(render_patch["resolution_percentage"]) <= 100
        ):
            raise ValueError("resolution_percentage must be an integer between 1 and 100")
        for field in ("pixel_aspect_x", "pixel_aspect_y"):
            if field in render_patch and not 0 < _finite_number(render_patch[field], field) <= 200:
                raise ValueError(f"{field} must be positive and at most 200")
        for field in ("min_x", "max_x", "min_y", "max_y"):
            if field in border_patch and not 0 <= _finite_number(border_patch[field], field) <= 1:
                raise ValueError(f"{field} must be between 0 and 1")
        current_min_x = border_patch.get("min_x", scene.render.border_min_x)
        current_max_x = border_patch.get("max_x", scene.render.border_max_x)
        current_min_y = border_patch.get("min_y", scene.render.border_min_y)
        current_max_y = border_patch.get("max_y", scene.render.border_max_y)
        if current_min_x >= current_max_x or current_min_y >= current_max_y:
            raise ValueError("Render border minima must be less than maxima")
        safe_allowed = {"title", "action", "title_center", "action_center"}
        if set(safe_patch) - safe_allowed:
            raise ValueError("Unsupported safe-area field")
        for field, value in safe_patch.items():
            if len(value) != 2 or any(not 0 <= _finite_number(item, field) <= 1 for item in value):
                raise ValueError(f"{field} must contain two values between 0 and 1")
            if not hasattr(scene.safe_areas, field):
                raise ValueError(f"Running Blender does not support safe-area field: {field}")
        if guide_patch and camera is None:
            raise ValueError("camera_name is required for camera guides")
        guide_patch = _validate_display(guide_patch)
        if set(guide_patch) - _CAMERA_GUIDES:
            raise ValueError("Unsupported camera guide field")
        missing_guides = (
            [field for field in guide_patch if not hasattr(camera.data, field)] if camera is not None else []
        )
        if missing_guides:
            raise ValueError(f"Running Blender does not support camera guide fields: {missing_guides}")
        old = {"render": {}, "border": {}, "safe_areas": {}, "guides": {}}
        new = {"render": {}, "border": {}, "safe_areas": {}, "guides": {}}
        try:
            for field, value in render_patch.items():
                old["render"][field] = _plain(getattr(scene.render, field))
                setattr(scene.render, field, value)
                new["render"][field] = _plain(getattr(scene.render, field))
            for public, rna in border_map.items():
                if public in border_patch:
                    old["border"][public] = _plain(getattr(scene.render, rna))
                    setattr(scene.render, rna, border_patch[public])
                    new["border"][public] = _plain(getattr(scene.render, rna))
            for field, value in safe_patch.items():
                old["safe_areas"][field] = _plain(getattr(scene.safe_areas, field))
                setattr(scene.safe_areas, field, value)
                new["safe_areas"][field] = _plain(getattr(scene.safe_areas, field))
            if guide_patch:
                old["guides"], new["guides"] = _patch_values(camera.data, guide_patch, _CAMERA_GUIDES)
        except Exception:
            for field, value in old["render"].items():
                setattr(scene.render, field, value)
            for public, value in old["border"].items():
                setattr(scene.render, border_map[public], value)
            for field, value in old["safe_areas"].items():
                setattr(scene.safe_areas, field, value)
            for field, value in old["guides"].items():
                setattr(camera.data, field, value)
            raise
        changed_resources = [scene.name] + ([camera.data.name] if guide_patch else [])
        return {
            "scene": scene.name,
            "camera": getattr(camera, "name", None),
            "old": old,
            "new": new,
            "changed_objects": [camera.name] if guide_patch else [],
            "changed_resources": changed_resources,
        }
