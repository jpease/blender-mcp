"""Blender-main-thread handlers for character cloth setup."""

from __future__ import annotations

import contextlib
import math

import bpy

from ...helpers import sync_from_editmode
from ..rna_patch import finite, get_object, patch_rna, restore_rna, validate_rna_value
from ..simulation_cache import set_cache_frame_range
from ._deform_binding import (
    _move_modifier_immediately_after,
    _move_modifier_immediately_before,
    _restore_attachment_modifier,
    _snapshot_attachment_modifier,
)
from ._geometry_sampling import _evaluated_bvh_overlap
from ._ownership import _tag_owned_component, _tag_owned_membership
from .inspection_and_setup import (
    _COLLIDER_FIELDS,
    _SOLVER_FIELDS,
    _animation_info,
    _cache_info,
    _collection_in_scene,
    _modifier_info,
    _scene_context_for_object,
    _tag_update,
    _vertex_group_stats,
)


class ClothCharacterSetupHandlers:
    """Blender-main-thread handlers for character cloth setup."""

    def create_character_cloth_setup(
        self,
        garment_object_name,
        armature_object_name,
        body_collider_object_names,
        pin_group_name,
        collision_collection_name,
        cloth_modifier_name="Cloth",
        armature_modifier_name="Cloth Armature",
        collider_modifier_name="Cloth Collision",
        subdivision_modifier_name="Cloth Subdivision",
        solidify_modifier_name="Cloth Solidify",
        existing_policy="ERROR",
        material=None,
        solver=None,
        collisions=None,
        collider_settings=None,
        add_subdivision=False,
        subdivision_levels=1,
        add_solidify=False,
        solidify_thickness=0.002,
        rest_frame=1,
        cache_frame_start=1,
        cache_frame_end=250,
    ):
        garment = get_object(garment_object_name, {"MESH"})
        sync_from_editmode(garment)
        armature = get_object(armature_object_name, {"ARMATURE"})
        if not body_collider_object_names:
            raise ValueError("At least one explicit body collider is required")
        if len(body_collider_object_names) > 64 or len(set(body_collider_object_names)) != len(
            body_collider_object_names
        ):
            raise ValueError("body_collider_object_names must contain 1-64 unique names")
        colliders = [get_object(name, {"MESH", "CURVE"}) for name in body_collider_object_names]
        if garment in colliders or armature in colliders:
            raise ValueError("Garment, armature, and collider objects must be distinct")
        if existing_policy not in {"ERROR", "REUSE"}:
            raise ValueError("existing_policy must be ERROR or REUSE")
        if cache_frame_start > cache_frame_end:
            raise ValueError("cache_frame_start must be <= cache_frame_end")
        if not cache_frame_start <= rest_frame <= cache_frame_end:
            raise ValueError("rest_frame must be inside the explicit cache frame range")
        if not 0 <= subdivision_levels <= 6:
            raise ValueError("subdivision_levels must be in [0, 6]")
        finite(solidify_thickness, "solidify_thickness")
        if solidify_thickness <= 0:
            raise ValueError("solidify_thickness must be positive")
        pin_group = garment.vertex_groups.get(pin_group_name)
        if pin_group is None:
            raise ValueError(f"Pin vertex group not found: {pin_group_name}")
        pin_stats = _vertex_group_stats(garment, pin_group)
        if not pin_stats["nonzero"]:
            raise ValueError(f"Pin vertex group '{pin_group_name}' has no nonzero weights")
        collection = bpy.data.collections.get(collision_collection_name)
        if collection is None:
            raise ValueError(f"Collection not found: {collision_collection_name}")
        scene, view_layer = _scene_context_for_object(garment)
        if not _collection_in_scene(collection, scene):
            raise ValueError(f"Collection '{collection.name}' is not linked to scene '{scene.name}'")
        for dependency in [armature, *colliders]:
            if dependency.name not in scene.objects:
                raise ValueError(f"Dependency '{dependency.name}' is not linked to garment scene '{scene.name}'")
        if any(not math.isfinite(value) or value == 0 for value in armature.scale):
            raise ValueError("Armature scale must be finite and nonzero")
        for collider in colliders:
            if collider.type == "MESH":
                sync_from_editmode(collider)
            evaluated = collider.evaluated_get(view_layer.depsgraph)
            evaluated_mesh = evaluated.to_mesh()
            try:
                counts = {
                    "vertices": len(evaluated_mesh.vertices),
                    "faces": len(evaluated_mesh.polygons),
                }
            finally:
                evaluated.to_mesh_clear()
            if not counts["vertices"] or not counts["faces"]:
                raise ValueError(f"Collider '{collider.name}' must evaluate to a nonempty surface")

        collision_patch = dict(collisions or {})
        requested_collection = collision_patch.get("collection_name")
        if requested_collection and requested_collection != collision_collection_name:
            raise ValueError("Collision patch collection_name conflicts with collision_collection_name")
        if collision_patch.get("clear_collection"):
            raise ValueError("Character cloth setup cannot clear its explicit collision collection")
        collision_patch["collection_name"] = collision_collection_name
        collision_patch.setdefault("use_collision", True)
        collider_patch = dict(collider_settings or {})
        if collider_patch.get("use") is False:
            raise ValueError("Character cloth colliders must remain enabled")
        collider_patch["use"] = True

        created_modifiers = []
        ownership_records = []
        created_links = []
        membership_records = []
        existing_modifier_snapshots = []
        cloth_changes = {"material": {}, "solver": {}, "collisions": {}}
        collider_changes = []
        cloth_created = False
        original_frame = scene.frame_current
        original_subframe = scene.frame_subframe

        def resolve_modifier(obj, name, modifier_type):
            existing = obj.modifiers.get(name)
            if existing is not None:
                if existing.type != modifier_type:
                    raise ValueError(f"Modifier '{name}' on '{obj.name}' is {existing.type}, not {modifier_type}")
                if existing_policy == "ERROR":
                    raise ValueError(f"Modifier '{name}' already exists on '{obj.name}'")
                return existing, False
            modifier = obj.modifiers.new(name=name, type=modifier_type)
            created_modifiers.append((obj, modifier))
            return modifier, True

        try:
            armature_modifier, armature_created = resolve_modifier(garment, armature_modifier_name, "ARMATURE")
            if not armature_created:
                existing_modifier_snapshots.append(
                    (
                        garment,
                        armature_modifier,
                        list(garment.modifiers).index(armature_modifier),
                        _snapshot_attachment_modifier(armature_modifier),
                    )
                )
            armature_modifier.object = armature
            armature_modifier.use_vertex_groups = True

            cloth_modifier, cloth_created = resolve_modifier(garment, cloth_modifier_name, "CLOTH")
            bpy.context.view_layer.update()
            if cloth_modifier.settings is None or cloth_modifier.collision_settings is None:
                raise RuntimeError("Blender did not initialize Cloth settings")
            if cloth_modifier.point_cache.is_baked:
                raise ValueError("Cannot assemble a character setup around a baked cloth cache")
            old_pin_group = cloth_modifier.settings.vertex_group_mass
            old_cache_range = (cloth_modifier.point_cache.frame_start, cloth_modifier.point_cache.frame_end)
            old_collision_collection = cloth_modifier.collision_settings.collection
            if not cloth_created:
                existing_modifier_snapshots.append(
                    (garment, cloth_modifier, list(garment.modifiers).index(cloth_modifier), None)
                )

            _move_modifier_immediately_before(garment, armature_modifier, cloth_modifier)
            cloth_modifier.settings.vertex_group_mass = pin_group_name
            cloth_changes["material"] = self._configure_material(garment, cloth_modifier, material, None)
            cloth_changes["solver"] = patch_rna(cloth_modifier.settings, solver or {}, _SOLVER_FIELDS)
            cloth_changes["collisions"] = self._configure_collisions(garment, cloth_modifier, collision_patch)
            for field, value in (
                ("frame_start", cache_frame_start),
                ("frame_end", cache_frame_end),
            ):
                validate_rna_value(cloth_modifier.point_cache, field, value)
            set_cache_frame_range(cloth_modifier.point_cache, cache_frame_start, cache_frame_end)

            collider_records = []
            for collider in colliders:
                collision_modifier, collision_created = resolve_modifier(collider, collider_modifier_name, "COLLISION")
                bpy.context.view_layer.update()
                if collider.collision is None:
                    raise RuntimeError(f"Blender did not initialize collision settings for '{collider.name}'")
                if not collision_created:
                    existing_modifier_snapshots.append(
                        (collider, collision_modifier, list(collider.modifiers).index(collision_modifier), None)
                    )
                changes = patch_rna(collider.collision, collider_patch, _COLLIDER_FIELDS)
                collider_changes.append((collider, changes))
                membership = None
                if collider.name not in collection.objects:
                    collection.objects.link(collider)
                    created_links.append((collection, collider))
                    membership = _tag_owned_membership(collider, collection)
                    membership_records.append((collider, membership))
                if collision_created:
                    ownership = _tag_owned_component(collider, collision_modifier, "collider")
                    ownership_records.append((collider, ownership))
                collider_records.append(
                    {
                        "object": collider.name,
                        "modifier": collision_modifier.name,
                        "modifier_created": collision_created,
                        "collection_membership_created": membership is not None,
                        "settings_changes": changes,
                    }
                )

            subdivision_modifier = None
            if add_subdivision:
                subdivision_modifier, subdivision_created = resolve_modifier(
                    garment, subdivision_modifier_name, "SUBSURF"
                )
                if not subdivision_created:
                    existing_modifier_snapshots.append(
                        (
                            garment,
                            subdivision_modifier,
                            list(garment.modifiers).index(subdivision_modifier),
                            {
                                "levels": subdivision_modifier.levels,
                                "render_levels": subdivision_modifier.render_levels,
                            },
                        )
                    )
                subdivision_modifier.levels = subdivision_levels
                subdivision_modifier.render_levels = subdivision_levels
                _move_modifier_immediately_after(garment, subdivision_modifier, cloth_modifier)

            solidify_modifier = None
            if add_solidify:
                solidify_modifier, solidify_created = resolve_modifier(garment, solidify_modifier_name, "SOLIDIFY")
                if not solidify_created:
                    existing_modifier_snapshots.append(
                        (
                            garment,
                            solidify_modifier,
                            list(garment.modifiers).index(solidify_modifier),
                            {"thickness": solidify_modifier.thickness},
                        )
                    )
                solidify_modifier.thickness = solidify_thickness
                _move_modifier_immediately_after(garment, solidify_modifier, subdivision_modifier or cloth_modifier)

            if cloth_created:
                ownership = _tag_owned_component(garment, cloth_modifier, "cloth")
                ownership_records.append((garment, ownership))
            if armature_created:
                ownership = _tag_owned_component(garment, armature_modifier, "attachment")
                ownership_records.append((garment, ownership))
            for obj, modifier in created_modifiers:
                if obj == garment and modifier in {subdivision_modifier, solidify_modifier}:
                    ownership = _tag_owned_component(obj, modifier, "render_finish")
                    ownership_records.append((obj, ownership))

            scene.frame_set(rest_frame)
            view_layer.update()
            intersection_evidence = [
                {
                    "collider": collider.name,
                    **_evaluated_bvh_overlap(garment, collider, view_layer.depsgraph),
                }
                for collider in colliders
            ]
            _tag_update(garment)
        except Exception:
            for obj, record in reversed(ownership_records + membership_records):
                with contextlib.suppress(Exception):
                    del obj[record["object_property"]]
            for linked_collection, linked_object in reversed(created_links):
                with contextlib.suppress(Exception):
                    linked_collection.objects.unlink(linked_object)
            for collider, changes in reversed(collider_changes):
                restore_rna(collider.collision, changes)
            if "cloth_modifier" in locals() and not cloth_created:
                restore_rna(cloth_modifier.settings, cloth_changes["material"])
                restore_rna(cloth_modifier.settings, cloth_changes["solver"])
                restore_rna(cloth_modifier.collision_settings, cloth_changes["collisions"])
                cloth_modifier.settings.vertex_group_mass = old_pin_group
                cloth_modifier.collision_settings.collection = old_collision_collection
                set_cache_frame_range(cloth_modifier.point_cache, *old_cache_range)
            for obj, modifier, original_index, snapshot in reversed(existing_modifier_snapshots):
                if snapshot:
                    if modifier.type in {"HOOK", "ARMATURE", "MESH_DEFORM", "SURFACE_DEFORM"}:
                        _restore_attachment_modifier(modifier, snapshot)
                    else:
                        for name, value in snapshot.items():
                            with contextlib.suppress(Exception):
                                setattr(modifier, name, value)
                with contextlib.suppress(Exception):
                    obj.modifiers.move(list(obj.modifiers).index(modifier), original_index)
            for obj, modifier in reversed(created_modifiers):
                with contextlib.suppress(Exception):
                    obj.modifiers.remove(modifier)
            raise
        finally:
            scene.frame_set(original_frame, subframe=original_subframe)
            view_layer.update()

        recommended_frames = sorted(
            {
                cache_frame_start,
                cache_frame_start + (cache_frame_end - cache_frame_start) // 4,
                cache_frame_start + (cache_frame_end - cache_frame_start) // 2,
                cache_frame_start + 3 * (cache_frame_end - cache_frame_start) // 4,
                cache_frame_end,
            }
        )
        intersection_warnings = [
            item["collider"]
            for item in intersection_evidence
            if item.get("checked") and item.get("overlapping_face_pairs", 0)
        ]
        return {
            "changed_objects": [garment.name, *[collider.name for collider in colliders]],
            "garment": garment.name,
            "armature": armature.name,
            "cloth_modifier": _modifier_info(garment, cloth_modifier),
            "armature_modifier": _modifier_info(garment, armature_modifier),
            "pin_group": pin_stats,
            "collision_collection": collection.name,
            "colliders": collider_records,
            "render_modifiers": {
                "subdivision": _modifier_info(garment, subdivision_modifier) if subdivision_modifier else None,
                "solidify": _modifier_info(garment, solidify_modifier) if solidify_modifier else None,
            },
            "cloth_changes": cloth_changes,
            "point_cache": _cache_info(cloth_modifier.point_cache),
            "rest_frame": rest_frame,
            "rest_frame_intersections": intersection_evidence,
            "armature_scale": list(armature.scale),
            "animation": {"garment": _animation_info(garment), "armature": _animation_info(armature)},
            "dependency_graph": {
                "armature_before_cloth": True,
                "cloth_before_render_finishing": True,
                "live_assets_preserved": True,
            },
            "recommended_test_frames": recommended_frames,
            "ownership": [record for _obj, record in ownership_records + membership_records],
            "warnings": [
                *self._scale_warnings(garment),
                *self._scale_warnings(armature),
                *(
                    [f"Rest-frame evaluated meshes overlap colliders: {intersection_warnings}"]
                    if intersection_warnings
                    else []
                ),
                "Rest-frame overlap is a bounded structural check; review representative animated "
                "frames before production baking.",
            ],
        }
