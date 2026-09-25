"""
Check that each test this matrix tracks fails once the behaviour it names is reverted.

A test that still passes with its fix reverted is a critical failure here. Per-file
evidence hides one, because a sibling failing in the same file looks like the revert
was caught, so each row names the exact node ids it expects to fail and runs only those.

Usage::

    .venv/bin/python scripts/revert_matrix.py                # run the whole matrix
    .venv/bin/python scripts/revert_matrix.py --list         # print the coverage map
    .venv/bin/python scripts/revert_matrix.py --only linking # rows whose label contains it

Every label starts with the area it guards, and `--only` matches that prefix as a
substring, so the prefix is how a group of rows is selected: `session:`, `barrier:`,
`handshake:`, `rehandshake:`, `text hygiene:`, `transaction:`, `file paths:`,
`file roots:`, `file lifecycle:`, `save_shot`'s `create_directories:`, `linking:`,
`candidates:`, `object lookup:`, `polyhaven:`, `output_roots:`, `get_addon_status:`,
`server tools:`, `server instructions:`, `transport:`, `rig:`, `docker:`,
`entrypoint:`, `quiet box:`, `reply budget:`, `lighting:`, `pose:`,
`render settings:`, `strict args:`, `addon surface:`, `action assignment:`,
`camera:`, `pagination:`, `simulation:`, `data users:`, `counted replies:`, `lint gate:` and
`boundary:`. A `... control:` row is the deliberate opposite of its neighbour: it proves
that over-enforcing the same line is caught too, either by the same node or by the
sibling node that exists to say the guard can be passed.

The rows live in `revert_rows/`, one module per stretch of the table, named for the area
most of its rows guard and listing every prefix it holds; `REVERTS` below joins them in the
order they were written. `revert_rows/common.py` holds the `Revert` type and the files and
test nodes rows cite. A new row goes in the module for its prefix, or in a new module added
to `REVERTS`.

Each row edits one file in place, runs its nodes, and restores the file in a
`finally`. Rows edit files under `src/`, `scripts/`, `tests/` and `docker/`, and
`pyproject.toml`: start from a clean tree, run nothing else against the checkout
meanwhile, and check `git status` afterwards in case a crash skipped a restore.

A row prints `FAILS as required` when all its nodes fail, and is listed as a SURVIVOR
otherwise. A node collected from NEW_TEST_FILES or listed in
NEW_NODES_IN_EXISTING_FILES is UNCOVERED unless a row names it or
NOT_INDIVIDUALLY_FALSIFIABLE gives a reason. The exit status is 0 only with neither.

In a copy of the repository, `.venv`'s editable install still imports the original
`src`, so reverts to the package never run and their rows falsely survive. Put the
copy's `src` first::

    PYTHONPATH=<copy>/src .venv/bin/python scripts/revert_matrix.py
"""

import argparse
import pathlib
import re
import subprocess
import sys

from dataclasses import dataclass, field

# `quiet_box` and `revert_rows` are importable because `scripts/` is sys.path[0] when this
# file or `check_revert_anchors.py` runs as a script. Code outside `scripts/` uses
# `quiet_box.load_quiet_box`.
import quiet_box

from revert_rows import (
    animation,
    argument_gates,
    artefact_truth,
    barrier,
    camera,
    counted_replies,
    data_users,
    docker,
    drain_and_viewport,
    error_paths,
    file_lifecycle,
    file_paths,
    handshake,
    install,
    keyframing,
    linking,
    lint_gate,
    load_stamp,
    object_lookup,
    output_roots,
    pagination,
    pose,
    registry,
    render_coverage,
    reply_shape,
    rig,
    server_tools,
    session,
    session_boundary,
    simulation,
    status_and_sampling,
    teardown,
    transaction,
    transport,
)
from revert_rows.common import (
    AMT,
    ANIMT,
    AUTHT,
    BUNT,
    CAMT,
    CANDT,
    CAPT,
    CLIT,
    CLOTHT,
    CONNFAILT,
    CONNT,
    CORET,
    CRTT,
    CTRLT,
    DEFORMT,
    DISPT,
    DOCKT,
    DRT,
    ENVT,
    EVASION,
    FLT,
    FPT,
    GNT,
    HOSTILE_LIB,
    KEYSTYLET,
    LIGHTT,
    LINTT,
    LIQUIDT,
    LIST_SCALAR,
    LISTT,
    LKT,
    MUTT,
    NDOUTT,
    NDSTATUST,
    OANIMT,
    OLT,
    PHT,
    POSET,
    QBT,
    RBWT,
    RCT,
    REACHT,
    REGT,
    RENDT,
    RIGT,
    RJOBT,
    RNAPT,
    ROOT,
    ROOTST,
    SCENETOOLT,
    SESSIONT,
    SFLT,
    SIT,
    SOIT,
    SRVPHT,
    STRICTT,
    SURFT,
    SVT,
    TDT,
    THREADT,
    TSWAPT,
    VIEWT,
    Revert,
)

