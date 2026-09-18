# ruff: file-ignore[missing-return-type-private-function, missing-return-type-undocumented-public-function, missing-type-function-argument, no-self-use, too-many-arguments, too-many-branches, too-many-positional-arguments, too-many-statements-in-try-clause, undocumented-public-method]
"""Blender-side scene rendering and view-layer handlers."""

import math
import os
import time

from contextlib import suppress

import bpy

_VIEW_LAYER_PROPERTIES = {
    "use",
    "use_sky",
    "use_solid",
    "use_strand",
    "use_pass_combined",
    "use_pass_z",
    "use_pass_mist",
    "use_pass_normal",
    "use_pass_position",
    "use_pass_vector",
    "use_pass_uv",
    "use_pass_object_index",
    "use_pass_material_index",
    "use_pass_cryptomatte_object",
    "use_pass_cryptomatte_material",
    "use_pass_cryptomatte_asset",
    "pass_cryptomatte_depth",
}

_RENDER_PROPERTIES = {
    "engine",
    "resolution_x",
    "resolution_y",
    "resolution_percentage",
    "pixel_aspect_x",
    "pixel_aspect_y",
    "fps",
    "fps_base",
    "film_transparent",
}
_SCENE_PROPERTIES = {"frame_start", "frame_end", "frame_step"}
_IMAGE_PROPERTY_MAPPING = {
    "image_format": "file_format",
    "color_mode": "color_mode",
    "color_depth": "color_depth",
    "compression": "compression",
    "quality": "quality",
}
_CYCLES_PROPERTY_MAPPING = {"cycles_samples": "samples", "cycles_use_denoising": "use_denoising"}
_RENDER_PATCH_PROPERTIES = (
    _RENDER_PROPERTIES
    | _SCENE_PROPERTIES
    | set(_IMAGE_PROPERTY_MAPPING)
    | set(_CYCLES_PROPERTY_MAPPING)
    | {"motion_blur", "film", "output", "metadata", "multiview", "cycles", "eevee"}
)
_MAX_ANIMATION_FRAMES = 10_000


def _scene(name):
    scene = bpy.data.scenes.get(name) if name else bpy.context.scene
    if scene is None:
        raise ValueError(f"Scene not found: {name}")
    return scene


def _pass_info(layer):
    return {
        name: getattr(layer, name)
        for name in sorted(_VIEW_LAYER_PROPERTIES)
        if name.startswith("use_pass_") or name == "pass_cryptomatte_depth"
    }


def _layer_info(layer):
    return {
        "name": layer.name,
        "use": layer.use,
        "use_sky": layer.use_sky,
        "use_solid": layer.use_solid,
        "use_strand": layer.use_strand,
        "material_override": layer.material_override.name if layer.material_override else None,
        "world_override": layer.world_override.name if layer.world_override else None,
        "passes": _pass_info(layer),
    }


