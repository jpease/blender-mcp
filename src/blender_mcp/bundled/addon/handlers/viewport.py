import contextlib

import bpy

from ..helpers import find_view3d
from .camera._shared import _camera, _look_quaternion, _object, _vector

# The only two space.shading.type values get_viewport_screenshot's shading_override accepts.
# RENDERED is real render-engine cost, not a cheap single-pass rasterization like these two -
# offering it here would silently make this "cheap inspection" tool expensive.
_SHADING_OVERRIDES = frozenset({"SOLID", "MATERIAL"})


def _synthetic_view_matrices(view, scene):
    """
    Build (view_matrix, window_matrix, view_source) for get_viewport_screenshot's optional ad hoc view.

    This is exactly the (view_matrix, window_matrix) pair a live viewport's RegionView3D
    already exposes when "looking through camera" - offscreen.draw_view3d takes them as plain
    arguments regardless of where they came from, so no live viewport mutation is needed to
    redirect it.

    camera_object reads an existing camera object's own matrix_world/lens via
    Object.calc_matrix_camera - the real Blender API for this projection matrix (it lives on
    Object, not on the Camera data-block, despite being "mostly useful for Camera and Light
    types" per its own docstring). The eye/target modes build a throwaway Camera + Object
    pair purely to reuse that same call and _look_quaternion (the identical look-at math
    create_camera's look_at_point already uses in production), then remove both before
    returning - never added to any collection or the view layer.

    The throwaway pair reads its view matrix off matrix_basis, not matrix_world: matrix_world
    is a depsgraph-evaluated result, and an object outside every view layer is in no
    depsgraph, so its matrix_world stays the identity no matter what location/rotation it was
    given (verified against a real Blender 5.2.2 - matrix_world.inverted() there would aim
    every ad hoc capture from the world origin). matrix_basis is composed on demand from
    location/rotation/scale with no depsgraph involved, and on this unparented object it is
    bit-identical to the matrix_world an equivalent linked camera reports. calc_matrix_camera
    needs no such care: it is transform-independent (also verified - identical matrix linked
    or unlinked).

    Args:
        view: A ViewSpec's fields as a plain dict: exactly one of camera_object or eye (with
            target or target_object), plus lens_mm.
        scene: The scene to size the projection for (its render resolution sets the aspect
            ratio, the same source render_scene uses, so a synthetic screenshot's framing
            matches what an actual render from that view would show).

    Returns:
        tuple: (view_matrix, window_matrix, view_source) where view_source is
        "camera_object" or "eye_target".

    Raises:
        ValueError: If a named camera_object/target_object does not exist, or eye and the
            resolved target coincide.

    """
    bpy.context.view_layer.update()
    width, height = scene.render.resolution_x, scene.render.resolution_y

    camera_object = view.get("camera_object")
    if camera_object is not None:
        cam_obj = _camera(camera_object)
        view_matrix = cam_obj.matrix_world.inverted()
        window_matrix = cam_obj.calc_matrix_camera(
            bpy.context.evaluated_depsgraph_get(), x=width, y=height, scale_x=1.0, scale_y=1.0
        )
        return view_matrix, window_matrix, "camera_object"

    cam_data = bpy.data.cameras.new("_mcp_synthetic_view")
    cam_obj = bpy.data.objects.new("_mcp_synthetic_view", cam_data)
    try:
        cam_data.lens = view.get("lens_mm", 50.0)
        eye = _vector(view["eye"], "view.eye")
        target_object = view.get("target_object")
        if target_object is not None:
            target = _object(target_object).matrix_world.translation
        else:
            target = _vector(view["target"], "view.target")
        cam_obj.location = eye
        cam_obj.rotation_mode = "QUATERNION"
        cam_obj.rotation_quaternion = _look_quaternion(eye, target)
        view_matrix = cam_obj.matrix_basis.inverted()
        window_matrix = cam_obj.calc_matrix_camera(
            bpy.context.evaluated_depsgraph_get(), x=width, y=height, scale_x=1.0, scale_y=1.0
        )
        return view_matrix, window_matrix, "eye_target"
    finally:
        bpy.data.objects.remove(cam_obj, do_unlink=True)
        bpy.data.cameras.remove(cam_data)


