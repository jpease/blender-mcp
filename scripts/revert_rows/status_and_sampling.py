"""
Rows guarding the status call's tool names and mount state, and neighbours.

INSPECT dispatch, deformed-geometry sampling, cycles, the open refusal, and rig reading.

Label prefixes: `get_addon_status:`, `dispatch:`, `deformed geometry:`, `cycle:`,
`file paths:`, `rig reading:`.
"""

from .common import (
    ADDON_ANIMATION,
    ADDON_CR_INSPECTION,
    ADDON_FILE_PATHS,
    ADDON_HELPERS,
    ADDON_POSING,
    ADDON_SERVER_CORE,
    ANIMT,
    CORET,
    CTRLT,
    DEFORMT,
    DRT,
    FPT,
    SERVER_CORE_TOOL,
    Revert,
)

ROWS: list[Revert] = [
    # --- the mounted tool names are an opt-in page, read off this process's own registry ---
    Revert(
        # Every name costs bytes in a reply most callers make for the version verdict alone; the
        # counts and the bundle summary are what the plain call is for.
        "get_addon_status: the mounted tool names ship on every status call, asked for or not",
        SERVER_CORE_TOOL,
        (
            "    if mounted_tools:\n"
            '        payload["mounted_tools"] = _mounted_tools_page(mounted, limit=tool_limit, offset=tool_offset)\n'
        ),
        '    payload["mounted_tools"] = _mounted_tools_page(mounted, limit=tool_limit, offset=tool_offset)\n',
        (f"{CORET}::test_get_addon_status_keeps_the_mounted_tool_names_opt_in",),
    ),
    Revert(
        # The catalog is every tool the project defines; the registry is what this process will
        # answer to. A caller chasing a tool its build does not carry is asking the second
        # question, and the catalog answers it wrongly while looking right.
        "get_addon_status: the tool names come from the project catalog, not this process's registry",
        SERVER_CORE_TOOL,
        "    names = sorted(mounted)\n",
        "    names = sorted(known_tool_names())\n",
        (f"{CORET}::test_get_addon_status_enumerates_exactly_the_tools_this_process_registered",),
    ),
    Revert(
        # The registry's own order is import order, which shifts with the selection, so a paged
        # walk of it repeats and skips names. Reversed rather than the set's own iteration
        # order: that order depends on the interpreter's hash seed, and this row's verdict
        # must not.
        "get_addon_status: the tool names are paged in an order the next call need not repeat",
        SERVER_CORE_TOOL,
        "    names = sorted(mounted)\n",
        "    names = sorted(mounted, reverse=True)\n",
        (f"{CORET}::test_get_addon_status_pages_the_tool_names_deterministically",),
    ),
    Revert(
        # A page that ends on the total and still asks to be continued sends the caller back to
        # the same empty page for ever, which is what `next_offset` is looped on.
        "get_addon_status: a page ending on the last name still asks to be continued",
        SERVER_CORE_TOOL,
        "    truncated = resume < len(names)\n",
        "    truncated = resume <= len(names)\n",
        (f"{CORET}::test_get_addon_status_reports_an_offset_past_the_last_name_as_the_end",),
    ),
    # --- an INSPECT cycle call reads, and a probe restores what it borrowed ---
    Revert(
        # Dispatched as a write, an inspection pays for a snapshot, a diff and an undo
        # checkpoint to report a number it only read.
        "dispatch: an INSPECT cycle call is dispatched as a write",
        ADDON_SERVER_CORE,
        (
            '        "set_action_cycle": CommandSpec(\n'
            '            read_only_when=lambda params: str(params.get("operation", "SET")).upper() == "INSPECT"\n'
            "        ),\n"
        ),
        '        "set_action_cycle": CommandSpec(),\n',
        (
            f"{DRT}::test_the_params_decide_whether_these_commands_read_or_write[set_action_cycle-reading15-writing15]",
            f"{DRT}::test_the_params_decide_whether_these_commands_read_or_write[set_action_cycle-reading16-writing16]",
        ),
    ),
    Revert(
        # The probe turns the bone, measures, and puts the pose back inside its own call, so
        # there is no net mutation for a transaction to snapshot - and the snapshot it would
        # take sits around every trial turn of a read-only question.
        "dispatch: the bone-axis probe is dispatched as a mutation",
        ADDON_SERVER_CORE,
        '        "probe_bone_axis": CommandSpec(read_only=True),\n',
        '        "probe_bone_axis": CommandSpec(),\n',
        (
            f"{DRT}::test_these_commands_run_outside_the_transaction"
            "[probe_bone_axis-restores its own trial pose, so there is no net mutation to snapshot]",
        ),
    ),
    # --- the mount state is on every status call; the per-name verdict is the opt-in one ---
    Revert(
        # The plain call is the setup check, and `capability_count` beside a short tool list
        # already read as a registration fault: this block is what explains the gap. Gating it
        # on `detail` leaves the explanation behind the same flag as the thing it explains.
        "get_addon_status: the mount state is withheld unless the caller asks for detail",
        SERVER_CORE_TOOL,
        '        "toolsets": _toolset_payload(mounted),\n    }\n    if tool_name is not None:\n',
        (
            "    }\n"
            "    if detail:\n"
            '        payload["toolsets"] = _toolset_payload(mounted)\n'
            "    if tool_name is not None:\n"
        ),
        (f"{CORET}::test_get_addon_status_reports_the_mount_state_without_being_asked",),
    ),
    Revert(
        # The variable is set whole, not appended to, so suggesting the bare bundle silently
        # unmounts the mode the session is running: the remedy for one missing tool would cost
        # the agent every other tool it was using.
        "get_addon_status: the suggested toolset drops the selection already in force",
        SERVER_CORE_TOOL,
        '    return f"{current},{bundle}" if current else bundle\n',
        "    return bundle\n",
        (f"{CORET}::test_tool_lookup_appends_the_missing_bundle_to_the_selection_already_in_force",),
    ),
    Revert(
        # Four situations hide behind one "unknown tool" error - mounted, unmounted, server
        # older than the add-on, and a typo - and they call for opposite responses. Folding the
        # stale-server case into the unknown one tells the caller to fix a spelling that is right.
        "get_addon_status: a server older than its add-on is reported as an unknown name",
        SERVER_CORE_TOOL,
        "    elif addon_command:\n",
        "    elif False:\n",
        (f"{CORET}::test_tool_lookup_separates_an_unmounted_tool_from_an_unknown_one",),
    ),
    Revert(
        # Bundles overlap - `lighting` and `lighting-construction` share a module - so summing
        # the per-bundle counts counts the shared tools twice and reports more absent tools than
        # the catalog has.
        "get_addon_status: the absent-tool count sums overlapping bundles instead of naming distinct tools",
        SERVER_CORE_TOOL,
        '        "unmounted_tool_count": len(known_tool_names() - mounted),\n',
        '        "unmounted_tool_count": sum(unmounted.values()),\n',
        (f"{CORET}::test_toolset_payload_counts_what_a_selection_left_out",),
    ),
    Revert(
        # The blind spot the whole tool exists for: `obj.data` answers identically before and
        # after a pose, so every measurement it reports is of the rest surface.
        "deformed geometry: the sample reads the base mesh instead of the evaluated one",
        ADDON_CR_INSPECTION,
        "    evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())\n",
        "    evaluated = obj\n",
        (
            f"{DEFORMT}::test_the_sample_reads_the_deformed_surface_and_measures_it_against_the_rest_mesh",
            f"{DEFORMT}::test_world_space_positions_carry_the_objects_own_transform",
            f"{DEFORMT}::test_a_generative_modifier_is_reported_as_a_different_numbering_rather_than_mismeasured",
            f"{DEFORMT}::test_a_refused_request_still_puts_the_playhead_back_and_releases_the_mesh",
            f"{DEFORMT}::test_an_explicit_index_list_is_paged_in_the_order_it_was_given",
            f"{DEFORMT}::test_a_page_reports_the_offset_to_continue_from",
        ),
    ),
    Revert(
        # Read-only, so no transaction covers it: a sample that moved the playhead and left it
        # there silently re-times every later call in the session.
        "deformed geometry: a frame-scoped sample leaves the playhead where it moved it",
        ADDON_CR_INSPECTION,
        "            if frame is not None and scene.frame_current != previous_frame:\n",
        "            if False:\n",
        (
            f"{DEFORMT}::test_the_sample_reads_the_deformed_surface_and_measures_it_against_the_rest_mesh",
            f"{DEFORMT}::test_a_refused_request_still_puts_the_playhead_back_and_releases_the_mesh",
        ),
    ),
    Revert(
        # A Subdivision or Mirror result is a different numbering, so pairing index i with base
        # vertex i measures the distance between two vertices that were never the same one.
        "deformed geometry: displacement pairs evaluated indices with base vertices regardless of count",
        ADDON_CR_INSPECTION,
        "    if len(mesh.vertices) != len(base_mesh.vertices):\n",
        "    if False:\n",
        (f"{DEFORMT}::test_a_generative_modifier_is_reported_as_a_different_numbering_rather_than_mismeasured",),
    ),
    Revert(
        # This repo's first wrong warning: a deliberately uncycled track told it drifts from the
        # stride, remedied by keying it over the stride's range - which destroys the take.
        "cycle: the period disagreement is measured over curves carrying no Cycles modifier",
        ADDON_ANIMATION,
        '    measurable = [record for record in records if operation != "INSPECT" or record["has_cycles_modifier"]]\n',
        "    measurable = list(records)\n",
        (f"{ANIMT}::test_inspect_does_not_tell_an_uncycled_curve_it_drifts_from_the_cycled_ones",),
    ),
    Revert(
        # A key span is only a period if something repeats it. Reported under the period field,
        # it reads as a rival cycle to every caller and to the warning that compares them.
        "cycle: an uncycled curve reports its key span as a period",
        ADDON_ANIMATION,
        '            record["period_frames"] = None\n',
        '            record["period_frames"] = record["key_extent_frames"]\n',
        (f"{ANIMT}::test_inspect_reports_a_curve_that_carries_no_cycle_where_remove_omits_it",),
    ),
    Revert(
        # "file does not exist" alone makes a mistyped folder and a mistyped filename one
        # sentence, and the refusal may not name the path that would tell them apart.
        "file paths: the open refusal cannot say which half of the path is wrong",
        ADDON_FILE_PATHS,
        "        directory_exists=os.path.isdir(os.path.dirname(path)),\n",
        "        directory_exists=True,\n",
        (f"{FPT}::test_a_mistyped_directory_and_a_mistyped_filename_are_not_the_same_refusal",),
    ),
    Revert(
        # The verdict itself, not its wiring: one sentence for both halves is what made a
        # mistyped folder and a mistyped filename indistinguishable to a caller.
        "file paths: one refusal covers a missing file and a missing directory alike",
        ADDON_FILE_PATHS,
        (
            "    if not exists:\n"
            "        if not directory_exists:\n"
            '            return "file does not exist, and neither does the directory named in its path"\n'
            '        return "file does not exist, though the directory named in its path does"\n'
        ),
        '    if not exists:\n        return "file does not exist"\n',
        (
            f"{FPT}::test_the_open_verdict_is_decided_from_facts_alone[a mistyped filename]",
            f"{FPT}::test_the_open_verdict_is_decided_from_facts_alone"
            "[a mistyped directory - the same sentence until this split them]",
            f"{FPT}::test_a_mistyped_directory_and_a_mistyped_filename_are_not_the_same_refusal",
        ),
    ),
    Revert(
        # `truncated` with nowhere to resume from is the one paging shape the envelope forbids.
        "rig reading: the deformed-mesh page ignores the offset it told the caller to resume from",
        ADDON_POSING,
        "paginate(len(bound), mesh_offset, _MAX_DEFORMED_MESHES, _MAX_DEFORMED_MESHES)",
        "paginate(len(bound), 0, _MAX_DEFORMED_MESHES, _MAX_DEFORMED_MESHES)",
        (f"{CTRLT}::test_a_rig_deforming_more_meshes_than_one_page_is_resumable",),
    ),
    Revert(
        # Transport parenting is not skinning: a prop that rides the rig is not part of the
        # silhouette a camera frames or a surface sample measures.
        "rig reading: any parent counts as a deform bind, not ARMATURE parenting",
        ADDON_HELPERS,
        '        parented = obj.parent == armature and obj.parent_type == "ARMATURE"\n',
        "        parented = obj.parent == armature\n",
        (f"{CTRLT}::test_bone_listing_names_the_meshes_the_rig_actually_deforms",),
    ),
]
