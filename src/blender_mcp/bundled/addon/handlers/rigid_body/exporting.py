# pyright: reportGeneralTypeIssues=false
"""Failure-safe rigid-body animation interchange."""

import contextlib
import json
import math
import os
import tempfile

from pathlib import Path

import bpy
import mathutils

from ...helpers import preserve_mode_and_selection, set_active
from .inspection_and_setup import (
    _body_info,
    _constraint_flat_info,
    _require_finished,
    _scene,
    _validate_object_batch,
    _view_layer_for,
)

_FORMAT_SUFFIXES = {
    "JSON": {".json"},
    "ALEMBIC": {".abc"},
    "USD": {".usd", ".usda", ".usdc"},
    "GLTF": {".glb"},
    "FBX": {".fbx"},
}


def _coordinate_matrix(convention):
    if convention == "BLENDER_Z_UP":
        return mathutils.Matrix.Identity(4)
    if convention == "Y_UP_RIGHT_HANDED":
        return mathutils.Matrix.Rotation(-math.pi / 2.0, 4, "X")
    raise ValueError(f"Unsupported coordinate convention: {convention}")


def _json_payload(scene, objects, frames, convention, unit_scale):
    conversion = _coordinate_matrix(convention)
    inverse = conversion.inverted()
    scale = mathutils.Matrix(
        (
            (unit_scale, 0.0, 0.0, 0.0),
            (0.0, unit_scale, 0.0, 0.0),
            (0.0, 0.0, unit_scale, 0.0),
            (0.0, 0.0, 0.0, 1.0),
        )
    )
    samples = []
    for frame in frames:
        scene.frame_set(frame)
        view_layer = _view_layer_for(scene)
        view_layer.update()
        depsgraph = view_layer.depsgraph
        transforms = []
        for obj in objects:
            matrix = scale @ conversion @ obj.evaluated_get(depsgraph).matrix_world @ inverse
            transforms.append({"object": obj.name, "matrix_world": [list(row) for row in matrix]})
        samples.append({"frame": frame, "objects": transforms})
    object_names = {obj.name for obj in objects}
    constraints = []
    for constraint_object in scene.objects:
        constraint = constraint_object.rigid_body_constraint
        if constraint is None or constraint.object1 is None or constraint.object2 is None:
            continue
        if constraint.object1.name not in object_names and constraint.object2.name not in object_names:
            continue
        constraints.append(
            {
                "object": constraint_object.name,
                "object1": constraint.object1.name,
                "object2": constraint.object2.name,
                "settings": _constraint_flat_info(constraint),
            }
        )
    return {
        "schema": "blender-mcp-rigid-body-animation-1",
        "scene": scene.name,
        "coordinate_convention": convention,
        "unit_scale": unit_scale,
        "frame_rate": float(scene.render.fps) / max(float(scene.render.fps_base), 1e-9),
        "frame_range": {"start": frames[0], "end": frames[-1], "step": frames[1] - frames[0] if len(frames) > 1 else 1},
        "objects": [
            {
                "name": obj.name,
                "type": obj.type,
                "parent": obj.parent.name if obj.parent else None,
                "rigid_body": _body_info(obj),
                "rig_id": obj.get("blendermcp_rigid_body_rig_id"),
                "role": obj.get("blendermcp_rigid_body_role"),
            }
            for obj in objects
        ],
        "constraints": constraints,
        "samples": samples,
    }


def _run_export(scene, objects, format_name, path, frame_start, frame_end, frame_step, convention, unit_scale):
    view_layer = _view_layer_for(scene)
    operators = {
        "ALEMBIC": getattr(bpy.ops.wm, "alembic_export", None),
        "USD": getattr(bpy.ops.wm, "usd_export", None),
        "GLTF": getattr(bpy.ops.export_scene, "gltf", None),
        "FBX": getattr(bpy.ops.export_scene, "fbx", None),
    }
    operator = operators[format_name]
    if operator is None:
        raise RuntimeError(f"Blender 5.1 exporter is unavailable for {format_name}")
    kwargs = {"filepath": path, "check_existing": False}
    if format_name == "ALEMBIC":
        kwargs.update(selected=True, start=frame_start, end=frame_end, global_scale=unit_scale)
    elif format_name == "USD":
        kwargs.update(
            selected_objects_only=True,
            export_animation=True,
            convert_orientation=convention == "Y_UP_RIGHT_HANDED",
        )
        if convention == "Y_UP_RIGHT_HANDED":
            kwargs.update(export_global_forward_selection="NEGATIVE_Z", export_global_up_selection="Y")
    elif format_name == "GLTF":
        kwargs.update(
            export_format="GLB",
            use_selection=True,
            export_animations=True,
            export_frame_range=True,
            export_frame_step=frame_step,
            export_yup=True,
        )
    else:
        kwargs.update(
            use_selection=True,
            global_scale=unit_scale,
            axis_forward="-Z" if convention == "Y_UP_RIGHT_HANDED" else "-Y",
            axis_up="Y" if convention == "Y_UP_RIGHT_HANDED" else "Z",
            bake_anim=True,
            bake_anim_step=float(frame_step),
            bake_anim_simplify_factor=0.0,
        )
    with bpy.context.temp_override(scene=scene, view_layer=view_layer), preserve_mode_and_selection():
        active = view_layer.objects.active
        if active is not None and active.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        for candidate in view_layer.objects:
            candidate.select_set(False)
        for obj in objects:
            obj.select_set(True)
        set_active(objects[0])
        if not operator.poll():
            raise RuntimeError(f"{operator.idname()} is unavailable in the current Blender context")
        result = operator(**kwargs)
    _require_finished(result, operator.idname())