# Test files this matrix owns outright: every node they collect must be accounted for.
# CAPT joins this set because `capability_params` is a trust-boundary field like the rest of
# them: it feeds a refusal the server makes before any round trip. Feature-behaviour files
# (posing, rendering, viewport) stay off it and are tracked node by node instead, matching how
# RENDT/POSET nodes are listed in NEW_NODES_IN_EXISTING_FILES.
# STRICTT joins it on the same rule: every node in it is about the argument dict a client sends,
# which is the outermost trust boundary this server has. SURFT joins it because the file is not
# about a feature at all - it is the freshness gate itself, and a node added there is by
# construction another claim about the committed snapshot, so leaving one unaccounted for would
# be leaving the gate's own coverage to chance. DISPT joins it because every tool in every
# package now reaches Blender through that one function, so a node added there is a claim about
# the whole server's transport, not about one feature. REGT joins it on the SURFT rule: it is
# not about a feature either, it is the dispatch-and-classification gate itself, and every node
# in it is a claim about which command the add-on runs and what protection it runs under. AUTHT
# joins it because every node in it is a claim the add-on writes into a shipped `.blend`'s
# provenance block, which a recipient cannot check against anything else.
NEW_TEST_FILES = (
    RIGT,
    DOCKT,
    ROOTST,
    CORET,
    CLIT,
    SESSIONT,
    QBT,
    TSWAPT,
    FPT,
    PHT,
    FLT,
    LKT,
    SFLT,
    OLT,
    CANDT,
    SIT,
    CAPT,
    STRICTT,
    SURFT,
    KEYSTYLET,
    DISPT,
    REGT,
    AUTHT,
    DEFORMT,
)
# Nodes in files the matrix does not own. `coverage_gaps()` sees only these and the nodes
# collected from NEW_TEST_FILES, so a node left off this list is never checked.
NEW_NODES_IN_EXISTING_FILES = (
    # --- the lint gate forgives only the metrics a function already carried at the base ---
    f"{LINTT}::test_a_legacy_metric_the_branch_only_touched_stays_in_the_backlog",
    f"{LINTT}::test_a_legacy_metric_the_branch_raised_is_owned",
    f"{LINTT}::test_a_renamed_function_has_no_base_reading_and_is_owned",
    f"{LINTT}::test_a_metric_is_keyed_on_the_innermost_definition_holding_it",
    # --- an inspection measures the cycled curves and nothing else ---
    f"{ANIMT}::test_inspect_does_not_tell_an_uncycled_curve_it_drifts_from_the_cycled_ones",
    f"{ANIMT}::test_inspect_reports_a_curve_that_carries_no_cycle_where_remove_omits_it",
    # --- which meshes a rig deforms, without moving a camera to find out ---
    f"{CTRLT}::test_bone_listing_names_the_meshes_the_rig_actually_deforms",
    f"{CTRLT}::test_a_rig_deforming_more_meshes_than_one_page_is_resumable",
    # --- the drain timer follows the traffic instead of a flat 50 ms poll ---
    f"{THREADT}::test_a_command_makes_the_next_drain_follow_within_the_active_poll",
    f"{THREADT}::test_the_drain_poll_relaxes_once_the_session_goes_quiet",
    f"{THREADT}::test_a_server_that_has_served_nothing_polls_at_the_idle_rate",
    # --- a failed viewport capture leaves no datablock behind ---
    f"{VIEWT}::test_offscreen_capture_removes_its_image_datablock_when_the_save_fails",
    f"{VIEWT}::test_window_grab_removes_the_loaded_screenshot_when_the_rescale_save_fails",
    # --- artefact truth: what the save discards, and who authored the file ---
    f"{MUTT}::test_persistence_an_unreferenced_created_datablock_is_reported",
    f"{MUTT}::test_persistence_a_fake_user_datablock_is_not_reported",
    f"{MUTT}::test_persistence_an_assigned_datablock_is_not_reported",
    f"{MUTT}::test_persistence_a_linked_datablock_is_never_this_commands_authorship",
    f"{MUTT}::test_persistence_the_warning_names_at_most_five_and_counts_the_rest",
    # --- a refusal in the console does not read like a fault ---
    f"{MUTT}::test_a_refused_request_is_logged_without_a_traceback",
    f"{SVT}::test_persistence_findings_flag_unreferenced_and_fake_user_only_datablocks",
    f"{SVT}::test_persistence_findings_ignore_linked_datablocks",
    f"{SVT}::test_persistence_findings_report_truncation_past_max_findings",
    f"{SVT}::test_validate_scene_runs_the_persistence_domain_on_request",
    # --- artefact truth: render intent lives on the scene ---
    f"{RENDT}::test_render_scene_without_a_filepath_names_the_tool_that_sets_one",
    f"{RENDT}::test_render_scene_renders_to_the_scenes_own_output_path",
    f"{RENDT}::test_render_scene_refuses_an_animation_over_blenders_untouched_default_range",
    f"{RENDT}::test_render_scene_accepts_the_default_range_when_it_was_chosen",
    f"{RENDT}::test_render_scene_persists_the_callers_template_not_the_resolved_path",
    f"{RENDT}::test_render_scene_leaves_the_output_path_alone_by_default",
    f"{RENDT}::test_render_scene_does_not_persist_a_cancelled_render",
    f"{RENDT}::test_render_scene_refuses_to_persist_a_still_path",
    f"{RENDT}::test_configure_render_settings_refuses_a_directory_as_the_stored_template",
    f"{RENDT}::test_render_scene_reply_summarises_and_detail_restores_the_per_frame_arrays",
    f"{DRT}::test_a_render_that_persists_its_output_template_is_transacted",
    # --- artefact truth: which render properties the schemas reach ---
    f"{RCT}::test_coverage_classifies_the_same_identifier_per_owner",
    f"{RCT}::test_coverage_never_reports_an_excluded_property_as_unreachable",
    f"{RCT}::test_coverage_ignores_an_absent_owner",
    f"{RCT}::test_the_recorded_baseline_and_the_shipped_routes_agree_on_the_reachable_set",
    # --- which object a name shared with an override resolves to ---
    f"{SOIT}::test_the_transaction_snapshots_the_same_object_the_handler_mutates_after_an_override",
    f"{SOIT}::test_an_ambiguous_target_name_is_skipped_rather_than_raising_out_of_the_snapshot",
    f"{SOIT}::test_object_name_lookups_resolve_to_the_override_even_when_the_linked_original_is_listed_first",
    f"{SOIT}::test_get_object_info_says_whether_it_resolved_an_override_or_a_linked_object",
    # --- the handshake carries the writable output roots ---
    f"{AMT}::test_handshake_surfaces_writable_output_roots",
    f"{AMT}::test_handshake_defaults_writable_output_roots_when_the_addon_omits_them",
    # --- the handshake carries the session ---
    f"{AMT}::test_handshake_surfaces_the_session_epoch_and_the_open_file",
    f"{AMT}::test_handshake_defaults_the_session_fields_when_the_addon_omits_them",
    *(
        f"{AMT}::test_handshake_refuses_a_current_filepath_that_is_not_a_string[{case}]"
        for case in ("dict", "list", "int", "bool", "float")
    ),
    f"{AMT}::test_handshake_bounds_a_pathologically_long_current_filepath",
    f"{AMT}::test_handshake_surfaces_the_session_id_so_the_epoch_survives_a_restart",
    *(
        f"{AMT}::test_handshake_refuses_a_session_id_that_is_not_a_string[{case}]"
        for case in ("dict", "list", "int", "bool", "float")
    ),
    # --- one addon in Blender's Add-ons list, however many times it is installed ---
    f"{AMT}::test_repeat_installs_leave_one_addon_for_blender_to_load",
    f"{AMT}::test_install_leaves_an_older_installers_backup_alone_and_names_it",
    f"{AMT}::test_install_refuses_to_write_through_a_development_symlink",
    # --- bounded rejection, aborts around the swap, and a refresh that stops retrying ---
    f"{THREADT}::test_rejecting_a_full_queue_to_distinct_stalled_peers_is_bounded",
    f"{THREADT}::test_an_abort_during_the_pre_swap_drain_answers_the_swaps_own_client",
    f"{THREADT}::test_an_abort_before_the_swap_is_dispatched_does_not_claim_the_database_is_half_replaced",
    f"{THREADT}::test_a_failed_load_then_an_abort_does_not_claim_a_known_clean_database_is_half_replaced",
    f"{THREADT}::test_a_second_identical_failure_then_an_abort_is_still_not_indeterminate",
    f"{CONNT}::test_a_refresh_that_learns_a_newer_session_than_the_one_observed_stops_retrying",
    # --- the barrier, the stamp, and the liveness bounds ---
    f"{THREADT}::test_a_swap_is_the_last_command_its_tick_executes",
    f"{THREADT}::test_a_failed_swap_also_discards_the_commands_queued_behind_it",
    f"{THREADT}::test_the_barrier_message_names_the_epoch_without_claiming_it_moved",
    f"{THREADT}::test_the_barrier_message_leaks_no_filesystem_path",
    f"{THREADT}::test_a_command_that_arrives_after_the_swap_is_serviced_normally",
    f"{THREADT}::test_the_epoch_moves_once_per_successful_swap_and_not_at_all_on_a_failed_one",
    f"{THREADT}::test_every_command_spanning_a_swap_is_answered_on_both_sockets",
    f"{THREADT}::test_a_command_queued_before_a_swap_that_lands_elsewhere_is_rejected_at_dequeue",
    f"{THREADT}::test_a_command_queued_after_the_swap_is_serviced_normally_under_the_stamp",
    f"{THREADT}::test_the_enqueue_path_is_the_only_producer_and_it_stamps",
    f"{THREADT}::test_the_stamp_is_read_without_touching_bpy_on_the_client_thread",
    f"{THREADT}::test_a_swap_command_the_addon_cannot_dispatch_discards_nobodys_batch",
    f"{THREADT}::test_rejecting_a_full_queue_to_a_stalled_peer_is_bounded",
    f"{THREADT}::test_the_pre_swap_drain_is_bounded_by_an_explicit_count",
    f"{THREADT}::test_the_swaps_own_client_is_answered_when_the_swap_raises_a_base_exception",
    f"{THREADT}::test_a_base_exception_during_the_pre_swap_drain_still_answers_what_was_taken",
    f"{THREADT}::test_the_barrier_rejection_carries_the_epoch_as_a_first_class_field",
    f"{THREADT}::test_the_queue_is_snapshotted_before_the_swap_runs_not_after",
    f"{THREADT}::test_a_swap_ends_its_tick_even_when_a_fresh_command_is_already_queued",
    f"{THREADT}::test_the_open_mainfile_stub_takes_use_scripts_the_way_production_passes_it",
    f"{THREADT}::test_an_escaping_exception_hands_the_drain_loop_to_a_fresh_timer",
    f"{THREADT}::test_a_stopped_server_does_not_resurrect_its_drain_timer",
    # --- the epoch made actionable on the client side ---
    f"{CONNT}::test_an_unchanged_session_marker_does_not_invalidate_the_cached_handshake",
    f"{CONNT}::test_a_moved_epoch_marks_the_cached_handshake_stale",
    f"{CONNT}::test_a_restarted_addon_at_the_same_epoch_still_marks_the_handshake_stale",
    f"{CONNT}::test_the_marker_is_read_from_a_command_result_as_well_as_the_frame",
    f"{CONNT}::test_a_response_carrying_no_marker_changes_nothing",
    f"{CONNT}::test_the_command_gate_reads_the_refreshed_capability_set",
    f"{CONNT}::test_a_barrier_rejection_read_off_the_socket_marks_the_handshake_stale",
    # --- liveness, the fail-closed stamp, and the abort path ---
    f"{THREADT}::test_a_healthy_peer_queued_behind_stalled_ones_is_still_answered",
    f"{THREADT}::test_a_peer_slower_than_a_loopback_reader_is_not_destroyed_for_it",
    f"{THREADT}::test_a_malformed_frame_arriving_mid_rejection_cannot_park_the_main_thread",
    f"{THREADT}::test_a_socket_whose_timeout_cannot_be_restored_is_dropped_not_left_spinning",
    f"{THREADT}::test_a_command_that_reached_the_queue_unstamped_is_rejected_not_run",
    f"{THREADT}::test_an_ordinary_response_carries_the_session_marker_too",
    f"{THREADT}::test_an_aborted_swap_invalidates_the_stamps_taken_during_its_load",
    f"{THREADT}::test_an_aborted_swap_publishes_an_indeterminate_note_every_client_can_poll",
    f"{THREADT}::test_a_dying_tick_leaves_exactly_one_live_drain_timer",
    *(
        f"{EVASION}[{case}]"
        for case in (
            "blocking put()-def sneak(self, item):\\n    self.command_queue.put(item)\\n",
            "async def-async def sneak(self, item):\\n    self.command_queue.put_nowait(item)\\n",
            "local alias-def sneak(self, item):\\n    q = self.command_queue\\n    q.put_nowait(item)\\n",
            "lambda at class scope-class C:\\n    sneak = lambda self, item: self.command_queue.put_nowait(item)\\n",
            "helper takes the queue-def sneak(target, item):\\n    target.put_nowait(item)\\n",
        )
    ),
    # --- the client-side reaction, made non-amplifying ---
    f"{CONNT}::test_an_ordinary_commands_result_cannot_trip_a_re_handshake",
    f"{CONNT}::test_a_non_conforming_epoch_does_not_re_arm_the_flag_forever",
    f"{CONNT}::test_one_swap_costs_exactly_one_re_handshake_over_a_real_round_trip",
    # Both halves of "the refresh did not report a session": the round trip died, and it
    # completed carrying half a pair.
    f"{CONNT}::test_a_refresh_whose_round_trip_dies_leaves_the_staleness_signal_standing",
    f"{CONNT}::test_a_refresh_that_reports_no_usable_session_leaves_the_staleness_signal_standing",
    # --- the server boundary, the latch, the send floor ---
    f"{AMT}::test_the_handshake_strips_control_characters_from_the_session_id",
    f"{AMT}::test_the_handshake_strips_control_characters_from_the_reported_filepath",
    f"{AMT}::test_a_session_id_that_is_nothing_but_control_characters_is_absent_not_empty",
    f"{AMT}::test_the_handshake_reports_an_indeterminate_session_only_when_the_addon_says_so",
    f"{AMT}::test_both_sides_of_the_socket_hold_the_same_control_character_block",
    f"{THREADT}::test_a_command_is_refused_while_the_session_is_indeterminate",
    f"{THREADT}::test_the_commands_that_report_or_repair_an_indeterminate_session_still_run",
    f"{THREADT}::test_an_abort_before_the_swap_runs_does_not_claim_the_database_was_touched",
    f"{THREADT}::test_an_abort_after_a_completed_load_does_not_bump_the_marker_twice",
    f"{THREADT}::test_the_past_budget_send_never_takes_the_socket_out_of_timeout_mode",
    f"{THREADT}::test_a_peer_whose_recv_reports_would_block_is_not_disconnected",
    # --- the whole handshake constructor, and load_pre ---
    *(
        f"{AMT}::test_every_handshake_field_refuses_the_same_hostile_string[{field}]"
        for field in (
            "addon_version",
            "blender_version",
            "capabilities",
            "current_filepath",
            "protocol_version",
            "session_epoch",
            "session_id",
            "session_indeterminate",
            "writable_output_roots",
        )
    ),
    *(
        f"{AMT}::test_a_hostile_element_inside_a_list_field_is_dropped_not_published[{field}]"
        for field in ("addon_version", "capabilities", "writable_output_roots")
    ),
    *(f"{AMT}::{LIST_SCALAR}[{field}]" for field in ("capabilities", "writable_output_roots")),
    f"{AMT}::test_an_addon_version_that_is_not_a_version_is_absent_rather_than_stripped",
    f"{AMT}::test_the_handshake_log_line_cannot_be_forged_by_the_addon_payload",
    f"{THREADT}::test_a_retry_then_an_abort_part_way_through_the_second_load_is_indeterminate",
    f"{THREADT}::test_an_abort_between_the_dispatch_and_the_load_is_not_claimed_to_have_touched_the_database",
    f"{THREADT}::test_an_abort_in_the_swaps_prologue_still_answers_the_swaps_own_client",
    f"{THREADT}::test_a_swap_that_answers_normally_is_not_answered_a_second_time_by_the_guard",
    f"{THREADT}::test_a_second_abort_that_began_no_load_does_not_bump_the_marker_again",
    # --- a file swap and a library reload under a transaction, kept as regression guards ---
    f"{MUTT}::test_regression_guard_a_transaction_unaware_of_a_file_swap_removes_the_whole_new_file",
    f"{MUTT}::test_regression_guard_a_transaction_unaware_of_a_library_reload_removes_the_reloaded_contents",
    # --- the file path policy crosses the handshake ---
    f"{AMT}::test_handshake_surfaces_the_file_path_policy",
    f"{AMT}::test_handshake_reads_an_addon_that_omits_the_file_path_policy_as_permissive",
    f"{AMT}::test_every_handshake_field_refuses_the_same_hostile_string[file_roots]",
    f"{AMT}::test_every_handshake_field_refuses_the_same_hostile_string[file_roots_enforced]",
    f"{AMT}::test_a_hostile_element_inside_a_list_field_is_dropped_not_published[file_roots]",
    # --- file_lifecycle joins core, and the ten tools' hints/prose ---
    f"{BUNT}::test_default_mode_payload_stays_under_its_ceiling",
    f"{BUNT}::test_file_lifecycle_tools_are_exactly_eleven_and_reachable_from_shot_and_asset",
    f"{BUNT}::test_file_lifecycle_tools_advertise_correct_hints",
    f"{BUNT}::test_file_lifecycle_tools_blend_file_prose_is_correct",
    # --- descriptions advertise only what the schema does not ---
    f"{TDT}::test_schema_constraints_are_not_restated_as_prose",
    f"{TDT}::test_a_parameter_the_schema_already_describes_carries_no_description",
    f"{TDT}::test_units_and_datablock_semantics_survive",
    f"{TDT}::test_a_non_numeric_parameter_is_never_labelled_with_scene_units",
    f"{TDT}::test_a_nested_model_title_is_not_spliced_into_its_parameters",
    f"{BUNT}::test_advertised_parameter_descriptions_do_not_restate_the_schema[None]",
    f"{BUNT}::test_advertised_parameter_descriptions_do_not_restate_the_schema[shot]",
    f"{BUNT}::test_advertised_parameter_descriptions_contain_no_letter_split_words[None]",
    f"{BUNT}::test_advertised_parameter_descriptions_contain_no_letter_split_words[shot]",
    # --- a driver expression may name frame and its declared variables ---
    f"{ANIMT}::test_a_driver_expression_may_name_frame_and_its_declared_variables",
    f"{ANIMT}::test_a_driver_expression_still_refuses_undeclared_names_and_calls",
    f"{ANIMT}::test_a_scripted_driver_reaches_blender_with_its_frame_expression",
    # --- a cycle reports the period it repeats and where a finite count stops ---
    f"{ANIMT}::test_a_cycle_reports_the_period_each_curve_will_actually_repeat",
    f"{ANIMT}::test_a_finite_cycle_count_says_where_the_repeat_stops",
    f"{POSET}::test_keying_past_a_cycle_says_the_period_it_just_changed",
    f"{POSET}::test_keying_inside_an_existing_cycle_warns_about_nothing",
    f"{POSET}::test_a_whole_rig_keyed_past_its_cycles_counts_the_bones_it_cannot_name",
    f"{ANIMT}::test_an_unscoped_cycle_names_the_parameter_that_narrows_it",
    # --- every reply is bounded by the per-reply byte budget ---
    f"{ENVT}::test_an_oversized_record_page_is_cut_to_the_budget_and_stays_resumable",
    f"{ENVT}::test_a_resumed_page_continues_from_the_offset_it_was_given",
    f"{ENVT}::test_the_largest_list_is_the_one_cut",
    f"{ENVT}::test_a_page_nested_inside_a_single_record_is_found",
    f"{ENVT}::test_an_unpaginated_oversized_list_is_cut_and_says_to_narrow_the_scope",
    f"{ENVT}::test_a_single_record_too_big_for_the_budget_is_still_reported",
    f"{ENVT}::test_the_budget_leaves_a_flat_oversized_reply_alone",
    f"{ENVT}::test_a_reply_within_the_budget_is_sent_whole",
    f"{ENVT}::test_the_keys_the_shortening_adds_are_inside_the_budget_it_measured",
    f"{ENVT}::test_a_page_paged_under_a_prefixed_name_is_still_marked_truncated",
    # --- a light record is trimmed to a listing's facts, with the rest behind detail ---
    f"{LIGHTT}::test_default_light_record_is_identity_plus_the_facts_a_listing_is_asked_for",
    f"{LIGHTT}::test_detail_records_carry_the_state_the_default_record_omits",
    f"{LIGHTT}::test_light_transform_floats_are_rounded_to_six_decimals",
    f"{LIGHTT}::test_light_inventories_trim_by_default_and_restore_full_records_with_detail",
    f"{LIGHTT}::test_preview_matched_state_names_its_lights_instead_of_embedding_them",
    f"{LIGHTT}::test_light_inventory_tools_forward_the_detail_flag",
    f"{LIGHTT}::test_lighting_quality_expands_strict_agent_payload",
    # --- a pose reply names what it changed; the matrices are the part that is trimmed ---
    f"{CTRLT}::test_pose_report_rounds_the_result_and_omits_the_pre_call_matrix",
    f"{CTRLT}::test_pose_detail_restores_the_pre_call_matrix_and_full_precision",
    f"{CTRLT}::test_pose_record_names_the_channels_and_custom_properties_the_call_set",
    f"{CTRLT}::test_the_budget_shortens_pose_records_but_never_the_changed_bone_names",
    f"{CTRLT}::test_keyframed_pose_names_every_bone_and_reports_no_matrices_by_default",
    f"{CTRLT}::test_keyframe_detail_reports_the_pose_that_was_keyed",
    f"{CTRLT}::test_pose_tools_forward_the_detail_flag",
    # --- the pose an agent authors reaches the file, and a child is solved against its parent ---
    # --- naming the bones instead of paging to them ---
    f"{LISTT}::test_named_bones_are_returned_in_one_page_instead_of_paged_to",
    f"{LISTT}::test_an_unknown_bone_name_is_refused_rather_than_silently_dropped",
    f"{LISTT}::test_no_filter_still_lists_every_bone",
    *(
        f"{LISTT}::test_a_malformed_bone_name_filter_is_refused[{case}]"
        for case in ("value0", "value1", "value2", "CHAR1_head_jnt")
    ),
    # --- the rest axes carry their own conclusion, in the vocabulary an aim takes ---
    f"{LISTT}::test_the_rest_axes_are_also_named_in_the_vocabulary_an_aim_takes",
    f"{LISTT}::test_the_up_axis_follows_the_rig_into_the_scene_where_the_nine_numbers_cannot",
    f"{LISTT}::test_a_rig_scaled_to_nothing_names_no_axis_for_any_direction",
    f"{POSET}::test_aim_points_the_named_axis_at_an_object_and_leaves_position_and_scale_alone",
    f"{POSET}::test_aim_at_a_world_point_resolves_through_the_rig_transform",
    f"{POSET}::test_aim_rejects_every_direction_it_cannot_define",
    f"{POSET}::test_posing_an_aim_lands_it_on_the_target_after_the_parent_has_moved",
    f"{POSET}::test_a_minimal_arc_aim_past_the_flip_angle_is_refused_rather_than_rolled_arbitrarily",
    f"{POSET}::test_rotate_resolves_named_axes_and_vectors_in_degrees",
    f"{POSET}::test_relative_rotate_composes_while_the_default_replaces",
    f"{POSET}::test_a_resolved_rotation_keys_only_the_bones_native_channel[QUATERNION-rotation_quaternion]",
    f"{POSET}::test_a_resolved_rotation_keys_only_the_bones_native_channel[XYZ-rotation_euler]",
    f"{POSET}::test_a_resolved_rotation_keys_only_the_bones_native_channel[AXIS_ANGLE-rotation_axis_angle]",
    f"{POSET}::test_an_absolute_space_child_is_resolved_against_the_parent_this_call_moved",
    f"{POSET}::test_a_local_space_pose_is_the_channel_value_whatever_the_parent_did",
    f"{POSET}::test_keying_leaves_the_rig_driven_by_the_action_it_authored",
    f"{POSET}::test_keying_reports_the_action_it_displaced",
    f"{POSET}::test_a_failed_key_hands_the_rig_back_as_it_arrived",
    f"{POSET}::test_keying_an_aim_without_an_up_reference_is_refused",
    f"{POSET}::test_a_keyed_aim_takes_the_short_way_round_from_the_previous_key",
    f"{POSET}::test_a_keyed_euler_aim_stays_on_the_previous_keys_branch",
    f"{LISTT}::test_rest_axes_are_reported_only_when_asked_for",
    # --- the chain, pole and target resolution both reach tools share ---
    f"{REACHT}::test_unbranched_ancestor_chain_stops_before_a_mid_chain_fork",
    f"{REACHT}::test_unbranched_ancestor_chain_stops_before_a_root_level_fork",
    f"{REACHT}::test_unbranched_ancestor_chain_includes_an_unforked_root",
    f"{REACHT}::test_unbranched_ancestor_chain_respects_max_length",
    f"{REACHT}::test_unbranched_ancestor_chain_of_a_root_bone_is_just_that_bone",
    f"{REACHT}::test_rest_ancestor_chain_returns_the_exact_requested_length",
    f"{REACHT}::test_rest_ancestor_chain_refuses_a_length_past_the_root",
    f"{REACHT}::test_synthesize_pole_finds_the_bend_side_of_a_bent_two_bone_chain",
    f"{REACHT}::test_synthesize_pole_refuses_a_straight_two_bone_rest_chain",
    f"{REACHT}::test_synthesize_pole_refuses_a_single_bone_chain",
    f"{REACHT}::test_synthesize_pole_refuses_a_chain_whose_root_and_tip_coincide",
    f"{REACHT}::test_synthesize_pole_converts_through_the_armatures_world_matrix",
    f"{REACHT}::test_resolve_reach_chain_refuses_an_unknown_tip_bone",
    f"{REACHT}::test_resolve_reach_chain_reports_resolved_when_chain_length_is_omitted",
    f"{REACHT}::test_resolve_reach_chain_reports_explicit_when_chain_length_is_given",
    f"{REACHT}::test_resolve_reach_chain_refuses_a_bone_already_claimed_by_an_earlier_reach",
    f"{REACHT}::test_resolved_reach_target_refuses_an_unknown_object_name",
    f"{REACHT}::test_a_reach_whose_pole_cannot_be_resolved_removes_the_targets_scratch_empty",
    f"{REACHT}::test_a_constraint_value_blender_refuses_removes_the_constraint_it_already_added",
    f"{REACHT}::test_bone_reach_requires_exactly_one_target_form",
    f"{REACHT}::test_bone_reach_allows_at_most_one_pole_form",
    f"{REACHT}::test_solve_bone_reach_forwards_reaches_and_omits_unset_optional_fields",
    # --- solve_bone_reach says whether it converged, and why not ---
    f"{REACHT}::test_a_reach_inside_its_tolerance_reports_converged_and_says_nothing_else",
    f"{REACHT}::test_a_tighter_tolerance_turns_the_same_solve_into_a_miss",
    f"{REACHT}::test_a_reachable_target_the_solve_stalled_short_of_warns_without_blaming_the_rig",
    f"{REACHT}::test_a_target_beyond_the_chains_reach_is_reported_as_unreachable",
    f"{REACHT}::test_the_chains_reach_is_measured_in_world_space_not_in_rest_bone_lengths",
    *(
        f"{REACHT}::test_a_tolerance_that_names_no_precision_is_refused_before_the_rig_is_touched[{case}]"
        for case in ("0.0", "-0.0001", "nan", "inf")
    ),
    f"{REACHT}::test_a_missed_reach_still_warns_after_the_envelope_has_shortened_the_reply",
    # --- a reach that raises part way through hands the rig back the action it arrived on ---
    f"{REACHT}::test_a_reach_that_fails_part_way_through_hands_back_the_action_it_arrived_on",
    # --- configure_render_settings answers with the paths it wrote, not the whole state ---
    f"{RENDT}::test_configure_render_settings_returns_only_the_patched_values",
    f"{RENDT}::test_configure_render_settings_detail_returns_both_full_state_blocks",
    f"{RENDT}::test_configure_render_settings_reports_a_patch_that_writes_nothing",
    f"{RENDT}::test_configure_render_settings_forwards_detail",
    # --- place and aim in one call, and refuse a vantage point on the target ---
    f"{CAMT}::test_point_camera_at_places_before_aiming_and_refuses_a_coincident_placement",
    f"{CAMT}::test_handler_point_camera_at_rejects_a_placement_on_the_aim_point",
    # --- an ID holds one action, so assigning one is always also unassigning another ---
    f"{CRTT}::test_keying_a_pose_refuses_to_displace_an_action_that_holds_keys",
    f"{CRTT}::test_confirming_the_displacement_moves_the_rig_onto_the_new_action",
    f"{CRTT}::test_ensure_keys_into_an_existing_action_where_create_refuses_it",
    f"{OANIMT}::test_keyframe_object_transform_refuses_one_action_for_several_objects",
    # --- a transform reply reports the world transform the scene holds, not the pre-edit one ---
    f"{SCENETOOLT}::test_set_object_transform_reports_the_world_transform_the_scene_now_holds",
    # --- the path text a render was written to reads the same frame back ---
    f"{RENDT}::test_inspect_render_output_reads_back_the_tilde_path_a_render_was_written_to",
    # --- one camera marker binds every frame before it, and both marker paths say so ---
    f"{CAMT}::test_camera_markers_warn_that_the_earliest_marker_claims_every_frame_before_it",
    f"{CAMT}::test_setting_the_scene_camera_reports_the_retroactive_binding_in_the_same_words",
    # --- a socket the peer already retired is reconnected, and only for a read-only command ---
    f"{CONNFAILT}::test_a_side_effect_free_command_is_resent_once_on_a_reconnected_socket",
    f"{CONNFAILT}::test_a_mutating_command_is_never_resent_after_the_peer_closed",
    f"{CONNFAILT}::test_a_reply_cut_off_mid_message_is_not_resent_even_for_a_read_only_command",
    # --- a roll about a bone's own length axis is judged by the travel it was measured to cause ---
    f"{POSET}::test_a_roll_that_moves_nothing_measurable_warns_and_quotes_what_it_measured",
    f"{POSET}::test_a_roll_that_swings_an_offset_child_bone_says_nothing",
    f"{POSET}::test_a_roll_that_carries_skinned_vertices_off_the_axis_says_nothing",
    f"{POSET}::test_a_deforming_bone_with_no_reachable_mesh_says_what_it_did_not_measure",
    f"{POSET}::test_a_bounded_vertex_scan_says_its_radius_is_a_floor",
    f"{POSET}::test_a_whole_rig_rolled_about_its_own_length_counts_the_bones_it_cannot_name",
    # --- the probe answers which axis moves a bone, and hands the pose back it borrowed ---
    f"{POSET}::test_the_probe_separates_the_axis_that_swings_a_bone_from_the_one_that_only_rolls_it",
    f"{POSET}::test_the_sign_of_a_reference_component_follows_the_sign_of_the_turn",
    f"{POSET}::test_the_probe_defaults_to_the_farthest_descendant_and_names_how_it_chose",
    f"{POSET}::test_the_probe_hands_the_pose_back_untouched",
    f"{POSET}::test_a_probe_that_raises_part_way_through_still_hands_the_pose_back",
    f"{POSET}::test_a_probe_refuses_a_direction_that_names_no_direction_by_name",
    f"{POSET}::test_a_probe_refuses_a_repeated_axis_and_an_unknown_one_before_touching_the_bone",
    # --- a whole character frames on the meshes its rig deforms, not on the bones ---
    f"{CAMT}::test_handler_framing_expands_an_armature_to_the_meshes_it_deforms",
    f"{CAMT}::test_handler_framing_refuses_every_unresolvable_armature_name_before_touching_the_camera",
    # --- validate_scene pages its findings, and bounds one finding's evidence ---
    f"{SVT}::test_validate_scene_dispatches_scope_max_findings_and_offset",
    f"{SVT}::test_validate_scene_offset_schema_rejects_out_of_range",
    f"{SVT}::test_validate_scene_rejects_out_of_range_offset_before_scanning_anything",
    f"{SVT}::test_validate_scene_separates_a_cut_page_from_a_domain_that_capped_itself",
    f"{SVT}::test_validate_scene_offset_returns_the_next_findings_and_a_matching_next_offset",
    f"{SVT}::test_validate_scene_asks_each_domain_for_enough_findings_to_fill_a_resumed_page",
    f"{SVT}::test_validate_scene_offset_past_the_findings_returns_an_empty_final_page",
    f"{SVT}::test_validate_scene_bounds_one_findings_evidence_without_starving_the_others",
    # --- deleting an object a session made is core work, not an authoring bundle's ---
    f"{BUNT}::test_removing_a_named_object_is_core_not_an_authoring_bundle",
    f"{BUNT}::test_remove_scene_objects_advertises_its_destructiveness_from_the_core_surface",
    # --- a new camera aims at a bone on the rig, and leaves no half-built camera behind ---
    f"{CAMT}::test_create_camera_treats_a_bone_as_a_qualifier_of_its_object_not_a_fifth_source",
    f"{CAMT}::test_handler_create_camera_aims_at_the_named_bone_not_the_rig_origin",
    f"{CAMT}::test_handler_create_camera_refuses_a_bone_the_armature_does_not_have",
    f"{CAMT}::test_handler_create_camera_removes_both_datablocks_when_configuration_is_refused",
    # --- a cycle's period is readable without deleting the cycle to produce it ---
    f"{ANIMT}::test_inspect_reports_the_period_without_destroying_the_cycle",
    f"{ANIMT}::test_inspect_reports_a_curve_that_carries_no_cycle_where_remove_omits_it",
    f"{ANIMT}::test_inspect_reads_the_modifier_that_is_there_not_this_calls_defaults",
    f"{ANIMT}::test_inspect_asserts_an_expected_period_and_still_writes_nothing",
    f"{ANIMT}::test_inspect_says_when_the_selected_curves_do_not_share_one_period",
    f"{ANIMT}::test_the_cycle_tool_lets_inspect_assert_a_period_and_refuses_what_it_cannot_write",
    # --- an edited key that redefines a cycled curve's period says so, once, measured first ---
    f"{ANIMT}::test_an_edited_key_outside_a_cycle_reports_the_period_it_redefines",
    f"{ANIMT}::test_an_edited_key_inside_the_cycle_or_off_a_cycled_curve_stays_quiet",
    f"{ANIMT}::test_the_cycle_notice_is_measured_before_the_batch_starts_inserting",
    # --- an INSPECT cycle call reads, and a probe restores what it borrowed ---
    f"{DRT}::test_the_params_decide_whether_these_commands_read_or_write[set_action_cycle-reading15-writing15]",
    f"{DRT}::test_the_params_decide_whether_these_commands_read_or_write[set_action_cycle-reading16-writing16]",
    f"{DRT}::test_these_commands_run_outside_the_transaction"
    "[probe_bone_axis-restores its own trial pose, so there is no net mutation to snapshot]",
    # --- one pagination primitive and one integer bound for every paged reply ---
    f"{SOIT}::test_get_object_info_resumes_its_type_data_pages_from_the_offset_it_was_given",
    f"{SOIT}::test_get_object_info_reports_the_page_size_it_applied_not_the_one_asked_for",
    f"{RJOBT}::test_a_list_offset_past_the_last_job_reports_the_empty_page_where_the_jobs_end",
    f"{CAMT}::test_setting_the_scene_camera_refuses_a_whole_float_marker_frame_before_binding_anything",
    f"{LIGHTT}::test_a_light_listing_refuses_a_whole_float_page_bound[limit]",
    f"{LIGHTT}::test_a_light_listing_refuses_a_whole_float_page_bound[offset]",
    # --- one RNA patch helper set and one verified cache frame-range write for every simulation ---
    f"{RNAPT}::test_restore_leaves_a_pointer_to_the_caller_that_holds_its_datablock",
    f"{RNAPT}::test_a_vector_read_back_is_its_numbers_not_its_repr",
    f"{LIQUIDT}::test_manage_liquid_cache_refuses_and_undoes_a_frame_range_blender_did_not_keep",
    f"{RBWT}::test_manage_rigid_body_cache_refuses_and_undoes_a_frame_range_blender_did_not_keep",
    # --- a simulation reply counts the bodies, helpers and duplicates it touched ---
    f"{RBWT}::test_a_cache_calculation_counts_its_bodies_and_names_none_of_them_as_changed",
    f"{RBWT}::test_removing_a_rigs_helpers_counts_them_and_names_none_as_a_next_target",
    f"{CLOTHT}::test_a_cloth_variant_counts_its_setup_and_names_the_variant",
    f"{LIQUIDT}::test_a_liquid_variant_counts_members_by_role_and_names_its_domain",
    # --- an edit reaching every user of one datablock counts them and names only the target ---
    f"{CRTT}::test_a_rest_edit_on_widely_shared_armature_data_counts_its_users_and_names_only_the_rig",
    f"{LIGHTT}::test_configuring_a_widely_shared_light_counts_its_users_and_names_only_the_light",
    f"{GNT}::test_patching_a_widely_used_group_counts_its_user_objects_and_changes_only_the_group",
    # --- a bulk call counts what it touched and names only what the caller acts on next ---
    f"{SCENETOOLT}::test_removing_a_managed_rig_counts_its_members_and_names_none_as_a_next_target",
    f"{SCENETOOLT}::test_reset_scene_counts_what_it_unlinked_and_names_the_scene_as_what_changed",
    f"{SCENETOOLT}::test_a_collection_of_many_members_is_counted_and_is_itself_what_changed",
    f"{NDSTATUST}::test_a_large_cleanup_counts_what_went_and_names_only_the_objects_that_lost_a_modifier",
    f"{NDOUTT}::test_nd_outcome_publishes_the_change_list_a_handler_names_itself_in_place_of_the_targets",
    f"{NDOUTT}::test_nd_outcome_cancelled_drops_a_change_list_the_handler_named",
    f"{SRVPHT}::test_a_model_import_reports_the_roots_the_handler_named_not_every_imported_object",
)

