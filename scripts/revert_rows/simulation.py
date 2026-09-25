"""
Rows guarding the RNA patch helpers and the cache frame-range write the simulation handlers share.

Cloth, liquid and rigid bodies each carried a copy of these, and the copies drifted; each row
reverts the shared version to the copy it replaced.

Label prefix: `simulation:`.
"""

from .common import (
    ADDON_LIQUID_INSPECTION,
    ADDON_RIGID_BODY_INSPECTION,
    ADDON_RNA_PATCH,
    ADDON_SIMULATION_CACHE,
    LIQUIDT,
    RBWT,
    RNAPT,
    Revert,
)

_LIQUID_RANGE_REFUSED = f"{LIQUIDT}::test_manage_liquid_cache_refuses_and_undoes_a_frame_range_blender_did_not_keep"
_RIGID_RANGE_REFUSED = f"{RBWT}::test_manage_rigid_body_cache_refuses_and_undoes_a_frame_range_blender_did_not_keep"

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
]
