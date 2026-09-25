"""Blender-main-thread handlers for duplicating cloth setup variants."""

from __future__ import annotations

import contextlib
import os
import uuid

import bpy

from ...helpers import sync_from_editmode
from ..rna_patch import get_object
from ._cache_helpers import _configure_independent_cache, _shared_cache_identity
from ._deform_binding import _bind_deform_modifier, _unbind_deform_modifier
from ._ownership import _tag_owned_component, _tag_owned_object
from .inspection_and_setup import _OWNERSHIP_PREFIX, _cache_info, _scene_context_for_object
from .proxy_rigs import (
    _modifier_dependency_target,
    _remove_created_object,
    _set_modifier_dependency_target,
    _validate_id_name,
)


def _duplicate_object(source, name, collection, *, copy_mesh, material_policy, animation_policy):
    duplicate = source.copy()
    duplicate.name = name
    copied_data = None
    copied_materials = []
    if source.data is not None and copy_mesh:
        copied_data = source.data.copy()
        duplicate.data = copied_data
        if source.type == "MESH" and material_policy == "COPY":
            copied_materials = _copy_mesh_materials(copied_data)
    elif material_policy == "COPY":
        raise ValueError("material_policy=COPY requires mesh_data_policy=COPY")
    copied_actions = []
    copied_action = _copy_animation_action(source, duplicate, animation_policy)
    if copied_action is not None and copied_action != getattr(getattr(source, "animation_data", None), "action", None):
        copied_actions.append(copied_action)
    if copied_data is not None:
        copied_actions.extend(_copy_data_actions(duplicate, animation_policy))
    collection.objects.link(duplicate)
    return duplicate, copied_data, copied_materials, copied_actions


def _copy_animation_action(source, duplicate, policy):
    animation = getattr(duplicate, "animation_data", None)
    action = getattr(animation, "action", None)
    if action is None:
        return None
    if policy == "COPY":
        copied = action.copy()
        copied.name = f"{source.name} Variant Action"
        animation.action = copied
        return copied
    return action


def _copy_data_actions(duplicate, policy):
    if policy != "COPY" or duplicate.data is None:
        return []
    copied = []
    owners = [duplicate.data, getattr(duplicate.data, "shape_keys", None)]
    for owner in owners:
        animation = getattr(owner, "animation_data", None)
        action = getattr(animation, "action", None)
        if action is None:
            continue
        action_copy = action.copy()
        action_copy.name = f"{action.name} Variant"
        animation.action = action_copy
        copied.append(action_copy)
    return copied


def _copy_mesh_materials(mesh):
    copied = []
    for index, material in enumerate(list(mesh.materials)):
        if material is None:
            continue
        duplicate = material.copy()
        duplicate.name = f"{material.name} Variant"
        mesh.materials[index] = duplicate
        copied.append(duplicate)
    return copied