# Nodes no single revert can break, each with the reason, so the gap check skips them.
_DOUBLE_DEFENDED = (
    "two independent defences produce the same client-visible outcome here, so no single revert can falsify "
    "it. `strip_unsafe` removes the character, and `_is_admissible_leaf` refuses a leaf that still carries "
    "one - and both end at the same published string (a clean name, or `the requested file`). Measured, not "
    "assumed: with the strip reverted this node passes because the allowlist refuses the leaf, and with the "
    "allowlist reverted it passes because the strip already removed the character. Naming it on either row "
    "would make that row a SURVIVOR, which is the false credit this harness exists to stop. The rows that "
    "*are* falsifiable for each mechanism are 'control characters survive into a client-facing note' (which "
    "names the two params that expect the stripped name) and 'the leaf allowlist goes back to a "
    "three-character blocklist'."
)

_ROOTED_TWICE = (
    "`///Users/...` is refused twice over and neither refusal can be reverted into the other's absence. "
    "`relative_link_body` returns None for a `//` followed by a root, so `blend_files._resolved` calls it "
    "UNRESOLVABLE; without that clause `os.path.join` takes the root as absolute and the containment check "
    "withholds `/Users/...` as OUTSIDE_ROOTS - so reverting either one leaves the link reduced to a leaf and this "
    "node passing. Measured both ways rather than argued. The two answer different questions "
    "(`relative_link_body` also decides `is_relative`; containment decides publication for every path). Each "
    "mechanism is falsifiable through a node that depends on it alone - the root clause through "
    "`test_a_rooted_relative_prefix_is_not_reported_as_relative` and the `///` case of "
    "`test_each_refusal_the_publisher_makes_has_its_own_reason_code`, containment through "
    "`test_a_link_that_climbs_out_of_the_roots_is_withheld_as_outside_roots`."
)

