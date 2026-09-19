# pyright: reportArgumentType=false, reportGeneralTypeIssues=false
"""Bounded liquid-domain evaluation and Mantaflow cache lifecycle handling."""

import contextlib
import hashlib
import itertools
import json
import math
import os

import bpy

from ...helpers import preserve_mode_and_selection, set_active
from ..simulation_cache import mantaflow_cache_info, require_cache_confirmation
from ._frame_evaluation import _evaluate_frames, _plan_frame_evaluation
from .inspection_and_setup import (
    _CACHE_FIELDS,
    _CACHE_FLAGS,
    _ensure_liquid_uuid,
    _get_domain,
    _patch_rna,
    _resolved_cache_path,
    _restore_rna,
    _validate_rna_value,
    _world_bounds,
)
from .manifest import read_manifest as _read_manifest
from .manifest import write_stage_entry as _write_manifest_entry

_PENDING_BAKE_KEY = "blendermcp_liquid_pending_bake"

_BAKE_STAGES = {
    "DATA": {
        "operator": "bake_data",
        "baked_flag": "has_cache_baked_data",
        "baking_flag": "is_cache_baking_data",
        "pause_flag": "cache_frame_pause_data",
    },
    "GUIDES": {
        "operator": "bake_guides",
        "baked_flag": "has_cache_baked_guide",
        "baking_flag": "is_cache_baking_guide",
        "pause_flag": "cache_frame_pause_guide",
    },
    "MESH": {
        "operator": "bake_mesh",
        "baked_flag": "has_cache_baked_mesh",
        "baking_flag": "is_cache_baking_mesh",
        "pause_flag": "cache_frame_pause_mesh",
    },
    "PARTICLES": {
        "operator": "bake_particles",
        "baked_flag": "has_cache_baked_particles",
        "baking_flag": "is_cache_baking_particles",
        "pause_flag": "cache_frame_pause_particles",
    },
    "NOISE": {
        "operator": "bake_noise",
        "baked_flag": "has_cache_baked_noise",
        "baking_flag": "is_cache_baking_noise",
        "pause_flag": "cache_frame_pause_noise",
    },
    "ALL": {
        "operator": "bake_all",
        "baked_flag": "has_cache_baked_any",
        "baking_flag": "is_cache_baking_any",
        "pause_flag": None,
    },
}

_CACHE_CONFIG_FIELDS = {
    "cache_directory",
    "cache_type",
    "cache_data_format",
    "cache_mesh_format",
    "cache_particle_format",
    "cache_frame_start",
    "cache_frame_end",
    "cache_frame_offset",
    "cache_resumable",
    "openvdb_cache_compress_type",
    "openvdb_data_depth",
}
# OpenVDB compression and precision only apply to OpenVDB-formatted cache stages. Verified against
# Blender 5.2.1: `openvdb_data_depth` is a dynamic enum whose static RNA enum_items reports only
# ['NONE'] while the accepted identifiers are the strings ('32', '16', '8'), so the generic
# _validate_rna_value enum check cannot be used for it; the tool-side Literal pins the valid values.
_OPENVDB_FIELDS = {"openvdb_cache_compress_type", "openvdb_data_depth"}
_OPENVDB_DATA_DEPTHS = ("8", "16", "32")
_OPENVDB_FORMAT_FIELDS = ("cache_data_format", "cache_mesh_format", "cache_particle_format")
_SECONDARY_TOGGLES = (
    "use_spray_particles",
    "use_foam_particles",
    "use_bubble_particles",
    "use_tracer_particles",
)


def _cache_state(settings):
    state = mantaflow_cache_info(settings, _CACHE_FLAGS, _CACHE_FIELDS)
    if hasattr(settings, "openvdb_data_depth"):
        # Blender reports this dynamic enum as a number while only string identifiers are writable;
        # normalize so callers can feed the reported value straight back into a CONFIGURE patch.
        state["configuration"]["openvdb_data_depth"] = str(settings.openvdb_data_depth)
    return state


def _active_cache_flags(settings, names=None):
    candidates = names or _CACHE_FLAGS
    return [name for name in candidates if bool(getattr(settings, name, False))]


