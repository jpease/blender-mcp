# ruff: file-ignore[too-many-branches, too-many-locals, undocumented-public-method]
"""Blender-side scene rendering and view-layer handlers."""

import math
import os
import re
import time

from contextlib import contextmanager, suppress

import bpy

from ..file_paths import create_save_directory, enforce_roots
from ..helpers import color_management_snapshot
from ..image_reply import finalize_image_reply, image_destination
from ..output_roots import configured_file_roots
from ..render_devices import effective_cycles_device
from ..render_job_supervisor import refuse_existing_output, render_frame
from ..render_properties import FLAT_ROUTES, NESTED_SECTIONS, RENDER_PATCH_PROPERTIES
from ..render_result_record import record_render_result, render_result_record
from .blend_files import require_bool

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

_MAX_ANIMATION_FRAMES = 10_000
# Extensions Blender writes for the image formats this tool's patch model can select, measured on
# 5.2 (PNG .png, JPEG .jpg, OPEN_EXR .exr, TIFF .tif, WEBP .webp) plus the alternate spellings a
# caller types by hand. Deliberately not texture/_shared's SUPPORTED_IMAGE_EXTENSIONS: that set
# lists images Blender can *read* and includes formats (.psd) it never renders to.
_RENDER_OUTPUT_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".exr", ".tif", ".tiff", ".webp"})
# Blender pads a frame number to four digits unless the frame itself needs more (measured on 5.2:
# "sh010_" -> "sh010_0001.png", frame 12345 -> "sh010_12345.png").
_FRAME_IN_FILENAME = re.compile(r"(?<![0-9])[0-9]{4}$")
# How many per-frame progress records a detail=True ANIMATION reply carries. Its twin is
# `_PROGRESS_ENTRY_LIMIT` in `src/blender_mcp/server/tools/rendering.py`, which caps the
# orchestrated per-frame path at the same length: one animation must truncate identically
# however it was driven. `tests/test_rendering_twins.py` fails when the two differ.
_PROGRESS_ENTRY_LIMIT = 1000
# A STILL is one `bpy.ops.render.render` call, which nothing in-process can interrupt, so a
# duration bound on it would be checked once, before it starts, and never again. Its twin is
# `_STILL_DURATION_REFUSAL` in `src/blender_mcp/server/tools/rendering.py`, which refuses the
# same request before it is sent; `tests/test_rendering_twins.py` fails when the two differ.
_STILL_DURATION_REFUSAL = (
    "max_duration_seconds cannot bound a STILL render: one frame renders in a single blocking call "
    "nothing in-process can interrupt, so the bound would never be applied. For a hard wall-clock "
    'limit use manage_render_job(action="CREATE", ..., max_duration_seconds=...); otherwise omit it.'
)


def _duration_overrun_warnings(duration_seconds, max_duration_seconds):
    """
    Say so when an ANIMATION ran past its between-frames duration bound.

    Its twin is `_duration_overrun_warnings` in `src/blender_mcp/server/tools/rendering.py`,
    for the orchestrated path; `tests/test_rendering_twins.py` fails when the two disagree.

    Args:
        duration_seconds: How long the run took.
        max_duration_seconds: The caller's bound, or None.

    Returns:
        list[str]: One warning when the run overran the bound, else none.

    """
    if max_duration_seconds is None or duration_seconds <= max_duration_seconds:
        return []
    return [
        f"This render took {duration_seconds:.1f} s, over max_duration_seconds={max_duration_seconds:g}: "
        "the bound is checked between frames, and a frame in progress always finishes. For a hard "
        'wall-clock limit use manage_render_job(action="CREATE", ..., max_duration_seconds=...).'
    ]


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
        # What Cycles will actually use here, which `cycles.device` only requests.
        "effective_cycles_device": effective_cycles_device(scene)[0],
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
        "cycles": _rna_values(
            cycles,
            (
                "samples",
                "preview_samples",
                "use_adaptive_sampling",
                "adaptive_threshold",
                "time_limit",
                "device",
                "use_denoising",
                "denoiser",
                "denoising_use_gpu",
                "denoising_input_passes",
                "denoising_prefilter",
                "denoising_quality",
                "pixel_filter_type",
                "filter_width",
            ),
        ),
        "eevee": _eevee_info(getattr(scene, "eevee", None)),
        # The names configure_render_settings' "performance" section takes.
        "performance": _rna_values(
            render,
            (
                "use_persistent_data",
                "use_simplify",
                "simplify_subdivision",
                "simplify_subdivision_render",
                "simplify_child_particles",
                "simplify_child_particles_render",
                "simplify_volumes",
                "filter_size",
            ),
        ),
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


def _eevee_info(eevee):
    """
    Report EEVEE sampling plus the ray-tracing state that decides whether glass renders at all.

    Blender 5.2 splits these across two structs: the switch and method sit on `scene.eevee`, the
    screen-trace controls on its `ray_tracing_options`. Both are reported because a scene with
    `use_raytracing` off renders clear glass black, and that is otherwise invisible in this reply.

    Args:
        eevee: The scene's EEVEE settings, or None on a build without them.

    Returns:
        dict | None: Present sampling and ray-tracing values, with "ray_tracing" holding the
        nested screen-trace options (None on a runtime that does not expose them).

    """
    if eevee is None:
        return None
    # `eevee` is not None here, so `_rna_values` returns a dict; `or {}` says that to the checker
    # rather than asserting it.
    info = (
        _rna_values(
            eevee,
            ("taa_samples", "taa_render_samples", "use_shadows", "use_raytracing", "ray_tracing_method"),
        )
        or {}
    )
    info["ray_tracing"] = _rna_values(
        getattr(eevee, "ray_tracing_options", None),
        ("resolution_scale", "screen_trace_quality", "screen_trace_thickness", "trace_max_roughness", "use_denoise"),
    )
    return info


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