NOT_INDIVIDUALLY_FALSIFIABLE: dict[str, str] = {
    f"{REGT}::test_target_names_reads_the_naming_convention[nothing-named]": (
        "it is the empty-input control for the eleven parametrized cases beside it: no line of "
        "`target_names` can be reverted so that a params dict naming nothing yields a name, because "
        "every branch there is a lookup keyed on a param the dict does not carry. Its job is the "
        "opposite direction - it stops the collecting rows being satisfied by a `target_names` that "
        "returned a constant, which is what the reverts for `scalar-key`, `list-key` and "
        "`records-in-a-list` would otherwise be free to do."
    ),
    f"{AUTHT}::test_the_snapshot_cannot_be_used_to_edit_the_ledger": (
        "the ledger stores `(collection, name)` tuples and `snapshot` builds a fresh dict per entry on "
        "every call, so the copy is a property of the data structure rather than of a line: no single "
        "reversal hands a caller the ledger's own objects without also changing what the ledger stores. "
        "The invariant it guards - that a caller holding a reply cannot rewrite what the saved .blend "
        "will claim - is why the entries are tuples, and the entries themselves are read back by "
        "`test_records_arrive_oldest_first_and_a_repeat_is_one_datablock`, which a row does falsify."
    ),
    f"{ENVT}::test_a_reply_within_the_budget_is_sent_whole": (
        "the early return in `_fit_budget` is a performance guard, not a decision: the loop below it "
        "re-measures and returns before shortening anything, so a within-budget reply is left whole "
        "either way. Measured on a pristine checkout, not argued - the row that claimed this node was "
        "a SURVIVOR there too. What the guard buys is one skipped encode per reply, which no assertion "
        "about reply content can see; the shortening itself is falsifiable through "
        "`test_an_oversized_record_page_is_cut_to_the_budget_and_stays_resumable`."
    ),
    f"{AMT}::test_every_handshake_field_refuses_the_same_hostile_string[protocol_version]": (
        "the field is `int | None` by construction and the only revert that reaches it takes the whole "
        "handshake down a different path. Measured: replacing the `int()` parse with `protocol_i = protocol` "
        "makes `protocol_i >= EXPECTED_ADDON_PROTOCOL_VERSION` raise `TypeError` on a string payload, "
        "`handshake_addon`'s outer `except Exception` returns the degraded handshake whose every field is "
        "None, and this node then passes on an empty result rather than on the parse - a SURVIVOR for a "
        "reason that has nothing to do with the behaviour it names. It is kept in the parametrization "
        "because that list is derived from `dataclasses.fields(AddonHandshake)`, which is the point of it: "
        "the case exists so that a future change publishing this field raw is caught, not because a revert "
        "can reach it today. The sibling fields that a single revert *can* reach each have a row."
    ),
    f"{AMT}::test_every_handshake_field_refuses_the_same_hostile_string[capability_params]": (
        "the sweep hands every field the same hostile *string*, and this field is a dict by "
        "construction: `normalized_capability_params` returns `{}` for any non-dict, so the node "
        "passes whatever the per-entry sanitisation does. Measured, not argued - replacing "
        "`normalized_session_text_list(params, ...)` with `list(params)` leaves this case green "
        "while `test_capability_params_drops_a_hostile_parameter_name_inside_the_list` fails, so "
        "naming it on that row made the row a SURVIVOR. It is kept in the parametrization because "
        "the list is derived from `dataclasses.fields(AddonHandshake)`: the case exists to catch a "
        "future change that publishes this field raw. The sanitisation a revert *can* reach is "
        "falsifiable through the two dedicated `capability_params` nodes."
    ),
    f"{CAPT}::test_self_is_never_reported_for_a_bound_method": (
        "nothing in `capability_params` excludes `self`: `inspect.signature` on an already-bound "
        "method does not show it at all, which is the fact this node pins. There is no line to "
        "revert - the assertion guards the shape of the input `_build_command_handlers()` hands in "
        "(bound methods, never unbound functions), so it would only fail if that call site changed "
        "to pass classes or unbound functions. The reporting rules a revert reaches - sorting, the "
        "`**kwargs` sentinel, positional-only exclusion, and the unreadable-signature fallback - "
        "each have their own row."
    ),
    f"{POSET}::test_a_local_space_pose_is_the_channel_value_whatever_the_parent_did": (
        "it is the control for the row above it: LOCAL is `matrix_basis`, which is parent-relative by "
        "construction, so no revert of the apply-time resolution can move it. Measured: setting "
        '`_PARENT_RELATIVE_SPACE = "POSE"` sends a LOCAL entry down the deferred path that the '
        "absolute spaces take, and the node still passes, because resolving `matrix_basis` before or "
        "after the parent moves gives the same answer. That is the claim - the fix for the absolute "
        "spaces had to leave LOCAL alone, and this node is what says so; the sibling node for the "
        "absolute spaces is the one a revert reaches."
    ),
    f"{STRICTT}::test_a_valid_call_still_validates_through_its_nested_model": (
        "`extra='forbid'` only ever adds a refusal, so no revert of the hardening can stop a "
        "well-formed call validating. Measured, not argued: with `forbid_unknown_tool_arguments(mcp)` "
        "neutralised the file reports `4 failed, 2 passed` and this is one of the two, because the "
        "nested `CameraOpticsPatch` is generated by the SDK from the handler's signature and owes "
        "nothing to the config key `_strict_args` writes. It is the control for its four siblings: it "
        "exists to catch a hardening pass that rebuilt the top-level model badly enough to lose the "
        "nested patch type, which is a change to `_strict_args`, not a revert of it."
    ),
    f"{STRICTT}::test_the_full_catalog_is_what_the_hardening_was_measured_against": (
        "a guard on its sibling's method, like the `-O` flag node below: it asserts that `all` really "
        "is a strictly wider catalog than the default selection, so the catalog-wide sweep above it "
        "cannot pass by measuring the same small surface twice. Nothing in `_strict_args` can move a "
        "set comparison between two toolset selections - it is falsified only by a change to "
        "`bundles.py`, whose own selection rules have rows of their own."
    ),
    f"{SURFT}::test_committed_surface_matches_the_live_dispatch_table": (
        "it compares two artefacts - the `commands` block committed in `addon_surface.json` and the "
        "table the bundled add-on builds - and the fix *is* that data, so there is no line of it to "
        "revert. Making the two disagree means either editing the committed JSON, which is data rather "
        "than behaviour, or deleting a handler from the dispatch table, which would prove that some "
        "unrelated command exists and nothing about drift detection. Measured: the protocol row leaves "
        "this node green because it only moves `protocol_version`. The mechanism it shares with the "
        "rest of the file - that the snapshot is regenerated in one fixed shape and pinned to a "
        "protocol number - is falsifiable through its three siblings, which the two `addon surface:` "
        "rows break."
    ),
    "tests/test_session_state.py::test_a_recorded_failure_names_one_bounded_leaf_and_nothing_else[newline]": (
        _DOUBLE_DEFENDED
    ),
    "tests/test_session_state.py::test_a_recorded_failure_names_one_bounded_leaf_and_nothing_else[ansi-escape]": (
        _DOUBLE_DEFENDED
    ),
    HOSTILE_LIB + "[line separator-/mnt/studio/a\\u2028b.blend-forbidden5]": _DOUBLE_DEFENDED,
    HOSTILE_LIB + "[bidi override-/mnt/studio/\\u202edneb.live\\u202c.blend-forbidden7]": _DOUBLE_DEFENDED,
    HOSTILE_LIB + "[rooted relative prefix-///Users/victim/clients/acme-merger/lib/canon.blend-forbidden10]": (
        _ROOTED_TWICE
    ),
    "tests/test_session_state.py::test_a_published_link_is_never_an_absolute_path": _ROOTED_TWICE,
    # These three pin behaviour outside this repository that `cli.py`'s docstrings rely
    # on. No edit to `cli.py` can move them, so a revert row would prove nothing.
    "tests/server/test_cli_transport.py::test_a_remote_host_header_is_refused_by_the_running_app": (
        "characterises FastMCP's TransportSecurityMiddleware, not this repo's code: it pins the 421 that "
        "_serve_http's docstring cites. It fails if `mcp` changes or `app.py` stops configuring transport "
        "security, which is the regression worth catching, and neither is reachable from cli.py."
    ),
    "tests/server/test_cli_transport.py::test_a_forged_loopback_host_header_is_served_so_it_is_no_access_control": (
        "same subject, opposite direction: it pins that a forged `Host: 127.0.0.1` IS served, which is the "
        "evidence for the docstring's claim that Host validation is browser-oriented and not an access "
        "control. Falsified only by a change in `mcp`, never by one in cli.py."
    ),
    "tests/server/test_cli_transport.py::test_the_o_flag_really_is_in_effect_for_that_check": (
        "a guard on its sibling's method, not on production code: it asserts the subprocess really did run "
        "under `python -O`, so that the -O test cannot pass because the flag silently stopped applying. "
        "Nothing in cli.py can make it fail."
    ),
}


