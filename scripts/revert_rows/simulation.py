"""
Rows guarding the simulation handlers' shared helpers, and the replies that count what they touched.

Cloth, liquid and rigid bodies each carried a copy of the RNA patch helpers and the cache
frame-range write, and the copies drifted; each of those rows reverts the shared version to the
copy it replaced. The reply rows revert a count back to the full list it replaced, or
`changed_objects` back to naming every member of a setup.

Label prefix: `simulation:`.
"""

from .common import (
    ADDON_CLOTH_VARIANTS,
    ADDON_LIQUID_DELIVERY,
    ADDON_LIQUID_INSPECTION,
    ADDON_RIGID_BODY_INSPECTION,
    ADDON_RIGID_BODY_LIFECYCLE,
    ADDON_RIGID_BODY_SIMULATION,
    ADDON_RNA_PATCH,
    ADDON_SIMULATION_CACHE,
    CLOTHT,
    LIQUIDT,
    RBWT,
    RNAPT,
    Revert,
)

_LIQUID_RANGE_REFUSED = f"{LIQUIDT}::test_manage_liquid_cache_refuses_and_undoes_a_frame_range_blender_did_not_keep"
_RIGID_RANGE_REFUSED = f"{RBWT}::test_manage_rigid_body_cache_refuses_and_undoes_a_frame_range_blender_did_not_keep"
_RIGID_CACHE_COUNTED = f"{RBWT}::test_a_cache_calculation_counts_its_bodies_and_names_none_of_them_as_changed"
_RIGID_HELPERS_COUNTED = f"{RBWT}::test_removing_a_rigs_helpers_counts_them_and_names_none_as_a_next_target"
_CLOTH_VARIANT_COUNTED = f"{CLOTHT}::test_a_cloth_variant_counts_its_setup_and_names_the_variant"
_LIQUID_VARIANT_COUNTED = f"{LIQUIDT}::test_a_liquid_variant_counts_members_by_role_and_names_its_domain"

