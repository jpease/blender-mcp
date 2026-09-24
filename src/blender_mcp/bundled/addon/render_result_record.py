"""
Which render_scene frame the in-memory Render Result holds, when that is known.

Render Result holds whatever rendered last - render_scene, a preview tool, a render
started from Blender's UI - and exposes nothing that says which. render_scene
records the scene, frame and output path of each frame it finishes; every render
start clears the record, so a render this add-on did not record leaves none behind
and `inspect_render_output` reports the pixels' origin as unknown instead of
mislabelling them. A record made against a database that has since been replaced
is ignored.

Main thread only: render handlers and add-on commands both run there.
"""

import bpy

from bpy.app.handlers import persistent

from .session import session_snapshot


class _Store:
    """
    Hold the one record, so writers rebind an attribute rather than a module global.

    Attributes:
        record: The last render_scene frame still held by Render Result, or None.

    """

    record: dict | None = None


_STORE = _Store()


def _session_marker() -> tuple[object, object]:
    """
    Read the pair that identifies the open database, as `server_core` does.

    Returns:
        tuple: `(session_id, session_epoch)`.

    """
    snapshot = session_snapshot()
    return (snapshot["session_id"], snapshot["session_epoch"])


def record_render_result(scene_name: str, frame: int, output_path: str | None) -> None:
    """
    Record that Render Result now holds one frame render_scene just finished.

    Args:
        scene_name: The scene that rendered.
        frame: The frame rendered.
        output_path: The file that frame was written to, or None when it is missing.

    """
    _STORE.record = {
        "scene": scene_name,
        "frame": frame,
        "output_path": output_path,
        "session": _session_marker(),
    }


def render_result_record() -> dict | None:
    """
    Return what Render Result holds, when render_scene rendered it into this database.

    Returns:
        dict | None: "scene", "frame" and "output_path", or None when the origin is unknown.

    """
    record = _STORE.record
    if record is None or record["session"] != _session_marker():
        return None
    return {key: record[key] for key in ("scene", "frame", "output_path")}


def snapshot_render_result_record() -> dict | None:
    """
    Take the raw record, so a caller that restores Render Result's pixels can restore it too.

    Returns:
        dict | None: An opaque value for `restore_render_result_record`.

    """
    return _STORE.record


def restore_render_result_record(snapshot: dict | None) -> None:
    """
    Put back a record taken before a render whose pixels were then restored.

    Args:
        snapshot: A value from `snapshot_render_result_record`.

    """
    _STORE.record = snapshot


@persistent
def _forget_on_render_init(_scene: object = None, _unused: object = None) -> None:
    """
    Clear the record as any render starts, whoever started it.

    Args:
        _scene: The scene about to render.
        _unused: Blender's second handler argument.

    """
    _STORE.record = None


def register_handlers() -> None:
    """Attach the render-start handler, exactly once; Blender's handler lists accept duplicates."""
    if _forget_on_render_init not in bpy.app.handlers.render_init:
        bpy.app.handlers.render_init.append(_forget_on_render_init)


def unregister_handlers() -> None:
    """Detach the render-start handler, including any a previous cycle stacked."""
    while _forget_on_render_init in bpy.app.handlers.render_init:
        bpy.app.handlers.render_init.remove(_forget_on_render_init)
