"""
Rows guarding the add-on's drain timer and the viewport capture's cleanup.

Label prefixes: `transport:`, `viewport:`.
"""

from .common import ADDON_SERVER_CORE, ADDON_VIEWPORT, THREADT, VIEWT, Revert

ROWS: list[Revert] = [
    # --- the drain timer follows the traffic instead of a flat 50 ms poll ---
    Revert(
        "transport: the drain timer back to a flat 50 ms poll",
        ADDON_SERVER_CORE,
        "        return self._poll_interval()",
        "        return 0.05",
        (f"{THREADT}::test_a_command_makes_the_next_drain_follow_within_the_active_poll",),
    ),
    Revert(
        "transport: the fast poll never relaxing back to the idle rate",
        ADDON_SERVER_CORE,
        """        if time.monotonic() - self._last_command_at < self._ACTIVE_WINDOW_SECONDS:
            return self._ACTIVE_POLL_SECONDS
        return self._IDLE_POLL_SECONDS""",
        "        return self._ACTIVE_POLL_SECONDS",
        (
            f"{THREADT}::test_the_drain_poll_relaxes_once_the_session_goes_quiet",
            f"{THREADT}::test_a_server_that_has_served_nothing_polls_at_the_idle_rate",
        ),
    ),
    # --- a failed viewport capture leaves no datablock behind ---
    Revert(
        "viewport: the offscreen capture's image removed only when the save succeeds",
        ADDON_VIEWPORT,
        """    try:
        image.pixels.foreach_set(pixels.ravel())
        image.filepath_raw = filepath
        image.file_format = image_format.upper()
        image.save()
    finally:
        bpy.data.images.remove(image)""",
        """    image.pixels.foreach_set(pixels.ravel())
    image.filepath_raw = filepath
    image.file_format = image_format.upper()
    image.save()
    bpy.data.images.remove(image)""",
        (f"{VIEWT}::test_offscreen_capture_removes_its_image_datablock_when_the_save_fails",),
    ),
    Revert(
        "viewport: the window grab's loaded screenshot removed only when the rescale succeeds",
        ADDON_VIEWPORT,
        """    try:
        width, height = img.size
        if max(width, height) > max_size:
            s = max_size / max(width, height)
            width, height = int(width * s), int(height * s)
            img.scale(width, height)
            img.file_format = image_format.upper()
            img.save()
    finally:
        # Same reason as _render_offscreen's: a failed scale/save must not leave the loaded
        # screenshot sitting in bpy.data.images.
        bpy.data.images.remove(img)""",
        """    width, height = img.size
    if max(width, height) > max_size:
        s = max_size / max(width, height)
        width, height = int(width * s), int(height * s)
        img.scale(width, height)
        img.file_format = image_format.upper()
        img.save()
    bpy.data.images.remove(img)""",
        (f"{VIEWT}::test_window_grab_removes_the_loaded_screenshot_when_the_rescale_save_fails",),
    ),
]