# The table, in the order its rows were written: the order `--list` prints and a run takes.
REVERTS: list[Revert] = [
    *drain_and_viewport.ROWS,
    *rig.ROWS,
    *docker.ROWS,
    *output_roots.ROWS,
    *session.ROWS,
    *barrier.ROWS,
    *transport.ROWS,
    *teardown.ROWS,
    *session_boundary.ROWS,
    *load_stamp.ROWS,
    *handshake.ROWS,
    *transaction.ROWS,
    *file_paths.ROWS,
    *install.ROWS,
    *error_paths.ROWS,
    *file_lifecycle.ROWS,
    *linking.ROWS,
    *server_tools.ROWS,
    *object_lookup.ROWS,
    *reply_shape.ROWS,
    *pose.ROWS,
    *artefact_truth.ROWS,
    *registry.ROWS,
    *render_coverage.ROWS,
    *argument_gates.ROWS,
    *keyframing.ROWS,
    *camera.ROWS,
    *animation.ROWS,
    *status_and_sampling.ROWS,
    *pagination.ROWS,
    *simulation.ROWS,
    *data_users.ROWS,
    *counted_replies.ROWS,
    *lint_gate.ROWS,
]


def collected_nodes() -> list[str]:
    """
    Ask pytest which nodes NEW_TEST_FILES collect.

    Returns:
        list[str]: Every collected node id, plus NEW_NODES_IN_EXISTING_FILES.

    Raises:
        SystemExit: If collection failed, which would make coverage meaningless.

    """
    result = subprocess.run(
        [".venv/bin/python", "-m", "pytest", "-q", "--collect-only", "-p", "no:cacheprovider", *NEW_TEST_FILES],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"collection failed:\n{result.stdout}\n{result.stderr}")
    nodes = [line.strip() for line in result.stdout.splitlines() if "::test_" in line]
    return nodes + list(NEW_NODES_IN_EXISTING_FILES)