def _render_info(scene):
    render = scene.render
    image = render.image_settings
    cycles = getattr(scene, "cycles", None)
    compositor_tree = getattr(scene, "compositing_node_group", None) or getattr(scene, "node_tree", None)
    return {
        "scene": scene.name,
        "engine": render.engine,
        "camera": scene.camera.name if scene.camera else None,
        "resolution": [render.resolution_x, render.resolution_y, render.resolution_percentage],
        "pixel_aspect": [render.pixel_aspect_x, render.pixel_aspect_y],
        "fps": render.fps,
        "fps_base": render.fps_base,
        "frame_range": [scene.frame_start, scene.frame_end, scene.frame_step],
        "film_transparent": render.film_transparent,
        "motion_blur": {
            "enabled": getattr(render, "use_motion_blur", None),
            "shutter": getattr(render, "motion_blur_shutter", None),
            "position": getattr(render, "motion_blur_position", None),
        },
        "film": {
            "transparent": render.film_transparent,
            "transparent_glass": getattr(cycles, "film_transparent_glass", None) if cycles else None,
            "transparent_roughness": getattr(cycles, "film_transparent_roughness", None) if cycles else None,
        },
        "output": {
            "filepath": render.filepath,
            "file_format": image.file_format,
            "color_mode": image.color_mode,
            "color_depth": image.color_depth,
            "compression": image.compression,
            "quality": image.quality,
            "use_file_extension": render.use_file_extension,
            "use_overwrite": render.use_overwrite,
            "use_placeholder": render.use_placeholder,
            "exr_codec": getattr(image, "exr_codec", None),
        },
        "cycles": {
            "samples": cycles.samples,
            "use_denoising": cycles.use_denoising,
        }
        if cycles is not None
        else None,
        "eevee": _rna_values(getattr(scene, "eevee", None), ("taa_samples", "taa_render_samples", "use_shadows")),
        "metadata": _rna_values(
            render,
            (
                "use_stamp",
                "use_stamp_date",
                "use_stamp_time",
                "use_stamp_render_time",
                "use_stamp_frame",
                "use_stamp_frame_range",
                "use_stamp_camera",
                "use_stamp_scene",
                "use_stamp_note",
                "stamp_note_text",
            ),
        ),
        "multiview": {
            "enabled": getattr(render, "use_multiview", None),
            "views_format": getattr(image, "views_format", None),
            "stereo_3d_format": getattr(getattr(image, "stereo_3d_format", None), "display_mode", None),
        },
        "view_layers": [_layer_info(layer) for layer in scene.view_layers],
        "compositor": {
            "use_nodes": scene.use_nodes,
            "node_tree": compositor_tree.name if compositor_tree else None,
            "node_count": len(compositor_tree.nodes) if compositor_tree else 0,
        },
    }


def _rna_values(owner, names):
    if owner is None:
        return None
    return {name: getattr(owner, name) for name in names if hasattr(owner, name)}


def _page(records, offset, limit):
    total = len(records)
    start = min(max(0, int(offset)), total)
    size = max(1, min(int(limit), 1000))
    end = min(start + size, total)
    return {
        "total": total,
        "offset": start,
        "limit": size,
        "returned_count": end - start,
        "truncated": end < total,
        "next_offset": end if end < total else None,
        "records": records[start:end],
    }


def _compositor_info(scene, graph_sections, offset, limit):
    tree = getattr(scene, "compositing_node_group", None) or getattr(scene, "node_tree", None)
    sections = set(graph_sections or ("NODES", "LINKS", "DEPENDENCIES"))
    result = {
        "use_nodes": scene.use_nodes,
        "node_tree": tree.name if tree else None,
        "node_count": len(tree.nodes) if tree else 0,
        "link_count": len(tree.links) if tree else 0,
    }
    if tree is None:
        return result
    if "NODES" in sections:
        nodes = [
            {
                "name": node.name,
                "type": node.bl_idname,
                "label": node.label,
                "mute": node.mute,
                "inputs": [socket.identifier for socket in node.inputs],
                "outputs": [socket.identifier for socket in node.outputs],
            }
            for node in tree.nodes
        ]
        result["nodes"] = _page(nodes, offset, limit)
    if "LINKS" in sections:
        links = [
            {
                "from_node": link.from_node.name,
                "from_socket": link.from_socket.identifier,
                "to_node": link.to_node.name,
                "to_socket": link.to_socket.identifier,
            }
            for link in tree.links
        ]
        result["links"] = _page(links, offset, limit)
    if "DEPENDENCIES" in sections:
        dependencies = []
        for node in tree.nodes:
            for property_name in ("scene", "image", "movie_clip", "node_tree", "texture"):
                value = getattr(node, property_name, None)
                if value is not None and getattr(value, "name", None):
                    dependencies.append(
                        {
                            "node": node.name,
                            "property": property_name,
                            "id_type": value.bl_rna.identifier,
                            "name": value.name,
                        }
                    )
            if node.bl_idname == "CompositorNodeOutputFile":
                dependencies.append(
                    {
                        "node": node.name,
                        "property": "base_path",
                        "path": node.base_path,
                        "slots": [slot.path for slot in node.file_slots],
                    }
                )
        result["dependencies"] = _page(dependencies, offset, limit)
    return result


