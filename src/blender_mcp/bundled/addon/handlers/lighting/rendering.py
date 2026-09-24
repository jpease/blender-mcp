# pyright: reportGeneralTypeIssues=false, reportOptionalSubscript=false
"""Lighting quality, color-management, and bounded preview-render handlers."""

import os

import bpy

from ...helpers import color_management_snapshot
from ...render_devices import effective_cycles_device
from ...render_properties import LIGHTING_CYCLES_FIELDS, LIGHTING_EEVEE_FIELD_MAP
from ...render_result_record import restore_render_result_record, snapshot_render_result_record
from ._shared import (
    finite_number,
    object_in_scene,
    patch_properties,
    resolve_engine,
    scene_by_name,
)
from .inspection import _quality_snapshot

QUALITY_PRESETS = {
    "PREVIEW": {
        "cycles": {"samples": 32, "use_adaptive_sampling": True, "adaptive_threshold": 0.1, "use_denoising": True},
        "eevee": {"render_samples": 16, "shadow_ray_count": 1, "shadow_step_count": 4, "volumetric_samples": 32},
    },
    "BALANCED": {
        "cycles": {"samples": 128, "use_adaptive_sampling": True, "adaptive_threshold": 0.03, "use_denoising": True},
        "eevee": {"render_samples": 64, "shadow_ray_count": 2, "shadow_step_count": 8, "volumetric_samples": 64},
    },
    "FINAL": {
        "cycles": {"samples": 512, "use_adaptive_sampling": True, "adaptive_threshold": 0.01, "use_denoising": True},
        "eevee": {"render_samples": 128, "shadow_ray_count": 4, "shadow_step_count": 16, "volumetric_samples": 128},
    },
}
MAX_RENDER_RESULT_FLOATS = 16 * 1024 * 1024
_MIN_FRAME = -1_048_574
_MAX_FRAME = 1_048_574
_MIN_PREVIEW_SIZE = 16
_MAX_PREVIEW_SIZE = 1024
_MAX_PREVIEW_SAMPLES = 1024


def _validate_quality_owner(owner, patch, allowed):
    """Preflight every quality property and enum before the first assignment."""
    unknown = set(patch) - allowed
    if unknown:
        raise ValueError(f"Unsupported quality fields: {sorted(unknown)}")
    missing = [field for field in patch if not hasattr(owner, field)]
    if missing:
        raise ValueError(f"Running Blender does not support quality fields: {missing}")
    for field, value in patch.items():
        prop = owner.bl_rna.properties[field]
        if prop.type == "ENUM" and value not in {item.identifier for item in prop.enum_items}:
            raise ValueError(f"{field} must be one of {sorted(item.identifier for item in prop.enum_items)}")


def _translated_eevee_patch(patch):
    """Translate the agent-facing render_samples name to Blender's runtime RNA field."""
    unknown = set(patch) - set(LIGHTING_EEVEE_FIELD_MAP)
    if unknown:
        raise ValueError(f"Unsupported EEVEE quality fields: {sorted(unknown)}")
    return {LIGHTING_EEVEE_FIELD_MAP[field]: value for field, value in patch.items()}


def _restore_properties(changes):
    """Restore a list of owner/field/native-value records in reverse order."""
    for owner, field, value in reversed(changes):
        setattr(owner, field, value)


def _snapshot_render_result():
    """Capture the current Render Result pixels or reject an unbounded restore cost."""
    image = bpy.data.images.get("Render Result")
    if image is None:
        return None
    pixel_count = len(image.pixels)
    if pixel_count > MAX_RENDER_RESULT_FLOATS:
        raise ValueError(
            "Existing Render Result is too large to restore safely; save or clear it before requesting a preview"
        )
    return {"image": image, "size": tuple(image.size), "pixels": list(image.pixels)}


def _restore_render_result(snapshot):
    """
    Restore or remove the Render Result changed by a preview render.

    Returns True only when the prior state is really back. Blender 5.2 exposes a rendered
    Render Result as a (0, 0) image with no pixels, so a snapshot without pixels captured nothing
    to put back: the preview has replaced it, and saying otherwise would hide that.
    """
    current = bpy.data.images.get("Render Result")
    if snapshot is None:
        if current is not None:
            try:
                bpy.data.images.remove(current)
                return True
            except Exception:
                return False
        return True
    if not snapshot["pixels"]:
        return False
    image = snapshot["image"]
    try:
        if tuple(image.size) != snapshot["size"]:
            image.scale(*snapshot["size"])
        image.pixels.foreach_set(snapshot["pixels"])
        image.update()
        return True
    except Exception:
        return False