def _set_properties(owner, patch, mapping=None, applied=None, prefix=""):
    previous = {}
    mapping = mapping or {}
    try:
        for name, value in patch.items():
            property_name = mapping.get(name, name)
            previous[property_name] = getattr(owner, property_name)
            setattr(owner, property_name, value)
            if applied is not None:
                applied[prefix + name] = (owner, property_name)
    except Exception:
        for name, value in previous.items():
            with suppress(Exception):
                setattr(owner, name, value)
        raise
    return previous


def _set_supported(owner, patch, label, mapping=None, applied=None, prefix=""):
    mapping = mapping or {}
    unavailable = sorted(name for name in patch if not hasattr(owner, mapping.get(name, name)))
    if unavailable:
        raise ValueError(f"{label} settings are unavailable in this Blender runtime: {unavailable}")
    return _set_properties(owner, patch, mapping, applied, prefix)


@contextmanager
def _rolled_back(snapshots):
    """
    Undo every property the body wrote if it raises, then re-raise.

    Reverse order, so an owner written by two routes ends on the value it started with.

    Args:
        snapshots: The (owner, {property: previous value}) list the body appends to as it
            writes. Read only when the body fails.

    Yields:
        None.

    Raises:
        Exception: Whatever the body raised, after the restore.

    """
    try:
        yield
    except Exception:
        for owner, values in reversed(snapshots):
            for name, value in values.items():
                with suppress(Exception):
                    setattr(owner, name, value)
        raise


def _section_owner(scene, owner_path):
    """
    Walk a `render_properties` owner path from the scene.

    Args:
        scene: The scene being patched.
        owner_path: A dotted path such as `render.image_settings`, or "" for the scene itself.

    Returns:
        The owner struct, or None when this Blender build has no such struct - which is also
        how an engine this build was compiled without (`scene.cycles`) reports itself.

    """
    owner = scene
    for part in owner_path.split(".") if owner_path else ():
        owner = getattr(owner, part, None)
        if owner is None:
            return None
    return owner


def _apply_routes(scene, routes, patch, applied, snapshots, *, prefix):
    """
    Apply one group of patch values across every owner `render_properties` routes it to.

    Each route but the last claims exactly the keys its mapping names; the last route takes
    whatever is left, with its mapping as a translation table and an identity fallback. That
    is what makes an unknown key reach `_set_supported` and be refused rather than dropped.

    Args:
        scene: The scene being patched.
        routes: `FLAT_ROUTES`, or one section's routes out of `NESTED_SECTIONS`.
        patch: The client's values for that group.
        applied: The handler's patch-key -> (owner, property) record.
        snapshots: The handler's rollback list, appended to per owner written.
        prefix: What the group's keys are reported under - "" for the flat routes, whose keys
            are already the reply's own names.

    Raises:
        ValueError: When this Blender build has no such struct, or a key is unsupported.

    """
    remaining = dict(patch)
    for index, (owner_path, mapping, label) in enumerate(routes):
        is_last = index == len(routes) - 1
        values = remaining if is_last else {key: remaining.pop(key) for key in tuple(remaining) if key in mapping}
        if not values:
            continue
        owner = _section_owner(scene, owner_path)
        if owner is None:
            raise ValueError(f"{label} settings are unavailable in this Blender runtime")
        snapshots.append((owner, _set_supported(owner, values, label, dict(mapping or {}), applied, prefix)))


def _pending_sections(scene, patch):
    """
    Take the nested sections out of a patch, refuse what this scene cannot take, and order them.

    Args:
        scene: The scene being patched.
        patch: The validated client patch.

    Returns:
        dict: section -> values, in `NESTED_SECTIONS` order rather than the client's key order,
        so two runs of the same patch write the same owners in the same sequence.

    Raises:
        ValueError: If a section needs an engine this patch does not leave selected, or its
            output template names a directory.

    """
    nested = {key: value for key, value in patch.items() if isinstance(value, dict)}
    resulting_engine = patch.get("engine", scene.render.engine)
    if nested.get("cycles") and resulting_engine != "CYCLES":
        raise ValueError("cycles settings require the CYCLES render engine")
    if nested.get("eevee") and resulting_engine != "BLENDER_EEVEE":
        raise ValueError("eevee settings require the BLENDER_EEVEE render engine")
    pending = {section: dict(values) for section, values in nested.items() if values}
    # Blender 5.2 keeps the screen-trace controls on a nested RaytraceEEVEE struct,
    # scene.eevee.ray_tracing_options, so they travel as their own section.
    ray_tracing = pending.get("eevee", {}).pop("ray_tracing", None)
    if ray_tracing:
        pending["eevee.ray_tracing"] = dict(ray_tracing)
    if pending.get("output", {}).get("filepath") is not None:
        # Refused here, at the moment it is stored, so a later render that reads the scene's
        # own template cannot inherit a shape Blender writes beside itself.
        _refuse_container_output(scene, pending["output"]["filepath"])
    return {section: pending[section] for section in NESTED_SECTIONS if pending.get(section)}


def _patch_reply(scene, applied, *, before, authored_range, detail):
    """
    Shape `configure_render_settings`' reply, with or without full before/after render info.

    Args:
        scene: The patched scene.
        applied: patch key -> (owner, RNA property) for every key written.
        before: The pre-patch `_render_info`, when detail was asked for, else None.
        authored_range: Whether this patch set the scene's frame range, which is reported as
            its own pseudo-key because it is a scene custom property, not an RNA write.
        detail: Whether to report the whole render configuration rather than the keys written.

    Returns:
        dict: "scene", "changed", "changed_resources", and either "before"/"after" render info
        or an "after" holding just the values this call wrote.

    """
    changed = sorted([*applied, "frame_range_authored"] if authored_range else applied)
    if detail:
        return {
            "scene": scene.name,
            "changed": changed,
            "before": before,
            "after": _render_info(scene),
            "changed_resources": [scene.name],
        }
    # `applied` holds (owner, property) pairs; the marker is not one, so it is added here
    # rather than smuggled into a mapping the comprehension below unpacks.
    after = {path: getattr(owner, name) for path, (owner, name) in applied.items()}
    if authored_range:
        after["frame_range_authored"] = True
    return {"scene": scene.name, "changed": changed, "after": after, "changed_resources": [scene.name]}