def _reject_cache_flags(settings, names, reason):
    active = _active_cache_flags(settings, names)
    if active:
        raise ValueError(f"{reason}; free or finish the exact cache stages first: {active}")


def _update_or_restore(obj, owner, changes):
    try:
        obj.update_tag(refresh={"DATA"})
        bpy.context.view_layer.update()
    except Exception:
        _restore_rna(owner, changes)
        raise


def _scene_context_for_object(obj):
    scenes = [scene for scene in bpy.data.scenes if obj.name in scene.objects]
    if not scenes:
        raise ValueError(f"Object '{obj.name}' is not linked to a scene")
    scene = bpy.context.scene if bpy.context.scene in scenes else scenes[0]
    view_layer = next((layer for layer in scene.view_layers if obj.name in layer.objects), None)
    if view_layer is None:
        raise ValueError(f"Object '{obj.name}' is excluded from every view layer in scene '{scene.name}'")
    return scene, view_layer


def _run_fluid_operator(obj, operator, extra_override=None, call_arg=None, accept_running_modal=False):
    scene, view_layer = _scene_context_for_object(obj)
    with preserve_mode_and_selection():
        set_active(obj)
        if obj.mode != "OBJECT":
            result = bpy.ops.object.mode_set(mode="OBJECT")
            if "FINISHED" not in result:
                raise RuntimeError(f"Could not enter Object Mode for fluid cache operation: {sorted(result)}")
        override = {
            "scene": scene,
            "view_layer": view_layer,
            "object": obj,
            "active_object": obj,
            "selected_objects": [obj],
            "selected_editable_objects": [obj],
        }
        override.update(extra_override or {})
        with bpy.context.temp_override(**override):
            result = operator(call_arg) if call_arg else operator()
    acceptable = {"FINISHED"} | ({"RUNNING_MODAL"} if accept_running_modal else set())
    if not acceptable & set(result):
        raise RuntimeError(f"Fluid cache operator did not finish: {sorted(result)}")
    return result


def _has_gui_window():
    """True only when this Blender process has a real window manager (never true under --background)."""
    try:
        return not bpy.app.background and bool(bpy.context.window_manager.windows)
    except AttributeError:
        return False


def _start_fluid_bake_job(obj, operator):
    """
    Start a fluid bake as a non-blocking, pollable Blender WM job when a GUI window is available.

    Blender's fluid bake operators only become non-blocking (INVOKE_DEFAULT, returning RUNNING_MODAL
    while the job runs in the background) when invoked under a real window/area/region context; the
    default calling convention runs the bake synchronously on the calling thread instead. `--background`
    Blender has no window manager at all, so it transparently falls back to that synchronous behavior.
    """
    if not _has_gui_window():
        return {"mode": "SYNCHRONOUS", "result": _run_fluid_operator(obj, operator)}
    window = bpy.context.window_manager.windows[0]
    screen = window.screen
    area = screen.areas[0] if screen.areas else None
    region = next((r for r in area.regions if r.type == "WINDOW"), None) if area is not None else None
    if area is None or region is None:
        return {"mode": "SYNCHRONOUS", "result": _run_fluid_operator(obj, operator)}
    result = _run_fluid_operator(
        obj,
        operator,
        extra_override={"window": window, "screen": screen, "area": area, "region": region},
        call_arg="INVOKE_DEFAULT",
        accept_running_modal=True,
    )
    return {"mode": "RUNNING_MODAL" if "RUNNING_MODAL" in result else "SYNCHRONOUS", "result": result}


def _cache_directory_evidence(path, max_entries=10_000):
    resolved = _resolved_cache_path(path)
    exists = os.path.isdir(resolved)
    entries = 0
    bytes_total = 0
    truncated = False
    if exists:
        for root, _directories, filenames in os.walk(resolved):
            for filename in filenames:
                entries += 1
                if entries > max_entries:
                    truncated = True
                    break
                with contextlib.suppress(OSError):
                    bytes_total += os.path.getsize(os.path.join(root, filename))
            if truncated:
                break
    return {
        "configured": path,
        "resolved": resolved,
        "exists": exists,
        "writable": exists and os.access(resolved, os.W_OK),
        "files_scanned": min(entries, max_entries),
        "bytes_scanned": bytes_total,
        "scan_truncated": truncated,
    }


