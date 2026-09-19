"""Blender-main-thread handlers for exporting cloth simulations."""

from __future__ import annotations

import contextlib
import hashlib
import os
import tempfile

import bpy

from ...helpers import preserve_mode_and_selection
from .inspection_and_setup import _get_object

_EXPORT_UNIT_METERS = {
    "METERS": 1.0,
    "CENTIMETERS": 0.01,
    "MILLIMETERS": 0.001,
}


def _export_frame_topology(objects, scene, view_layer, frames):
    records = []
    for frame in frames:
        scene.frame_set(frame)
        view_layer.update()
        counts = {}
        for obj in objects:
            evaluated = obj.evaluated_get(view_layer.depsgraph)
            mesh = evaluated.to_mesh()
            try:
                digest = hashlib.blake2b(digest_size=16)
                for polygon in mesh.polygons:
                    digest.update(len(polygon.vertices).to_bytes(4, "little"))
                    for vertex_index in polygon.vertices:
                        digest.update(int(vertex_index).to_bytes(8, "little"))
                counts[obj.name] = {
                    "vertices": len(mesh.vertices),
                    "edges": len(mesh.edges),
                    "faces": len(mesh.polygons),
                    "connectivity_digest": digest.hexdigest(),
                }
            finally:
                evaluated.to_mesh_clear()
        records.append({"frame": frame, "counts": counts})
    stable = all(record["counts"] == records[0]["counts"] for record in records[1:])
    return records, stable


def _set_scene_frame_range(scene, frame_start, frame_end, frame_step):
    if frame_start > scene.frame_end:
        scene.frame_end = frame_end
        scene.frame_start = frame_start
    else:
        scene.frame_start = frame_start
        scene.frame_end = frame_end
    scene.frame_step = frame_step
    if (scene.frame_start, scene.frame_end, scene.frame_step) != (frame_start, frame_end, frame_step):
        raise ValueError("Blender did not retain the requested scene frame range")


def _validate_distinct_axes(forward_axis, up_axis):
    forward = forward_axis.removeprefix("NEGATIVE_")
    up = up_axis.removeprefix("NEGATIVE_")
    if forward == up:
        raise ValueError("forward_axis and up_axis must use different axes")


