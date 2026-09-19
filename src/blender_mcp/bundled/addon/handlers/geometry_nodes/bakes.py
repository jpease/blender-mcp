# ruff: file-ignore[too-many-boolean-expressions, too-many-branches, too-many-locals, too-many-statements, too-many-statements-in-try-clause, undocumented-public-method]
"""Geometry Nodes bake and simulation-cache lifecycle handlers."""

import contextlib
import os
import time

from typing import Any

import bpy

from ...helpers import preserve_mode_and_selection, set_active
from ._shared import require_nodes_modifier, require_object


def _directory_evidence(directory: str | None, byte_limit: int | None, max_files: int = 10_000) -> dict[str, Any]:
    """Inspect an explicit cache directory without unbounded traversal."""
    if not directory:
        return {
            "configured": directory,
            "resolved": None,
            "exists": False,
            "files_scanned": 0,
            "bytes_scanned": 0,
            "scan_truncated": False,
            "byte_limit_exceeded": False,
        }
    resolved = os.path.realpath(bpy.path.abspath(directory))
    exists = os.path.isdir(resolved)
    files = 0
    bytes_scanned = 0
    truncated = False
    if exists:
        for root, _directories, filenames in os.walk(resolved):
            for filename in filenames:
                files += 1
                if files > max_files:
                    truncated = True
                    break
                with contextlib.suppress(OSError):
                    bytes_scanned += os.path.getsize(os.path.join(root, filename))
                if byte_limit is not None and bytes_scanned > byte_limit:
                    truncated = True
                    break
            if truncated:
                break
    return {
        "configured": directory,
        "resolved": resolved,
        "exists": exists,
        "writable": exists and os.access(resolved, os.W_OK),
        "files_scanned": min(files, max_files),
        "bytes_scanned": bytes_scanned,
        "scan_truncated": truncated,
        "byte_limit_exceeded": byte_limit is not None and bytes_scanned > byte_limit,
    }


def _bake_record(bake, modifier, byte_limit: int | None = None) -> dict[str, Any]:
    """Serialize public RNA for one stable Geometry Nodes bake entry."""
    target = bake.bake_target if bake.bake_target != "INHERIT" else modifier.bake_target
    directory = bake.directory if bake.use_custom_path else modifier.bake_directory
    disk = _directory_evidence(directory, byte_limit) if target == "DISK" else None
    if target == "DISK":
        cache_evidence = "DISK_DATA_PRESENT" if disk and disk["files_scanned"] else "NO_DISK_DATA_FOUND"
    else:
        cache_evidence = "PACKED_STATE_NOT_EXPOSED_BY_PUBLIC_RNA"
    node = bake.node
    return {
        "bake_id": bake.bake_id,
        "node": node.name if node else None,
        "node_type": node.bl_idname if node else None,
        "bake_mode": bake.bake_mode,
        "frame_start": bake.frame_start,
        "frame_end": bake.frame_end,
        "uses_custom_frame_range": bake.use_custom_simulation_frame_range,
        "bake_target": bake.bake_target,
        "effective_target": target,
        "directory": bake.directory,
        "uses_custom_path": bake.use_custom_path,
        "disk_evidence": disk,
        "cache_evidence": cache_evidence,
        "referenced_data_blocks": [
            {
                "id_type": item.id.bl_rna.identifier if item.id else None,
                "name": item.id.name if item.id else None,
                "library_name": item.lib_name,
            }
            for item in bake.data_blocks
        ],
    }


def _find_bake(modifier, bake_id: int):
    """Resolve one bake entry by the stable ID used by Blender operators."""
    bake = next((item for item in modifier.bakes if item.bake_id == bake_id), None)
    if bake is None:
        available = [item.bake_id for item in modifier.bakes]
        raise ValueError(f"Bake ID {bake_id} not found on '{modifier.name}'; available IDs: {available}")
    return bake


def _scene_context(obj):
    """Resolve a scene and view layer that actually contain the target object."""
    scenes = [scene for scene in bpy.data.scenes if obj.name in scene.objects]
    if not scenes:
        raise ValueError(f"Object '{obj.name}' is not linked to a scene")
    scene = bpy.context.scene if bpy.context.scene in scenes else scenes[0]
    view_layer = next((layer for layer in scene.view_layers if obj.name in layer.objects), None)
    if view_layer is None:
        raise ValueError(f"Object '{obj.name}' is excluded from every view layer in scene '{scene.name}'")
    return scene, view_layer