def _validate_render_patch(patch):
    if not isinstance(patch, dict) or not patch:
        raise ValueError("patch must be a non-empty object")
    unknown = sorted(set(patch) - RENDER_PATCH_PROPERTIES)
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


def _resolved_path(path):
    """
    Resolve a caller's path text to the file it names, the way the render writer does.

    `~`, Blender's `//` blend-relative prefix and a plain relative path each name a real file
    only once resolved. Doing it in one place is what keeps a file `render_scene` wrote
    readable by `inspect_render_output` from the same text the caller typed: the two differed
    on `~` for as long as each spelled the resolution itself.

    Args:
        path: The caller's path text.

    Returns:
        str: The absolute path that text names.

    """
    return os.path.abspath(bpy.path.abspath(os.path.expanduser(path)))


def _render_output_suggestion(name, extension):
    """
    Rebuild a filename with the scene's image-format extension, the way Blender itself would.

    Measured on 5.2: a still render into "sh010.jpg" while PNG is active writes "sh010.png" - an
    extension Blender recognises is replaced, anything else is kept and the real one appended.

    Args:
        name: Basename the caller asked for.
        extension: Extension the scene's image format writes, leading dot included.

    Returns:
        str: The basename carrying that extension.

    """
    suffix = os.path.splitext(name)[1]
    stem = name[: -len(suffix)] if suffix.lower() in _RENDER_OUTPUT_EXTENSIONS else name
    return stem + extension


def _refuse_container_output(scene, filepath):
    """
    Refuse an output path Blender would treat as a prefix rather than a folder.

    Blender appends the frame number to the path as given, so a directory writes files beside
    it and leaves it empty. Shared by `render_scene` and `configure_render_settings`, which
    must refuse the same shape at the moment it is stored.

    Args:
        scene: Scene whose image format decides the suggested extension.
        filepath: The caller's text, used verbatim in the message.

    Raises:
        ValueError: When the path names a directory, or is not a usable string.

    """
    if not isinstance(filepath, str) or not filepath.strip():
        raise ValueError("filepath must be a non-empty string")
    extension = scene.render.file_extension
    output = _resolved_path(filepath)
    # os.path.abspath drops a trailing separator, so the directory intent has to be read off the
    # caller's own text before it is normalised away.
    if filepath.endswith(("/", os.sep)) or os.path.isdir(output):
        shown = filepath.rstrip("/" + os.sep) or filepath
        raise ValueError(
            f"Output filepath is a directory: {filepath!r}. Blender appends the frame number to the "
            f"path as given, so a directory writes files beside it and leaves it empty. Pass a "
            f"filename prefix inside it, such as {shown + '/frame_'!r} or "
            f"{shown + '/frame_####' + extension!r}."
        )


def _resolve_render_output(scene, filepath, mode, create_directories=False):
    """
    Resolve a caller's output path to the exact file Blender will write, or refuse its shape.

    Blender never treats the render path as a container. For an ANIMATION it appends the frame
    number after the whole path (measured on 5.2: ".../renders" -> ".../renders0001.png",
    ".../sh010.png" -> ".../sh010.png0001.png"); for a STILL it swaps in the active format's
    extension (".../sh010_" -> ".../sh010_.png"). Both rules put the file somewhere the reply
    would not name, which on stage reads as a render that vanished, so the shapes that cannot be
    honoured are refused here - before any frame is rendered - naming the path that would work.

    Args:
        scene: Scene whose render settings decide the extension.
        filepath: The caller's requested path, used verbatim in errors so the message never
            exposes Blender's working directory.
        mode: Either "STILL" or "ANIMATION".
        create_directories: Accept an output directory that does not exist yet; the caller
            makes it with `_create_output_directory` once every other check has passed.

    Returns:
        str: Absolute path to assign to scene.render.filepath.

    Raises:
        ValueError: If the directory does not exist and may not be created, or the path's shape
            would make Blender write somewhere other than where the caller asked.

    """
    extension = scene.render.file_extension
    _refuse_container_output(scene, filepath)
    output = _resolved_path(filepath)
    directory = os.path.dirname(output)
    if not directory or (not create_directories and not os.path.isdir(directory)):
        # The caller's own text, not the resolved path, which can expose Blender's working directory.
        raise ValueError(
            f"Output directory does not exist: {os.path.dirname(filepath) or '.'}; pass "
            "create_directories=true to create it"
        )
    name = os.path.basename(output)
    suffix = os.path.splitext(name)[1].lower()
    if mode == "ANIMATION":
        looks_like_one_file = suffix in _RENDER_OUTPUT_EXTENSIONS or suffix == extension.lower()
        # `suffix` gates the slice below: without it `name[:-0]` would silently empty the stem.
        if suffix and "#" not in name and looks_like_one_file:
            stem = name[: -len(suffix)]
            raise ValueError(
                f"Output filepath names a single file: {filepath!r}. An ANIMATION writes one file per "
                f"frame and Blender appends the frame number after the whole path, so this would "
                f"write {name + format(scene.frame_start, '04d') + extension!r}. Pass a frame prefix "
                f"such as {stem + '_'!r}, or a template such as {stem + '_####' + extension!r}."
            )
    elif scene.render.use_file_extension and suffix != extension.lower():
        raise ValueError(
            f"Output filepath does not end in the scene's image-format extension {extension!r}: "
            f"{filepath!r}. A STILL render writes one file and Blender applies that extension itself, "
            f"so the reply would name a file that was never written. Pass "
            f"{os.path.join(os.path.dirname(filepath), _render_output_suggestion(name, extension))!r}."
        )
    return output