def coverage_gaps() -> list[str]:
    """
    List nodes that neither a revert nor a written-down exception accounts for.

    Returns:
        list[str]: The uncovered node ids, sorted.

    """
    covered = {node for revert in REVERTS for node in revert.nodes}
    return sorted(set(collected_nodes()) - covered - set(NOT_INDIVIDUALLY_FALSIFIABLE))


def _invalidate_bytecode(path: pathlib.Path) -> None:
    r"""
    Drop the cached bytecode for a file this harness just rewrote.

    CPython keys a `__pycache__` entry on the source's mtime in whole seconds and
    its size. Two rows that change a module by the same length within one second
    would otherwise run the first row's bytecode, and the second would report
    SURVIVOR without testing its edit.

    Args:
        path: The source file whose cached bytecode must go.

    """
    cache = path.parent / "__pycache__"
    if not cache.is_dir():
        return
    for compiled in cache.glob(f"{path.stem}.*.pyc"):
        compiled.unlink(missing_ok=True)


def apply(revert: Revert) -> str | None:
    """
    Write the reverted file and return what was there before.

    Args:
        revert: The revert to apply.

    Returns:
        str | None: The original contents, or None if the file did not exist.

    Raises:
        SystemExit: If the anchor text is not in the file.

    """
    original = revert.path.read_text(encoding="utf-8") if revert.path.exists() else None
    if revert.old is None:
        revert.path.write_text((original or "") + revert.new, encoding="utf-8")
        _invalidate_bytecode(revert.path)
        return original
    if original is None or revert.old not in original:
        raise SystemExit(f"anchor not found for {revert.label!r} in {revert.path}")
    revert.path.write_text(original.replace(revert.old, revert.new, 1) + revert.also, encoding="utf-8")
    _invalidate_bytecode(revert.path)
    return original