def _matched_state(scene):
    """Name the lights and world state a preview was rendered under, without echoing their state."""
    names = sorted(obj.name for obj in scene.objects if obj.type == "LIGHT")
    return {
        "world": scene.world.name if scene.world else None,
        "exposure": float(scene.view_settings.exposure),
        "view_transform": scene.view_settings.view_transform,
        "lights": names,
        "light_count": len(names),
    }


def _quality_patches(target_engine, preset, cycles, eevee):
    """Merge a preset under explicit settings and refuse patches that do not fit target_engine."""
    if target_engine not in {"CYCLES", "EEVEE", "BOTH"}:
        raise ValueError("target_engine must be CYCLES, EEVEE, or BOTH")
    if preset is not None and preset not in QUALITY_PRESETS:
        raise ValueError(f"preset must be one of {sorted(QUALITY_PRESETS)}")
    cycles_patch = dict(cycles or {})
    eevee_patch = dict(eevee or {})
    if preset is not None:
        preset_values = QUALITY_PRESETS[preset]
        if target_engine in {"CYCLES", "BOTH"}:
            cycles_patch = {**preset_values["cycles"], **cycles_patch}
        if target_engine in {"EEVEE", "BOTH"}:
            eevee_patch = {**preset_values["eevee"], **eevee_patch}
    if target_engine == "CYCLES" and eevee_patch:
        raise ValueError("EEVEE settings do not apply to target_engine='CYCLES'")
    if target_engine == "EEVEE" and cycles_patch:
        raise ValueError("Cycles settings do not apply to target_engine='EEVEE'")
    if target_engine == "BOTH" and (not cycles_patch or not eevee_patch):
        raise ValueError("target_engine='BOTH' requires settings for both engines")
    if not cycles_patch and not eevee_patch:
        raise ValueError("Provide a preset or at least one engine quality setting")
    return cycles_patch, eevee_patch


def _validate_preview_request(camera, camera_name, target_engine, frame, width, height, samples):
    """Refuse a non-camera, an unknown engine, and an out-of-range frame, size or sample count."""
    if camera.type != "CAMERA":
        raise ValueError(f"Object '{camera_name}' is not a camera")
    if target_engine not in {"CYCLES", "EEVEE", "BOTH"}:
        raise ValueError("target_engine must be CYCLES, EEVEE, or BOTH")
    if isinstance(frame, bool) or int(frame) != frame or not _MIN_FRAME <= int(frame) <= _MAX_FRAME:
        raise ValueError("frame must be a valid Blender frame integer")
    for value, label in ((width, "width"), (height, "height")):
        if isinstance(value, bool) or int(value) != value or not _MIN_PREVIEW_SIZE <= int(value) <= _MAX_PREVIEW_SIZE:
            raise ValueError(f"{label} must be an integer in [16, 1024]")
    if isinstance(samples, bool) or int(samples) != samples or not 1 <= int(samples) <= _MAX_PREVIEW_SAMPLES:
        raise ValueError("samples must be an integer in [1, 1024]")


def _preview_output_paths(engines, output_paths, confirm_overwrite):
    """Validate one distinct absolute .png per engine, in an existing directory, overwritten only on request."""
    paths = dict(output_paths or {})
    if set(paths) != set(engines):
        raise ValueError(f"output_paths must contain exactly {engines}")
    if len(set(paths.values())) != len(paths):
        raise ValueError("Each preview engine requires a distinct output path")
    for engine, path in paths.items():
        if not isinstance(path, str) or not os.path.isabs(path) or os.path.splitext(path)[1].lower() != ".png":
            raise ValueError(f"{engine} output path must be an absolute .png path")
        parent = os.path.dirname(path)
        if not os.path.isdir(parent):
            raise ValueError(f"Output directory does not exist: {parent}")
        if os.path.exists(path) and not confirm_overwrite:
            raise ValueError(f"Output exists; set confirm_overwrite=true to replace: {path}")
    return paths