class RigidBodyExportHandlers:
    """Export explicit animation selections while preserving Blender context and source data."""

    def export_rigid_body_animation(
        self,
        scene_name,
        object_names,
        filepath,
        format,
        frame_start,
        frame_end,
        frame_step=1,
        coordinate_convention="BLENDER_Z_UP",
        unit_scale=1.0,
        confirm_overwrite=False,
    ):
        scene = _scene(scene_name)
        objects = _validate_object_batch(scene, object_names)
        if not 1 <= len(objects) <= 100 or len(objects) != len(set(objects)):
            raise ValueError("object_names must contain 1-100 unique objects")
        if frame_start > frame_end or not 1 <= frame_step <= 120:
            raise ValueError("Require frame_start <= frame_end and frame_step in [1, 120]")
        frames = list(range(frame_start, frame_end + 1, frame_step))
        if not frames or len(frames) * len(objects) > 50_000:
            raise ValueError("Export is limited to 50000 object-frame samples")
        if format not in _FORMAT_SUFFIXES:
            raise ValueError(f"Unsupported export format: {format}")
        if not math.isfinite(unit_scale) or not 0 < unit_scale <= 10_000:
            raise ValueError("unit_scale must be finite and in (0, 10000]")
        if format in {"USD", "GLTF"} and not math.isclose(unit_scale, 1.0):
            raise ValueError(f"{format} has no verified Blender 5.1 global-scale parameter; use unit_scale=1")
        if format == "GLTF" and coordinate_convention != "Y_UP_RIGHT_HANDED":
            raise ValueError("GLTF uses Y_UP_RIGHT_HANDED coordinates")
        if format == "ALEMBIC" and coordinate_convention != "BLENDER_Z_UP":
            raise ValueError("ALEMBIC export currently preserves Blender's Z-up convention")
        output = Path(bpy.path.abspath(filepath)).expanduser().resolve()
        if output.suffix.lower() not in _FORMAT_SUFFIXES[format]:
            expected = ", ".join(sorted(_FORMAT_SUFFIXES[format]))
            raise ValueError(f"{format} filepath must use one of: {expected}")
        if not output.parent.is_dir():
            raise ValueError(f"Export directory does not exist: {output.parent}")
        if output.exists() and not confirm_overwrite:
            raise ValueError(f"Export already exists; set confirm_overwrite=True to replace it: {output}")
        dynamic = [
            obj.name for obj in objects if obj.rigid_body and obj.rigid_body.enabled and not obj.rigid_body.kinematic
        ]
        if dynamic:
            world = scene.rigidbody_world
            if world is None or not world.point_cache.is_baked:
                raise ValueError(f"Dynamic rigid bodies require an approved baked world cache before export: {dynamic}")
        if format in {"GLTF", "FBX"} and any(obj.rigid_body is not None for obj in objects):
            raise ValueError(f"{format} requires transform-baked objects without live rigid bodies")
        original_frame = scene.frame_current
        original_subframe = scene.frame_subframe
        original_range = (scene.frame_start, scene.frame_end, scene.frame_step)
        file_descriptor, temporary_path = tempfile.mkstemp(
            prefix=f".{output.stem}-",
            suffix=output.suffix,
            dir=output.parent,
        )
        os.close(file_descriptor)
        try:
            scene.frame_start = frame_start
            scene.frame_end = frame_end
            scene.frame_step = frame_step
            if format == "JSON":
                payload = _json_payload(scene, objects, frames, coordinate_convention, unit_scale)
                with open(temporary_path, "w", encoding="utf-8") as stream:
                    json.dump(payload, stream, indent=2, sort_keys=True)
            else:
                os.unlink(temporary_path)
                _run_export(
                    scene,
                    objects,
                    format,
                    temporary_path,
                    frame_start,
                    frame_end,
                    frame_step,
                    coordinate_convention,
                    unit_scale,
                )
            if not os.path.isfile(temporary_path) or os.path.getsize(temporary_path) <= 0:
                raise RuntimeError(f"{format} exporter produced no non-empty output file")
            os.replace(temporary_path, output)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary_path)
            scene.frame_start, scene.frame_end, scene.frame_step = original_range
            scene.frame_set(original_frame, subframe=original_subframe)
            _view_layer_for(scene).update()
        contracts = {
            "JSON": "Evaluated world matrices plus hierarchy, rigid-body, and rigid-body-constraint metadata.",
            "ALEMBIC": "Evaluated transforms and geometry; Bullet constraints are not serialized.",
            "USD": "Evaluated scene animation and geometry; Bullet constraints are not guaranteed to round-trip.",
            "GLTF": "Baked transform/armature animation and meshes; Bullet rigid-body settings are not serialized.",
            "FBX": "Baked transform/armature animation and meshes; Bullet rigid-body settings are not serialized.",
        }
        return {
            "changed_objects": [],
            "created_files": [str(output)],
            "scene": scene.name,
            "objects": [obj.name for obj in objects],
            "format": format,
            "filepath": str(output),
            "bytes": output.stat().st_size,
            "frame_range": {"start": frame_start, "end": frame_end, "step": frame_step},
            "coordinate_convention": coordinate_convention,
            "unit_scale": unit_scale,
            "format_contract": contracts[format],
            "source_simulation_retained": True,
            "timeline_restored": {"frame": scene.frame_current, "subframe": scene.frame_subframe},
        }