def restore(revert: Revert, original: str | None) -> None:
    """
    Put the file back exactly as it was.

    Args:
        revert: The revert that was applied.
        original: What `apply` returned.

    """
    if original is None:
        revert.path.unlink(missing_ok=True)
    else:
        revert.path.write_text(original, encoding="utf-8")
    _invalidate_bytecode(revert.path)


def run_nodes(nodes: tuple[str, ...]) -> tuple[bool, str]:
    """
    Run exactly the named nodes and report whether every one of them failed.

    The exit code cannot say that. A revert that breaks parsing gives a collection
    error, not a failure of the behaviour the row names, so any error counts as not
    caught. A row naming several nodes needs all of them to fail, not just one.

    Args:
        nodes: Node ids to run.

    Returns:
        tuple[bool, str]: Whether all the named nodes failed for a reason that
            counts, and pytest's summary line.

    """
    result = subprocess.run(
        [".venv/bin/python", "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider", *nodes],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    summary = [line for line in result.stdout.splitlines() if " passed" in line or " failed" in line]
    tail = summary[-1] if summary else (result.stdout.strip().splitlines() or ["no output"])[-1]
    return _every_node_failed(tail, len(nodes)), tail


def _every_node_failed(tail: str, expected: int) -> bool:
    """
    Read pytest's summary line and decide whether the revert was really caught.

    Args:
        tail: pytest's last summary line, e.g. "2 failed, 1 passed in 0.10s".
        expected: How many nodes the row named.

    Returns:
        bool: True only when the line reports `expected` failures, no errors and
            nothing passed.

    """
    if re.search(r"\d+ error", tail):
        return False
    if re.search(r"\d+ passed", tail):
        return False
    failed = re.search(r"(\d+) failed", tail)
    return failed is not None and int(failed.group(1)) == expected


@dataclass
class Report:
    """
    What one run of the matrix found.

    Attributes:
        survivors: Labels of reverts no named node noticed.
        gaps: Node ids nothing accounts for.

    """

    survivors: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)