def _create_output_directory(output):
    """
    Create a render output's missing directory, inside the file roots when they are configured.

    A render into an existing directory is not held to the roots; making a new one is, the same
    rule `save_shot(create_directories=true)` follows, because creating a directory is a write
    of its own that no render setting implied.

    Args:
        output: The absolute path `_resolve_render_output` returned.

    Returns:
        bool: True when a directory was created, False when it already existed.

    Raises:
        ValueError: When the directory lies outside the configured roots or cannot be made.

    """
    if os.path.isdir(os.path.dirname(output)):
        return False
    enforce_roots(output, configured_file_roots())
    return create_save_directory(output)


def _frame_from_filename(path):
    """
    Recover the frame number Blender encoded in a written filename, or None when it is ambiguous.

    Blender writes the frame as a four-digit run immediately before the extension, so that run is
    readable back. A longer run is not: ".../sh0100001.png" is the prefix "sh010" plus frame 1 and
    is indistinguishable from a five-plus-digit frame, and a shorter run came from a "##" template
    that a version suffix looks exactly like. Those stay None rather than become a guess.

    Args:
        path: Path to a file Blender rendered.

    Returns:
        int | None: The frame number, or None when the filename does not carry one unambiguously.

    """
    match = _FRAME_IN_FILENAME.search(os.path.splitext(os.path.basename(path))[0])
    return int(match.group()) if match else None


def _default_requested_filepath(scene, filepath):
    """
    Resolve the caller's requested output path, defaulting to the scene's own stored template.

    Shared by render_scene and plan_render_animation, so the "no filepath and no scene default"
    refusal is worded identically whichever entry point a caller used.

    Args:
        scene: Scene whose stored render.filepath is the fallback.
        filepath: The caller's explicit path, or None to use the scene's own.

    Returns:
        str: The resolved, non-empty requested path text (not yet validated for shape).

    Raises:
        ValueError: If no path was given and the scene has none stored, or the resolved text is
            empty.

    """
    requested_filepath = filepath
    if requested_filepath is None:
        requested_filepath = scene.render.filepath
        if not isinstance(requested_filepath, str) or not requested_filepath.strip():
            raise ValueError(
                "No filepath was given and the scene has no render output path set. Pass filepath, "
                "or set it once with configure_render_settings(patch={'output': {'filepath': ...}})."
            )
    if not isinstance(requested_filepath, str) or not requested_filepath.strip():
        raise ValueError("filepath must be a non-empty string")
    return requested_filepath