def _job_id(domain_uuid, stage, resolved_directory):
    digest = hashlib.sha256(resolved_directory.encode("utf-8")).hexdigest()[:12]
    return f"{domain_uuid}:{stage}:{digest}"


def _reconcile_pending_bake_manifest(obj, settings, resolved_directory):
    """
    Write the ownership manifest for a previously async-started bake once it has finished.

    START_BAKE/RESUME may dispatch a real Blender WM job (INVOKE_DEFAULT) that keeps running after
    the call returns, so nothing runs the manifest write at the moment baking actually completes.
    Every later call into this domain's cache management re-checks the stage's baked flag and writes
    the manifest then, since that is the earliest point script code runs again.
    """
    pending = obj.get(_PENDING_BAKE_KEY)
    if not isinstance(pending, dict) or not pending:
        return
    baked_flag = pending.get("baked_flag")
    if not baked_flag or not bool(getattr(settings, baked_flag, False)):
        return
    with contextlib.suppress(OSError):
        _write_manifest_entry(
            resolved_directory,
            pending["domain_uuid"],
            pending["stage_action"],
            pending["cache_type"],
            pending["frame_range"],
        )
    obj[_PENDING_BAKE_KEY] = ""


def _set_cache_range(settings, start, end):
    if start > end:
        raise ValueError("cache_frame_start must be <= cache_frame_end")
    _validate_rna_value(settings, "cache_frame_start", start)
    _validate_rna_value(settings, "cache_frame_end", end)
    if start > settings.cache_frame_end:
        settings.cache_frame_end = end
        settings.cache_frame_start = start
    else:
        settings.cache_frame_start = start
        settings.cache_frame_end = end


def _reject_unused_openvdb_fields(settings, patch):
    """Reject OpenVDB compression/precision when no cache stage will be written as OpenVDB."""
    if not _OPENVDB_FIELDS & set(patch):
        return
    formats = {patch.get(name, getattr(settings, name, None)) for name in _OPENVDB_FORMAT_FIELDS}
    if "OPENVDB" not in formats:
        raise ValueError(
            "openvdb_cache_compress_type/openvdb_data_depth only affect OpenVDB caches; set "
            "cache_data_format, cache_mesh_format, or cache_particle_format to OPENVDB first"
        )


def _set_openvdb_data_depth(settings, value):
    """
    Set the OpenVDB float precision, bypassing the RNA enum whose static items are unreliable.

    Blender 5.2.1 reports ``enum_items == ['NONE']`` for this dynamic enum while only the strings
    ``'32'``, ``'16'`` and ``'8'`` are accepted, so the value is checked against the verified
    identifiers instead of the RNA metadata.
    """
    if value not in _OPENVDB_DATA_DEPTHS:
        raise ValueError(f"openvdb_data_depth must be one of {list(_OPENVDB_DATA_DEPTHS)}, got {value!r}")
    old = str(settings.openvdb_data_depth)
    settings.openvdb_data_depth = value
    return {"old": old, "new": str(settings.openvdb_data_depth)}


def _baked_frame_ceiling(settings):
    """
    Return the highest frame guaranteed to have baked cache data for a MODULAR/ALL domain.

    A pause frame > 0 on the stage that drives the domain's visible output means baking stopped
    there; a fully completed (or never-started) stage leaves its pause field at 0, so the ceiling
    falls back to ``cache_frame_end``.
    """
    pause_fields = [
        "cache_frame_pause_mesh" if settings.use_mesh else None,
        "cache_frame_pause_data",
        "cache_frame_pause_particles",
        "cache_frame_pause_guide",
    ]
    pauses = [getattr(settings, name) for name in pause_fields if name and getattr(settings, name, 0) > 0]
    return min(pauses) if pauses else settings.cache_frame_end