def _preview_render_state(scene):
    """Capture the scene and render fields a preview render temporarily overrides."""
    render = scene.render
    return {
        "engine": render.engine,
        "filepath": render.filepath,
        "resolution_x": render.resolution_x,
        "resolution_y": render.resolution_y,
        "resolution_percentage": render.resolution_percentage,
        "file_format": render.image_settings.file_format,
        "color_mode": render.image_settings.color_mode,
        "camera": scene.camera,
        "frame": scene.frame_current,
        "cycles_samples": getattr(scene.cycles, "samples", None),
        "eevee_samples": getattr(scene.eevee, "taa_render_samples", None),
    }


def _restore_preview_render_state(scene, old):
    """Put back the fields `_preview_render_state` captured."""
    render = scene.render
    render.engine = old["engine"]
    render.filepath = old["filepath"]
    render.resolution_x = old["resolution_x"]
    render.resolution_y = old["resolution_y"]
    render.resolution_percentage = old["resolution_percentage"]
    render.image_settings.file_format = old["file_format"]
    render.image_settings.color_mode = old["color_mode"]
    scene.camera = old["camera"]
    scene.frame_set(old["frame"])
    if old["cycles_samples"] is not None:
        scene.cycles.samples = old["cycles_samples"]
    if old["eevee_samples"] is not None:
        scene.eevee.taa_render_samples = old["eevee_samples"]


def _render_preview(scene, engine, runtime_engine, samples, path):
    """Render one engine's still to `path` and describe the non-empty PNG it wrote."""
    render = scene.render
    render.engine = runtime_engine
    if engine == "CYCLES":
        scene.cycles.samples = int(samples)
    else:
        scene.eevee.taa_render_samples = int(samples)
    render.filepath = path
    with bpy.context.temp_override(scene=scene):
        result = bpy.ops.render.render(write_still=True, scene=scene.name)
    if not isinstance(result, (set, frozenset)) or "FINISHED" not in result:
        raise RuntimeError(f"{engine} render did not finish: {result}")
    if not os.path.isfile(path) or os.path.getsize(path) <= 0:
        raise RuntimeError(f"{engine} render did not create a non-empty PNG: {path}")
    return {
        "engine": engine,
        "runtime_engine": runtime_engine,
        "path": path,
        "size_bytes": os.path.getsize(path),
        "samples": int(samples),
    }


