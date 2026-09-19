# pyright: reportGeneralTypeIssues=false
"""Bounded rigid-body evaluation and exact world-cache lifecycle handling."""

import contextlib
import json
import math
import os
import time

from itertools import combinations

import bpy
import mathutils

from ...helpers import preserve_mode_and_selection
from .inspection_and_setup import (
    _aabb_overlap,
    _bounds,
    _cache_info,
    _ensure_world,
    _require_finished,
    _scene,
    _set_cache_range,
    _validate_object_batch,
    _validate_rna_properties,
    _view_layer_for,
)

_CACHE_FIELDS = {
    "frame_start",
    "frame_end",
    "frame_step",
    "name",
    "index",
    "use_disk_cache",
    "use_external",
    "use_library_path",
    "filepath",
}


def _frame_list(selection):
    if "frames" in selection:
        frames = list(selection["frames"])
    else:
        start = selection.get("frame_start")
        end = selection.get("frame_end")
        step = selection.get("frame_step", 1)
        if start is None or end is None or start > end:
            raise ValueError("A valid frame_start/frame_end range is required")
        frames = list(range(start, end + 1, step))
    if not frames or len(frames) > 100 or frames != sorted(set(frames)):
        raise ValueError("Frame selection must produce 1-100 unique ordered frames")
    return frames


def _evaluated_sample(obj, depsgraph):
    evaluated = obj.evaluated_get(depsgraph)
    location, rotation, scale = evaluated.matrix_world.decompose()
    bounds = (
        _bounds([evaluated.matrix_world @ mathutils.Vector(corner) for corner in evaluated.bound_box])
        if evaluated.bound_box
        else None
    )
    return {
        "object": obj.name,
        "matrix_world": [list(row) for row in evaluated.matrix_world],
        "location_world": list(location),
        "rotation_quaternion_wxyz": list(rotation),
        "scale_world": list(scale),
        "bounds_world": bounds,
    }


def _external_cache_evidence(path, max_entries=10_000):
    resolved = bpy.path.abspath(path) if path else ""
    exists = bool(resolved and os.path.isdir(resolved))
    entries = 0
    bytes_total = 0
    truncated = False
    if exists:
        for root, _directories, files in os.walk(resolved):
            for filename in files:
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


def _run_cache_operator(scene, cache, operator, **kwargs):
    view_layer = _view_layer_for(scene)
    with (
        bpy.context.temp_override(scene=scene, view_layer=view_layer, point_cache=cache),
        preserve_mode_and_selection(),
    ):
        active = view_layer.objects.active
        if active is not None and active.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        result = operator(**kwargs)
    _require_finished(result, operator.idname())