class ClothExportingHandlers:
    """Blender-main-thread handlers for exporting cloth simulations."""

    def export_cloth_simulation(
        self,
        scene_name,
        filepath,
        file_format,
        object_names,
        frame_start,
        frame_end,
        frame_step,
        coordinate_space,
        units,
        forward_axis,
        up_axis,
        topology_policy,
        evaluation_policy,
        include_uvs=True,
        include_normals=True,
        include_vertex_colors=True,
        include_materials=True,
        overwrite=False,
        max_frames=500,
    ):
        scene = bpy.data.scenes.get(scene_name)
        if scene is None:
            raise ValueError(f"Scene not found: {scene_name}")
        if file_format not in {"ALEMBIC", "USD"}:
            raise ValueError("file_format must be ALEMBIC or USD")
        if coordinate_space not in {"WORLD", "LOCAL"}:
            raise ValueError("coordinate_space must be WORLD or LOCAL")
        if units not in {"SCENE", *_EXPORT_UNIT_METERS}:
            raise ValueError("units must be SCENE, METERS, CENTIMETERS, or MILLIMETERS")
        if topology_policy not in {"REQUIRE_STABLE", "ALLOW_VARYING"}:
            raise ValueError("topology_policy must be REQUIRE_STABLE or ALLOW_VARYING")
        if evaluation_policy not in {"REQUIRE_BAKED", "EVALUATE"}:
            raise ValueError("evaluation_policy must be REQUIRE_BAKED or EVALUATE")
        valid_axes = {"X", "Y", "Z", "NEGATIVE_X", "NEGATIVE_Y", "NEGATIVE_Z"}
        if forward_axis not in valid_axes or up_axis not in valid_axes:
            raise ValueError("forward_axis and up_axis must be explicit signed X, Y, or Z axes")
        _validate_distinct_axes(forward_axis, up_axis)
        if frame_step <= 0 or frame_start > frame_end:
            raise ValueError("frame_step must be positive and frame_start must be <= frame_end")
        frame_count = (frame_end - frame_start) // frame_step + 1
        if not 1 <= frame_count <= max_frames <= 2_000:
            raise ValueError("Export frame count must be positive, within max_frames, and max_frames <= 2000")
        if not object_names or len(object_names) > 64 or len(set(object_names)) != len(object_names):
            raise ValueError("object_names must contain 1-64 unique object names")
        objects = [_get_object(name, {"MESH"}) for name in object_names]
        if any(obj.name not in scene.objects for obj in objects):
            raise ValueError("Every export object must be linked to the explicit scene")
        view_layer = next(
            (layer for layer in scene.view_layers if all(obj.name in layer.objects for obj in objects)),
            None,
        )
        if view_layer is None:
            raise ValueError("No scene view layer contains every export object")
        cloths = [(obj, modifier) for obj in objects for modifier in obj.modifiers if modifier.type == "CLOTH"]
        if not cloths:
            raise ValueError("At least one export object must have a Cloth modifier")
        if evaluation_policy == "REQUIRE_BAKED":
            unbaked = [f"{obj.name}:{modifier.name}" for obj, modifier in cloths if not modifier.point_cache.is_baked]
            if unbaked:
                raise ValueError(f"REQUIRE_BAKED found unbaked cloth caches: {unbaked}")
        resolved = os.path.abspath(bpy.path.abspath(filepath))
        if not os.path.isabs(resolved):
            raise ValueError("filepath must resolve to an absolute path")
        expected_extensions = {"ALEMBIC": {".abc"}, "USD": {".usd", ".usda", ".usdc"}}[file_format]
        extension = os.path.splitext(resolved)[1].lower()
        if extension not in expected_extensions:
            raise ValueError(f"{file_format} filepath must use one of {sorted(expected_extensions)}")
        parent = os.path.dirname(resolved)
        if not os.path.isdir(parent) or not os.access(parent, os.W_OK):
            raise ValueError(f"Export parent directory must exist and be writable: {parent}")
        if os.path.exists(resolved) and not overwrite:
            raise ValueError("Export path already exists; set overwrite=True to replace it")
        if file_format == "ALEMBIC":
            if frame_step != 1:
                raise ValueError("Blender 5.1's Alembic exporter does not expose a frame-step option")
            if (forward_axis, up_axis) != ("NEGATIVE_Z", "Y"):
                raise ValueError("Blender 5.1's Alembic exporter has fixed NEGATIVE_Z forward and Y up orientation")

        frames = list(range(frame_start, frame_end + 1, frame_step))
        original_frame = scene.frame_current
        original_subframe = scene.frame_subframe
        original_range = (scene.frame_start, scene.frame_end, scene.frame_step)
        temporary_path = None
        topology_records = []
        stable_topology = True
        try:
            topology_records, stable_topology = _export_frame_topology(objects, scene, view_layer, frames)
            if topology_policy == "REQUIRE_STABLE" and not stable_topology:
                raise ValueError("Evaluated vertex/edge/face counts vary across the export frame range")
            descriptor, temporary_path = tempfile.mkstemp(prefix=".blendermcp-cloth-", suffix=extension, dir=parent)
            os.close(descriptor)
            os.unlink(temporary_path)
            _set_scene_frame_range(scene, frame_start, frame_end, frame_step)
            scene.frame_set(frame_start)
            with (
                bpy.context.temp_override(scene=scene, view_layer=view_layer),
                preserve_mode_and_selection(),
            ):
                for selected in list(bpy.context.selected_objects):
                    selected.select_set(False)
                for obj in objects:
                    obj.select_set(True)
                view_layer.objects.active = objects[0]
                if file_format == "ALEMBIC":
                    scene_scale = float(scene.unit_settings.scale_length) or 1.0
                    target_scale = scene_scale if units == "SCENE" else _EXPORT_UNIT_METERS[units]
                    result = bpy.ops.wm.alembic_export(
                        filepath=temporary_path,
                        start=frame_start,
                        end=frame_end,
                        selected=True,
                        flatten=coordinate_space == "WORLD",
                        uvs=include_uvs,
                        normals=include_normals,
                        vcolors=include_vertex_colors,
                        global_scale=scene_scale / target_scale,
                        export_custom_properties=True,
                        as_background_job=False,
                        evaluation_mode="RENDER",
                        init_scene_frame_range=False,
                    )
                else:
                    target_meters = (
                        float(scene.unit_settings.scale_length) or 1.0
                        if units == "SCENE"
                        else _EXPORT_UNIT_METERS[units]
                    )
                    result = bpy.ops.wm.usd_export(
                        filepath=temporary_path,
                        selected_objects_only=True,
                        export_animation=frame_count > 1,
                        export_uvmaps=include_uvs,
                        export_mesh_colors=include_vertex_colors,
                        export_normals=include_normals,
                        export_materials=include_materials,
                        export_custom_properties=True,
                        export_textures_mode="KEEP",
                        evaluation_mode="RENDER",
                        convert_orientation=True,
                        export_global_forward_selection=forward_axis,
                        export_global_up_selection=up_axis,
                        convert_scene_units="CUSTOM",
                        meters_per_unit=target_meters,
                        merge_parent_xform=coordinate_space == "WORLD",
                    )
            if "FINISHED" not in result:
                raise RuntimeError(f"{file_format} exporter did not finish: {sorted(result)}")
            if not os.path.isfile(temporary_path) or os.path.getsize(temporary_path) <= 0:
                raise RuntimeError(f"{file_format} exporter did not write a nonempty file")
            os.replace(temporary_path, resolved)
            temporary_path = None
        finally:
            _set_scene_frame_range(scene, *original_range)
            scene.frame_set(original_frame, subframe=original_subframe)
            view_layer.update()
            if temporary_path and os.path.exists(temporary_path):
                with contextlib.suppress(OSError):
                    os.unlink(temporary_path)
        warnings = []
        if evaluation_policy == "EVALUATE":
            warnings.append("Export evaluation may have populated unbaked in-memory cloth caches.")
        if file_format == "ALEMBIC" and include_materials:
            warnings.append("Blender's Alembic exporter does not export Blender material networks.")
        return {
            "changed_objects": object_names if evaluation_policy == "EVALUATE" else [],
            "filepath": resolved,
            "format": file_format,
            "bytes": os.path.getsize(resolved),
            "scene": scene.name,
            "objects": object_names,
            "frame_range": {"start": frame_start, "end": frame_end, "step": frame_step, "count": frame_count},
            "coordinate_space": coordinate_space,
            "coordinate_space_contract": (
                "Parent hierarchy is flattened and transforms are written in world coordinates."
                if file_format == "ALEMBIC" and coordinate_space == "WORLD"
                else "Object-local geometry and parent hierarchy are retained."
                if coordinate_space == "LOCAL"
                else "USD object transforms preserve world placement; point data remains object-local."
            ),
            "units": units,
            "axes": {"forward": forward_axis, "up": up_axis},
            "topology": {
                "policy": topology_policy,
                "stable_counts": stable_topology,
                "per_frame": topology_records,
            },
            "attributes": {
                "uvs": include_uvs,
                "normals": include_normals,
                "vertex_colors": include_vertex_colors,
                "materials": include_materials if file_format == "USD" else False,
            },
            "evaluation_policy": evaluation_policy,
            "source_objects_and_caches_preserved": True,
            "warnings": warnings,
        }