def _frame_sample(obj, frame, domain_bounds, tolerance):
    output = _evaluated_output(obj)
    particle_counts = {system.name: len(system.particles) for system in obj.particle_systems}
    output_bounds = output["bounds"]
    near_faces = []
    if output_bounds:
        faces = (
            ("LEFT", 0, "minimum"),
            ("RIGHT", 0, "maximum"),
            ("BACK", 1, "minimum"),
            ("FRONT", 1, "maximum"),
            ("BOTTOM", 2, "minimum"),
            ("TOP", 2, "maximum"),
        )
        for label, axis, side in faces:
            if abs(output_bounds[side][axis] - domain_bounds[side][axis]) <= tolerance:
                near_faces.append(label)
    return {
        "frame": frame,
        "evaluated_mesh": output,
        "particle_counts": particle_counts,
        "total_particles": sum(particle_counts.values()),
        "empty_output": output["vertices"] == 0 and not any(particle_counts.values()),
        "near_domain_faces": near_faces,
    }


def _evaluated_output(obj):
    evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    mesh = evaluated.to_mesh()
    try:
        count = len(mesh.vertices)
        sampled = (
            list(mesh.vertices)
            if count <= 200_000
            else [mesh.vertices[index] for index in range(0, count, math.ceil(count / 200_000))]
        )
        world = [evaluated.matrix_world @ vertex.co for vertex in sampled]
        finite = all(math.isfinite(float(value)) for point in world for value in point)
        bounds = None
        if world:
            minimum = [min(float(point[axis]) for point in world) for axis in range(3)]
            maximum = [max(float(point[axis]) for point in world) for axis in range(3)]
            bounds = {
                "coordinate_space": "WORLD",
                "minimum": minimum,
                "maximum": maximum,
                "dimensions": [maximum[axis] - minimum[axis] for axis in range(3)],
            }
        return {
            "vertices": count,
            "edges": len(mesh.edges),
            "faces": len(mesh.polygons),
            "sampled_vertices": len(sampled),
            "finite": finite,
            "bounds": bounds,
        }
    finally:
        evaluated.to_mesh_clear()


