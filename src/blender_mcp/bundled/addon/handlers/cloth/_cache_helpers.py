"""Shared point-cache helpers for cloth handlers."""

from __future__ import annotations

import json
import os
import uuid

import bpy

from ...helpers import preserve_mode_and_selection, set_active
from ..simulation_cache import point_cache_identity
from .inspection_and_setup import _cache_info, _collection_in_scene, _object_scenes, _scene_context_for_object


def _shared_cache_identity(cache):
    """Return only an explicit cache identity that can collide across modifiers."""
    return point_cache_identity(cache)


def _external_cache_path_status(cache):
    resolved = bpy.path.abspath(cache.filepath) if cache.filepath else ""
    return {
        "filepath": cache.filepath,
        "resolved": resolved,
        "valid_directory": bool(resolved and os.path.isdir(resolved)),
    }


def _external_directory_evidence(filepath):
    resolved = bpy.path.abspath(filepath) if filepath else ""
    exists = bool(resolved and os.path.isdir(resolved))
    entries = []
    truncated = False
    if exists:
        with os.scandir(resolved) as iterator:
            for entry in iterator:
                entries.append(entry.name)
                if len(entries) >= 100:
                    truncated = True
                    break
    return {
        "filepath": filepath,
        "resolved": resolved,
        "exists": exists,
        "writable": bool(exists and os.access(resolved, os.W_OK)),
        "entries": entries,
        "entries_truncated": truncated,
    }


def _all_cloth_caches():
    return [
        (obj, modifier, modifier.point_cache)
        for obj in bpy.data.objects
        for modifier in obj.modifiers
        if modifier.type == "CLOTH"
    ]


def _cloth_cache_dependency_issues(obj, modifier):
    issues = []
    if not obj.data.vertices or not obj.data.edges:
        issues.append("cloth mesh has empty vertex or edge topology")
    settings = modifier.settings
    collisions = modifier.collision_settings
    for field in (
        "vertex_group_mass",
        "vertex_group_structural_stiffness",
        "vertex_group_shear_stiffness",
        "vertex_group_bending",
        "vertex_group_shrink",
        "vertex_group_pressure",
        "vertex_group_intern",
    ):
        group_name = getattr(settings, field, "")
        if group_name and obj.vertex_groups.get(group_name) is None:
            issues.append(f"missing vertex group {field}='{group_name}'")
    for field in ("vertex_group_object_collisions", "vertex_group_self_collisions"):
        group_name = getattr(collisions, field, "")
        if group_name and obj.vertex_groups.get(group_name) is None:
            issues.append(f"missing collision vertex group {field}='{group_name}'")
    scenes = _object_scenes(obj)
    if collisions.collection and not any(_collection_in_scene(collisions.collection, scene) for scene in scenes):
        issues.append(f"collision collection '{collisions.collection.name}' is not linked to a cloth scene")
    effector_collection = settings.effector_weights.collection
    if effector_collection and not any(_collection_in_scene(effector_collection, scene) for scene in scenes):
        issues.append(f"effector collection '{effector_collection.name}' is not linked to a cloth scene")
    return issues


def _prospective_cache_identity(cache, patch):
    use_external = patch.get("use_external", cache.use_external)
    filepath = patch.get("filepath", cache.filepath)
    if not use_external or not filepath:
        return None
    name = patch.get("name", cache.name)
    index = patch.get("index", cache.index)
    return (
        "EXTERNAL",
        os.path.normcase(os.path.normpath(bpy.path.abspath(filepath))),
        str(name),
        int(index),
    )


def _point_cache_context(obj, cache):
    scene, view_layer = _scene_context_for_object(obj)
    return (
        scene,
        view_layer,
        {
            "scene": scene,
            "view_layer": view_layer,
            "object": obj,
            "active_object": obj,
            "selected_objects": [obj],
            "selected_editable_objects": [obj],
            "point_cache": cache,
        },
    )


def _run_point_cache_operator(obj, cache, operator, **kwargs):
    _scene, _view_layer, override = _point_cache_context(obj, cache)
    with preserve_mode_and_selection():
        set_active(obj)
        if obj.mode != "OBJECT":
            result = bpy.ops.object.mode_set(mode="OBJECT")
            if "FINISHED" not in result:
                raise RuntimeError(f"Could not enter Object Mode for point cache: {sorted(result)}")
        with bpy.context.temp_override(**override):
            result = operator(**kwargs)
    if "FINISHED" not in result:
        raise RuntimeError(
            f"Point-cache operator did not finish: {sorted(result)}; current state={json.dumps(_cache_info(cache))}"
        )
    return result


def _configure_independent_cache(
    cache,
    object_name,
    modifier_name,
    cache_directory=None,
    index=0,
    identity_token=None,
):
    token = identity_token or uuid.uuid4().hex[:8]
    cache.name = f"{object_name[:30]}_{modifier_name[:16]}_{token[:8]}"
    cache.index = index
    if cache_directory:
        resolved = bpy.path.abspath(cache_directory)
        if not os.path.isdir(resolved) or not os.access(resolved, os.W_OK):
            raise ValueError(f"cache_directory must be an existing writable directory: {cache_directory}")
        cache.use_external = True
        cache.filepath = cache_directory
    else:
        cache.use_external = False
        cache.use_disk_cache = False
        cache.filepath = ""
