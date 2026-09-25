"""
Rows guarding the session epoch, the session state, and its failure notes.

The epoch as the handshake and `get_addon_status` report it, the transitions read as a
table, and the text hygiene a failure note passes through.

Label prefixes: `handshake:`, `get_addon_status:`, `session:`, `text hygiene:`.
"""

from .common import (
    ADDON_BLEND_FILES,
    ADDON_COMMAND_REGISTRY,
    ADDON_FILE_LIFECYCLE,
    ADDON_INIT,
    ADDON_MANAGER,
    ADDON_SESSION,
    ADDON_TEXT_HYGIENE,
    AMT,
    CORET,
    SERVER_CORE_TOOL,
    SESSIONT,
    THREADT,
    Revert,
)

ROWS: list[Revert] = [
    # --- the handshake, and the MCP tool payload (`get_addon_status`) ---
    Revert(
        "handshake: the writable_output_roots parse removed from the handshake",
        ADDON_MANAGER,
        '            writable_output_roots=normalized_session_text_list(info.get("writable_output_roots")),\n',
        "",
        (
            f"{AMT}::test_handshake_surfaces_writable_output_roots",
            f"{AMT}::test_handshake_defaults_writable_output_roots_when_the_addon_omits_them",
        ),
    ),
    Revert(
        "handshake: the handshake reorders the roots it was sent",
        ADDON_MANAGER,
        '            writable_output_roots=normalized_session_text_list(info.get("writable_output_roots")),',
        '            writable_output_roots=normalized_session_text_list(info.get("writable_output_roots"))[::-1],',
        (f"{AMT}::test_handshake_surfaces_writable_output_roots",),
    ),
    Revert(
        "get_addon_status: get_addon_status hardcodes an empty roots list",
        SERVER_CORE_TOOL,
        '        "writable_output_roots": result.writable_output_roots,',
        '        "writable_output_roots": [],',
        (
            f"{CORET}::test_get_addon_status_reports_the_writable_output_roots",
            f"{CORET}::test_get_addon_status_reports_no_roots_for_an_addon_that_does_not_send_them",
        ),
    ),
    Revert(
        "get_addon_status: get_addon_status reorders the roots the handshake gave it",
        SERVER_CORE_TOOL,
        '        "writable_output_roots": result.writable_output_roots,',
        '        "writable_output_roots": list(reversed(result.writable_output_roots)),',
        (f"{CORET}::test_get_addon_status_reports_the_writable_output_roots",),
    ),
    Revert(
        "get_addon_status: get_addon_status invents roots for an addon that sent none",
        SERVER_CORE_TOOL,
        '        "writable_output_roots": result.writable_output_roots,',
        '        "writable_output_roots": result.writable_output_roots or ["/invented"],',
        (f"{CORET}::test_get_addon_status_reports_no_roots_for_an_addon_that_does_not_send_them",),
    ),
    # --- the session epoch reaching the agent ---
    Revert(
        "get_addon_status: the session epoch is hardcoded instead of read off the handshake",
        SERVER_CORE_TOOL,
        '        "session_epoch": result.session_epoch,',
        '        "session_epoch": 0,',
        (f"{CORET}::test_get_addon_status_reports_the_session_epoch_and_the_open_file",),
    ),
    Revert(
        "get_addon_status: the open .blend is hardcoded, so the payload cannot report a swap",
        SERVER_CORE_TOOL,
        '        "current_filepath": result.current_filepath,',
        '        "current_filepath": None,',
        (f"{CORET}::test_get_addon_status_reports_no_epoch_for_an_addon_that_does_not_send_one",),
    ),
    # --- session state, the epoch, and the failure notes -------------
    Revert(
        "session: a completed load stops moving the epoch, so no client learns its capabilities are stale",
        ADDON_SESSION,
        "        session_epoch=state.session_epoch + 1,\n        current_filepath=_reported_path(file_path),",
        "        session_epoch=state.session_epoch + 0,\n        current_filepath=_reported_path(file_path),",
        (
            f"{SESSIONT}::test_a_completed_load_moves_the_session_epoch_exactly_once",
            f"{SESSIONT}::test_resetting_the_session_moves_the_epoch_through_load_post_alone",
            f"{SESSIONT}::test_a_swap_after_a_disable_enable_cycle_still_moves_the_epoch_once",
            f"{SESSIONT}::test_get_addon_info_carries_the_session_epoch_and_the_current_filepath",
            f"{SESSIONT}::test_a_swap_moves_the_epoch_without_disturbing_the_session_id",
            f"{THREADT}::test_the_epoch_moves_once_per_successful_swap_and_not_at_all_on_a_failed_one",
        ),
    ),
    Revert(
        "session: a FAILED load bumps the epoch, forcing a re-handshake storm on an event that changed nothing",
        ADDON_SESSION,
        '        last_load_error=_failure_note("Loading", file_path, is_directory=is_directory),',
        (
            '        last_load_error=_failure_note("Loading", file_path, is_directory=is_directory),\n'
            "        session_epoch=state.session_epoch + 1,"
        ),
        (
            f"{SESSIONT}::test_a_failed_load_does_not_move_the_session_epoch",
            f"{SESSIONT}::test_get_session_info_reports_a_load_failure_without_moving_the_epoch",
        ),
    ),
    Revert(
        "session: a successful SAVE bumps the epoch, invalidating every client cache for nothing",
        ADDON_SESSION,
        "        current_filepath=now_open,",
        "        current_filepath=now_open,\n        session_epoch=state.session_epoch + 1,",
        (f"{SESSIONT}::test_a_successful_save_does_not_move_the_session_epoch",),
    ),
    Revert(
        "session: save_post trusts its argument, so save_as_mainfile(copy=True) names a file nobody has open",
        ADDON_SESSION,
        "    now_open = _reported_path(open_path)",
        "    now_open = _reported_path(written)",
        (
            f"{SESSIONT}::test_a_save_copy_does_not_make_the_state_name_a_file_nobody_has_open",
            f"{SESSIONT}::test_a_save_copy_does_not_clear_a_failure_belonging_to_a_different_file",
        ),
    ),
    Revert(
        "session: a failed save records nothing, so a blocked checkpoint looks like a clean one",
        ADDON_SESSION,
        '    return replace(state, last_save_error=_failure_note("Saving", file_path, is_directory=is_directory))',
        "    return replace(state, last_save_error=None)",
        (
            f"{SESSIONT}::test_a_failed_save_records_the_error_without_moving_the_epoch",
            f"{SESSIONT}::test_a_later_success_clears_the_recorded_failure",
        ),
    ),
    # --- the transitions read as a table: what each event may and may not move ---
    Revert(
        "session: load_pre moves the epoch too, so announcing a load invalidates every cache before anything is",
        ADDON_SESSION,
        "    return replace(state, load_in_flight=True)",
        "    return replace(state, load_in_flight=True, session_epoch=state.session_epoch + 1)",
        (f"{SESSIONT}::test_the_epoch_moves_in_exactly_the_transitions_that_may_have_replaced_the_database",),
    ),
    Revert(
        "session: a completed load stops accounting for the load it completed, leaving it in flight forever",
        ADDON_SESSION,
        "        load_in_flight=False,\n        session_indeterminate=False,",
        "        session_indeterminate=False,",
        (f"{SESSIONT}::test_every_transition_that_accounts_for_a_load_clears_the_in_flight_flag",),
    ),
    Revert(
        "session: a failed load clears the latch, so a refused reopen reads as evidence the database is whole",
        ADDON_SESSION,
        '        last_load_error=_failure_note("Loading", file_path, is_directory=is_directory),',
        (
            '        last_load_error=_failure_note("Loading", file_path, is_directory=is_directory),\n'
            "        session_indeterminate=False,"
        ),
        (f"{SESSIONT}::test_a_completed_load_is_the_only_transition_that_clears_the_indeterminate_latch",),
    ),
    Revert(
        "session: any save clears the recorded failure, so a copy written elsewhere answers for the file that refused",
        ADDON_SESSION,
        "    saved_the_open_file = _reported_path(written) == now_open\n"
        "    return replace(\n"
        "        state,\n"
        "        current_filepath=now_open,\n"
        "        last_save_error=None if saved_the_open_file else state.last_save_error,\n"
        "    )",
        "    return replace(\n        state,\n        current_filepath=now_open,\n        last_save_error=None,\n    )",
        (f"{SESSIONT}::test_a_save_copy_leaves_a_recorded_failure_belonging_to_another_file_alone",),
    ),
    # The atomicity the frozen state buys, stated as a property of the returned value:
    # the revert bumps the epoch on the state it was handed, which is exactly the
    # half-applied event a client thread could read between two assignments.
    Revert(
        "session: the abort bumps the epoch in place, so a reader holds the new epoch beside a False latch",
        ADDON_SESSION,
        "    return replace(\n"
        "        state,\n"
        "        session_epoch=state.session_epoch + 1,\n"
        "        last_load_error=INDETERMINATE_SESSION_NOTE,",
        '    object.__setattr__(state, "session_epoch", state.session_epoch + 1)\n'
        "    return replace(\n"
        "        state,\n"
        "        session_epoch=state.session_epoch,\n"
        "        last_load_error=INDETERMINATE_SESSION_NOTE,",
        (f"{SESSIONT}::test_an_abort_reaches_a_snapshot_whole_or_not_at_all",),
    ),
    Revert(
        "text hygiene: the leaf split goes back to os.path.basename, which on posix splits on / only",
        ADDON_TEXT_HYGIENE,
        "    leaf = strip_unsafe(raw)\n"
        "    for separator in _LEAF_SEPARATORS:\n"
        "        leaf = leaf.rsplit(separator, 1)[-1]",
        "    leaf = os.path.basename(raw)",
        (
            f"{SESSIONT}::test_a_recorded_failure_names_one_bounded_leaf_and_nothing_else[windows]",
            f"{SESSIONT}::test_a_recorded_failure_names_one_bounded_leaf_and_nothing_else[unc]",
        ),
    ),
    Revert(
        "text hygiene: control characters survive into a client-facing note (newline = prompt-injection surface)",
        ADDON_TEXT_HYGIENE,
        '    return "".join(character for character in str(value or "") if not is_unsafe(character)).strip()',
        '    return str(value or "").strip()',
        (
            # Not `[newline]` or `[ansi-escape]`: the leaf allowlist refuses those
            # without the strip, so they would pass (see NOT_INDIVIDUALLY_FALSIFIABLE).
            # These expect the stripped name `ab.blend`, which only stripping produces.
            f"{SESSIONT}::test_a_recorded_failure_names_one_bounded_leaf_and_nothing_else[nul]",
            f"{SESSIONT}::test_a_recorded_failure_names_one_bounded_leaf_and_nothing_else[c1-control]",
        ),
    ),
    # The anchor carries the line above the bound, so it names `client_safe_text`'s bound
    # and no other length check in the module.
    Revert(
        "text hygiene: the note's length bound goes away, so a 400-character name ships whole",
        ADDON_TEXT_HYGIENE,
        "    text = strip_unsafe(value)\n    if len(text) > max_chars:",
        "    text = strip_unsafe(value)\n    if False:",
        (f"{SESSIONT}::test_a_recorded_failure_names_one_bounded_leaf_and_nothing_else[over-long]",),
    ),
    Revert(
        "text hygiene: the leaf assertion stops rejecting traversal tokens and empty components",
        ADDON_TEXT_HYGIENE,
        'NOT_A_LEAF = frozenset({"", ".", ".."})',
        "NOT_A_LEAF = frozenset()",
        (
            f"{SESSIONT}::test_a_recorded_failure_names_one_bounded_leaf_and_nothing_else[dot-dot]",
            f"{SESSIONT}::test_a_recorded_failure_names_one_bounded_leaf_and_nothing_else[empty]",
            f"{SESSIONT}::test_a_recorded_failure_names_one_bounded_leaf_and_nothing_else[trailing-separator]",
        ),
    ),
    # The `isdir` probe is `session._names_a_directory`'s, not `text_hygiene`'s: that module
    # touches no filesystem, so the caller holding the path does the stat.
    Revert(
        "text hygiene: a directory is named in a failure note - which for an empty path is the server's own CWD",
        ADDON_SESSION,
        '        return os.path.isdir(str(file_path or ""))',
        "        return False",
        (f"{SESSIONT}::test_a_directory_is_never_named_in_a_recorded_failure",),
    ),
    Revert(
        "session: the snapshot loses its process-unique id, reopening the epoch's ABA hole across a restart",
        ADDON_SESSION,
        '        "session_id": SESSION_ID,',
        '        "session_id": "",',
        (
            f"{SESSIONT}::test_the_snapshot_carries_a_process_unique_session_id",
            f"{SESSIONT}::test_get_addon_info_and_get_session_info_agree_about_the_session_id",
        ),
    ),
    Revert(
        "session: handler registration stacks duplicates, so one swap moves the epoch twice",
        ADDON_SESSION,
        "        if handler not in handler_list:",
        "        if True:",
        (f"{SESSIONT}::test_registering_twice_does_not_stack_duplicate_handlers",),
    ),
    Revert(
        "session: unregistering an absent handler raises, leaving the addon un-unloadable",
        ADDON_SESSION,
        "        while handler in handler_list:\n            handler_list.remove(handler)",
        "        handler_list.remove(handler)",
        (f"{SESSIONT}::test_unregistering_without_registering_is_not_an_error",),
    ),
    Revert(
        "session: the addon lifecycle never wires the session handlers in, so nothing maintains the epoch",
        ADDON_INIT,
        "    session.register_handlers()",
        "    pass  # handler wiring reverted",
        (f"{SESSIONT}::test_the_addon_registers_and_unregisters_the_session_handlers",),
    ),
    Revert(
        "session: get_session_info is absent from the dispatch table, so the poll surface cannot be polled",
        ADDON_COMMAND_REGISTRY,
        '        "get_session_info": CommandSpec(read_only=True, indeterminate_safe=True),',
        '        "get_session_info_reverted": CommandSpec(read_only=True, indeterminate_safe=True),',
        (f"{SESSIONT}::test_get_session_info_is_registered_and_read_only",),
    ),
    Revert(
        "text hygiene: the library summary publishes an absolute filepath, mapping the asset library out",
        ADDON_BLEND_FILES,
        "        **published_path_fields(filepath, frame=frame),",
        '        "filepath": filepath,\n'
        '        "filepath_redacted": False,\n'
        '        "filepath_redaction_reason": None,',
        (f"{SESSIONT}::test_the_library_summary_reports_identity_without_the_asset_library_layout",),
    ),
    Revert(
        "session: save_shot joins the swap set, discarding a whole batch every time a client checkpoints",
        ADDON_COMMAND_REGISTRY,
        '        "save_shot": CommandSpec(tick_ending=True),',
        '        "save_shot": CommandSpec(tick_ending=True, session_swap=True),',
        (f"{SESSIONT}::test_the_session_swap_set_holds_the_commands_that_replace_the_database",),
    ),
    Revert(
        "session: a swap is wrapped in mutation_transaction, whose rollback would enumerate the whole new file",
        ADDON_COMMAND_REGISTRY,
        "            or spec.session_swap\n",
        "",
        (f"{SESSIONT}::test_a_session_swap_command_never_reaches_mutation_transaction",),
    ),
    Revert(
        "text hygiene: get_session_info reports the dirty flag and libraries from nowhere",
        ADDON_FILE_LIFECYCLE,
        '            "is_dirty": bool(bpy.data.is_dirty),',
        '            "is_dirty": False,',
        (f"{SESSIONT}::test_get_session_info_reports_the_dirty_flag_and_the_library_summary",),
    ),
    Revert(
        "session: an unsaved session reports an empty string, which reads as a real path in a client's logs",
        ADDON_SESSION,
        '    reported = str(file_path or "")\n    return reported or None',
        '    return str(file_path or "")',
        (f"{SESSIONT}::test_an_unsaved_session_reports_no_filepath_rather_than_an_empty_string",),
    ),
    Revert(
        "session: a disable/enable cycle leaves the handler lists stacked",
        ADDON_SESSION,
        "def unregister_handlers() -> None:",
        "def unregister_handlers() -> None:\n    return",
        (f"{SESSIONT}::test_a_disable_enable_cycle_leaves_exactly_one_of_each_handler",),
    ),
]
