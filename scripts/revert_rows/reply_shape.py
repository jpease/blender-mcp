"""
Rows guarding replies bounded to what was asked for.

The status summary, a library's pages, the envelope's change lists, the budget's page
recognition, and light records.

Label prefixes: `get_addon_status:`, `linking:`, `server tools:`, `reply budget:`,
`lighting:`.
"""

from .common import (
    ADDON_HELPERS,
    ADDON_LIGHTING_INSPECTION,
    ADDON_LIGHTING_RENDERING,
    ADDON_LIGHTING_SHARED,
    ADDON_LINKING,
    CORET,
    ENVT,
    LIGHTT,
    LKT,
    SERVER_CORE_TOOL,
    SERVER_ENVELOPE,
    SERVER_LIGHTING_INSPECTION_TOOL,
    SERVER_LIGHTING_RENDERING_TOOL,
    SFLT,
    Revert,
)

ROWS: list[Revert] = [
    # --- get_addon_status summarizes the capability list instead of shipping 291 names ---
    Revert(
        "get_addon_status: the command names ship on every status call, asked for or not",
        SERVER_CORE_TOOL,
        '    if detail:\n        payload["capabilities"] = result.capabilities\n',
        '    payload["capabilities"] = result.capabilities\n    if detail:\n',
        (f"{CORET}::test_get_addon_status_summarizes_the_capabilities_instead_of_listing_them",),
    ),
    Revert(
        "get_addon_status: detail is ignored, so the command names cannot be retrieved at all",
        SERVER_CORE_TOOL,
        "    if detail:\n",
        "    if False:\n",
        (f"{CORET}::test_get_addon_status_lists_the_command_names_only_on_request",),
    ),
    Revert(
        "get_addon_status: the machine's whole device list ships on every status call",
        SERVER_CORE_TOOL,
        '            else {key: value for key, value in result.render_devices.items() if key != "available_devices"}\n',
        "            else result.render_devices\n",
        (f"{CORET}::test_get_addon_status_reports_render_devices_with_the_machine_list_only_on_detail",),
    ),
    Revert(
        "get_addon_status: the capability count is hardcoded instead of counted",
        SERVER_CORE_TOOL,
        '        "capability_count": len(result.capabilities),\n',
        '        "capability_count": 0,\n',
        (
            f"{CORET}::test_get_addon_status_summarizes_the_capabilities_instead_of_listing_them",
            f"{CORET}::test_get_addon_status_lists_the_command_names_only_on_request",
        ),
    ),
    Revert(
        "get_addon_status: every optional integration is reported available",
        SERVER_CORE_TOOL,
        "            provider: command in result.capabilities for provider, command in "
        "_INTEGRATION_CAPABILITIES.items()\n",
        "            provider: True for provider, command in _INTEGRATION_CAPABILITIES.items()\n",
        (
            f"{CORET}::test_get_addon_status_summarizes_the_capabilities_instead_of_listing_them",
            f"{CORET}::test_get_addon_status_reports_an_addon_with_no_optional_integrations",
        ),
    ),
    # --- what a library links is counted by type; the names, then the records, are pages ---
    Revert(
        "linking: the datablock counts by type go away, leaving only how many there are",
        ADDON_HELPERS,
        (
            "    counted: dict[str, object] = {\n"
            '        "total": len(items),\n'
            '        "by_type": count_by_type(type_of(item) for item in items),\n'
            "    }\n"
        ),
        '    counted: dict[str, object] = {"total": len(items)}\n',
        (
            f"{LKT}::test_a_reload_reports_what_it_replaced_by_type_without_the_records[reload_library]",
            f"{LKT}::test_a_reload_reports_what_it_replaced_by_type_without_the_records[relocate_library]",
        ),
    ),
    Revert(
        "linking: the type counts keep the order the datablocks arrived in, so one mix reads two ways",
        ADDON_HELPERS,
        "    return dict(sorted(counts.items()))",
        "    return counts",
        (f"{LKT}::test_type_counts_are_ordered_by_type_whatever_order_the_datablocks_arrived_in",),
    ),
    Revert(
        "linking: a bounded sub-list offers an offset to resume from, which every one of these commands rejects",
        ADDON_HELPERS,
        '        "limit": limit,\n        "returned_count": len(shown),',
        '        "limit": limit,\n        "offset": 0,\n        "next_offset": len(shown),\n'
        '        "returned_count": len(shown),',
        tuple(
            f"{LKT}::test_a_truncated_datablock_page_offers_no_offset_to_resume_from[{case}]"
            for case in ("names", "records")
        ),
    ),
    Revert(
        "linking: the default datablock page carries the records, not the names",
        ADDON_HELPERS,
        '        return {**counted, **record_page("names", items, name_of, MAX_LISTED_NAMES)}\n',
        '        return {**counted, **record_page("records", items, describe, limit)}\n',
        (
            f"{LKT}::test_a_reload_reports_what_it_replaced_by_type_without_the_records[reload_library]",
            f"{LKT}::test_a_reload_reports_what_it_replaced_by_type_without_the_records[relocate_library]",
        ),
    ),
    Revert(
        "linking: detail is ignored, so a library's datablock records are unreachable",
        ADDON_HELPERS,
        '    if not detail:\n        return {**counted, **record_page("names", items, name_of, MAX_LISTED_NAMES)}\n',
        '    if True:\n        return {**counted, **record_page("names", items, name_of, MAX_LISTED_NAMES)}\n',
        (f"{LKT}::test_list_libraries_lists_the_datablock_records_only_on_request",),
    ),
    Revert(
        "linking: the default name page grows to the record cap, so a listing is ten times its size",
        ADDON_HELPERS,
        "MAX_LISTED_NAMES = 10\n",
        "MAX_LISTED_NAMES = 100\n",
        (f"{LKT}::test_list_libraries_bounds_the_datablocks_it_lists_per_library",),
    ),
    Revert(
        "linking: an override's objects page carries records by default, not a sample of names",
        ADDON_LINKING,
        "        describe=_override_entry,\n        limit=MAX_LISTED_DATABLOCKS,\n        detail=detail,\n",
        "        describe=_override_entry,\n        limit=MAX_LISTED_DATABLOCKS,\n        detail=True,\n",
        (f"{LKT}::test_create_override_counts_the_objects_it_made_with_a_sample_of_names",),
    ),
    Revert(
        "linking: nothing names the objects an override made, so changed_objects is empty",
        ADDON_LINKING,
        "    return _root_names(_distinct(obj for override in overrides for obj in override.all_objects))",
        "    return []",
        (
            f"{LKT}::test_create_override_reports_the_override_objects",
            f"{LKT}::test_link_as_override_names_the_override_roots_and_counts_the_rest",
        ),
    ),
    Revert(
        "linking: changed_objects names every member of a linked set, not the roots a caller acts on",
        ADDON_LINKING,
        '            if getattr(obj, "parent", None) is None or _uid_of(obj.parent) not in uids'
        "  # type: ignore[attr-defined]\n",
        "            if True\n",
        (
            f"{LKT}::test_link_names_the_roots_it_brought_in_and_counts_every_member",
            f"{LKT}::test_link_as_override_names_the_override_roots_and_counts_the_rest",
            f"{LKT}::test_a_linked_set_reply_fits_the_budget_with_every_root_named",
            f"{LKT}::test_create_override_reports_the_override_objects",
        ),
    ),
    Revert(
        "linking: link_canon_library ignores detail for the objects it instanced",
        ADDON_LINKING,
        "            brought_in,\n            type_of=_id_type,\n            name_of=_display_name,\n"
        "            describe=_linked_entry,\n            limit=MAX_LISTED_DATABLOCKS,\n            detail=detail,\n",
        "            brought_in,\n            type_of=_id_type,\n            name_of=_display_name,\n"
        "            describe=_linked_entry,\n            limit=MAX_LISTED_DATABLOCKS,\n            detail=False,\n",
        (f"{LKT}::test_link_detail_pages_the_members_as_records",),
    ),
    Revert(
        "linking: link_canon_library ignores detail for the overrides it builds",
        ADDON_LINKING,
        "        overrides = _override_all(list(linked_collections), scene, detail=detail)\n",
        "        overrides = _override_all(list(linked_collections), scene, detail=False)\n",
        (f"{LKT}::test_link_as_override_names_the_override_roots_and_counts_the_rest",),
    ),
    Revert(
        "linking: a linked collection's members are left out of the instanced count",
        ADDON_LINKING,
        "    members = [obj for collection in linked_collections for obj in collection.all_objects]"
        "  # type: ignore[attr-defined]\n",
        "    members = []\n",
        (f"{LKT}::test_link_names_the_roots_it_brought_in_and_counts_every_member",),
    ),
    # --- changed_objects crosses into the envelope, bounded, with its total named ---
    # All three moved to `envelope.py`: the twelve `_call` copies were unified onto
    # `envelope_for`, which owns the extraction, the bound and the warning. The behaviour
    # each row guards, and the tool-level node that notices, are unchanged.
    Revert(
        "server tools: the addon's changed_objects is left in the reply data as well as the envelope",
        SERVER_ENVELOPE,
        "        data = {key: value for key, value in reply.items() if key not in _CHANGE_KEYS}\n",
        "        data = dict(reply)\n",
        (f"{SFLT}::test_changed_objects_move_from_the_addon_result_into_the_envelope",),
    ),
    Revert(
        "server tools: changed_objects is published whole, so linking a set floods the agent's context",
        SERVER_ENVELOPE,
        "changed_objects=list(objects[:limit])",
        "changed_objects=list(objects)",
        (f"{SFLT}::test_changed_objects_are_bounded_and_the_total_is_reported",),
    ),
    Revert(
        "server tools: changed_resources is published whole, outside the budget that bounds data",
        SERVER_ENVELOPE,
        "changed_resources=list(resources[:limit])",
        "changed_resources=list(resources)",
        (f"{ENVT}::test_a_long_resource_list_is_bounded_the_same_way",),
    ),
    Revert(
        "server tools: a cut change list never says how many names there really were",
        SERVER_ENVELOPE,
        '            notices.append(f"{key} lists the first {limit} of {len(names)} {noun}")\n',
        "            pass\n",
        (
            f"{SFLT}::test_changed_objects_are_bounded_and_the_total_is_reported",
            f"{ENVT}::test_a_long_resource_list_is_bounded_the_same_way",
        ),
    ),
    # --- the budget recognises a page named after its own list ---
    Revert(
        "reply budget: a page paged under its list's own name is not recognised as a page",
        SERVER_ENVELOPE,
        '    for prefix in (f"{key}_", ""):\n',
        '    for prefix in ("",):\n',
        (f"{ENVT}::test_a_page_paged_under_a_prefixed_name_is_still_marked_truncated",),
    ),
    Revert(
        "reply budget: a page whose tool takes no offset for it is handed one to resume from",
        SERVER_ENVELOPE,
        '        if f"{prefix}offset" in owner or f"{prefix}next_offset" in owner:\n',
        "        if True:\n",
        (
            f"{ENVT}::test_a_page_whose_owner_takes_no_offset_is_marked_truncated_but_offers_none",
            f"{ENVT}::test_a_prefixed_page_without_its_own_offset_offers_no_offset_to_resume_from",
            f"{LKT}::test_a_detail_listing_shortened_by_the_budget_still_offers_no_datablock_offset",
        ),
    ),
    Revert(
        "reply budget: a prefixed page's resume hint names an offset= its tool may not take",
        SERVER_ENVELOPE,
        '    if names["next_offset"] != "next_offset":\n',
        "    if False:\n",
        (
            f"{ENVT}::test_a_camera_rig_page_resumes_from_the_offset_it_was_requested_at",
            f"{ENVT}::test_each_shortened_pages_warning_names_that_page_and_no_other",
        ),
    ),
    Revert(
        "reply budget: a shortened page resumes from 0 instead of the offset it was requested at",
        SERVER_ENVELOPE,
        '    return int(owner.get(names["offset"]) or 0)\n',
        "    return 0\n",
        (
            f"{ENVT}::test_a_camera_rig_page_resumes_from_the_offset_it_was_requested_at",
            f"{ENVT}::test_a_second_page_is_shortened_when_cutting_the_first_one_is_not_enough",
        ),
    ),
    # --- a light record is trimmed to what a listing is asked for ---
    Revert(
        "lighting: the default light record goes back to the full snapshot",
        ADDON_LIGHTING_SHARED,
        '    """Build the default inventory record: what identifies one light plus what a listing is asked for."""\n',
        '    """Reverted: the default record is the full snapshot again."""\n    return light_snapshot(obj)\n',
        (f"{LIGHTT}::test_default_light_record_is_identity_plus_the_facts_a_listing_is_asked_for",),
    ),
    Revert(
        "lighting: detail returns the same trimmed record, so the full state is unreachable",
        ADDON_LIGHTING_SHARED,
        '    """Build the full record light_summary trims: every transform, setting, and link one light carries."""\n',
        '    """Reverted: detail returns the trimmed record too."""\n    return light_summary(obj)\n',
        (f"{LIGHTT}::test_detail_records_carry_the_state_the_default_record_omits",),
    ),
    Revert(
        "lighting: the transform rounding helper returns the float unrounded",
        ADDON_LIGHTING_SHARED,
        "    return [round(float(value), TRANSFORM_DECIMALS) for value in values]\n",
        "    return [float(value) for value in values]\n",
        (f"{LIGHTT}::test_light_transform_floats_are_rounded_to_six_decimals",),
    ),
    Revert(
        "lighting: list_lights ignores detail and returns the full record either way",
        ADDON_LIGHTING_INSPECTION,
        "        record = light_snapshot if detail else light_summary\n"
        "        records = [record(obj) for obj in lights[start:end]]\n",
        "        records = [light_snapshot(obj) for obj in lights[start:end]]\n",
        (f"{LIGHTT}::test_light_inventories_trim_by_default_and_restore_full_records_with_detail",),
    ),
    Revert(
        "lighting: a preview's matched_state embeds every light's full record again",
        ADDON_LIGHTING_RENDERING,
        '    names = sorted(obj.name for obj in scene.objects if obj.type == "LIGHT")\n',
        '    names = [light_snapshot(obj) for obj in scene.objects if obj.type == "LIGHT"]\n',
        (f"{LIGHTT}::test_preview_matched_state_names_its_lights_instead_of_embedding_them",),
        # The slice dropped the import with the last call; a row has one anchor and cannot
        # add one, and a NameError would fail the node on the wrong thing.
        "\n\nfrom ._shared import light_snapshot\n",
    ),
    Revert(
        "lighting: list_lights does not forward detail, so the full records cannot be asked for",
        SERVER_LIGHTING_INSPECTION_TOOL,
        '            "detail": detail,\n',
        '            "detail": False,\n',
        (f"{LIGHTT}::test_light_inventory_tools_forward_the_detail_flag",),
    ),
    Revert(
        "lighting: configure_lighting_quality does not forward detail",
        SERVER_LIGHTING_RENDERING_TOOL,
        '            "detail": detail,\n',
        '            "detail": False,\n',
        (f"{LIGHTT}::test_lighting_quality_expands_strict_agent_payload",),
    ),
]