@contextlib.contextmanager
def _shading_override(space, shading_override):
    """
    Temporarily force a 3D viewport space's shading.type, always restoring it.

    A plain, reversible RNA property write - not an operator call, so it needs no context
    override to be safe from this addon's main-thread timer call site (the same call site
    set_viewport_overlay already writes other space-level UI properties from).
    get_viewport_screenshot is a read-only command (never wrapped in mutation_transaction), so
    this restore is the only thing undoing the write; it must run even when the capture that
    follows raises.

    Args:
        space: The SpaceView3D to override.
        shading_override: "SOLID", "MATERIAL", or None to leave shading.type untouched.

    Yields:
        str: The shading.type value actually in effect for the duration of the block.

    """
    if shading_override is None:
        yield space.shading.type
        return
    original = space.shading.type
    space.shading.type = shading_override
    try:
        yield shading_override
    finally:
        space.shading.type = original


def _render_offscreen(space, region, view_matrix, window_matrix, max_size, filepath, image_format):
    """
    Rasterize one GPU offscreen capture of a 3D viewport and save it to filepath.

    Split out of _capture_view so that function's own try clause stays within this project's
    ruff statement budget. gpu/numpy stay imported here rather than at module level: gpu does
    not exist outside real Blender at all (only an empty type-checking stub satisfies `import
    gpu` there), so a module-level import would break every test that loads this addon package
    outside Blender (test_mutation_transaction._load_addon and everything built on it).

    Returns:
        tuple: (width, height) of the saved image.

    """
    import gpu
    import numpy as np

    src_w, src_h = region.width, region.height
    if max(src_w, src_h) > max_size:
        s = max_size / max(src_w, src_h)
        width, height = max(1, int(src_w * s)), max(1, int(src_h * s))
    else:
        width, height = src_w, src_h

    offscreen = gpu.types.GPUOffScreen(width, height)
    try:
        offscreen.draw_view3d(
            bpy.context.scene,
            bpy.context.view_layer,
            space,
            region,
            view_matrix,
            window_matrix,
            do_color_management=True,
        )
        buf = offscreen.texture_color.read()
    finally:
        offscreen.free()

    buf.dimensions = width * height * 4
    pixels = np.asarray(buf, dtype=np.float32) / 255.0  # GPU buffer is 0..255

    image = bpy.data.images.new("mcp_viewport", width, height, alpha=True)
    image.pixels.foreach_set(pixels.ravel())
    image.filepath_raw = filepath
    image.file_format = image_format.upper()
    image.save()
    bpy.data.images.remove(image)
    return width, height


def _window_grab_fallback(area, max_size, filepath, image_format):
    """
    Grab the live Blender window's framebuffer and save it to filepath.

    Split out of _capture_view for the same reason as _render_offscreen. Only reachable for
    view_source == "live_viewport" - see _capture_view's docstring for why a synthetic view
    never falls back to this.

    Returns:
        tuple: (width, height) of the saved image.

    """
    with bpy.context.temp_override(area=area):
        bpy.ops.screen.screenshot_area(filepath=filepath)
    img = bpy.data.images.load(filepath)
    width, height = img.size
    if max(width, height) > max_size:
        s = max_size / max(width, height)
        width, height = int(width * s), int(height * s)
        img.scale(width, height)
        img.file_format = image_format.upper()
        img.save()
    bpy.data.images.remove(img)
    return width, height


def _capture_view(
    area, region, space, view_matrix, window_matrix, view_source, max_size, filepath, image_format, shading_override
):
    """
    Rasterize one viewport capture with view_matrix/window_matrix and write it to filepath.

    Split out of get_viewport_screenshot itself so that method's own branch/statement/local
    count stays under this project's ruff budget - the offscreen-vs-window-grab fallback and
    the temporary shading override are exactly the part that grew past it.

    A synthetic view_source ("camera_object"/"eye_target") has no live-window equivalent, so
    it skips the window_grab fallback entirely: silently grabbing the live (wrong) viewpoint
    instead of reporting the real offscreen failure would be worse than failing.

    Args:
        area: The VIEW_3D area found by get_viewport_screenshot.
        region: That area's WINDOW region.
        space: That area's active SpaceView3D.
        view_matrix: 4x4 view matrix to render with.
        window_matrix: 4x4 projection matrix to render with.
        view_source: "live_viewport", "camera_object", or "eye_target".
        max_size: Maximum pixel length of the image's largest dimension.
        filepath: Path to save the screenshot file to.
        image_format: Image format (png, jpg, etc.), matching bpy image.file_format casing.
        shading_override: "SOLID", "MATERIAL", or None to leave the live shading untouched.

    Returns:
        dict: "success", "width", "height", "filepath", "method" ("offscreen" or
        "window_grab"), "view_source", "shading_mode" (the space.shading.type actually used).

    """
    method = "offscreen"
    with _shading_override(space, shading_override) as shading_mode:
        try:
            width, height = _render_offscreen(
                space, region, view_matrix, window_matrix, max_size, filepath, image_format
            )
        except Exception as offscreen_err:
            if view_source != "live_viewport":
                raise
            print(
                f"[BlenderMCP] offscreen capture failed ({offscreen_err}); falling back to window grab",
                flush=True,
            )
            method = "window_grab"
            width, height = _window_grab_fallback(area, max_size, filepath, image_format)

    return {
        "success": True,
        "width": width,
        "height": height,
        "filepath": filepath,
        "method": method,
        "view_source": view_source,
        "shading_mode": shading_mode,
    }