def _render_pass_info(scene, view_layer_name, render_result):
    """Return rendered passes, or the verified view-layer contract on Blender 5.2+."""
    result_layers = getattr(render_result, "layers", None) if render_result is not None else None
    if result_layers is not None:
        passes = [
            {"layer": layer.name, "pass": render_pass.name} for layer in result_layers for render_pass in layer.passes
        ]
        return passes, "RENDER_RESULT"

    # Blender 5.2 removed Image.layers from the Python API. The render itself and
    # output file are still verified above; report the enabled pass contract from
    # the exact view layer that was rendered instead of claiming no passes exist.
    layers = [scene.view_layers[view_layer_name]] if view_layer_name else list(scene.view_layers)
    passes = []
    for layer in layers:
        with suppress(Exception):
            layer.update_render_passes()
        for prop in layer.bl_rna.properties:
            if not prop.identifier.startswith("use_pass_") or not getattr(layer, prop.identifier, False):
                continue
            passes.append({"layer": layer.name, "pass": prop.name, "setting": prop.identifier})
    return passes, "VIEW_LAYER_CONFIGURATION"


def _set_properties(owner, patch, mapping=None):
    previous = {}
    mapping = mapping or {}
    try:
        for name, value in patch.items():
            property_name = mapping.get(name, name)
            previous[property_name] = getattr(owner, property_name)
            setattr(owner, property_name, value)
    except Exception:
        for name, value in previous.items():
            with suppress(Exception):
                setattr(owner, name, value)
        raise
    return previous


def _set_supported(owner, patch, label, mapping=None):
    mapping = mapping or {}
    unavailable = sorted(name for name in patch if not hasattr(owner, mapping.get(name, name)))
    if unavailable:
        raise ValueError(f"{label} settings are unavailable in this Blender runtime: {unavailable}")
    return _set_properties(owner, patch, mapping)