def _run_bake_operator(obj, modifier, bake_id: int, operator, **operator_kwargs):
    """Run a single-entry bake operator under a deterministic Object Mode context."""
    scene, view_layer = _scene_context(obj)
    previous_frame = scene.frame_current
    previous_subframe = scene.frame_subframe
    try:
        with preserve_mode_and_selection():
            set_active(obj)
            if obj.mode != "OBJECT":
                mode_result = bpy.ops.object.mode_set(mode="OBJECT")
                if "FINISHED" not in mode_result:
                    raise RuntimeError(f"Could not enter Object Mode: {sorted(mode_result)}")
            with bpy.context.temp_override(
                scene=scene,
                view_layer=view_layer,
                object=obj,
                active_object=obj,
                selected_objects=[obj],
                selected_editable_objects=[obj],
            ):
                result = operator(
                    session_uid=obj.session_uid,
                    modifier_name=modifier.name,
                    bake_id=bake_id,
                    **operator_kwargs,
                )
    finally:
        if scene.frame_current != previous_frame or scene.frame_subframe != previous_subframe:
            scene.frame_set(previous_frame, subframe=previous_subframe)
            view_layer.update()
    if "RUNNING_MODAL" in result:
        raise RuntimeError("Geometry Nodes bake unexpectedly entered modal execution")
    if "FINISHED" not in result:
        raise RuntimeError(f"Geometry Nodes bake operator returned {sorted(result)}")
    return sorted(result)


def _validate_directory(directory: str, *, require_writable: bool = True) -> str:
    """Normalize and validate a user-provided external cache directory."""
    resolved = os.path.realpath(bpy.path.abspath(directory))
    if not os.path.isdir(resolved):
        raise ValueError(f"Cache directory must already exist: {resolved}")
    if require_writable and not os.access(resolved, os.W_OK):
        raise ValueError(f"Cache directory is not writable: {resolved}")
    return resolved