class ViewportHandlersMixin:
    """Provide handlers for inspecting and capturing the 3D viewport."""

    _OVERLAY_TOGGLES = {
        "CAVITY": ("shading", "show_cavity"),
        "WIREFRAMES": ("overlay", "show_wireframes"),
        "FACE_ORIENTATION": ("overlay", "show_face_orientation"),
    }

    def set_viewport_overlay(self, toggle, enabled):
        """
        Set a native Blender viewport overlay to an explicit on/off state.

        A true idempotent setter, unlike ND's pulse-style toggles - calling it
        again with the same enabled value is a no-op.

        Args:
            toggle: One of CAVITY, WIREFRAMES, FACE_ORIENTATION.
            enabled: Desired on/off state.

        Returns:
            Result produced by the operation.

        Raises:
            ValueError: If the operation cannot be completed.
            RuntimeError: If the operation cannot be completed.

        """
        toggle = str(toggle).upper()
        mapping = self._OVERLAY_TOGGLES.get(toggle)
        if mapping is None:
            raise ValueError(f"Invalid toggle: {toggle}. Must be one of {sorted(self._OVERLAY_TOGGLES)}")
        holder_attr, overlay_prop = mapping
        area, _region = find_view3d()
        if area is None:
            raise RuntimeError("No 3D viewport found to toggle")
        space = area.spaces.active
        holder = getattr(space, holder_attr)
        setattr(holder, overlay_prop, bool(enabled))
        return {"toggle": toggle, "enabled": bool(enabled)}

    def get_viewport_screenshot(self, max_size=800, filepath=None, format="png", view=None, shading_override=None):
        """
        Capture a screenshot of the current 3D viewport and save it to the specified path.

        Args:
            max_size: Maximum size in pixels for the largest dimension of the image
            filepath: Path where to save the screenshot file
            format: Image format (png, jpg, etc.)
            view: Optional ViewSpec fields (dict) to capture from an ad hoc camera_object or
                eye/target/target_object view instead of the live viewport's own navigation.
                None captures exactly what the viewport is currently showing (unchanged
                default behavior).
            shading_override: Optional "SOLID" or "MATERIAL" to force that viewport shading
                for just this capture, restoring the live viewport's shading afterward.

        Returns:
            success/error status, plus "view_source" ("live_viewport", "camera_object", or
            "eye_target") and "shading_mode" (the space.shading.type actually used).

        """
        # screen.screenshot_area captures the OS window framebuffer, which is
        # all-black whenever the Blender window is not composited in the
        # foreground (the normal case when Blender is driven headless-style via
        # MCP). Render the viewport with gpu.types.GPUOffScreen.draw_view3d
        # instead, which is independent of window compositing state, and fall
        # back to the window grab if offscreen rendering is unavailable (e.g. no
        # GPU context). The response reports which path produced the image.
        try:
            if not filepath:
                return {"error": "No filepath provided"}
            if shading_override is not None and shading_override not in _SHADING_OVERRIDES:
                allowed = sorted(_SHADING_OVERRIDES)
                return {"error": f"Invalid shading_override: {shading_override}. Must be one of {allowed}"}

            area = region = space = None
            for a in bpy.context.screen.areas:
                if a.type == "VIEW_3D":
                    area = a
                    space = a.spaces.active
                    region = next((r for r in a.regions if r.type == "WINDOW"), None)
                    break

            if not area or region is None or space is None:
                return {"error": "No 3D viewport found"}

            if view is not None:
                view_matrix, window_matrix, view_source = _synthetic_view_matrices(view, bpy.context.scene)
            else:
                r3d = space.region_3d
                view_matrix, window_matrix, view_source = r3d.view_matrix, r3d.window_matrix, "live_viewport"

            return _capture_view(
                area,
                region,
                space,
                view_matrix,
                window_matrix,
                view_source,
                max_size,
                filepath,
                format,
                shading_override,
            )

        except Exception as e:
            return {"error": str(e)}