ROWS: list[Revert] = [
    # --- one patch/restore/serialize helper set for every simulation domain -------------------
    Revert(
        # Liquid's copy: the recorded None or name is written over the pointer the caller holds.
        "simulation: restore writes a pointer's reply-form old value back over the datablock",
        ADDON_RNA_PATCH,
        "        prop = owner.bl_rna.properties.get(name)\n"
        '        if prop is not None and prop.type == "POINTER":\n'
        "            continue\n"
        "        with contextlib.suppress(Exception):\n",
        "        with contextlib.suppress(Exception):\n",
        (f"{RNAPT}::test_restore_leaves_a_pointer_to_the_caller_that_holds_its_datablock",),
    ),
    Revert(
        # Rigid body's copy: a mathutils value has no `__iter__`, so it fell through to `str`.
        "simulation: only a value with __iter__ is iterated, so a vector reads back as its repr",
        ADDON_RNA_PATCH,
        "    try:\n"
        "        return [serialize(item, typed_ids=typed_ids) for item in value]\n"
        "    except TypeError:\n"
        "        return str(value)\n",
        '    if hasattr(value, "__iter__"):\n'
        "        with contextlib.suppress(TypeError):\n"
        "            return [serialize(item, typed_ids=typed_ids) for item in value]\n"
        "    return str(value)\n",
        (f"{RNAPT}::test_a_vector_read_back_is_its_numbers_not_its_repr",),
    ),
    # --- a cache frame range Blender did not keep is refused --------------------------------
    Revert(
        "simulation: the shared cache frame-range write stops reading the range back",
        ADDON_SIMULATION_CACHE,
        "    if kept != (frame_start, frame_end):\n",
        "    if False:\n",
        (_LIQUID_RANGE_REFUSED, _RIGID_RANGE_REFUSED),
    ),
    Revert(
        # Liquid's copy wrote the range in the right order and never read it back.
        "simulation: a liquid cache range is written without reading it back",
        ADDON_LIQUID_INSPECTION,
        '    set_cache_frame_range(settings, start, end, start_name="cache_frame_start", end_name="cache_frame_end")\n',
        "    if start > settings.cache_frame_end:\n"
        "        settings.cache_frame_end = end\n"
        "        settings.cache_frame_start = start\n"
        "    else:\n"
        "        settings.cache_frame_start = start\n"
        "        settings.cache_frame_end = end\n",
        (_LIQUID_RANGE_REFUSED,),
    ),
    Revert(
        # Rigid body's copy read the range back but raised a fault, which the add-on logs with a
        # traceback, for what is the client's request being refused.
        "simulation: a rigid-body cache range Blender did not keep is raised as a fault, not refused",
        ADDON_RIGID_BODY_INSPECTION,
        "    set_cache_frame_range(cache, start, end)\n",
        "    if start > cache.frame_end:\n"
        "        cache.frame_end = end\n"
        "        cache.frame_start = start\n"
        "    else:\n"
        "        cache.frame_start = start\n"
        "        cache.frame_end = end\n"
        "    if (cache.frame_start, cache.frame_end) != (start, end):\n"
        '        raise RuntimeError("Blender did not retain the requested rigid-body cache range")\n',
        (_RIGID_RANGE_REFUSED,),
    ),
    # --- a reply counts the bodies, helpers and duplicates it touched -------------------------
    Revert(
        "simulation: a rigid-body cache operation names every body as changed, and counts none",
        ADDON_RIGID_BODY_SIMULATION,
        '            "changed_objects": [],\n'
        '            "changed_resources": [scene.name],\n'
        '            "scene": scene.name,\n'
        '            "action": action,\n'
        '            "simulated_objects": counted_page(\n'
        "                [obj for obj in scene.objects if obj.rigid_body is not None],\n"
        "                type_of=lambda obj: obj.type,\n"
        "                name_of=lambda obj: obj.name,\n"
        "            ),\n",
        '            "changed_objects": [obj.name for obj in scene.objects if obj.rigid_body is not None],\n'
        '            "changed_resources": [scene.name],\n'
        '            "scene": scene.name,\n'
        '            "action": action,\n',
        (_RIGID_CACHE_COUNTED,),
    ),
    Revert(
        "simulation: removing a rig's tagged helpers lists every one, as removed and as changed",
        ADDON_RIGID_BODY_LIFECYCLE,
        "            removed = _removed_page(candidates, _object_type)\n"
        "            for obj in candidates:\n"
        "                bpy.data.objects.remove(obj, do_unlink=True)\n"
        "            return {\n"
        '                "changed_objects": [],\n',
        "            removed = [obj.name for obj in candidates]\n"
        "            for obj in candidates:\n"
        "                bpy.data.objects.remove(obj, do_unlink=True)\n"
        "            return {\n"
        '                "changed_objects": removed,\n',
        (_RIGID_HELPERS_COUNTED,),
    ),
    Revert(
        "simulation: a cloth variant names every duplicate and every copied datablock as changed",
        ADDON_CLOTH_VARIANTS,
        '            "changed_objects": [variant.name],\n'
        '            "changed_resources": [collection.name for collection in created_collections],\n',
        '            "changed_objects": sorted(obj.name for obj, _data, _materials, _actions in created),\n'
        '            "changed_resources": [\n'
        "                *[collection.name for collection in created_collections],\n"
        "                *[datablock.name for datablock in copied_datablocks],\n"
        "            ],\n",
        (_CLOTH_VARIANT_COUNTED,),
    ),
    Revert(
        "simulation: a cloth variant lists every ownership record it tagged",
        ADDON_CLOTH_VARIANTS,
        '            "ownership": _ownership_page(ownership),\n',
        '            "ownership": [record for _owner, record in ownership],\n',
        (_CLOTH_VARIANT_COUNTED,),
    ),
    Revert(
        "simulation: a cloth variant lists every collider it found",
        ADDON_CLOTH_VARIANTS,
        '                "colliders": _object_page(colliders.values()),\n',
        '                "colliders": sorted(colliders),\n',
        (_CLOTH_VARIANT_COUNTED,),
    ),
    Revert(
        "simulation: a liquid variant names every duplicated member as changed",
        ADDON_LIQUID_DELIVERY,
        '            "changed_objects": [variant.name, *([source.name] if disabled == source.name else [])],\n',
        '            "changed_objects": [item.name for item in mapping.values()]\n'
        "            + ([source.name] if disabled == source.name else []),\n",
        (_LIQUID_VARIANT_COUNTED,),
    ),
    Revert(
        "simulation: a liquid variant maps every member to its duplicate",
        ADDON_LIQUID_DELIVERY,
        '            "variant_objects": _role_page(variant_objects),\n',
        '            "object_mapping": {original.name: duplicate.name for original, duplicate in mapping.items()},\n',
        (_LIQUID_VARIANT_COUNTED,),
    ),
    Revert(
        "simulation: a liquid variant maps every animated duplicate to its action",
        ADDON_LIQUID_DELIVERY,
        '            "animation_actions": counted_page(\n'
        "                list(dict.fromkeys(duplicate.animation_data.action for duplicate in animated)),\n"
        "                type_of=lambda action: action.id_type,\n"
        "                name_of=lambda action: action.name,\n"
        "            ),\n",
        '            "animation_actions": {\n'
        "                duplicate.name: duplicate.animation_data.action.name for duplicate in animated\n"
        "            },\n",
        (_LIQUID_VARIANT_COUNTED,),
    ),
]