class RigidBodySimulationHandlers:
    """Sample rigid-body motion and manage only the requested world's point cache."""

    def sample_rigid_body_simulation(
        self,
        scene_name,
        object_names,
        frame_selection,
        include_velocity=True,
        stationary_speed=0.001,
        escape_bounds_min=None,
        escape_bounds_max=None,
        timeout_seconds=30.0,
    ):
        scene = _scene(scene_name)
        objects = _validate_object_batch(scene, object_names, require_body=True)
        frames = _frame_list(frame_selection)
        if not math.isfinite(stationary_speed) or stationary_speed < 0:
            raise ValueError("stationary_speed must be finite and nonnegative")
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 300:
            raise ValueError("timeout_seconds must be in (0, 300]")
        if (escape_bounds_min is None) != (escape_bounds_max is None):
            raise ValueError("escape_bounds_min and escape_bounds_max must be supplied together")
        bounded_escape = escape_bounds_min is not None and escape_bounds_max is not None
        if escape_bounds_min is not None:
            if any(a > b for a, b in zip(escape_bounds_min, escape_bounds_max, strict=True)):
                raise ValueError("escape bounds minimum must not exceed maximum")
        world = _ensure_world(scene)
        original_frame = scene.frame_current
        original_subframe = scene.frame_subframe
        cache_before = _cache_info(world.point_cache)
        fps = float(scene.render.fps) / max(float(scene.render.fps_base), 1e-9)
        previous = {}
        samples = []
        deadline = time.monotonic() + timeout_seconds
        timed_out = False
        try:
            for frame in frames:
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                scene.frame_set(frame)
                view_layer = _view_layer_for(scene)
                view_layer.update()
                depsgraph = view_layer.depsgraph
                records = [_evaluated_sample(obj, depsgraph) for obj in objects]
                for record in records:
                    prior = previous.get(record["object"])
                    velocity = None
                    if include_velocity and prior is not None:
                        delta_seconds = (frame - prior["frame"]) / fps
                        location_delta = [
                            (record["location_world"][axis] - prior["location"][axis]) / delta_seconds
                            for axis in range(3)
                        ]
                        speed = math.sqrt(sum(value * value for value in location_delta))
                        current_q = (
                            bpy.data.objects[record["object"]].evaluated_get(depsgraph).matrix_world.to_quaternion()
                        )
                        delta_q = prior["rotation"].rotation_difference(current_q)
                        angular_speed = abs(float(delta_q.angle)) / delta_seconds
                        velocity = {
                            "linear_world_units_per_second": location_delta,
                            "linear_speed": speed,
                            "angular_speed_radians_per_second": angular_speed,
                            "stationary": speed <= stationary_speed and angular_speed <= stationary_speed,
                        }
                    record["velocity_estimate"] = velocity
                    center = record["bounds_world"]["center"] if record["bounds_world"] else record["location_world"]
                    record["escaped_bounds"] = bool(
                        bounded_escape
                        and any(
                            center[axis] < escape_bounds_min[axis] or center[axis] > escape_bounds_max[axis]  # pyright: ignore[reportOptionalSubscript]
                            for axis in range(3)
                        )
                    )
                    obj = bpy.data.objects[record["object"]]
                    previous[record["object"]] = {
                        "frame": frame,
                        "location": record["location_world"],
                        "rotation": obj.evaluated_get(depsgraph).matrix_world.to_quaternion(),
                    }
                overlaps = []
                for first, second in list(combinations(records, 2))[:256]:
                    if (
                        first["bounds_world"]
                        and second["bounds_world"]
                        and _aabb_overlap(first["bounds_world"], second["bounds_world"])
                    ):
                        overlaps.append([first["object"], second["object"]])
                constraints = []
                for constraint_obj in scene.objects:
                    constraint = constraint_obj.rigid_body_constraint
                    if constraint and constraint.object1 in objects and constraint.object2 in objects:
                        first_location = constraint.object1.evaluated_get(depsgraph).matrix_world.translation
                        second_location = constraint.object2.evaluated_get(depsgraph).matrix_world.translation
                        constraints.append(
                            {
                                "constraint": constraint_obj.name,
                                "endpoint_distance_world": float((first_location - second_location).length),
                            }
                        )
                samples.append(
                    {
                        "frame": frame,
                        "objects": records,
                        "aabb_overlap_candidates": overlaps,
                        "constraint_distances": constraints,
                    }
                )
        finally:
            scene.frame_set(original_frame, subframe=original_subframe)
            _view_layer_for(scene).update()
        return {
            "changed_objects": [obj.name for obj in objects],
            "scene": scene.name,
            "requested_frames": frames,
            "evaluated_frames": [sample["frame"] for sample in samples],
            "timed_out": timed_out,
            "samples": samples,
            "timeline_restored": {"frame": scene.frame_current, "subframe": scene.frame_subframe},
            "point_cache_before": cache_before,
            "point_cache_after": _cache_info(world.point_cache),
            "cache_effect": "Sequential evaluation may populate Blender's temporary in-memory rigid-body cache.",
            "claim": "Velocities and overlap candidates are derived diagnostics, not Bullet contact or impulse data.",
        }

    def manage_rigid_body_cache(
        self,
        scene_name,
        action="INSPECT",
        settings=None,
        calculate_frame=None,
        confirm_bake=False,
        confirm_free=False,
        confirm_external_overwrite=False,
        max_frame_steps=250,
    ):
        scene = _scene(scene_name)
        world = scene.rigidbody_world
        if world is None:
            raise ValueError(f"Scene '{scene.name}' has no rigid-body world")
        cache = world.point_cache
        actions = {"INSPECT", "CONFIGURE", "CALCULATE_TO_FRAME", "BAKE", "BAKE_FROM_CACHE", "FREE"}
        if action not in actions:
            raise ValueError(f"Unsupported cache action: {action}")
        patch = dict(settings or {})
        if action == "CONFIGURE" and not patch:
            raise ValueError("CONFIGURE requires settings")
        if action != "CONFIGURE" and patch:
            raise ValueError(f"{action} does not accept settings")
        if cache.is_baking:
            raise ValueError("Rigid-body point cache is currently baking")
        before = _cache_info(cache)
        external_before = _external_cache_evidence(cache.filepath)
        if action == "INSPECT":
            return {
                "changed_objects": [],
                "scene": scene.name,
                "action": action,
                "point_cache": before,
                "external_path": external_before,
            }
        if not 1 <= max_frame_steps <= 10_000:
            raise ValueError("max_frame_steps must be in [1, 10000]")
        if action == "CONFIGURE":
            if cache.is_baked:
                raise ValueError("Cannot configure a baked cache; free it explicitly first")
            unknown = set(patch) - _CACHE_FIELDS
            if unknown:
                raise ValueError(f"Unsupported PointCache settings: {sorted(unknown)}")
            prepared = _validate_rna_properties(cache.bl_rna.properties, patch, _CACHE_FIELDS)
            start = int(prepared.pop("frame_start", cache.frame_start))
            end = int(prepared.pop("frame_end", cache.frame_end))
            step = int(prepared.pop("frame_step", cache.frame_step))
            use_external = prepared.get("use_external", cache.use_external)
            filepath = prepared.get("filepath", cache.filepath)
            if use_external:
                evidence = _external_cache_evidence(filepath)
                if not filepath or not evidence["exists"] or not evidence["writable"]:
                    raise ValueError("External cache filepath must identify an existing writable directory")
            if prepared.get("use_disk_cache", cache.use_disk_cache) and not use_external and not bpy.data.filepath:
                raise ValueError("Internal disk caching requires a saved .blend file")
            old = {name: getattr(cache, name) for name in _CACHE_FIELDS}
            try:
                for name, value in prepared.items():
                    setattr(cache, name, value)
                _set_cache_range(cache, {"frame_start": start, "frame_end": end, "frame_step": step})
            except Exception:
                for name, value in old.items():
                    with contextlib.suppress(Exception):
                        setattr(cache, name, value)
                raise
            return {
                "changed_objects": [],
                "changed_resources": [scene.name],
                "scene": scene.name,
                "action": action,
                "point_cache_before": before,
                "point_cache_after": _cache_info(cache),
                "external_path": _external_cache_evidence(cache.filepath),
                "warnings": ["Point-cache configuration changed; previously evaluated in-memory state is stale."],
            }
        frame_count = (cache.frame_end - cache.frame_start) // cache.frame_step + 1
        if action in {"BAKE", "BAKE_FROM_CACHE"}:
            if not confirm_bake:
                raise ValueError(f"{action} requires confirm_bake=True")
            if cache.is_baked:
                raise ValueError("Rigid-body point cache is already baked")
            if frame_count > max_frame_steps:
                raise ValueError(f"Cache has {frame_count} steps, exceeding max_frame_steps={max_frame_steps}")
            if cache.use_external and external_before["files_scanned"] and not confirm_external_overwrite:
                raise ValueError("External cache directory is not empty; confirm_external_overwrite=True is required")
            operator = bpy.ops.ptcache.bake if action == "BAKE" else bpy.ops.ptcache.bake_from_cache
            _run_cache_operator(scene, cache, operator, **({"bake": True} if action == "BAKE" else {}))
            if not cache.is_baked:
                raise RuntimeError(
                    f"{action} reported FINISHED but cache is not baked: {json.dumps(_cache_info(cache))}"
                )
        elif action == "FREE":
            if not confirm_free:
                raise ValueError("FREE requires confirm_free=True")
            if not cache.is_baked:
                raise ValueError("Rigid-body point cache is not baked")
            if cache.use_external and external_before["files_scanned"] and not confirm_external_overwrite:
                raise ValueError("Freeing external files requires confirm_external_overwrite=True")
            _run_cache_operator(scene, cache, bpy.ops.ptcache.free_bake)
            if cache.is_baked:
                raise RuntimeError("FREE reported FINISHED but the rigid-body cache remains baked")
        else:
            if calculate_frame is None:
                raise ValueError("CALCULATE_TO_FRAME requires calculate_frame")
            if cache.is_baked:
                raise ValueError("Cannot calculate into an already baked cache")
            if calculate_frame < cache.frame_start or calculate_frame > cache.frame_end:
                raise ValueError("calculate_frame must be inside the point-cache range")
            frames = list(range(cache.frame_start, calculate_frame + 1, cache.frame_step))
            if len(frames) > max_frame_steps:
                raise ValueError(f"Evaluation has {len(frames)} steps, exceeding max_frame_steps={max_frame_steps}")
            original_frame = scene.frame_current
            original_subframe = scene.frame_subframe
            try:
                for frame in frames:
                    scene.frame_set(frame)
                    _view_layer_for(scene).update()
            finally:
                scene.frame_set(original_frame, subframe=original_subframe)
                _view_layer_for(scene).update()
            frame_count = len(frames)
        return {
            "changed_objects": [obj.name for obj in scene.objects if obj.rigid_body is not None],
            "changed_resources": [scene.name],
            "scene": scene.name,
            "action": action,
            "frame_steps": frame_count,
            "operator_scope": "EXACT_RIGID_BODY_WORLD_POINT_CACHE",
            "point_cache_before": before,
            "point_cache_after": _cache_info(cache),
            "external_path": _external_cache_evidence(cache.filepath),
            "warnings": ["Cache operations are synchronous and cannot be interrupted inside a Bullet solve step."],
        }
