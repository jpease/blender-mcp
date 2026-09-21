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
`camera:` and `boundary:`. A `... control:` row is the deliberate opposite of its
neighbour: it proves that over-enforcing the same line is caught too, either by the
same node or by the sibling node that exists to say the guard can be passed.

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

# Importable because `scripts/` is sys.path[0] when this file or `check_revert_anchors.py`
# runs as a script. Code outside `scripts/` uses `quiet_box.load_quiet_box`.
import quiet_box

ROOT = pathlib.Path(__file__).resolve().parents[1]
RIG = ROOT / "scripts/blender_rig.py"
ADDON_MANAGER = ROOT / "src/blender_mcp/addon_manager.py"
ADDON_OUTPUT_ROOTS = ROOT / "src/blender_mcp/bundled/addon/output_roots.py"
ADDON_FILE_PATHS = ROOT / "src/blender_mcp/bundled/addon/file_paths.py"
ADDON_LIBRARY_DIGEST = ROOT / "src/blender_mcp/bundled/addon/library_digest.py"
ADDON_POLYHAVEN = ROOT / "src/blender_mcp/bundled/addon/handlers/polyhaven.py"
ADDON_SERVER_CORE = ROOT / "src/blender_mcp/bundled/addon/server_core.py"
ADDON_CAPABILITY_INTROSPECTION = ROOT / "src/blender_mcp/bundled/addon/capability_introspection.py"
ADDON_KEY_STYLE = ROOT / "src/blender_mcp/bundled/addon/handlers/key_style.py"
SERVER_CORE_TOOL = ROOT / "src/blender_mcp/server/tools/core.py"
SERVER_CONNECTION = ROOT / "src/blender_mcp/server/connection.py"
ADDON_SESSION = ROOT / "src/blender_mcp/bundled/addon/session.py"
ADDON_TRANSACTION = ROOT / "src/blender_mcp/bundled/addon/transaction.py"
ADDON_AUTHORED = ROOT / "src/blender_mcp/bundled/addon/authored.py"
ADDON_OBJECT_STATE = ROOT / "src/blender_mcp/bundled/addon/object_state.py"
ADDON_TEXT_HYGIENE = ROOT / "src/blender_mcp/bundled/addon/text_hygiene.py"
SERVER_TEXT_HYGIENE = ROOT / "src/blender_mcp/text_hygiene.py"
ADDON_FILE_LIFECYCLE = ROOT / "src/blender_mcp/bundled/addon/handlers/file_lifecycle.py"
# What every command touching a `.blend` on disk shares (paths, flags, library summaries,
# operator failures), and the provenance block a save stamps and a delivery scan reads back.
ADDON_BLEND_FILES = ROOT / "src/blender_mcp/bundled/addon/handlers/blend_files.py"
ADDON_PROVENANCE = ROOT / "src/blender_mcp/bundled/addon/handlers/provenance.py"
ADDON_LINKING = ROOT / "src/blender_mcp/bundled/addon/handlers/linking.py"
ADDON_VIEWPORT = ROOT / "src/blender_mcp/bundled/addon/handlers/viewport.py"
# Server-side tool wrappers and their registration/documentation surface.
SERVER_FILE_LIFECYCLE_TOOL = ROOT / "src/blender_mcp/server/tools/file_lifecycle.py"
# The one socket round trip every server-side tool makes.
SERVER_DISPATCH = ROOT / "src/blender_mcp/server/tools/_dispatch.py"
# `save_shot.create_directories` and name resolution after a library override.
ADDON_OBJECT_LOOKUP = ROOT / "src/blender_mcp/bundled/addon/object_lookup.py"
ADDON_CANDIDATES = ROOT / "src/blender_mcp/bundled/addon/candidates.py"
SERVER_APP = ROOT / "src/blender_mcp/server/app.py"
ADDON_SCENE = ROOT / "src/blender_mcp/bundled/addon/handlers/scene.py"
ADDON_ANIMATION = ROOT / "src/blender_mcp/bundled/addon/handlers/animation.py"
SERVER_ANIMATION_TOOL = ROOT / "src/blender_mcp/server/tools/animation.py"
# The object-keyframing handler and the action-assignment module it and the posing handler
# both key through: one ID holds one action, so both tools make the same mistake.
ADDON_OBJECT_ANIMATION = ROOT / "src/blender_mcp/bundled/addon/handlers/object_animation.py"
ADDON_ACTION_ASSIGNMENT = ROOT / "src/blender_mcp/bundled/addon/handlers/action_assignment.py"
SERVER_ENVELOPE = ROOT / "src/blender_mcp/server/tools/envelope.py"
# The lighting, posing and render-settings handlers the reply-shape work reshaped, and the
# server-side wrappers that carry their `detail` flag across the socket.
ADDON_LIGHTING_SHARED = ROOT / "src/blender_mcp/bundled/addon/handlers/lighting/_shared.py"
ADDON_LIGHTING_INSPECTION = ROOT / "src/blender_mcp/bundled/addon/handlers/lighting/inspection.py"
ADDON_LIGHTING_RENDERING = ROOT / "src/blender_mcp/bundled/addon/handlers/lighting/rendering.py"
SERVER_LIGHTING_INSPECTION_TOOL = ROOT / "src/blender_mcp/server/tools/lighting/inspection.py"
SERVER_LIGHTING_RENDERING_TOOL = ROOT / "src/blender_mcp/server/tools/lighting/rendering.py"
ADDON_CR_FOUNDATION = ROOT / "src/blender_mcp/bundled/addon/handlers/character_rigging/foundation.py"
ADDON_POSING = ROOT / "src/blender_mcp/bundled/addon/handlers/character_rigging/posing.py"
SERVER_POSING_TOOL = ROOT / "src/blender_mcp/server/tools/character_rigging/posing.py"
# Camera aiming, on both sides of the socket: the server wrapper preflights a placement it can
# resolve without a round trip, and the handler repeats the check against the resolved target.
ADDON_CAMERA_TARGETING = ROOT / "src/blender_mcp/bundled/addon/handlers/camera/targeting.py"
SERVER_CAMERA_TARGETING_TOOL = ROOT / "src/blender_mcp/server/tools/camera/targeting.py"
ADDON_RENDERING = ROOT / "src/blender_mcp/bundled/addon/handlers/rendering.py"
ADDON_DELIVERY = ROOT / "src/blender_mcp/bundled/addon/handlers/delivery.py"
RENDER_COVERAGE_SCRIPT = ROOT / "scripts/render_coverage.py"
SERVER_RENDERING_TOOL = ROOT / "src/blender_mcp/server/tools/rendering.py"
SERVER_DOCUMENTATION = ROOT / "src/blender_mcp/server/tools/_documentation.py"
# Where the tool catalog is registered, and where the hardening pass that follows the
# registration imports is called from.
SERVER_TOOLS_INIT = ROOT / "src/blender_mcp/server/tools/__init__.py"
SERVER_BUNDLES = ROOT / "src/blender_mcp/server/bundles.py"
TEST_BUNDLES_FILE = ROOT / "tests/server/test_bundles.py"
# `scripts/update_addon_surface.py` imports the snapshot's builder and serializer from this
# test module, so the generator and the assertion are one code path and a row reverting it
# reverts what `just addon-surface` writes.
TEST_ADDON_SURFACE_FILE = ROOT / "tests/test_addon_surface.py"
ADDON_INIT = ROOT / "src/blender_mcp/bundled/addon/__init__.py"
TEST_THREADING_FILE = ROOT / "tests/server/test_threading.py"
SERVER_CLI = ROOT / "src/blender_mcp/server/cli.py"
PACKAGE_INIT = ROOT / "src/blender_mcp/__init__.py"
SCRIPTS_INIT = ROOT / "scripts/__init__.py"
QUIET_BOX = ROOT / "scripts/quiet_box.py"
PYPROJECT = ROOT / "pyproject.toml"
DOCKERFILE = ROOT / "docker/blender/Dockerfile"
DOCKERIGNORE = ROOT / "docker/blender/Dockerfile.dockerignore"
COMPOSE = ROOT / "docker/blender/docker-compose.yml"
ENTRYPOINT = ROOT / "docker/blender/entrypoint.sh"
HEALTHCHECK = ROOT / "docker/blender/healthcheck.py"
DOCKER_START = ROOT / "docker/blender/start_server.py"

# Short names for the test files rows cite. Node ids carry parameter text
# verbatim, so the rows would otherwise be unreadably long lines.
RIGT = "tests/test_blender_rig.py"
DOCKT = "tests/test_docker_rig.py"
ROOTST = "tests/test_output_roots.py"
FPT = "tests/test_file_paths.py"
PHT = "tests/test_polyhaven_blend_guard.py"
FLT = "tests/test_file_lifecycle_handlers.py"
LKT = "tests/test_linking_handlers.py"
CORET = "tests/server/tools/test_core.py"
CLIT = "tests/server/test_cli_transport.py"
# Server-side tool wrapper tests. Named apart from FLT (the addon/bpy-level handler
# tests): same subject, different layer.
SFLT = "tests/server/tools/test_file_lifecycle.py"
BUNT = "tests/server/test_bundles.py"
TDT = "tests/server/test_tool_documentation.py"
ENVT = "tests/server/tools/test_envelope.py"
ANIMT = "tests/test_animation_tools.py"
OLT = "tests/test_object_lookup.py"
CANDT = "tests/test_candidates.py"
SIT = "tests/server/test_server_instructions.py"
SOIT = "tests/server/tools/test_scene_object_inspection.py"
AMT = "tests/test_addon_manager.py"
SURFT = "tests/test_addon_surface.py"
STRICTT = "tests/server/test_strict_tool_args.py"
# The reply-shape tests for lighting, posing and render settings, in files this matrix
# does not own: their nodes are listed in NEW_NODES_IN_EXISTING_FILES.
LIGHTT = "tests/server/tools/lighting/test_tools.py"
CTRLT = "tests/server/tools/character_rigging/test_controls.py"
POSET = "tests/server/tools/character_rigging/test_posing.py"
CRFT = "tests/server/tools/character_rigging/test_foundation.py"
RENDT = "tests/test_rendering_tools.py"
VIEWT = "tests/server/tools/test_viewport.py"
SVT = "tests/server/tools/test_scene_validate.py"
DRT = "tests/server/test_dispatch_rules.py"
RCT = "tests/test_render_coverage.py"
# The camera, character-rigging and object-keyframing tool tests this matrix does not own
# either; the place-and-aim and action-assignment nodes are listed one by one below.
CAMT = "tests/server/tools/camera/test_tools.py"
CRTT = "tests/server/tools/character_rigging/test_tools.py"
OANIMT = "tests/server/tools/test_object_animation.py"
# Named because inline it passes the line limit, and `ruff format` rejoins a split f-string.
_LIST_SCALAR = "test_a_string_where_a_list_belongs_is_not_iterated_character_by_character"
# The two table tests over `file_paths`' pure verdicts: their parameter ids are readable,
# which makes the full node ids too long to inline.
_OPEN_VERDICT = f"{FPT}::test_the_open_verdict_is_decided_from_facts_alone"
_SAVE_VERDICT = f"{FPT}::test_the_save_verdict_holds_when_the_caller_opted_into_creating_directories"
SESSIONT = "tests/test_session_state.py"
TSWAPT = "tests/test_transaction_session_swap.py"
MUTT = "tests/test_mutation_transaction.py"
# The two files this wave's command-registry work added: the dispatch/classification
# gate itself, and the bounded provenance ledger `save_shot` writes into the .blend.
REGT = "tests/test_command_registry.py"
AUTHT = "tests/test_authored_ledger.py"
QBT = "tests/test_quiet_box.py"
THREADT = "tests/server/test_threading.py"
CONNT = "tests/server/test_connection_framing.py"
CAPT = "tests/test_capability_introspection.py"
KEYSTYLET = "tests/test_key_style.py"
DISPT = "tests/server/tools/test_dispatch.py"
HOSTILE_LIB = f"{SESSIONT}::test_a_hostile_library_path_is_reduced_the_same_way_a_failure_note_is"
# The `name` half of the same table, with short ids so a row can list its nodes; the
# `filepath` half's ids run to hundreds of characters.
HOSTILE_LIB_NAME = f"{SESSIONT}::test_a_hostile_library_name_is_reduced_to_a_leaf_like_the_filepath_is"
HOSTILE_LIB_NAME_IDS = (
    "traversal out of the shot",
    "ANSI escape, relative branch",
    "ANSI escape, absolute branch",
    "500 characters, relative branch",
    "500 characters, absolute branch",
    "line separator",
    "fullwidth solidus",
    "bidi override",
    "nfkc-backslash (U+FE68)",
    "big solidus (U+29F8)",
    "rooted relative prefix",
    "zero-width-hidden traversal",
    "format character inside a component",
    "name is an absolute path",
    "name traverses out of the shot",
    "name is a Windows path",
)
EVASION = f"{THREADT}::test_the_producer_scan_catches_every_shape_that_evaded_it"
# Named because inline the node id passes the line limit.
NFKC_BACKSLASH_LIB = (
    f"{HOSTILE_LIB}[nfkc-backslash (U+FE68)-//..\\ufe68..\\ufe68clients\\ufe68acme\\ufe68canon.blend-forbidden8]"
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
)
# Nodes in files the matrix does not own. `coverage_gaps()` sees only these and the nodes
# collected from NEW_TEST_FILES, so a node left off this list is never checked.
NEW_NODES_IN_EXISTING_FILES = (
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
    f"{CONNT}::test_a_refresh_that_fails_leaves_the_staleness_signal_standing",
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
    *(f"{AMT}::{_LIST_SCALAR}[{field}]" for field in ("capabilities", "writable_output_roots")),
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
    f"{POSET}::test_named_bones_are_returned_in_one_page_instead_of_paged_to",
    f"{POSET}::test_an_unknown_bone_name_is_refused_rather_than_silently_dropped",
    f"{POSET}::test_no_filter_still_lists_every_bone",
    *(
        f"{POSET}::test_a_malformed_bone_name_filter_is_refused[{case}]"
        for case in ("value0", "value1", "value2", "CHAR1_head_jnt")
    ),
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
    f"{POSET}::test_rest_axes_are_reported_only_when_asked_for",
    # --- the chain, pole and target resolution both reach tools share ---
    f"{POSET}::test_unbranched_ancestor_chain_stops_before_a_mid_chain_fork",
    f"{POSET}::test_unbranched_ancestor_chain_stops_before_a_root_level_fork",
    f"{POSET}::test_unbranched_ancestor_chain_includes_an_unforked_root",
    f"{POSET}::test_unbranched_ancestor_chain_respects_max_length",
    f"{POSET}::test_unbranched_ancestor_chain_of_a_root_bone_is_just_that_bone",
    f"{POSET}::test_rest_ancestor_chain_returns_the_exact_requested_length",
    f"{POSET}::test_rest_ancestor_chain_refuses_a_length_past_the_root",
    f"{POSET}::test_synthesize_pole_finds_the_bend_side_of_a_bent_two_bone_chain",
    f"{POSET}::test_synthesize_pole_refuses_a_straight_two_bone_rest_chain",
    f"{POSET}::test_synthesize_pole_refuses_a_single_bone_chain",
    f"{POSET}::test_synthesize_pole_refuses_a_chain_whose_root_and_tip_coincide",
    f"{POSET}::test_synthesize_pole_converts_through_the_armatures_world_matrix",
    f"{POSET}::test_resolve_reach_chain_refuses_an_unknown_tip_bone",
    f"{POSET}::test_resolve_reach_chain_reports_resolved_when_chain_length_is_omitted",
    f"{POSET}::test_resolve_reach_chain_reports_explicit_when_chain_length_is_given",
    f"{POSET}::test_resolve_reach_chain_refuses_a_bone_already_claimed_by_an_earlier_reach",
    f"{POSET}::test_resolved_reach_target_refuses_an_unknown_object_name",
    f"{POSET}::test_a_reach_whose_pole_cannot_be_resolved_removes_the_targets_scratch_empty",
    f"{POSET}::test_a_constraint_value_blender_refuses_removes_the_constraint_it_already_added",
    f"{POSET}::test_bone_reach_requires_exactly_one_target_form",
    f"{POSET}::test_bone_reach_allows_at_most_one_pole_form",
    f"{POSET}::test_solve_bone_reach_forwards_reaches_and_omits_unset_optional_fields",
    # --- solve_bone_reach says whether it converged, and why not ---
    f"{POSET}::test_a_reach_inside_its_tolerance_reports_converged_and_says_nothing_else",
    f"{POSET}::test_a_tighter_tolerance_turns_the_same_solve_into_a_miss",
    f"{POSET}::test_a_reachable_target_the_solve_stalled_short_of_warns_without_blaming_the_rig",
    f"{POSET}::test_a_target_beyond_the_chains_reach_is_reported_as_unreachable",
    f"{POSET}::test_the_chains_reach_is_measured_in_world_space_not_in_rest_bone_lengths",
    *(
        f"{POSET}::test_a_tolerance_that_names_no_precision_is_refused_before_the_rig_is_touched[{case}]"
        for case in ("0.0", "-0.0001", "nan", "inf")
    ),
    f"{POSET}::test_a_missed_reach_still_warns_after_the_envelope_has_shortened_the_reply",
    # --- a reach that raises part way through hands the rig back the action it arrived on ---
    f"{POSET}::test_a_reach_that_fails_part_way_through_hands_back_the_action_it_arrived_on",
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
    "`relative_link_body` returns None for a `//` followed by a root, and `safe_relative_link`'s own loop "
    "then refuses the empty first component the same string produces - so reverting either clause leaves the "
    "link reduced to a leaf and this node passing. Measured both ways rather than argued. The redundancy is "
    "kept deliberately: this is the boundary that failed three cycles running, and the two clauses answer "
    "different questions (`relative_link_body` decides `is_relative`, the loop decides publication). Each "
    "mechanism is falsifiable through a node that depends on it alone - the root clause through "
    "`test_a_rooted_relative_prefix_is_not_reported_as_relative`, the component loop through "
    "`test_a_link_published_whole_names_nothing_above_its_own_shot`."
)

# The type-dispatched fallback table, and the reverted form that generates no semantics at all.
TYPE_DISPATCHED_DESCRIPTIONS = """    match _primary_type(schema):
        case "boolean":
            return _boolean_description(name)
        case "string":
            return _string_description(name)
        case "array":
            return _sequence_description(name)
        case "integer" | "number":
            return _numeric_description(name)
        case _:
            return None"""

DROPPED_DESCRIPTION_SEMANTICS = "    return None"

# Reverted: each nested model's own prose spliced into every one of its parameters, which is where
# "e e v e e" and "n l a" came from.
SPLICED_MODEL_CONTEXT = '''    for definition in schema.get("$defs", {}).values():
        if isinstance(definition, dict):
            _describe_schema(definition)
            source = definition.get("description") or definition.get("title") or "input"
            spaced = re.sub(r"(?<!^)(?=[A-Z])", " ", source.rstrip(".")).lower()
            for property_schema in (definition.get("properties") or {}).values():
                if isinstance(property_schema, dict):
                    property_schema["description"] = f"Numeric value for {spaced}."'''

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


@dataclass(frozen=True)
class Revert:
    """
    One reverted behaviour and the test nodes that must notice.

    Attributes:
        label: What is being undone, behind the prefix naming the area it guards.
        path: File to edit.
        old: Text to replace; None means "append `new`", creating the file if needed.
        new: Replacement text.
        nodes: Node ids expected to fail, and the only ones run.
        also: Extra text appended to the same file alongside the replacement.

    """

    label: str
    path: pathlib.Path
    old: str | None
    new: str
    nodes: tuple[str, ...]
    also: str = ""


# Appended by the library-name revert. The helper imports `client_safe_text` itself: a
# row has one anchor and cannot add an import, and a missing one would fail the test
# with `NameError` instead of the reverted behaviour.
REVERTED_LIBRARY_NAME = '''

def _reverted_unallowlisted_name(value: object) -> str:
    """
    Reverted: the control-stripped, truncated, un-allowlisted form `name` used to take.

    Args:
        value: The raw `Library.name`.

    Returns:
        str: One bounded line, with no allowlist applied.

    """
    from ..text_hygiene import client_safe_text

    return client_safe_text(value)
'''

# Appended by the four `stats Library.name` rows. `client_safe_leaf` no longer probes
# anything - the caller that holds the path passes `is_directory` in - so a revert that
# merely swapped the two leaf rules would be a no-op and its node would keep passing.
# This writes the probe back out. It imports `os` itself and leans on the
# `client_safe_name_leaf` every one of those call sites already has in scope: a row has
# one anchor and cannot add an import.
REVERTED_STATTING_NAME = '''

def _reverted_statting_name(name: object) -> str:
    """
    Reverted: the leaf rule as it was when it stat-ed the name itself.

    Args:
        name: The raw `Library.name`.

    Returns:
        str: The leaf, or `the requested file` when the filesystem calls the name a directory.

    """
    import os

    if os.path.isdir(str(name or "")):
        return "the requested file"
    return client_safe_name_leaf(name)
'''

# `_bare_connect` exists only for the readiness revert; it is appended with it.
BARE_CONNECT = '''

def _bare_connect(port: int) -> bool:
    """
    Reverted readiness: a connect with no round-trip (for the revert matrix only).

    Args:
        port: The port to probe.

    Returns:
        bool: Whether a TCP connection was accepted.

    """
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except OSError:
        return False
'''

# Appended by the two canonicalization rows. `_has_ancestor_directory`'s device/inode
# check also accepts the symlinked-root and trailing-separator cases, so reverting
# canonicalization alone would leave those nodes passing; this disables it too.
NO_SAME_DIRECTORY_FALLBACK = """

def _has_ancestor_directory(candidate, root):
    return False
"""

REVERTS: list[Revert] = [
    # --- the drain timer follows the traffic instead of a flat 50 ms poll ---
    Revert(
        "transport: the drain timer back to a flat 50 ms poll",
        ADDON_SERVER_CORE,
        "        return self._poll_interval()",
        "        return 0.05",
        (f"{THREADT}::test_a_command_makes_the_next_drain_follow_within_the_active_poll",),
    ),
    Revert(
        "transport: the fast poll never relaxing back to the idle rate",
        ADDON_SERVER_CORE,
        """        if time.monotonic() - self._last_command_at < self._ACTIVE_WINDOW_SECONDS:
            return self._ACTIVE_POLL_SECONDS
        return self._IDLE_POLL_SECONDS""",
        "        return self._ACTIVE_POLL_SECONDS",
        (
            f"{THREADT}::test_the_drain_poll_relaxes_once_the_session_goes_quiet",
            f"{THREADT}::test_a_server_that_has_served_nothing_polls_at_the_idle_rate",
        ),
    ),
    # --- a failed viewport capture leaves no datablock behind ---
    Revert(
        "viewport: the offscreen capture's image removed only when the save succeeds",
        ADDON_VIEWPORT,
        """    try:
        image.pixels.foreach_set(pixels.ravel())
        image.filepath_raw = filepath
        image.file_format = image_format.upper()
        image.save()
    finally:
        bpy.data.images.remove(image)""",
        """    image.pixels.foreach_set(pixels.ravel())
    image.filepath_raw = filepath
    image.file_format = image_format.upper()
    image.save()
    bpy.data.images.remove(image)""",
        (f"{VIEWT}::test_offscreen_capture_removes_its_image_datablock_when_the_save_fails",),
    ),
    Revert(
        "viewport: the window grab's loaded screenshot removed only when the rescale succeeds",
        ADDON_VIEWPORT,
        """    try:
        width, height = img.size
        if max(width, height) > max_size:
            s = max_size / max(width, height)
            width, height = int(width * s), int(height * s)
            img.scale(width, height)
            img.file_format = image_format.upper()
            img.save()
    finally:
        # Same reason as _render_offscreen's: a failed scale/save must not leave the loaded
        # screenshot sitting in bpy.data.images.
        bpy.data.images.remove(img)""",
        """    width, height = img.size
    if max(width, height) > max_size:
        s = max_size / max(width, height)
        width, height = int(width * s), int(height * s)
        img.scale(width, height)
        img.file_format = image_format.upper()
        img.save()
    bpy.data.images.remove(img)""",
        (f"{VIEWT}::test_window_grab_removes_the_loaded_screenshot_when_the_rescale_save_fails",),
    ),
    # --- which Blender, and which configuration, the rig launches ---
    Revert(
        "rig: --factory-startup dropped from the launch",
        RIG,
        '    command = [str(_BLENDER), "--factory-startup", "--python", str(bootstrap)]',
        '    command = [str(_BLENDER), "--python", str(bootstrap)]',
        (f"{RIGT}::test_blender_is_launched_with_factory_startup",),
    ),
    Revert(
        "rig: only BLENDER_USER_SCRIPTS is set",
        RIG,
        '        "BLENDER_USER_RESOURCES": str(work_dir),\n',
        "",
        (f"{RIGT}::test_both_blender_user_resource_roots_point_at_the_work_dir",),
    ),
    Revert(
        "rig: the launched Blender's output roots left unscoped",
        RIG,
        '        "BLENDERMCP_OUTPUT_ROOTS": str(work_dir),\n',
        "",
        (f"{RIGT}::test_the_launched_blender_is_pointed_at_the_work_dir_first",),
    ),
    Revert(
        "rig: TMPDIR left at the user's own, so bpy.app.tempdir escapes the work dir",
        RIG,
        '        "TMPDIR": str(work_dir / "tmp"),\n',
        "",
        (f"{RIGT}::test_blender_s_session_temp_dir_is_redirected_under_the_work_dir",),
    ),
    Revert(
        "rig: PYTHONPATH no longer pruned from the child environment",
        RIG,
        '_PRUNED_ENVIRONMENT_NAMES = frozenset({"PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"})',
        '_PRUNED_ENVIRONMENT_NAMES = frozenset({"PYTHONHOME", "PYTHONSTARTUP"})',
        (f"{RIGT}::test_an_inherited_pythonpath_cannot_shadow_the_staged_addon",),
    ),
    Revert(
        "rig: the prune narrowed back to BLENDER_USER_, so BLENDER_SYSTEM_* survives",
        RIG,
        '_PRUNED_ENVIRONMENT_PREFIXES = ("BLENDER",)\n'
        '_PRUNED_ENVIRONMENT_NAMES = frozenset({"PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"})',
        '_PRUNED_ENVIRONMENT_PREFIXES = ("BLENDER_USER_", "BLENDERMCP_")\n'
        '_PRUNED_ENVIRONMENT_NAMES = frozenset({"PYTHONPATH"})',
        (f"{RIGT}::test_blender_system_and_python_home_variables_cannot_redirect_the_child",),
    ),
    # --- which process answers the port ---
    Revert(
        "rig: the preflight no longer refuses an occupied port",
        RIG,
        '    try:\n        with socket.create_connection(("127.0.0.1", port), timeout=1.0):\n'
        "            pass\n    except OSError:\n        return\n    raise RigError(",
        "    if True:\n        return\n    raise RigError(",
        (f"{RIGT}::test_the_rig_refuses_a_port_something_is_already_listening_on",),
    ),
    Revert(
        "rig control: the preflight refuses every port",
        RIG,
        '    try:\n        with socket.create_connection(("127.0.0.1", port), timeout=1.0):\n'
        "            pass\n    except OSError:\n        return\n    raise RigError(",
        "    raise RigError(",
        (f"{RIGT}::test_a_free_port_passes_the_preflight",),
    ),
    Revert(
        "rig: the default port back to the addon's own 9876",
        RIG,
        "_DEFAULT_PORT = 0\n",
        "_DEFAULT_PORT = 9876\n",
        (f"{RIGT}::test_the_default_port_is_an_unused_ephemeral_port",),
    ),
    Revert(
        "rig control: _choose_port ignores an explicit --port",
        RIG,
        "    if requested:\n        return requested\n",
        "    if False:\n        return requested\n",
        (f"{RIGT}::test_an_explicit_port_is_honoured",),
    ),
    Revert(
        "rig: the port is not re-checked immediately before Popen",
        RIG,
        "    _require_port_free(port)\n    return subprocess.Popen(",
        "    return subprocess.Popen(",
        (f"{RIGT}::test_the_port_is_rechecked_immediately_before_blender_is_started",),
    ),
    Revert(
        "rig: readiness back to a bare connect",
        RIG,
        "        if _receipt_matches(receipt, launch.nonce, launch.port) and _ping_answers(launch.port):",
        "        if _receipt_matches(receipt, launch.nonce, launch.port) and _bare_connect(launch.port):",
        (f"{RIGT}::test_readiness_requires_a_ping_round_trip_not_a_bare_connect",),
        also=BARE_CONNECT,
    ),
    Revert(
        "rig: the receipt is not checked at all",
        RIG,
        '    try:\n        written = json.loads(receipt.read_text(encoding="utf-8"))\n'
        "    except (OSError, ValueError):\n        return False\n"
        '    return isinstance(written, dict) and written.get("nonce") == nonce and written.get("port") == port',
        "    return True",
        (f"{RIGT}::test_the_readiness_receipt_must_carry_the_rig_s_own_nonce_and_port",),
    ),
    Revert(
        "rig: the receipt's port is recorded but never checked",
        RIG,
        ' and written.get("port") == port',
        "",
        (f"{RIGT}::test_the_readiness_receipt_must_carry_the_rig_s_own_nonce_and_port",),
    ),
    Revert(
        "rig: a swallowed bind failure is left buried in the log tail",
        RIG,
        '    if _BIND_FAILURE_MARKER not in text:\n        return ""',
        '    if True:\n        return ""',
        (f"{RIGT}::test_a_startup_failure_names_a_bind_failure_the_addon_swallowed",),
    ),
    Revert(
        "rig: the log tail is formatted before the reader is joined",
        RIG,
        "            launch.drain.join(_SHUTDOWN_GRACE_SECONDS)\n            raise RigError(",
        "            raise RigError(",
        (f"{RIGT}::test_the_log_reader_is_joined_before_a_failure_quotes_its_log",),
    ),
    # --- what the rig may delete and overwrite ---
    Revert(
        "rig: any --work-dir is claimed, marker or not",
        RIG,
        "        if existing:\n            raise RigError(_foreign_work_dir_message(work_dir, existing))",
        "        if False:\n            raise RigError(_foreign_work_dir_message(work_dir, existing))",
        (f"{RIGT}::test_a_populated_work_dir_the_rig_did_not_create_is_refused",),
    ),
    Revert(
        "rig: a real Blender resources root is no longer recognised as one",
        RIG,
        '_BLENDER_RESOURCE_ENTRIES = ("config", "datafiles", "extensions", "scripts", "userpref.blend")',
        "_BLENDER_RESOURCE_ENTRIES = ()",
        (f"{RIGT}::test_a_work_dir_that_is_a_real_blender_resources_root_is_refused",),
    ),
    Revert(
        "rig: an auto-executing startup/ tree is no longer recognised",
        RIG,
        '_AUTO_EXECUTED_SCRIPT_DIRS = ("startup", "modules")',
        "_AUTO_EXECUTED_SCRIPT_DIRS = ()",
        (f"{RIGT}::test_a_work_dir_holding_auto_executed_scripts_is_refused",),
    ),
    Revert(
        "rig control: the work dir is never marked, so the rig refuses its own",
        RIG,
        '    marker.write_text(_OWNED_MARKER_TEXT, encoding="utf-8")\n\n\ndef _foreign_work_dir_message',
        "\n\ndef _foreign_work_dir_message",
        (f"{RIGT}::test_an_empty_or_rig_created_work_dir_is_claimed",),
    ),
    Revert(
        "rig: the marker no longer guards a directory the rig did not create",
        RIG,
        "    if directory.is_dir() and not marker.is_file():\n        raise RigError(\n"
        '            f"{directory} exists but carries no {_OWNED_MARKER_NAME}, so the rig did not create it "\n'
        '            "and will not write into it. Point --work-dir at an empty or rig-created directory."\n'
        "        )\n",
        "",
        (
            f"{RIGT}::test_staging_refuses_to_delete_an_addons_directory_it_does_not_own",
            f"{RIGT}::test_a_pre_placed_fixture_is_not_silently_overwritten",
        ),
    ),
    Revert(
        "rig: <work-dir>/addons as a regular file is no longer refused",
        RIG,
        "    if directory.exists() and not directory.is_dir():\n"
        '        raise RigError(f"{directory} exists and is not a directory; the rig will not replace it.")\n',
        "",
        (f"{RIGT}::test_an_addons_path_that_is_a_regular_file_is_refused",),
    ),
    Revert(
        "rig control: staging merges into its own previous stage instead of rebuilding it",
        RIG,
        "    if staged.exists():\n        shutil.rmtree(staged)\n",
        "",
        (f"{RIGT}::test_staging_replaces_its_own_previous_stage",),
    ),
    Revert(
        "rig: the whole addons/ tree is removed again, not just blender_mcp",
        RIG,
        "    if staged.exists():\n        shutil.rmtree(staged)\n",
        "    if addons.exists():\n        shutil.rmtree(addons)\n    addons.mkdir(parents=True)\n",
        (f"{RIGT}::test_staging_leaves_other_add_ons_in_its_own_directory_alone",),
    ),
    Revert(
        "rig: a symlinked addons/ directory is no longer refused",
        RIG,
        "    if directory.is_symlink():\n"
        '        raise RigError(f"{directory} is a symlink; refusing to write through it. '
        'Use a --work-dir the rig owns.")\n',
        "",
        (f"{RIGT}::test_staging_refuses_a_symlinked_addons_directory",),
    ),
    Revert(
        "rig: fixtures handed over by their original path",
        RIG,
        "        destination.unlink(missing_ok=True)\n        shutil.copy2(source, destination)\n"
        "        copies[name] = destination",
        "        copies[name] = source",
        (f"{RIGT}::test_fixtures_are_copied_so_a_scenario_cannot_write_through_to_the_original",),
    ),
    Revert(
        "rig: blends/ is created without the rig's marker, so a pre-placed file is overwritten",
        RIG,
        '    staged_dir = _rig_owned_subdirectory(work_dir, "blends")',
        '    staged_dir = work_dir / "blends"\n    staged_dir.mkdir(parents=True, exist_ok=True)',
        (f"{RIGT}::test_a_pre_placed_fixture_is_not_silently_overwritten",),
    ),
    Revert(
        "rig control: every existing fixture destination is refused, reused work dir or not",
        RIG,
        "        destination.unlink(missing_ok=True)\n",
        '        if destination.exists():\n            raise RigError("refusing any existing destination")\n',
        (f"{RIGT}::test_the_rig_replaces_a_fixture_copy_it_made_itself",),
    ),
    Revert(
        "rig: copy2 follows a symlinked fixture destination again",
        RIG,
        "        if destination.is_symlink():\n"
        '            raise RigError(f"{destination} is a symlink; refusing to write through it.")\n',
        "",
        (f"{RIGT}::test_a_symlinked_fixture_destination_is_not_written_through",),
    ),
    Revert(
        "rig: two --blend fixtures may share one name again",
        RIG,
        "        if name in copies:\n"
        '            raise RigError(f"--blend {name} was given twice; the two copies would overwrite each other.")\n',
        "",
        (f"{RIGT}::test_two_fixtures_with_the_same_name_are_refused",),
    ),
    Revert(
        "rig: fixture names unvalidated, so one can escape the work dir",
        RIG,
        "    if not _FIXTURE_NAME.match(name):",
        "    if False:",
        (f"{RIGT}::test_a_fixture_name_cannot_escape_the_work_dir",),
    ),
    Revert(
        "rig: importing the scenario writes __pycache__ beside the caller's file again",
        RIG,
        "    sys.dont_write_bytecode = True\n    spec = importlib_util.spec_from_file_location",
        "    spec = importlib_util.spec_from_file_location",
        (f"{RIGT}::test_importing_a_scenario_leaves_no_pycache_beside_the_caller_s_file",),
    ),
    # --- liveness of the rig itself ---
    Revert(
        "rig: no deadline on the scenario",
        RIG,
        "    if thread.is_alive():\n        abandoned.set()",
        "    if False:\n        abandoned.set()",
        (f"{RIGT}::test_a_scenario_that_never_returns_is_abandoned_at_its_deadline",),
    ),
    Revert(
        "rig control: the scenario's own failure is swallowed",
        RIG,
        "    if raised:\n        raise raised[0]",
        "    return",
        (f"{RIGT}::test_a_failing_scenario_still_reports_its_own_error",),
    ),
    Revert(
        "rig: an abandoned scenario may still send commands",
        RIG,
        "        self._refuse_if_abandoned(command_type)\n        self._sequence += 1",
        "        self._sequence += 1",
        (f"{RIGT}::test_an_abandoned_scenario_cannot_send_another_command",),
    ),
    Revert(
        "rig: the deadline no longer tells the rig it abandoned the scenario",
        RIG,
        "    if thread.is_alive():\n        abandoned.set()\n        raise RigError(",
        "    if thread.is_alive():\n        raise RigError(",
        (f"{RIGT}::test_the_deadline_silences_the_scenario_it_could_not_stop",),
    ),
    Revert(
        "rig: the transcript keeps growing after the verdict",
        RIG,
        "        if not self._abandoned.is_set():\n            print(line, flush=True)",
        "        print(line, flush=True)",
        (f"{RIGT}::test_the_deadline_silences_the_scenario_it_could_not_stop",),
    ),
    Revert(
        "rig: main catches Exception, so sys.exit(0) leaves the rig exiting 0",
        RIG,
        '    except BaseException as failure:\n        detail = ""',
        '    except Exception as failure:\n        detail = ""',
        (f"{RIGT}::test_a_scenario_that_exits_the_process_is_not_reported_as_a_pass",),
    ),
    Revert(
        "rig: Popen used as a context manager, reintroducing an unbounded wait()",
        RIG,
        "        blender = _launch_blender(work_dir, port, nonce, blender_scripts)\n"
        "        drain = _OutputDrain(blender, log_path, abandoned)\n",
        "        with _launch_blender(work_dir, port, nonce, blender_scripts) as blender:\n"
        "            drain = _OutputDrain(blender, log_path, abandoned)\n",
        (f"{RIGT}::test_the_process_is_never_waited_on_without_a_timeout",),
    ),
    Revert(
        "rig: Blender's output no longer drained to exhaustion",
        RIG,
        "            for line in stream:\n                log.write(line)",
        "            for line in [stream.readline()]:\n                log.write(line)",
        (f"{RIGT}::test_blender_s_output_is_drained_to_a_log_instead_of_filling_the_pipe",),
    ),
    Revert(
        "rig: the pipe decodes strictly again",
        RIG,
        '        errors="replace",\n',
        "",
        (f"{RIGT}::test_blender_s_output_is_decoded_leniently",),
    ),
    Revert(
        "rig: the reader dies on the first byte it cannot decode",
        RIG,
        "        try:\n            self._tee()\n"
        "        # Deliberately BaseException: the only outcome worse than losing the log is\n"
        "        # leaving the pipe unread, which deadlocks Blender on its main thread.\n"
        "        except BaseException as failure:\n            self.failure = failure\n"
        "            self._drain_silently()",
        "        self._tee()",
        (f"{RIGT}::test_the_log_reader_survives_output_it_cannot_decode",),
    ),
    Revert(
        "rig: the log reader keeps echoing after the verdict",
        RIG,
        "                if line.startswith(_ECHOED_PREFIXES) and not self._abandoned.is_set():",
        "                if line.startswith(_ECHOED_PREFIXES):",
        (f"{RIGT}::test_the_log_reader_stops_echoing_once_the_scenario_is_abandoned",),
    ),
    Revert(
        "rig: a reader that died is not reported, so Blender is blamed instead",
        RIG,
        "    if drain.failure is not None:\n        problems.append(\n"
        '            f"the rig\'s own log reader died with {type(drain.failure).__name__}: {drain.failure} "\n'
        '            "(the pipe was drained anyway, so this did not hang Blender, but the log may be short)"\n'
        "        )\n",
        "",
        (f"{RIGT}::test_teardown_reports_the_rig_s_own_log_reader_dying",),
    ),
    Revert(
        "rig: a reader still holding the pipe is not reported",
        RIG,
        "    if drain.is_alive():\n        problems.append(\n",
        "    if False:\n        problems.append(\n",
        (f"{RIGT}::test_teardown_reports_a_log_reader_that_outlived_blender",),
    ),
    Revert(
        "rig: failures no longer quote Blender's log",
        RIG,
        '    tail = "\\n".join(lines[-_LOG_TAIL_LINES:])',
        '    tail = ""',
        (f"{RIGT}::test_failures_quote_the_tail_of_that_log",),
    ),
    # --- the boundary the rig has to stay outside of ---
    Revert(
        "boundary: a packaged module references the rig",
        PACKAGE_INIT,
        None,
        "\n# scripts/blender_rig is imported here, which is exactly what must never happen.\n",
        (f"{RIGT}::test_no_packaged_module_references_the_rig",),
    ),
    Revert(
        "boundary: scripts/ becomes a package",
        SCRIPTS_INIT,
        None,
        '"""Make scripts importable, which is the drift this guard exists for."""\n',
        (f"{RIGT}::test_the_rig_is_not_importable_as_part_of_the_package",),
    ),
    Revert(
        "boundary: a packaging root outside src/ would distribute scripts/",
        PYPROJECT,
        'packages = [{ include = "blender_mcp", from = "src" }]',
        'packages = [{ include = "blender_mcp", from = "src" }, { include = "blender_rig", from = "scripts" }]',
        (f"{RIGT}::test_the_rig_is_not_importable_as_part_of_the_package",),
    ),
    # --- the container rig's static guards ---
    Revert(
        "docker: the build arg the entrypoint reads is no longer exported as ENV",
        DOCKERFILE,
        "ENV BLENDER_MAJOR_MINOR=${BLENDER_MAJOR_MINOR}\n",
        "",
        (f"{DOCKT}::test_build_args_the_entrypoint_reads_are_exported_as_env",),
    ),
    Revert(
        "docker: the entrypoint carries its own Blender version default again",
        ENTRYPOINT,
        'ADDONS_DIR="$HOME/.config/blender/${BLENDER_MAJOR_MINOR}/scripts/addons"',
        'ADDONS_DIR="$HOME/.config/blender/${BLENDER_MAJOR_MINOR:-5.2}/scripts/addons"',
        (f"{DOCKT}::test_entrypoint_does_not_hardcode_its_own_blender_version",),
    ),
    Revert(
        "docker: an unset Blender version no longer aborts the container",
        ENTRYPOINT,
        'BLENDER_MAJOR_MINOR="${BLENDER_MAJOR_MINOR:?must be set by the image (see Dockerfile ENV)}"',
        'BLENDER_MAJOR_MINOR="${BLENDER_MAJOR_MINOR}"',
        (f"{DOCKT}::test_entrypoint_fails_loudly_when_the_version_is_missing",),
    ),
    Revert(
        "docker: the Blender download URL hardcodes a version instead of interpolating it",
        DOCKERFILE,
        'RUN wget -q "https://download.blender.org/release/Blender${BLENDER_MAJOR_MINOR}/'
        'blender-${BLENDER_VERSION}-linux-x64.tar.xz" \\\n'
        "        -O /tmp/blender.tar.xz \\\n"
        "    && tar xf /tmp/blender.tar.xz -C /opt \\\n"
        "    && rm /tmp/blender.tar.xz \\\n"
        '    && ln -s "/opt/blender-${BLENDER_VERSION}-linux-x64/blender" /usr/local/bin/blender',
        'RUN wget -q "https://download.blender.org/release/Blender5.2/blender-5.2.1-linux-x64.tar.xz" \\\n'
        "        -O /tmp/blender.tar.xz \\\n"
        "    && tar xf /tmp/blender.tar.xz -C /opt \\\n"
        "    && rm /tmp/blender.tar.xz \\\n"
        '    && ln -s "/opt/blender-5.2.1-linux-x64/blender" /usr/local/bin/blender',
        (f"{DOCKT}::test_dockerfile_installs_the_blender_version_it_declares",),
    ),
    Revert(
        "docker: the advertised output root is not the path ./output is mounted at",
        COMPOSE,
        "      - ./output:/output",
        "      - ./renders:/output",
        (f"{DOCKT}::test_compose_declares_the_mounted_output_root",),
    ),
    Revert(
        "docker: the MCP port is published on every interface",
        COMPOSE,
        '      - "127.0.0.1:8000:8000"',
        '      - "8000:8000"',
        (f"{DOCKT}::test_compose_publishes_only_on_loopback",),
    ),
    Revert(
        "docker: Blender's own socket is published too",
        COMPOSE,
        '      - "127.0.0.1:8000:8000"',
        '      - "127.0.0.1:8000:8000"\n      - "127.0.0.1:9876:9876"',
        (f"{DOCKT}::test_compose_exposes_the_mcp_server_but_not_blender",),
    ),
    Revert(
        "docker: the MCP server listens on a port compose does not publish",
        ENTRYPOINT,
        "BLENDERMCP_HTTP_PORT=8000",
        "BLENDERMCP_HTTP_PORT=8001",
        (f"{DOCKT}::test_entrypoint_serves_the_mcp_server_over_http_on_the_published_port",),
    ),
    Revert(
        "entrypoint: the container asks for a transport compose's published port never serves",
        ENTRYPOINT,
        "BLENDERMCP_TRANSPORT=http",
        "BLENDERMCP_TRANSPORT=stdio",
        (f"{DOCKT}::test_entrypoint_serves_the_mcp_server_over_http_on_the_published_port",),
    ),
    Revert(
        "entrypoint: the readiness gate is gone, so the MCP server starts before Blender",
        ENTRYPOINT,
        "if ! wait_for_blender; then\n    terminate_children\n    exit 1\nfi\n\n",
        "",
        (f"{DOCKT}::test_entrypoint_waits_for_blender_before_starting_the_mcp_server",),
    ),
    Revert(
        "entrypoint: the readiness gate stops round-tripping Blender's socket",
        ENTRYPOINT,
        '"type": "ping"',
        '"type": "get_addon_info"',
        (f"{DOCKT}::test_entrypoint_waits_for_blender_before_starting_the_mcp_server",),
    ),
    Revert(
        "entrypoint: a bare wait is back, so Xvfb keeps a dead container alive",
        ENTRYPOINT,
        '    for pid in "$blender_pid" "$mcp_pid" "$xvfb_pid" "$readiness_pid"; do\n'
        '        wait "$pid" 2>/dev/null || true\n'
        "    done",
        "wait || true",
        (f"{DOCKT}::test_entrypoint_waits_only_on_the_processes_it_started",),
    ),
    Revert(
        "docker: Blender's socket bound to all interfaces",
        DOCKER_START,
        None,
        '\n# BlenderMCPServer(host="0.0.0.0") is exactly what must never ship.\n',
        (f"{DOCKT}::test_blender_keeps_its_socket_on_loopback",),
    ),
    Revert(
        "docker: dependencies resolved rather than installed from poetry.lock",
        DOCKERFILE,
        "poetry install --only main --no-root --no-interaction",
        "poetry install --no-root --no-interaction",
        (f"{DOCKT}::test_server_dependencies_are_installed_from_the_poetry_lock",),
    ),
    Revert(
        "docker: the build context excludes a file the Dockerfile COPYs",
        DOCKERIGNORE,
        "!docker/blender/healthcheck.py\n",
        "",
        (f"{DOCKT}::test_build_context_ignore_file_admits_everything_the_dockerfile_copies",),
    ),
    Revert(
        "docker: the healthcheck stops round-tripping Blender's socket",
        HEALTHCHECK,
        '"type": "ping"',
        '"type": "get_addon_info"',
        (f"{DOCKT}::test_compose_healthcheck_round_trips_both_blender_and_the_mcp_server",),
    ),
    Revert(
        "docker: the compose toolsets key is mistyped",
        COMPOSE,
        "      BLENDER_MCP_TOOLSETS: shot",
        "      BLENDER_MCP_TOOLSET: shot",
        (f"{DOCKT}::test_compose_pins_a_toolset_selection_the_server_can_resolve",),
    ),
    Revert(
        "docker: the compose toolsets value does not resolve",
        COMPOSE,
        "      BLENDER_MCP_TOOLSETS: shot",
        "      BLENDER_MCP_TOOLSETS: shto",
        (f"{DOCKT}::test_compose_pins_a_toolset_selection_the_server_can_resolve",),
    ),
    # --- the ported output_roots module ---
    Revert(
        "output_roots: the environment variable is no longer split on os.pathsep",
        ADDON_OUTPUT_ROOTS,
        '(raw or "").split(os.pathsep)',
        '[raw or ""]',
        (f"{ROOTST}::test_configured_roots_splits_the_environment_variable",),
    ),
    # The whole line, because `split_roots` reads the same variable for the file roots:
    # a bare `get(OUTPUT_ROOTS_ENV_VAR)` anchor matches twice.
    Revert(
        "output_roots: an unset variable falls back to a hardcoded root",
        ADDON_OUTPUT_ROOTS,
        "    return split_roots((os.environ if environ is None else environ).get(OUTPUT_ROOTS_ENV_VAR))",
        '    return split_roots((os.environ if environ is None else environ).get(OUTPUT_ROOTS_ENV_VAR, "/default"))',
        (f"{ROOTST}::test_configured_roots_is_empty_when_unset",),
    ),
    Revert(
        "output_roots: blank entries are no longer stripped out",
        ADDON_OUTPUT_ROOTS,
        '    return [entry.strip() for entry in (raw or "").split(os.pathsep) if entry.strip()]',
        '    return [entry for entry in (raw or "").split(os.pathsep) if entry]',
        (
            f"{ROOTST}::test_configured_roots_ignores_blank_entries",
            # The same revert at the pure helper both readers share: without the
            # `.strip()` a padded entry stays padded and `"  "` stays a root.
            f"{ROOTST}::test_split_roots_trims_each_entry_and_drops_the_blanks",
        ),
    ),
    Revert(
        "output_roots: an unset variable is stringified, so a variable nobody set reads as one root named None",
        ADDON_OUTPUT_ROOTS,
        '    return [entry.strip() for entry in (raw or "").split(os.pathsep) if entry.strip()]',
        '    return [entry.strip() for entry in f"{raw}".split(os.pathsep) if entry.strip()]',
        (f"{ROOTST}::test_split_roots_reads_an_unset_variable_as_no_roots",),
    ),
    Revert(
        "output_roots control: writable_roots keeps nothing at all",
        ADDON_OUTPUT_ROOTS,
        "        seen.add(path)\n        normalized.append(path)",
        "        seen.add(path)",
        (f"{ROOTST}::test_writable_roots_keeps_existing_writable_directories",),
    ),
    # The next three share an anchor - the one comprehension `writable_roots` is now -
    # and revert that one line three different ways.
    Revert(
        "output_roots: a path that does not exist is kept",
        ADDON_OUTPUT_ROOTS,
        "if os.path.isdir(path) and os.access(path, os.W_OK)]",
        "if not (os.path.isfile(path) or (os.path.isdir(path) and not os.access(path, os.W_OK)))]",
        (f"{ROOTST}::test_writable_roots_drops_paths_that_do_not_exist",),
    ),
    Revert(
        "output_roots: a plain file is accepted as a root",
        ADDON_OUTPUT_ROOTS,
        "if os.path.isdir(path) and os.access(path, os.W_OK)]",
        "if os.path.exists(path) and os.access(path, os.W_OK)]",
        (f"{ROOTST}::test_writable_roots_drops_files",),
    ),
    Revert(
        "output_roots: a read-only directory is offered as writable",
        ADDON_OUTPUT_ROOTS,
        "if os.path.isdir(path) and os.access(path, os.W_OK)]",
        "if os.path.isdir(path)]",
        (f"{ROOTST}::test_writable_roots_drops_read_only_directories",),
    ),
    Revert(
        "output_roots: duplicates are no longer collapsed",
        ADDON_OUTPUT_ROOTS,
        "        if path in seen:\n            continue\n",
        "",
        (f"{ROOTST}::test_writable_roots_dedupes_while_preserving_order",),
    ),
    Revert(
        "output_roots: roots are reported relative to the addon's cwd",
        ADDON_OUTPUT_ROOTS,
        "        path = os.path.abspath(os.path.expanduser(str(candidate)))",
        "        path = os.path.expanduser(str(candidate))",
        (f"{ROOTST}::test_writable_roots_reports_absolute_paths",),
    ),
    Revert(
        "output_roots: empty and None candidates reach the scan",
        ADDON_OUTPUT_ROOTS,
        "        if not candidate:\n            continue\n",
        "",
        (f"{ROOTST}::test_writable_roots_ignores_empty_candidates",),
    ),
    # `normalized_candidates` is the half of `writable_roots` that touches nothing, so
    # its own test uses paths that do not exist: a probe put back in empties the result.
    Revert(
        "output_roots: the pure half probes the filesystem again, so a root is dropped before it is offered",
        ADDON_OUTPUT_ROOTS,
        "        seen.add(path)\n        normalized.append(path)",
        "        seen.add(path)\n        if os.path.isdir(path):\n            normalized.append(path)",
        (f"{ROOTST}::test_normalized_candidates_expand_and_dedupe_without_probing_anything",),
    ),
    Revert(
        "output_roots wiring: the handshake ignores the deployment-configured roots",
        ADDON_SERVER_CORE,
        "            *configured_roots(),\n",
        "",
        (f"{ROOTST}::test_get_addon_info_reports_writable_output_roots",),
    ),
    Revert(
        "output_roots wiring: an unconfigured Blender reports no writable root at all",
        ADDON_SERVER_CORE,
        '            getattr(bpy.app, "tempdir", None),\n            tempfile.gettempdir(),\n'
        '            os.path.expanduser("~"),\n',
        "",
        (f"{ROOTST}::test_get_addon_info_reports_roots_without_any_configuration",),
    ),
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
    Revert(
        "get_addon_status: a payload key goes undocumented",
        SERVER_CORE_TOOL,
        '"writable_output_roots" (empty when none)',
        "writable output roots (empty when none)",
        (f"{CORET}::test_get_addon_status_documents_every_key_it_returns",),
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
    # `safe_relative_link` bounds its own length the same way, so the anchor carries the
    # line above it to name `client_safe_text`'s bound and not that one.
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
        ADDON_SERVER_CORE,
        '        "get_session_info": CommandSpec(read_only=True, indeterminate_safe=True),',
        '        "get_session_info_reverted": CommandSpec(read_only=True, indeterminate_safe=True),',
        (f"{SESSIONT}::test_get_session_info_is_registered_and_read_only",),
    ),
    Revert(
        "text hygiene: the library summary publishes an absolute filepath, mapping the asset library out",
        ADDON_BLEND_FILES,
        '        "filepath": whole if whole is not None else client_safe_leaf(filepath),',
        '        "filepath": filepath,',
        (f"{SESSIONT}::test_the_library_summary_reports_identity_without_the_asset_library_layout",),
    ),
    Revert(
        "session: save_shot joins the swap set, discarding a whole batch every time a client checkpoints",
        ADDON_SERVER_CORE,
        '        "save_shot": CommandSpec(tick_ending=True),',
        '        "save_shot": CommandSpec(tick_ending=True, session_swap=True),',
        (f"{SESSIONT}::test_the_session_swap_set_holds_the_commands_that_replace_the_database",),
    ),
    Revert(
        "session: a swap is wrapped in mutation_transaction, whose rollback would enumerate the whole new file",
        ADDON_SERVER_CORE,
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
    # --- the barrier itself -------------------------------------------
    Revert(
        "barrier: THE BARRIER - a swap no longer ends its tick or discards the batch queued behind it",
        ADDON_SERVER_CORE,
        "                self._run_session_swap(command, client)\n                break",
        (
            "                self._execute_and_answer(command, client)\n"
            "                processed += 1\n"
            "                continue"
        ),
        (
            # Only what the snapshot alone protects; the stamp also covers the
            # rest, so they pass with just the snapshot reverted.
            f"{THREADT}::test_a_failed_swap_also_discards_the_commands_queued_behind_it",
            f"{THREADT}::test_the_barrier_message_names_the_epoch_without_claiming_it_moved",
            f"{THREADT}::test_a_swap_ends_its_tick_even_when_a_fresh_command_is_already_queued",
        ),
    ),
    Revert(
        "barrier: no rejection frame is sent on EITHER barrier half, so the client is dropped in silence",
        ADDON_SERVER_CORE,
        "        deadline = time.monotonic() + self._REJECTION_TIME_BUDGET_SECONDS",
        "        return  # the whole rejection path reverted",
        (
            f"{THREADT}::test_a_swap_is_the_last_command_its_tick_executes",
            f"{THREADT}::test_the_barrier_message_leaks_no_filesystem_path",
            f"{THREADT}::test_the_barrier_rejection_carries_the_epoch_as_a_first_class_field",
        ),
    ),
    Revert(
        "barrier: superseded commands are dropped, not answered, so their clients hang",
        ADDON_SERVER_CORE,
        "            self._discard_superseded(superseded)",
        "            superseded.clear()",
        (f"{THREADT}::test_every_command_spanning_a_swap_is_answered_on_both_sockets",),
    ),
    Revert(
        "barrier: the enqueue path stops stamping, so the mid-load window is invisible again",
        ADDON_SERVER_CORE,
        "        self._stamp_session(command)",
        "        pass  # stamping reverted",
        (
            # Not `..._rejected_at_dequeue`: the drain loop rejects an unstamped
            # command too, and the test cannot tell the two rejections apart.
            # Without the stamp, the serviced case below is rejected instead.
            f"{THREADT}::test_a_command_queued_after_the_swap_is_serviced_normally_under_the_stamp",
            f"{THREADT}::test_the_enqueue_path_is_the_only_producer_and_it_stamps",
        ),
    ),
    Revert(
        "barrier: the dequeue comparison goes away, so a stale stamp is never acted on",
        ADDON_SERVER_CORE,
        "        if stamp != self._session_marker():",
        "        if False:",
        (
            f"{THREADT}::test_a_command_queued_before_a_swap_that_lands_elsewhere_is_rejected_at_dequeue",
            f"{THREADT}::test_the_queue_is_snapshotted_before_the_swap_runs_not_after",
        ),
    ),
    Revert(
        "barrier: the enqueue path reaches for bpy on a client thread",
        ADDON_SERVER_CORE,
        "        self._stamp_session(command)\n        logger.debug(",
        "        self._stamp_session(command)\n        _ = bpy.data\n        logger.debug(",
        (f"{THREADT}::test_the_stamp_is_read_without_touching_bpy_on_the_client_thread",),
    ),
    Revert(
        "barrier: the barrier fires for an undispatchable swap, so one frame discards everyone batch",
        ADDON_SERVER_CORE,
        ' and self._is_dispatchable(\n                    command.get("type")\n                )',
        "",
        (f"{THREADT}::test_a_swap_command_the_addon_cannot_dispatch_discards_nobodys_batch",),
    ),
    Revert(
        "barrier: the rejection path loses its deadline, pinning Blender's main thread on a stalled peer",
        ADDON_SERVER_CORE,
        "            timeout = (\n"
        "                self._REJECTION_SEND_TIMEOUT_SECONDS\n"
        "                if time.monotonic() < deadline\n"
        "                else self._PAST_BUDGET_SEND_TIMEOUT_SECONDS\n"
        "            )",
        "            timeout = self._CLIENT_SOCKET_TIMEOUT_SECONDS",
        (f"{THREADT}::test_rejecting_a_full_queue_to_a_stalled_peer_is_bounded",),
    ),
    Revert(
        "barrier: the pre-swap drain goes back to `while True`, which races producers that keep refilling",
        ADDON_SERVER_CORE,
        "        for _slot in range(self._MAX_QUEUED_COMMANDS):",
        "        while True:",
        (f"{THREADT}::test_the_pre_swap_drain_is_bounded_by_an_explicit_count",),
    ),
    Revert(
        "barrier: only Exception is caught, so a blender-side abort strands the swap's own client",
        ADDON_SERVER_CORE,
        "        except BaseException as e:",
        "        except SystemExit as e:",
        (f"{THREADT}::test_the_swaps_own_client_is_answered_when_the_swap_raises_a_base_exception",),
    ),
    Revert(
        "barrier: superseded is built outside the try, so an abort mid-drain loses dequeued commands",
        ADDON_SERVER_CORE,
        (
            "                superseded.append(self.command_queue.get_nowait())\n"
            "            except queue.Empty:\n"
            "                break"
        ),
        (
            "                taken.append(self.command_queue.get_nowait())\n"
            "            except queue.Empty:\n"
            "                break\n"
            "        superseded.extend(taken)"
        ),
        (f"{THREADT}::test_a_base_exception_during_the_pre_swap_drain_still_answers_what_was_taken",),
        also="\n# taken is defined at module scope so the reverted body still runs.\ntaken = []\n",
    ),
    Revert(
        "barrier: the queue is drained AFTER the swap, so a command enqueued mid-load is swept up with it",
        ADDON_SERVER_CORE,
        (
            "            self._drain_queue_into(superseded)\n"
            "            self._execute_and_answer(command, client, receipt=receipt)"
        ),
        (
            "            self._execute_and_answer(command, client, receipt=receipt)\n"
            "            self._drain_queue_into(superseded)"
        ),
        (f"{THREADT}::test_the_queue_is_snapshotted_before_the_swap_runs_not_after",),
    ),
    Revert(
        "barrier: the rejection frame drops the machine-readable epoch, leaving a client to regex prose",
        ADDON_SERVER_CORE,
        '                    "session_epoch": epoch,',
        '                    "session_epoch_prose_only": epoch,',
        (f"{THREADT}::test_the_barrier_rejection_carries_the_epoch_as_a_first_class_field",),
    ),
    Revert(
        "barrier: a dying tick hands off to nothing, so one escaping exception kills the drain loop",
        ADDON_SERVER_CORE,
        "            self._replace_this_dying_timer()\n            raise",
        "            raise",
        (f"{THREADT}::test_an_escaping_exception_hands_the_drain_loop_to_a_fresh_timer",),
    ),
    Revert(
        "barrier: recovery resurrects a timer stop() deliberately removed",
        ADDON_SERVER_CORE,
        "        if not self.running:\n            return\n        dying = self._drain_timer",
        "        dying = self._drain_timer",
        (f"{THREADT}::test_a_stopped_server_does_not_resurrect_its_drain_timer",),
    ),
    Revert(
        "barrier: a command sent after the swap is never serviced, so the barrier wedges the server",
        ADDON_SERVER_CORE,
        (
            "            self._execute_and_answer(command, client)\n"
            "            processed += 1\n"
            '            if self.command_spec(command.get("type")).tick_ending:'
        ),
        '            processed += 1\n            if self.command_spec(command.get("type")).tick_ending:',
        (
            f"{THREADT}::test_a_command_that_arrives_after_the_swap_is_serviced_normally",
            f"{THREADT}::test_a_command_queued_after_the_swap_is_serviced_normally_under_the_stamp",
        ),
    ),
    Revert(
        "barrier: the harness's open_mainfile stub refuses production's own `use_scripts` spelling",
        TEST_THREADING_FILE,
        'def open_mainfile(self, filepath: str = "", use_scripts: bool = False) -> set[str]:',
        'def open_mainfile(self, filepath: str = "", _use_scripts: bool = False) -> set[str]:',
        (f"{THREADT}::test_the_open_mainfile_stub_takes_use_scripts_the_way_production_passes_it",),
    ),
    # --- the handshake and the client-side reaction ------------------
    Revert(
        "handshake: the handshake stops carrying the session fields",
        ADDON_MANAGER,
        '            session_epoch=normalized_session_epoch(info.get("session_epoch")),',
        "            session_epoch=None,",
        (
            f"{AMT}::test_handshake_surfaces_the_session_epoch_and_the_open_file",
            f"{AMT}::test_handshake_defaults_the_session_fields_when_the_addon_omits_them",
        ),
    ),
    Revert(
        "handshake: current_filepath is parsed with `or None`, so any JSON type reaches a str|None field",
        ADDON_MANAGER,
        '            current_filepath=normalized_session_text(info.get("current_filepath")),',
        '            current_filepath=info.get("current_filepath") or None,',
        (
            *(
                f"{AMT}::test_handshake_refuses_a_current_filepath_that_is_not_a_string[{case}]"
                for case in ("dict", "list", "int", "bool", "float")
            ),
            f"{AMT}::test_handshake_bounds_a_pathologically_long_current_filepath",
        ),
    ),
    Revert(
        "handshake: session_id is parsed with `or None`, so the ABA guard takes any JSON type off the socket",
        ADDON_MANAGER,
        '            session_id=normalized_session_id(info.get("session_id")),',
        '            session_id=info.get("session_id") or None,',
        tuple(
            f"{AMT}::test_handshake_refuses_a_session_id_that_is_not_a_string[{case}]"
            for case in ("dict", "list", "int", "bool", "float")
        ),
    ),
    Revert(
        "handshake: the handshake marker drops the session id, so a restart at the same epoch looks unchanged",
        ADDON_MANAGER,
        "        return (self.session_id, self.session_epoch)",
        "        return (None, self.session_epoch)",
        (f"{AMT}::test_handshake_surfaces_the_session_id_so_the_epoch_survives_a_restart",),
    ),
    # The re-handshake's own "stayed stale" path sets the same event, so the anchor carries
    # the line above it to name the observation site and not that one.
    Revert(
        "rehandshake: a reported session change no longer marks the cached handshake stale",
        SERVER_CONNECTION,
        '    _OBSERVED_MARKER["pending"] = observed\n    _session_marker_stale.set()',
        '    _OBSERVED_MARKER["pending"] = observed\n    return',
        (
            f"{CONNT}::test_a_moved_epoch_marks_the_cached_handshake_stale",
            f"{CONNT}::test_the_marker_is_read_from_a_command_result_as_well_as_the_frame",
            f"{CONNT}::test_a_barrier_rejection_read_off_the_socket_marks_the_handshake_stale",
        ),
    ),
    Revert(
        "rehandshake: only the epoch is compared, so a restart back to the same number looks unchanged (ABA)",
        SERVER_CONNECTION,
        "or observed == _addon_handshake.session_marker():",
        "or observed[1] == _addon_handshake.session_epoch:",
        (f"{CONNT}::test_a_restarted_addon_at_the_same_epoch_still_marks_the_handshake_stale",),
    ),
    Revert(
        "rehandshake: the marker is read at frame level only, so get_session_info's nested pair is missed",
        SERVER_CONNECTION,
        '    if command_type in _SESSION_REPORTING_COMMANDS:\n        sources.append(payload.get("result"))',
        "    pass  # nested result read reverted",
        (f"{CONNT}::test_the_marker_is_read_from_a_command_result_as_well_as_the_frame",),
    ),
    Revert(
        "rehandshake: every response invalidates the handshake, so each tool call costs two commands",
        SERVER_CONNECTION,
        (
            "    if observed is None or _addon_handshake is None or observed == _addon_handshake.session_marker():\n"
            "        return"
        ),
        "    if _addon_handshake is None:\n        return",
        (
            f"{CONNT}::test_an_unchanged_session_marker_does_not_invalidate_the_cached_handshake",
            f"{CONNT}::test_a_response_carrying_no_marker_changes_nothing",
        ),
    ),
    Revert(
        "rehandshake: the stale flag is never cleared, so every command pays for another handshake",
        SERVER_CONNECTION,
        "    _session_marker_stale.clear()",
        "    pass  # clear() reverted",
        (f"{CONNT}::test_one_swap_costs_exactly_one_re_handshake_over_a_real_round_trip",),
    ),
    Revert(
        "rehandshake: the command gate goes back to the handshake cached once per process",
        SERVER_CONNECTION,
        "        handshake = refresh_handshake_if_session_changed(self)",
        "        handshake = get_last_handshake()",
        (f"{CONNT}::test_the_command_gate_reads_the_refreshed_capability_set",),
    ),
    Revert(
        "rehandshake: the receive path never observes the marker, so the mechanism is never reached",
        SERVER_CONNECTION,
        "            note_session_marker(response, command_type)",
        "            pass  # note_session_marker reverted",
        (f"{CONNT}::test_a_barrier_rejection_read_off_the_socket_marks_the_handshake_stale",),
    ),
    # --- send liveness, the fail-closed drain, the marker, path hygiene, the producer scan ---
    Revert(
        "barrier: `or` short-circuits again, so a spent budget closes every remaining peer unsent",
        ADDON_SERVER_CORE,
        "            if not self._send_bounded(client, frame, timeout):",
        "            if time.monotonic() >= deadline or not self._send_bounded(client, frame, timeout):",
        (
            f"{THREADT}::test_a_healthy_peer_queued_behind_stalled_ones_is_still_answered",
            # With the short-circuit back, every peer past the budget is closed
            # without a send attempt, which the distinct-peer sibling also sees.
            f"{THREADT}::test_rejecting_a_full_queue_to_distinct_stalled_peers_is_bounded",
        ),
    ),
    Revert(
        "barrier: the send timeout goes back to a performance target used as a health threshold",
        ADDON_SERVER_CORE,
        "    _REJECTION_SEND_TIMEOUT_SECONDS = 0.25",
        "    _REJECTION_SEND_TIMEOUT_SECONDS = 0.05",
        (f"{THREADT}::test_a_peer_slower_than_a_loopback_reader_is_not_destroyed_for_it",),
    ),
    Revert(
        "barrier: an abandoned peer is written to again, paying a syscall per remaining frame",
        ADDON_SERVER_CORE,
        "            if client in abandoned:\n                continue",
        "            if False:\n                continue",
        (f"{THREADT}::test_rejecting_a_full_queue_to_a_stalled_peer_is_bounded",),
    ),
    Revert(
        "barrier: the write-lock acquisition is unbounded again, outside both of the path's own bounds",
        ADDON_SERVER_CORE,
        "        acquired = send_lock.acquire(False) if lock_timeout <= 0 else send_lock.acquire(timeout=lock_timeout)",
        "        acquired = send_lock.acquire()",
        (f"{THREADT}::test_a_malformed_frame_arriving_mid_rejection_cannot_park_the_main_thread",),
    ),
    Revert(
        "barrier: a failed timeout restore reports the frame as delivered, leaving the peer spinning",
        ADDON_SERVER_CORE,
        (
            "            logger.warning(\n"
            "                \"Could not restore a client socket's own timeout"
            ' - closing it rather than leaving it spinning"\n'
            "            )\n"
            "            return False"
        ),
        "            pass  # restore failure ignored",
        (f"{THREADT}::test_a_socket_whose_timeout_cannot_be_restored_is_dropped_not_left_spinning",),
    ),
    Revert(
        "barrier: the drain loop fails OPEN again, so an unstamped command runs",
        ADDON_SERVER_CORE,
        "        if stamp != self._session_marker():",
        "        if stamp is not None and stamp != self._session_marker():",
        (f"{THREADT}::test_a_command_that_reached_the_queue_unstamped_is_rejected_not_run",),
    ),
    Revert(
        "barrier: ordinary responses stop carrying the marker, so Blender's own File -> Open is invisible",
        ADDON_SERVER_CORE,
        '        response["session_id"], response["session_epoch"] = self._session_marker()',
        "        pass  # per-frame marker reverted",
        (f"{THREADT}::test_an_ordinary_response_carries_the_session_marker_too",),
    ),
    Revert(
        "barrier: an aborted swap leaves the marker where it was, so mid-load stamps still match",
        ADDON_SERVER_CORE,
        (
            "            mid_load = load_in_flight()\n"
            "            if mid_load:\n"
            "                mark_session_indeterminate()"
        ),
        "            mid_load = False",
        (
            f"{THREADT}::test_an_aborted_swap_invalidates_the_stamps_taken_during_its_load",
            f"{THREADT}::test_an_aborted_swap_publishes_an_indeterminate_note_every_client_can_poll",
        ),
    ),
    Revert(
        "barrier: the dying timer is never unregistered, so one live drain timer rests on Blender alone",
        ADDON_SERVER_CORE,
        (
            "        if dying is not None:\n"
            "            with suppress(Exception):\n"
            "                bpy.app.timers.unregister(dying)"
        ),
        "        pass  # explicit unregister reverted",
        (f"{THREADT}::test_a_dying_tick_leaves_exactly_one_live_drain_timer",),
    ),
    Revert(
        "session: a swap across a disable/enable cycle is observed and discarded",
        ADDON_SESSION,
        "    missed_a_swap = observed != state.current_filepath",
        "    missed_a_swap = False",
        (f"{SESSIONT}::test_a_swap_while_the_addon_was_disabled_still_moves_the_marker",),
    ),
    Revert(
        "session: every registration bumps the epoch, so a plain Blender start invalidates every cache",
        ADDON_SESSION,
        "    missed_a_swap = observed != state.current_filepath",
        "    missed_a_swap = True",
        (f"{SESSIONT}::test_re_enabling_on_the_same_file_does_not_move_the_marker",),
    ),
    Revert(
        "text hygiene: the unsafe-character set stops at C0/C1, so U+2028 and the bidi overrides survive",
        ADDON_TEXT_HYGIENE,
        'UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})',
        'UNSAFE_CATEGORIES = frozenset({"Cc"})',
        (
            # Only `name` depends on this set; for `filepath` the leaf allowlist
            # refuses `Zl` and `Cf` anyway. With the set reverted, the bidi override
            # survives the strip and the allowlist refuses the whole leaf, so the
            # test asserts the stripped string, not just that the output is safe.
            f"{SESSIONT}::test_a_library_name_is_published_without_its_control_characters",
        ),
    ),
    Revert(
        "text hygiene: the link allowlist stops rejecting traversal and empty components",
        ADDON_TEXT_HYGIENE,
        "        if component in NOT_A_LEAF or not set(component) <= LINK_COMPONENT_ALLOWED:",
        '        if not set(component) <= LINK_COMPONENT_ALLOWED | {"."}:',
        (
            f"{HOSTILE_LIB}[traversal out of the shot-//../../../clients/acme-merger/lib/canon.blend-forbidden0]",
            f"{HOSTILE_LIB}[zero-width-hidden traversal-//.\\u200b./.\\u200b./clients/acme/canon.blend-forbidden11]",
            f"{SESSIONT}::test_a_link_published_whole_names_nothing_above_its_own_shot",
        ),
    ),
    Revert(
        "text hygiene: the link predicate goes back to a blocklist, which is a list of the attacks already known",
        ADDON_TEXT_HYGIENE,
        "        if component in NOT_A_LEAF or not set(component) <= LINK_COMPONENT_ALLOWED:",
        '        if component in NOT_A_LEAF or any(marker in component for marker in ("/", "\\\\", ":")):',
        (
            f"{HOSTILE_LIB}[fullwidth solidus-//shots/\\uff0fUsers\\uff0fvictim\\uff0facme.blend-forbidden6]",
            f"{HOSTILE_LIB}[big solidus (U+29F8)-//..\\u29f8..\\u29f8clients\\u29f8acme\\u29f8canon.blend-forbidden9]",
            NFKC_BACKSLASH_LIB,
        ),
    ),
    Revert(
        "text hygiene: the confusable check goes, so an admitted letter publishes a name that renders as another",
        ADDON_TEXT_HYGIENE,
        "    if is_confusable(leaf) or not _is_admissible_leaf(leaf):",
        "    if not _is_admissible_leaf(leaf):",
        (f"{SESSIONT}::test_a_confusable_leaf_name_is_refused_rather_than_published",),
    ),
    Revert(
        "text hygiene: `//` followed by a root is called relative again, so an absolute path is published whole",
        ADDON_TEXT_HYGIENE,
        "    if body[:1] in _LEAF_SEPARATORS:\n        return None",
        "    if False:\n        return None",
        (
            # Only `is_relative` depends on this clause; the empty-component check
            # still keeps `///Users/...` from being published. See `_ROOTED_TWICE`.
            f"{SESSIONT}::test_a_rooted_relative_prefix_is_not_reported_as_relative",
        ),
    ),
    Revert(
        "text hygiene: the gate admits one string and the publisher returns another, manufacturing what it rejected",
        ADDON_BLEND_FILES,
        '        "filepath": whole if whole is not None else client_safe_leaf(filepath),',
        '        "filepath": filepath if whole is not None else client_safe_leaf(filepath),',
        (
            f"{SESSIONT}::test_a_whole_published_link_is_the_string_the_gate_looked_at",
            f"{HOSTILE_LIB}[format character inside a component-//libs/\\u200bcanon.blend-forbidden12]",
        ),
    ),
    Revert(
        "text hygiene: the library summary loses the hygiene its sibling field has, on both branches",
        ADDON_BLEND_FILES,
        '        "filepath": whole if whole is not None else client_safe_leaf(filepath),',
        '        "filepath": filepath,',
        (
            f"{HOSTILE_LIB}[ANSI escape, relative branch-//shots/\\x1b[31mx.blend-forbidden1]",
            f"{HOSTILE_LIB}[ANSI escape, absolute branch-/mnt/studio/\\x1b[31mx.blend-forbidden2]",
            f"{HOSTILE_LIB}[500 characters, relative branch-//{'a' * 500}.blend-forbidden3]",
            f"{HOSTILE_LIB}[500 characters, absolute branch-/mnt/{'b' * 500}.blend-forbidden4]",
        ),
    ),
    Revert(
        "get_addon_status: get_addon_status publishes the epoch without the id it is only comparable within",
        SERVER_CORE_TOOL,
        '        "session_id": result.session_id,',
        "",
        (f"{CORET}::test_get_addon_status_reports_the_session_id_the_epoch_is_only_comparable_within",),
    ),
    Revert(
        "rehandshake: the refresh's own response is read as news, so one swap costs two handshakes",
        SERVER_CONNECTION,
        '    if getattr(_refreshing, "active", False):\n        return',
        "    if False:\n        return",
        (f"{CONNT}::test_one_swap_costs_exactly_one_re_handshake_over_a_real_round_trip",),
    ),
    Revert(
        "rehandshake: a failed re-handshake clears the staleness signal permanently and never retries",
        SERVER_CONNECTION,
        "    if learned is None or None in learned:",
        "    if False:",
        (f"{CONNT}::test_a_refresh_that_fails_leaves_the_staleness_signal_standing",),
    ),
    Revert(
        "rehandshake: the observed pair is compared raw, so a normalized value never equals its own twin",
        SERVER_CONNECTION,
        (
            '                normalized_session_id(source.get("session_id")),\n'
            '                normalized_session_epoch(source.get("session_epoch")),'
        ),
        ('                source.get("session_id"),\n                source.get("session_epoch"),'),
        (f"{CONNT}::test_a_non_conforming_epoch_does_not_re_arm_the_flag_forever",),
    ),
    Revert(
        "rehandshake: any result carrying a session_epoch key trips a re-handshake, whatever command it answers",
        SERVER_CONNECTION,
        "    if command_type in _SESSION_REPORTING_COMMANDS:",
        "    if True:",
        (f"{CONNT}::test_an_ordinary_commands_result_cannot_trip_a_re_handshake",),
    ),
    Revert(
        "barrier: the producer scan goes back to the form five shapes were shown to evade",
        TEST_THREADING_FILE,
        '_ENQUEUE_METHODS = frozenset({"put", "put_nowait"})',
        '_ENQUEUE_METHODS = frozenset({"put_nowait"})',
        (f"{EVASION}[blocking put()-def sneak(self, item):\\n    self.command_queue.put(item)\\n]",),
    ),
    Revert(
        "barrier: the producer scan walks FunctionDef only, missing an async def and a lambda",
        TEST_THREADING_FILE,
        "_CALLABLE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)",
        "_CALLABLE_NODES = (ast.FunctionDef,)",
        (
            f"{EVASION}[async def-async def sneak(self, item):\\n    self.command_queue.put_nowait(item)\\n]",
            f"{EVASION}[lambda at class scope-class C:\\n"
            "    sneak = lambda self, item: self.command_queue.put_nowait(item)\\n]",
        ),
    ),
    Revert(
        "barrier: the producer scan requires the receiver spelled `<x>.command_queue` again",
        TEST_THREADING_FILE,
        "            and node.func.attr in _ENQUEUE_METHODS\n        )",
        (
            "            and node.func.attr in _ENQUEUE_METHODS\n"
            "            and isinstance(node.func.value, ast.Attribute)\n"
            '            and node.func.value.attr == "command_queue"\n'
            "        )"
        ),
        (
            f"{EVASION}[local alias-def sneak(self, item):\\n    q = self.command_queue\\n    q.put_nowait(item)\\n]",
            f"{EVASION}[helper takes the queue-def sneak(target, item):\\n    target.put_nowait(item)\\n]",
        ),
    ),
    # --- the ported HTTP transport (ee25ffc) ---
    Revert(
        "transport: HTTP becomes the default, so every existing stdio client config breaks",
        SERVER_CLI,
        'env.get(TRANSPORT_ENV, "stdio")',
        'env.get(TRANSPORT_ENV, "http")',
        (
            f"{CLIT}::test_stdio_is_the_default",
            f"{CLIT}::test_http_settings_are_ignored_under_stdio",
            f"{CLIT}::test_main_serves_stdio_by_default",
        ),
    ),
    Revert(
        "transport: opting into HTTP alone binds every interface",
        SERVER_CLI,
        'DEFAULT_HTTP_HOST = "127.0.0.1"',
        'DEFAULT_HTTP_HOST = "0.0.0.0"',
        (f"{CLIT}::test_http_defaults_to_loopback_on_port_8000",),
    ),
    Revert(
        "transport: the default HTTP port is not the one the rig publishes",
        SERVER_CLI,
        "DEFAULT_HTTP_PORT = 8000",
        "DEFAULT_HTTP_PORT = 9000",
        (f"{CLIT}::test_http_defaults_to_loopback_on_port_8000",),
    ),
    Revert(
        "transport: the configured host and port are ignored",
        SERVER_CLI,
        "        return HttpConfig(host=_resolve_http_host(env), port=_parse_port(env))",
        "        return HttpConfig(host=DEFAULT_HTTP_HOST, port=DEFAULT_HTTP_PORT)",
        (f"{CLIT}::test_http_host_and_port_are_configurable",),
    ),
    Revert(
        "transport: the transport name is read raw, so compose's padding or casing is rejected",
        SERVER_CLI,
        'env.get(TRANSPORT_ENV, "stdio").strip().lower()',
        'env.get(TRANSPORT_ENV, "stdio")',
        (f"{CLIT}::test_transport_name_ignores_case_and_whitespace",),
    ),
    Revert(
        "transport: a misspelt transport silently serves stdio nobody reads",
        SERVER_CLI,
        "    raise ValueError(f\"{TRANSPORT_ENV} must be 'stdio' or 'http', got {name!r}\")",
        "    return StdioConfig()",
        (
            f"{CLIT}::test_unknown_transport_is_rejected",
            f"{CLIT}::test_main_reports_a_bad_transport_without_serving",
        ),
    ),
    Revert(
        "transport: the HTTP port is no longer range-checked before anything binds",
        SERVER_CLI,
        "    if not 1 <= port <= MAX_PORT:",
        "    if False:",
        (
            f"{CLIT}::test_invalid_http_port_is_rejected[abc]",
            f"{CLIT}::test_invalid_http_port_is_rejected[0]",
            f"{CLIT}::test_invalid_http_port_is_rejected[65536]",
            f"{CLIT}::test_invalid_http_port_is_rejected[-1]",
        ),
    ),
    Revert(
        "transport: main() never dispatches to HTTP, so the container serves stdio into EOF",
        SERVER_CLI,
        '    if config.transport == "streamable-http":',
        "    if False:",
        (f"{CLIT}::test_main_serves_http_on_the_configured_address",),
    ),
    Revert(
        "transport: the configured bind address is never applied before serving",
        SERVER_CLI,
        "    mcp.settings.host = host\n    mcp.settings.port = port\n",
        "",
        (f"{CLIT}::test_main_serves_http_on_the_configured_address",),
    ),
    Revert(
        "transport: binding 0.0.0.0 drops the DNS-rebinding protection FastMCP built in",
        SERVER_CLI,
        "    mcp.settings.host = host\n    mcp.settings.port = port\n",
        "    mcp.settings.host = host\n    mcp.settings.port = port\n    mcp.settings.transport_security = None\n",
        (f"{CLIT}::test_binding_all_interfaces_keeps_dns_rebinding_protection",),
    ),
    # --- HTTP host, port and opt-in validation ---
    Revert(
        "transport: an empty BLENDERMCP_HTTP_HOST falls through to a wildcard bind again",
        SERVER_CLI,
        "    host = raw.strip()\n    if not host:",
        "    host = raw.strip() or DEFAULT_HTTP_HOST\n    if False:",
        (
            f"{CLIT}::test_empty_http_host_is_rejected[]",
            f"{CLIT}::test_empty_http_host_is_rejected[ ]",
            f"{CLIT}::test_empty_http_host_is_rejected[\\t\\n]",
            f"{CLIT}::test_an_empty_host_is_rejected_even_with_remote_binds_allowed",
        ),
    ),
    Revert(
        "transport: a non-loopback bind needs no opt-in",
        SERVER_CLI,
        "    if not _allows_remote(env):",
        "    if False:",
        (
            f"{CLIT}::test_a_non_loopback_bind_is_refused_without_the_opt_in[0]",
            f"{CLIT}::test_a_non_loopback_bind_is_refused_without_the_opt_in[0.0.0.0]",
            f"{CLIT}::test_a_non_loopback_bind_is_refused_without_the_opt_in[::]",
            f"{CLIT}::test_a_non_loopback_bind_is_refused_without_the_opt_in[192.168.1.5]",
            f"{CLIT}::test_a_non_loopback_bind_is_refused_without_the_opt_in[example.internal]",
            f"{CLIT}::test_main_refuses_a_wildcard_bind_without_serving",
        ),
    ),
    Revert(
        "transport: loopback is not recognised, so even 127.0.0.1 demands consent",
        SERVER_CLI,
        'LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})',
        "LOOPBACK_HOSTS = frozenset()",
        (
            f"{CLIT}::test_loopback_hosts_need_no_opt_in[127.0.0.1]",
            f"{CLIT}::test_loopback_hosts_need_no_opt_in[localhost]",
            f"{CLIT}::test_loopback_hosts_need_no_opt_in[LOCALHOST]",
            f"{CLIT}::test_loopback_hosts_need_no_opt_in[::1]",
            f"{CLIT}::test_a_loopback_bind_is_not_warned_about",
        ),
    ),
    Revert(
        "transport: the opt-in is tested for presence, so ALLOW_REMOTE=false reads as consent",
        SERVER_CLI,
        '    return env.get(HTTP_ALLOW_REMOTE_ENV, "").strip().lower() in TRUTHY_VALUES',
        "    return HTTP_ALLOW_REMOTE_ENV in env",
        (
            f"{CLIT}::test_a_falsy_opt_in_does_not_open_the_bind[0]",
            f"{CLIT}::test_a_falsy_opt_in_does_not_open_the_bind[false]",
            f"{CLIT}::test_a_falsy_opt_in_does_not_open_the_bind[no]",
            f"{CLIT}::test_a_falsy_opt_in_does_not_open_the_bind[off]",
            f"{CLIT}::test_a_falsy_opt_in_does_not_open_the_bind[]",
            f"{CLIT}::test_a_falsy_opt_in_does_not_open_the_bind[ ]",
            f"{CLIT}::test_a_falsy_opt_in_does_not_open_the_bind[maybe]",
        ),
    ),
    Revert(
        "transport: no value opts in at all, so every spelling of yes is refused",
        SERVER_CLI,
        'TRUTHY_VALUES = frozenset({"1", "true", "yes", "on"})',
        "TRUTHY_VALUES = frozenset()",
        (
            f"{CLIT}::test_the_opt_in_honours_the_usual_spellings_of_yes[1]",
            f"{CLIT}::test_the_opt_in_honours_the_usual_spellings_of_yes[true]",
            f"{CLIT}::test_the_opt_in_honours_the_usual_spellings_of_yes[TRUE]",
            f"{CLIT}::test_the_opt_in_honours_the_usual_spellings_of_yes[on]",
            f"{CLIT}::test_the_opt_in_honours_the_usual_spellings_of_yes[ yes ]",
        ),
    ),
    Revert(
        "transport: an authorised wide bind is made silently, with nothing in the log",
        SERVER_CLI,
        "    logger.warning(\n"
        '        f"{HTTP_ALLOW_REMOTE_ENV} is set, so BlenderMCP will bind {host}, which {widening}. "',
        "    logger.debug(\n"
        '        f"{HTTP_ALLOW_REMOTE_ENV} is set, so BlenderMCP will bind {host}, which {widening}. "',
        (f"{CLIT}::test_an_allowed_non_loopback_bind_says_what_it_costs",),
    ),
    Revert(
        "transport: the port is parsed by int() alone, so underscores and full-width digits pass",
        SERVER_CLI,
        "    port = int(digits) if len(digits) <= MAX_PORT_DIGITS and digits.isascii() and digits.isdecimal() else 0",
        "    port = int(digits) if digits else 0",
        (
            f"{CLIT}::test_port_syntax_no_port_includes_is_rejected[8_000]",
            f"{CLIT}::test_port_syntax_no_port_includes_is_rejected[+8000]",
            f"{CLIT}::test_port_syntax_no_port_includes_is_rejected[\\uff11\\uff12\\uff13]",
            f"{CLIT}::test_port_syntax_no_port_includes_is_rejected[\\uff10\\uff10\\uff10\\uff18\\uff10\\uff10\\uff10]",
        ),
    ),
    Revert(
        "transport: the port length is unbounded, so CPython's own int() error escapes instead of ours",
        SERVER_CLI,
        "    port = int(digits) if len(digits) <= MAX_PORT_DIGITS and digits.isascii() and digits.isdecimal() else 0",
        "    port = int(digits) if digits.isascii() and digits.isdecimal() else 0",
        (f"{CLIT}::test_a_rejected_value_is_not_echoed_whole_into_the_log",),
    ),
    Revert(
        "transport: the port is not stripped, so a padded compose value is rejected",
        SERVER_CLI,
        "    digits = raw.strip()",
        "    digits = raw",
        (f"{CLIT}::test_padded_http_port_is_accepted",),
    ),
    Revert(
        "transport: a rejected value is echoed into the log whole, however long it is",
        SERVER_CLI,
        "    clipped = raw[:MAX_ECHOED_CHARS]",
        "    clipped = raw",
        (f"{CLIT}::test_a_rejected_value_is_not_echoed_whole_into_the_log",),
    ),
    Revert(
        "transport: stdio warns about ignored HTTP settings that were never set",
        SERVER_CLI,
        "    ignored = [name for name in HTTP_ONLY_ENVS if name in env]",
        "    ignored = list(HTTP_ONLY_ENVS)",
        (f"{CLIT}::test_stdio_without_http_settings_warns_about_nothing",),
    ),
    Revert(
        "transport: HttpConfig defaults its address again, so the illegal state is constructible",
        SERVER_CLI,
        "    host: str\n    port: int\n",
        "    host: str | None = None\n    port: int | None = None\n",
        (f"{CLIT}::test_an_http_config_without_an_address_cannot_be_built_even_under_o",),
    ),
    # --- the rig's --work-dir, its frame bounds, and its teardown ---
    Revert(
        "rig: --work-dir is no longer resolved, so the rig reports a path it is not writing to",
        RIG,
        "        _execute(arguments, arguments.work_dir.expanduser().resolve())",
        "        _execute(arguments, arguments.work_dir.expanduser())",
        (f"{RIGT}::test_a_symlinked_work_dir_is_resolved_before_anything_is_claimed",),
    ),
    Revert(
        "rig: the unreachable symlink refusal is restored, now reachable and refusing valid dirs",
        RIG,
        "    if work_dir.exists() and not work_dir.is_dir():",
        "    if work_dir.is_symlink():\n"
        '        raise RigError(f"--work-dir {work_dir} is a symlink; point it at a real directory.")\n'
        "    if work_dir.exists() and not work_dir.is_dir():",
        (
            f"{RIGT}::test_a_symlinked_work_dir_is_claimed_through_to_the_directory_it_points_at",
            f"{RIGT}::test_foreign_data_behind_a_symlinked_work_dir_is_still_refused",
        ),
    ),
    Revert(
        "rig: a timed-out command escapes as a bare TimeoutError naming neither command nor port",
        RIG,
        "        except TimeoutError as expiry:\n"
        "            raise RigError(self._unanswered_message(request)) from expiry\n",
        "",
        (f"{RIGT}::test_a_command_that_never_came_back_is_reported_as_possibly_still_running",),
    ),
    Revert(
        "rig: the frame timeout bounds each recv() again, so a dribbling peer is read for ever",
        RIG,
        "        remaining = deadline - time.monotonic()\n"
        "        if remaining <= 0:\n"
        '            raise TimeoutError(f"no complete frame within {timeout:g}s ({len(buffer)} bytes received)")\n'
        "        sock.settimeout(remaining)",
        "        sock.settimeout(timeout)",
        (f"{RIGT}::test_one_frame_is_bounded_as_a_whole_not_one_recv_at_a_time",),
    ),
    Revert(
        "rig: the launch and its reader move back outside the try, orphaning Blender on a reader failure",
        RIG,
        "        blender = _launch_blender(work_dir, port, nonce, blender_scripts)\n"
        "        drain = _OutputDrain(blender, log_path, abandoned)\n"
        "        drain.start()",
        "        drain.start()",
        (f"{RIGT}::test_a_launched_blender_is_stopped_even_if_its_reader_cannot_be_constructed",),
        also="",
    ),
    Revert(
        "rig: join() is intolerant of a reader that never started, masking the real cause",
        RIG,
        "        if not self._started:\n            return\n",
        "",
        (f"{RIGT}::test_teardown_tolerates_a_reader_that_never_started",),
    ),
    # --- the container's teardown, its readiness probe, and the bind note ---
    Revert(
        "entrypoint: teardown has no SIGKILL escalation, so a wedged child blocks it for ever",
        ENTRYPOINT,
        "        kill -KILL $blender_pid $mcp_pid $xvfb_pid $readiness_pid 2>/dev/null || true",
        "        : no escalation",
        (f"{DOCKT}::test_teardown_finishes_even_when_a_child_ignores_sigterm",),
    ),
    Revert(
        "entrypoint: the grace period is always spent, even when every child has already gone",
        ENTRYPOINT,
        '    kill -TERM "$watchdog_pid" 2>/dev/null || true',
        "    : leave the watchdog running",
        (f"{DOCKT}::test_teardown_costs_nothing_when_every_child_has_already_gone",),
    ),
    Revert(
        "entrypoint: the readiness probe runs in the foreground again, deferring docker stop",
        ENTRYPOINT,
        "    blender_readiness_probe &",
        "    blender_readiness_probe",
        (f"{DOCKT}::test_a_stop_signal_during_the_readiness_wait_is_handled_at_once",),
    ),
    Revert(
        "entrypoint: the note explaining why a 0.0.0.0 bind is contained is deleted",
        ENTRYPOINT,
        "# different file. docker-compose.yml maps `127.0.0.1:8000:8000`; run this image",
        "# different file, and this note used to name the mapping it depends on.",
        (f"{DOCKT}::test_binding_all_interfaces_records_the_publish_that_makes_it_safe",),
    ),
    # --- the allowlist a published leaf name is held to ---
    Revert(
        "text hygiene: the leaf allowlist goes back to a three-character blocklist",
        ADDON_TEXT_HYGIENE,
        "    return all(\n"
        "        character in LEAF_PUNCTUATION or unicodedata.category(character).startswith(LEAF_CATEGORY_PREFIXES)\n"
        "        for character in leaf\n"
        "    )",
        '    return not any(marker in leaf for marker in ("/", "\\\\", ":"))',
        (f"{SESSIONT}::test_no_character_can_smuggle_a_separator_through_a_leaf_name",),
    ),
    Revert(
        "text hygiene: the leaf allowlist refuses every letter, so a real file name reports as unnameable",
        ADDON_TEXT_HYGIENE,
        'LEAF_CATEGORY_PREFIXES = ("L", "N", "M")',
        'LEAF_CATEGORY_PREFIXES = ("N",)',
        (f"{SESSIONT}::test_a_confusable_check_does_not_refuse_an_ordinary_name",),
    ),
    # --- the latch, the abort guard, the send floor ---
    Revert(
        "session: an aborted swap latches nothing, so the state stays advisory and nothing reads it",
        ADDON_SESSION,
        "        session_indeterminate=True,\n        current_filepath=None,",
        "        session_indeterminate=False,\n        current_filepath=None,",
        (
            f"{SESSIONT}::test_an_aborted_swap_latches_a_state_a_client_can_read",
            f"{SESSIONT}::test_only_a_completed_load_clears_the_indeterminate_latch",
            f"{THREADT}::test_a_command_is_refused_while_the_session_is_indeterminate",
        ),
    ),
    Revert(
        "session: an aborted session still names the shot it was replacing, which save_shot would write over",
        ADDON_SESSION,
        "        session_indeterminate=True,\n        current_filepath=None,",
        "        session_indeterminate=True,",
        (f"{SESSIONT}::test_an_aborted_swap_latches_a_state_a_client_can_read",),
    ),
    Revert(
        "session: a completed load stops clearing the latch, so the addon wedges after one abort",
        ADDON_SESSION,
        "        load_in_flight=False,\n        session_indeterminate=False,",
        "        load_in_flight=False,",
        (
            f"{SESSIONT}::test_only_a_completed_load_clears_the_indeterminate_latch",
            f"{THREADT}::test_the_commands_that_report_or_repair_an_indeterminate_session_still_run",
        ),
    ),
    Revert(
        "session: the snapshot stops publishing the latch, so no client can see why it is being refused",
        ADDON_SESSION,
        '        "session_indeterminate": state.session_indeterminate,',
        '        "session_indeterminate": False,',
        (
            f"{SESSIONT}::test_an_aborted_swap_latches_a_state_a_client_can_read",
            f"{SESSIONT}::test_only_a_completed_load_clears_the_indeterminate_latch",
        ),
    ),
    Revert(
        "session: get_addon_info hides the latch, so the one surface a refused client can reach says nothing",
        ADDON_SERVER_CORE,
        '            "session_indeterminate": session["session_indeterminate"],',
        '            "session_indeterminate": False,',
        (f"{SESSIONT}::test_an_aborted_swap_latches_a_state_a_client_can_read",),
    ),
    Revert(
        "barrier: the drain loop stops enforcing the latch, so a command runs against a half-replaced database",
        ADDON_SERVER_CORE,
        '        if session_is_indeterminate() and not self.command_spec(command.get("type")).indeterminate_safe:',
        "        if False:",
        (f"{THREADT}::test_a_command_is_refused_while_the_session_is_indeterminate",),
    ),
    Revert(
        "barrier: the latch refuses the commands that repair and report it, wedging the addon for good",
        ADDON_SERVER_CORE,
        (
            '        "get_addon_info": CommandSpec(read_only=True, indeterminate_safe=True),\n'
            '        "get_session_info": CommandSpec(read_only=True, indeterminate_safe=True),\n'
            '        "open_shot": CommandSpec(session_swap=True, indeterminate_safe=True),\n'
            '        "save_shot": CommandSpec(tick_ending=True),\n'
            '        "reset_session": CommandSpec(session_swap=True, indeterminate_safe=True),'
        ),
        (
            '        "get_addon_info": CommandSpec(read_only=True),\n'
            '        "get_session_info": CommandSpec(read_only=True),\n'
            '        "open_shot": CommandSpec(session_swap=True),\n'
            '        "save_shot": CommandSpec(tick_ending=True),\n'
            '        "reset_session": CommandSpec(session_swap=True),'
        ),
        (f"{THREADT}::test_the_commands_that_report_or_repair_an_indeterminate_session_still_run",),
    ),
    Revert(
        "barrier: the abort guard goes back to a bare except, claiming a partial replace when no load ran",
        ADDON_SERVER_CORE,
        (
            "            mid_load = load_in_flight()\n"
            "            if mid_load:\n"
            "                mark_session_indeterminate()\n"
            "            if not receipt.answered:\n"
            "                self._answer(command, client, "
            '{"status": "error", "message": self._abort_message(mid_load)})'
        ),
        "            mark_session_indeterminate()",
        (
            f"{THREADT}::test_an_abort_before_the_swap_runs_does_not_claim_the_database_was_touched",
            f"{THREADT}::test_an_abort_after_a_completed_load_does_not_bump_the_marker_twice",
            f"{THREADT}::test_an_abort_before_the_swap_is_dispatched_does_not_claim_the_database_is_half_replaced",
            f"{THREADT}::test_a_failed_load_then_an_abort_does_not_claim_a_known_clean_database_is_half_replaced",
            f"{THREADT}::test_a_second_identical_failure_then_an_abort_is_still_not_indeterminate",
            f"{THREADT}::test_an_abort_between_the_dispatch_and_the_load_is_not_claimed_to_have_touched_the_database",
            f"{THREADT}::test_a_second_abort_that_began_no_load_does_not_bump_the_marker_again",
        ),
    ),
    Revert(
        "barrier: the abort guard's two concerns go back to being exclusive, so a mid-load abort never latches",
        ADDON_SERVER_CORE,
        (
            "            mid_load = load_in_flight()\n"
            "            if mid_load:\n"
            "                mark_session_indeterminate()\n"
            "            if not receipt.answered:\n"
            "                self._answer(command, client, "
            '{"status": "error", "message": self._abort_message(mid_load)})'
        ),
        (
            "            if not receipt.answered:\n"
            "                self._answer(command, client, "
            '{"status": "error", "message": self._ABORTED_BEFORE_HANDOFF})\n'
            "            elif load_in_flight():\n"
            "                mark_session_indeterminate()"
        ),
        (f"{THREADT}::test_an_abort_that_beats_the_answer_still_latches_a_load_that_was_in_flight",),
    ),
    Revert(
        "barrier: the abort message stops asking the flag, so a mid-load abort still says the database is unchanged",
        ADDON_SERVER_CORE,
        "        return self._ABORTED_MID_LOAD if mid_load else self._ABORTED_BEFORE_HANDOFF",
        "        return self._ABORTED_BEFORE_HANDOFF",
        (f"{THREADT}::test_an_abort_that_beats_the_answer_still_latches_a_load_that_was_in_flight",),
    ),
    Revert(
        "barrier: the abort message always claims a half-replaced database, even when no load had begun",
        ADDON_SERVER_CORE,
        "        return self._ABORTED_MID_LOAD if mid_load else self._ABORTED_BEFORE_HANDOFF",
        "        return self._ABORTED_MID_LOAD",
        (f"{THREADT}::test_an_abort_with_no_load_in_flight_still_answers_and_still_claims_nothing_moved",),
    ),
    Revert(
        "barrier: the past-budget send goes back to settimeout(0), which drops the peer it is answering",
        ADDON_SERVER_CORE,
        "    _PAST_BUDGET_SEND_TIMEOUT_SECONDS = 0.001",
        "    _PAST_BUDGET_SEND_TIMEOUT_SECONDS = 0.0",
        (f"{THREADT}::test_the_past_budget_send_never_takes_the_socket_out_of_timeout_mode",),
    ),
    Revert(
        "barrier: BlockingIOError falls through to `break` again, so EAGAIN reads as a dead client",
        ADDON_SERVER_CORE,
        "                except BlockingIOError:",
        "                except TimeoutError:",
        (f"{THREADT}::test_a_peer_whose_recv_reports_would_block_is_not_disconnected",),
    ),
    # --- the server boundary ---
    Revert(
        "handshake: the server boundary stops filtering control characters",
        ADDON_MANAGER,
        "    cleaned = strip_unsafe(value)",
        "    cleaned = value",
        (
            f"{AMT}::test_the_handshake_strips_control_characters_from_the_session_id",
            f"{AMT}::test_the_handshake_strips_control_characters_from_the_reported_filepath",
            f"{AMT}::test_a_session_id_that_is_nothing_but_control_characters_is_absent_not_empty",
        ),
    ),
    Revert(
        "handshake: `warning` goes back to carrying the addon's own error message verbatim",
        ADDON_MANAGER,
        '            warning=strip_unsafe(f"Addon handshake failed: {e}"),',
        '            warning=f"Addon handshake failed: {e}",',
        (f"{AMT}::test_a_hostile_addon_error_message_does_not_reach_the_handshake_warning",),
    ),
    Revert(
        "handshake: the list fields publish the cleaned form again, manufacturing a traversal and an exact capability",
        ADDON_MANAGER,
        "    return isinstance(element, str) and cleaned == element",
        "    return True",
        (
            f"{AMT}::test_a_root_whose_traversal_only_exists_once_cf_is_stripped_is_refused",
            f"{AMT}::test_a_capability_that_only_matches_once_cf_is_stripped_is_refused",
        ),
    ),
    Revert(
        "handshake: the structural gate allows an end-trim again, so padding synthesises a root and a capability",
        ADDON_MANAGER,
        "    return isinstance(element, str) and cleaned == element",
        "    return isinstance(element, str) and cleaned == element.strip()",
        (f"{AMT}::test_an_element_that_only_differs_by_end_whitespace_is_refused_too",),
    ),
    Revert(
        "handshake: an untrusted payload's truthiness decides whether the session is indeterminate",
        ADDON_MANAGER,
        '            session_indeterminate=info.get("session_indeterminate") is True,',
        '            session_indeterminate=info.get("session_indeterminate") is not None,',
        (f"{AMT}::test_the_handshake_reports_an_indeterminate_session_only_when_the_addon_says_so",),
    ),
    Revert(
        "text hygiene: the two copies of the control-character rule drift, which is the risk duplication carries",
        SERVER_TEXT_HYGIENE,
        'UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})',
        'UNSAFE_CATEGORIES = frozenset({"Cc"})',
        (f"{AMT}::test_both_sides_of_the_socket_hold_the_same_control_character_block",),
    ),
    Revert(
        "get_addon_status: get_addon_status carries the latch under a name its docstring never mentions",
        SERVER_CORE_TOOL,
        '        "session_indeterminate": result.session_indeterminate,',
        '        "session_indeterminate_x": result.session_indeterminate,',
        (
            f"{CORET}::test_get_addon_status_documents_every_key_it_returns",
            f"{CORET}::test_get_addon_status_reports_an_indeterminate_session",
            f"{CORET}::test_get_addon_status_reports_a_healthy_session_as_determinate",
        ),
    ),
    # --- the library name, is_confusable, the refresh, and abort accounting ---
    Revert(
        "text hygiene: the library name goes back through client_safe_text, which allowlists nothing",
        ADDON_BLEND_FILES,
        '        "name": client_safe_name_leaf(getattr(library, "name", "")),',
        '        "name": _reverted_unallowlisted_name(getattr(library, "name", "")),',
        tuple(f"{HOSTILE_LIB_NAME}[{case}]" for case in HOSTILE_LIB_NAME_IDS),
        REVERTED_LIBRARY_NAME,
    ),
    Revert(
        "text hygiene: is_confusable compares against the raw string, so an NFD name is called a disguise",
        ADDON_TEXT_HYGIENE,
        '    return unicodedata.normalize("NFKC", text) != unicodedata.normalize("NFC", text)',
        '    return unicodedata.normalize("NFKC", text) != text',
        (f"{SESSIONT}::test_a_decomposed_accent_is_a_real_name_not_a_disguise",),
    ),
    Revert(
        "text hygiene: is_confusable is made constantly False, so a compatibility disguise is published",
        ADDON_TEXT_HYGIENE,
        '    return unicodedata.normalize("NFKC", text) != unicodedata.normalize("NFC", text)',
        '    return unicodedata.normalize("NFKC", text) != unicodedata.normalize("NFKC", text)',
        (
            f"{SESSIONT}::test_the_confusable_check_still_refuses_a_compatibility_disguise_after_nfc",
            f"{SESSIONT}::test_a_confusable_leaf_name_is_refused_rather_than_published",
        ),
    ),
    Revert(
        "rehandshake: the refresh demands the stale pair again, so the staleness flag re-arms forever",
        SERVER_CONNECTION,
        "    if learned is None or None in learned:",
        "    if learned is None or learned != observed:",
        (f"{CONNT}::test_a_refresh_that_learns_a_newer_session_than_the_one_observed_stops_retrying",),
    ),
    Revert(
        "barrier: an abort during the pre-swap drain leaves the swap's own client with nothing",
        ADDON_SERVER_CORE,
        "            if not receipt.answered:",
        "            if False:",
        (
            f"{THREADT}::test_an_abort_during_the_pre_swap_drain_answers_the_swaps_own_client",
            f"{THREADT}::test_an_abort_in_the_swaps_prologue_still_answers_the_swaps_own_client",
        ),
    ),
    Revert(
        "barrier: the answer receipt goes back to a prediction the caller writes up front",
        ADDON_SERVER_CORE,
        "        try:\n            self._drain_queue_into(superseded)",
        "        try:\n            receipt.answered = True\n            self._drain_queue_into(superseded)",
        (f"{THREADT}::test_an_abort_in_the_swaps_prologue_still_answers_the_swaps_own_client",),
    ),
    Revert(
        "session: a clean load_post_fail stops accounting for its load, so it reads as an abort",
        ADDON_SESSION,
        "        load_failures=state.load_failures + 1,\n        load_in_flight=False,",
        "        load_failures=state.load_failures + 1,",
        (
            f"{THREADT}::test_a_failed_load_then_an_abort_does_not_claim_a_known_clean_database_is_half_replaced",
            f"{THREADT}::test_a_second_identical_failure_then_an_abort_is_still_not_indeterminate",
        ),
    ),
    # --- the load stamp itself, which decides whether a timing result is evidence ---
    Revert(
        "quiet box: an unreadable load average reports as the quietest possible machine",
        QUIET_BOX,
        "    except (OSError, AttributeError):\n        return None",
        "    except (OSError, AttributeError):\n        return 0.0",
        (f"{QBT}::test_an_unreadable_load_average_is_unknown_not_quiet",),
    ),
    Revert(
        "quiet box: an unknown load is treated as quiet, which is the failure the stamp exists to stop",
        QUIET_BOX,
        "    return per_core is not None and per_core <= QUIET_BOX_LOAD_PER_CORE",
        "    return per_core is None or per_core <= QUIET_BOX_LOAD_PER_CORE",
        (f"{QBT}::test_an_unknown_load_is_never_reported_as_quiet_box_verified",),
    ),
    Revert(
        "quiet box: the threshold stops being inclusive at parity",
        QUIET_BOX,
        "    return per_core is not None and per_core <= QUIET_BOX_LOAD_PER_CORE",
        "    return per_core is not None and per_core < QUIET_BOX_LOAD_PER_CORE",
        (f"{QBT}::test_the_threshold_is_inclusive_at_parity_and_excludes_anything_above_it",),
    ),
    Revert(
        "quiet box: a cpu count of 0 falls into the division instead of reporting unknown",
        QUIET_BOX,
        "    if not cores:",
        "    if cores is None:",
        (f"{QBT}::test_a_cpu_count_of_zero_is_unknown_rather_than_a_division_error",),
    ),
    Revert(
        "quiet box: the 15-minute average is stamped in place of the 1-minute one",
        QUIET_BOX,
        "        one_minute = os.getloadavg()[0]",
        "        one_minute = os.getloadavg()[1]",
        (f"{QBT}::test_load_per_core_divides_the_one_minute_average_by_the_cpu_count",),
    ),
    Revert(
        "quiet box: every stamp reads verified, whatever the load was",
        QUIET_BOX,
        '    verdict = "quiet-box verified" if per_core <= QUIET_BOX_LOAD_PER_CORE else "NOT quiet-box verified"',
        '    verdict = "quiet-box verified"',
        (f"{QBT}::test_a_contended_stamp_says_so_rather_than_only_omitting_the_verdict",),
    ),
    Revert(
        "quiet box: the stamp drops the threshold it was judged against",
        QUIET_BOX,
        '    return f"QUIET BOX [{label}]: {per_core:.2f} load per core '
        '(threshold {QUIET_BOX_LOAD_PER_CORE:.2f}) - {verdict}"',
        '    return f"QUIET BOX [{label}]: {per_core:.2f} load per core - {verdict}"',
        (f"{QBT}::test_the_stamp_names_the_load_the_threshold_and_the_verdict",),
    ),
    Revert(
        "quiet box: an unavailable load average is stamped as 0.00 and verified",
        QUIET_BOX,
        '        return f"QUIET BOX [{label}]: load average unavailable - NOT quiet-box verified"',
        '        return f"QUIET BOX [{label}]: 0.00 load per core - quiet-box verified"',
        (f"{QBT}::test_the_stamp_says_not_verified_when_the_load_is_unavailable",),
    ),
    Revert(
        "quiet box: a scenario copied out of the repository gets None instead of a loud failure",
        QUIET_BOX,
        '    raise FileNotFoundError(f"scripts/quiet_box.py not found above {start}")',
        "    return None  # reverted: silently unstamped",
        (f"{QBT}::test_the_by_path_loader_refuses_a_caller_outside_the_repository",),
    ),
    # --- every handshake field refuses a hostile value, and load_pre ---
    Revert(
        "handshake: addon_version goes back to whatever the socket sent, in a field declared list[int]",
        ADDON_MANAGER,
        '            addon_version=normalized_addon_version(info.get("addon_version")),',
        '            addon_version=info.get("addon_version"),',
        (
            f"{AMT}::test_every_handshake_field_refuses_the_same_hostile_string[addon_version]",
            f"{AMT}::test_a_hostile_element_inside_a_list_field_is_dropped_not_published[addon_version]",
            f"{AMT}::test_an_addon_version_that_is_not_a_version_is_absent_rather_than_stripped",
        ),
    ),
    Revert(
        "handshake: capabilities is `list(... or [])` again, which validates the container and nothing in it",
        ADDON_MANAGER,
        # Hoisted out of the AddonHandshake(...) call by the surface-gap check, which needs the
        # normalized list before the dataclass is built; the revert follows it to its new line.
        '        capabilities = normalized_session_text_list(info.get("capabilities"))',
        '        capabilities = list(info.get("capabilities") or [])',
        (
            f"{AMT}::test_every_handshake_field_refuses_the_same_hostile_string[capabilities]",
            f"{AMT}::test_a_hostile_element_inside_a_list_field_is_dropped_not_published[capabilities]",
            f"{AMT}::{_LIST_SCALAR}[capabilities]",
        ),
    ),
    Revert(
        "handshake: blender_version is published raw, into get_addon_status and the handshake log line",
        ADDON_MANAGER,
        '            blender_version=normalized_session_text(info.get("blender_version")),',
        '            blender_version=info.get("blender_version"),',
        (
            f"{AMT}::test_every_handshake_field_refuses_the_same_hostile_string[blender_version]",
            f"{AMT}::test_the_handshake_log_line_cannot_be_forged_by_the_addon_payload",
        ),
    ),
    Revert(
        "handshake: writable_output_roots publishes its elements raw",
        ADDON_MANAGER,
        '            writable_output_roots=normalized_session_text_list(info.get("writable_output_roots")),',
        '            writable_output_roots=list(info.get("writable_output_roots") or []),',
        (
            f"{AMT}::test_every_handshake_field_refuses_the_same_hostile_string[writable_output_roots]",
            f"{AMT}::test_a_hostile_element_inside_a_list_field_is_dropped_not_published[writable_output_roots]",
            f"{AMT}::{_LIST_SCALAR}[writable_output_roots]",
        ),
    ),
    Revert(
        "handshake: current_filepath is published raw, so the hostile string reaches get_addon_status verbatim",
        ADDON_MANAGER,
        '            current_filepath=normalized_session_text(info.get("current_filepath")),',
        '            current_filepath=info.get("current_filepath"),',
        (f"{AMT}::test_every_handshake_field_refuses_the_same_hostile_string[current_filepath]",),
    ),
    Revert(
        "handshake: session_id is published raw, the field the whole hygiene module was written for",
        ADDON_MANAGER,
        '            session_id=normalized_session_id(info.get("session_id")),',
        '            session_id=info.get("session_id"),',
        (f"{AMT}::test_every_handshake_field_refuses_the_same_hostile_string[session_id]",),
    ),
    Revert(
        "handshake: session_epoch takes any JSON value, so a string epoch is published as an epoch",
        ADDON_MANAGER,
        '            session_epoch=normalized_session_epoch(info.get("session_epoch")),',
        '            session_epoch=info.get("session_epoch"),',
        (f"{AMT}::test_every_handshake_field_refuses_the_same_hostile_string[session_epoch]",),
    ),
    Revert(
        "handshake: session_indeterminate carries the payload itself rather than a verdict about it",
        ADDON_MANAGER,
        '            session_indeterminate=info.get("session_indeterminate") is True,',
        '            session_indeterminate=info.get("session_indeterminate"),',
        (f"{AMT}::test_every_handshake_field_refuses_the_same_hostile_string[session_indeterminate]",),
    ),
    Revert(
        "session: load_pre stops recording that a load began, so no abort is ever indeterminate",
        ADDON_SESSION,
        "    return replace(state, load_in_flight=True)",
        "    return replace(state, load_in_flight=False)",
        (
            f"{THREADT}::test_an_aborted_swap_invalidates_the_stamps_taken_during_its_load",
            f"{THREADT}::test_an_aborted_swap_publishes_an_indeterminate_note_every_client_can_poll",
            f"{THREADT}::test_a_retry_then_an_abort_part_way_through_the_second_load_is_indeterminate",
            f"{THREADT}::test_a_swap_that_answers_normally_is_not_answered_a_second_time_by_the_guard",
            f"{THREADT}::test_a_second_abort_that_began_no_load_does_not_bump_the_marker_again",
        ),
    ),
    Revert(
        "session: the load_pre handler is never registered, so Blender never tells the addon a load began",
        ADDON_SESSION,
        '    ("load_pre", _on_load_pre),\n',
        "",
        (f"{THREADT}::test_a_retry_then_an_abort_part_way_through_the_second_load_is_indeterminate",),
    ),
    Revert(
        "session: an abort stops accounting for its own load, so the next abort latches on the strength of it",
        ADDON_SESSION,
        (
            "        # this transition - so leaving the flag set would make the *next* abort,\n"
            "        # however unrelated, latch on the strength of this one.\n"
            "        load_in_flight=False,"
        ),
        (
            "        # this transition - so leaving the flag set would make the *next* abort,\n"
            "        # however unrelated, latch on the strength of this one."
        ),
        (f"{THREADT}::test_a_second_abort_that_began_no_load_does_not_bump_the_marker_again",),
    ),
    Revert(
        "barrier: _answer stops writing the receipt, so the guard answers a client that was already answered",
        ADDON_SERVER_CORE,
        "        if receipt is not None:\n            receipt.answered = True\n\n",
        "",
        (f"{THREADT}::test_a_swap_that_answers_normally_is_not_answered_a_second_time_by_the_guard",),
    ),
    # --- rollback that survives a file swap or a library reload -------
    Revert(
        "transaction: the library commands enter mutation_transaction, so a failed reload deletes what it reloaded",
        ADDON_SERVER_CORE,
        "            or spec.datablock_replacing\n",
        "",
        (
            f"{TSWAPT}::test_a_library_replacing_command_never_reaches_mutation_transaction",
            f"{TSWAPT}::test_a_reload_that_fails_after_churning_its_library_removes_nothing",
        ),
    ),
    Revert(
        "transaction: link_canon_library joins the datablock-replacing set, so a failed link leaks its library",
        ADDON_SERVER_CORE,
        '        "link_canon_library": CommandSpec(),',
        '        "link_canon_library": CommandSpec(datablock_replacing=True),',
        (
            f"{TSWAPT}::test_the_datablock_replacing_set_is_the_three_library_commands_and_nothing_read_only",
            f"{TSWAPT}::test_link_canon_library_still_enters_mutation_transaction",
            f"{TSWAPT}::test_a_failed_link_rolls_back_its_library_with_the_file_handlers_registered",
            f"{TSWAPT}::test_a_failed_link_never_removes_a_datablock_its_library_removal_already_freed",
        ),
    ),
    Revert(
        "transaction: Transaction.invalidate() does nothing, so a swap inside a transaction is rolled back",
        ADDON_TRANSACTION,
        (
            "        self.invalidated = True\n"
            "        self._before_ids = {}\n"
            "        self._backup_ids = frozenset()\n"
            "        invalidate_object_states(self._states)\n"
            "        self._states = []\n"
        ),
        "        return\n",
        (
            f"{TSWAPT}::test_a_swap_inside_an_open_transaction_is_not_rolled_back_and_says_so",
            f"{TSWAPT}::test_an_invalidated_geometry_backup_is_dropped_without_remove",
            f"{TSWAPT}::test_blend_import_post_during_a_flagged_reload_invalidates_the_open_transaction",
        ),
    ),
    Revert(
        "transaction: rollback ignores the invalidation and diffs against the emptied snapshot",
        ADDON_TRANSACTION,
        "        if self.invalidated:\n            return ROLLBACK_SKIPPED_WARNING\n",
        "",
        (
            f"{TSWAPT}::test_a_swap_inside_an_open_transaction_is_not_rolled_back_and_says_so",
            f"{TSWAPT}::test_an_invalidated_geometry_backup_is_dropped_without_remove",
            f"{TSWAPT}::test_blend_import_post_during_a_flagged_reload_invalidates_the_open_transaction",
        ),
    ),
    Revert(
        "transaction: a skipped rollback re-raises the original error, so the warning never reaches the envelope",
        ADDON_TRANSACTION,
        "        if warning is None:\n            raise\n",
        "        raise\n",
        (
            f"{TSWAPT}::test_a_swap_inside_an_open_transaction_is_not_rolled_back_and_says_so",
            f"{TSWAPT}::test_blend_import_post_during_a_flagged_reload_invalidates_the_open_transaction",
        ),
    ),
    Revert(
        "transaction: ObjectState.invalidate() removes the geometry backup a load may already have freed",
        ADDON_OBJECT_STATE,
        "        self.materials = []\n        self.geometry_backup = None\n\n    def discard_backup",
        "        self.materials = []\n        self.discard_backup()\n\n    def discard_backup",
        (
            f"{TSWAPT}::test_an_invalidated_geometry_backup_is_dropped_without_remove",
            f"{TSWAPT}::test_object_state_invalidate_releases_every_live_reference_without_touching_bpy",
        ),
    ),
    Revert(
        "transaction: libraries untracked, so a failed link leaks the Library datablock",
        ADDON_TRANSACTION,
        '    "libraries",\n)',
        ")",
        (
            f"{TSWAPT}::test_libraries_are_tracked",
            f"{TSWAPT}::test_a_failed_link_rolls_back_its_library_with_the_file_handlers_registered",
            f"{TSWAPT}::test_a_failed_link_never_removes_a_datablock_its_library_removal_already_freed",
        ),
    ),
    Revert(
        "transaction: libraries removed in reverse order with everything else, before their linked datablocks",
        ADDON_TRANSACTION,
        'if coll_name not in {"objects", "libraries"}]',
        'if coll_name != "objects"]',
        (f"{TSWAPT}::test_a_failed_link_never_removes_a_datablock_its_library_removal_already_freed",),
    ),
    Revert(
        "transaction: blend_import_post invalidates on every import, disarming a failed link's rollback",
        ADDON_SESSION,
        "    if library_replace_in_progress():\n        invalidate_active_transaction()\n",
        "    invalidate_active_transaction()\n",
        (
            f"{TSWAPT}::test_a_failed_link_rolls_back_its_library_with_the_file_handlers_registered",
            f"{TSWAPT}::test_blend_import_post_without_the_flag_leaves_the_transaction_armed",
        ),
    ),
    Revert(
        "transaction: the replace flag is not restored when the reload raises",
        ADDON_TRANSACTION,
        "    try:\n        yield\n    finally:\n        _DISPATCH.library_replace_in_progress = previous",
        "    yield\n    _DISPATCH.library_replace_in_progress = previous",
        (f"{TSWAPT}::test_the_replace_flag_is_cleared_when_the_reload_raises",),
    ),
    Revert(
        "transaction: load_post stops invalidating the open transaction",
        ADDON_SESSION,
        "    invalidate_active_transaction()\n    # The datablocks",
        "    # The datablocks",
        (
            f"{TSWAPT}::test_a_swap_inside_an_open_transaction_is_not_rolled_back_and_says_so",
            f"{TSWAPT}::test_an_invalidated_geometry_backup_is_dropped_without_remove",
        ),
    ),
    Revert(
        "transaction: the blend_import_post handler is never registered",
        ADDON_SESSION,
        '    ("blend_import_post", _on_blend_import_post),\n',
        "",
        (
            f"{TSWAPT}::test_blend_import_post_is_registered_once_across_disable_enable_cycles",
            f"{TSWAPT}::test_blend_import_post_during_a_flagged_reload_invalidates_the_open_transaction",
        ),
    ),
    Revert(
        "transaction: the active transaction is cleared only on success, so a failed command stays reachable",
        ADDON_TRANSACTION,
        (
            "        txn.commit()\n"
            "    finally:\n"
            "        # `finally`, not `except`: a BaseException (Esc's KeyboardInterrupt)\n"
            "        # must not leave a finished command reachable from the next handler.\n"
            "        _DISPATCH.active = previous\n"
        ),
        "        txn.commit()\n        _DISPATCH.active = previous\n",
        (
            f"{TSWAPT}::test_the_active_transaction_never_outlives_its_command[exception]",
            f"{TSWAPT}::test_the_active_transaction_never_outlives_its_command[base]",
        ),
    ),
    Revert(
        "transaction: the active transaction is never cleared",
        ADDON_TRANSACTION,
        (
            "    finally:\n"
            "        # `finally`, not `except`: a BaseException (Esc's KeyboardInterrupt)\n"
            "        # must not leave a finished command reachable from the next handler.\n"
            "        _DISPATCH.active = previous\n"
        ),
        "",
        (f"{TSWAPT}::test_the_active_transaction_never_outlives_its_command[success]",),
    ),
    Revert(
        "transaction harness: rollback stops removing new datablocks, so the regression guards stop reproducing",
        ADDON_TRANSACTION,
        "        _remove_datablocks(_new_datablocks(self._before_ids, exclude_ids=self._backup_ids))\n",
        "",
        (
            f"{MUTT}::test_regression_guard_a_transaction_unaware_of_a_file_swap_removes_the_whole_new_file",
            f"{MUTT}::test_regression_guard_a_transaction_unaware_of_a_library_reload_removes_the_reloaded_contents",
        ),
    ),
    # --- the filesystem trust boundary ---
    Revert(
        "file paths: file_paths imports bpy",
        ADDON_FILE_PATHS,
        None,
        "\nimport bpy\n",
        (f"{FPT}::test_file_paths_imports_no_bpy",),
    ),
    Revert(
        "file paths: a non-string path reaches the string handling",
        ADDON_FILE_PATHS,
        '    if not isinstance(raw, str):\n        raise ValueError("path must be a string")\n',
        "",
        tuple(
            f"{FPT}::test_a_non_string_path_is_refused[{case}]"
            for case in ("None", "7", "b'shot.blend'", "['shot.blend']", "PosixPath('shot.blend')")
        ),
    ),
    Revert(
        "file paths: an empty or blank path is not refused (Blender opens the process CWD)",
        ADDON_FILE_PATHS,
        '    if not raw.strip():\n        raise ValueError("path must not be empty")\n',
        "",
        (f"{FPT}::test_an_empty_path_is_refused", f"{FPT}::test_a_whitespace_only_path_is_refused"),
    ),
    Revert(
        "file paths: a NUL byte is not refused",
        ADDON_FILE_PATHS,
        '    if "\\x00" in raw:\n        raise ValueError("path must not contain a NUL byte")\n',
        "",
        (f"{FPT}::test_a_nul_byte_is_refused",),
    ),
    Revert(
        "file paths: an unexpanded Blender-relative prefix is resolved as a POSIX path",
        ADDON_FILE_PATHS,
        "    if raw.startswith(BLENDER_RELATIVE_PREFIX):",
        "    if False:",
        (f"{FPT}::test_an_unexpanded_blender_relative_prefix_is_refused",),
    ),
    Revert(
        "file paths: the .blend suffix is not checked",
        ADDON_FILE_PATHS,
        "    if not (_has_blend_suffix(raw) and _has_blend_suffix(resolved)):",
        "    if False:",
        (
            f"{FPT}::test_a_non_blend_extension_is_refused",
            f"{FPT}::test_a_trailing_dot_after_the_blend_suffix_is_refused",
            f"{FPT}::test_a_trailing_space_after_the_blend_suffix_is_refused",
        ),
    ),
    Revert(
        "file paths: trailing dots and spaces are stripped before the suffix is compared",
        ADDON_FILE_PATHS,
        "    leaf = os.path.basename(path)\n",
        '    leaf = os.path.basename(path).rstrip(". ")\n',
        (
            f"{FPT}::test_a_trailing_dot_after_the_blend_suffix_is_refused",
            f"{FPT}::test_a_trailing_space_after_the_blend_suffix_is_refused",
        ),
    ),
    Revert(
        "file paths: the .blend suffix is compared case-sensitively",
        ADDON_FILE_PATHS,
        "    return leaf.lower().endswith(BLEND_SUFFIX)",
        "    return leaf.endswith(BLEND_SUFFIX)",
        (f"{FPT}::test_the_blend_suffix_is_matched_case_insensitively",),
    ),
    Revert(
        "file paths: realpath reverted to abspath, symlinks compared by name (plus the same-directory fallback)",
        ADDON_FILE_PATHS,
        "    return os.path.realpath(os.path.abspath(os.path.expanduser(path)))",
        "    return os.path.abspath(os.path.expanduser(path))",
        (
            f"{FPT}::test_a_symlink_inside_a_root_pointing_outside_it_is_refused",
            f"{FPT}::test_a_symlinked_parent_directory_is_refused",
            f"{FPT}::test_a_root_reached_through_a_symlink_still_contains_its_files",
            f"{FPT}::test_blenders_relative_form_resolves_inside_the_blend_directory",
            f"{PHT}::test_a_downloaded_blend_resolving_outside_its_download_directory_is_never_loaded",
        ),
        also=NO_SAME_DIRECTORY_FALLBACK,
    ),
    Revert(
        "file paths: no abspath or realpath, relative and `..` paths compared as typed (plus the same-dir fallback)",
        ADDON_FILE_PATHS,
        "    return os.path.realpath(os.path.abspath(os.path.expanduser(path)))",
        "    return os.path.expanduser(path)",
        (
            f"{FPT}::test_a_bare_relative_path_comes_out_absolute",
            f"{FPT}::test_dotdot_traversal_is_normalised_before_the_containment_check",
            f"{FPT}::test_a_relative_form_climbing_out_of_the_blend_directory_is_normalised",
            f"{FPT}::test_a_path_inside_a_root_is_accepted_even_when_the_root_has_a_trailing_separator",
        ),
        also=NO_SAME_DIRECTORY_FALLBACK,
    ),
    Revert(
        "file paths: ~ is not expanded, so it names a directory under the process CWD",
        ADDON_FILE_PATHS,
        "    return os.path.realpath(os.path.abspath(os.path.expanduser(path)))",
        "    return os.path.realpath(os.path.abspath(path))",
        (f"{FPT}::test_tilde_expands_to_the_home_directory",),
    ),
    # The prefix test now lives in `contains`, the pure predicate `enforce_roots` calls, so
    # the row reverts the predicate and the predicate's own test notices alongside the walk's.
    Revert(
        "file paths: containment by string prefix (the /output-evil bug)",
        ADDON_FILE_PATHS,
        "        return os.path.commonpath((canonical_root, canonical_candidate)) == canonical_root",
        "        return canonical_candidate.startswith(canonical_root)",
        (
            f"{FPT}::test_a_sibling_directory_sharing_the_roots_prefix_is_refused",
            f"{FPT}::test_a_sibling_sharing_the_roots_spelling_is_not_contained",
        ),
    ),
    Revert(
        "file paths: a root no longer contains itself, so a save into the root directory is refused",
        ADDON_FILE_PATHS,
        "        return os.path.commonpath((canonical_root, canonical_candidate)) == canonical_root",
        "        return (\n"
        "            os.path.commonpath((canonical_root, canonical_candidate)) == canonical_root\n"
        "            and canonical_candidate != canonical_root\n"
        "        )",
        (f"{FPT}::test_a_root_contains_itself_and_what_lies_under_it",),
    ),
    # `commonpath` raises on two drive letters only under `ntpath`; on posix that pair is
    # two relative spellings sharing no component, and it returns "". So the row swaps the
    # module as well as the refusal: with the raise treated as containment, the drive case
    # and the absolute/relative case both come back contained. `also` adds the import
    # because a row has one anchor and cannot add one.
    Revert(
        "file paths: a comparison that cannot be made counts as contained, so the refusal fails open",
        ADDON_FILE_PATHS,
        "        return os.path.commonpath((canonical_root, canonical_candidate)) == canonical_root\n"
        "    except ValueError:\n"
        "        return False  # different drives on Windows: not contained by spelling",
        "        return ntpath.commonpath((canonical_root, canonical_candidate)) == canonical_root\n"
        "    except ValueError:\n"
        "        return True  # a comparison that cannot be made is read as containment",
        (
            f"{FPT}::test_a_root_on_another_windows_drive_contains_nothing",
            f"{FPT}::test_paths_that_cannot_be_compared_are_reported_as_not_contained",
        ),
        also="\nimport ntpath\n",
    ),
    Revert(
        "file paths: no configured roots refuses everything instead of enforcing nothing",
        ADDON_FILE_PATHS,
        (
            "    canonical_roots = [canonical_path(root) for root in roots]\n"
            "    if not canonical_roots:\n"
            "        return  # the permissive default costs no syscall\n"
            "    candidate = canonical_path(path)\n"
            "    if inside_roots(candidate, canonical_roots):\n"
            "        return\n"
        ),
        (
            "    canonical_roots = [canonical_path(root) for root in roots]\n"
            "    candidate = canonical_path(path)\n"
            "    if canonical_roots and inside_roots(candidate, canonical_roots):\n"
            "        return\n"
        ),
        (f"{FPT}::test_no_configured_roots_enforces_nothing",),
    ),
    Revert(
        "file paths: unset roots authorize nothing instead of everything, at the verdict itself",
        ADDON_FILE_PATHS,
        "    return not canonical_roots or any(contains(root, canonical_candidate) for root in canonical_roots)",
        "    return any(contains(root, canonical_candidate) for root in canonical_roots)",
        (f"{FPT}::test_a_path_is_authorized_by_any_one_root_and_by_no_roots_at_all",),
    ),
    Revert(
        "file paths: the containment refusal echoes the resolved path",
        ADDON_FILE_PATHS,
        "    raise PathOutsideRootsError(ROOTS_REFUSAL)",
        '    raise PathOutsideRootsError(f"path {candidate} is outside the allowed file roots")',
        (
            f"{FPT}::test_the_containment_refusal_names_the_policy_not_a_path",
            f"{FPT}::test_a_sibling_directory_sharing_the_roots_prefix_is_refused",
            f"{FPT}::test_dotdot_traversal_is_normalised_before_the_containment_check",
            f"{FPT}::test_a_symlink_inside_a_root_pointing_outside_it_is_refused",
        ),
    ),
    Revert(
        "file paths: the open verdict decides nothing, so a directory, a missing file and a zip all pass",
        ADDON_FILE_PATHS,
        (
            "    if is_directory:\n"
            '        return "path is a directory, not a .blend file"\n'
            "    if not exists:\n"
            '        return "file does not exist"\n'
            "    if not readable:\n"
            '        return "file could not be read"\n'
            "    if not is_blend_header(header):\n"
            '        return "file is not a .blend file (unrecognised header)"\n'
            "    return None\n"
        ),
        "    return None\n",
        (
            f"{_OPEN_VERDICT}[a directory named x.blend is not a file]",
            f"{_OPEN_VERDICT}[missing]",
            f"{_OPEN_VERDICT}[unreadable is refused, not treated as a bad header]",
            f"{_OPEN_VERDICT}[a zip renamed .blend]",
        ),
    ),
    Revert(
        "file paths control: the header test refuses every file, a real .blend included",
        ADDON_FILE_PATHS,
        "    if not is_blend_header(header):\n",
        "    if True:\n",
        (f"{_OPEN_VERDICT}[a real .blend]",),
    ),
    Revert(
        "file paths: a directory is read as a missing file",
        ADDON_FILE_PATHS,
        ('    if is_directory:\n        return "path is a directory, not a .blend file"\n    if not exists:\n'),
        "    if not exists:\n",
        (f"{FPT}::test_a_directory_where_a_file_is_expected_is_refused",),
    ),
    Revert(
        "file paths: a directory is accepted as a save target",
        ADDON_FILE_PATHS,
        (
            "    if is_directory:\n"
            '        return "path is a directory, not a .blend file"\n'
            "    if not directory_exists:\n"
        ),
        "    if not directory_exists:\n",
        (
            f"{FPT}::test_a_directory_where_a_save_target_is_expected_is_refused",
            f"{_SAVE_VERDICT}[saving over a directory]",
        ),
    ),
    Revert(
        "file paths: a missing file is not refused before it is opened",
        ADDON_FILE_PATHS,
        ('    if not exists:\n        return "file does not exist"\n'),
        "",
        (f"{FPT}::test_a_missing_file_is_refused",),
    ),
    Revert(
        "file paths: the magic-byte check is skipped",
        ADDON_FILE_PATHS,
        "    if not is_blend_header(header):",
        "    if False:",
        (
            f"{FPT}::test_a_file_whose_magic_bytes_are_not_a_blend_is_refused",
            f"{PHT}::test_a_downloaded_blend_whose_header_is_not_a_blend_is_never_loaded",
        ),
    ),
    Revert(
        "file paths: an unreadable file's OSError text (and its path) reaches the refusal",
        ADDON_FILE_PATHS,
        ("        except OSError as exc:\n            cause = exc\n"),
        ('        except OSError as exc:\n            raise ValueError(f"file could not be read: {exc}") from exc\n'),
        (f"{FPT}::test_an_unreadable_file_is_refused_without_naming_it",),
    ),
    Revert(
        "file paths: a save target's missing directory is not refused",
        ADDON_FILE_PATHS,
        '        return "target directory does not exist; pass create_directories=true to create it"\n',
        "        return None\n",
        (
            f"{FPT}::test_a_save_target_whose_directory_does_not_exist_is_refused",
            f"{FLT}::test_save_shot_refuses_a_missing_directory_unless_asked_to_create_it",
            f"{FPT}::test_a_missing_save_directory_is_refused_without_the_opt_in",
        ),
    ),
    Revert(
        "create_directories: the opt-in is ignored, so a missing directory is refused anyway",
        ADDON_FILE_PATHS,
        ("    if not directory_exists:\n        if create_directories:\n            return None\n"),
        ("    if not directory_exists:\n        if False:\n            return None\n"),
        (f"{_SAVE_VERDICT}[a missing directory the caller opted into creating]",),
    ),
    Revert(
        "file paths: a save target's read-only directory is not refused",
        ADDON_FILE_PATHS,
        ('    if not directory_writable:\n        return "target directory is not writable"\n'),
        "",
        (
            f"{FPT}::test_a_save_target_in_a_read_only_directory_is_refused",
            f"{_SAVE_VERDICT}[create_directories does not excuse an unwritable existing directory]",
        ),
    ),
    Revert(
        "file paths: the magic check accepts only b'BLENDER', rejecting every compressed .blend",
        ADDON_FILE_PATHS,
        "BLEND_MAGIC_PREFIXES = (BLEND_MAGIC_UNCOMPRESSED, BLEND_MAGIC_ZSTD, BLEND_MAGIC_GZIP)",
        "BLEND_MAGIC_PREFIXES = (BLEND_MAGIC_UNCOMPRESSED,)",
        (
            f"{FPT}::test_a_zstd_compressed_blend_is_accepted",
            f"{FPT}::test_a_gzip_blend_written_without_an_fname_is_accepted",
            f"{PHT}::test_a_valid_downloaded_blend_is_still_imported",
        ),
    ),
    Revert(
        "file paths: the superseded 4-byte gzip constant, pinning the FNAME flag",
        ADDON_FILE_PATHS,
        r'BLEND_MAGIC_GZIP = b"\x1f\x8b"',
        r'BLEND_MAGIC_GZIP = b"\x1f\x8b\x08\x08"',
        (f"{FPT}::test_a_gzip_blend_written_without_an_fname_is_accepted",),
    ),
    Revert(
        "file paths: the superseded 12-byte BLENDER17-01 constant, pinning 5.x's header",
        ADDON_FILE_PATHS,
        'BLEND_MAGIC_UNCOMPRESSED = b"BLENDER"',
        'BLEND_MAGIC_UNCOMPRESSED = b"BLENDER17-01"',
        (f"{FPT}::test_a_pre_5x_blend_header_is_accepted",),
    ),
    # The `max()` moved out of the `read()` call into `BLEND_HEADER_BYTES`; the read is
    # still what this row shortens.
    Revert(
        "file paths: the header read is shorter than the longest prefix",
        ADDON_FILE_PATHS,
        "BLEND_HEADER_BYTES = max(len(prefix) for prefix in BLEND_MAGIC_PREFIXES)",
        "BLEND_HEADER_BYTES = len(BLEND_MAGIC_GZIP)",
        (
            f"{FPT}::test_an_uncompressed_blend_is_accepted",
            f"{FPT}::test_a_zstd_compressed_blend_is_accepted",
            f"{FPT}::test_a_pre_5x_blend_header_is_accepted",
        ),
    ),
    # The predicate the file check is built on has its own tests, which reach it with
    # bytes rather than a file: the rows above revert the constants it reads, these four
    # revert the comparison itself.
    Revert(
        "file paths: the header must equal a magic exactly, so the version digits every real header carries reject it",
        ADDON_FILE_PATHS,
        "    return header.startswith(BLEND_MAGIC_PREFIXES)",
        "    return header in BLEND_MAGIC_PREFIXES",
        tuple(
            f"{FPT}::test_every_header_form_blender_writes_is_recognised[{case}]"
            for case in ("5x-uncompressed", "pre-5x-uncompressed", "zstd", "gzip-with-an-fname")
        ),
    ),
    Revert(
        "file paths: the header comparison runs the other way round, so a short read matches every magic",
        ADDON_FILE_PATHS,
        "    return header.startswith(BLEND_MAGIC_PREFIXES)",
        "    return any(prefix.startswith(header) for prefix in BLEND_MAGIC_PREFIXES)",
        tuple(
            f"{FPT}::test_anything_that_is_not_a_header_is_rejected[{case}]"
            for case in ("empty-file", "truncated-to-inside-the-magic")
        ),
    ),
    Revert(
        "file paths: the magic is matched case-insensitively, so a lowercased near-miss is read as a .blend",
        ADDON_FILE_PATHS,
        "    return header.startswith(BLEND_MAGIC_PREFIXES)",
        "    return header.upper().startswith(BLEND_MAGIC_PREFIXES)",
        (f"{FPT}::test_anything_that_is_not_a_header_is_rejected[lowercased-near-miss]",),
    ),
    Revert(
        "file paths: the zstd magic truncated to three bytes, so a frame one byte off it is accepted",
        ADDON_FILE_PATHS,
        r'BLEND_MAGIC_ZSTD = b"\x28\xb5\x2f\xfd"',
        r'BLEND_MAGIC_ZSTD = b"\x28\xb5\x2f"',
        (f"{FPT}::test_anything_that_is_not_a_header_is_rejected[one-byte-off-zstd]",),
    ),
    Revert(
        "file paths: the sanitizer is bypassed and Blender's text goes out raw",
        ADDON_FILE_PATHS,
        "    text = _PATH_IN_TEXT.sub(_placeholder_for, text)",
        "    text = raw",
        (
            f"{FPT}::test_sanitizer_removes_the_path_from_a_missing_file_error",
            f"{FPT}::test_sanitizer_keeps_the_cause_when_nothing_follows_the_path",
            f"{FPT}::test_sanitizer_removes_the_process_cwd_from_the_empty_path_shape",
            f"{FPT}::test_sanitizer_removes_every_occurrence_of_the_path",
            f"{FPT}::test_sanitizer_removes_derived_temp_write_path",
            f"{FPT}::test_sanitizer_removes_the_path_from_a_library_reload_error",
            f"{FPT}::test_sanitizer_does_not_present_the_id_code_as_part_of_the_library_name",
            f"{FPT}::test_sanitizer_removes_quoted_paths_containing_a_space_and_an_apostrophe",
            f"{FPT}::test_sanitizer_removes_an_unquoted_path_containing_a_space",
            f"{FPT}::test_sanitizer_removes_windows_drive_and_unc_paths",
            f"{FPT}::test_sanitizer_removes_home_relative_paths",
            f"{PHT}::test_a_failed_blend_load_reports_no_absolute_path",
        ),
    ),
    Revert(
        "file paths: the sanitizer replaces only the first path (shape 3 ships its second copy)",
        ADDON_FILE_PATHS,
        "    text = _PATH_IN_TEXT.sub(_placeholder_for, text)",
        "    text = _PATH_IN_TEXT.sub(_placeholder_for, text, count=1)",
        (
            f"{FPT}::test_sanitizer_removes_every_occurrence_of_the_path",
            f"{FPT}::test_sanitizer_removes_quoted_paths_containing_a_space_and_an_apostrophe",
        ),
    ),
    Revert(
        "file paths: only quoted paths are detected (shape 4's bare derived `<abs>@` survives)",
        ADDON_FILE_PATHS,
        "(?P<bare>{_PATH_START}",
        "(?P<bare>(?!){_PATH_START}",
        (
            f"{FPT}::test_sanitizer_removes_derived_temp_write_path",
            f"{FPT}::test_sanitizer_removes_an_unquoted_path_containing_a_space",
            f"{FPT}::test_sanitizer_removes_home_relative_paths",
        ),
    ),
    Revert(
        "file paths: a bare path stops at its first space",
        ADDON_FILE_PATHS,
        r"(?:(?:\s+\S*[^\s:;,])*?\s+\S*[/\\]\S*?{_TRAILING})*)",
        r")",
        (f"{FPT}::test_sanitizer_removes_an_unquoted_path_containing_a_space",),
    ),
    Revert(
        "file paths: a quoted path ends at the first matching quote, even an apostrophe inside it",
        ADDON_FILE_PATHS,
        r"(?P=quote)(?=$|[\s:;,.?!)\]>])",
        "(?P=quote)",
        (f"{FPT}::test_sanitizer_removes_quoted_paths_containing_a_space_and_an_apostrophe",),
    ),
    Revert(
        "file paths: only POSIX-rooted paths are detected",
        ADDON_FILE_PATHS,
        r'_PATH_START = r"(?:/|\\\\|~[\w.-]*[/\\]|[A-Za-z]:[\\/])"',
        '_PATH_START = r"(?:/)"',
        (
            f"{FPT}::test_sanitizer_removes_windows_drive_and_unc_paths",
            f"{FPT}::test_sanitizer_removes_home_relative_paths",
        ),
    ),
    Revert(
        "file paths: a bare path may start mid-word, so `and/or` is cut",
        ADDON_FILE_PATHS,
        r"""(?<![^\s"'(\[=,])""",
        "",
        (f"{FPT}::test_sanitizer_leaves_text_without_a_path_alone",),
    ),
    Revert(
        "file paths: the reload shape's LI type code is presented as part of the library name",
        ADDON_FILE_PATHS,
        '    return _LIBRARY_ID_NAME.sub(r"\\1", text)',
        "    return text",
        (f"{FPT}::test_sanitizer_does_not_present_the_id_code_as_part_of_the_library_name",),
    ),
    Revert(
        "file paths: an exception with no text sanitizes to an empty error",
        ADDON_FILE_PATHS,
        "    if not raw:\n        return type(exc).__name__\n",
        "",
        (f"{FPT}::test_sanitizer_names_the_exception_type_when_it_carries_no_text",),
    ),
    Revert(
        "polyhaven: Poly Haven loads the download without checking it is a .blend",
        ADDON_POLYHAVEN,
        "        return resolve_blend_path(path, roots=[download_dir], must_exist=True)",
        "        return path",
        (f"{PHT}::test_a_downloaded_blend_whose_header_is_not_a_blend_is_never_loaded",),
    ),
    Revert(
        "polyhaven: Poly Haven does not contain the download to its own directory",
        ADDON_POLYHAVEN,
        "        return resolve_blend_path(path, roots=[download_dir], must_exist=True)",
        "        return resolve_blend_path(path, roots=[], must_exist=True)",
        (f"{PHT}::test_a_downloaded_blend_resolving_outside_its_download_directory_is_never_loaded",),
    ),
    Revert(
        "polyhaven: Poly Haven's download is held to the deployment's file roots, breaking the import",
        ADDON_POLYHAVEN,
        "        return resolve_blend_path(path, roots=[download_dir], must_exist=True)",
        "        return resolve_blend_path(path, roots=configured_file_roots(), must_exist=True)",
        (f"{PHT}::test_a_valid_downloaded_blend_is_still_imported",),
        ("\n\nfrom ..output_roots import configured_file_roots\n"),
    ),
    Revert(
        "polyhaven: Poly Haven's import error reaches the client unsanitized",
        ADDON_POLYHAVEN,
        '{"error": f"Failed to import model: {sanitize_blender_error(e)}"}',
        '{"error": f"Failed to import model: {e!s}"}',
        (f"{PHT}::test_a_failed_blend_load_reports_no_absolute_path",),
    ),
    # All four re-anchor onto the single line `configured_file_roots` is now: one
    # `split_roots` call per variable, and the `or` between them is the fallback.
    Revert(
        "file roots: file roots ignore their own variable when the output roots are set",
        ADDON_OUTPUT_ROOTS,
        "    return split_roots(source.get(FILE_ROOTS_ENV_VAR)) or split_roots(source.get(OUTPUT_ROOTS_ENV_VAR))",
        "    return split_roots(source.get(OUTPUT_ROOTS_ENV_VAR)) or split_roots(source.get(FILE_ROOTS_ENV_VAR))",
        (f"{ROOTST}::test_configured_file_roots_read_their_own_variable_first",),
    ),
    Revert(
        "file roots: file roots do not fall back to the output roots",
        ADDON_OUTPUT_ROOTS,
        "    return split_roots(source.get(FILE_ROOTS_ENV_VAR)) or split_roots(source.get(OUTPUT_ROOTS_ENV_VAR))",
        "    return split_roots(source.get(FILE_ROOTS_ENV_VAR))",
        (
            f"{ROOTST}::test_configured_file_roots_fall_back_to_the_output_roots",
            f"{ROOTST}::test_a_blank_file_roots_variable_counts_as_unset",
        ),
    ),
    Revert(
        "file roots: a blank file-roots variable counts as set, so the deployment silently goes permissive",
        ADDON_OUTPUT_ROOTS,
        "    return split_roots(source.get(FILE_ROOTS_ENV_VAR)) or split_roots(source.get(OUTPUT_ROOTS_ENV_VAR))",
        "    return split_roots(source.get(FILE_ROOTS_ENV_VAR)) if FILE_ROOTS_ENV_VAR in source "
        "else split_roots(source.get(OUTPUT_ROOTS_ENV_VAR))",
        (f"{ROOTST}::test_a_blank_file_roots_variable_counts_as_unset",),
    ),
    Revert(
        "file roots: the enforced roots borrow the advisory home-directory default",
        ADDON_OUTPUT_ROOTS,
        "    return split_roots(source.get(FILE_ROOTS_ENV_VAR)) or split_roots(source.get(OUTPUT_ROOTS_ENV_VAR))",
        "    return split_roots(source.get(FILE_ROOTS_ENV_VAR)) or split_roots(source.get(OUTPUT_ROOTS_ENV_VAR))"
        ' or [os.path.expanduser("~")]',
        (f"{ROOTST}::test_configured_file_roots_never_include_the_advisory_defaults",),
    ),
    Revert(
        "file roots: the handshake publishes the configured roots un-canonicalized",
        ADDON_SERVER_CORE,
        "    return tuple(dict.fromkeys(canonical_path(root) for root in roots))",
        "    return tuple(dict.fromkeys(roots))",
        (f"{ROOTST}::test_get_addon_info_publishes_enforced_file_roots_in_canonical_form",),
    ),
    Revert(
        "file roots: the handshake publishes the advisory writable roots as the enforced ones",
        ADDON_SERVER_CORE,
        "        roots = list(_canonical_file_roots(tuple(configured_file_roots())))",
        "        roots = BlenderMCPServer._writable_output_roots()",
        (f"{ROOTST}::test_get_addon_info_publishes_a_permissive_policy_when_no_roots_are_configured",),
    ),
    Revert(
        "file roots: the handshake never reports the policy as enforced",
        ADDON_SERVER_CORE,
        '"file_roots_enforced": bool(roots)',
        '"file_roots_enforced": False',
        (f"{ROOTST}::test_get_addon_info_publishes_enforced_file_roots_in_canonical_form",),
    ),
    Revert(
        "handshake: the server drops the addon's file roots",
        ADDON_MANAGER,
        '            file_roots=normalized_session_text_list(info.get("file_roots")),',
        "            file_roots=[],",
        (f"{AMT}::test_handshake_surfaces_the_file_path_policy",),
    ),
    Revert(
        "handshake: the file roots cross the server boundary unnormalized",
        ADDON_MANAGER,
        '            file_roots=normalized_session_text_list(info.get("file_roots")),',
        '            file_roots=list(info.get("file_roots") or []),',
        (
            f"{AMT}::test_every_handshake_field_refuses_the_same_hostile_string[file_roots]",
            f"{AMT}::test_a_hostile_element_inside_a_list_field_is_dropped_not_published[file_roots]",
        ),
    ),
    Revert(
        "handshake: file_roots_enforced carries the payload itself rather than a verdict about it",
        ADDON_MANAGER,
        '            file_roots_enforced=info.get("file_roots_enforced") is True,',
        '            file_roots_enforced=info.get("file_roots_enforced"),',
        (
            f"{AMT}::test_every_handshake_field_refuses_the_same_hostile_string[file_roots_enforced]",
            f"{AMT}::test_handshake_reads_an_addon_that_omits_the_file_path_policy_as_permissive",
        ),
    ),
    # --- installing the addon leaves exactly one addon for Blender to list ---
    Revert(
        "install: the backup is kept inside the directory Blender scans for addons",
        ADDON_MANAGER,
        '    backup = backup_directory(path.parent) / (path.name + ".bak")\n',
        '    backup = path.with_name(path.name + ".bak")\n',
        (
            f"{AMT}::test_repeat_installs_leave_one_addon_for_blender_to_load",
            f"{AMT}::test_repeat_install_preserves_original_backup",
        ),
    ),
    Revert(
        "install: the installer treats its own backup as an install and backs that up too",
        ADDON_MANAGER,
        '        if path.name.endswith(".bak"):\n            stale_backups.append(str(path))\n            continue\n',
        "",
        (f"{AMT}::test_install_leaves_an_older_installers_backup_alone_and_names_it",),
    ),
    Revert(
        "install: a development symlink is copied through instead of being left alone",
        ADDON_MANAGER,
        "    if target.is_symlink():\n",
        "    if False:\n",
        (f"{AMT}::test_install_refuses_to_write_through_a_development_symlink",),
    ),
    Revert(
        "get_addon_status: get_addon_status hardcodes the policy as unenforced",
        SERVER_CORE_TOOL,
        '        "file_roots_enforced": result.file_roots_enforced,',
        '        "file_roots_enforced": False,',
        (f"{CORET}::test_get_addon_status_reports_the_file_path_policy",),
    ),
    Revert(
        "get_addon_status: the file-policy keys go undocumented",
        SERVER_CORE_TOOL,
        '"file_roots"/"file_roots_enforced"',
        "file roots and whether enforced",
        (f"{CORET}::test_get_addon_status_documents_every_key_it_returns",),
    ),
    # --- cause text, known paths, case-folding volumes, Poly Haven siblings ---
    Revert(
        "file paths: a bare path extends across a word ending in ':' into the cause",
        ADDON_FILE_PATHS,
        r"(?:(?:\s+\S*[^\s:;,])*?",
        r"(?:(?:\s+\S+)*?",
        (f"{FPT}::test_sanitizer_keeps_an_errno_text_that_contains_a_slash",),
    ),
    Revert(
        "file paths: a lone '/' is taken for a path",
        ADDON_FILE_PATHS,
        "{_PATH_START}(?=\\S)",
        "{_PATH_START}",
        (f"{FPT}::test_sanitizer_keeps_an_errno_text_that_contains_a_slash",),
    ),
    Revert(
        "file paths: a bare path swallows the punctuation that closes it",
        ADDON_FILE_PATHS,
        r"(?=\S)\S*?{_TRAILING}",
        r"(?=\S)\S*",
        (f"{FPT}::test_sanitizer_leaves_punctuation_after_a_bare_path",),
    ),
    # The library-name pattern closes on the same punctuation set, so the anchor carries the
    # back-reference that only the quoted-path alternative has.
    Revert(
        "file paths: a quoted path closed by '?', ')' or '>' is not recognised as quoted",
        ADDON_FILE_PATHS,
        r"(?P=quote)(?=$|[\s:;,.?!)\]>])",
        r"(?P=quote)(?=$|[\s:;,.)\]])",
        (f"{FPT}::test_sanitizer_keeps_punctuation_closing_a_quoted_path",),
    ),
    Revert(
        "file paths: known paths are ignored",
        ADDON_FILE_PATHS,
        "        text = text.replace(known, PATH_PLACEHOLDER)",
        "        pass",
        (
            f"{FPT}::test_sanitizer_replaces_a_known_path_whole_even_with_a_space_in_its_leaf",
            f"{FPT}::test_sanitizer_replaces_the_derived_temp_name_of_a_known_path",
        ),
    ),
    Revert(
        "file paths: a known path's derived '@' temp name is not known",
        ADDON_FILE_PATHS,
        '    return usable | {f"{path}@" for path in usable}',
        "    return usable",
        (f"{FPT}::test_sanitizer_replaces_the_derived_temp_name_of_a_known_path",),
    ),
    Revert(
        "file paths: known paths replaced shortest-first, leaving '<path>@'",
        ADDON_FILE_PATHS,
        "key=len, reverse=True)",
        "key=len)",
        (f"{FPT}::test_sanitizer_replaces_the_derived_temp_name_of_a_known_path",),
    ),
    # The ancestor walk is one pass over the roots now, after every root has been tried by
    # spelling, so the row deletes that pass rather than a root's turn in the first loop.
    Revert(
        "file paths: containment compares spellings only, refusing a case variant on APFS",
        ADDON_FILE_PATHS,
        "    if any(_has_ancestor_directory(candidate, canonical_root) for canonical_root in canonical_roots):\n"
        "        return\n",
        "",
        (f"{FPT}::test_a_root_spelled_in_another_case_still_contains_its_files",),
    ),
    Revert(
        "polyhaven: the HDRI setup error goes out raw",
        ADDON_POLYHAVEN,
        "Failed to set up HDRI in Blender: {sanitize_blender_error(e)}",
        "Failed to set up HDRI in Blender: {e!s}",
        (f"{PHT}::test_a_failed_hdri_setup_reports_no_absolute_path",),
    ),
    Revert(
        "polyhaven: the texture processing error goes out raw",
        ADDON_POLYHAVEN,
        "Failed to process textures: {sanitize_blender_error(e)}",
        "Failed to process textures: {e!s}",
        (f"{PHT}::test_a_failed_texture_load_reports_no_absolute_path",),
    ),
    Revert(
        "polyhaven: the asset import's outer error goes out raw",
        ADDON_POLYHAVEN,
        "Failed to download asset: {sanitize_blender_error(e)}",
        "Failed to download asset: {e!s}",
        (f"{PHT}::test_a_failure_before_any_download_reports_no_absolute_path[import_polyhaven_asset-arguments0]",),
    ),
    Revert(
        "polyhaven: the categories error goes out raw",
        ADDON_POLYHAVEN,
        '            return {"error": sanitize_blender_error(e)}\n\n    def list_polyhaven_assets',
        '            return {"error": str(e)}\n\n    def list_polyhaven_assets',
        (f"{PHT}::test_a_failure_before_any_download_reports_no_absolute_path[get_polyhaven_categories-arguments1]",),
    ),
    Revert(
        "polyhaven: the asset listing error goes out raw",
        ADDON_POLYHAVEN,
        ('            return {"error": sanitize_blender_error(e)}\n\n    def _configured_environment'),
        ('            return {"error": str(e)}\n\n    def _configured_environment'),
        (f"{PHT}::test_a_failure_before_any_download_reports_no_absolute_path[list_polyhaven_assets-arguments2]",),
    ),
    # --- open_shot, save_shot, reset_session ---
    Revert(
        "file lifecycle: open_mainfile inherits use_scripts instead of passing False",
        ADDON_FILE_LIFECYCLE,
        "bpy.ops.wm.open_mainfile(filepath=canonical, load_ui=load_ui, use_scripts=False)",
        "bpy.ops.wm.open_mainfile(filepath=canonical, load_ui=load_ui)",
        (f"{FLT}::test_open_shot_passes_use_scripts_false_explicitly",),
    ),
    Revert(
        "file lifecycle: open_mainfile inherits load_ui (the operator default is True)",
        ADDON_FILE_LIFECYCLE,
        "bpy.ops.wm.open_mainfile(filepath=canonical, load_ui=load_ui, use_scripts=False)",
        "bpy.ops.wm.open_mainfile(filepath=canonical, use_scripts=False)",
        (f"{FLT}::test_open_shot_passes_load_ui_false_explicitly_by_default",),
    ),
    Revert(
        "file lifecycle: use_scripts exposed as an open_shot parameter",
        ADDON_FILE_LIFECYCLE,
        "self, filepath: object, load_ui: object = False, discard_unsaved: object = False\n",
        "self, filepath: object, load_ui: object = False, discard_unsaved: object = False, "
        "use_scripts: object = False\n",
        (f"{FLT}::test_no_file_command_takes_a_use_scripts_parameter",),
    ),
    Revert(
        "file lifecycle: a server-side tool schema names use_scripts",
        SERVER_CORE_TOOL,
        None,
        "\n# use_scripts\n",
        (f"{FLT}::test_use_scripts_appears_in_no_server_side_schema",),
    ),
    Revert(
        "file lifecycle: open_shot destroys unsaved work without asking",
        ADDON_FILE_LIFECYCLE,
        "        if dirty and not discard_unsaved:\n",
        "        if False:\n",
        (f"{FLT}::test_open_shot_refuses_a_dirty_session_without_discard_unsaved",),
    ),
    Revert(
        "file lifecycle: discard_unsaved is ignored, so a dirty session can never be replaced",
        ADDON_FILE_LIFECYCLE,
        "        if dirty and not discard_unsaved:\n",
        "        if dirty:\n",
        (f"{FLT}::test_open_shot_opens_a_dirty_session_when_discard_unsaved_is_true",),
    ),
    Revert(
        "file lifecycle: use_scripts_auto_execute is not checked before the load",
        ADDON_FILE_LIFECYCLE,
        "        refuse_scripts_auto_execute()\n",
        "",
        (
            f"{FLT}::test_open_shot_refuses_while_scripts_auto_execute_is_enabled",
            f"{FLT}::test_open_shot_refuses_when_the_auto_execute_preference_cannot_be_read",
        ),
    ),
    Revert(
        "file lifecycle: an unreadable auto-execute preference is read as off (fail open)",
        ADDON_BLEND_FILES,
        'if getattr(filepaths, "use_scripts_auto_execute", True) is not False:',
        'if getattr(filepaths, "use_scripts_auto_execute", False) is True:',
        (f"{FLT}::test_open_shot_refuses_when_the_auto_execute_preference_cannot_be_read",),
    ),
    Revert(
        "file lifecycle: the auto-execute check refuses whatever the preference says",
        ADDON_BLEND_FILES,
        'if getattr(filepaths, "use_scripts_auto_execute", True) is not False:',
        "if True:",
        (f"{FLT}::test_open_shot_proceeds_while_scripts_auto_execute_is_disabled",),
    ),
    Revert(
        "file lifecycle: resolve_blend_path skipped, the raw path reaches the operator",
        ADDON_BLEND_FILES,
        (
            "    return resolve_blend_path(\n"
            "        _expand_blender_relative(raw),\n"
            "        roots=configured_file_roots(),\n"
            "        must_exist=must_exist,\n"
            "        create_directories=create_directories,\n"
            "    )\n"
        ),
        (
            "    expanded = _expand_blender_relative(raw)\n"
            '    if isinstance(expanded, str) and expanded.strip() and "\\x00" not in expanded:\n'
            "        enforce_roots(expanded, configured_file_roots())\n"
            "    return str(expanded)\n"
        ),
        (
            *(
                f"{FLT}::test_open_shot_validates_the_path_before_any_operator_runs[{case}]"
                for case in ("missing", "directory", "non-blend", "bad-magic", "empty")
            ),
            f"{FLT}::test_each_file_command_is_answered_exactly_once_through_the_drain_loop[open refused]",
        ),
        ("\n\nfrom ..file_paths import enforce_roots\n"),
    ),
    Revert(
        "file lifecycle: a // path in an unsaved session resolves against the process CWD",
        ADDON_BLEND_FILES,
        ('    if not bpy.data.filepath:\n        raise ValueError(\n            "a Blender-relative'),
        ('    if False:\n        raise ValueError(\n            "a Blender-relative'),
        (
            f"{FLT}::test_open_shot_refuses_a_blender_relative_path_in_an_unsaved_session",
            f"{FLT}::test_save_shot_refuses_a_blender_relative_path_in_an_unsaved_session",
        ),
    ),
    Revert(
        "file lifecycle: a // path is never expanded",
        ADDON_BLEND_FILES,
        "    return bpy.path.abspath(raw)\n",
        "    return raw\n",
        (f"{FLT}::test_open_shot_expands_a_blender_relative_path_against_the_open_file",),
    ),
    Revert(
        "file lifecycle: file roots not enforced on open or save",
        ADDON_BLEND_FILES,
        "        roots=configured_file_roots(),\n",
        "        roots=[],\n",
        (
            f"{FLT}::test_open_shot_enforces_the_configured_roots",
            f"{FLT}::test_open_shot_refuses_outside_the_roots_before_saying_whether_the_file_exists",
            f"{FLT}::test_save_shot_enforces_the_roots_for_an_explicit_target_and_for_the_open_file",
            f"{FLT}::test_save_shot_creates_no_directory_outside_the_roots_or_on_a_refusal",
        ),
    ),
    Revert(
        "file lifecycle: roots checked after the file checks, so existence leaks outside the roots",
        ADDON_FILE_PATHS,
        (
            "    resolved = canonical_path(raw)\n"
            "    enforce_roots(resolved, roots)\n"
            "    if not (_has_blend_suffix(raw) and _has_blend_suffix(resolved)):\n"
            '        raise ValueError("path must name a file ending in .blend")\n'
            "    if must_exist:\n"
            "        _require_blend_file(resolved)\n"
            "    else:\n"
            "        _require_save_target(resolved, create_directories=create_directories)\n"
        ),
        (
            "    resolved = canonical_path(raw)\n"
            "    if not (_has_blend_suffix(raw) and _has_blend_suffix(resolved)):\n"
            '        raise ValueError("path must name a file ending in .blend")\n'
            "    if must_exist:\n"
            "        _require_blend_file(resolved)\n"
            "    else:\n"
            "        _require_save_target(resolved, create_directories=create_directories)\n"
            "    enforce_roots(resolved, roots)\n"
        ),
        (f"{FLT}::test_open_shot_refuses_outside_the_roots_before_saying_whether_the_file_exists",),
    ),
    Revert(
        "file lifecycle: the in-place save target is not held to the roots",
        ADDON_FILE_LIFECYCLE,
        "        canonical = checked_blend_path(requested, must_exist=False, create_directories=create_directories)\n",
        (
            "        canonical = (\n"
            "            str(requested)\n"
            "            if in_place\n"
            "            else checked_blend_path(requested, must_exist=False, create_directories=create_directories)\n"
            "        )\n"
        ),
        (f"{FLT}::test_save_shot_enforces_the_roots_for_an_explicit_target_and_for_the_open_file",),
    ),
    Revert(
        "file lifecycle: open_mainfile's RuntimeError reaches the client raw",
        ADDON_FILE_LIFECYCLE,
        'raise RuntimeError(operator_failure_message("open_shot", exc, (filepath, canonical))) from exc',
        "raise RuntimeError(str(exc)) from exc",
        (
            f"{FLT}::test_a_runtime_error_from_open_mainfile_is_a_clean_error_response",
            f"{FLT}::test_each_file_command_is_answered_exactly_once_through_the_drain_loop[open raises]",
        ),
    ),
    Revert(
        "file lifecycle: the sanitizer is not given the known paths (structural detection only)",
        ADDON_BLEND_FILES,
        "sanitize_blender_error(exc, known_paths=known)",
        "sanitize_blender_error(exc)",
        (f"{FLT}::test_the_known_path_closes_what_structural_detection_leaves_behind",),
    ),
    Revert(
        "file lifecycle: a save operator's RuntimeError reaches the client raw",
        ADDON_FILE_LIFECYCLE,
        (
            '        message = operator_failure_message("save_shot", exc, (request.requested, request.canonical))\n'
            "        raise RuntimeError(message) from exc\n"
        ),
        "        raise RuntimeError(str(exc)) from exc\n",
        (
            f"{FLT}::test_a_runtime_error_from_a_save_operator_is_a_clean_error_response[save_as_mainfile]",
            f"{FLT}::test_a_runtime_error_from_a_save_operator_is_a_clean_error_response[save_mainfile]",
            f"{FLT}::test_each_file_command_is_answered_exactly_once_through_the_drain_loop[save raises]",
        ),
    ),
    Revert(
        "file lifecycle: the reset operator's RuntimeError reaches the client raw",
        ADDON_FILE_LIFECYCLE,
        'raise RuntimeError(operator_failure_message("reset_session", exc, (previous,))) from exc',
        "raise RuntimeError(str(exc)) from exc",
        (
            f"{FLT}::test_a_runtime_error_from_the_reset_operator_is_a_clean_error_response",
            f"{FLT}::test_each_file_command_is_answered_exactly_once_through_the_drain_loop[reset raises]",
        ),
    ),
    Revert(
        "file lifecycle: a swap result does not tell the client to re-handshake",
        ADDON_FILE_LIFECYCLE,
        '            "rehandshake_required": True,\n',
        '            "rehandshake_required": False,\n',
        (
            f"{FLT}::test_open_shot_reports_the_new_session_and_asks_for_a_rehandshake",
            f"{FLT}::test_reset_session_reads_the_empty_factory_startup_file_and_never_factory_settings",
            f"{FLT}::test_a_swap_that_landed_is_still_reported_as_a_success_when_its_report_fails",
        ),
    ),
    Revert(
        "file lifecycle: capabilities_changed is hard-coded False",
        ADDON_FILE_LIFECYCLE,
        'report["capabilities_changed"] = self._capability_names() != capabilities_before',
        'report["capabilities_changed"] = False',
        (f"{FLT}::test_open_shot_reports_that_the_capability_set_followed_the_file",),
    ),
    Revert(
        "file lifecycle: a failing post-swap report turns a landed swap into an error",
        ADDON_FILE_LIFECYCLE,
        ('        except _POST_SWAP_READ_ERRORS as exc:\n            print(f"BlenderMCP: the swap completed'),
        ('        except ZeroDivisionError as exc:\n            print(f"BlenderMCP: the swap completed'),
        (f"{FLT}::test_a_swap_that_landed_is_still_reported_as_a_success_when_its_report_fails",),
    ),
    Revert(
        "file lifecycle: flags are coerced with bool(), so the string 'true' confirms",
        ADDON_BLEND_FILES,
        (
            "    if not isinstance(value, bool):\n"
            '        raise ValueError(f"{name} must be true or false")\n'
            "    return value\n"
        ),
        "    return bool(value)\n",
        tuple(
            f"{FLT}::test_a_flag_that_is_not_a_real_bool_is_refused[{case}]"
            for case in (
                "open_shot-load_ui",
                "open_shot-discard_unsaved",
                "save_shot-compress",
                "save_shot-relative_remap",
                "save_shot-confirm_overwrite",
                "reset_session-confirm",
            )
        ),
    ),
    Revert(
        "file lifecycle: save inherits relative_remap (save_as_mainfile's default is True)",
        ADDON_FILE_LIFECYCLE,
        "operator(filepath=request.canonical, compress=request.compress, relative_remap=request.relative_remap)",
        "operator(filepath=request.canonical, compress=request.compress)",
        (
            f"{FLT}::test_save_shot_passes_compress_and_relative_remap_false_explicitly",
            f"{FLT}::test_save_shot_in_place_uses_save_mainfile_with_explicit_arguments",
        ),
    ),
    Revert(
        "file lifecycle: save inherits compress (use_file_compression wins at factory settings)",
        ADDON_FILE_LIFECYCLE,
        "operator(filepath=request.canonical, compress=request.compress, relative_remap=request.relative_remap)",
        "operator(filepath=request.canonical, relative_remap=request.relative_remap)",
        (
            f"{FLT}::test_save_shot_passes_compress_and_relative_remap_false_explicitly",
            f"{FLT}::test_save_shot_in_place_uses_save_mainfile_with_explicit_arguments",
        ),
    ),
    Revert(
        "file lifecycle: an explicit compress / relative_remap opt-in is dropped",
        ADDON_FILE_LIFECYCLE,
        "operator(filepath=request.canonical, compress=request.compress, relative_remap=request.relative_remap)",
        "operator(filepath=request.canonical, compress=False, relative_remap=False)",
        (f"{FLT}::test_save_shot_forwards_an_explicit_opt_in",),
    ),
    Revert(
        "file lifecycle: no overwrite pre-check, check_existing left to guard (it does not)",
        ADDON_FILE_LIFECYCLE,
        "        if exists and not confirm_overwrite:\n",
        "        if False:\n",
        (
            f"{FLT}::test_saving_over_an_existing_file_without_confirmation_calls_no_operator",
            f"{FLT}::test_save_shot_in_place_needs_confirmation_because_it_overwrites_the_file_on_disk",
            f"{FLT}::test_each_file_command_is_answered_exactly_once_through_the_drain_loop[save refused]",
        ),
    ),
    Revert(
        "file lifecycle: confirm_overwrite is ignored, so an existing file can never be replaced",
        ADDON_FILE_LIFECYCLE,
        "        if exists and not confirm_overwrite:\n",
        "        if exists:\n",
        (
            f"{FLT}::test_saving_over_an_existing_file_with_confirmation_writes_it",
            f"{FLT}::test_save_shot_in_place_uses_save_mainfile_with_explicit_arguments",
            f"{FLT}::test_a_runtime_error_from_a_save_operator_is_a_clean_error_response[save_mainfile]",
        ),
    ),
    Revert(
        "file lifecycle: an in-place save on a never-saved session is not refused up front",
        ADDON_FILE_LIFECYCLE,
        "        if in_place and not bpy.data.filepath:\n",
        "        if False:\n",
        (f"{FLT}::test_save_shot_in_place_on_an_unsaved_session_is_refused_with_an_actionable_message",),
    ),
    Revert(
        "file lifecycle: an in-place save goes through save_as_mainfile",
        ADDON_FILE_LIFECYCLE,
        "    operator = bpy.ops.wm.save_mainfile if request.in_place else bpy.ops.wm.save_as_mainfile",
        "    operator = bpy.ops.wm.save_as_mainfile",
        (
            f"{FLT}::test_save_shot_in_place_uses_save_mainfile_with_explicit_arguments",
            f"{FLT}::test_a_runtime_error_from_a_save_operator_is_a_clean_error_response[save_mainfile]",
        ),
    ),
    Revert(
        "file lifecycle: reset_session runs without confirm",
        ADDON_FILE_LIFECYCLE,
        '        if not require_bool("confirm", confirm):\n',
        '        if not require_bool("confirm", True):\n',
        (
            f"{FLT}::test_reset_session_without_confirm_is_refused",
            f"{FLT}::test_a_flag_that_is_not_a_real_bool_is_refused[reset_session-confirm]",
            f"{FLT}::test_each_file_command_is_answered_exactly_once_through_the_drain_loop[reset refused]",
        ),
    ),
    Revert(
        "file lifecycle: reset_session uses read_factory_settings (unregisters every add-on)",
        ADDON_FILE_LIFECYCLE,
        "bpy.ops.wm.read_homefile(use_empty=True, use_factory_startup=True, load_ui=False)",
        "bpy.ops.wm.read_factory_settings(use_empty=True)",
        (
            f"{FLT}::test_reset_session_reads_the_empty_factory_startup_file_and_never_factory_settings",
            f"{FLT}::test_a_runtime_error_from_the_reset_operator_is_a_clean_error_response",
            f"{FLT}::test_each_file_command_is_answered_exactly_once_through_the_drain_loop[reset raises]",
        ),
    ),
    Revert(
        "file lifecycle: the three commands are not in the dispatch table",
        ADDON_SERVER_CORE,
        '        "open_shot": CommandSpec(session_swap=True, indeterminate_safe=True),\n'
        '        "save_shot": CommandSpec(tick_ending=True),\n'
        '        "reset_session": CommandSpec(session_swap=True, indeterminate_safe=True),\n',
        "",
        (
            f"{FLT}::test_the_file_commands_are_dispatchable_and_advertised_beside_get_session_info",
            *(
                f"{FLT}::test_each_file_command_is_answered_exactly_once_through_the_drain_loop[{case}]"
                for case in ("open ok", "save ok", "reset ok")
            ),
        ),
    ),
    # --- save_shot: the dirty flag, relative-link warnings, the temp name ---
    Revert(
        "file lifecycle: the drain tick does not end after save_shot, so an edit behind it loses its dirty flag",
        ADDON_SERVER_CORE,
        '            if self.command_spec(command.get("type")).tick_ending:\n                break\n',
        "",
        (f"{FLT}::test_the_drain_tick_ends_after_a_save_so_a_queued_edit_runs_after_blender_clears_the_dirty_flag",),
    ),
    Revert(
        "file lifecycle: save_shot reports a same-tick is_dirty that Blender has not cleared yet",
        ADDON_FILE_LIFECYCLE,
        ('        "relative_remap": request.relative_remap,\n        "session_id": session["session_id"],\n'),
        (
            '        "relative_remap": request.relative_remap,\n'
            '        "is_dirty": bool(bpy.data.is_dirty),\n'
            '        "session_id": session["session_id"],\n'
        ),
        (f"{FLT}::test_save_shot_does_not_report_a_dirty_flag_blender_has_not_cleared_yet",),
    ),
    Revert(
        "file lifecycle: no warning for //-relative links a save to a new directory breaks",
        ADDON_FILE_LIFECYCLE,
        "        broken_links = _unresolvable_relative_paths(request.canonical, request.relative_remap)\n",
        "        broken_links = 0\n",
        (
            f"{FLT}::test_saving_to_another_directory_warns_about_relative_links_that_will_not_resolve",
            f"{FLT}::test_a_relative_image_path_counts_as_an_external_path_that_will_not_resolve",
            f"{FLT}::test_an_indirect_library_is_not_counted_because_blender_rederives_it_from_its_parent",
        ),
    ),
    Revert(
        "file lifecycle: the relative-link warning ignores the directory and the remap flag",
        ADDON_FILE_LIFECYCLE,
        "    if relative_remap or not current:\n        return 0\n"
        "    if os.path.dirname(canonical) == os.path.dirname(canonical_path(current)):\n        return 0\n",
        "",
        tuple(
            f"{FLT}::test_no_relative_link_warning_when_the_links_still_resolve[{case}]"
            for case in ("same directory", "relative_remap", "in place")
        ),
    ),
    Revert(
        "file lifecycle: a planted <target>@ is not checked, so the save follows it out of the roots",
        ADDON_FILE_LIFECYCLE,
        "        _refuse_a_leftover_temp_save(canonical)\n",
        "",
        tuple(
            f"{FLT}::test_a_leftover_temp_save_name_beside_the_target_refuses_the_save[{place}-{kind}]"
            for place in ("explicit", "in place")
            for kind in ("file", "dangling symlink", "directory")
        ),
    ),
    Revert(
        "file lifecycle: the temp-name check uses exists(), which a dangling symlink passes",
        ADDON_FILE_LIFECYCLE,
        '        os.lstat(f"{canonical}@")\n    except FileNotFoundError:\n        return\n',
        '        if not os.path.exists(f"{canonical}@"):\n'
        "            return\n"
        "    except FileNotFoundError:\n"
        "        return\n",
        tuple(
            f"{FLT}::test_a_leftover_temp_save_name_beside_the_target_refuses_the_save[{place}-dangling symlink]"
            for place in ("explicit", "in place")
        ),
    ),
    # --- save_shot: temp-name lstat failures, and which relative paths are counted ---
    Revert(
        "file lifecycle: an lstat failure other than not-found reads as a clear temp name",
        ADDON_FILE_LIFECYCLE,
        "    except OSError as exc:\n        raise ValueError(\n",
        "    except OSError as exc:\n        return\n        raise ValueError(\n",
        tuple(
            f"{FLT}::test_a_temp_save_name_that_cannot_be_checked_refuses_the_save[{place}-{errno_id}]"
            for errno_id in ("EACCES", "ENAMETOOLONG")
            for place in ("explicit", "in place")
        ),
    ),
    Revert(
        "file lifecycle: an occupied temp name is not said to be possibly left by an interrupted save",
        ADDON_FILE_LIFECYCLE,
        'already exists beside the target, possibly "\n        "left by an interrupted save;',
        'already exists beside the target; "\n        "remove it now;',
        (f"{FLT}::test_an_occupied_temp_save_name_says_it_may_be_left_by_an_interrupted_save",),
    ),
    Revert(
        "file lifecycle: only libraries are counted, so a relative image path breaks with no warning",
        ADDON_FILE_LIFECYCLE,
        "        for path in bpy.utils.blend_paths(absolute=False, packed=False, local=True)\n",
        "        for path in (library.filepath for library in bpy.data.libraries)\n",
        (
            f"{FLT}::test_a_relative_image_path_counts_as_an_external_path_that_will_not_resolve",
            f"{FLT}::test_an_indirect_library_is_not_counted_because_blender_rederives_it_from_its_parent",
        ),
    ),
    Revert(
        "file lifecycle: indirect libraries are counted although they re-resolve from their parent",
        ADDON_FILE_LIFECYCLE,
        "for library in bpy.data.libraries if is_indirect_library(library)",
        "for library in bpy.data.libraries if False",
        (f"{FLT}::test_an_indirect_library_is_not_counted_because_blender_rederives_it_from_its_parent",),
    ),
    # --- link_canon_library, create_override, list/reload/relocate/unlink ---
    Revert(
        "linking: link hands the raw path to Blender without roots or file checks",
        ADDON_LINKING,
        (
            "        canonical = checked_blend_path(filepath, must_exist=True)\n"
            '        refuse_scripts_auto_execute("link_canon_library")\n'
        ),
        ('        canonical = str(filepath)\n        refuse_scripts_auto_execute("link_canon_library")\n'),
        (f"{LKT}::test_link_validates_its_path_before_blender_reads_it", f"{LKT}::test_link_enforces_the_file_roots"),
    ),
    Revert(
        "linking: absent names are not refused inside the load block",
        ADDON_LINKING,
        "                _refuse_absent_names(data_from, collection_names, object_names)\n",
        "",
        (f"{LKT}::test_link_refuses_a_name_absent_from_the_file_and_leaves_no_library",),
    ),
    Revert(
        "linking: link_canon_library joins the replacing set, so a failed link keeps its Library",
        ADDON_SERVER_CORE,
        '        "link_canon_library": CommandSpec(),',
        '        "link_canon_library": CommandSpec(datablock_replacing=True),',
        (
            f"{LKT}::test_a_link_that_fails_after_linking_rolls_its_library_back",
            f"{LKT}::test_the_three_replacing_commands_never_enter_a_transaction_and_the_link_does",
        ),
    ),
    Revert(
        "linking: reload_library leaves the replacing set and is transacted",
        ADDON_SERVER_CORE,
        '        "reload_library": CommandSpec(datablock_replacing=True),',
        '        "reload_library": CommandSpec(),',
        (f"{LKT}::test_the_three_replacing_commands_never_enter_a_transaction_and_the_link_does",),
    ),
    Revert(
        "linking: a linked collection is not instanced, so the next save drops it",
        ADDON_LINKING,
        "            _link_into(scene.collection.children, linked_collections)  # type: ignore[attr-defined]\n",
        "",
        (f"{LKT}::test_link_instances_what_it_linked_so_a_save_keeps_it",),
    ),
    Revert(
        "linking: collections defaults to a mutable list",
        ADDON_LINKING,
        "        collections: object = None,\n",
        "        collections: object = [],  # noqa: B006\n",
        (f"{LKT}::test_link_names_default_to_none_and_are_not_shared_across_calls",),
    ),
    Revert(
        "linking: the link passes create_liboverrides (Route A) when as_override is set",
        ADDON_LINKING,
        "bpy.data.libraries.load(canonical, link=True, relative=relative)",
        "bpy.data.libraries.load(canonical, link=True, relative=relative, create_liboverrides=as_override)",
        (f"{LKT}::test_link_as_override_uses_route_c_not_create_liboverrides",),
    ),
    Revert(
        "linking: as_override with objects is not refused",
        ADDON_LINKING,
        "        if as_override and object_names:\n",
        "        if False:\n",
        (f"{LKT}::test_link_refuses_as_override_with_objects",),
    ),
    Revert(
        "linking: relative is accepted in a never-saved session",
        ADDON_LINKING,
        "        if relative and not bpy.data.filepath:\n",
        "        if False:\n",
        (f"{LKT}::test_link_refuses_relative_in_a_never_saved_session",),
    ),
    Revert(
        "linking: link flags are coerced with bool()",
        ADDON_LINKING,
        (
            '        as_override = require_bool("as_override", as_override)\n'
            '        relative = require_bool("relative", relative)\n'
        ),
        ("        as_override = bool(as_override)\n        relative = bool(relative)\n"),
        (
            f"{LKT}::test_link_flags_must_be_real_bools[as_override]",
            f"{LKT}::test_link_flags_must_be_real_bools[relative]",
        ),
    ),
    Revert(
        "linking: a libraries.load failure goes out raw",
        ADDON_LINKING,
        'raise RuntimeError(operator_failure_message("link_canon_library", exc, (filepath, canonical))) from exc',
        "raise RuntimeError(str(exc)) from exc",
        (f"{LKT}::test_a_blender_link_failure_reaches_the_client_without_its_path",),
    ),
    Revert(
        "linking: do_fully_editable is inherited (Route B, system overrides)",
        ADDON_LINKING,
        "            do_fully_editable=True,\n",
        "",
        (
            f"{LKT}::test_create_override_passes_do_fully_editable_true_explicitly",
            f"{LKT}::test_link_as_override_uses_route_c_not_create_liboverrides",
            f"{LKT}::test_create_override_reports_same_named_linked_and_override_objects_distinguishably",
        ),
    ),
    Revert(
        "linking: create_override takes the scene from bpy.context",
        ADDON_LINKING,
        '        report = _override_all([collection], _scene(scene_uid), detail=require_bool("detail", detail))[0]\n',
        '        report = _override_all([collection], bpy.context.scene, detail=require_bool("detail", detail))[0]\n',
        (f"{LKT}::test_create_override_takes_the_scene_from_bpy_data_not_bpy_context",),
    ),
    Revert(
        "linking: create_override takes the view layer from bpy.context",
        ADDON_LINKING,
        "            scene.view_layers[0],  # type: ignore[attr-defined]\n",
        "            bpy.context.view_layer,\n",
        (f"{LKT}::test_create_override_takes_the_scene_from_bpy_data_not_bpy_context",),
    ),
    Revert(
        "linking: with several scenes the first is used silently",
        ADDON_LINKING,
        "    if len(scenes) != 1:\n",
        "    if False:\n",
        (f"{LKT}::test_create_override_refuses_to_guess_between_scenes",),
    ),
    Revert(
        "linking: a local or override collection is not refused before Blender is asked",
        ADDON_LINKING,
        '    if getattr(collection, "library", None) is None:\n',
        "    if False:\n",
        (f"{LKT}::test_create_override_resolves_by_session_uid_among_same_named_collections",),
    ),
    Revert(
        "linking: an already-overridden collection is overridden again",
        ADDON_LINKING,
        "    if existing:\n",
        "    if False:\n",
        (f"{LKT}::test_create_override_resolves_by_session_uid_among_same_named_collections",),
    ),
    Revert(
        "linking: a bool or float resolves the datablock whose uid equals it",
        ADDON_LINKING,
        "    if isinstance(value, bool) or not isinstance(value, int):\n",
        "    if not isinstance(value, (int, float)):\n",
        (
            f"{LKT}::test_create_override_refuses_a_uid_that_is_not_an_integer[True]",
            f"{LKT}::test_create_override_refuses_a_uid_that_is_not_an_integer[1.0]",
            f"{LKT}::test_unlink_requires_an_explicit_bounded_uid_list[bool]",
        ),
    ),
    Revert(
        "linking: an override's reference is not reported",
        ADDON_LINKING,
        '        "reference_uid": _uid_of(getattr(override, "reference", None)),\n',
        '        "reference_uid": None,\n',
        (f"{LKT}::test_create_override_reports_same_named_linked_and_override_objects_distinguishably",),
    ),
    Revert(
        "linking: the linked instance stays beside its override",
        ADDON_LINKING,
        "            parent.children.unlink(collection)  # type: ignore[attr-defined]\n",
        "            pass\n",
        (f"{LKT}::test_create_override_replaces_the_linked_instance_it_overrides",),
    ),
    Revert(
        "linking: a None override is not checked",
        ADDON_LINKING,
        "    if override is None:\n",
        "    if False:\n",
        (f"{LKT}::test_create_override_that_blender_declines_is_an_error_and_rolls_back",),
    ),
    Revert(
        "linking: an override failure goes out raw",
        ADDON_LINKING,
        'raise RuntimeError(operator_failure_message("create_override", exc, known)) from exc',
        "raise RuntimeError(str(exc)) from exc",
        (f"{LKT}::test_an_override_failure_reaches_the_client_sanitized",),
    ),
    Revert(
        "linking: list_libraries is not read-only, so it pays for a transaction",
        ADDON_SERVER_CORE,
        '        "list_libraries": CommandSpec(read_only=True),',
        '        "list_libraries": CommandSpec(),',
        (
            f"{LKT}::test_list_libraries_is_a_read_only_command_and_never_enters_a_transaction",
            f"{LKT}::test_the_linking_commands_are_dispatchable_and_advertised",
            f"{LKT}::test_the_three_replacing_commands_never_enter_a_transaction_and_the_link_does",
        ),
    ),
    Revert(
        "linking: list_libraries ignores offset",
        ADDON_LINKING,
        "        page = libraries[offset : offset + limit]\n",
        "        page = libraries[:limit]\n",
        (f"{LKT}::test_list_libraries_paginates_and_reports_what_a_reload_decision_needs",),
    ),
    Revert(
        "linking: list_libraries page bounds are coerced, not checked",
        ADDON_LINKING,
        '        limit = _bounded_int("limit", limit, 1, MAX_PAGE_SIZE)\n'
        '        offset = _bounded_int("offset", offset, 0, None)\n',
        "        limit, offset = int(limit), int(offset)  # type: ignore[arg-type]\n",
        tuple(
            f"{LKT}::test_list_libraries_bounds_its_page[{case}]"
            for case in ("zero", "over-bound", "bool-limit", "string-limit", "negative-offset", "bool-offset")
        ),
    ),
    Revert(
        "linking: needs_liboverride_resync is not reported",
        ADDON_LINKING,
        '        "needs_liboverride_resync": bool(getattr(library, "needs_liboverride_resync", False)),\n',
        '        "needs_liboverride_resync": False,\n',
        (f"{LKT}::test_list_libraries_paginates_and_reports_what_a_reload_decision_needs",),
    ),
    Revert(
        "linking: linked datablocks are listed without their uids",
        ADDON_LINKING,
        '        "session_uid": _uid_of(datablock),\n        "name": _display_name(datablock),\n        "id_type"',
        '        "session_uid": None,\n        "name": _display_name(datablock),\n        "id_type"',
        (
            f"{LKT}::test_list_libraries_paginates_and_reports_what_a_reload_decision_needs",
            f"{LKT}::test_reload_library_uses_the_data_api_inside_the_replace_flag",
        ),
    ),
    Revert(
        "linking: the per-library datablock list is uncapped",
        ADDON_LINKING,
        "    shown = items[:limit]\n",
        "    shown = list(items)\n",
        (f"{LKT}::test_list_libraries_bounds_the_datablocks_it_lists_per_library",),
    ),
    Revert(
        "linking: the reload is not wrapped in replacing_library_contents",
        ADDON_LINKING,
        "        with replacing_library_contents():\n",
        "        if True:\n",
        (f"{LKT}::test_reload_library_uses_the_data_api_inside_the_replace_flag",),
    ),
    Revert(
        "linking: the reload goes through wm.lib_reload",
        ADDON_LINKING,
        "            library.reload()  # type: ignore[attr-defined]\n",
        "            bpy.ops.wm.lib_reload(library=library.name)  # type: ignore[attr-defined]\n",
        (
            f"{LKT}::test_no_wm_lib_operator_exists_in_the_linking_module",
            f"{LKT}::test_reload_library_uses_the_data_api_inside_the_replace_flag",
        ),
    ),
    Revert(
        "linking: a reload failure goes out raw",
        ADDON_LINKING,
        "        raise RuntimeError(operator_failure_message(command, exc, known_paths)) from exc\n",
        '        raise RuntimeError(f"{command} failed: {exc}") from exc\n',
        (
            f"{LKT}::test_reload_failure_reaches_the_client_sanitized_from_a_captured_blender_error",
            f"{LKT}::test_reload_failure_of_a_relative_link_under_a_comma_directory_is_sanitized",
            f"{LKT}::test_a_failed_relocate_restores_the_previous_path_and_is_sanitized",
        ),
    ),
    Revert(
        "linking: relative known paths reach the sanitizer and eat the library name",
        ADDON_LINKING,
        "    return tuple(path for path in paths if isinstance(path, str) and os.path.isabs(path))\n",
        "    return tuple(path for path in paths if isinstance(path, str))\n",
        (f"{LKT}::test_a_reload_failure_in_an_unsaved_session_still_names_the_library",),
    ),
    Revert(
        "linking: relocate hands the raw path to Blender without roots or file checks",
        ADDON_LINKING,
        (
            "        canonical = checked_blend_path(filepath, must_exist=True)\n"
            "        for other in bpy.data.libraries:\n"
        ),
        ("        canonical = str(filepath)\n        for other in bpy.data.libraries:\n"),
        (
            f"{LKT}::test_relocate_validates_the_new_path_through_the_roots",
            f"{LKT}::test_relocate_assigns_the_canonical_path_reloads_and_reports_the_name_both_sides",
        ),
    ),
    Revert(
        "linking: relocate to a file another library already links",
        ADDON_LINKING,
        "            if other.session_uid != library.session_uid and canonical in _library_paths(other):\n",
        "            if False:\n",
        (f"{LKT}::test_relocate_refuses_a_file_another_library_already_links",),
    ),
    Revert(
        "linking: a failed relocate leaves the library pointing at the new file",
        ADDON_LINKING,
        "            library.filepath = previous  # type: ignore[attr-defined]\n            raise\n",
        "            raise\n",
        (f"{LKT}::test_a_failed_relocate_restores_the_previous_path_and_is_sanitized",),
    ),
    Revert(
        "linking: relocate stores the path as the client spelled it",
        ADDON_LINKING,
        "        library.filepath = canonical  # type: ignore[attr-defined]\n",
        "        library.filepath = filepath  # type: ignore[attr-defined]\n",
        (f"{LKT}::test_relocate_assigns_the_canonical_path_reloads_and_reports_the_name_both_sides",),
    ),
    Revert(
        "linking: an unknown uid refusal does not say where to read a current one",
        ADDON_LINKING,
        '        "library reload, so read a current one from list_libraries"\n',
        '        "library reload"\n',
        (
            f"{LKT}::test_an_unknown_library_uid_is_refused[reload_library]",
            f"{LKT}::test_an_unknown_library_uid_is_refused[relocate_library]",
        ),
    ),
    Revert(
        "linking: unlink runs without confirm",
        ADDON_LINKING,
        "    if not confirm:\n",
        "    if False:\n",
        (f"{LKT}::test_unlink_refuses_without_a_real_confirmation[false]",),
    ),
    Revert(
        "linking: unlink's confirm is coerced with bool()",
        ADDON_LINKING,
        '        confirm = require_bool("confirm", confirm)\n',
        "        confirm = bool(confirm)\n",
        (
            f"{LKT}::test_unlink_refuses_without_a_real_confirmation[string]",
            f"{LKT}::test_unlink_refuses_without_a_real_confirmation[int]",
        ),
    ),
    Revert(
        "linking: unlink removes every library, not only the named ones",
        ADDON_LINKING,
        "            bpy.data.libraries.remove(current)\n",
        "            for everything in list(bpy.data.libraries):\n"
        "                bpy.data.libraries.remove(everything)\n",
        (f"{LKT}::test_unlink_never_touches_a_library_that_was_not_named",),
    ),
    Revert(
        "linking: unknown uids are skipped instead of refusing the request",
        ADDON_LINKING,
        "    libraries = [_library(uid) for uid in uids]\n",
        "    libraries = [lib for lib in bpy.data.libraries if lib.session_uid in uids]\n",
        (f"{LKT}::test_unlink_resolves_every_uid_before_removing_anything",),
    ),
    Revert(
        "linking: the uid list is neither non-empty nor bounded",
        ADDON_LINKING,
        "    if not isinstance(library_uids, list) or not 0 < len(library_uids) <= MAX_UNLINK_UIDS:\n",
        "    if not isinstance(library_uids, list):\n",
        (
            f"{LKT}::test_unlink_requires_an_explicit_bounded_uid_list[empty]",
            f"{LKT}::test_unlink_requires_an_explicit_bounded_uid_list[big]",
        ),
    ),
    Revert(
        "linking: uid list entries are coerced with int()",
        ADDON_LINKING,
        '    uids = list(dict.fromkeys(_require_uid("library_uids entry", uid) for uid in library_uids))\n',
        "    uids = list(dict.fromkeys(int(uid) for uid in library_uids))\n",
        (
            f"{LKT}::test_unlink_requires_an_explicit_bounded_uid_list[bool]",
            f"{LKT}::test_unlink_requires_an_explicit_bounded_uid_list[string]",
        ),
    ),
    Revert(
        "linking: the removal report counts only the libraries",
        ADDON_LINKING,
        '            "removed_by_type": summarize_type_counts(before[uid][0] for uid in removed),\n',
        '            "removed_by_type": {"libraries": len(removed_libraries)},\n',
        (f"{LKT}::test_unlink_reports_exactly_what_it_removed",),
    ),
    # One node, not two: `_newly_orphaned` keys its candidates by uid now, so a doubled walk
    # can no longer hand `batch_remove` duplicates and the purge test stopped noticing this
    # revert. The removal report still does - every uid's collection name becomes `all_ids`.
    Revert(
        "linking: the census walks bpy.data.all_ids, counting everything twice",
        ADDON_LINKING,
        '        aggregate = getattr(getattr(prop, "fixed_type", None), "identifier", None) == "ID"\n',
        "        aggregate = False\n",
        (f"{LKT}::test_unlink_reports_exactly_what_it_removed",),
    ),
    Revert(
        "linking: an indirect library is unlinked",
        ADDON_LINKING,
        "    if indirect:\n",
        "    if False:\n",
        (f"{LKT}::test_unlink_refuses_an_indirect_library",),
    ),
    Revert(
        "linking: orphans are purged without purge_orphans",
        ADDON_LINKING,
        "        purged = _purge_newly_orphaned(before, known) if purge_orphans else []\n",
        "        purged = _purge_newly_orphaned(before, known)\n",
        (f"{LKT}::test_unlink_purges_only_when_asked_and_only_what_it_orphaned",),
    ),
    Revert(
        "linking: the purge also takes datablocks that were orphans before the unlink",
        ADDON_LINKING,
        'if users == 0 and before.get(uid, ("", 0))[1] > 0]',
        "if users == 0]",
        (
            f"{LKT}::test_unlink_purges_only_when_asked_and_only_what_it_orphaned",
            # The same clause at `compute_orphaned`, the pure rule the purge is built on:
            # without it every zero-user datablock is this unlink's orphan, which is the
            # mistake `orphans_purge` makes.
            f"{LKT}::test_only_a_datablock_this_unlink_emptied_counts_as_orphaned",
        ),
    ),
    Revert(
        "linking: a library is removed through a reference an earlier removal may have freed",
        ADDON_LINKING,
        "        current = next((library for library in bpy.data.libraries if library.session_uid == uid), None)\n",
        "        current = next((library for library in libraries if library.session_uid == uid), None)\n",
        (f"{LKT}::test_unlink_never_removes_a_datablock_an_earlier_removal_freed",),
    ),
    Revert(
        "linking: a libraries.remove failure goes out raw",
        ADDON_LINKING,
        '            message = operator_failure_message("unlink_libraries", exc, known_paths)\n',
        "            message = str(exc)\n",
        (f"{LKT}::test_an_unlink_failure_reaches_the_client_sanitized",),
    ),
    Revert(
        "linking: a part-way unlink failure reports what it removed as prose, not as data",
        ADDON_LINKING,
        "            raise PartialUnlinkError(message, removed, already_removed) from exc\n",
        '            raise RuntimeError(f"{message} (libraries already removed: {removed})") from exc\n',
        (f"{LKT}::test_a_part_way_unlink_failure_carries_what_it_already_removed",),
    ),
    # `linking: the name helper returns the first of several matches` stood here. The helper
    # it guarded, `resolve_unique_name`, is gone: no command took a datablock name as a
    # handle, so nothing called it and its test went with it.
    Revert(
        "linking: create_override grows a name handle",
        ADDON_LINKING,
        "    def create_override(\n"
        "        collection_uid: object, *, scene_uid: object = None, detail: object = False\n"
        "    ) -> dict[str, object]:\n",
        "    def create_override(\n"
        "        collection_uid: object, *, scene_uid: object = None, detail: object = False,\n"
        "        collection_name: object = None,\n"
        "    ) -> dict[str, object]:\n",
        (f"{LKT}::test_no_linking_command_takes_a_datablock_name_as_a_handle",),
    ),
    Revert(
        "linking: unlink_libraries is not registered",
        ADDON_SERVER_CORE,
        '        "unlink_libraries": CommandSpec(datablock_replacing=True),\n',
        "",
        (
            f"{LKT}::test_the_linking_commands_are_dispatchable_and_advertised",
            f"{LKT}::test_the_three_replacing_commands_never_enter_a_transaction_and_the_link_does",
        ),
    ),
    # --- script auto-execution refusals (a .blend's scripts must never run) ---
    Revert(
        "linking: link_canon_library does not check use_scripts_auto_execute",
        ADDON_LINKING,
        '        refuse_scripts_auto_execute("link_canon_library")\n',
        "",
        (
            f"{LKT}::test_link_refuses_while_scripts_auto_execute_is_on[on]",
            f"{LKT}::test_link_refuses_while_scripts_auto_execute_is_on[unreadable]",
        ),
    ),
    Revert(
        "linking: reload_library does not check use_scripts_auto_execute",
        ADDON_LINKING,
        '        refuse_scripts_auto_execute("reload_library")\n',
        "",
        (
            f"{LKT}::test_reload_and_relocate_refuse_while_scripts_auto_execute_is_on[on]",
            f"{LKT}::test_reload_and_relocate_refuse_while_scripts_auto_execute_is_on[unreadable]",
        ),
    ),
    Revert(
        "linking: relocate_library does not check use_scripts_auto_execute",
        ADDON_LINKING,
        '        refuse_scripts_auto_execute("relocate_library")\n',
        "",
        (
            f"{LKT}::test_reload_and_relocate_refuse_while_scripts_auto_execute_is_on[on]",
            f"{LKT}::test_reload_and_relocate_refuse_while_scripts_auto_execute_is_on[unreadable]",
        ),
    ),
    Revert(
        "linking: the scripts check refuses even with the preference off",
        ADDON_BLEND_FILES,
        '    if getattr(filepaths, "use_scripts_auto_execute", True) is not False:\n',
        "    if True:\n",
        (
            f"{LKT}::test_link_proceeds_while_scripts_auto_execute_is_off",
            f"{LKT}::test_reload_and_relocate_proceed_while_scripts_auto_execute_is_off",
            f"{LKT}::test_create_override_proceeds_while_scripts_auto_execute_is_off",
            f"{PHT}::test_a_downloaded_blend_is_loaded_while_scripts_auto_execute_is_off",
        ),
    ),
    Revert(
        "linking: the scripts refusal always names open_shot",
        ADDON_BLEND_FILES,
        '            f"{command} refuses to load while',
        '            f"open_shot refuses to load while',
        (
            f"{LKT}::test_link_refuses_while_scripts_auto_execute_is_on[on]",
            f"{PHT}::test_a_downloaded_blend_is_never_loaded_while_scripts_auto_execute_is_on[on]",
        ),
    ),
    Revert(
        "polyhaven: the Poly Haven .blend import does not check use_scripts_auto_execute",
        ADDON_POLYHAVEN,
        '    refuse_scripts_auto_execute("import_polyhaven_asset")\n',
        "",
        (
            f"{PHT}::test_a_downloaded_blend_is_never_loaded_while_scripts_auto_execute_is_on[on]",
            f"{PHT}::test_a_downloaded_blend_is_never_loaded_while_scripts_auto_execute_is_on[unreadable]",
        ),
    ),
    # --- override re-linking, reload placeholders, relocation, quoted library names ---
    Revert(
        "linking: overrides are validated one at a time and nothing re-links a replaced instance",
        ADDON_LINKING,
        "    for collection in collections:\n"
        "        _refuse_unoverridable(collection)\n"
        "    unlinked: list[tuple[object, object]] = []\n"
        "    reports = []\n"
        "    try:\n"
        "        for collection in collections:\n"
        "            # Again, just before its own override: overriding a parent overrides every collection\n"
        "            # inside it, so a nested request would otherwise build a second copy (measured, section K).\n"
        "            _refuse_unoverridable(collection)\n"
        "            reports.append(_override_hierarchy(collection, scene, unlinked, detail=detail))\n"
        "    except Exception:\n"
        "        for parent, child in reversed(unlinked):\n"
        "            if not _has_child(parent, child):\n"
        "                parent.children.link(child)  # type: ignore[attr-defined]\n"
        "        raise\n"
        "    return reports\n",
        "    unlinked: list[tuple[object, object]] = []\n"
        "    reports = []\n"
        "    for collection in collections:\n"
        "        _refuse_unoverridable(collection)\n"
        "        reports.append(_override_hierarchy(collection, scene, unlinked, detail=detail))\n"
        "    return reports\n",
        (
            f"{LKT}::test_a_refused_multi_collection_override_keeps_the_existing_placement",
            f"{LKT}::test_a_later_override_failure_restores_the_instances_earlier_overrides_replaced",
        ),
    ),
    Revert(
        "linking: a later override failure does not re-link the instances earlier ones replaced",
        ADDON_LINKING,
        "            if not _has_child(parent, child):\n"
        "                parent.children.link(child)  # type: ignore[attr-defined]\n",
        "            pass\n",
        (f"{LKT}::test_a_later_override_failure_restores_the_instances_earlier_overrides_replaced",),
    ),
    Revert(
        "linking: a datablock the file no longer holds is listed as present",
        ADDON_LINKING,
        '        "is_missing": bool(getattr(datablock, "is_missing", False)),\n',
        '        "is_missing": False,\n',
        (
            f"{LKT}::test_datablocks_the_file_no_longer_holds_are_reported_missing_with_a_warning[reload_library]",
            f"{LKT}::test_datablocks_the_file_no_longer_holds_are_reported_missing_with_a_warning[relocate_library]",
        ),
    ),
    Revert(
        "linking: no warning when a reload leaves placeholders",
        ADDON_LINKING,
        "    if not missing:\n        return {}\n",
        "    return {}\n",
        (
            f"{LKT}::test_datablocks_the_file_no_longer_holds_are_reported_missing_with_a_warning[reload_library]",
            f"{LKT}::test_datablocks_the_file_no_longer_holds_are_reported_missing_with_a_warning[relocate_library]",
        ),
    ),
    Revert(
        "linking: the missing-datablock warning is sent when nothing is missing",
        ADDON_LINKING,
        "    if not missing:\n        return {}\n",
        "",
        (f"{LKT}::test_a_reload_that_finds_everything_carries_no_warning",),
    ),
    Revert(
        "linking: an indirect library can be relocated",
        ADDON_LINKING,
        (
            "        if is_indirect_library(library):\n"
            "            raise ValueError(\n"
            '                "that library is indirect'
        ),
        ('        if False:\n            raise ValueError(\n                "that library is indirect'),
        (f"{LKT}::test_relocate_refuses_an_indirect_library",),
    ),
    Revert(
        "linking: create_override does not check use_scripts_auto_execute",
        ADDON_LINKING,
        '        refuse_scripts_auto_execute("create_override")\n',
        "",
        (
            f"{LKT}::test_create_override_refuses_while_scripts_auto_execute_is_on[on]",
            f"{LKT}::test_create_override_refuses_while_scripts_auto_execute_is_on[unreadable]",
        ),
    ),
    Revert(
        "file paths: a quoted library name is not reduced before path detection",
        ADDON_FILE_PATHS,
        "    text = _QUOTED_LIBRARY_NAME.sub(_leaf_library_name, text)\n",
        "",
        (
            f"{FPT}::test_sanitizer_reduces_a_library_name_holding_an_absolute_path_to_its_leaf",
            *(
                f"{FPT}::test_sanitizer_reduces_every_quoted_library_name_shape_to_its_leaf[{case}]"
                for case in ("relocate-indirect", "delete-indirect", "from-library")
            ),
        ),
    ),
    Revert(
        "file paths: a quoted library name is kept whole instead of reduced to its leaf",
        ADDON_FILE_PATHS,
        "{client_safe_name_leaf(match['name'])}",
        "{match['name']}",
        (
            f"{FPT}::test_sanitizer_reduces_a_library_name_holding_an_absolute_path_to_its_leaf",
            f"{FPT}::test_sanitizer_reduces_a_library_name_without_touching_the_filesystem",
            *(
                f"{FPT}::test_sanitizer_reduces_every_quoted_library_name_shape_to_its_leaf[{case}]"
                for case in ("relocate-indirect", "delete-indirect", "from-library")
            ),
        ),
    ),
    # --- nested override requests, and reducing a library name ---
    Revert(
        "linking: a nested request is not re-checked before each override, so a child is overridden twice",
        ADDON_LINKING,
        "            _refuse_unoverridable(collection)\n            reports.append(",
        "            reports.append(",
        (f"{LKT}::test_a_nested_request_is_refused_before_it_overrides_a_collection_twice",),
    ),
    Revert(
        "file paths: the quoted-name match stops at a newline",
        ADDON_FILE_PATHS,
        '>]))", re.DOTALL\n',
        '>]))"\n',
        (f"{FPT}::test_sanitizer_reduces_a_library_name_containing_a_newline",),
    ),
    # `file paths: a library name is reduced with the isdir-checking leaf rule` stood here.
    # `client_safe_leaf` stats nothing any more - the caller that holds the path passes
    # `is_directory` in - so swapping the leaf rules no longer probes the filesystem and the
    # row could not make its node fail. The node moved to the row above, which still can.
    Revert(
        "linking: an absolute library name is not a known path",
        ADDON_LINKING,
        "    return _absolute((raw, expanded, canonical_path(expanded), name))\n",
        "    return _absolute((raw, expanded, canonical_path(expanded)))\n",
        (f"{LKT}::test_an_absolute_library_name_is_a_known_path_so_no_relative_tail_survives",),
    ),
    # --- nested collection overrides, and nothing stats Library.name ---
    Revert(
        "linking: a collection whose inner collection is already overridden is overridden again",
        ADDON_LINKING,
        "    if inner_overrides:\n",
        "    if False:\n",
        tuple(
            f"{LKT}::test_overriding_a_parent_whose_inner_collection_is_already_overridden_is_refused[{route}]"
            for route in ("create_override", "link_canon_library")
        ),
    ),
    Revert(
        "linking: a request naming a collection and one inside it is not refused up front",
        ADDON_LINKING,
        "    _refuse_nested_requests(collections)\n",
        "",
        (f"{LKT}::test_a_request_naming_a_collection_and_one_inside_it_is_refused_up_front",),
    ),
    Revert(
        "linking: the library summary stats Library.name",
        ADDON_BLEND_FILES,
        '        "name": client_safe_name_leaf(getattr(library, "name", "")),',
        '        "name": _reverted_statting_name(getattr(library, "name", "")),',
        (f"{LKT}::test_library_names_are_reduced_without_touching_the_filesystem",),
        also=REVERTED_STATTING_NAME,
    ),
    Revert(
        "linking: relocate stats Library.name for name_before",
        ADDON_LINKING,
        "        name_before = client_safe_name_leaf(library.name)",
        "        name_before = _reverted_statting_name(library.name)",
        (f"{LKT}::test_library_names_are_reduced_without_touching_the_filesystem",),
        also=REVERTED_STATTING_NAME,
    ),
    Revert(
        "linking: relocate stats Library.name for name_after",
        ADDON_LINKING,
        '            "name_after": client_safe_name_leaf(library.name),',
        '            "name_after": _reverted_statting_name(library.name),',
        (f"{LKT}::test_library_names_are_reduced_without_touching_the_filesystem",),
        also=REVERTED_STATTING_NAME,
    ),
    Revert(
        "linking: a refusal's candidate list stats Library.name",
        # `candidates.display_name`, imported by linking as `_display_name`.
        ADDON_CANDIDATES,
        "        return client_safe_name_leaf(name)\n",
        "        return _reverted_statting_name(name)\n",
        (f"{LKT}::test_library_names_are_reduced_without_touching_the_filesystem",),
        also=REVERTED_STATTING_NAME,
    ),
    # --- the ten server-side file-lifecycle/linking tools ---
    Revert(
        "server tools: get_session_info is not registered as an MCP tool",
        SERVER_FILE_LIFECYCLE_TOOL,
        "@mcp.tool()\nasync def get_session_info(ctx: Context) -> dict:",
        "async def get_session_info(ctx: Context) -> dict:",
        (f"{SFLT}::test_file_lifecycle_tools_are_registered_and_dispatched",),
    ),
    Revert(
        "server tools: get_session_info sends an extra param the addon command takes none of",
        SERVER_FILE_LIFECYCLE_TOOL,
        '    return await call_blender("get_session_info", {})',
        '    return await call_blender("get_session_info", {"extra": True})',
        (f"{SFLT}::test_get_session_info_forwards_no_params",),
    ),
    Revert(
        "server tools: open_shot's discard_unsaved default is unpinned to True",
        SERVER_FILE_LIFECYCLE_TOOL,
        "    discard_unsaved: bool = False,\n",
        "    discard_unsaved: bool = True,\n",
        (f"{SFLT}::test_open_shot_defaults",),
    ),
    Revert(
        "server tools: open_shot does not forward discard_unsaved",
        SERVER_FILE_LIFECYCLE_TOOL,
        '{"filepath": filepath, "load_ui": load_ui, "discard_unsaved": discard_unsaved}',
        '{"filepath": filepath, "load_ui": load_ui, "discard_unsaved": False}',
        (f"{SFLT}::test_open_shot_forwards_every_parameter",),
    ),
    Revert(
        "server tools: save_shot does not forward compress",
        SERVER_FILE_LIFECYCLE_TOOL,
        '"compress": compress,',
        '"compress": False,',
        (f"{SFLT}::test_save_shot_forwards_every_parameter",),
    ),
    Revert(
        "server tools: save_shot's filepath=None default is unpinned",
        SERVER_FILE_LIFECYCLE_TOOL,
        "    filepath: str | None = None,\n    compress: bool = False,",
        '    filepath: str | None = "",\n    compress: bool = False,',
        (f"{SFLT}::test_save_shot_default_filepath_is_none",),
    ),
    Revert(
        "server tools: save_shot removed from _DESTRUCTIVE_TOOLS, so the hint depends on the schema alone",
        SERVER_DOCUMENTATION,
        '    "reload_library",\n    "save_shot",\n}',
        '    "reload_library",\n}',
        (f"{SFLT}::test_save_shot_destructive_hint_is_explicit_not_schema_derived",),
    ),
    Revert(
        "server tools: reset_session's confirm default is unpinned to True",
        SERVER_FILE_LIFECYCLE_TOOL,
        "async def reset_session(ctx: Context, confirm: bool = False) -> dict:",
        "async def reset_session(ctx: Context, confirm: bool = True) -> dict:",
        (f"{SFLT}::test_reset_session_defaults",),
    ),
    Revert(
        "server tools: reset_session does not forward confirm",
        SERVER_FILE_LIFECYCLE_TOOL,
        '    return await call_blender("reset_session", {"confirm": confirm})',
        '    return await call_blender("reset_session", {"confirm": False})',
        (f"{SFLT}::test_reset_session_forwards_confirm",),
    ),
    Revert(
        "server tools: link_canon_library does not forward as_override",
        SERVER_FILE_LIFECYCLE_TOOL,
        '"as_override": as_override,\n            "relative": relative,',
        '"as_override": False,\n            "relative": relative,',
        (f"{SFLT}::test_link_canon_library_forwards_every_parameter",),
    ),
    Revert(
        "server tools: link_canon_library's relative=False default is unpinned to True",
        SERVER_FILE_LIFECYCLE_TOOL,
        "    relative: bool = False,\n    scene_uid: int | None = None,\n) -> dict:",
        "    relative: bool = True,\n    scene_uid: int | None = None,\n) -> dict:",
        (f"{SFLT}::test_link_canon_library_defaults",),
    ),
    Revert(
        "server tools: create_override drops scene_uid before forwarding it",
        SERVER_FILE_LIFECYCLE_TOOL,
        '        {"collection_uid": collection_uid, "scene_uid": scene_uid, "detail": detail},',
        '        {"collection_uid": collection_uid, "scene_uid": None, "detail": detail},',
        (f"{SFLT}::test_create_override_forwards_every_parameter",),
    ),
    Revert(
        "server tools: create_override's scene_uid=None default is unpinned",
        SERVER_FILE_LIFECYCLE_TOOL,
        "    ctx: Context, collection_uid: int, scene_uid: int | None = None, detail: bool = False\n",
        "    ctx: Context, collection_uid: int, scene_uid: int | None = 999, detail: bool = False\n",
        (f"{SFLT}::test_create_override_defaults",),
    ),
    Revert(
        "server tools: list_libraries forces offset to 0 before forwarding it",
        SERVER_FILE_LIFECYCLE_TOOL,
        '    return await call_blender("list_libraries", {"limit": limit, "offset": offset, "detail": detail})',
        '    return await call_blender("list_libraries", {"limit": limit, "offset": 0, "detail": detail})',
        (f"{SFLT}::test_list_libraries_forwards_pagination",),
    ),
    Revert(
        "server tools: list_libraries' limit=25 default is unpinned",
        SERVER_FILE_LIFECYCLE_TOOL,
        "async def list_libraries(ctx: Context, limit: int = 25, offset: int = 0, detail: bool = False) -> dict:",
        "async def list_libraries(ctx: Context, limit: int = 10, offset: int = 0, detail: bool = False) -> dict:",
        (f"{SFLT}::test_list_libraries_defaults",),
    ),
    Revert(
        "server tools: reload_library does not forward library_uid",
        SERVER_FILE_LIFECYCLE_TOOL,
        '    return await call_blender("reload_library", {"library_uid": library_uid, "detail": detail})',
        '    return await call_blender("reload_library", {"library_uid": 0, "detail": detail})',
        (f"{SFLT}::test_reload_library_forwards_uid",),
    ),
    Revert(
        "server tools: relocate_library does not forward filepath",
        SERVER_FILE_LIFECYCLE_TOOL,
        '        {"library_uid": library_uid, "filepath": filepath, "detail": detail},',
        '        {"library_uid": library_uid, "filepath": None, "detail": detail},',
        (f"{SFLT}::test_relocate_library_forwards_uid_and_filepath",),
    ),
    Revert(
        "server tools: reload_library removed from _DESTRUCTIVE_TOOLS",
        SERVER_DOCUMENTATION,
        '    "relocate_library",\n    "reload_library",\n    "save_shot",\n}',
        '    "relocate_library",\n    "save_shot",\n}',
        (f"{BUNT}::test_file_lifecycle_tools_advertise_correct_hints",),
    ),
    Revert(
        "server tools: open_shot removed from _BLEND_FILE_TOOLS",
        SERVER_DOCUMENTATION,
        '_BLEND_FILE_TOOLS = {\n    "open_shot",\n    "save_shot",',
        '_BLEND_FILE_TOOLS = {\n    "save_shot",',
        (f"{BUNT}::test_file_lifecycle_tools_advertise_correct_hints",),
    ),
    Revert(
        "server tools: openWorldHint drops the _BLEND_FILE_TOOLS clause",
        SERVER_DOCUMENTATION,
        "openWorldHint=(tool.name in _EXTERNAL_TOOLS or tool.name in _FILE_TOOLS or tool.name in _BLEND_FILE_TOOLS),",
        "openWorldHint=(tool.name in _EXTERNAL_TOOLS or tool.name in _FILE_TOOLS),",
        (f"{BUNT}::test_file_lifecycle_tools_advertise_correct_hints",),
    ),
    Revert(
        "server tools: unlink_libraries does not forward purge_orphans",
        SERVER_FILE_LIFECYCLE_TOOL,
        '{"library_uids": library_uids, "confirm": confirm, "purge_orphans": purge_orphans},',
        '{"library_uids": library_uids, "confirm": confirm, "purge_orphans": False},',
        (f"{SFLT}::test_unlink_libraries_forwards_every_parameter",),
    ),
    Revert(
        "server tools: unlink_libraries' confirm=False default is unpinned to True",
        SERVER_FILE_LIFECYCLE_TOOL,
        "    confirm: bool = False,\n    purge_orphans: bool = False,\n) -> dict:",
        "    confirm: bool = True,\n    purge_orphans: bool = False,\n) -> dict:",
        (f"{SFLT}::test_unlink_libraries_defaults",),
    ),
    Revert(
        "server tools: the shared dispatch swallows an addon failure instead of propagating it",
        SERVER_DISPATCH,
        (
            "    except BlenderOperationError as exc:\n"
            '        logger.error("Blender refused %s: %s", command, exc)\n'
            "        raise ToolError(str(exc)) from exc\n"
        ),
        (
            "    except BlenderOperationError as exc:\n"
            '        logger.error("Blender refused %s: %s", command, exc)\n'
            "        return {}\n"
        ),
        tuple(
            f"{SFLT}::test_addon_failure_reaches_the_client_as_a_tool_error_unchanged[{name}]"
            for name in sorted(
                (
                    "get_session_info",
                    "open_shot",
                    "save_shot",
                    "reset_session",
                    "link_canon_library",
                    "create_override",
                    "list_libraries",
                    "reload_library",
                    "relocate_library",
                    "unlink_libraries",
                    "inspect_delivery",
                )
            )
        ),
    ),
    # The rest of `tests/server/tools/test_dispatch.py`. The row above already guards one half of
    # the taxonomy through the file-lifecycle tools; these guard it from the dispatch's own side,
    # plus the two properties nothing else in the suite can see: that the socket call leaves the
    # event loop, and that the one function that sends is also the one that wraps.
    Revert(
        "server tools: the shared dispatch runs the socket call on the event loop again",
        SERVER_DISPATCH,
        "    return await asyncio.to_thread(send_command, command, params)",
        "    return send_command(command, params)",
        (f"{DISPT}::test_the_blocking_socket_call_never_runs_on_the_event_loop",),
    ),
    Revert(
        "server tools: the shared dispatch hands back the raw reply instead of the envelope",
        SERVER_DISPATCH,
        (
            "    reply = await send_blender_command(command, params)\n"
            "    return envelope_for(\n"
            "        reply,\n"
            "        changed_objects=changed_objects or (),\n"
            "        changed_resources=changed_resources or (),\n"
            "        warnings=warnings or (),\n"
            "    )\n"
        ),
        "    return await send_blender_command(command, params)\n",
        (f"{DISPT}::test_the_reply_comes_back_as_the_shared_envelope",),
    ),
    Revert(
        "server tools: an operation failure is prefixed with the command again",
        SERVER_DISPATCH,
        "        raise ToolError(str(exc)) from exc\n",
        '        raise ToolError(f"Error running {command}: {exc}") from exc\n',
        (f"{DISPT}::test_an_operation_failure_reaches_the_client_as_blenders_own_message",),
    ),
    Revert(
        "server tools: a transport failure loses the reconnect advice",
        SERVER_DISPATCH,
        '        raise ToolError(f"{exc} {_RETRY_HINT}") from exc\n',
        "        raise ToolError(str(exc)) from exc\n",
        (f"{DISPT}::test_a_transport_failure_says_the_connection_was_dropped_and_is_worth_a_retry",),
    ),
    Revert(
        "server tools: the shared dispatch relabels every other failure as its own again",
        SERVER_DISPATCH,
        (
            "    except BlenderTransportError as exc:\n"
            '        logger.error("Transport failure running %s: %s", command, exc)\n'
            '        raise ToolError(f"{exc} {_RETRY_HINT}") from exc\n'
        ),
        (
            "    except BlenderTransportError as exc:\n"
            '        logger.error("Transport failure running %s: %s", command, exc)\n'
            '        raise ToolError(f"{exc} {_RETRY_HINT}") from exc\n'
            "    except Exception as exc:\n"
            '        raise ToolError(f"Error running {command}: {exc}") from exc\n'
        ),
        (f"{DISPT}::test_a_failure_the_taxonomy_does_not_claim_is_left_alone",),
    ),
    Revert(
        "server tools: file_lifecycle removed from CORE_MODULES",
        SERVER_BUNDLES,
        '    "animation",\n    "file_lifecycle",\n)',
        '    "animation",\n)',
        (f"{BUNT}::test_file_lifecycle_tools_are_exactly_eleven_and_reachable_from_shot_and_asset",),
    ),
    # Each ceiling constant is its mode's measured payload, so the only revert that can still
    # falsify these two tests is one byte below it. The rows this replaced named historical
    # ceilings (203_094, 217_718, 78_019) that the trimmed catalog now sits under, which made them
    # SURVIVORs for an arithmetic reason rather than a behavioural one.
    Revert(
        "server tools: the shot ceiling reverted one byte below the measured payload",
        TEST_BUNDLES_FILE,
        "SHOT_MODE_BYTE_CEILING = 234_000",
        # One byte below the *measured* payload (233,211), not below the ceiling: the ceiling has
        # headroom by design, so reverting it to 233_999 would still pass and prove nothing.
        "SHOT_MODE_BYTE_CEILING = 233_210",
        (f"{BUNT}::test_shot_mode_payload_stays_under_its_ceiling",),
    ),
    Revert(
        "server tools: the default ceiling reverted one byte below the measured payload",
        TEST_BUNDLES_FILE,
        "DEFAULT_MODE_BYTE_CEILING = 80_500",
        # Same rule: one byte below the measured core payload (79,834), not below the ceiling.
        "DEFAULT_MODE_BYTE_CEILING = 79_833",
        (f"{BUNT}::test_default_mode_payload_stays_under_its_ceiling",),
    ),
    Revert(
        "server tools: the constraint keywords are appended to each parameter description again",
        SERVER_DOCUMENTATION,
        '        if description:\n            property_schema["description"] = description.rstrip()',
        '        if "default" in property_schema:\n'
        "            description = f\"{description or ''} Default: {property_schema['default']!r}.\".strip()\n"
        "        if description:\n"
        '            property_schema["description"] = description.rstrip()',
        (
            f"{TDT}::test_schema_constraints_are_not_restated_as_prose",
            f"{BUNT}::test_advertised_parameter_descriptions_do_not_restate_the_schema[None]",
            f"{BUNT}::test_advertised_parameter_descriptions_do_not_restate_the_schema[shot]",
        ),
    ),
    Revert(
        "server tools: a parameter the name and schema already describe gets a generated sentence again",
        SERVER_DOCUMENTATION,
        "    if name in _PARAMETER_DESCRIPTIONS:\n"
        "        return _PARAMETER_DESCRIPTIONS[name]\n"
        "    match _primary_type(schema):",
        "    if name in _PARAMETER_DESCRIPTIONS:\n"
        "        return _PARAMETER_DESCRIPTIONS[name]\n"
        '    return f"Explicit {name} input for this operation."\n'
        "    match _primary_type(schema):",
        (f"{TDT}::test_a_parameter_the_schema_already_describes_carries_no_description",),
    ),
    Revert(
        "server tools: every generated parameter semantic - unit, existing datablock, refusal - is dropped",
        SERVER_DOCUMENTATION,
        TYPE_DISPATCHED_DESCRIPTIONS,
        DROPPED_DESCRIPTION_SEMANTICS,
        (f"{TDT}::test_units_and_datablock_semantics_survive",),
    ),
    Revert(
        "server tools: a name token decides the unit before the type does, so an enum is scene units again",
        SERVER_DOCUMENTATION,
        'def _string_description(name: str) -> str | None:\n    if name == "resolution":',
        "def _string_description(name: str) -> str | None:\n"
        "    if any(token in name for token in _DISTANCE_TOKENS):\n"
        '        return "In Blender scene units."\n'
        '    if name == "resolution":',
        (f"{TDT}::test_a_non_numeric_parameter_is_never_labelled_with_scene_units",),
    ),
    Revert(
        "server tools: a nested model's own prose is spliced into each of its parameter descriptions again",
        SERVER_DOCUMENTATION,
        '    for definition in schema.get("$defs", {}).values():\n'
        "        if isinstance(definition, dict):\n"
        "            _describe_schema(definition)",
        SPLICED_MODEL_CONTEXT,
        (
            f"{TDT}::test_a_nested_model_title_is_not_spliced_into_its_parameters",
            f"{BUNT}::test_advertised_parameter_descriptions_contain_no_letter_split_words[None]",
            f"{BUNT}::test_advertised_parameter_descriptions_contain_no_letter_split_words[shot]",
        ),
    ),
    Revert(
        "animation: the addon's expression allowlist drops ast.Load, refusing every named variable",
        ADDON_ANIMATION,
        "    ast.Load,\n",
        "",
        (f"{ANIMT}::test_a_driver_expression_may_name_frame_and_its_declared_variables",),
    ),
    Revert(
        "animation: the server's expression allowlist drops ast.Load, refusing every named variable",
        SERVER_ANIMATION_TOOL,
        "    ast.Load,\n",
        "",
        (f"{ANIMT}::test_a_scripted_driver_reaches_blender_with_its_frame_expression",),
    ),
    Revert(
        "animation: the addon's expression allowlist admits every AST node, so calls pass again",
        ADDON_ANIMATION,
        "_SAFE_EXPRESSION_NODES = (\n    ast.Expression,",
        "_SAFE_EXPRESSION_NODES = (\n    ast.AST,\n    ast.Expression,",
        (f"{ANIMT}::test_a_driver_expression_still_refuses_undeclared_names_and_calls",),
    ),
    Revert(
        "animation: a cycle prefix that matches no curve is a silent success again",
        ADDON_ANIMATION,
        "        if not selected:\n",
        "        if False:\n",
        (f"{ANIMT}::test_a_cycle_prefix_that_names_no_curve_is_refused",),
    ),
    Revert(
        "server tools: the five open-world tools are folded into _FILE_TOOLS instead of _BLEND_FILE_TOOLS",
        SERVER_DOCUMENTATION,
        '_FILE_TOOLS = {\n    "bake_retopology_maps",',
        "_FILE_TOOLS = {\n"
        '    "open_shot",\n'
        '    "save_shot",\n'
        '    "link_canon_library",\n'
        '    "reload_library",\n'
        '    "relocate_library",\n'
        '    "bake_retopology_maps",',
        (f"{BUNT}::test_file_lifecycle_tools_blend_file_prose_is_correct",),
    ),
    Revert(
        "server tools: the _BLEND_FILE_TOOLS effects-prose branch is deleted",
        SERVER_DOCUMENTATION,
        "    elif name in _BLEND_FILE_TOOLS:\n"
        '        effects = "Side effects: reads or writes a .blend file on disk."\n'
        "    elif name in _EXTERNAL_TOOLS:",
        "    elif name in _EXTERNAL_TOOLS:",
        (f"{BUNT}::test_file_lifecycle_tools_blend_file_prose_is_correct",),
    ),
    # --- every reply is bounded by the per-reply byte budget ---
    Revert(
        "reply budget: ok() stops bounding the reply it builds",
        SERVER_ENVELOPE,
        "    _fit_budget(reply)\n",
        "",
        (
            f"{ENVT}::test_an_oversized_record_page_is_cut_to_the_budget_and_stays_resumable",
            f"{ENVT}::test_a_resumed_page_continues_from_the_offset_it_was_given",
            f"{ENVT}::test_the_largest_list_is_the_one_cut",
            f"{ENVT}::test_a_page_nested_inside_a_single_record_is_found",
            f"{ENVT}::test_an_unpaginated_oversized_list_is_cut_and_says_to_narrow_the_scope",
            f"{ENVT}::test_a_single_record_too_big_for_the_budget_is_still_reported",
            f"{ENVT}::test_the_budget_leaves_a_flat_oversized_reply_alone",
        ),
    ),
    Revert(
        "reply budget: the shortening walk stops at a one-entry list instead of descending into it",
        SERVER_ENVELOPE,
        "                if len(value) > 1:\n"
        "                    pages.append((current, key))\n"
        "                pending.extend(value)",
        "                if len(value) > 1:\n"
        "                    pages.append((current, key))\n"
        "                    pending.extend(value)",
        (f"{ENVT}::test_a_page_nested_inside_a_single_record_is_found",),
    ),
    Revert(
        "reply budget: the pagination keys are written after the page was measured, not before",
        SERVER_ENVELOPE,
        "    widest = _pagination_updates(owner, key, len(records))\n",
        "    widest = {}\n",
        (f"{ENVT}::test_the_keys_the_shortening_adds_are_inside_the_budget_it_measured",),
    ),
    Revert(
        "reply budget: a shortened page's warning carries another page's numbers",
        SERVER_ENVELOPE,
        "else _NO_RESUME) for cut in cuts\n",
        "else _NO_RESUME) for cut in reversed(cuts)\n",
        (f"{ENVT}::test_each_shortened_pages_warning_names_that_page_and_no_other",),
    ),
    # --- save_shot.create_directories ---
    Revert(
        "create_directories: save_shot ignores create_directories and never makes the directory",
        ADDON_FILE_LIFECYCLE,
        "        created_directory = create_save_directory(request.canonical)\n",
        "        created_directory = False\n",
        (f"{FLT}::test_save_shot_refuses_a_missing_directory_unless_asked_to_create_it",),
    ),
    Revert(
        "create_directories: save_shot does not validate create_directories as a bool",
        ADDON_FILE_LIFECYCLE,
        '        create_directories = require_bool("create_directories", create_directories)\n',
        "",
        (
            f"{FLT}::test_save_shot_creates_no_directory_outside_the_roots_or_on_a_refusal",
            f"{FLT}::test_a_flag_that_is_not_a_real_bool_is_refused[save_shot-create_directories]",
        ),
    ),
    Revert(
        "create_directories: resolving a save target creates its directory before any refusal has run",
        ADDON_FILE_PATHS,
        ("    directory = os.path.dirname(path)\n    directory_exists = os.path.isdir(directory)\n"),
        (
            "    directory = os.path.dirname(path)\n"
            "    if create_directories:\n"
            "        os.makedirs(directory, exist_ok=True)\n"
            "    directory_exists = os.path.isdir(directory)\n"
        ),
        (f"{FPT}::test_a_missing_save_directory_is_accepted_and_created_only_on_opt_in",),
    ),
    Revert(
        "create_directories: create_save_directory reports a directory that already existed as created",
        ADDON_FILE_PATHS,
        "    if os.path.isdir(directory):\n        return False\n",
        "    if os.path.isdir(directory):\n        return True\n",
        (
            f"{FPT}::test_a_missing_save_directory_is_accepted_and_created_only_on_opt_in",
            f"{FLT}::test_save_shot_reports_no_created_directory_when_it_already_existed",
        ),
    ),
    Revert(
        "create_directories: a directory-creation OSError's text (and its path) reaches the refusal",
        ADDON_FILE_PATHS,
        '        raise ValueError("target directory could not be created") from exc',
        '        raise ValueError(f"target directory could not be created: {exc}") from exc',
        (f"{FPT}::test_a_save_directory_blocked_by_a_file_is_refused_without_naming_it",),
    ),
    Revert(
        "create_directories: the save_shot tool drops create_directories",
        SERVER_FILE_LIFECYCLE_TOOL,
        '            "create_directories": create_directories,\n',
        "",
        (
            f"{SFLT}::test_save_shot_forwards_every_parameter",
            f"{SFLT}::test_save_shot_default_filepath_is_none",
        ),
    ),
    # --- one object per name after a library override ---
    Revert(
        "object lookup: find_object does not prefer the local object by its (name, None) key",
        ADDON_OBJECT_LOOKUP,
        "    local = objects.get((name, None))",
        "    local = None",
        (
            f"{OLT}::test_the_local_override_wins_over_a_linked_original_listed_first",
            f"{SOIT}::test_object_name_lookups_resolve_to_the_override_even_when_the_linked_original_is_listed_first",
        ),
    ),
    Revert(
        "object lookup: several linked objects with one name are guessed between",
        ADDON_OBJECT_LOOKUP,
        "    if len(matches) > 1:",
        "    if False:",
        (f"{OLT}::test_two_linked_objects_with_one_name_and_no_local_one_are_refused",),
    ),
    Revert(
        "candidates: the ambiguity refusal publishes library names unreduced",
        ADDON_CANDIDATES,
        '    return client_safe_name_leaf(getattr(library, "name", ""))\n',
        '    return str(getattr(library, "name", ""))\n',
        (
            f"{OLT}::test_two_linked_objects_with_one_name_and_no_local_one_are_refused",
            f"{CANDT}::test_describe_library_candidates_reduces_to_a_leaf_without_consulting_id_type",
            f"{CANDT}::test_candidates_that_reduce_to_one_name_stay_distinguishable_by_uid",
        ),
    ),
    # --- the refusal reuses linking's bounded candidate list ---
    Revert(
        "candidates: the candidate list is unbounded again",
        ADDON_CANDIDATES,
        "MAX_CANDIDATES = 10\n",
        "MAX_CANDIDATES = 10_000\n",
        (
            f"{CANDT}::test_the_list_is_bounded_and_reports_how_many_were_left_out",
            f"{OLT}::test_the_ambiguity_refusal_is_bounded_however_many_libraries_link_the_name",
        ),
    ),
    Revert(
        "candidates: entries lose their session_uid",
        ADDON_CANDIDATES,
        'f"{namer(d)!r} (session_uid {session_uid_of(d)})"',
        'f"{namer(d)!r}"',
        (
            f"{CANDT}::test_a_non_library_name_is_published_not_blanked",
            f"{CANDT}::test_describe_candidates_reduces_a_library_name_to_a_leaf_by_its_id_type",
            f"{CANDT}::test_describe_library_candidates_reduces_to_a_leaf_without_consulting_id_type",
            f"{CANDT}::test_candidates_that_reduce_to_one_name_stay_distinguishable_by_uid",
            f"{OLT}::test_two_linked_objects_with_one_name_and_no_local_one_are_refused",
        ),
    ),
    Revert(
        "candidates: the refusal drops its true total",
        ADDON_OBJECT_LOOKUP,
        'f"({len(matches)} of them: {describe_library_candidates(libraries)}) and no local object has it; "',
        'f"({describe_library_candidates(libraries)}) and no local object has it; "',
        (
            f"{OLT}::test_two_linked_objects_with_one_name_and_no_local_one_are_refused",
            f"{OLT}::test_the_ambiguity_refusal_is_bounded_however_many_libraries_link_the_name",
        ),
    ),
    Revert(
        "candidates: the library describer trusts id_type instead of forcing the leaf rule",
        ADDON_CANDIDATES,
        "    return _describe(libraries, _library_leaf)\n",
        "    return _describe(libraries, display_name)\n",
        (
            f"{CANDT}::test_describe_library_candidates_reduces_to_a_leaf_without_consulting_id_type",
            f"{OLT}::test_two_linked_objects_with_one_name_and_no_local_one_are_refused",
        ),
    ),
    Revert(
        "candidates: a name string reaches a namer that expects the datablock, blanking names",
        ADDON_CANDIDATES,
        'f"{namer(d)!r} (session_uid',
        "f\"{namer(getattr(d, 'name', ''))!r} (session_uid",
        (f"{CANDT}::test_a_non_library_name_is_published_not_blanked",),
    ),
    Revert(
        "candidates: display_name loses its library branch",
        ADDON_CANDIDATES,
        '    if getattr(datablock, "id_type", None) == "LIBRARY":\n',
        "    if False:\n",
        (f"{CANDT}::test_describe_candidates_reduces_a_library_name_to_a_leaf_by_its_id_type",),
    ),
    # --- the name-resolution rule is stated once, in the server instructions ---
    Revert(
        "server instructions: the object-name resolution rule is deleted from the instructions",
        SERVER_APP,
        "\n\nObject names after a library override: a name shared with the linked original resolves to the\n"
        "editable override. A name linked from several libraries with no local object is refused with each\n"
        "library's session_uid - override one with create_override.",
        "",
        (
            f"{SIT}::test_the_instructions_state_how_an_overridden_name_resolves",
            f"{SIT}::test_the_instructions_name_the_way_out_of_the_ambiguity_refusal",
        ),
    ),
    Revert(
        "server instructions: the parameter-naming conventions are deleted from the instructions",
        SERVER_APP,
        "\n\nTool parameter naming follows three conventions, so a name is guessable rather than something to\n"
        "fail once and learn: a tool that creates a new object of some kind names that kind parameter\n"
        "`<noun>_type` (light_type, primitive_type, domain_type), never bare `type`. A tool that creates a\n"
        "new object always takes `collection_name` explicitly - it is looked up or created, never defaulted\n"
        "from scene state. A tool that patches fields on an object that already exists (set_object_transform,\n"
        "configure_light, configure_camera, ...) takes one nested argument named `patch` (or a named group\n"
        "such as `optics`/`display`) instead of flat top-level keywords - read that argument's own schema for\n"
        "its field list before calling.",
        "",
        (f"{SIT}::test_the_instructions_state_the_three_parameter_naming_conventions",),
    ),
    Revert(
        # The three rules an agent cannot infer from any single tool's schema: one shot is one
        # action, the playhead is movable, and a held contact is IK rather than repeated FK.
        "server instructions: the animation paragraph is deleted from the instructions",
        SERVER_APP,
        "\n\nAnimation is authored one frame at a time into one named action: pass the same `action_name` to\n"
        "`keyframe_object_transform` and the pose tools, because an ID holds one action and a second one\n"
        "silently stops the first driving the rig. `set_scene_frame` is how any inspection tool or\n"
        "screenshot is pointed at another frame - they all report the current frame, so without it you\n"
        "are reviewing frame 1 forever. A contact that must hold still while the body moves over it -\n"
        "a planted foot, a hand on a prop - is held by `keyframe_bone_reach`, which re-solves the IK\n"
        "against the evaluated body pose at each frame; repeated FK rotation slides it instead.",
        "",
        (
            f"{SIT}::test_the_instructions_state_that_one_shot_is_one_action",
            f"{SIT}::test_the_instructions_name_the_tool_that_moves_the_playhead",
            f"{SIT}::test_the_instructions_point_a_held_contact_at_the_ik_tool",
        ),
    ),
    Revert(
        "server instructions: the instructions are written but never handed to FastMCP",
        SERVER_APP,
        "instructions=SERVER_INSTRUCTIONS)",
        "instructions=None)",
        (f"{SIT}::test_the_served_instructions_are_the_ones_stated_here",),
    ),
    # --- the lookup itself, and the commands that resolve a name through it ---
    Revert(
        "object lookup: a single linked object is not resolved",
        ADDON_OBJECT_LOOKUP,
        "    return matches[0] if matches else None",
        "    return None",
        (f"{OLT}::test_a_single_linked_object_is_returned_when_no_local_one_has_the_name",),
    ),
    Revert(
        "object lookup: a missing name resolves to some other object",
        ADDON_OBJECT_LOOKUP,
        "    return matches[0] if matches else None",
        "    return matches[0] if matches else next(iter(objects.values()), None)",
        (f"{OLT}::test_a_missing_name_is_none",),
    ),
    Revert(
        "object lookup: scene tools look objects up by Blender's list order",
        ADDON_SCENE,
        '    obj = find_object(bpy.data.objects, _required_name(name, "object_name"))',
        '    obj = bpy.data.objects.get(_required_name(name, "object_name"))',
        (f"{SOIT}::test_object_name_lookups_resolve_to_the_override_even_when_the_linked_original_is_listed_first",),
    ),
    Revert(
        "object lookup: get_object_info looks its object up by Blender's list order",
        ADDON_SERVER_CORE,
        # The 8-space call alone also matches, as a suffix, the deeper copy in
        # `_resolve_targets`, which comes first, and `apply()` replaces only the
        # first match. The trailing `if not obj:` makes the anchor unique.
        "        obj = find_object(bpy.data.objects, name)\n        if not obj:\n",
        "        obj = bpy.data.objects.get(name)\n        if not obj:\n",
        (f"{SOIT}::test_object_name_lookups_resolve_to_the_override_even_when_the_linked_original_is_listed_first",),
    ),
    Revert(
        "object lookup: the transaction snapshots its targets by Blender's list order",
        ADDON_SERVER_CORE,
        "                obj = find_object(bpy.data.objects, name)\n",
        "                obj = bpy.data.objects.get(name)\n",
        (f"{SOIT}::test_the_transaction_snapshots_the_same_object_the_handler_mutates_after_an_override",),
    ),
    Revert(
        "object lookup: an ambiguous target name raises out of the snapshot instead of being skipped",
        ADDON_SERVER_CORE,
        "            try:\n"
        "                obj = find_object(bpy.data.objects, name)\n"
        "            except ValueError:\n"
        "                continue\n",
        "            obj = find_object(bpy.data.objects, name)\n",
        (f"{SOIT}::test_an_ambiguous_target_name_is_skipped_rather_than_raising_out_of_the_snapshot",),
    ),
    Revert(
        "object lookup: get_object_info does not say whether it read an override",
        ADDON_SERVER_CORE,
        '            "is_override": getattr(obj, "override_library", None) is not None,',
        '            "is_override": False,',
        (f"{SOIT}::test_get_object_info_says_whether_it_resolved_an_override_or_a_linked_object",),
    ),
    Revert(
        "object lookup: get_object_info publishes a linked object's library name unreduced",
        ADDON_SERVER_CORE,
        '"library": client_safe_name_leaf(obj.library.name) if',
        '"library": obj.library.name if',
        (f"{SOIT}::test_get_object_info_says_whether_it_resolved_an_override_or_a_linked_object",),
    ),
    # --- get_addon_status summarizes the capability list instead of shipping 291 names ---
    Revert(
        "get_addon_status: the command names ship on every status call, asked for or not",
        SERVER_CORE_TOOL,
        '    if detail:\n        payload["capabilities"] = result.capabilities\n',
        '    payload["capabilities"] = result.capabilities\n',
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
        ADDON_LINKING,
        (
            "    counted: dict[str, object] = {\n"
            '        "total": len(items),\n'
            '        "by_type": summarize_type_counts(str(getattr(item, "id_type", "")) for item in items),\n'
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
        ADDON_LINKING,
        "    return dict(sorted(counts.items()))",
        "    return counts",
        (f"{LKT}::test_type_counts_are_ordered_by_type_whatever_order_the_datablocks_arrived_in",),
    ),
    Revert(
        "linking: a bounded sub-list offers an offset to resume from, which every one of these commands rejects",
        ADDON_LINKING,
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
        ADDON_LINKING,
        '    return {**counted, **_record_page("names", items, _display_name, name_limit)}\n',
        '    return {**counted, **_record_page("records", items, describe, limit)}\n',
        (
            f"{LKT}::test_a_reload_reports_what_it_replaced_by_type_without_the_records[reload_library]",
            f"{LKT}::test_a_reload_reports_what_it_replaced_by_type_without_the_records[relocate_library]",
        ),
    ),
    Revert(
        "linking: detail is ignored, so a library's datablock records are unreachable",
        ADDON_LINKING,
        ('    if detail:\n        return {**counted, **_record_page("records", items, describe, limit)}\n'),
        ('    if False:\n        return {**counted, **_record_page("records", items, describe, limit)}\n'),
        (f"{LKT}::test_list_libraries_lists_the_datablock_records_only_on_request",),
    ),
    Revert(
        "linking: the default name page grows to the record cap, so a listing is ten times its size",
        ADDON_LINKING,
        "MAX_LISTED_NAMES = 10\n",
        "MAX_LISTED_NAMES = 100\n",
        (f"{LKT}::test_list_libraries_bounds_the_datablocks_it_lists_per_library",),
    ),
    Revert(
        "linking: create_override lists the override's objects as records, repeating changed_objects",
        ADDON_LINKING,
        "    listed = _counted_page(objects, _override_entry, MAX_LISTED_DATABLOCKS, detail=detail, name_limit=None)",
        "    listed = _counted_page(objects, _override_entry, MAX_LISTED_DATABLOCKS, detail=True, name_limit=None)",
        (f"{LKT}::test_create_override_counts_the_objects_it_made_and_leaves_their_names_to_changed_objects",),
    ),
    Revert(
        "linking: nothing names the objects an override made, so changed_objects is empty",
        ADDON_LINKING,
        "    return sorted(names)\n",
        "    return []\n",
        (
            f"{LKT}::test_create_override_reports_the_override_objects",
            f"{LKT}::test_link_as_override_reports_the_override_objects",
        ),
    ),
    Revert(
        "linking: a linked collection's members are left out of changed_objects",
        ADDON_LINKING,
        "            members = [obj for collection in linked_collections for obj in collection.all_objects]"
        "  # type: ignore[attr-defined]\n",
        "            members = []\n",
        (f"{LKT}::test_link_reports_the_objects_it_brought_into_the_scene",),
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
        "        objects = objects[:limit]\n",
        "",
        (f"{SFLT}::test_changed_objects_are_bounded_and_the_total_is_reported",),
    ),
    Revert(
        "server tools: a cut changed_objects list never says how many objects there really were",
        SERVER_ENVELOPE,
        '        notices.append(f"changed_objects lists the first {limit} of {len(objects)} objects")\n',
        "",
        (f"{SFLT}::test_changed_objects_are_bounded_and_the_total_is_reported",),
    ),
    # --- the budget recognises a page named after its own list ---
    Revert(
        "reply budget: a page paged under its list's own name is not recognised as a page",
        SERVER_ENVELOPE,
        '    for prefix in ("", f"{key}_"):\n',
        '    for prefix in ("",):\n',
        (f"{ENVT}::test_a_page_paged_under_a_prefixed_name_is_still_marked_truncated",),
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
    # --- a pose reply names what it changed; the matrices are the part that is trimmed ---
    Revert(
        "pose: the pose matrix is published at full float precision",
        ADDON_POSING,
        "    return [[round(float(value), _POSE_MATRIX_DECIMALS) for value in row] for row in matrix]\n",
        "    return [[float(value) for value in row] for row in matrix]\n",
        (f"{CTRLT}::test_pose_report_rounds_the_result_and_omits_the_pre_call_matrix",),
    ),
    Revert(
        "pose: detail is ignored, so the pre-call matrix and full precision are unreachable",
        ADDON_POSING,
        "        if detail:\n"
        '            record["before_pose_matrix"] = _matrix_list(before[pose_bone.name])\n'
        '            record["after_pose_matrix"] = _matrix_list(pose_bone.matrix)\n'
        "        else:\n"
        '            record["after_pose_matrix"] = _rounded_matrix_list(pose_bone.matrix)\n',
        '        record["after_pose_matrix"] = _rounded_matrix_list(pose_bone.matrix)\n',
        (
            f"{CTRLT}::test_pose_detail_restores_the_pre_call_matrix_and_full_precision",
            f"{CTRLT}::test_keyframe_detail_reports_the_pose_that_was_keyed",
        ),
    ),
    Revert(
        "pose: a record names the transform channels but not the custom properties it set",
        ADDON_POSING,
        '    channels.extend(f\'["{name}"]\' for name in sorted(spec.get("custom_properties", {})))\n',
        "",
        (f"{CTRLT}::test_pose_record_names_the_channels_and_custom_properties_the_call_set",),
    ),
    Revert(
        # `solve_bone_reach` reports `changed_bones` from the same records with the same
        # spelling, so the anchor reaches up to `"space"`, which only `set_character_pose` sends.
        "pose: changed_bones is dropped, so a shortened page of records is all the agent gets",
        ADDON_POSING,
        '            "space": space,\n'
        "            # Complete, and cheap enough to stay complete: the per-bone records are what the\n"
        "            # reply budget shortens, so this is what still names every bone the call posed.\n"
        '            "changed_bones": [record["bone"] for record in records],\n',
        '            "space": space,\n',
        (f"{CTRLT}::test_the_budget_shortens_pose_records_but_never_the_changed_bone_names",),
    ),
    Revert(
        "pose: a keyframed pose reports every bone's matrices whether or not they were asked for",
        ADDON_POSING,
        "    if pose_records is not None:\n"
        "        # The pose is restored before this returns, so these matrices describe what was keyed at\n"
        "        # the requested frame, not what the rig is holding now.\n"
        '        reply["bones"] = pose_records\n',
        '    reply["bones"] = pose_records\n',
        (f"{CTRLT}::test_keyframed_pose_names_every_bone_and_reports_no_matrices_by_default",),
    ),
    Revert(
        "pose: keyframe_character_pose does not forward detail",
        SERVER_POSING_TOOL,
        # `keyframe_bone_reach` ends its payload with the same two lines, so the anchor reaches
        # up to `space`, which only `keyframe_character_pose` sends.
        '            "space": space,\n'
        '            "keying_policy": keying_policy,\n'
        '            "interpolation": interpolation,\n'
        '            "handle_left": handle_left,\n'
        '            "handle_right": handle_right,\n'
        '            "easing": easing,\n'
        '            "action_policy": action_policy,\n'
        '            "confirm_displace_action": confirm_displace_action,\n'
        '            "action_slot_identifier": action_slot_identifier,\n'
        '            "detail": detail,\n',
        '            "space": space,\n'
        '            "keying_policy": keying_policy,\n'
        '            "interpolation": interpolation,\n'
        '            "handle_left": handle_left,\n'
        '            "handle_right": handle_right,\n'
        '            "easing": easing,\n'
        '            "action_policy": action_policy,\n'
        '            "confirm_displace_action": confirm_displace_action,\n'
        '            "action_slot_identifier": action_slot_identifier,\n'
        '            "detail": False,\n',
        (f"{CTRLT}::test_pose_tools_forward_the_detail_flag",),
    ),
    # --- the pose an agent authors reaches the file, and a child is solved against its parent ---
    Revert(
        # The restore is conditional on the block having raised: a call that authored keys has
        # to leave its own action assigned, or Blender drops it at save. Turning the `except`
        # into a `finally` is exactly the unconditional hand-back that discards the work.
        "pose: the keyed action is unassigned again, so Blender drops it at save",
        ADDON_POSING,
        "    try:\n"
        "        yield previous_action\n"
        "    except BaseException:\n"
        "        animation.action = previous_action\n"
        "        if previous_action is not None and previous_slot is not None:\n"
        "            # Assigning an action resets the slot, and a slot Blender no longer considers\n"
        "            # suitable is its refusal to make, not this unwind's to force.\n"
        "            with contextlib.suppress(Exception):\n"
        "                animation.action_slot = previous_slot\n"
        "        raise\n",
        "    try:\n"
        "        yield previous_action\n"
        "    finally:\n"
        "        animation.action = previous_action\n"
        "        if previous_action is not None and previous_slot is not None:\n"
        "            with contextlib.suppress(Exception):\n"
        "                animation.action_slot = previous_slot\n",
        (
            f"{POSET}::test_keying_leaves_the_rig_driven_by_the_action_it_authored",
            f"{POSET}::test_keying_reports_the_action_it_displaced",
        ),
    ),
    Revert(
        "pose: an absolute-space target is built before the parent this call moves",
        ADDON_POSING,
        '        elif space == _PARENT_RELATIVE_SPACE and "aim_at" not in spec:\n',
        '        elif "aim_at" not in spec:\n',
        (f"{POSET}::test_an_absolute_space_child_is_resolved_against_the_parent_this_call_moved",),
    ),
    Revert(
        "pose: aim_at resolves to nothing, so the bone keeps the rotation it had",
        ADDON_POSING,
        '    if "aim_at" in spec:\n'
        "        # An aim is stated in the scene, not in the call's space, and lands in armature space.\n"
        '        return _aim_pose_matrix(armature, pose_bone, spec["aim_at"]), "POSE"\n',
        "",
        (
            f"{POSET}::test_posing_an_aim_lands_it_on_the_target_after_the_parent_has_moved",
            f"{POSET}::test_a_keyed_aim_takes_the_short_way_round_from_the_previous_key",
            f"{POSET}::test_a_keyed_euler_aim_stays_on_the_previous_keys_branch",
        ),
    ),
    Revert(
        "pose: a keyed aim is spelled without regard to the previous key, so it spins between them",
        ADDON_POSING,
        '                if "aim_at" in spec and path in _ROTATION_CHANNEL_WIDTH:\n'
        "                    _match_previous_rotation(action, pose_bone, path, frame)\n",
        "",
        (
            f"{POSET}::test_a_keyed_aim_takes_the_short_way_round_from_the_previous_key",
            f"{POSET}::test_a_keyed_euler_aim_stays_on_the_previous_keys_branch",
        ),
    ),
    Revert(
        "pose: rest axes are withheld even when the caller asks for them",
        ADDON_POSING,
        '            if rest_axes:\n                item["rest_axes"] = _rest_axes(bone)\n',
        "",
        (f"{POSET}::test_rest_axes_are_reported_only_when_asked_for",),
    ),
    Revert(
        "pose: an aim's up axis is not made perpendicular, so the basis shears",
        ADDON_POSING,
        "    columns = {track_letter: direction * track_sign, up_letter: residual.normalized() * up_sign}\n",
        "    columns = {track_letter: direction * track_sign, up_letter: up_pose * up_sign}\n",
        (f"{POSET}::test_aim_points_the_named_axis_at_an_object_and_leaves_position_and_scale_alone",),
    ),
    Revert(
        "pose: an aim reads its world target as if the rig were at the origin",
        ADDON_POSING,
        "    world_to_pose = armature.matrix_world.inverted()\n",
        "    world_to_pose = mathutils.Matrix.Identity(4)\n",
        (
            f"{POSET}::test_aim_at_a_world_point_resolves_through_the_rig_transform",
            f"{POSET}::test_aim_points_the_named_axis_at_an_object_and_leaves_position_and_scale_alone",
        ),
    ),
    Revert(
        "pose: an aim target on the bone head is normalised instead of refused",
        ADDON_POSING,
        "    if distance <= _AIM_MIN_DISTANCE:\n",
        "    if False:\n",
        (f"{POSET}::test_aim_rejects_every_direction_it_cannot_define",),
    ),
    Revert(
        "pose: a minimal-arc aim accepts a half turn and rolls the bone arbitrarily",
        ADDON_POSING,
        "        if swing > _AIM_MAX_MINIMAL_ARC:\n",
        "        if False:\n",
        (f"{POSET}::test_a_minimal_arc_aim_past_the_flip_angle_is_refused_rather_than_rolled_arbitrarily",),
    ),
    Revert(
        "pose: rotate reads its angle as radians, so a degree value under-rotates",
        ADDON_POSING,
        '"angle": math.radians(degrees),',
        '"angle": degrees,',
        (f"{POSET}::test_rotate_resolves_named_axes_and_vectors_in_degrees",),
    ),
    Revert(
        "pose: relative rotate replaces the rotation instead of composing with it",
        ADDON_POSING,
        '        rotation = (delta @ mathutils.Quaternion(rotation)) if record["relative"] else delta\n',
        "        rotation = delta\n",
        (f"{POSET}::test_relative_rotate_composes_while_the_default_replaces",),
    ),
    Revert(
        "pose: a resolved rotation keys no channel at all, so an aim writes no curves",
        ADDON_POSING,
        '_ROTATION_CHANNELS = ("rotation_euler", "rotation_quaternion", "rotation_axis_angle", "rotate", "aim_at")\n',
        '_ROTATION_CHANNELS = ("rotation_euler", "rotation_quaternion", "rotation_axis_angle")\n',
        (
            f"{POSET}::test_a_resolved_rotation_keys_only_the_bones_native_channel[QUATERNION-rotation_quaternion]",
            f"{POSET}::test_a_resolved_rotation_keys_only_the_bones_native_channel[XYZ-rotation_euler]",
            f"{POSET}::test_a_resolved_rotation_keys_only_the_bones_native_channel[AXIS_ANGLE-rotation_axis_angle]",
        ),
    ),
    Revert(
        # `restored_bone_pose` owns every hand-back now, so the failure path is the branch that
        # runs `restore()` before re-raising; dropping it leaves the last solved pose on the rig.
        "pose: a failed keying call leaves the half-applied pose on the rig",
        ADDON_POSING,
        "    try:\n        yield\n    except BaseException:\n        restore()\n        raise\n",
        "    try:\n        yield\n    except BaseException:\n        raise\n",
        (f"{POSET}::test_a_failed_key_hands_the_rig_back_as_it_arrived",),
    ),
    Revert(
        "pose: keying accepts a roll-preserving aim, so two frames key two different rolls",
        ADDON_POSING,
        '        if "aim_at" in spec and spec["aim_at"]["up"] is None:\n',
        "        if False:\n",
        (f"{POSET}::test_keying_an_aim_without_an_up_reference_is_refused",),
    ),
    # --- solve_bone_reach says whether it converged, and why not ---
    Revert(
        "pose: a bone reach reports itself converged whatever it achieved",
        ADDON_POSING,
        '        converged=measured["achieved_error_m"] <= tolerance_m,\n',
        "        converged=True,\n",
        (
            f"{POSET}::test_a_tighter_tolerance_turns_the_same_solve_into_a_miss",
            f"{POSET}::test_a_reachable_target_the_solve_stalled_short_of_warns_without_blaming_the_rig",
            f"{POSET}::test_a_target_beyond_the_chains_reach_is_reported_as_unreachable",
            f"{POSET}::test_a_missed_reach_still_warns_after_the_envelope_has_shortened_the_reply",
        ),
    ),
    Revert(
        "pose: a bone reach cannot tell an unreachable target from a stalled solve",
        ADDON_POSING,
        '        out_of_reach=measured["target_distance_m"] > measured["chain_reach_m"],\n',
        "        out_of_reach=False,\n",
        (f"{POSET}::test_a_target_beyond_the_chains_reach_is_reported_as_unreachable",),
    ),
    Revert(
        "pose: a bone reach sums rest bone lengths, ignoring the rig's world scale",
        ADDON_POSING,
        "    matrix = armature.matrix_world\n"
        "    return sum((matrix @ bone.tail_local - matrix @ bone.head_local).length for bone in rest_chain)\n",
        "    return sum(bone.length for bone in rest_chain)\n",
        (f"{POSET}::test_the_chains_reach_is_measured_in_world_space_not_in_rest_bone_lengths",),
    ),
    Revert(
        "pose: a missed bone reach reports its numbers but raises no warning",
        ADDON_POSING,
        '            "warnings": [\n'
        "                warning\n"
        "                for warning in (_reach_convergence_warning(solution, tolerance_m) for solution in solutions)\n"
        "                if warning is not None\n"
        "            ],\n",
        '            "warnings": [],\n',
        (
            f"{POSET}::test_a_reachable_target_the_solve_stalled_short_of_warns_without_blaming_the_rig",
            f"{POSET}::test_a_target_beyond_the_chains_reach_is_reported_as_unreachable",
            f"{POSET}::test_a_missed_reach_still_warns_after_the_envelope_has_shortened_the_reply",
        ),
    ),
    Revert(
        "pose: a bone reach takes tolerance_m as given, so 0 or NaN reaches the solve",
        ADDON_POSING,
        # Both reach tools read the tolerance through `_validated_tolerance`, so reverting the
        # one helper is what lets 0 or NaN reach either solve.
        '    tolerance_m = _finite(tolerance_m, "tolerance_m")\n'
        "    if tolerance_m <= 0.0:\n"
        '        raise ValueError(f"tolerance_m must be greater than 0 metres, not {tolerance_m}")\n'
        "    return tolerance_m\n",
        "    return float(tolerance_m)\n",
        tuple(
            f"{POSET}::test_a_tolerance_that_names_no_precision_is_refused_before_the_rig_is_touched[{case}]"
            for case in ("0.0", "-0.0001", "nan", "inf")
        ),
    ),
    Revert(
        "pose: a bone reach never says which tolerance it judged the solve against",
        ADDON_POSING,
        '            "tolerance_m": tolerance_m,\n',
        "",
        (
            f"{POSET}::test_a_reach_inside_its_tolerance_reports_converged_and_says_nothing_else",
            f"{POSET}::test_a_tighter_tolerance_turns_the_same_solve_into_a_miss",
        ),
    ),
    Revert(
        "pose: every missed bone reach is blamed on the target being out of reach",
        ADDON_POSING,
        "    if solution.out_of_reach:\n",
        "    if True:\n",
        (f"{POSET}::test_a_reachable_target_the_solve_stalled_short_of_warns_without_blaming_the_rig",),
    ),
    Revert(
        "pose: a converged bone reach warns anyway, so every solve carries a notice",
        ADDON_POSING,
        "    if solution.converged:\n        return None\n",
        "    if False:\n        return None\n",
        (f"{POSET}::test_a_reach_inside_its_tolerance_reports_converged_and_says_nothing_else",),
    ),
    # --- the chain, pole and target resolution both reach tools share ---
    Revert(
        # A chain that walks through a fork picks up a bone the IK solver will then drive
        # sideways: the reach bends the other arm as well as the one it was asked about.
        "pose: an auto-resolved chain walks straight through a fork",
        ADDON_POSING,
        "        if parent is None or len(parent.children) > 1:\n",
        "        if parent is None:\n",
        (
            f"{POSET}::test_unbranched_ancestor_chain_stops_before_a_mid_chain_fork",
            f"{POSET}::test_unbranched_ancestor_chain_stops_before_a_root_level_fork",
        ),
    ),
    Revert(
        "pose: an auto-resolved chain ignores the cap and runs to the root",
        ADDON_POSING,
        "    while len(chain) < max_length:\n",
        "    while True:\n",
        (f"{POSET}::test_unbranched_ancestor_chain_respects_max_length",),
    ),
    Revert(
        # The deliberate opposite of the fork row: stopping one bone short of an unforked root
        # is equally wrong, and a leg rooted at the hips loses the hip bone that carries it.
        "pose control: an auto-resolved chain stops one short of an unforked root",
        ADDON_POSING,
        "        chain.append(parent)\n",
        "        if parent.parent is None:\n            break\n        chain.append(parent)\n",
        (f"{POSET}::test_unbranched_ancestor_chain_includes_an_unforked_root",),
    ),
    Revert(
        # The tip seeds its own chain, so a root bone still resolves to a one-bone reach
        # rather than to nothing the solver can drive.
        "pose: a resolved chain leaves out the tip bone it was asked to solve",
        ADDON_POSING,
        "    chain = [tip]\n    bone = tip\n    while len(chain) < max_length:\n",
        "    chain = []\n    bone = tip\n    while len(chain) < max_length:\n",
        (f"{POSET}::test_unbranched_ancestor_chain_of_a_root_bone_is_just_that_bone",),
    ),
    Revert(
        # An explicit chain_length is an assertion about the rig, and it is the only way past a
        # fork the auto-resolve stops at; one bone short is a chain that cannot reach.
        "pose: an explicit chain_length resolves one bone short",
        ADDON_POSING,
        "    for _step in range(length - 1):\n",
        "    for _step in range(length - 2):\n",
        (f"{POSET}::test_rest_ancestor_chain_returns_the_exact_requested_length",),
    ),
    Revert(
        "pose: a chain_length past the root is silently shortened instead of refused",
        ADDON_POSING,
        "        if bone.parent is None:\n"
        "            raise ValueError(f\"'{tip.name}' has only {len(chain)} ancestor(s); "
        'chain_length={length} exceeds them")\n',
        "        if bone.parent is None:\n            break\n",
        (f"{POSET}::test_rest_ancestor_chain_refuses_a_length_past_the_root",),
    ),
    Revert(
        # The round-2 draft this row pins: taking `chain[len(chain) // 2]` as the pole reference
        # is the ROOT bone on a two-bone chain, so offset-from-root is zero and every elbow and
        # knee is refused as "straight".
        "pose: pole synthesis takes the chain's root as its bend reference",
        ADDON_POSING,
        "    joints = [tip_tail, *(bone.head_local for bone in chain)]\n    mid = joints[len(joints) // 2]\n",
        "    mid = chain[len(chain) // 2].head_local\n",
        (f"{POSET}::test_synthesize_pole_finds_the_bend_side_of_a_bent_two_bone_chain",),
    ),
    Revert(
        # A straight rest chain names no bend direction, so a synthesized pole would be noise
        # pointing wherever float error happened to land: refuse and say to supply one.
        "pose: a straight rest chain has a pole guessed from float noise instead of refusing",
        ADDON_POSING,
        "    if projected.length <= _AIM_MIN_RESIDUAL:\n",
        "    if False:\n",
        (
            f"{POSET}::test_synthesize_pole_refuses_a_straight_two_bone_rest_chain",
            f"{POSET}::test_synthesize_pole_refuses_a_single_bone_chain",
        ),
    ),
    Revert(
        "pose: a chain whose root and tip coincide is normalised instead of refused",
        ADDON_POSING,
        "    if axis.length <= _AIM_MIN_LENGTH:\n",
        "    if False:\n",
        (f"{POSET}::test_synthesize_pole_refuses_a_chain_whose_root_and_tip_coincide",),
    ),
    Revert(
        # Rest bones are armature-space; the pole is handed to an IK constraint as a world
        # point, so a rig anywhere but the origin bends towards a point beside the character.
        "pose: a synthesized pole is reported in armature space as if it were world space",
        ADDON_POSING,
        "    return armature.matrix_world @ pole_local\n",
        "    return pole_local\n",
        (f"{POSET}::test_synthesize_pole_converts_through_the_armatures_world_matrix",),
    ),
    Revert(
        "pose: a reach on a bone the rig does not have is solved instead of refused",
        ADDON_POSING,
        '    if rest_tip is None:\n        raise ValueError(f"Pose bone not found: {tip_name}")\n',
        '    if False:\n        raise ValueError(f"Pose bone not found: {tip_name}")\n',
        (f"{POSET}::test_resolve_reach_chain_refuses_an_unknown_tip_bone",),
    ),
    Revert(
        # chain_length_source is how a caller learns whether the chain it got was the one it
        # asked for or one this handler inferred; swapping the two labels keeps both reports
        # present and makes both of them lies.
        "pose: a reach mislabels whether its chain length was inferred or given",
        ADDON_POSING,
        '        chain_length_source = "resolved"\n'
        "    else:\n"
        "        rest_chain = _rest_ancestor_chain(rest_tip, requested_length)\n"
        '        chain_length_source = "explicit"\n',
        '        chain_length_source = "explicit"\n'
        "    else:\n"
        "        rest_chain = _rest_ancestor_chain(rest_tip, requested_length)\n"
        '        chain_length_source = "resolved"\n',
        (
            f"{POSET}::test_resolve_reach_chain_reports_resolved_when_chain_length_is_omitted",
            f"{POSET}::test_resolve_reach_chain_reports_explicit_when_chain_length_is_given",
        ),
    ),
    Revert(
        # Two reaches solving one bone to two targets is ambiguous; without the refusal the
        # later reach silently wins and the earlier one reports a pose it did not get.
        "pose: two reaches may claim the same bone, and the later one silently wins",
        ADDON_POSING,
        '    if overlap:\n        raise ValueError(f"Bones claimed by more than one reach: {overlap}")\n',
        '    if False:\n        raise ValueError(f"Bones claimed by more than one reach: {overlap}")\n',
        (f"{POSET}::test_resolve_reach_chain_refuses_a_bone_already_claimed_by_an_earlier_reach",),
    ),
    Revert(
        "pose: a reach target naming an object that is not there resolves to None",
        ADDON_POSING,
        '        if obj is None:\n            raise ValueError(f"{label} object not found: {object_name}")\n',
        '        if False:\n            raise ValueError(f"{label} object not found: {object_name}")\n',
        (f"{POSET}::test_resolved_reach_target_refuses_an_unknown_object_name",),
    ),
    Revert(
        # The target's scratch Empty exists before the pole is resolved and before
        # `_solve_one_reach`'s own try/finally starts, so an unresolvable pole strands it in
        # the file under a `__solve_bone_reach__` name nobody will recognise.
        "pose: a reach refused over its pole strands the target's scratch Empty in the file",
        ADDON_POSING,
        "        if target_is_temp:\n            bpy.data.objects.remove(target_obj, do_unlink=True)\n        raise\n",
        "        raise\n",
        (f"{POSET}::test_a_reach_whose_pole_cannot_be_resolved_removes_the_targets_scratch_empty",),
    ),
    Revert(
        # `constraints.new` lands the constraint on the rig before any field is written, so a
        # value Blender's RNA refuses would leave a live IK constraint on the tip bone.
        "pose: a constraint value Blender refuses leaves the IK constraint live on the rig",
        ADDON_POSING,
        "    except Exception:\n"
        "        # The constraint is on the rig from `new()` onwards, and the caller's own try/finally\n"
        "        # only covers a constraint this function returned. A value Blender's RNA refuses must\n"
        "        # not leave a live IK constraint behind - the same reason `add_pose_bone_constraint`\n"
        "        # removes a constraint it created but could not configure.\n"
        "        tip_pose_bone.constraints.remove(constraint)\n"
        "        raise\n",
        "    except Exception:\n        raise\n",
        (f"{POSET}::test_a_constraint_value_blender_refuses_removes_the_constraint_it_already_added",),
    ),
    Revert(
        # Neither form given is a reach with nowhere to go; both given is two answers to one
        # question. The schema cannot express "exactly one", so the model has to.
        "pose: a reach naming no target, or two, is accepted by the schema",
        SERVER_POSING_TOOL,
        "            BoneReach: This model, unchanged.\n"
        "\n"
        "        Raises:\n"
        "            ValueError: If neither or both target forms are given.\n"
        "\n"
        '        """\n'
        "        if (self.target is None) == (self.target_object is None):\n"
        '            raise ValueError("Supply exactly one of target or target_object")\n',
        "            BoneReach: This model, unchanged.\n"
        "\n"
        "        Raises:\n"
        "            ValueError: If neither or both target forms are given.\n"
        "\n"
        '        """\n',
        (f"{POSET}::test_bone_reach_requires_exactly_one_target_form",),
    ),
    Revert(
        "pose: a reach naming two pole targets is accepted by the schema",
        SERVER_POSING_TOOL,
        "        if self.pole_target is not None and self.pole_target_object is not None:\n"
        '            raise ValueError("Supply at most one of pole_target or pole_target_object")\n',
        "",
        (f"{POSET}::test_bone_reach_allows_at_most_one_pole_form",),
    ),
    Revert(
        # An unset optional field sent as null is not the same request as one left out: the
        # handler reads `reach.get("pole_target")` and an explicit None would stop pole
        # synthesis from ever running.
        "pose: a reach sends every optional field as null instead of omitting it",
        SERVER_POSING_TOOL,
        '            "reaches": [reach.model_dump(exclude_none=True) for reach in reaches],\n'
        '            "tolerance_m": tolerance_m,\n'
        '            "detail": detail,\n',
        '            "reaches": [reach.model_dump() for reach in reaches],\n'
        '            "tolerance_m": tolerance_m,\n'
        '            "detail": detail,\n',
        (f"{POSET}::test_solve_bone_reach_forwards_reaches_and_omits_unset_optional_fields",),
    ),
    # --- a reach that raises part way through hands the rig back the action it arrived on ---
    Revert(
        # The deliberate opposite of "the keyed action is unassigned again": that row proves
        # restoring unconditionally is caught, this one proves never restoring is caught too.
        # `object_state` does not snapshot `animation_data.action`, so nothing else in the
        # transaction puts the displaced action back when the solve raises mid-range.
        "pose: a reach that raises keeps the action it assigned, displacement and all",
        ADDON_POSING,
        "    except BaseException:\n"
        "        animation.action = previous_action\n"
        "        if previous_action is not None and previous_slot is not None:\n",
        "    except BaseException:\n        if False:\n",
        (f"{POSET}::test_a_reach_that_fails_part_way_through_hands_back_the_action_it_arrived_on",),
    ),
    # --- configure_render_settings answers with the paths it wrote, not the whole state ---
    Revert(
        "render settings: the reply carries the whole render state again instead of what it wrote",
        ADDON_RENDERING,
        "    after = {path: getattr(owner, name) for path, (owner, name) in applied.items()}\n",
        "    after = _render_info(scene)\n",
        (
            f"{RENDT}::test_configure_render_settings_returns_only_the_patched_values",
            f"{RENDT}::test_configure_render_settings_reports_a_patch_that_writes_nothing",
        ),
    ),
    Revert(
        "render settings: changed names the patch's top-level keys, not the property paths written",
        ADDON_RENDERING,
        '    changed = sorted([*applied, "frame_range_authored"] if authored_range else applied)\n',
        # `_patch_reply` is handed `applied`, not the patch, so the coarse top-level keys are
        # reconstructed from it: for a flat key the two are the same string, and for a nested
        # one `applied` carries "<section>.<key>" where the patch carried "<section>".
        '    changed = sorted({key.split(".")[0] for key in applied} | ({"frame_range_authored"} '
        "if authored_range else set()))\n",
        (f"{RENDT}::test_configure_render_settings_returns_only_the_patched_values",),
    ),
    Revert(
        "render settings: detail is ignored, so the before/after state is unreachable",
        ADDON_RENDERING,
        "    if detail:\n",
        "    if False:\n",
        (f"{RENDT}::test_configure_render_settings_detail_returns_both_full_state_blocks",),
    ),
    Revert(
        "render settings: configure_render_settings does not forward detail",
        SERVER_RENDERING_TOOL,
        '        {"scene_name": scene_name, "patch": patch.model_dump(exclude_none=True), "detail": detail},\n',
        '        {"scene_name": scene_name, "patch": patch.model_dump(exclude_none=True), "detail": False},\n',
        (f"{RENDT}::test_configure_render_settings_forwards_detail",),
    ),
    # --- artefact truth: the datablocks a save discards ---
    Revert(
        "transaction: a created datablock with no user is not reported as one the save discards",
        ADDON_TRANSACTION,
        '                if int(getattr(db, "users", 1) or 0) == 0',
        '                if int(getattr(db, "users", 1) or 0) < 0',
        (
            f"{MUTT}::test_persistence_an_unreferenced_created_datablock_is_reported",
            f"{MUTT}::test_persistence_the_warning_names_at_most_five_and_counts_the_rest",
        ),
    ),
    Revert(
        "transaction: a fake user is counted as no user, so a deliberate keep is reported as a loss",
        ADDON_TRANSACTION,
        '                if int(getattr(db, "users", 1) or 0) == 0',
        '                if int(getattr(db, "users", 1) or 0) == 0 or getattr(db, "use_fake_user", False)',
        (f"{MUTT}::test_persistence_a_fake_user_datablock_is_not_reported",),
    ),
    Revert(
        "transaction: a datablock with one real user is reported as unreferenced",
        ADDON_TRANSACTION,
        '                if int(getattr(db, "users", 1) or 0) == 0',
        '                if int(getattr(db, "users", 1) or 0) <= 1',
        (f"{MUTT}::test_persistence_an_assigned_datablock_is_not_reported",),
    ),
    Revert(
        "transaction: another file's datablock is claimed as this command's authorship",
        ADDON_TRANSACTION,
        "            if coll_name not in _AUTHORSHIP_EXEMPT_COLLECTIONS and "
        'getattr(datablock, "library", None) is None',
        "            if coll_name not in _AUTHORSHIP_EXEMPT_COLLECTIONS",
        (f"{MUTT}::test_persistence_a_linked_datablock_is_never_this_commands_authorship",),
    ),
    Revert(
        "transaction: the discard warning names every datablock instead of a bounded few",
        ADDON_TRANSACTION,
        "    shown = \", \".join(f\"{entry['collection']}:{entry['name']}\" "
        "for entry in entries[:MAX_REPORTED_UNREFERENCED])",
        "    shown = \", \".join(f\"{entry['collection']}:{entry['name']}\" for entry in entries)",
        (f"{MUTT}::test_persistence_the_warning_names_at_most_five_and_counts_the_rest",),
    ),
    Revert(
        "scene validation: the persistence domain stops reporting datablocks with no user",
        ADDON_SCENE,
        "            if users == 0:",
        "            if users < 0:",
        (
            f"{SVT}::test_persistence_findings_flag_unreferenced_and_fake_user_only_datablocks",
            f"{SVT}::test_persistence_findings_report_truncation_past_max_findings",
            f"{SVT}::test_validate_scene_runs_the_persistence_domain_on_request",
        ),
    ),
    Revert(
        "scene validation: an action kept alive only by a fake user is reported as driving something",
        ADDON_SCENE,
        '            elif coll_name == "actions" and getattr(datablock, "use_fake_user", False) and users <= 1:',
        "            elif False:",
        (f"{SVT}::test_persistence_findings_flag_unreferenced_and_fake_user_only_datablocks",),
    ),
    Revert(
        "scene validation: a linked datablock is reported as this file's to lose",
        ADDON_SCENE,
        '            if getattr(datablock, "library", None) is not None:\n                continue',
        "            if False:\n                continue",
        (f"{SVT}::test_persistence_findings_ignore_linked_datablocks",),
    ),
    Revert(
        "scene validation: the persistence domain is never run",
        ADDON_SCENE,
        '        if "persistence" in domains:',
        "        if False:",
        (f"{SVT}::test_validate_scene_runs_the_persistence_domain_on_request",),
    ),
    # --- artefact truth: will this file resolve elsewhere ---
    Revert(
        "delivery: a path outside the shot is published whole instead of by leaf",
        ADDON_DELIVERY,
        (
            "    whole = safe_relative_link(text, MAX_REPORTED_LINK_CHARS)\n"
            "    return whole if whole is not None else client_safe_leaf(text, is_directory=is_directory)"
        ),
        "    return text",
        (
            f"{FLT}::test_inspect_delivery_reports_an_absolute_image_by_leaf_not_by_directory",
            f"{FLT}::test_inspect_delivery_does_not_mistake_a_rooted_triple_slash_path_for_a_relative_one",
        ),
    ),
    Revert(
        "delivery: relativity is judged by the // prefix, so a rooted path reads as portable",
        ADDON_DELIVERY,
        "    return relative_link_body(strip_unsafe(raw)) is not None",
        '    return str(raw or "").startswith("//")',
        (f"{FLT}::test_inspect_delivery_does_not_mistake_a_rooted_triple_slash_path_for_a_relative_one",),
    ),
    Revert(
        "delivery: packed pixels are judged by their path rather than by being packed",
        ADDON_DELIVERY,
        '        if getattr(image, "packed_file", None) is not None:\n            verdict = "PACKED"',
        '        if False:\n            verdict = "PACKED"',
        (f"{FLT}::test_inspect_delivery_reports_a_packed_image_as_portable",),
    ),
    Revert(
        "delivery: a broken image link is reported by path shape alone",
        ADDON_DELIVERY,
        '        elif image_path_missing(image):\n            verdict = "MISSING"',
        '        elif False:\n            verdict = "MISSING"',
        (f"{FLT}::test_inspect_delivery_reports_an_image_whose_file_is_gone_as_missing",),
    ),
    Revert(
        "delivery: portability is judged from the returned page, so a defect hides on page two",
        ADDON_DELIVERY,
        '    for entry in entries:\n        bucket = classes[entry["kind"]]',
        '    for entry in page:\n        bucket = classes[entry["kind"]]',
        (f"{FLT}::test_inspect_delivery_judges_portability_over_every_entry_not_the_returned_page",),
    ),
    Revert(
        "delivery: a cache that lives in memory is reported as a file that travels",
        ADDON_DELIVERY,
        '    if info.get("use_disk_cache"):\n        return "RELATIVE_OK" if bpy.data.filepath else "UNSET"\n'
        '    return "UNSET"',
        '    return "RELATIVE_OK"',
        (f"{FLT}::test_inspect_delivery_reports_a_memory_only_point_cache_as_unset",),
    ),
    Revert(
        "delivery: an unset Mantaflow cache directory passes as a portable default",
        ADDON_DELIVERY,
        '    verdict = "MISSING" if not resolved or not os.path.isdir(resolved) else _shape_verdict(raw)',
        "    verdict = _shape_verdict(raw)",
        (f"{FLT}::test_inspect_delivery_reports_an_unset_fluid_cache_directory_as_missing",),
    ),
    Revert(
        "delivery: hashing linked files needs no configured roots, making it a read oracle",
        ADDON_DELIVERY,
        '        roots = require_digest_roots("hash_libraries") if hash_libraries else []\n',
        "        roots = configured_file_roots()\n",
        (f"{FLT}::test_inspect_delivery_refuses_to_hash_libraries_without_configured_file_roots",),
        ("\n\nfrom ..output_roots import configured_file_roots\n"),
    ),
    Revert(
        "delivery: a library outside the roots is hashed anyway",
        ADDON_LIBRARY_DIGEST,
        "            enforce_roots(resolved, roots)",
        "            pass",
        (f"{FLT}::test_inspect_delivery_skips_hashing_a_library_outside_the_configured_roots",),
    ),
    Revert(
        "delivery: the page bounds are not checked before the scan walks bpy.data",
        ADDON_DELIVERY,
        "    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:",
        "    if False:",
        (f"{FLT}::test_inspect_delivery_refuses_an_out_of_range_page",),
    ),
    Revert(
        "delivery: an unsaved session is called portable, though // resolves against nothing",
        ADDON_DELIVERY,
        '        "portable": saved and not capped and unportable == 0,',
        '        "portable": not capped and unportable == 0,',
        (f"{FLT}::test_inspect_delivery_warns_that_an_unsaved_session_cannot_resolve_relative_paths",),
    ),
    # --- artefact truth: C2PA-shaped provenance in the file ---
    Revert(
        "provenance: the save writes no authorship block at all",
        ADDON_FILE_LIFECYCLE,
        (
            "        if request.write_provenance:\n"
            "            backup, ingredients = stamp_provenance(request.digest_roots)\n"
        ),
        ("        if False:\n            backup, ingredients = stamp_provenance(request.digest_roots)\n"),
        (
            f"{FLT}::test_save_shot_writes_a_json_provenance_block_into_every_local_scene",
            f"{FLT}::test_save_shot_names_the_datablocks_this_session_authored",
        ),
    ),
    Revert(
        "provenance: write_provenance=false writes a block anyway",
        ADDON_FILE_LIFECYCLE,
        (
            "        if request.write_provenance:\n"
            "            backup, ingredients = stamp_provenance(request.digest_roots)\n"
        ),
        ("        if True:\n            backup, ingredients = stamp_provenance(request.digest_roots)\n"),
        (f"{FLT}::test_save_shot_writes_nothing_when_provenance_is_declined",),
    ),
    Revert(
        "provenance: a linked scene is stamped with this file's authorship",
        ADDON_PROVENANCE,
        ("        if scene.library is not None:\n            continue"),
        ("        if False:\n            continue"),
        (f"{FLT}::test_save_shot_writes_a_json_provenance_block_into_every_local_scene",),
    ),
    Revert(
        "provenance: a save Blender refused leaves its claim on the scenes",
        ADDON_FILE_LIFECYCLE,
        ("    except RuntimeError as exc:\n        restore_provenance(backup)\n"),
        "    except RuntimeError as exc:\n",
        (f"{FLT}::test_a_failed_save_leaves_no_scene_claiming_provenance",),
    ),
    Revert(
        "provenance: checksums are taken with no roots to confine them",
        ADDON_LIBRARY_DIGEST,
        ("    roots = configured_file_roots()\n    if not roots:\n        raise ValueError(\n"),
        (
            "    roots = configured_file_roots()\n"
            "    if not roots:\n"
            '        return ["/"]\n'
            "    if not roots:\n"
            "        raise ValueError(\n"
        ),
        (f"{FLT}::test_save_shot_refuses_checksums_without_configured_file_roots",),
    ),
    Revert(
        "provenance: a library is hashed to the call's whole budget, not to the per-file bound",
        ADDON_PROVENANCE,
        "        library_digests(libraries, digest_roots, max_file_bytes=MAX_DIGEST_FILE_BYTES)\n",
        "        library_digests(libraries, digest_roots, max_file_bytes=MAX_DIGEST_TOTAL_BYTES)\n",
        (f"{FLT}::test_save_shot_bounds_each_library_hash_by_the_per_file_limit",),
        also="\nfrom ..file_digest import MAX_DIGEST_TOTAL_BYTES\n",
    ),
    Revert(
        "provenance: the block records no datablocks, so the file claims nothing was authored",
        ADDON_PROVENANCE,
        (
            "                \"datablocks\": [f\"{entry['collection']}:{entry['name']}\" "
            "for entry in authored.snapshot()],"
        ),
        '                "datablocks": [],',
        (f"{FLT}::test_save_shot_names_the_datablocks_this_session_authored",),
    ),
    Revert(
        "provenance: an oversized block is read back whole into the agent's context",
        ADDON_PROVENANCE,
        "    if not isinstance(raw, str) or len(raw) > MAX_PROVENANCE_CHARS:",
        "    if not isinstance(raw, str):",
        (f"{FLT}::test_inspect_delivery_reports_a_hostile_provenance_block_as_invalid[oversized]",),
    ),
    Revert(
        "provenance: unparseable text is reported as a valid block",
        ADDON_PROVENANCE,
        (
            "    except (TypeError, ValueError):\n"
            '        return {"present": True, "valid": False, "reason": "unparseable"}'
        ),
        (
            "    except (TypeError, ValueError):\n"
            '        return {"present": True, "valid": True, "reason": "unparseable"}'
        ),
        (f"{FLT}::test_inspect_delivery_reports_a_hostile_provenance_block_as_invalid[not json at all]",),
    ),
    Revert(
        "provenance: a JSON array is reported as a valid block",
        ADDON_PROVENANCE,
        (
            "    if not isinstance(block, dict):\n"
            '        return {"present": True, "valid": False, "reason": "unparseable"}'
        ),
        (
            "    if not isinstance(block, dict):\n"
            '        return {"present": True, "valid": True, "reason": "unparseable"}'
        ),
        (f"{FLT}::test_inspect_delivery_reports_a_hostile_provenance_block_as_invalid[[1, 2, 3]]",),
    ),
    Revert(
        "provenance: an ingredient list of any length is read back whole",
        ADDON_PROVENANCE,
        '        for entry in _bounded_dicts(block.get("ingredients"))\n',
        '        for entry in _unbounded_dicts(block.get("ingredients"))\n',
        (f"{FLT}::test_inspect_delivery_bounds_a_valid_provenance_block",),
        (
            "\n"
            "\n"
            "def _unbounded_dicts(value: object) -> list[dict]:\n"
            '    """\n'
            "    Reverted: `_bounded_dicts` without its `MAX_PROVENANCE_ENTRIES` cap.\n"
            "\n"
            "    Args:\n"
            "        value: The parsed value, of any shape.\n"
            "\n"
            "    Returns:\n"
            "        list[dict]: Every usable entry, however many the file carried.\n"
            "\n"
            '    """\n'
            "    if not isinstance(value, list):\n"
            "        return []\n"
            "    return [entry for entry in value if isinstance(entry, dict)]\n"
        ),
    ),
    Revert(
        "provenance: a file with no block is reported as carrying an invalid one",
        ADDON_PROVENANCE,
        ("    if raw is None:\n        return None"),
        ('    if raw is None:\n        return {"present": True, "valid": False, "reason": "unparseable"}'),
        (f"{FLT}::test_inspect_delivery_reports_no_provenance_for_a_file_without_one",),
    ),
    Revert(
        "provenance: a completed load keeps the replaced session's authorship",
        ADDON_SESSION,
        "    authored.clear()\n    _STORE.state = applied_load_post(_STORE.state, file_path)",
        "    _STORE.state = applied_load_post(_STORE.state, file_path)",
        (f"{SESSIONT}::test_a_completed_load_forgets_what_the_replaced_session_authored",),
    ),
    Revert(
        "provenance: a load that never landed throws away the open file's authorship",
        ADDON_SESSION,
        "    _STORE.state = applied_load_failure(_STORE.state, file_path, is_directory=_names_a_directory(file_path))",
        "    authored.clear()\n"
        "    _STORE.state = applied_load_failure(_STORE.state, file_path, is_directory=_names_a_directory(file_path))",
        (f"{SESSIONT}::test_a_failed_load_keeps_the_open_files_authorship",),
    ),
    Revert(
        "provenance: an aborted swap keeps an authorship claim it can no longer describe",
        ADDON_SESSION,
        "    # The swap was aborted part-way: what is open cannot be described truthfully, so the\n"
        "    # authorship claim goes with it.\n"
        "    authored.clear()",
        "    # The swap was aborted part-way.",
        (f"{SESSIONT}::test_an_aborted_swap_forgets_the_authorship_it_can_no_longer_describe",),
    ),
    # --- artefact truth: the delivery tool's own surface ---
    Revert(
        "server tools: inspect_delivery drops the parameters it is given",
        SERVER_FILE_LIFECYCLE_TOOL,
        '            "hash_libraries": hash_libraries,\n            "max_hash_bytes": max_hash_bytes,',
        '            "hash_libraries": False,\n            "max_hash_bytes": 1,',
        (
            f"{SFLT}::test_inspect_delivery_forwards_every_parameter",
            f"{SFLT}::test_inspect_delivery_defaults_do_not_read_linked_files",
        ),
    ),
    Revert(
        "server tools: inspect_delivery's paging and hash bounds are undeclared",
        SERVER_FILE_LIFECYCLE_TOOL,
        "    limit: Annotated[int, Field(ge=1, le=200)] = 50,\n"
        "    offset: Annotated[int, Field(ge=0)] = 0,\n"
        "    hash_libraries: bool = False,\n"
        "    max_hash_bytes: Annotated[int, Field(ge=1, le=8 * 1024**3)] = 268_435_456,",
        "    limit: int = 50,\n"
        "    offset: int = 0,\n"
        "    hash_libraries: bool = False,\n"
        "    max_hash_bytes: int = 268_435_456,",
        tuple(
            f"{SFLT}::test_inspect_delivery_schema_rejects_out_of_range_paging[{case}]"
            for case in ("limit-0", "limit-201", "max_hash_bytes-0", "max_hash_bytes-8589934593", "offset--1")
        ),
    ),
    # --- artefact truth: render intent belongs to the scene ---
    Revert(
        "rendering: a render with no filepath falls back to Blender's own output path silently",
        ADDON_RENDERING,
        "    requested_filepath = filepath\n    if requested_filepath is None:",
        '    requested_filepath = filepath or "/tmp/fallback.png"\n    if False:',
        (
            f"{RENDT}::test_render_scene_without_a_filepath_names_the_tool_that_sets_one",
            f"{RENDT}::test_render_scene_renders_to_the_scenes_own_output_path",
        ),
    ),
    Revert(
        "rendering: an ANIMATION over Blender's untouched default range renders unasked",
        ADDON_RENDERING,
        "        (scene.frame_start, scene.frame_end) == (1, 250)",
        "        False",
        (f"{RENDT}::test_render_scene_refuses_an_animation_over_blenders_untouched_default_range",),
    ),
    Revert(
        "rendering: a frame range the MCP set still trips the default-range guard",
        ADDON_RENDERING,
        '        and not scene.get("blender_mcp_frame_range_authored", False)',
        "        and True",
        (f"{RENDT}::test_render_scene_accepts_the_default_range_when_it_was_chosen",),
    ),
    Revert(
        "rendering: configure_render_settings stops marking a frame range as authored",
        ADDON_RENDERING,
        '            scene["blender_mcp_frame_range_authored"] = True',
        "            pass",
        (f"{RENDT}::test_render_scene_accepts_the_default_range_when_it_was_chosen",),
    ),
    Revert(
        "rendering: persist_output stores the resolved path instead of the caller's template",
        ADDON_RENDERING,
        "            scene.render.filepath = requested_filepath if persisted else original_path",
        "            scene.render.filepath = output if persisted else original_path",
        (f"{RENDT}::test_render_scene_persists_the_callers_template_not_the_resolved_path",),
    ),
    Revert(
        "rendering: every render stores its output path, whether or not it was asked to",
        ADDON_RENDERING,
        "            persisted = bool(persist_output) and not cancelled and completed",
        "            persisted = True",
        (
            f"{RENDT}::test_render_scene_leaves_the_output_path_alone_by_default",
            f"{RENDT}::test_render_scene_does_not_persist_a_cancelled_render",
        ),
    ),
    Revert(
        "rendering: a still's one-file path is stored as a per-frame template",
        ADDON_RENDERING,
        '        if persist_output and mode == "STILL":',
        "        if False:",
        (f"{RENDT}::test_render_scene_refuses_to_persist_a_still_path",),
    ),
    Revert(
        "rendering: a directory is accepted as the scene's stored output template",
        ADDON_RENDERING,
        '        _refuse_container_output(scene, pending["output"]["filepath"])',
        "        pass",
        (f"{RENDT}::test_configure_render_settings_refuses_a_directory_as_the_stored_template",),
    ),
    Revert(
        "rendering: the default reply carries every frame's bookkeeping again",
        ADDON_RENDERING,
        "    if not detail:",
        "    if False:",
        (f"{RENDT}::test_render_scene_reply_summarises_and_detail_restores_the_per_frame_arrays",),
    ),
    Revert(
        "dispatch: a render that stores its output template still bypasses the transaction",
        ADDON_SERVER_CORE,
        "            or (spec.non_undo_when is not None and spec.non_undo_when(params))",
        '            or cmd_type in {"render_scene"}',
        (f"{DRT}::test_a_render_that_persists_its_output_template_is_transacted",),
    ),
    # --- the one command registry: a row's name, its gate, and its classification ---
    Revert(
        "registry: a registered command names a handler this class does not have, so it dispatches to nothing",
        ADDON_SERVER_CORE,
        '        "sync_data_name": CommandSpec(),',
        '        "sync_data_name_typo": CommandSpec(),',
        (f"{REGT}::test_every_registered_command_resolves_to_a_handler",),
    ),
    Revert(
        "registry: an enabled provider's rows are dropped from the built table, so they classify nothing",
        ADDON_SERVER_CORE,
        "itertools.chain(_UNGATED_COMMAND_NAMES, *enabled_names)",
        "_UNGATED_COMMAND_NAMES",
        (f"{REGT}::test_the_whole_dispatch_table_comes_from_the_registry",),
    ),
    Revert(
        "registry: the provider gate stops gating, so a disabled integration's commands are advertised anyway",
        ADDON_SERVER_CORE,
        "zip(_PROVIDER_SCENE_FLAGS, gate, strict=True) if on",
        "zip(_PROVIDER_SCENE_FLAGS, gate, strict=True) if True",
        (f"{REGT}::test_a_disabled_provider_withholds_exactly_its_own_commands",),
    ),
    Revert(
        "registry: a second, name-keyed classification table comes back beside the registry",
        ADDON_SERVER_CORE,
        "_UNCLASSIFIED = CommandSpec()",
        "_UNCLASSIFIED = CommandSpec()\n_READ_ONLY_COMMANDS = frozenset()",
        (f"{REGT}::test_no_classification_set_survives_outside_the_registry",),
    ),
    Revert(
        "registry: an unregistered command name is answered read-only, so a client's typo skips the transaction",
        ADDON_SERVER_CORE,
        "        return COMMANDS.get(cmd_type, _UNCLASSIFIED) if isinstance(cmd_type, str) else _UNCLASSIFIED",
        "        return COMMANDS.get(cmd_type, CommandSpec(read_only=True)) if isinstance(cmd_type, str) "
        "else _UNCLASSIFIED",
        (f"{REGT}::test_an_unregistered_command_is_classified_as_an_ordinary_mutation",),
    ),
    Revert(
        "registry: ping is classified as a mutation, so a liveness check opens a transaction",
        ADDON_SERVER_CORE,
        '        "ping": CommandSpec(read_only=True),',
        '        "ping": CommandSpec(),',
        (f"{REGT}::test_ping_answers_read_only_without_a_live_blender",),
    ),
    # --- rollback protection is decided by parameter naming, and `target_names` is that decision ---
    Revert(
        "registry: the scalar object-name params are not read, so a named target gets no state captured",
        ADDON_SERVER_CORE,
        "    for key in _TARGET_NAME_PARAMS:",
        "    for key in ():",
        (
            f"{REGT}::test_target_names_reads_the_naming_convention[scalar-key]",
            f"{REGT}::test_target_names_reads_the_naming_convention[duplicates-collapse]",
            f"{REGT}::test_target_names_reads_the_naming_convention[order-follows-the-table-not-the-params]",
        ),
    ),
    Revert(
        "registry: the list-valued object-name params are not read, so a multi-target edit rolls back nothing",
        ADDON_SERVER_CORE,
        "    for key in _TARGET_NAMES_PARAMS:",
        "    for key in ():",
        (
            f"{REGT}::test_target_names_reads_the_naming_convention[list-key]",
            f"{REGT}::test_target_names_reads_the_naming_convention[non-strings-in-a-list-are-skipped]",
        ),
    ),
    Revert(
        "registry: the nested record params are not walked, so a per-record target is unprotected",
        ADDON_SERVER_CORE,
        "    for container_key, name_keys in _TARGET_RECORD_PARAMS:",
        "    for container_key, name_keys in ():",
        (
            f"{REGT}::test_target_names_reads_the_naming_convention[records-in-a-list]",
            f"{REGT}::test_target_names_reads_the_naming_convention[two-name-keys-in-one-record]",
            f"{REGT}::test_target_names_reads_the_naming_convention[a-lone-record]",
            f"{REGT}::test_target_names_reads_the_naming_convention[rigid-body-record-keys]",
        ),
    ),
    Revert(
        "registry: target names stop being deduplicated, so one object is snapshotted and restored twice",
        ADDON_SERVER_CORE,
        "    return list(dict.fromkeys(names))",
        "    return names",
        (f"{REGT}::test_target_names_reads_the_naming_convention[duplicates-collapse]",),
    ),
    Revert(
        "registry: `name` joins the target params, so create_primitive captures the object it is about to make",
        ADDON_SERVER_CORE,
        '_TARGET_NAME_PARAMS: tuple[str, ...] = (\n    "object_name",',
        '_TARGET_NAME_PARAMS: tuple[str, ...] = (\n    "name",\n    "object_name",',
        (f"{REGT}::test_target_names_reads_the_naming_convention[name-is-a-new-object-not-a-target]",),
    ),
    Revert(
        "registry: a list's elements are taken untyped, so a malformed params dict reaches find_object",
        ADDON_SERVER_CORE,
        "names.extend(name for name in value if isinstance(name, str))",
        "names.extend(value)",
        (f"{REGT}::test_target_names_reads_the_naming_convention[non-strings-in-a-list-are-skipped]",),
    ),
    Revert(
        "registry: a scalar param is taken untyped, so a number where a name belongs is treated as a target",
        ADDON_SERVER_CORE,
        "        value = params.get(key)\n        if isinstance(value, str):\n            names.append(value)",
        "        value = params.get(key)\n        if value is not None:\n            names.append(value)",
        (f"{REGT}::test_target_names_reads_the_naming_convention[a-non-string-scalar-is-not-a-name]",),
    ),
    # --- the provenance ledger a saved .blend carries ---
    Revert(
        "authored: the ledger reports newest first, so the saved file's provenance order is a lie",
        ADDON_AUTHORED,
        'return [{"collection": collection, "name": name} for collection, name in _LEDGER.entries]',
        'return [{"collection": collection, "name": name} for collection, name in reversed(_LEDGER.entries)]',
        (f"{AUTHT}::test_records_arrive_oldest_first_and_a_repeat_is_one_datablock",),
    ),
    Revert(
        "authored: the ledger stops evicting, so a long session grows unbounded and its freed names stay suppressed",
        ADDON_AUTHORED,
        "        if len(_LEDGER.entries) >= MAX_TRACKED_AUTHORED:",
        "        if False:",
        (
            f"{AUTHT}::test_overflow_drops_the_oldest_and_stops_claiming_a_complete_history",
            f"{AUTHT}::test_a_name_that_was_evicted_can_be_recorded_again",
        ),
    ),
    Revert(
        "authored: clear() forgets the entries but keeps the truncation claim, describing a database that is gone",
        ADDON_AUTHORED,
        "    _LEDGER.truncated = False",
        "    pass  # truncation claim left standing",
        (f"{AUTHT}::test_clearing_forgets_the_entries_and_the_truncation_claim",),
    ),
    # --- artefact truth: which render properties the schemas reach ---
    Revert(
        "render coverage: reachability is judged by identifier, ignoring which owner carries it",
        RENDER_COVERAGE_SCRIPT,
        '    reached = sorted(f"{owner}:{name}" for owner, name in probed & set(reachable))',
        "    reached = sorted(\n"
        '        f"{owner}:{name}" for owner, name in probed if name in {name for _owner, name in reachable}\n'
        "    )",
        (f"{RCT}::test_coverage_classifies_the_same_identifier_per_owner",),
    ),
    Revert(
        "render coverage: an excluded property is reported as an unreachable gap",
        RENDER_COVERAGE_SCRIPT,
        '    unreachable = sorted(f"{owner}:{name}" for owner, name in probed - set(reachable) - set(excluded))',
        '    unreachable = sorted(f"{owner}:{name}" for owner, name in probed - set(reachable))',
        (f"{RCT}::test_coverage_never_reports_an_excluded_property_as_unreachable",),
    ),
    Revert(
        "render coverage: an absent owner's placeholder is read as a property name",
        RENDER_COVERAGE_SCRIPT,
        '        if identifier == "(absent)":\n            continue',
        "        if False:\n            continue",
        (f"{RCT}::test_coverage_ignores_an_absent_owner",),
    ),
    Revert(
        "render coverage: the routing table's flat render properties are never counted as reachable",
        RENDER_COVERAGE_SCRIPT,
        '    reachable = {("scene.render", name) for name in table.RENDER_PROPERTIES}',
        "    reachable = set()",
        (f"{RCT}::test_the_recorded_baseline_and_the_shipped_routes_agree_on_the_reachable_set",),
    ),
    Revert(
        "posing: the bone filter is ignored, so naming three bones still reads the whole rig",
        ADDON_CR_FOUNDATION,
        "    if bone_names is None:\n        return bones",
        "    if True:\n        return bones",
        (
            f"{POSET}::test_named_bones_are_returned_in_one_page_instead_of_paged_to",
            f"{POSET}::test_an_unknown_bone_name_is_refused_rather_than_silently_dropped",
            f"{CRFT}::test_selected_bones_returns_named_bones_in_armature_order_not_request_order",
        ),
    ),
    Revert(
        "posing: a bone the rig does not have is dropped from the page instead of refused",
        ADDON_CR_FOUNDATION,
        '    if missing:\n        raise ValueError(f"Bones not found in armature',
        '    if False:\n        raise ValueError(f"Bones not found in armature',
        (
            f"{POSET}::test_an_unknown_bone_name_is_refused_rather_than_silently_dropped",
            f"{CRFT}::test_selected_bones_refuses_an_unknown_name",
        ),
    ),
    Revert(
        "posing: the bone filter accepts a shape that is not a list of names",
        ADDON_CR_FOUNDATION,
        "    if not isinstance(bone_names, list) or not 1 <= len(bone_names) <= _MAX_BONE_PAGE:",
        "    if False:",
        (
            f"{POSET}::test_a_malformed_bone_name_filter_is_refused[value0]",
            f"{POSET}::test_a_malformed_bone_name_filter_is_refused[CHAR1_head_jnt]",
        ),
    ),
    Revert(
        "posing: a blank or non-string bone name passes the filter",
        ADDON_CR_FOUNDATION,
        "        if not isinstance(name, str) or not name.strip():",
        "        if False:",
        (
            f"{POSET}::test_a_malformed_bone_name_filter_is_refused[value1]",
            f"{POSET}::test_a_malformed_bone_name_filter_is_refused[value2]",
        ),
    ),
    Revert(
        "posing: the list is narrowed even when no filter was asked for",
        ADDON_CR_FOUNDATION,
        "    if bone_names is None:\n        return bones",
        "    if bone_names is None:\n        return bones[:1]",
        (
            f"{POSET}::test_no_filter_still_lists_every_bone",
            f"{CRFT}::test_selected_bones_returns_every_bone_in_armature_order_when_unfiltered",
        ),
    ),
    # --- the preflight parameter gate: what the addon accepts, not just what it names ---
    Revert(
        "handshake: a command's accepted-keyword list is reported unsorted",
        ADDON_CAPABILITY_INTROSPECTION,
        "            result[name] = sorted(parameter.name for parameter in parameters",
        "            result[name] = list(parameter.name for parameter in reversed(list(parameters))",
        (f"{CAPT}::test_a_handler_with_named_parameters_reports_them_sorted",),
    ),
    Revert(
        "handshake: a **kwargs handler is reported as accepting a fixed name list",
        ADDON_CAPABILITY_INTROSPECTION,
        "        if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters):",
        "        if False:",
        (f"{CAPT}::test_a_handler_taking_kwargs_reports_the_wildcard_sentinel",),
    ),
    Revert(
        "handshake: a positional-only parameter is published as an accepted keyword",
        ADDON_CAPABILITY_INTROSPECTION,
        "if parameter.kind in _KEYWORD_KINDS)",
        "if parameter.kind is not inspect.Parameter.VAR_POSITIONAL)",
        (f"{CAPT}::test_a_positional_only_parameter_is_not_reported_as_an_accepted_keyword",),
    ),
    Revert(
        "handshake: an unreadable handler signature crashes the handshake instead of reporting '*'",
        ADDON_CAPABILITY_INTROSPECTION,
        "        except (TypeError, ValueError):",
        "        except NotImplementedError:",
        (f"{CAPT}::test_a_value_that_is_not_callable_reports_the_wildcard_sentinel_instead_of_raising",),
    ),
    Revert(
        "handshake: a hostile command name is published cleaned instead of dropped",
        ADDON_MANAGER,
        "        if command is None or not _is_structurally_intact(raw_command, command):",
        "        if command is None:",
        (f"{AMT}::test_capability_params_drops_a_command_name_that_only_matches_once_cleaned",),
    ),
    Revert(
        "handshake: parameter names inside the list are published unsanitised",
        ADDON_MANAGER,
        "        cleaned[command] = normalized_session_text_list(params, max_chars=_MAX_CAPABILITY_PARAM_NAME_CHARS)[",
        "        cleaned[command] = list(params)[",
        (f"{AMT}::test_capability_params_drops_a_hostile_parameter_name_inside_the_list",),
    ),
    Revert(
        "transport: the gate accepts a parameter the installed addon's handler does not",
        SERVER_CONNECTION,
        "            if isinstance(accepted, list):",
        "            if isinstance(accepted, dict):",
        (f"{CONNT}::test_the_command_gate_refuses_a_parameter_the_addon_predates",),
    ),
    Revert(
        "transport control: the gate filters against a command it was told accepts anything",
        SERVER_CONNECTION,
        "            if isinstance(accepted, list):",
        "            if accepted is not None:",
        (f"{CONNT}::test_the_command_gate_never_filters_a_command_marked_as_accepting_anything",),
    ),
    Revert(
        "transport control: an addon predating capability_params is gated as accepting nothing",
        SERVER_CONNECTION,
        "            accepted = handshake.capability_params.get(command_type)",
        "            accepted = handshake.capability_params.get(command_type, [])",
        (f"{CONNT}::test_the_command_gate_does_not_filter_when_the_addon_omits_capability_params",),
    ),
    # --- the SDK's generated argument models refuse a key the handler never declared ---
    Revert(
        # The call site rather than the function body, because that is where the mechanism is:
        # `_strict_args` rewrites models that already exist, so it only ever hardens the tools
        # registered by the import loop above it. This is also how the fix was falsified while it
        # was being written - neutralise the call and four of the file's six nodes go red.
        "strict args: the hardening pass never runs, so an unknown top-level argument is dropped again",
        SERVER_TOOLS_INIT,
        "\nforbid_unknown_tool_arguments(mcp)\n",
        "\nif False:\n    forbid_unknown_tool_arguments(mcp)\n",
        (
            f"{STRICTT}::test_a_misspelled_argument_is_refused_instead_of_dropped",
            f"{STRICTT}::test_the_wrongly_nested_patch_from_the_incident_is_refused",
            f"{STRICTT}::test_every_registered_tool_refuses_an_unknown_argument[all]",
            f"{STRICTT}::test_every_registered_tool_refuses_an_unknown_argument[None]",
        ),
    ),
    # --- a dispatch-surface change cannot reach a user behind an unchanged protocol number ---
    Revert(
        # The anchor carries the current number, so `just anchors` reports this row BROKEN on the
        # next protocol bump. That is the cheapest possible reminder that the row's claim - the
        # surface moved and the number did not - has to be re-pointed at the new pair.
        "addon surface: the dispatch table moved while the protocol number stayed where it was",
        ADDON_MANAGER,
        "EXPECTED_ADDON_PROTOCOL_VERSION = 34",
        "EXPECTED_ADDON_PROTOCOL_VERSION = 33",
        (
            f"{SURFT}::test_snapshot_records_the_protocol_version_the_server_expects",
            f"{SURFT}::test_both_protocol_constants_agree",
        ),
    ),
    Revert(
        # `scripts/update_addon_surface.py` imports this helper, so reverting it really does revert
        # what `just addon-surface` writes; the node compares the committed bytes against it.
        "addon surface: the snapshot is written in dispatch order, so one new command reflows the file",
        TEST_ADDON_SURFACE_FILE,
        '    return json.dumps(surface, indent=2, sort_keys=True) + "\\n"',
        '    return json.dumps(surface, indent=2) + "\\n"',
        (f"{SURFT}::test_committed_surface_is_serialized_the_way_the_generator_writes_it",),
    ),
    # --- one keyframe-style vocabulary, stated twice and enforced once -----------------------
    Revert(
        # The add-on cannot import the server package, so the enum members are necessarily
        # written down twice. A member added to one side only is not a type error and not a
        # formatting error - it is a schema offering a mode the socket's far end refuses.
        "key style: the add-on drifts back to the three interpolations the surface used to carry",
        ADDON_KEY_STYLE,
        '        "CONSTANT",\n        "LINEAR",\n        "BEZIER",\n        "SINE",\n',
        '        "CONSTANT",\n        "LINEAR",\n        "BEZIER",\n',
        (f"{KEYSTYLET}::test_the_advertised_vocabulary_is_the_one_the_addon_accepts[Literal-INTERPOLATIONS]",),
    ),
    Revert(
        "key style: handle types are written under every interpolation, not only BEZIER",
        ADDON_KEY_STYLE,
        '    if style.interpolation == "BEZIER":\n        point.handle_left_type = style.handle_left\n',
        "    if True:\n        point.handle_left_type = style.handle_left\n",
        (f"{KEYSTYLET}::test_bezier_is_the_only_interpolation_that_records_handle_types",),
    ),
    Revert(
        # Easing is the half of the vocabulary that makes SINE..ELASTIC mean anything; dropped
        # silently, a call asking for EASE_IN_OUT gets linear-feeling motion and no error.
        "key style: an easing request is accepted and then dropped",
        ADDON_KEY_STYLE,
        "    if style.easing is not None:\n        point.easing = style.easing\n",
        "    if False:\n        point.easing = style.easing\n",
        (f"{KEYSTYLET}::test_easing_is_written_on_any_interpolation_and_omitted_when_unset",),
    ),
    Revert(
        # The four style values arrive together in one `KeyStyle`, so "Unsupported key style"
        # leaves the caller to guess which of them it meant.
        "key style: a refusal no longer names the argument that was wrong",
        ADDON_KEY_STYLE,
        "            if value not in HANDLE_TYPES:\n"
        '                raise ValueError(f"Unsupported {label}: {value}; expected one of {sorted(HANDLE_TYPES)}")\n',
        '            if value not in HANDLE_TYPES:\n                raise ValueError("Unsupported key style")\n',
        (
            f"{KEYSTYLET}::test_an_unsupported_style_is_refused_by_the_argument_that_is_wrong[style1-handle_left]",
            f"{KEYSTYLET}::test_an_unsupported_style_is_refused_by_the_argument_that_is_wrong[style2-handle_right]",
        ),
    ),
    Revert(
        "key style: the add-on stops accepting a handle type the schema still advertises",
        ADDON_KEY_STYLE,
        'HANDLE_TYPES = frozenset({"FREE", "ALIGNED", "VECTOR", "AUTO", "AUTO_CLAMPED"})',
        'HANDLE_TYPES = frozenset({"ALIGNED", "VECTOR", "AUTO", "AUTO_CLAMPED"})',
        (f"{KEYSTYLET}::test_the_advertised_vocabulary_is_the_one_the_addon_accepts[Literal-HANDLE_TYPES]",),
    ),
    Revert(
        "key style: the add-on stops accepting an easing direction the schema still advertises",
        ADDON_KEY_STYLE,
        'EASINGS = frozenset({"AUTO", "EASE_IN", "EASE_OUT", "EASE_IN_OUT"})',
        'EASINGS = frozenset({"EASE_IN", "EASE_OUT", "EASE_IN_OUT"})',
        (f"{KEYSTYLET}::test_the_advertised_vocabulary_is_the_one_the_addon_accepts[Literal-EASINGS]",),
    ),
    Revert(
        "key style: the interpolation refusal no longer names interpolation",
        ADDON_KEY_STYLE,
        "            raise ValueError(\n"
        '                f"Unsupported interpolation: {self.interpolation}; expected one of {sorted(INTERPOLATIONS)}"\n'
        "            )\n",
        '            raise ValueError("Unsupported key style")\n',
        (f"{KEYSTYLET}::test_an_unsupported_style_is_refused_by_the_argument_that_is_wrong[style0-interpolation]",),
    ),
    Revert(
        "key style: the easing refusal no longer names easing",
        ADDON_KEY_STYLE,
        '            raise ValueError(f"Unsupported easing: {self.easing}; expected one of {sorted(EASINGS)}")\n',
        '            raise ValueError("Unsupported key style")\n',
        (f"{KEYSTYLET}::test_an_unsupported_style_is_refused_by_the_argument_that_is_wrong[style3-easing]",),
    ),
    # --- an ID holds one action, so assigning one is always also unassigning another ---
    Revert(
        "action assignment: an action holding keys is displaced without anyone being asked",
        ADDON_ACTION_ASSIGNMENT,
        "    if not confirm_displace:",
        "    if False:",
        (f"{CRTT}::test_keying_a_pose_refuses_to_displace_an_action_that_holds_keys",),
    ),
    Revert(
        # The opposite direction on the same line: a guard that cannot be confirmed past is not a
        # confirmation, it is a wall, and the node that says the caller may mean it is the one
        # that notices.
        "action assignment control: the confirmation is ignored, so a confirmed displacement is still refused",
        ADDON_ACTION_ASSIGNMENT,
        "    if not confirm_displace:",
        "    if True:",
        (f"{CRTT}::test_confirming_the_displacement_moves_the_rig_onto_the_new_action",),
    ),
    Revert(
        "action assignment: CREATE stops asserting that the action it names is not already in the file",
        ADDON_ACTION_ASSIGNMENT,
        '    elif policy == "CREATE":',
        "    elif False:",
        (f"{CRTT}::test_ensure_keys_into_an_existing_action_where_create_refuses_it",),
    ),
    Revert(
        "action assignment: one action is spread across every object a batch names",
        ADDON_OBJECT_ANIMATION,
        "    if len(objects) > 1:",
        "    if False:",
        (f"{OANIMT}::test_keyframe_object_transform_refuses_one_action_for_several_objects",),
    ),
    # --- place and aim in one call, and refuse a vantage point on the target ---
    Revert(
        "camera: camera_location never leaves the server, so a placed aim moves nothing",
        SERVER_CAMERA_TARGETING_TOOL,
        '            "camera_location": camera_location,',
        '            "camera_location": None,',
        (f"{CAMT}::test_point_camera_at_places_before_aiming_and_refuses_a_coincident_placement",),
    ),
    Revert(
        "camera: a placement on the aim point is dispatched instead of refused before the round trip",
        SERVER_CAMERA_TARGETING_TOOL,
        " and tuple(camera_location) == tuple(target_point):",
        " and False:",
        (f"{CAMT}::test_point_camera_at_places_before_aiming_and_refuses_a_coincident_placement",),
    ),
    Revert(
        # Reverting `_set_world_location` itself proves nothing - no node reads the placed transform -
        # but reverting the guard reaches it, because the node's fake camera raises the moment its
        # matrix_world is touched. That is the claim: the refusal happens before the camera moves.
        "camera: the handler moves the camera onto the aim point before noticing the two coincide",
        ADDON_CAMERA_TARGETING,
        "            if (point - placement).length_squared <= _COINCIDENT_DISTANCE_SQUARED:",
        "            if False:",
        (f"{CAMT}::test_handler_point_camera_at_rejects_a_placement_on_the_aim_point",),
    ),
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