def _validate_render_patch(patch):
    if not isinstance(patch, dict) or not patch:
        raise ValueError("patch must be a non-empty object")
    unknown = sorted(set(patch) - _RENDER_PATCH_PROPERTIES)
    if unknown:
        raise ValueError(f"Unsupported render settings: {unknown}")
    numeric_ranges = {
        "resolution_x": (4, 65_536),
        "resolution_y": (4, 65_536),
        "resolution_percentage": (1, 100),
        "pixel_aspect_x": (0, 200),
        "pixel_aspect_y": (0, 200),
        "fps": (1, 960),
        "fps_base": (0, 1000),
        "frame_step": (1, None),
        "compression": (0, 100),
        "quality": (0, 100),
        "cycles_samples": (1, 16_384),
    }
    for name, (minimum, maximum) in numeric_ranges.items():
        if name not in patch:
            continue
        value = patch[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{name} must be a finite number")
        if value < minimum or (
            minimum == 0 and name in {"pixel_aspect_x", "pixel_aspect_y", "fps_base"} and value == 0
        ):
            comparator = "greater than" if minimum == 0 else "at least"
            raise ValueError(f"{name} must be {comparator} {minimum}")
        if maximum is not None and value > maximum:
            raise ValueError(f"{name} must be at most {maximum}")
    allowed_values = {
        "engine": {"BLENDER_EEVEE", "BLENDER_WORKBENCH", "CYCLES"},
        "image_format": {"PNG", "JPEG", "OPEN_EXR", "OPEN_EXR_MULTILAYER", "TIFF", "WEBP"},
        "color_mode": {"BW", "RGB", "RGBA"},
        "color_depth": {"8", "16", "32"},
    }
    for name, allowed in allowed_values.items():
        if name in patch and patch[name] not in allowed:
            raise ValueError(f"Unsupported {name}: {patch[name]}")
    return patch


class RenderingHandlersMixin:
    """Expose production render configuration and bounded rendering."""

    def inspect_render_setup(self, scene_name=None, graph_sections=None, limit=100, offset=0):
        scene = _scene(scene_name)
        result = _render_info(scene)
        result["compositor"] = _compositor_info(scene, graph_sections, offset, limit)
        return result

    def configure_render_settings(self, scene_name, patch):
        scene = _scene(scene_name)
        patch = _validate_render_patch(patch)
        resulting_start = patch.get("frame_start", scene.frame_start)
        resulting_end = patch.get("frame_end", scene.frame_end)
        if resulting_end < resulting_start:
            raise ValueError("Resulting frame_end must be greater than or equal to frame_start")
        before = _render_info(scene)
        snapshots = []
        try:
            snapshots.append(
                (
                    scene.render,
                    _set_properties(scene.render, {k: v for k, v in patch.items() if k in _RENDER_PROPERTIES}),
                )
            )
            snapshots.append(
                (scene, _set_properties(scene, {k: v for k, v in patch.items() if k in _SCENE_PROPERTIES}))
            )
            snapshots.append(
                (
                    scene.render.image_settings,
                    _set_properties(
                        scene.render.image_settings,
                        {k: v for k, v in patch.items() if k in _IMAGE_PROPERTY_MAPPING},
                        _IMAGE_PROPERTY_MAPPING,
                    ),
                )
            )
            cycles_patch = {k: v for k, v in patch.items() if k in _CYCLES_PROPERTY_MAPPING}
            if cycles_patch:
                if not hasattr(scene, "cycles"):
                    raise ValueError("Cycles settings are unavailable in this Blender build")
                snapshots.append((scene.cycles, _set_properties(scene.cycles, cycles_patch, _CYCLES_PROPERTY_MAPPING)))
            nested = {key: value for key, value in patch.items() if isinstance(value, dict)}
            resulting_engine = patch.get("engine", scene.render.engine)
            if nested.get("cycles"):
                if resulting_engine != "CYCLES":
                    raise ValueError("cycles settings require the CYCLES render engine")
                snapshots.append((scene.cycles, _set_supported(scene.cycles, nested["cycles"], "Cycles")))
            if nested.get("eevee"):
                if resulting_engine != "BLENDER_EEVEE":
                    raise ValueError("eevee settings require the BLENDER_EEVEE render engine")
                snapshots.append((scene.eevee, _set_supported(scene.eevee, nested["eevee"], "EEVEE")))
            if nested.get("motion_blur"):
                mapping = {
                    "enabled": "use_motion_blur",
                    "shutter": "motion_blur_shutter",
                    "position": "motion_blur_position",
                }
                snapshots.append(
                    (scene.render, _set_supported(scene.render, nested["motion_blur"], "motion blur", mapping))
                )
            if nested.get("film"):
                film = dict(nested["film"])
                if "transparent" in film:
                    snapshots.append(
                        (scene.render, _set_properties(scene.render, {"film_transparent": film.pop("transparent")}))
                    )
                if film:
                    mapping = {
                        "transparent_glass": "film_transparent_glass",
                        "transparent_roughness": "film_transparent_roughness",
                    }
                    snapshots.append((scene.cycles, _set_supported(scene.cycles, film, "Cycles film", mapping)))
            if nested.get("output"):
                output_patch = dict(nested["output"])
                render_patch = {
                    key: output_patch.pop(key)
                    for key in tuple(output_patch)
                    if key in {"filepath", "use_file_extension", "use_overwrite", "use_placeholder"}
                }
                snapshots.append((scene.render, _set_supported(scene.render, render_patch, "render output")))
                snapshots.append(
                    (
                        scene.render.image_settings,
                        _set_supported(
                            scene.render.image_settings, output_patch, "image output", _IMAGE_PROPERTY_MAPPING
                        ),
                    )
                )
            if nested.get("metadata"):
                snapshots.append((scene.render, _set_supported(scene.render, nested["metadata"], "render metadata")))
            if nested.get("multiview"):
                multiview = dict(nested["multiview"])
                if "enabled" in multiview:
                    snapshots.append(
                        (
                            scene.render,
                            _set_supported(scene.render, {"use_multiview": multiview.pop("enabled")}, "multiview"),
                        )
                    )
                stereo = multiview.pop("stereo_3d_format", None)
                snapshots.append(
                    (
                        scene.render.image_settings,
                        _set_supported(scene.render.image_settings, multiview, "multiview image"),
                    )
                )
                if stereo is not None:
                    stereo_owner = scene.render.image_settings.stereo_3d_format
                    snapshots.append(
                        (stereo_owner, _set_supported(stereo_owner, {"display_mode": stereo}, "stereo output"))
                    )
            if scene.frame_end < scene.frame_start:
                raise ValueError("Resulting frame_end must be greater than or equal to frame_start")
        except Exception:
            for owner, values in reversed(snapshots):
                for name, value in values.items():
                    with suppress(Exception):
                        setattr(owner, name, value)
            raise
        return {
            "changed": sorted(patch),
            "before": before,
            "after": _render_info(scene),
            "settings": _render_info(scene),
            "changed_resources": [scene.name],
        }

    def manage_view_layers(self, scene_name, action, view_layer_name, patch=None, confirm_remove=False):
        scene = _scene(scene_name)
        action = str(action).upper()
        if action not in {"CREATE", "PATCH", "REMOVE"}:
            raise ValueError(f"Unsupported view-layer action: {action}")
        if not isinstance(view_layer_name, str) or not view_layer_name.strip():
            raise ValueError("view_layer_name must be a non-empty string")
        if patch is not None and not isinstance(patch, dict):
            raise ValueError("patch must be an object")
        if action == "PATCH" and not patch:
            raise ValueError("PATCH requires a non-empty patch")
        if action == "REMOVE" and patch:
            raise ValueError("REMOVE does not accept patch")
        layer = scene.view_layers.get(view_layer_name)
        if action == "CREATE":
            if layer is not None:
                raise ValueError(f"View layer already exists: {view_layer_name}")
            layer = scene.view_layers.new(view_layer_name)
        elif layer is None:
            raise ValueError(f"View layer not found: {view_layer_name}")
        if action in {"CREATE", "PATCH"}:
            patch = patch or {}
            unknown = sorted(set(patch) - _VIEW_LAYER_PROPERTIES - {"material_override", "world_override"})
            if unknown:
                raise ValueError(f"Unsupported view-layer settings: {unknown}")
            prepared = {key: value for key, value in patch.items() if key in _VIEW_LAYER_PROPERTIES}
            if "material_override" in patch:
                name = patch["material_override"]
                material = bpy.data.materials.get(name) if name else None
                if name and material is None:
                    raise ValueError(f"Material not found: {name}")
                prepared["material_override"] = material
            if "world_override" in patch:
                name = patch["world_override"]
                world = bpy.data.worlds.get(name) if name else None
                if name and world is None:
                    raise ValueError(f"World not found: {name}")
                prepared["world_override"] = world
            try:
                _set_properties(layer, prepared)
            except Exception:
                if action == "CREATE":
                    scene.view_layers.remove(layer)
                raise
        elif action == "REMOVE":
            if not confirm_remove:
                raise ValueError("confirm_remove=True is required")
            if len(scene.view_layers) == 1:
                raise ValueError("A scene must retain at least one view layer")
            scene.view_layers.remove(layer)
            return {"removed": view_layer_name, "changed_resources": [view_layer_name]}
        return {"view_layer": _layer_info(layer), "changed_resources": [layer.name]}

    def render_scene(
        self,
        scene_name,
        filepath,
        mode="STILL",
        view_layer_name=None,
        frame=None,
        max_animation_frames=250,
        confirm_render=False,
        confirm_overwrite=False,
        render_slot_policy="USE_ACTIVE",
        verify_outputs=True,
        verify_passes=True,
        max_duration_seconds=None,
    ):
        if not confirm_render:
            raise ValueError("confirm_render=True is required")
        scene = _scene(scene_name)
        mode = str(mode).upper()
        if mode not in {"STILL", "ANIMATION"}:
            raise ValueError("mode must be STILL or ANIMATION")
        if isinstance(max_animation_frames, bool) or not isinstance(max_animation_frames, int):
            raise ValueError("max_animation_frames must be an integer")
        if not 1 <= max_animation_frames <= _MAX_ANIMATION_FRAMES:
            raise ValueError("max_animation_frames must be between 1 and 10000")
        if frame is not None and (isinstance(frame, bool) or not isinstance(frame, int)):
            raise ValueError("frame must be an integer")
        if mode == "ANIMATION" and frame is not None:
            raise ValueError("frame is only valid for STILL renders")
        if not isinstance(filepath, str) or not filepath.strip():
            raise ValueError("filepath must be a non-empty string")
        output = os.path.abspath(bpy.path.abspath(filepath))
        directory = os.path.dirname(output)
        if not directory or not os.path.isdir(directory):
            raise ValueError(f"Output directory does not exist: {directory}")
        if mode == "STILL" and os.path.exists(output) and not confirm_overwrite:
            raise ValueError("Output file already exists; set confirm_overwrite=True to replace it")
        if view_layer_name and scene.view_layers.get(view_layer_name) is None:
            raise ValueError(f"View layer not found: {view_layer_name}")
        render_slot_policy = str(render_slot_policy).upper()
        if render_slot_policy not in {"USE_ACTIVE", "NEW_SLOT", "REPLACE_ACTIVE"}:
            raise ValueError("render_slot_policy must be USE_ACTIVE, NEW_SLOT, or REPLACE_ACTIVE")
        if max_duration_seconds is not None and (
            isinstance(max_duration_seconds, bool)
            or not isinstance(max_duration_seconds, (int, float))
            or not math.isfinite(max_duration_seconds)
            or max_duration_seconds <= 0
        ):
            raise ValueError("max_duration_seconds must be a positive finite number")
        frame_count = ((scene.frame_end - scene.frame_start) // scene.frame_step) + 1
        if mode == "ANIMATION" and frame_count > max_animation_frames:
            raise ValueError(
                f"Animation contains {frame_count} frames, exceeding max_animation_frames={max_animation_frames}"
            )

        original_path = scene.render.filepath
        original_frame = scene.frame_current
        started = time.monotonic()
        written_files = []
        progress = []
        cancelled = False
        render_result = bpy.data.images.get("Render Result")
        if render_slot_policy == "NEW_SLOT" and render_result is not None:
            slot = render_result.render_slots.new(name=f"MCP {int(time.time())}")
            render_result.render_slots.active_index = list(render_result.render_slots).index(slot)
        try:
            scene.render.filepath = output
            frames = (
                list(range(scene.frame_start, scene.frame_end + 1, scene.frame_step))
                if mode == "ANIMATION"
                else [frame if frame is not None else scene.frame_current]
            )
            result = {"FINISHED"}
            for index, current_frame in enumerate(frames):
                if max_duration_seconds is not None and time.monotonic() - started >= max_duration_seconds:
                    cancelled = True
                    break
                scene.frame_set(current_frame)
                if mode == "ANIMATION":
                    scene.render.filepath = output
                    frame_output = os.path.abspath(scene.render.frame_path(frame=current_frame))
                    if os.path.exists(frame_output) and not confirm_overwrite:
                        raise ValueError(
                            f"Animation output already exists for frame {current_frame}; "
                            "set confirm_overwrite=True to replace it"
                        )
                    scene.render.filepath = frame_output
                else:
                    frame_output = output
                kwargs = {"animation": False, "write_still": True, "scene": scene.name}
                if view_layer_name:
                    kwargs["layer"] = view_layer_name
                result = bpy.ops.render.render(**kwargs)
                if "FINISHED" not in result:
                    raise RuntimeError(f"Blender render was cancelled at frame {current_frame}: {result}")
                exists = os.path.isfile(frame_output)
                if verify_outputs and not exists:
                    raise RuntimeError(f"Render reported FINISHED but output is missing: {frame_output}")
                written_files.append(
                    {
                        "frame": current_frame,
                        "path": frame_output,
                        "bytes": os.path.getsize(frame_output) if exists else None,
                    }
                )
                if len(progress) < 1000:
                    progress.append(
                        {
                            "frame": current_frame,
                            "completed": index + 1,
                            "total": len(frames),
                            "fraction": (index + 1) / len(frames),
                        }
                    )
        finally:
            scene.render.filepath = original_path
            scene.frame_set(original_frame)
        render_result = bpy.data.images.get("Render Result")
        passes, pass_verification = _render_pass_info(scene, view_layer_name, render_result)
        if verify_passes and not passes:
            raise RuntimeError("Render completed but no enabled passes could be verified")
        duration = time.monotonic() - started
        return {
            "scene": scene.name,
            "mode": mode,
            "filepath": output,
            "frame": frame if frame is not None else scene.frame_current,
            "frame_count": len(written_files),
            "operator_result": sorted(result),
            "settings_restored": True,
            "status": "CANCELLED" if cancelled else "COMPLETED",
            "cancelled": cancelled,
            "cancellation_reason": "max_duration_seconds exceeded" if cancelled else None,
            "duration_seconds": duration,
            "render_slot_policy": render_slot_policy,
            "files": written_files,
            "passes": passes,
            "pass_verification": pass_verification,
            "progress": progress,
            "progress_truncated": len(written_files) > len(progress),
        }

    def inspect_render_output(self, filepath, output_path=None, frame=None, max_size=1000, format="png"):
        """
        Read a previously rendered frame's pixels into a bounded copy for visual inspection.

        Unlike a viewport screenshot, this reads actual render output: an explicit
        output_path (a file render_scene already wrote), read-only and never modified,
        or - when omitted - the in-memory "Render Result" datablock. Render Result only
        ever reflects the most recently rendered frame, so an animation's earlier frames
        are only reachable through their own written output_path.

        Args:
            filepath: Destination path this call writes the (possibly downscaled) copy to.
            output_path: Path to an existing rendered file on disk. Takes precedence over frame.
            frame: Frame number the in-memory Render Result must currently hold; only
                checked when output_path is omitted.
            max_size: Maximum size in pixels for the largest dimension of the saved copy.
            format: Image format for the saved copy (png, jpg, etc.)

        Returns:
            success status with width/height/native dimensions, source, and frame.

        Raises:
            ValueError: If the operation cannot be completed.
            RuntimeError: If the operation cannot be completed.

        """
        if not filepath:
            raise ValueError("No destination filepath provided")

        staging_path = None
        if output_path:
            if not os.path.isfile(output_path):
                raise ValueError(f"Render output file not found: {output_path}")
            source = "output_path"
            img = bpy.data.images.load(output_path, check_existing=False)
        else:
            render_result = bpy.data.images.get("Render Result")
            if render_result is None:
                raise RuntimeError("No render result available; render a frame first with render_scene")
            current_frame = bpy.context.scene.frame_current
            if frame is not None and frame != current_frame:
                raise ValueError(
                    f"Render Result currently holds frame {current_frame}, not {frame}; pass "
                    "output_path to inspect a specific previously-written frame instead"
                )
            frame = current_frame
            source = "render_result"
            staging_path = f"{filepath}.src.png"
            render_result.save_render(filepath=staging_path)
            img = bpy.data.images.load(staging_path, check_existing=False)

        try:
            native_width, native_height = img.size
            width, height = native_width, native_height
            if max(width, height) > max_size:
                scale = max_size / max(width, height)
                width, height = max(1, int(width * scale)), max(1, int(height * scale))
                img.scale(width, height)
            img.filepath_raw = filepath
            img.file_format = format.upper()
            img.save()
        finally:
            bpy.data.images.remove(img)
            if staging_path and os.path.exists(staging_path):
                os.remove(staging_path)

        return {
            "success": True,
            "width": width,
            "height": height,
            "native_width": native_width,
            "native_height": native_height,
            "filepath": filepath,
            "source": source,
            "source_path": output_path,
            "frame": frame,
        }