class LightingRenderHandlers:
    """Configure lighting-sensitive render state and produce state-restored preview images."""

    def configure_lighting_quality(
        self,
        scene_name,
        target_engine,
        preset=None,
        cycles=None,
        eevee=None,
        detail=False,
    ):
        """Atomically patch allowlisted Cycles and/or EEVEE lighting-quality properties."""
        scene = scene_by_name(scene_name)
        cycles_patch, eevee_patch = _quality_patches(target_engine, preset, cycles, eevee)
        if cycles_patch:
            resolve_engine("CYCLES")
        if eevee_patch:
            resolve_engine("EEVEE")
        cycles_owner = getattr(scene, "cycles", None)
        eevee_owner = getattr(scene, "eevee", None)
        if cycles_patch and cycles_owner is None:
            raise ValueError("Running Blender does not expose Cycles scene settings")
        if eevee_patch and eevee_owner is None:
            raise ValueError("Running Blender does not expose EEVEE scene settings")
        translated_eevee = _translated_eevee_patch(eevee_patch)
        _validate_quality_owner(cycles_owner, cycles_patch, LIGHTING_CYCLES_FIELDS)
        _validate_quality_owner(eevee_owner, translated_eevee, set(LIGHTING_EEVEE_FIELD_MAP.values()))
        before = _quality_snapshot(scene) if detail else None
        applied = {}
        changes = []
        try:
            for prefix, owner, patch, field_map in (
                ("cycles.", cycles_owner, cycles_patch, {}),
                ("eevee.", eevee_owner, eevee_patch, LIGHTING_EEVEE_FIELD_MAP),
            ):
                for field, value in patch.items():
                    rna_field = field_map.get(field, field)
                    changes.append((owner, rna_field, getattr(owner, rna_field)))
                    setattr(owner, rna_field, value)
                    applied[prefix + field] = (owner, rna_field)
        except Exception:
            _restore_properties(changes)
            raise
        # What Cycles will actually render on here: `cycles.device` only asks for a GPU, and a
        # request Preferences cannot honour is written anyway (a .blend may be bound for a GPU
        # farm) but reported, so a CPU render is never a surprise.
        effective_device, device_warning = effective_cycles_device(scene)
        reply = {
            "scene": scene.name,
            "target_engine": target_engine,
            "preset": preset,
            "changed": sorted(applied),
            "effective_cycles_device": effective_device,
            "changed_resources": [scene.name],
        }
        if detail:
            reply.update(before=before, after=_quality_snapshot(scene))
        else:
            reply["after"] = {path: getattr(owner, field) for path, (owner, field) in applied.items()}
        if device_warning:
            reply["warnings"] = [device_warning]
        return reply

    def configure_color_management(
        self,
        scene_name,
        view_transform=None,
        look=None,
        exposure=None,
        gamma=None,
    ):
        """Atomically patch OCIO-validated display transform fields."""
        scene = scene_by_name(scene_name)
        patch = {
            key: value
            for key, value in {
                "view_transform": view_transform,
                "look": look,
                "exposure": exposure,
                "gamma": gamma,
            }.items()
            if value is not None
        }
        if not patch:
            raise ValueError("Provide at least one color-management setting")
        settings = scene.view_settings
        if "exposure" in patch and not -32 <= finite_number(patch["exposure"], "exposure") <= 32:
            raise ValueError("exposure must be in [-32, 32]")
        if "gamma" in patch and not 0 < finite_number(patch["gamma"], "gamma") <= 5:
            raise ValueError("gamma must be in (0, 5]")
        before = color_management_snapshot(scene)
        try:
            patch_properties(settings, patch, {"view_transform", "look", "exposure", "gamma"})
        except TypeError as exc:
            raise ValueError(f"Color-management value is unavailable in the active OCIO configuration: {exc}") from exc
        after = color_management_snapshot(scene)
        return {
            "scene": scene.name,
            "before": before,
            "after": after,
            "exposure_multiplier": float(2.0**settings.exposure),
            "changed_resources": [scene.name],
        }

    def render_lighting_preview(
        self,
        scene_name,
        camera_name,
        frame,
        target_engine,
        width=512,
        height=512,
        samples=32,
        output_paths=None,
        confirm_overwrite=False,
    ):
        """Render bounded PNG previews and restore all temporary scene/render state."""
        scene = scene_by_name(scene_name)
        camera = object_in_scene(scene, camera_name)
        _validate_preview_request(camera, camera_name, target_engine, frame, width, height, samples)
        engines = [target_engine] if target_engine != "BOTH" else ["CYCLES", "EEVEE"]
        resolved_engines = {engine: resolve_engine(engine) for engine in engines}
        paths = _preview_output_paths(engines, output_paths, confirm_overwrite)
        render_result = _snapshot_render_result()
        # Each preview render clears render_scene's record of what Render Result holds.
        prior_record = snapshot_render_result_record()
        render = scene.render
        old = _preview_render_state(scene)
        outputs = []
        restore_warning = None
        try:
            scene.camera = camera
            scene.frame_set(int(frame))
            render.resolution_x = int(width)
            render.resolution_y = int(height)
            render.resolution_percentage = 100
            render.image_settings.file_format = "PNG"
            render.image_settings.color_mode = "RGBA"
            for engine in engines:
                outputs.append(_render_preview(scene, engine, resolved_engines[engine], samples, paths[engine]))
        finally:
            _restore_preview_render_state(scene, old)
            if not _restore_render_result(render_result):
                restore_warning = (
                    "The prior Render Result could not be restored and now holds this preview; "
                    "scene render settings were restored."
                )
            elif render_result is not None:
                # Its prior pixels are back, so whatever rendered them is again what it holds.
                restore_render_result_record(prior_record)
        return {
            "scene": scene.name,
            "camera": camera.name,
            "frame": int(frame),
            "width": int(width),
            "height": int(height),
            "outputs": outputs,
            "matched_state": _matched_state(scene),
            "warnings": [restore_warning] if restore_warning else [],
            "changed_objects": [],
            "changed_resources": [],
        }