def _validate_animation_frame_range(scene, max_animation_frames, confirm_frame_range, frame_range=None):
    """
    Enforce the max_animation_frames cap and the untouched-default-range guard.

    Shared by render_scene's ANIMATION branch, plan_render_animation and manage_render_job, so an
    animation's frame count is validated identically whether or not, and wherever, it is rendered.

    Args:
        scene: Scene whose frame_step - and, without `frame_range`, frame_start/frame_end -
            decide the frame count.
        max_animation_frames: Upper bound on the number of frames this call may touch; the
            caller has already checked this is an int in range.
        confirm_frame_range: Whether the caller explicitly accepted rendering the untouched
            default 1-250 range.
        frame_range: An explicit (frame_start, frame_end) pair the caller chose instead of the
            scene's own range, or None. A range the caller named is a choice, so the
            untouched-default guard does not apply to it.

    Returns:
        list[int]: The frames the animation covers, in render order.

    Raises:
        ValueError: If an explicit range ends before it starts, the frame count exceeds
            max_animation_frames, or the range is Blender's untouched default and unconfirmed.

    """
    frame_start, frame_end = frame_range or (scene.frame_start, scene.frame_end)
    if frame_end < frame_start:
        raise ValueError("frame_end must be greater than or equal to frame_start")
    frame_count = ((frame_end - frame_start) // scene.frame_step) + 1
    if frame_count > max_animation_frames:
        raise ValueError(
            f"Animation contains {frame_count} frames, exceeding max_animation_frames={max_animation_frames}"
        )
    if (
        frame_range is None
        and (frame_start, frame_end) == (1, 250)
        and not scene.get("blender_mcp_frame_range_authored", False)
        and not confirm_frame_range
    ):
        raise ValueError(
            "The scene's frame range is still Blender's default 1-250 and no MCP call has set it; "
            f"this ANIMATION would render {frame_count} frames. Set frame_start/frame_end with "
            "configure_render_settings, or pass confirm_frame_range=true to render 1-250 deliberately."
        )
    return list(range(frame_start, frame_end + 1, scene.frame_step))


def _animation_summary(
    *,
    scene_name,
    mode,
    output,
    frame,
    engine,
    effective_cycles_device,
    warnings,
    files,
    frame_total,
    operator_result,
    persisted,
    cancelled,
    cancellation_reason,
    duration_seconds,
    render_slot_policy,
    passes,
    pass_verification,
    created_directory,
    detail,
):
    """
    Build a render reply out of the frames that were actually written.

    This is the one place the shape is spelled on this side of the socket. Its twin is
    `_animation_summary` in `src/blender_mcp/server/tools/rendering.py`, which builds the same
    keys from N per-frame STILL replies for the orchestrated path; the add-on cannot import the
    server package, so the shape is stated twice, and `tests/test_rendering_twins.py` fails when
    the two disagree.

    Args:
        scene_name: Name of the scene that rendered.
        mode: "STILL" or "ANIMATION".
        output: The resolved absolute output path or per-frame template.
        frame: The frame the reply reports as current.
        engine: The render engine the frames rendered with.
        effective_cycles_device: The device Cycles rendered on ("CPU"/"GPU"), None for another engine.
        warnings: Notices for the envelope, such as a GPU request that rendered on the CPU.
        files: One record per written frame, in render order: "frame", "path", "bytes".
        frame_total: How many frames the run planned, which is what "fraction" divides by.
        operator_result: The last rendered frame's sorted operator result, or ["FINISHED"]
            when no frame rendered.
        persisted: Whether the caller's output template was written back onto the scene.
        cancelled: Whether the run stopped before every planned frame.
        cancellation_reason: Why it stopped, or None.
        duration_seconds: Wall-clock time the run took.
        render_slot_policy: The caller's requested slot policy, echoed back.
        passes: Render passes read after the run.
        pass_verification: How `passes` was established.
        created_directory: Whether the call created the output's missing directory.
        detail: Whether to add the per-frame "files"/"progress" arrays.

    Returns:
        dict: The render reply, with "files"/"progress"/"progress_truncated" only when detail and
        "warnings" only when there are any.

    """
    progress = [
        {
            "frame": entry["frame"],
            "completed": index + 1,
            "total": frame_total,
            "fraction": (index + 1) / frame_total,
        }
        for index, entry in enumerate(files[:_PROGRESS_ENTRY_LIMIT])
    ]
    summary = {
        "scene": scene_name,
        "mode": mode,
        "engine": engine,
        "effective_cycles_device": effective_cycles_device,
        "filepath": output,
        "frame": frame,
        "frame_count": len(files),
        "operator_result": operator_result,
        "settings_restored": not persisted,
        "status": "CANCELLED" if cancelled else "COMPLETED",
        "cancelled": cancelled,
        "cancellation_reason": cancellation_reason,
        "duration_seconds": duration_seconds,
        "render_slot_policy": render_slot_policy,
        "output_persisted": persisted,
        "first_file": files[0]["path"] if files else None,
        "last_file": files[-1]["path"] if files else None,
        "bytes_written": sum(entry["bytes"] or 0 for entry in files),
        "passes": passes,
        "pass_verification": pass_verification,
        "created_directory": created_directory,
    }
    if warnings:
        summary["warnings"] = warnings
    if not detail:
        # Absent rather than empty: `envelope._record_pages` shortens a page it can see, and
        # warning about data nobody asked for spends the reply budget on bookkeeping.
        return summary
    return {**summary, "files": files, "progress": progress, "progress_truncated": len(files) > len(progress)}


def _validate_render_request(scene, *, mode, frame, max_animation_frames, view_layer_name, max_duration_seconds):
    """
    Check the request fields `render_scene` and `manage_render_job` share, before any output is resolved.

    One definition, so a render in this process and a render job refuse the same request in the
    same words. What only one of them refuses - a STILL `max_duration_seconds` in process, which
    no in-process render can honour - stays with that one.

    Args:
        scene: Scene whose view layers `view_layer_name` must name.
        mode: "STILL" or "ANIMATION", in any case.
        frame: The STILL frame, or None.
        max_animation_frames: Upper bound on the frames an ANIMATION may cover.
        view_layer_name: The one view layer to render, or None for every enabled one.
        max_duration_seconds: The caller's wall-clock bound, or None.

    Returns:
        str: The mode, upper-cased.

    Raises:
        ValueError: On the first field out of shape or range.

    """
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
    if view_layer_name and scene.view_layers.get(view_layer_name) is None:
        raise ValueError(f"View layer not found: {view_layer_name}")
    if max_duration_seconds is not None and (
        isinstance(max_duration_seconds, bool)
        or not isinstance(max_duration_seconds, (int, float))
        or not math.isfinite(max_duration_seconds)
        or max_duration_seconds <= 0
    ):
        raise ValueError("max_duration_seconds must be a positive finite number")
    return mode


def _frame_outputs(scene, output, frames):
    """
    Name the exact file Blender writes for each frame of an ANIMATION template.

    `frame_path()` is Blender's own templating, not reproducible outside bpy; it reads
    `scene.render.filepath`, which is restored before returning whatever it held.

    Args:
        scene: Scene whose output format decides the extension.
        output: The resolved absolute per-frame template.
        frames: Frame numbers, in render order.

    Returns:
        list[dict]: One {"frame": int, "path": str} per frame, in the order given.

    """
    original_path = scene.render.filepath
    try:
        scene.render.filepath = output
        return [{"frame": frame, "path": os.path.abspath(scene.render.frame_path(frame=frame))} for frame in frames]
    finally:
        scene.render.filepath = original_path


def plan_render_job(
    scene_name,
    *,
    filepath,
    mode,
    frame,
    frame_start,
    frame_end,
    max_animation_frames,
    view_layer_name,
    max_duration_seconds,
    confirm_overwrite,
    confirm_frame_range,
    create_directories,
):
    """
    Validate a `manage_render_job` request with `render_scene`'s own checks, and name every file it writes.

    Nothing is rendered or created. The job renders in another process, so what `render_scene`
    only finds partway through - an ANIMATION frame whose file already exists - is refused here,
    before that process starts.

    Args:
        scene_name: Scene to render, or the active scene when omitted.
        filepath: The caller's output path; defaults to the scene's stored template, as in
            `render_scene`.
        mode: "STILL" or "ANIMATION".
        frame: The STILL frame, or None for the scene's current frame.
        frame_start: An ANIMATION's explicit first frame, or None for the scene's; passed
            together with frame_end.
        frame_end: An ANIMATION's explicit last frame, or None for the scene's.
        max_animation_frames: Upper bound on the frames an ANIMATION may cover.
        view_layer_name: The one view layer to render, or None for every enabled one.
        max_duration_seconds: The job's wall-clock bound, or None.
        confirm_overwrite: Whether existing output files may be replaced.
        confirm_frame_range: Whether the scene's untouched 1-250 default range is intended.
        create_directories: Accept an output directory that does not exist yet; the caller
            creates it once everything else has succeeded.

    Returns:
        dict: "scene" (the Scene), "mode", "output" (the absolute STILL file or ANIMATION
        template), and "frames" - one {"frame", "path"} per frame, in render order.

    Raises:
        ValueError: On any request `render_scene` would refuse, a half-given or non-integer
            frame range, a frame range on a STILL, or an output file that already exists
            unconfirmed.

    """
    scene = _scene(scene_name)
    mode = _validate_render_request(
        scene,
        mode=mode,
        frame=frame,
        max_animation_frames=max_animation_frames,
        view_layer_name=view_layer_name,
        max_duration_seconds=max_duration_seconds,
    )
    frame_range = None
    if frame_start is not None or frame_end is not None:
        if mode == "STILL":
            raise ValueError("frame_start/frame_end are only valid for ANIMATION renders; a STILL takes frame")
        if any(isinstance(value, bool) or not isinstance(value, int) for value in (frame_start, frame_end)):
            raise ValueError("Pass frame_start and frame_end together as integers, or neither to use the scene's range")
        frame_range = (frame_start, frame_end)
    requested_filepath = _default_requested_filepath(scene, filepath)
    output = _resolve_render_output(
        scene, requested_filepath, mode, require_bool("create_directories", create_directories)
    )
    confirm_overwrite = require_bool("confirm_overwrite", confirm_overwrite)
    if mode == "STILL":
        refuse_existing_output(output, confirm_overwrite)
        frames = [{"frame": frame if frame is not None else scene.frame_current, "path": output}]
    else:
        numbers = _validate_animation_frame_range(scene, max_animation_frames, confirm_frame_range, frame_range)
        frames = _frame_outputs(scene, output, numbers)
        for entry in frames:
            refuse_existing_output(entry["path"], confirm_overwrite, entry["frame"])
    return {"scene": scene, "mode": mode, "output": output, "frames": frames}


class RenderingHandlersMixin:
    """Expose production render configuration and bounded rendering."""

    def inspect_render_setup(self, scene_name=None, graph_sections=None, limit=100, offset=0):
        scene = _scene(scene_name)
        result = _render_info(scene)
        result["color_management"] = color_management_snapshot(scene)
        result["compositor"] = _compositor_info(scene, graph_sections, offset, limit)
        _effective, device_warning = effective_cycles_device(scene)
        if device_warning:
            result["warnings"] = [device_warning]
        return result

    def configure_render_settings(self, scene_name, patch, detail=False):
        scene = _scene(scene_name)
        patch = _validate_render_patch(patch)
        resulting_start = patch.get("frame_start", scene.frame_start)
        resulting_end = patch.get("frame_end", scene.frame_end)
        if resulting_end < resulting_start:
            raise ValueError("Resulting frame_end must be greater than or equal to frame_start")
        before = _render_info(scene) if detail else None
        applied = {}
        snapshots = []
        with _rolled_back(snapshots):
            # A non-dict value is a flat property; every nested section arrives as a dict. The
            # flat routes' last entry takes the remainder, so a flat key no route claims is
            # refused by name rather than silently skipped.
            flat = {key: value for key, value in patch.items() if not isinstance(value, dict)}
            if flat:
                _apply_routes(scene, FLAT_ROUTES, flat, applied, snapshots, prefix="")
            for section, values in _pending_sections(scene, patch).items():
                _apply_routes(scene, NESTED_SECTIONS[section], values, applied, snapshots, prefix=f"{section}.")
            if scene.frame_end < scene.frame_start:
                raise ValueError("Resulting frame_end must be greater than or equal to frame_start")
        # A scene custom property, so "an MCP call chose this range" survives save and reopen.
        # `render_scene`'s default-range guard reads it; nothing else may write it.
        authored_range = any(key in patch for key in ("frame_start", "frame_end"))
        if authored_range:
            scene["blender_mcp_frame_range_authored"] = True
        return _patch_reply(scene, applied, before=before, authored_range=authored_range, detail=detail)

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

    def plan_render_animation(
        self, scene_name, filepath=None, max_animation_frames=250, confirm_frame_range=False, create_directories=False
    ):
        """
        Validate and resolve an ANIMATION render without rendering anything.

        Read-only: scene.render.filepath is restored before returning, whatever it was set to
        while computing frame_path(). Shared validation (_default_requested_filepath,
        _validate_animation_frame_range) keeps this plan's refusals - and frame count - identical
        to render_scene(mode="ANIMATION")'s own. Exists so a caller can drive an animation as N
        independent STILL calls (for per-frame progress and cancellation) while still getting
        Blender's own frame_path() templating for each frame's output path, which is not
        reproducible outside bpy.

        Args:
            scene_name: Scene to plan against, or the active scene when omitted.
            filepath: Caller's requested animation template; defaults to the scene's own stored
                render output path, exactly as render_scene does.
            max_animation_frames: Upper bound on the frames this plan may cover.
            confirm_frame_range: Whether the caller explicitly accepted the untouched 1-250
                default range.
            create_directories: Accept an output directory that does not exist yet. Nothing is
                created here; the first frame's render_scene call makes it.

        Returns:
            dict: requested_filepath (the caller's template, unresolved - what persist_output
                would store), output (its absolute resolved form), frame_current (unchanged by
                this call), and frames - an ordered list of {"frame": int, "path": str}, each
                path the exact absolute file render_scene(mode="STILL", frame=..., filepath=...)
                must be given to reproduce this animation's naming.

        Raises:
            ValueError: The same shapes render_scene(mode="ANIMATION") itself refuses: no
                filepath and no scene default, an unusable output shape, an out-of-bounds
                max_animation_frames, too many frames, or an unconfirmed default frame range.

        """
        scene = _scene(scene_name)
        if isinstance(max_animation_frames, bool) or not isinstance(max_animation_frames, int):
            raise ValueError("max_animation_frames must be an integer")
        if not 1 <= max_animation_frames <= _MAX_ANIMATION_FRAMES:
            raise ValueError("max_animation_frames must be between 1 and 10000")
        requested_filepath = _default_requested_filepath(scene, filepath)
        output = _resolve_render_output(
            scene, requested_filepath, "ANIMATION", require_bool("create_directories", create_directories)
        )
        frames = _validate_animation_frame_range(scene, max_animation_frames, confirm_frame_range)
        plan = _frame_outputs(scene, output, frames)
        return {
            "requested_filepath": requested_filepath,
            "output": output,
            "frame_current": scene.frame_current,
            "frames": plan,
        }

    def render_scene(
        self,
        scene_name,
        filepath=None,
        mode="STILL",
        view_layer_name=None,
        frame=None,
        max_animation_frames=250,
        confirm_render=False,
        confirm_overwrite=False,
        confirm_frame_range=False,
        render_slot_policy="USE_ACTIVE",
        verify_outputs=True,
        verify_passes=True,
        max_duration_seconds=None,
        persist_output=False,
        detail=False,
        create_directories=False,
    ):
        if not confirm_render:
            raise ValueError("confirm_render=True is required")
        scene = _scene(scene_name)
        mode = _validate_render_request(
            scene,
            mode=mode,
            frame=frame,
            max_animation_frames=max_animation_frames,
            view_layer_name=view_layer_name,
            max_duration_seconds=max_duration_seconds,
        )
        if max_duration_seconds is not None and mode == "STILL":
            raise ValueError(_STILL_DURATION_REFUSAL)
        requested_filepath = _default_requested_filepath(scene, filepath)
        if persist_output and mode == "STILL":
            raise ValueError(
                "persist_output stores a per-frame template, and a STILL path names one file: storing it "
                "would make the next ANIMATION write '<name>.png0001.png'. Persist from an ANIMATION, or "
                "set the template with configure_render_settings."
            )
        create_directories = require_bool("create_directories", create_directories)
        output = _resolve_render_output(scene, requested_filepath, mode, create_directories)
        if mode == "STILL":
            refuse_existing_output(output, confirm_overwrite)
        render_slot_policy = str(render_slot_policy).upper()
        if render_slot_policy not in {"USE_ACTIVE", "NEW_SLOT", "REPLACE_ACTIVE"}:
            raise ValueError("render_slot_policy must be USE_ACTIVE, NEW_SLOT, or REPLACE_ACTIVE")
        if mode == "ANIMATION":
            _validate_animation_frame_range(scene, max_animation_frames, confirm_frame_range)
        # Last of the checks, so a refused render leaves no directory behind.
        created_directory = _create_output_directory(output) if create_directories else False

        original_path = scene.render.filepath
        original_frame = scene.frame_current
        started = time.monotonic()
        written_files = []
        cancelled = False
        completed = False
        render_result = bpy.data.images.get("Render Result")
        if render_slot_policy == "NEW_SLOT" and render_result is not None:
            slot = render_result.render_slots.new(name=f"MCP {int(time.time())}")
            render_result.render_slots.active_index = list(render_result.render_slots).index(slot)
        try:
            frames = (
                list(range(scene.frame_start, scene.frame_end + 1, scene.frame_step))
                if mode == "ANIMATION"
                else [frame if frame is not None else scene.frame_current]
            )
            result = {"FINISHED"}
            for current_frame in frames:
                # Checked between frames only: a frame in progress always finishes, so a run can
                # overshoot the bound by up to one frame, which the reply then warns about.
                if max_duration_seconds is not None and time.monotonic() - started >= max_duration_seconds:
                    cancelled = True
                    break
                entry, result = render_frame(
                    scene,
                    current_frame,
                    output,
                    animation=mode == "ANIMATION",
                    view_layer_name=view_layer_name,
                    confirm_overwrite=confirm_overwrite,
                    require_output=verify_outputs,
                )
                written_files.append(entry)
                # After the render, whose start cleared any earlier record.
                record_render_result(scene.name, current_frame, entry["path"] if entry["bytes"] is not None else None)
            completed = True
        finally:
            # The caller's template text, never the resolved absolute path: persisting `output`
            # would replace a portable `//renders/sh010_` with this machine's layout and make
            # `inspect_delivery` report the scene as unportable.
            persisted = bool(persist_output) and not cancelled and completed
            scene.render.filepath = requested_filepath if persisted else original_path
            scene.frame_set(original_frame)
        render_result = bpy.data.images.get("Render Result")
        passes, pass_verification = _render_pass_info(scene, view_layer_name, render_result)
        if verify_passes and not passes:
            raise RuntimeError("Render completed but no enabled passes could be verified")
        effective_device, device_warning = effective_cycles_device(scene)
        duration_seconds = time.monotonic() - started
        return _animation_summary(
            scene_name=scene.name,
            mode=mode,
            output=output,
            frame=frame if frame is not None else scene.frame_current,
            engine=scene.render.engine,
            effective_cycles_device=effective_device,
            warnings=[
                *([device_warning] if device_warning else []),
                *_duration_overrun_warnings(duration_seconds, max_duration_seconds),
            ],
            files=written_files,
            frame_total=len(frames),
            # `result` is the last frame's operator result, and stays the initial {"FINISHED"}
            # when the loop never ran - the same value the orchestrated twin reports by reading
            # the last per-frame reply, or ["FINISHED"] when there was none.
            operator_result=sorted(result),
            persisted=persisted,
            cancelled=cancelled,
            cancellation_reason="max_duration_seconds exceeded" if cancelled else None,
            duration_seconds=duration_seconds,
            render_slot_policy=render_slot_policy,
            passes=passes,
            pass_verification=pass_verification,
            created_directory=created_directory,
            detail=detail,
        )

    def inspect_render_output(
        self, filepath=None, output_path=None, frame=None, max_size=1000, format="png", inline=False
    ):
        """
        Read a previously rendered frame's pixels into a bounded copy for visual inspection.

        Unlike a viewport screenshot, this reads actual render output: an explicit
        output_path (a file render_scene already wrote), read-only and never modified,
        or - when omitted - the in-memory "Render Result" datablock. Render Result only
        ever reflects the most recent render, whoever started it, so an animation's earlier
        frames are only reachable through their own written output_path. Its scene, frame and
        source_path are what render_scene recorded when it rendered that frame; a render this
        add-on did not record (Blender's UI, a preview tool) leaves them null with a warning
        that the pixels' origin is unknown.

        Args:
            filepath: Destination path this call writes the (possibly downscaled) copy to;
                optional when `inline`, which lets this process choose its own.
            output_path: Path to an existing rendered file on disk. Takes precedence over frame;
                the reported frame is then read back out of the filename Blender wrote, and stays
                null when that filename does not carry one unambiguously. `~` and Blender's `//`
                prefix resolve exactly as they do when render_scene writes, so the text that
                wrote a frame reads it back; source_path reports the resolved path.
            frame: Frame number the in-memory Render Result must hold, checked against the
                frame render_scene recorded rendering into it; refused when nothing recorded
                it. Only checked when output_path is omitted.
            max_size: Maximum size in pixels for the largest dimension of the saved copy.
            format: Image format for the saved copy (png, jpg, etc.)
            inline: Return the image bytes in the reply instead of writing to filepath.

        Returns:
            dict: success status with width/height/native dimensions, source, source_path,
            scene and frame, plus "warnings" when Render Result's origin is unknown, carrying
            the image bytes instead of a path when `inline`.

        """
        with image_destination(inline, filepath) as path:
            return finalize_image_reply(
                self._copy_render_output(path, output_path, frame, max_size, format), inline=inline, path=path
            )

    @staticmethod
    def _copy_render_output(path, output_path, frame, max_size, format):
        """
        Write a bounded copy of the requested render to `path`.

        Args:
            path: Destination the copy is written to.
            output_path: Path to an existing rendered file on disk.
            frame: Frame the in-memory Render Result must hold, when used.
            max_size: Maximum size in pixels for the largest dimension.
            format: Image format for the saved copy (png, jpg, etc.)

        Returns:
            dict: success status with dimensions, source, source_path, scene, frame, and
            "warnings" when Render Result's origin is unknown.

        Raises:
            ValueError: If the operation cannot be completed.
            RuntimeError: If the operation cannot be completed.

        """
        staging_path = None
        scene_name = None
        warnings = []
        resolved_output_path = _resolved_path(output_path) if output_path else None
        source_path = resolved_output_path
        if resolved_output_path:
            if not os.path.isfile(resolved_output_path):
                raise ValueError(f"Render output file not found: {resolved_output_path}")
            source = "output_path"
            # The caller's own `frame` is documented as ignored here, so a narration of "here is
            # frame 24" is only backed by what Blender actually encoded in the filename.
            frame = _frame_from_filename(resolved_output_path)
            img = bpy.data.images.load(resolved_output_path, check_existing=False)
        else:
            render_result = bpy.data.images.get("Render Result")
            if render_result is None:
                raise RuntimeError("No render result available; render a frame first with render_scene")
            # The playhead is no witness: render_scene puts it back after rendering, and a render
            # from anywhere else never moved it.
            record = render_result_record()
            if record is None:
                if frame is not None:
                    raise ValueError(
                        f"Render Result's origin is unknown - render_scene did not render what it holds - "
                        f"so it cannot be confirmed to hold frame {frame}; pass output_path to inspect "
                        "a specific previously-written frame instead"
                    )
                warnings.append(
                    "Render Result's origin is unknown: render_scene did not render what it holds (a render "
                    "from Blender's UI or another tool did, or the file changed since), so its scene and frame "
                    "are not reported. Pass output_path=<render_scene's last_file> to inspect a known frame."
                )
            else:
                if frame is not None and frame != record["frame"]:
                    raise ValueError(
                        f"Render Result holds frame {record['frame']} of scene '{record['scene']}', not {frame}; "
                        "pass output_path to inspect a specific previously-written frame instead"
                    )
                frame = record["frame"]
                scene_name = record["scene"]
                source_path = record["output_path"]
            source = "render_result"
            staging_path = f"{path}.src.png"
            render_result.save_render(filepath=staging_path)
            img = bpy.data.images.load(staging_path, check_existing=False)

        try:
            native_width, native_height = img.size
            width, height = native_width, native_height
            if max(width, height) > max_size:
                scale = max_size / max(width, height)
                width, height = max(1, int(width * scale)), max(1, int(height * scale))
                img.scale(width, height)
            img.filepath_raw = path
            img.file_format = format.upper()
            img.save()
        finally:
            bpy.data.images.remove(img)
            if staging_path and os.path.exists(staging_path):
                os.remove(staging_path)

        reply = {
            "success": True,
            "width": width,
            "height": height,
            "native_width": native_width,
            "native_height": native_height,
            "filepath": path,
            "source": source,
            "source_path": source_path,
            "scene": scene_name,
            "frame": frame,
        }
        if warnings:
            reply["warnings"] = warnings
        return reply