class ClothVariantHandlers:
    """Blender-main-thread handlers for duplicating cloth setup variants."""

    def duplicate_cloth_setup_variant(
        self,
        source_object_name,
        variant_object_name,
        variant_collection_name,
        name_suffix,
        mesh_data_policy,
        material_policy,
        animation_policy,
        collider_policy,
        force_field_policy,
        render_surface_policy,
        cache_directory=None,
    ):
        source = get_object(source_object_name, {"MESH"})
        sync_from_editmode(source)
        _validate_id_name(variant_object_name, "variant_object_name")
        _validate_id_name(variant_collection_name, "variant_collection_name")
        cloth_modifiers = [modifier for modifier in source.modifiers if modifier.type == "CLOTH"]
        if not cloth_modifiers:
            raise ValueError(f"Object '{source.name}' has no Cloth modifier")
        if bpy.data.objects.get(variant_object_name) is not None:
            raise ValueError(f"Object already exists: {variant_object_name}")
        if bpy.data.collections.get(variant_collection_name) is not None:
            raise ValueError(f"Collection already exists: {variant_collection_name}")
        if not name_suffix or len(name_suffix) > 32:
            raise ValueError("name_suffix must contain 1-32 characters")
        if mesh_data_policy not in {"COPY", "SHARE"} or material_policy not in {"COPY", "SHARE"}:
            raise ValueError("Mesh and material policies must be COPY or SHARE")
        if animation_policy not in {"COPY", "SHARE"}:
            raise ValueError("animation_policy must be COPY or SHARE")
        if collider_policy not in {"DUPLICATE", "SHARE"} or force_field_policy not in {"DUPLICATE", "SHARE"}:
            raise ValueError("Collider and force-field policies must be DUPLICATE or SHARE")
        if render_surface_policy not in {"DUPLICATE", "OMIT"}:
            raise ValueError("render_surface_policy must be DUPLICATE or OMIT")
        if material_policy == "COPY" and mesh_data_policy != "COPY":
            raise ValueError("material_policy=COPY requires mesh_data_policy=COPY")
        if cache_directory:
            resolved = bpy.path.abspath(cache_directory)
            if not os.path.isdir(resolved) or not os.access(resolved, os.W_OK):
                raise ValueError(f"cache_directory must be an existing writable directory: {cache_directory}")
        scene, view_layer = _scene_context_for_object(source)

        colliders = {}
        effectors = {}
        for modifier in cloth_modifiers:
            collision_collection = modifier.collision_settings.collection
            if collision_collection:
                for obj in collision_collection.all_objects:
                    if any(item.type == "COLLISION" for item in obj.modifiers):
                        colliders[obj.name] = obj
            effector_collection = modifier.settings.effector_weights.collection
            if effector_collection:
                for obj in effector_collection.all_objects:
                    if getattr(getattr(obj, "field", None), "type", "NONE") != "NONE":
                        effectors[obj.name] = obj
        render_surfaces = {}
        for candidate in scene.objects:
            if candidate == source:
                continue
            for modifier in candidate.modifiers:
                if (
                    modifier.type in {"SURFACE_DEFORM", "MESH_DEFORM"}
                    and _modifier_dependency_target(modifier) == source
                ):
                    render_surfaces[candidate.name] = candidate
                    break
        duplicate_dependencies = []
        if collider_policy == "DUPLICATE":
            duplicate_dependencies.extend(colliders.values())
        if force_field_policy == "DUPLICATE":
            duplicate_dependencies.extend(effectors.values())
        if render_surface_policy == "DUPLICATE":
            duplicate_dependencies.extend(render_surfaces.values())
        duplicate_dependencies = list(dict.fromkeys(duplicate_dependencies))
        generated_names = [f"{obj.name}{name_suffix}" for obj in duplicate_dependencies]
        for generated_name in generated_names:
            _validate_id_name(generated_name, "generated dependency name")
        if len({variant_object_name, *generated_names}) != len(generated_names) + 1:
            raise ValueError("Variant and generated dependency object names must be unique")
        for label, enabled in (
            ("Colliders", collider_policy == "DUPLICATE" and bool(colliders)),
            ("Effectors", force_field_policy == "DUPLICATE" and bool(effectors)),
            ("Render Surfaces", render_surface_policy == "DUPLICATE" and bool(render_surfaces)),
        ):
            if enabled:
                child_name = _validate_id_name(f"{variant_collection_name} {label}", "variant child collection")
                if bpy.data.collections.get(child_name) is not None:
                    raise ValueError(f"Collection already exists: {child_name}")
        collisions = [name for name in generated_names if bpy.data.objects.get(name) is not None]
        if collisions:
            raise ValueError(f"Variant dependency object names already exist: {collisions}")

        root_collection = bpy.data.collections.new(variant_collection_name)
        scene.collection.children.link(root_collection)
        created_collections = [root_collection]
        created = []
        copied_materials = []
        copied_actions = []
        ownership = []
        source_map = {}
        simulation_id = uuid.uuid4().hex
        try:
            variant, data, materials, actions = _duplicate_object(
                source,
                variant_object_name,
                root_collection,
                copy_mesh=mesh_data_policy == "COPY",
                material_policy=material_policy,
                animation_policy=animation_policy,
            )
            created.append((variant, data, materials, actions))
            copied_materials.extend(materials)
            copied_actions.extend(actions)
            source_map[source.name] = variant.name
            for key in list(variant.keys()):
                if key.startswith(_OWNERSHIP_PREFIX):
                    del variant[key]
            variant_cloth = [modifier for modifier in variant.modifiers if modifier.type == "CLOTH"]
            for cache_index, modifier in enumerate(variant_cloth):
                if modifier.point_cache.is_baked or modifier.point_cache.is_baking:
                    raise ValueError("Copied Cloth modifier unexpectedly retained an active bake state")
                _configure_independent_cache(
                    modifier.point_cache,
                    variant.name,
                    modifier.name,
                    cache_directory,
                    cache_index,
                    simulation_id,
                )
                source_modifier = source.modifiers.get(modifier.name)
                if source_modifier is not None and source_modifier.type == "CLOTH":
                    if modifier.point_cache is source_modifier.point_cache:
                        raise RuntimeError("Variant Cloth modifier shares the source PointCache instance")
                    variant_identity = _shared_cache_identity(modifier.point_cache)
                    if variant_identity is not None and variant_identity == _shared_cache_identity(
                        source_modifier.point_cache
                    ):
                        raise RuntimeError("Variant Cloth modifier retained the source external cache identity")
                ownership.append(
                    (variant, _tag_owned_component(variant, modifier, "cloth_variant", simulation_id, source.name))
                )

            duplicate_map = {source.name: variant}

            def duplicate_group(objects, label):
                if not objects:
                    return None
                collection = bpy.data.collections.new(f"{variant_collection_name} {label}")
                root_collection.children.link(collection)
                created_collections.append(collection)
                for original in sorted(objects.values(), key=lambda item: item.name):
                    if original.name in duplicate_map:
                        duplicate = duplicate_map[original.name]
                        if duplicate.name not in collection.objects:
                            collection.objects.link(duplicate)
                        continue
                    duplicate, copied_data, materials, actions = _duplicate_object(
                        original,
                        f"{original.name}{name_suffix}",
                        collection,
                        copy_mesh=bool(original.data),
                        material_policy=material_policy if original.type == "MESH" else "SHARE",
                        animation_policy=animation_policy,
                    )
                    created.append((duplicate, copied_data, materials, actions))
                    copied_materials.extend(materials)
                    copied_actions.extend(actions)
                    for key in list(duplicate.keys()):
                        if key.startswith(_OWNERSHIP_PREFIX):
                            del duplicate[key]
                    duplicate_map[original.name] = duplicate
                    source_map[original.name] = duplicate.name
                return collection

            collider_collection = duplicate_group(colliders, "Colliders") if collider_policy == "DUPLICATE" else None
            effector_collection = duplicate_group(effectors, "Effectors") if force_field_policy == "DUPLICATE" else None
            duplicate_group(render_surfaces, "Render Surfaces") if render_surface_policy == "DUPLICATE" else None

            for modifier in variant_cloth:
                if collider_collection and modifier.collision_settings.collection is not None:
                    modifier.collision_settings.collection = collider_collection
                if effector_collection and modifier.settings.effector_weights.collection is not None:
                    modifier.settings.effector_weights.collection = effector_collection
            for original_name, duplicate in duplicate_map.items():
                original = bpy.data.objects.get(original_name)
                if original is None:
                    raise RuntimeError(f"Variant source object disappeared during duplication: {original_name}")
                for modifier in duplicate.modifiers:
                    target = _modifier_dependency_target(modifier)
                    target_name = target.name if target is not None else None
                    if target_name not in duplicate_map:
                        continue
                    if modifier.type in {"SURFACE_DEFORM", "MESH_DEFORM"} and modifier.is_bound:
                        _unbind_deform_modifier(duplicate, modifier)
                    _set_modifier_dependency_target(modifier, duplicate_map[target_name])
                    if modifier.type in {"SURFACE_DEFORM", "MESH_DEFORM"}:
                        _bind_deform_modifier(duplicate, modifier)
                if original != source:
                    role = (
                        "variant_render_surface"
                        if original in render_surfaces.values()
                        else "variant_collider"
                        if original in colliders.values()
                        else "variant_effector"
                    )
                    ownership.append(
                        (
                            duplicate,
                            _tag_owned_object(duplicate, role, simulation_id, original.name),
                        )
                    )
            view_layer.update()
        except Exception:
            for owner, record in reversed(ownership):
                with contextlib.suppress(Exception):
                    del owner[record["object_property"]]
            for obj, data, materials, actions in reversed(created):
                _remove_created_object(obj, data, materials, actions)
            for collection in reversed(created_collections):
                with contextlib.suppress(Exception):
                    bpy.data.collections.remove(collection)
            raise
        caches = [
            {"modifier": modifier.name, "point_cache": _cache_info(modifier.point_cache)}
            for modifier in variant.modifiers
            if modifier.type == "CLOTH"
        ]
        return {
            "changed_objects": sorted(source_map.values()),
            "changed_resources": list(
                dict.fromkeys(
                    [
                        *[collection.name for collection in created_collections],
                        *[data.name for _obj, data, _materials, _actions in created if data is not None],
                        *[material.name for material in copied_materials],
                        *[action.name for action in copied_actions],
                    ]
                )
            ),
            "source_object": source.name,
            "variant_object": variant.name,
            "variant_collection": root_collection.name,
            "simulation_id": simulation_id,
            "source_to_variant": source_map,
            "policies": {
                "mesh_data": mesh_data_policy,
                "materials": material_policy,
                "animation": animation_policy,
                "colliders": collider_policy,
                "force_fields": force_field_policy,
                "render_surfaces": render_surface_policy,
            },
            "dependencies": {
                "colliders": sorted(colliders),
                "force_fields": sorted(effectors),
                "render_surfaces": sorted(render_surfaces),
                "unremapped_attachment_targets": sorted(
                    {
                        target.name
                        for modifier in variant.modifiers
                        if modifier.type in {"HOOK", "ARMATURE", "MESH_DEFORM", "SURFACE_DEFORM"}
                        if (target := _modifier_dependency_target(modifier)) is not None
                        and target not in duplicate_map.values()
                    }
                ),
            },
            "point_caches": caches,
            "mesh_data_shared": variant.data == source.data,
            "shape_keys_shared": getattr(variant.data, "shape_keys", None) == getattr(source.data, "shape_keys", None),
            "ownership": [record for _owner, record in ownership],
            "warnings": ["Shared dependencies remain intentionally coupled to the source setup."]
            if "SHARE" in {collider_policy, force_field_policy, animation_policy, mesh_data_policy}
            else [],
        }