class GeometryNodesBakeHandlersMixin:
    """Inspect and explicitly operate on individual Geometry Nodes cache entries."""

    def manage_geometry_nodes_bake(
        self,
        object_name,
        modifier_name,
        action="INSPECT",
        bake_id=None,
        frame_start=None,
        frame_end=None,
        bake_target=None,
        directory=None,
        max_frames=None,
        max_bytes=None,
        time_limit_seconds=None,
        unpack_method="USE_ORIGINAL",
        confirm_bake=False,
        confirm_overwrite=False,
        confirm_delete=False,
    ):
        obj = require_object(object_name)
        modifier = require_nodes_modifier(obj, modifier_name)
        action = action.upper()
        if action not in {"INSPECT", "BAKE", "PACK", "UNPACK", "DELETE"}:
            raise ValueError(f"Unsupported Geometry Nodes bake action: {action}")
        if max_frames is not None and int(max_frames) < 1:
            raise ValueError("max_frames must be positive")
        if max_bytes is not None and int(max_bytes) < 1:
            raise ValueError("max_bytes must be positive")
        if time_limit_seconds is not None and float(time_limit_seconds) <= 0:
            raise ValueError("time_limit_seconds must be positive")
        if unpack_method not in {"USE_LOCAL", "WRITE_LOCAL", "USE_ORIGINAL", "WRITE_ORIGINAL"}:
            raise ValueError(f"Unsupported unpack_method: {unpack_method}")
        if action == "INSPECT":
            entries = list(modifier.bakes)
            if bake_id is not None:
                entries = [_find_bake(modifier, int(bake_id))]
            return {
                "object": obj.name,
                "modifier": modifier.name,
                "node_group": modifier.node_group.name if modifier.node_group else None,
                "modifier_defaults": {
                    "bake_target": modifier.bake_target,
                    "bake_directory": modifier.bake_directory,
                },
                "bakes": [_bake_record(entry, modifier, max_bytes) for entry in entries],
                "public_api_limitations": [
                    "Blender 5.1 public RNA does not expose a definitive packed-cache is_baked flag; "
                    "packed state is reported as unverified."
                ],
            }
        if bake_id is None:
            raise ValueError(f"{action} requires bake_id")
        bake = _find_bake(modifier, int(bake_id))
        before = _bake_record(bake, modifier, max_bytes)
        warnings = []
        operator_result = None
        elapsed = 0.0

        if action == "BAKE":
            if not confirm_bake:
                raise ValueError("confirm_bake=True is required for BAKE")
            if (
                frame_start is None
                or frame_end is None
                or bake_target is None
                or max_frames is None
                or max_bytes is None
                or time_limit_seconds is None
            ):
                raise ValueError("BAKE requires frame range, target, frame/byte limits, and a time limit")
            frame_start = int(frame_start)
            frame_end = int(frame_end)
            if frame_start > frame_end:
                raise ValueError("frame_start must not exceed frame_end")
            frame_count = frame_end - frame_start + 1
            if frame_count > int(max_frames):
                raise ValueError(f"Requested {frame_count} frames exceeds max_frames={max_frames}")
            if bake_target not in {"PACKED", "DISK"}:
                raise ValueError("bake_target must be PACKED or DISK")
            resolved_directory = None
            if bake_target == "DISK":
                if not directory:
                    raise ValueError("DISK baking requires directory")
                resolved_directory = _validate_directory(directory)
                evidence = _directory_evidence(resolved_directory, int(max_bytes))
                if evidence["files_scanned"] and not confirm_overwrite:
                    raise ValueError("The cache directory is not empty; set confirm_overwrite=True to continue")
                if evidence["byte_limit_exceeded"]:
                    raise ValueError("Existing directory contents already exceed max_bytes")
            previous = {
                "modifier_target": modifier.bake_target,
                "modifier_directory": modifier.bake_directory,
                "target": bake.bake_target,
                "directory": bake.directory,
                "custom_path": bake.use_custom_path,
                "mode": bake.bake_mode,
                "custom_range": bake.use_custom_simulation_frame_range,
                "start": bake.frame_start,
                "end": bake.frame_end,
            }
            try:
                modifier.bake_target = bake_target
                bake.bake_target = bake_target
                bake.bake_mode = "STILL" if frame_start == frame_end else "ANIMATION"
                bake.use_custom_simulation_frame_range = True
                bake.frame_start = frame_start
                bake.frame_end = frame_end
                if resolved_directory is not None:
                    modifier.bake_directory = resolved_directory
                    bake.use_custom_path = True
                    bake.directory = resolved_directory
                started = time.monotonic()
                operator_result = _run_bake_operator(
                    obj, modifier, bake.bake_id, bpy.ops.object.geometry_node_bake_single
                )
                elapsed = time.monotonic() - started
            except Exception:
                modifier.bake_target = previous["modifier_target"]
                modifier.bake_directory = previous["modifier_directory"]
                bake.bake_target = previous["target"]
                bake.directory = previous["directory"]
                bake.use_custom_path = previous["custom_path"]
                bake.bake_mode = previous["mode"]
                bake.use_custom_simulation_frame_range = previous["custom_range"]
                bake.frame_start = previous["start"]
                bake.frame_end = previous["end"]
                raise
            if elapsed > float(time_limit_seconds):
                warnings.append(
                    f"Bake took {elapsed:.3f}s, exceeding time_limit_seconds={time_limit_seconds}; "
                    "Blender's synchronous single-bake operator cannot be preempted safely through public RNA."
                )
            if bake_target == "PACKED":
                warnings.append(
                    "Blender public RNA does not expose packed-cache byte size; max_bytes could not be verified "
                    "for this packed bake."
                )
        elif action == "PACK":
            operator_result = _run_bake_operator(
                obj, modifier, bake.bake_id, bpy.ops.object.geometry_node_bake_pack_single
            )
        elif action == "UNPACK":
            if not directory:
                raise ValueError("UNPACK requires directory")
            resolved_directory = _validate_directory(directory)
            evidence = _directory_evidence(resolved_directory, max_bytes)
            if evidence["files_scanned"] and not confirm_overwrite:
                raise ValueError("The unpack directory is not empty; set confirm_overwrite=True to continue")
            previous_path = {"custom": bake.use_custom_path, "directory": bake.directory}
            try:
                bake.use_custom_path = True
                bake.directory = resolved_directory
                operator_result = _run_bake_operator(
                    obj,
                    modifier,
                    bake.bake_id,
                    bpy.ops.object.geometry_node_bake_unpack_single,
                    method=unpack_method,
                )
            except Exception:
                bake.use_custom_path = previous_path["custom"]
                bake.directory = previous_path["directory"]
                raise
        elif action == "DELETE":
            if not confirm_delete:
                raise ValueError("confirm_delete=True is required for DELETE")
            operator_result = _run_bake_operator(
                obj, modifier, bake.bake_id, bpy.ops.object.geometry_node_bake_delete_single
            )
        after = _bake_record(bake, modifier, max_bytes)
        disk_evidence = after.get("disk_evidence") or {}
        if max_bytes is not None and disk_evidence.get("byte_limit_exceeded"):
            warnings.append(
                "The resulting disk cache exceeds max_bytes. It was retained because deleting a completed cache "
                "requires a separate confirmed DELETE request."
            )
        return {
            "object": obj.name,
            "modifier": modifier.name,
            "action": action,
            "bake_id": bake.bake_id,
            "operator_result": operator_result,
            "elapsed_seconds": elapsed,
            "before": before,
            "after": after,
            "warnings": warnings,
            "changed_objects": [obj.name],
        }
