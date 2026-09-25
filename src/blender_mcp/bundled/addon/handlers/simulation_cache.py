"""Shared cache identity, state, frame-range, confirmation, and rollback primitives."""

import contextlib
import os

import bpy


def serialize_cache_value(value):
    """Convert common RNA cache values to JSON-safe primitives."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    try:
        return list(value)
    except TypeError:
        return str(value)


def point_cache_info(cache):
    """Return the normalized PointCache state used by cloth and rigid bodies."""
    fields = (
        "name",
        "index",
        "filepath",
        "frame_start",
        "frame_end",
        "frame_step",
        "use_disk_cache",
        "use_external",
        "use_library_path",
        "is_baked",
        "is_baking",
        "is_outdated",
        "is_frame_skip",
        "info",
    )
    return {
        "cache_kind": "POINT_CACHE",
        **{name: serialize_cache_value(getattr(cache, name)) for name in fields if hasattr(cache, name)},
    }


def point_cache_identity(cache):
    """Return a collision-relevant external PointCache identity, if one exists."""
    if not getattr(cache, "use_external", False) or not getattr(cache, "filepath", ""):
        return None
    return (
        "EXTERNAL",
        os.path.normcase(os.path.normpath(bpy.path.abspath(cache.filepath))),
        str(cache.name),
        int(cache.index),
    )


def set_cache_frame_range(cache, frame_start, frame_end, *, start_name="frame_start", end_name="frame_end"):
    """
    Set a cache's frame range without transiently inverting it, and refuse one Blender did not keep.

    A PointCache (cloth, rigid bodies) names its bounds `frame_start`/`frame_end`; a Mantaflow
    domain names them `cache_frame_start`/`cache_frame_end`, hence `start_name`/`end_name`.
    The bound that would otherwise cross the current one is written first, and both are read
    back, because Blender clamps an assignment silently instead of raising.

    Raises:
        ValueError: The range is inverted, or Blender holds a different range after the writes.

    """
    if frame_start > frame_end:
        raise ValueError(f"{start_name} must be <= {end_name}")
    if frame_start > getattr(cache, end_name):
        setattr(cache, end_name, frame_end)
        setattr(cache, start_name, frame_start)
    else:
        setattr(cache, start_name, frame_start)
        setattr(cache, end_name, frame_end)
    kept = (getattr(cache, start_name), getattr(cache, end_name))
    if kept != (frame_start, frame_end):
        raise ValueError(
            "Blender did not retain the requested cache frame range "
            f"[{frame_start}, {frame_end}] (got [{kept[0]}, {kept[1]}])"
        )


def require_cache_confirmation(action, *, confirm_bake=False, confirm_free=False):
    """Apply consistent explicit confirmation gates to costly or destructive cache actions."""
    normalized = str(action).upper()
    if normalized.startswith(("BAKE", "START_BAKE", "RESUME")) and not confirm_bake:
        raise ValueError(f"{normalized} requires confirm_bake=True")
    if normalized.startswith("FREE") and not confirm_free:
        raise ValueError(f"{normalized} requires confirm_free=True")


@contextlib.contextmanager
def rollback_properties(owner, names):
    """Restore selected writable RNA properties if a cache configuration block fails."""
    previous = {name: getattr(owner, name) for name in names}
    try:
        yield previous
    except Exception:
        for name, value in previous.items():
            with contextlib.suppress(Exception):
                setattr(owner, name, value)
        raise


def mantaflow_cache_info(settings, stage_flags, configuration_fields):
    """Return the same top-level cache contract for Mantaflow domains."""
    return {
        "cache_kind": "MANTAFLOW",
        "configuration": {
            name: serialize_cache_value(getattr(settings, name))
            for name in configuration_fields
            if hasattr(settings, name)
        },
        "stages": {name: bool(getattr(settings, name, False)) for name in stage_flags},
    }