def main(argv: list[str] | None = None) -> int:
    """
    Run the matrix, or just print its coverage map.

    Args:
        argv: Arguments to parse; None means `sys.argv`.

    Returns:
        int: 0 when every revert broke its own nodes and nothing is uncovered.

    """
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true", help="print the coverage map and exit")
    parser.add_argument("--only", default="", help="run only reverts whose label contains this substring")
    arguments = parser.parse_args(argv)

    report = Report(gaps=coverage_gaps())
    if arguments.list:
        for revert in REVERTS:
            print(f"{revert.label}")
            for node in revert.nodes:
                print(f"    node: {node}")
        for node, reason in sorted(NOT_INDIVIDUALLY_FALSIFIABLE.items()):
            print(f"[not individually falsifiable] {node}\n    reason: {reason}")
        print(f"\nreverts: {len(REVERTS)}; uncovered nodes: {len(report.gaps)}")
        for node in report.gaps:
            print(f"    UNCOVERED {node}")
        return 1 if report.gaps else 0

    # A full run is hundreds of pytest runs; the load stamps show whether a survivor
    # may be a busy machine's timing artefact.
    print(quiet_box.stamp("before"))

    for revert in REVERTS:
        if arguments.only and arguments.only not in revert.label:
            continue
        original = apply(revert)
        try:
            broke, tail = run_nodes(revert.nodes)
        finally:
            restore(revert, original)
        if not broke:
            report.survivors.append(revert.label)
        print(f"[{'FAILS as required' if broke else 'SURVIVED THE REVERT'}] {revert.label}")
        for node in revert.nodes:
            print(f"    node: {node}")
        print(f"    pytest: {tail}")

    print(f"\n{quiet_box.stamp('after')}")
    print(f"\nreverts run: {len(REVERTS) if not arguments.only else 'subset'}")
    print(f"reverts that failed to break their own nodes: {len(report.survivors)}")
    for label in report.survivors:
        print(f"    SURVIVOR {label}")
    print(f"new test nodes with no revert and no written-down reason: {len(report.gaps)}")
    for node in report.gaps:
        print(f"    UNCOVERED {node}")
    return 1 if report.survivors or report.gaps else 0


if __name__ == "__main__":
    sys.exit(main())
