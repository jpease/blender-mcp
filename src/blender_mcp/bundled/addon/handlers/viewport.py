import bpy

from ..helpers import find_view3d
from ..image_reply import finalize_image_reply, image_destination


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

    def get_viewport_screenshot(self, max_size=800, filepath=None, format="png", inline=False):
        """
        Capture a screenshot of the current 3D viewport and save it to the specified path.

        Args:
            max_size: Maximum size in pixels for the largest dimension of the image
            filepath: Path where to save the screenshot file
            inline: Return the image bytes in the reply instead of writing to filepath
            format: Image format (png, jpg, etc.)

        Returns:
            success/error status

        """
        # screen.screenshot_area captures the OS window framebuffer, which is
        # all-black whenever the Blender window is not composited in the
        # foreground (the normal case when Blender is driven headless-style via
        # MCP). Render the viewport with gpu.types.GPUOffScreen.draw_view3d
        # instead, which is independent of window compositing state, and fall
        # back to the window grab if offscreen rendering is unavailable (e.g. no
        # GPU context). The response reports which path produced the image.
        try:
            with image_destination(inline, filepath) as path:
                return finalize_image_reply(self._capture_viewport(path, max_size, format), inline=inline, path=path)

        except Exception as e:
            return {"error": str(e)}

    @staticmethod
    def _capture_viewport(path, max_size, format):
        """
        Render the 3D viewport to `path` and describe what was written.

        Args:
            path: Destination the image is written to.
            max_size: Maximum size in pixels for the largest dimension.
            format: Image format (png, jpg, etc.)

        Returns:
            dict: width/height/filepath/method for the written image.

        Raises:
            RuntimeError: If no 3D viewport is available to capture.

        """
        area = region = space = None
        for a in bpy.context.screen.areas:
            if a.type == "VIEW_3D":
                area = a
                space = a.spaces.active
                region = next((r for r in a.regions if r.type == "WINDOW"), None)
                break

        if not area or region is None or space is None:
            raise RuntimeError("No 3D viewport found")

        method = "offscreen"
        try:
            import gpu
            import numpy as np

            r3d = space.region_3d
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
                    r3d.view_matrix,
                    r3d.window_matrix,
                    do_color_management=True,
                )
                buf = offscreen.texture_color.read()
            finally:
                offscreen.free()

            buf.dimensions = width * height * 4
            pixels = np.asarray(buf, dtype=np.float32) / 255.0  # GPU buffer is 0..255

            image = bpy.data.images.new("mcp_viewport", width, height, alpha=True)
            image.pixels.foreach_set(pixels.ravel())
            image.filepath_raw = path
            image.file_format = format.upper()
            image.save()
            bpy.data.images.remove(image)

        except Exception as offscreen_err:
            print(
                f"[BlenderMCP] offscreen capture failed ({offscreen_err}); falling back to window grab",
                flush=True,
            )
            method = "window_grab"
            with bpy.context.temp_override(area=area):
                bpy.ops.screen.screenshot_area(filepath=path)
            img = bpy.data.images.load(path)
            width, height = img.size
            if max(width, height) > max_size:
                s = max_size / max(width, height)
                width, height = int(width * s), int(height * s)
                img.scale(width, height)
                img.file_format = format.upper()
                img.save()
            bpy.data.images.remove(img)

        return {
            "success": True,
            "width": width,
            "height": height,
            "filepath": path,
            "method": method,
        }