class LiquidSimulationHandlers:
    """Sample evaluated liquid motion and manage only the requested domain's cache."""

    def sample_liquid_simulation(
        self,
        domain_object_name,
        modifier_name,
        frames,
        timeout_seconds=30.0,
        boundary_tolerance_cells=1.0,
        max_preroll_frames=250,
    ):
        obj, modifier, settings = _get_domain(domain_object_name, modifier_name)
        scene, view_layer = _scene_context_for_object(obj)
        plan = _plan_frame_evaluation(
            frames,
            scene,
            settings,
            baked_frame_ceiling=_baked_frame_ceiling,
            max_preroll_frames=max_preroll_frames,
            operation="Sampling",
        )
        domain_bounds = _world_bounds(obj, evaluated=False)
        cell = max(domain_bounds["dimensions"]) / settings.resolution_max
        tolerance = cell * boundary_tolerance_cells
        samples, timed_out = _evaluate_frames(
            scene,
            view_layer,
            plan,
            timeout_seconds,
            lambda frame: _frame_sample(obj, frame, domain_bounds, tolerance),
        )
        discontinuities = []
        for previous, current in itertools.pairwise(samples):
            before = previous["evaluated_mesh"]["vertices"]
            after = current["evaluated_mesh"]["vertices"]
            ratio = abs(after - before) / max(before, 1)
            if ratio > 0.75:
                discontinuities.append(
                    {"from_frame": previous["frame"], "to_frame": current["frame"], "vertex_count_change_ratio": ratio}
                )
        return {
            "changed_objects": [obj.name] if plan.is_replay else [],
            "domain": obj.name,
            "modifier": modifier.name,
            "cache_type": settings.cache_type,
            "requested_frames": plan.frames,
            "evaluated_frames": [item["frame"] for item in samples],
            "timed_out": timed_out,
            "timeout_seconds": timeout_seconds,
            "preroll_frames": plan.preroll_frames,
            "max_preroll_frames": max_preroll_frames if plan.is_replay else None,
            "domain_bounds": domain_bounds,
            "estimated_cell_size": cell,
            "samples": samples,
            "large_frame_changes": discontinuities,
            "timeline_restored": {"frame": scene.frame_current, "subframe": scene.frame_subframe},
            "cache_effect": (
                "REPLAY sampling steps sequentially from cache_frame_start so every intermediate frame is "
                "evaluated in order; existing modular/final cache files are not changed."
                if plan.is_replay
                else "Frame evaluation reads the existing modular/final bake; no cache files are changed."
            ),
            "claim": "Bounded numerical evidence only; this is not a final bake or visual-quality assessment.",
        }

    def manage_liquid_cache(
        self,
        domain_object_name,
        modifier_name,
        action="STATUS",
        patch=None,
        stage=None,
        confirm_bake=False,
        confirm_free=False,
        confirm_external_path=False,
        confirm_external_overwrite=False,
        max_bake_frames=250,
        max_existing_cache_bytes=10_000_000_000,
        domain_type="LIQUID",
    ):
        domain_type = str(domain_type).upper()
        obj, modifier, settings = _get_domain(domain_object_name, modifier_name, domain_type)
        actions = {
            "STATUS",
            "CONFIGURE",
            "BAKE_DATA",
            "BAKE_GUIDES",
            "BAKE_MESH",
            "BAKE_PARTICLES",
            "BAKE_NOISE",
            "BAKE_ALL",
            "START_BAKE",
            "RESUME",
            "CANCEL",
            "PAUSE",
            "FREE_DATA",
            "FREE_GUIDES",
            "FREE_MESH",
            "FREE_PARTICLES",
            "FREE_NOISE",
            "FREE_ALL",
        }
        if action not in actions:
            raise ValueError(f"Unsupported fluid cache action: {action}")
        require_cache_confirmation(action, confirm_bake=confirm_bake, confirm_free=confirm_free)
        gas_only = {"BAKE_NOISE", "FREE_NOISE"}
        liquid_only = {"BAKE_GUIDES", "FREE_GUIDES", "BAKE_MESH", "FREE_MESH", "BAKE_PARTICLES", "FREE_PARTICLES"}
        if domain_type == "LIQUID" and action in gas_only:
            raise ValueError(f"{action} is only valid for GAS domains")
        if domain_type == "GAS" and action in liquid_only:
            raise ValueError(f"{action} is only valid for LIQUID domains")
        if domain_type == "GAS" and stage in {"GUIDES", "MESH", "PARTICLES"}:
            raise ValueError(f"Stage {stage} is only valid for LIQUID domains")
        if domain_type == "LIQUID" and stage == "NOISE":
            raise ValueError("Stage NOISE is only valid for GAS domains")
        patch = dict(patch or {})
        if action == "CONFIGURE" and not patch:
            raise ValueError("CONFIGURE requires a nonempty cache patch")
        if action != "CONFIGURE" and patch:
            raise ValueError(f"{action} does not accept a cache patch")
        stage_actions = {"START_BAKE", "RESUME", "CANCEL"}
        if action in stage_actions and stage not in _BAKE_STAGES:
            raise ValueError(f"{action} requires stage to be one of {sorted(_BAKE_STAGES)}")
        if action not in stage_actions and stage is not None:
            raise ValueError(f"{action} does not accept a stage")
        if settings.cache_directory:
            _reconcile_pending_bake_manifest(obj, settings, _resolved_cache_path(settings.cache_directory))
        before = _cache_state(settings)
        path_before = _cache_directory_evidence(settings.cache_directory)
        if action == "STATUS":
            domain_uuid = obj.get("blendermcp_liquid_uuid")
            manifest = _read_manifest(path_before["resolved"]) if path_before["exists"] else None
            pending = obj.get(_PENDING_BAKE_KEY)
            return {
                "changed_objects": [],
                "domain": obj.name,
                "modifier": modifier.name,
                "action": action,
                "cache": before,
                "directory": path_before,
                "domain_uuid": domain_uuid,
                "manifest": manifest,
                "directory_is_manifest_owned": bool(
                    manifest and domain_uuid and manifest.get("domain_uuid") == domain_uuid
                ),
                "pending_bake": pending if isinstance(pending, dict) and pending else None,
            }
        if action == "CONFIGURE":
            _reject_cache_flags(settings, _CACHE_FLAGS, "Cannot configure an active or baked cache")
            unknown = set(patch) - _CACHE_CONFIG_FIELDS
            if unknown:
                raise ValueError(f"Unsupported cache properties: {sorted(unknown)}")
            start = patch.get("cache_frame_start", settings.cache_frame_start)
            end = patch.get("cache_frame_end", settings.cache_frame_end)
            if start > end:
                raise ValueError("cache_frame_start must be <= cache_frame_end")
            _reject_unused_openvdb_fields(settings, patch)
            if "cache_directory" in patch:
                if not confirm_external_path:
                    raise ValueError("Changing cache_directory requires confirm_external_path=True")
                resolved = _resolved_cache_path(patch["cache_directory"])
                if not os.path.isdir(resolved) or not os.access(resolved, os.W_OK):
                    raise ValueError(f"Cache directory must already exist and be writable: {resolved}")
                for other in bpy.data.objects:
                    for other_modifier in other.modifiers:
                        if other_modifier == modifier or other_modifier.type != "FLUID":
                            continue
                        other_settings = getattr(other_modifier, "domain_settings", None)
                        if other_settings and _resolved_cache_path(other_settings.cache_directory) == resolved:
                            raise ValueError(f"Cache directory is already used by '{other.name}:{other_modifier.name}'")
            if patch.get("cache_type") == "FINAL":
                patch["cache_type"] = "ALL"
            old_range = (settings.cache_frame_start, settings.cache_frame_end)
            scalar_patch = {
                name: value
                for name, value in patch.items()
                if name not in {"cache_frame_start", "cache_frame_end", "openvdb_data_depth"}
            }
            changes = _patch_rna(settings, scalar_patch, _CACHE_CONFIG_FIELDS)
            try:
                if "openvdb_data_depth" in patch:
                    changes["openvdb_data_depth"] = _set_openvdb_data_depth(settings, patch["openvdb_data_depth"])
                if "cache_frame_start" in patch or "cache_frame_end" in patch:
                    _set_cache_range(settings, start, end)
                    changes["cache_frame_start"] = {"old": old_range[0], "new": settings.cache_frame_start}
                    changes["cache_frame_end"] = {"old": old_range[1], "new": settings.cache_frame_end}
                _update_or_restore(obj, settings, changes)
            except Exception:
                _restore_rna(settings, changes)
                with contextlib.suppress(Exception):
                    _set_cache_range(settings, *old_range)
                raise
            return {
                "changed_objects": [obj.name],
                "domain": obj.name,
                "modifier": modifier.name,
                "action": action,
                "changes": changes,
                "cache_before": before,
                "cache_after": _cache_state(settings),
                "directory": _cache_directory_evidence(settings.cache_directory),
                "warnings": ["Cache configuration changed; any in-memory replay state is stale."],
            }
        frame_count = settings.cache_frame_end - settings.cache_frame_start + 1
        bake_actions = {"BAKE_DATA", "BAKE_GUIDES", "BAKE_MESH", "BAKE_PARTICLES", "BAKE_NOISE", "BAKE_ALL"}
        free_actions = {"FREE_DATA", "FREE_GUIDES", "FREE_MESH", "FREE_PARTICLES", "FREE_NOISE", "FREE_ALL"}
        requested_action = action
        if action == "CANCEL":
            if bool(getattr(settings, _BAKE_STAGES[stage]["baking_flag"], False)):
                raise ValueError(
                    f"Cannot cancel an in-progress {stage} bake from a script; Blender exposes no scripted "
                    "abort for a running fluid bake job (press Esc in the Blender window running the bake, "
                    "or wait for it to finish or reach a pause point). CANCEL degrades to freeing that "
                    "stage's cache once it is no longer actively baking."
                )
            action = f"FREE_{stage}"
        if action == "RESUME":
            details = _BAKE_STAGES[stage]
            if not (settings.cache_type == "MODULAR" and bool(settings.cache_resumable)):
                raise ValueError(
                    "RESUME requires cache_type=MODULAR with cache_resumable=True; Bake All cannot be paused or resumed"
                )
            if bool(getattr(settings, details["baking_flag"], False)):
                raise ValueError(f"{stage} is already baking; nothing to resume")
            if bool(getattr(settings, details["baked_flag"], False)):
                raise ValueError(f"{stage} is already fully baked; nothing to resume")
            pause_flag = details["pause_flag"]
            if not pause_flag or not getattr(settings, pause_flag, 0) > 0:
                raise ValueError(f"{stage} has no paused bake to resume (cache_frame_pause is 0)")
        resolved_bake_action = (
            f"BAKE_{stage}" if action in {"START_BAKE", "RESUME"} else (action if action in bake_actions else None)
        )
        directory = None
        domain_uuid = None
        if resolved_bake_action:
            gate_action = resolved_bake_action
            if not confirm_bake:
                raise ValueError(f"{requested_action} requires confirm_bake=True")
            if frame_count > max_bake_frames:
                raise ValueError(f"Cache range has {frame_count} frames, exceeding max_bake_frames={max_bake_frames}")
            if settings.cache_type == "REPLAY":
                raise ValueError("Explicit baking is unavailable in REPLAY mode; configure MODULAR or ALL first")
            if gate_action == "BAKE_ALL" and settings.cache_type != "ALL":
                raise ValueError("BAKE_ALL requires cache_type ALL")
            if gate_action != "BAKE_ALL" and settings.cache_type != "MODULAR":
                raise ValueError(f"{gate_action} requires cache_type MODULAR")
            if gate_action in {"BAKE_MESH", "BAKE_PARTICLES"} and not settings.has_cache_baked_data:
                raise ValueError(f"{gate_action} requires the DATA stage to be baked first")
            if gate_action == "BAKE_MESH" and not settings.use_mesh:
                raise ValueError("BAKE_MESH requires use_mesh=True")
            if gate_action == "BAKE_PARTICLES" and not any(getattr(settings, name) for name in _SECONDARY_TOGGLES):
                raise ValueError("BAKE_PARTICLES requires at least one enabled secondary particle type")
            directory = _cache_directory_evidence(settings.cache_directory)
            if not directory["exists"] or not directory["writable"]:
                raise ValueError(f"Configured cache directory must exist and be writable: {directory['resolved']}")
            if directory["scan_truncated"] or directory["bytes_scanned"] > max_existing_cache_bytes:
                raise ValueError("Existing cache directory exceeds the configured inspection bound")
            domain_uuid = _ensure_liquid_uuid(obj)
            existing_manifest = _read_manifest(directory["resolved"])
            pending_marker = obj.get(_PENDING_BAKE_KEY)
            directory_is_manifest_owned = bool(
                existing_manifest and existing_manifest.get("domain_uuid") == domain_uuid
            ) or bool(isinstance(pending_marker, dict) and pending_marker.get("domain_uuid") == domain_uuid)
            if directory["files_scanned"] and not confirm_external_overwrite and not directory_is_manifest_owned:
                raise ValueError("Cache directory is not empty; confirm_external_overwrite=True is required")
        if action in free_actions:
            if not confirm_free:
                raise ValueError(f"{requested_action} requires confirm_free=True")
            if path_before["files_scanned"] and not confirm_external_overwrite:
                raise ValueError("Freeing cache data may remove files; confirm_external_overwrite=True is required")
        if requested_action in {"START_BAKE", "RESUME"}:
            operator = getattr(bpy.ops.fluid, _BAKE_STAGES[stage]["operator"])
        elif action == "PAUSE":
            operator = bpy.ops.fluid.pause_bake
        else:
            operator_name = {
                "BAKE_DATA": "bake_data",
                "BAKE_GUIDES": "bake_guides",
                "BAKE_MESH": "bake_mesh",
                "BAKE_PARTICLES": "bake_particles",
                "BAKE_NOISE": "bake_noise",
                "BAKE_ALL": "bake_all",
                "FREE_DATA": "free_data",
                "FREE_GUIDES": "free_guides",
                "FREE_MESH": "free_mesh",
                "FREE_PARTICLES": "free_particles",
                "FREE_NOISE": "free_noise",
                "FREE_ALL": "free_all",
            }[action]
            operator = getattr(bpy.ops.fluid, operator_name)
        if action == "PAUSE":
            if not settings.is_cache_baking_any:
                raise ValueError("No liquid cache stage is currently baking")
            if not (settings.cache_type == "MODULAR" and bool(settings.cache_resumable)):
                raise ValueError(
                    "PAUSE requires cache_type=MODULAR with cache_resumable=True; Bake All cannot be paused or resumed"
                )
        expected_before = {
            "FREE_DATA": "has_cache_baked_data",
            "FREE_GUIDES": "has_cache_baked_guide",
            "FREE_MESH": "has_cache_baked_mesh",
            "FREE_PARTICLES": "has_cache_baked_particles",
            "FREE_NOISE": "has_cache_baked_noise",
        }.get(action)
        if expected_before and not getattr(settings, expected_before):
            raise ValueError(f"{action} has no baked stage to free")
        job = None
        if requested_action in {"START_BAKE", "RESUME"}:
            job = _start_fluid_bake_job(obj, operator)
        else:
            _run_fluid_operator(obj, operator)
        after = _cache_state(settings)
        expected_after = {
            "BAKE_DATA": ("has_cache_baked_data", True),
            "BAKE_GUIDES": ("has_cache_baked_guide", True),
            "BAKE_MESH": ("has_cache_baked_mesh", True),
            "BAKE_PARTICLES": ("has_cache_baked_particles", True),
            "BAKE_NOISE": ("has_cache_baked_noise", True),
            "BAKE_ALL": ("has_cache_baked_any", True),
            "FREE_DATA": ("has_cache_baked_data", False),
            "FREE_GUIDES": ("has_cache_baked_guide", False),
            "FREE_MESH": ("has_cache_baked_mesh", False),
            "FREE_PARTICLES": ("has_cache_baked_particles", False),
            "FREE_NOISE": ("has_cache_baked_noise", False),
            "FREE_ALL": ("has_cache_baked_any", False),
        }.get(resolved_bake_action if requested_action in {"START_BAKE", "RESUME"} else action)
        job_still_running = bool(job) and job["mode"] == "RUNNING_MODAL"
        if expected_after and not job_still_running and bool(getattr(settings, expected_after[0])) != expected_after[1]:
            raise RuntimeError(
                f"{action} reported FINISHED but {expected_after[0]} is not {expected_after[1]}; "
                f"state={json.dumps(after)}"
            )
        warnings = [
            "Fluid bake operators are Blender jobs; frame count is bounded but a single frame cannot be "
            "timed out by MCP.",
            "Free actions delete derived cache data and cannot be rolled back through Blender datablocks.",
        ]
        job_id = None
        if resolved_bake_action:
            assert directory is not None
            job_id = _job_id(domain_uuid, stage or resolved_bake_action, directory["resolved"])
            if job_still_running:
                assert expected_after is not None
                obj[_PENDING_BAKE_KEY] = {
                    "domain_uuid": domain_uuid,
                    "stage_action": resolved_bake_action,
                    "baked_flag": expected_after[0],
                    "cache_type": settings.cache_type,
                    "frame_range": [settings.cache_frame_start, settings.cache_frame_end],
                }
                warnings.append(
                    "Bake dispatched as a non-blocking Blender job (RUNNING_MODAL); poll STATUS until the "
                    "stage's has_cache_baked_* flag is true before sampling or chaining another bake "
                    "action. The ownership manifest is written the next time this cache is queried after "
                    "the job finishes."
                )
            else:
                if job is not None and not _has_gui_window():
                    warnings.append(
                        "Blender is running with no GUI window (--background), so this bake ran "
                        "synchronously and blocked the calling thread; non-blocking/pollable bakes "
                        "require a running Blender window."
                    )
                try:
                    _write_manifest_entry(
                        directory["resolved"],
                        domain_uuid,
                        resolved_bake_action,
                        settings.cache_type,
                        [settings.cache_frame_start, settings.cache_frame_end],
                    )
                except OSError as error:
                    warnings.append(f"Cache bake succeeded but the ownership manifest could not be written: {error}")
        return {
            "changed_objects": [obj.name],
            "domain": obj.name,
            "modifier": modifier.name,
            "action": requested_action,
            "stage": stage,
            "job_id": job_id,
            "job_mode": job["mode"] if job else None,
            "frame_count": frame_count,
            "operator_scope": f"EXACT_{domain_type}_DOMAIN",
            "cache_before": before,
            "cache_after": after,
            "directory_before": path_before,
            "directory_after": _cache_directory_evidence(settings.cache_directory),
            "warnings": warnings,
        }

    def manage_fluid_cache(self, domain_type, **kwargs):
        """Canonical LIQUID/GAS wrapper over the shared Mantaflow cache lifecycle."""
        return self.manage_liquid_cache(domain_type=domain_type, **kwargs)
