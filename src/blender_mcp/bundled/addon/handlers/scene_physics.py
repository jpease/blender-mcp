# ruff: file-ignore[undocumented-public-method]
"""Blender-side scene unit, gravity, and playback-sync handlers."""

import math

from contextlib import suppress

import bpy

_UNIT_SYSTEMS = {"NONE", "METRIC", "IMPERIAL"}
_SYNC_MODES = {"NONE", "FRAME_DROP", "AUDIO_SYNC"}
_UNIT_PROPERTIES = {"system", "scale_length"}
_SCENE_PROPERTIES = {"sync_mode", "use_gravity", "gravity"}
_PHYSICS_PATCH_PROPERTIES = _UNIT_PROPERTIES | _SCENE_PROPERTIES
_MAX_CONVERT_SECONDS = 32
_MIN_SCALE_LENGTH = 0.001
_MAX_SCALE_LENGTH = 100.0
_GRAVITY_COMPONENTS = 3


def _scene(name):
    scene = bpy.data.scenes.get(name) if name else bpy.context.scene
    if scene is None:
        raise ValueError(f"Scene not found: {name}")
    return scene


def _set_properties(owner, patch):
    previous = {}
    try:
        for name, value in patch.items():
            previous[name] = getattr(owner, name)
            setattr(owner, name, value)
    except Exception:
        for name, value in previous.items():
            with suppress(Exception):
                setattr(owner, name, value)
        raise
    return previous


def _validate_physics_patch(patch):
    if not isinstance(patch, dict) or not patch:
        raise ValueError("patch must be a non-empty object")
    unknown = sorted(set(patch) - _PHYSICS_PATCH_PROPERTIES)
    if unknown:
        raise ValueError(f"Unsupported scene physics settings: {unknown}")
    if "system" in patch and patch["system"] not in _UNIT_SYSTEMS:
        raise ValueError(f"Unsupported unit system: {patch['system']}")
    if "scale_length" in patch:
        value = patch["scale_length"]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("scale_length must be a finite number")
        if not (_MIN_SCALE_LENGTH <= value <= _MAX_SCALE_LENGTH):
            raise ValueError(f"scale_length must be between {_MIN_SCALE_LENGTH} and {_MAX_SCALE_LENGTH}")
    if "sync_mode" in patch and patch["sync_mode"] not in _SYNC_MODES:
        raise ValueError(f"Unsupported sync_mode: {patch['sync_mode']}")
    if "gravity" in patch:
        gravity = patch["gravity"]
        if not isinstance(gravity, (list, tuple)) or len(gravity) != _GRAVITY_COMPONENTS:
            raise ValueError("gravity must be a 3-component [x, y, z] sequence")
        for component in gravity:
            if isinstance(component, bool) or not isinstance(component, (int, float)) or not math.isfinite(component):
                raise ValueError("gravity components must be finite numbers")
    if "use_gravity" in patch and not isinstance(patch["use_gravity"], bool):
        raise ValueError("use_gravity must be a boolean")
    return patch


def _scene_fps(scene):
    return scene.render.fps / scene.render.fps_base if scene.render.fps_base else float(scene.render.fps)


def _seconds_to_frame_table(scene, convert_seconds):
    if convert_seconds is None:
        return None
    if not isinstance(convert_seconds, (list, tuple)) or len(convert_seconds) > _MAX_CONVERT_SECONDS:
        raise ValueError(f"convert_seconds must be a list of at most {_MAX_CONVERT_SECONDS} numbers")
    fps = _scene_fps(scene)
    table = []
    for seconds in convert_seconds:
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds):
            raise ValueError("convert_seconds entries must be finite numbers")
        table.append({"seconds": float(seconds), "frame": scene.frame_start + float(seconds) * fps})
    return table


def _physics_info(scene):
    return {
        "scene": scene.name,
        "unit_settings": {
            "system": scene.unit_settings.system,
            "scale_length": float(scene.unit_settings.scale_length),
            "length_unit": scene.unit_settings.length_unit,
        },
        "gravity": list(scene.gravity),
        "use_gravity": scene.use_gravity,
        "sync_mode": scene.sync_mode,
        "fps": _scene_fps(scene),
        "frame_start": scene.frame_start,
        "frame_end": scene.frame_end,
    }


def _non_default_scale_object_names(scene):
    return sorted(
        obj.name
        for obj in scene.objects
        if obj.type == "MESH" and tuple(round(component, 6) for component in obj.scale) != (1.0, 1.0, 1.0)
    )


class ScenePhysicsHandlersMixin:
    """Expose scene-wide unit, gravity, and playback-sync configuration."""

    def get_scene_physics_info(self, scene_name=None, convert_seconds=None):
        scene = _scene(scene_name)
        info = _physics_info(scene)
        table = _seconds_to_frame_table(scene, convert_seconds)
        if table is not None:
            info["seconds_to_frame"] = table
        return info

    def configure_scene_physics(self, scene_name, patch):
        scene = _scene(scene_name)
        patch = _validate_physics_patch(dict(patch))
        warnings = []
        if "system" in patch or "scale_length" in patch:
            affected = _non_default_scale_object_names(scene)
            if affected:
                warnings.append(
                    "Changing the unit system/scale does not rescale existing geometry, only how new "
                    f"values are interpreted; these mesh objects already have non-uniform/non-1.0 scale "
                    f"and may now read as a different real-world size: {affected}"
                )
        unit_patch = {k: v for k, v in patch.items() if k in _UNIT_PROPERTIES}
        scene_patch = {k: v for k, v in patch.items() if k in _SCENE_PROPERTIES}
        snapshots = []
        try:
            if unit_patch:
                snapshots.append((scene.unit_settings, _set_properties(scene.unit_settings, unit_patch)))
            if scene_patch:
                snapshots.append((scene, _set_properties(scene, scene_patch)))
        except Exception:
            for owner, values in reversed(snapshots):
                for name, value in values.items():
                    with suppress(Exception):
                        setattr(owner, name, value)
            raise
        return {
            "changed": sorted(patch),
            "settings": _physics_info(scene),
            "changed_resources": [scene.name],
            "warnings": warnings,
        }
