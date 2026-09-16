"""
Prove every test added by Phase 2 fails once the thing it names is broken.

Task 1 built this and owns most of its rows; later tasks add rows for the nodes
they add to the files listed in NEW_TEST_FILES, because the coverage check below
reports an uncovered node as a gap rather than ignoring it.

**Why this exists in the repository rather than in a session scratchpad.** The
phase's rubric makes "a test that still passes with its fix reverted" an
automatic critical failure, and four such tests were found in this task alone.
Three of the four hid the same way: the evidence was gathered per *file* rather
than per *test node*, so a sibling assertion failing in the same file read as
"the revert was caught". Every row below therefore names the exact node ids it
expects to fail, and only those nodes are run.

**How to run it:**

    .venv/bin/python scripts/revert_matrix.py           # run the whole matrix
    .venv/bin/python scripts/revert_matrix.py --list    # print the coverage map
    .venv/bin/python scripts/revert_matrix.py --only R2 # rows whose label matches

Each row edits one file in place, runs only its own node ids, and restores the
file in a `finally`. It must be run on a clean tree: a crash between the edit
and the restore leaves a modified working copy, so check `git status` after.
It edits `scripts/blender_rig.py`, the addon, the Docker files and
`pyproject.toml`, so nothing else may be running against the checkout at the
time.

**Running it from a copy of the repository silently credits itself.** A first
matrix run in a plain `cp -a` copy produced **39 false survivors**: `.venv` is
an editable install whose `.pth` file points at the *original* `src`, so every
revert this harness wrote under `src/blender_mcp/server/` or
`addon_manager.py` was written to the copy and imported from the original. The
reverted code was never executed, the tests passed, and each row was recorded
as "the fix is not falsifiable". Set `PYTHONPATH` to the copy's own `src`,
which precedes `.pth` entries on `sys.path`::

    PYTHONPATH=<copy>/src .venv/bin/python scripts/revert_matrix.py

This is the same class of false credit as the `__pycache__` staleness bug this
harness already guards against: the harness measures something real, just not
the thing it names. Running from the repository root needs no `PYTHONPATH`.

**Coverage is checked, not assumed.** `--list` (and every run) compares the
nodes named here against the nodes pytest actually collects in the five test
files this task added, plus the two tests it added to
`tests/test_addon_manager.py`. A node that is neither covered by a row nor
listed in `NOT_INDIVIDUALLY_FALSIFIABLE`, with a reason, is reported as a gap.
"""

import argparse
import pathlib
import re
import subprocess
import sys

from dataclasses import dataclass, field

# `scripts/` is sys.path[0] when this is run as `python scripts/revert_matrix.py`, which is
# the only supported way to run it (see the PYTHONPATH note above). Nothing imports this
# module, so a plain import is safe here; a consumer loaded by path uses
# `quiet_box.load_quiet_box` instead.
import quiet_box

ROOT = pathlib.Path(__file__).resolve().parents[1]
RIG = ROOT / "scripts/blender_rig.py"
ADDON_MANAGER = ROOT / "src/blender_mcp/addon_manager.py"
ADDON_OUTPUT_ROOTS = ROOT / "src/blender_mcp/bundled/addon/output_roots.py"
ADDON_SERVER_CORE = ROOT / "src/blender_mcp/bundled/addon/server_core.py"
SERVER_CORE_TOOL = ROOT / "src/blender_mcp/server/tools/core.py"
SERVER_CONNECTION = ROOT / "src/blender_mcp/server/connection.py"
ADDON_SESSION = ROOT / "src/blender_mcp/bundled/addon/session.py"
ADDON_TEXT_HYGIENE = ROOT / "src/blender_mcp/bundled/addon/text_hygiene.py"
SERVER_TEXT_HYGIENE = ROOT / "src/blender_mcp/text_hygiene.py"
ADDON_FILE_LIFECYCLE = ROOT / "src/blender_mcp/bundled/addon/handlers/file_lifecycle.py"
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

# The two parametrized node-id prefixes Task 3 cycle 3 added. Named because
# the ids carry the parameter text verbatim and the rows would otherwise be
# unreadably long lines.
RIGT = "tests/test_blender_rig.py"
DOCKT = "tests/test_docker_rig.py"
ROOTST = "tests/test_output_roots.py"
CORET = "tests/server/tools/test_core.py"
CLIT = "tests/server/test_cli_transport.py"
AMT = "tests/test_addon_manager.py"
# Named because the node id plus its parameter is one character past the line
# limit inline, and splitting the f-string is what `ruff format` joins back.
_LIST_SCALAR = "test_a_string_where_a_list_belongs_is_not_iterated_character_by_character"
SESSIONT = "tests/test_session_state.py"
QBT = "tests/test_quiet_box.py"
THREADT = "tests/server/test_threading.py"
CONNT = "tests/server/test_connection_framing.py"
HOSTILE_LIB = f"{SESSIONT}::test_a_hostile_library_path_is_reduced_the_same_way_a_failure_note_is"
# The `name` half of the same table, parametrized with short ids on purpose:
# a row has to name the nodes it expects to fail, and the `filepath` half's
# 500-character ids are unreadable in one.
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
# Named because the full node id is one character past the line limit inline.
NFKC_BACKSLASH_LIB = (
    f"{HOSTILE_LIB}[nfkc-backslash (U+FE68)-//..\\ufe68..\\ufe68clients\\ufe68acme\\ufe68canon.blend-forbidden8]"
)

# The files added outright by a Phase 2 task; every node they collect must be
# accounted for. Task 1 added the first five; Task 3 added `test_session_state.py`.
NEW_TEST_FILES = (RIGT, DOCKT, ROOTST, CORET, CLIT, SESSIONT, QBT)
# Nodes added to files that already existed. **Not optional bookkeeping:**
# `coverage_gaps()` subtracts the rows below from *this* universe, so a task that
# adds nodes here without listing them gets a "0 uncovered" that is true of the
# files the matrix knows about and vacuous as a claim about the task - which is
# precisely the blind spot this harness was built to prevent. Task 1 added the
# first two; the rest are Task 3's.
NEW_NODES_IN_EXISTING_FILES = (
    f"{AMT}::test_handshake_surfaces_writable_output_roots",
    f"{AMT}::test_handshake_defaults_writable_output_roots_when_the_addon_omits_them",
    # --- Task 3: the handshake carries the session ---
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
    # --- Task 3 cycle 5: the triaged repair round ---
    f"{THREADT}::test_rejecting_a_full_queue_to_distinct_stalled_peers_is_bounded",
    f"{THREADT}::test_an_abort_during_the_pre_swap_drain_answers_the_swaps_own_client",
    f"{THREADT}::test_an_abort_before_the_swap_is_dispatched_does_not_claim_the_database_is_half_replaced",
    f"{THREADT}::test_a_failed_load_then_an_abort_does_not_claim_a_known_clean_database_is_half_replaced",
    f"{THREADT}::test_a_second_identical_failure_then_an_abort_is_still_not_indeterminate",
    f"{CONNT}::test_a_refresh_that_learns_a_newer_session_than_the_one_observed_stops_retrying",
    # --- Task 3: the barrier, the stamp, and the liveness bounds ---
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
    # --- Task 3: the epoch made actionable on the client side ---
    f"{CONNT}::test_an_unchanged_session_marker_does_not_invalidate_the_cached_handshake",
    f"{CONNT}::test_a_moved_epoch_marks_the_cached_handshake_stale",
    f"{CONNT}::test_a_restarted_addon_at_the_same_epoch_still_marks_the_handshake_stale",
    f"{CONNT}::test_the_marker_is_read_from_a_command_result_as_well_as_the_frame",
    f"{CONNT}::test_a_response_carrying_no_marker_changes_nothing",
    f"{CONNT}::test_the_command_gate_reads_the_refreshed_capability_set",
    f"{CONNT}::test_a_barrier_rejection_read_off_the_socket_marks_the_handshake_stale",
    # --- Task 3 cycle 3: liveness, the fail-closed stamp, and the abort path ---
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
    # --- Task 3 cycle 3: the client-side reaction, made non-amplifying ---
    f"{CONNT}::test_an_ordinary_commands_result_cannot_trip_a_re_handshake",
    f"{CONNT}::test_a_non_conforming_epoch_does_not_re_arm_the_flag_forever",
    f"{CONNT}::test_one_swap_costs_exactly_one_re_handshake_over_a_real_round_trip",
    f"{CONNT}::test_a_refresh_that_fails_leaves_the_staleness_signal_standing",
    # --- Task 3 structural pass: the server boundary, the latch, the send floor ---
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
    # --- Task 3 gate round: the whole handshake constructor, and load_pre -----
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
)

# Nodes no single revert can break on their own, with the reason. Keeping these
# named is the point: an omission reads as coverage, which is the defect this
# harness was extended to stop.
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

NOT_INDIVIDUALLY_FALSIFIABLE: dict[str, str] = {
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
    # These three characterise code this repository does not own, which is the
    # point of them: they are what make the security claims in `cli.py`'s
    # docstrings falsifiable instead of asserted. No edit to `cli.py` can move
    # them, so a revert row would be theatre. Recorded here with a reason
    # rather than left as a silent gap.
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
        label: Human-readable description of what is being undone.
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


# Appended by the library-name revert. The production import of
# `client_safe_text` was removed when `name` was routed through
# `client_safe_leaf`, so reverting the call site alone would raise `NameError`
# and the row would be credited for the wrong reason - the defect class
# `check_revert_anchors.py` exists to catch. This restores the reverted
# behaviour without restoring the import statement, which a single-anchor row
# cannot reach.
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

REVERTS: list[Revert] = [
    # --- which Blender, and which configuration, the rig launches ---
    Revert(
        "A: --factory-startup dropped from the launch",
        RIG,
        '    command = [str(_BLENDER), "--factory-startup", "--python", str(bootstrap)]',
        '    command = [str(_BLENDER), "--python", str(bootstrap)]',
        (f"{RIGT}::test_blender_is_launched_with_factory_startup",),
    ),
    Revert(
        "R3: only BLENDER_USER_SCRIPTS is set",
        RIG,
        '        "BLENDER_USER_RESOURCES": str(work_dir),\n',
        "",
        (f"{RIGT}::test_both_blender_user_resource_roots_point_at_the_work_dir",),
    ),
    Revert(
        "R5: the launched Blender's output roots left unscoped",
        RIG,
        '        "BLENDERMCP_OUTPUT_ROOTS": str(work_dir),\n',
        "",
        (f"{RIGT}::test_the_launched_blender_is_pointed_at_the_work_dir_first",),
    ),
    Revert(
        "B2: TMPDIR left at the user's own, so bpy.app.tempdir escapes the work dir",
        RIG,
        '        "TMPDIR": str(work_dir / "tmp"),\n',
        "",
        (f"{RIGT}::test_blender_s_session_temp_dir_is_redirected_under_the_work_dir",),
    ),
    Revert(
        "R8: PYTHONPATH no longer pruned from the child environment",
        RIG,
        '_PRUNED_ENVIRONMENT_NAMES = frozenset({"PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"})',
        '_PRUNED_ENVIRONMENT_NAMES = frozenset({"PYTHONHOME", "PYTHONSTARTUP"})',
        (f"{RIGT}::test_an_inherited_pythonpath_cannot_shadow_the_staged_addon",),
    ),
    Revert(
        "C2: the prune narrowed back to BLENDER_USER_, so BLENDER_SYSTEM_* survives",
        RIG,
        '_PRUNED_ENVIRONMENT_PREFIXES = ("BLENDER",)\n'
        '_PRUNED_ENVIRONMENT_NAMES = frozenset({"PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"})',
        '_PRUNED_ENVIRONMENT_PREFIXES = ("BLENDER_USER_", "BLENDERMCP_")\n'
        '_PRUNED_ENVIRONMENT_NAMES = frozenset({"PYTHONPATH"})',
        (f"{RIGT}::test_blender_system_and_python_home_variables_cannot_redirect_the_child",),
    ),
    # --- which process answers the port ---
    Revert(
        "R1b: the preflight no longer refuses an occupied port",
        RIG,
        '    try:\n        with socket.create_connection(("127.0.0.1", port), timeout=1.0):\n'
        "            pass\n    except OSError:\n        return\n    raise RigError(",
        "    if True:\n        return\n    raise RigError(",
        (f"{RIGT}::test_the_rig_refuses_a_port_something_is_already_listening_on",),
    ),
    Revert(
        "R1b control: the preflight refuses every port",
        RIG,
        '    try:\n        with socket.create_connection(("127.0.0.1", port), timeout=1.0):\n'
        "            pass\n    except OSError:\n        return\n    raise RigError(",
        "    raise RigError(",
        (f"{RIGT}::test_a_free_port_passes_the_preflight",),
    ),
    Revert(
        "R1a: the default port back to the addon's own 9876",
        RIG,
        "_DEFAULT_PORT = 0\n",
        "_DEFAULT_PORT = 9876\n",
        (f"{RIGT}::test_the_default_port_is_an_unused_ephemeral_port",),
    ),
    Revert(
        "R1a control: _choose_port ignores an explicit --port",
        RIG,
        "    if requested:\n        return requested\n",
        "    if False:\n        return requested\n",
        (f"{RIGT}::test_an_explicit_port_is_honoured",),
    ),
    Revert(
        "C4: the port is not re-checked immediately before Popen",
        RIG,
        "    _require_port_free(port)\n    return subprocess.Popen(",
        "    return subprocess.Popen(",
        (f"{RIGT}::test_the_port_is_rechecked_immediately_before_blender_is_started",),
    ),
    Revert(
        "R1c: readiness back to a bare connect",
        RIG,
        "        if _receipt_matches(receipt, launch.nonce, launch.port) and _ping_answers(launch.port):",
        "        if _receipt_matches(receipt, launch.nonce, launch.port) and _bare_connect(launch.port):",
        (f"{RIGT}::test_readiness_requires_a_ping_round_trip_not_a_bare_connect",),
        also=BARE_CONNECT,
    ),
    Revert(
        "R1d: the receipt is not checked at all",
        RIG,
        '    try:\n        written = json.loads(receipt.read_text(encoding="utf-8"))\n'
        "    except (OSError, ValueError):\n        return False\n"
        '    return isinstance(written, dict) and written.get("nonce") == nonce and written.get("port") == port',
        "    return True",
        (f"{RIGT}::test_the_readiness_receipt_must_carry_the_rig_s_own_nonce_and_port",),
    ),
    Revert(
        "C4: the receipt's port is recorded but never checked",
        RIG,
        ' and written.get("port") == port',
        "",
        (f"{RIGT}::test_the_readiness_receipt_must_carry_the_rig_s_own_nonce_and_port",),
    ),
    Revert(
        "C4: a swallowed bind failure is left buried in the log tail",
        RIG,
        '    if _BIND_FAILURE_MARKER not in text:\n        return ""',
        '    if True:\n        return ""',
        (f"{RIGT}::test_a_startup_failure_names_a_bind_failure_the_addon_swallowed",),
    ),
    Revert(
        "A5: the log tail is formatted before the reader is joined",
        RIG,
        "            launch.drain.join(_SHUTDOWN_GRACE_SECONDS)\n            raise RigError(",
        "            raise RigError(",
        (f"{RIGT}::test_the_log_reader_is_joined_before_a_failure_quotes_its_log",),
    ),
    # --- what the rig may delete and overwrite ---
    Revert(
        "B3/B5: any --work-dir is claimed, marker or not",
        RIG,
        "        if existing:\n            raise RigError(_foreign_work_dir_message(work_dir, existing))",
        "        if False:\n            raise RigError(_foreign_work_dir_message(work_dir, existing))",
        (f"{RIGT}::test_a_populated_work_dir_the_rig_did_not_create_is_refused",),
    ),
    Revert(
        "B5: a real Blender resources root is no longer recognised as one",
        RIG,
        '_BLENDER_RESOURCE_ENTRIES = ("config", "datafiles", "extensions", "scripts", "userpref.blend")',
        "_BLENDER_RESOURCE_ENTRIES = ()",
        (f"{RIGT}::test_a_work_dir_that_is_a_real_blender_resources_root_is_refused",),
    ),
    Revert(
        "C1: an auto-executing startup/ tree is no longer recognised",
        RIG,
        '_AUTO_EXECUTED_SCRIPT_DIRS = ("startup", "modules")',
        "_AUTO_EXECUTED_SCRIPT_DIRS = ()",
        (f"{RIGT}::test_a_work_dir_holding_auto_executed_scripts_is_refused",),
    ),
    Revert(
        "B3 control: the work dir is never marked, so the rig refuses its own",
        RIG,
        '    marker.write_text(_OWNED_MARKER_TEXT, encoding="utf-8")\n\n\ndef _foreign_work_dir_message',
        "\n\ndef _foreign_work_dir_message",
        (f"{RIGT}::test_an_empty_or_rig_created_work_dir_is_claimed",),
    ),
    Revert(
        "R4: the marker no longer guards a directory the rig did not create",
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
        "C3/F10: <work-dir>/addons as a regular file is no longer refused",
        RIG,
        "    if directory.exists() and not directory.is_dir():\n"
        '        raise RigError(f"{directory} exists and is not a directory; the rig will not replace it.")\n',
        "",
        (f"{RIGT}::test_an_addons_path_that_is_a_regular_file_is_refused",),
    ),
    Revert(
        "R4 control: staging merges into its own previous stage instead of rebuilding it",
        RIG,
        "    if staged.exists():\n        shutil.rmtree(staged)\n",
        "",
        (f"{RIGT}::test_staging_replaces_its_own_previous_stage",),
    ),
    Revert(
        "B4: the whole addons/ tree is removed again, not just blender_mcp",
        RIG,
        "    if staged.exists():\n        shutil.rmtree(staged)\n",
        "    if addons.exists():\n        shutil.rmtree(addons)\n    addons.mkdir(parents=True)\n",
        (f"{RIGT}::test_staging_leaves_other_add_ons_in_its_own_directory_alone",),
    ),
    Revert(
        "R4: a symlinked addons/ directory is no longer refused",
        RIG,
        "    if directory.is_symlink():\n"
        '        raise RigError(f"{directory} is a symlink; refusing to write through it. '
        'Use a --work-dir the rig owns.")\n',
        "",
        (f"{RIGT}::test_staging_refuses_a_symlinked_addons_directory",),
    ),
    Revert(
        "R6: fixtures handed over by their original path",
        RIG,
        "        destination.unlink(missing_ok=True)\n        shutil.copy2(source, destination)\n"
        "        copies[name] = destination",
        "        copies[name] = source",
        (f"{RIGT}::test_fixtures_are_copied_so_a_scenario_cannot_write_through_to_the_original",),
    ),
    Revert(
        "B3: blends/ is created without the rig's marker, so a pre-placed file is overwritten",
        RIG,
        '    staged_dir = _rig_owned_subdirectory(work_dir, "blends")',
        '    staged_dir = work_dir / "blends"\n    staged_dir.mkdir(parents=True, exist_ok=True)',
        (f"{RIGT}::test_a_pre_placed_fixture_is_not_silently_overwritten",),
    ),
    Revert(
        "B3 control: every existing fixture destination is refused, reused work dir or not",
        RIG,
        "        destination.unlink(missing_ok=True)\n",
        '        if destination.exists():\n            raise RigError("refusing any existing destination")\n',
        (f"{RIGT}::test_the_rig_replaces_a_fixture_copy_it_made_itself",),
    ),
    Revert(
        "B3: copy2 follows a symlinked fixture destination again",
        RIG,
        "        if destination.is_symlink():\n"
        '            raise RigError(f"{destination} is a symlink; refusing to write through it.")\n',
        "",
        (f"{RIGT}::test_a_symlinked_fixture_destination_is_not_written_through",),
    ),
    Revert(
        "B3: two --blend fixtures may share one name again",
        RIG,
        "        if name in copies:\n"
        '            raise RigError(f"--blend {name} was given twice; the two copies would overwrite each other.")\n',
        "",
        (f"{RIGT}::test_two_fixtures_with_the_same_name_are_refused",),
    ),
    Revert(
        "R6: fixture names unvalidated, so one can escape the work dir",
        RIG,
        "    if not _FIXTURE_NAME.match(name):",
        "    if False:",
        (f"{RIGT}::test_a_fixture_name_cannot_escape_the_work_dir",),
    ),
    Revert(
        "F4: importing the scenario writes __pycache__ beside the caller's file again",
        RIG,
        "    sys.dont_write_bytecode = True\n    spec = importlib_util.spec_from_file_location",
        "    spec = importlib_util.spec_from_file_location",
        (f"{RIGT}::test_importing_a_scenario_leaves_no_pycache_beside_the_caller_s_file",),
    ),
    # --- liveness of the rig itself ---
    Revert(
        "R7: no deadline on the scenario",
        RIG,
        "    if thread.is_alive():\n        abandoned.set()",
        "    if False:\n        abandoned.set()",
        (f"{RIGT}::test_a_scenario_that_never_returns_is_abandoned_at_its_deadline",),
    ),
    Revert(
        "R7 control: the scenario's own failure is swallowed",
        RIG,
        "    if raised:\n        raise raised[0]",
        "    return",
        (f"{RIGT}::test_a_failing_scenario_still_reports_its_own_error",),
    ),
    Revert(
        "A3: an abandoned scenario may still send commands",
        RIG,
        "        self._refuse_if_abandoned(command_type)\n        self._sequence += 1",
        "        self._sequence += 1",
        (f"{RIGT}::test_an_abandoned_scenario_cannot_send_another_command",),
    ),
    Revert(
        "A3: the deadline no longer tells the rig it abandoned the scenario",
        RIG,
        "    if thread.is_alive():\n        abandoned.set()\n        raise RigError(",
        "    if thread.is_alive():\n        raise RigError(",
        (f"{RIGT}::test_the_deadline_silences_the_scenario_it_could_not_stop",),
    ),
    Revert(
        "A3: the transcript keeps growing after the verdict",
        RIG,
        "        if not self._abandoned.is_set():\n            print(line, flush=True)",
        "        print(line, flush=True)",
        (f"{RIGT}::test_the_deadline_silences_the_scenario_it_could_not_stop",),
    ),
    Revert(
        "A4: main catches Exception, so sys.exit(0) leaves the rig exiting 0",
        RIG,
        '    except BaseException as failure:\n        detail = ""',
        '    except Exception as failure:\n        detail = ""',
        (f"{RIGT}::test_a_scenario_that_exits_the_process_is_not_reported_as_a_pass",),
    ),
    Revert(
        "A2: Popen used as a context manager, reintroducing an unbounded wait()",
        RIG,
        "        blender = _launch_blender(work_dir, port, nonce, blender_scripts)\n"
        "        drain = _OutputDrain(blender, log_path, abandoned)\n",
        "        with _launch_blender(work_dir, port, nonce, blender_scripts) as blender:\n"
        "            drain = _OutputDrain(blender, log_path, abandoned)\n",
        (f"{RIGT}::test_the_process_is_never_waited_on_without_a_timeout",),
    ),
    Revert(
        "R2: Blender's output no longer drained to exhaustion",
        RIG,
        "            for line in stream:\n                log.write(line)",
        "            for line in [stream.readline()]:\n                log.write(line)",
        (f"{RIGT}::test_blender_s_output_is_drained_to_a_log_instead_of_filling_the_pipe",),
    ),
    Revert(
        "A1: the pipe decodes strictly again",
        RIG,
        '        errors="replace",\n',
        "",
        (f"{RIGT}::test_blender_s_output_is_decoded_leniently",),
    ),
    Revert(
        "A1: the reader dies on the first byte it cannot decode",
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
        "A3: the log reader keeps echoing after the verdict",
        RIG,
        "                if line.startswith(_ECHOED_PREFIXES) and not self._abandoned.is_set():",
        "                if line.startswith(_ECHOED_PREFIXES):",
        (f"{RIGT}::test_the_log_reader_stops_echoing_once_the_scenario_is_abandoned",),
    ),
    Revert(
        "A1: a reader that died is not reported, so Blender is blamed instead",
        RIG,
        "    if drain.failure is not None:\n        problems.append(\n"
        '            f"the rig\'s own log reader died with {type(drain.failure).__name__}: {drain.failure} "\n'
        '            "(the pipe was drained anyway, so this did not hang Blender, but the log may be short)"\n'
        "        )\n",
        "",
        (f"{RIGT}::test_teardown_reports_the_rig_s_own_log_reader_dying",),
    ),
    Revert(
        "A6: a reader still holding the pipe is not reported",
        RIG,
        "    if drain.is_alive():\n        problems.append(\n",
        "    if False:\n        problems.append(\n",
        (f"{RIGT}::test_teardown_reports_a_log_reader_that_outlived_blender",),
    ),
    Revert(
        "R2: failures no longer quote Blender's log",
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
        "D1: a packaging root outside src/ would distribute scripts/",
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
        "R19: the compose toolsets key is mistyped",
        COMPOSE,
        "      BLENDER_MCP_TOOLSETS: shot",
        "      BLENDER_MCP_TOOLSET: shot",
        (f"{DOCKT}::test_compose_pins_a_toolset_selection_the_server_can_resolve",),
    ),
    Revert(
        "R19: the compose toolsets value does not resolve",
        COMPOSE,
        "      BLENDER_MCP_TOOLSETS: shot",
        "      BLENDER_MCP_TOOLSETS: shto",
        (f"{DOCKT}::test_compose_pins_a_toolset_selection_the_server_can_resolve",),
    ),
    # --- the ported output_roots module ---
    Revert(
        "output_roots: the environment variable is no longer split on os.pathsep",
        ADDON_OUTPUT_ROOTS,
        "raw.split(os.pathsep)",
        "[raw]",
        (f"{ROOTST}::test_configured_roots_splits_the_environment_variable",),
    ),
    Revert(
        "output_roots: an unset variable falls back to a hardcoded root",
        ADDON_OUTPUT_ROOTS,
        'get(OUTPUT_ROOTS_ENV_VAR, "")',
        'get(OUTPUT_ROOTS_ENV_VAR, "/default")',
        (f"{ROOTST}::test_configured_roots_is_empty_when_unset",),
    ),
    Revert(
        "output_roots: blank entries are no longer stripped out",
        ADDON_OUTPUT_ROOTS,
        "return [entry.strip() for entry in raw.split(os.pathsep) if entry.strip()]",
        "return [entry for entry in raw.split(os.pathsep) if entry]",
        (f"{ROOTST}::test_configured_roots_ignores_blank_entries",),
    ),
    Revert(
        "output_roots control: writable_roots keeps nothing at all",
        ADDON_OUTPUT_ROOTS,
        "        seen.add(path)\n        roots.append(path)",
        "        seen.add(path)",
        (f"{ROOTST}::test_writable_roots_keeps_existing_writable_directories",),
    ),
    Revert(
        "output_roots: a path that does not exist is kept",
        ADDON_OUTPUT_ROOTS,
        "        if not os.path.isdir(path) or not os.access(path, os.W_OK):",
        "        if os.path.isfile(path) or (os.path.isdir(path) and not os.access(path, os.W_OK)):",
        (f"{ROOTST}::test_writable_roots_drops_paths_that_do_not_exist",),
    ),
    Revert(
        "output_roots: a plain file is accepted as a root",
        ADDON_OUTPUT_ROOTS,
        "        if not os.path.isdir(path) or not os.access(path, os.W_OK):",
        "        if not os.path.exists(path) or not os.access(path, os.W_OK):",
        (f"{ROOTST}::test_writable_roots_drops_files",),
    ),
    Revert(
        "output_roots: a read-only directory is offered as writable",
        ADDON_OUTPUT_ROOTS,
        "        if not os.path.isdir(path) or not os.access(path, os.W_OK):",
        "        if not os.path.isdir(path):",
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
    Revert(
        "R1 wiring: the handshake ignores the deployment-configured roots",
        ADDON_SERVER_CORE,
        "            *configured_roots(),\n",
        "",
        (f"{ROOTST}::test_get_addon_info_reports_writable_output_roots",),
    ),
    Revert(
        "R1 wiring: an unconfigured Blender reports no writable root at all",
        ADDON_SERVER_CORE,
        '            getattr(bpy.app, "tempdir", None),\n            tempfile.gettempdir(),\n'
        '            os.path.expanduser("~"),\n',
        "",
        (f"{ROOTST}::test_get_addon_info_reports_roots_without_any_configuration",),
    ),
    # --- the handshake, and the MCP tool payload (defect ruling C's sixth site) ---
    Revert(
        "R17: the writable_output_roots parse removed from the handshake",
        ADDON_MANAGER,
        '            writable_output_roots=normalized_session_text_list(info.get("writable_output_roots")),\n',
        "",
        (
            f"{AMT}::test_handshake_surfaces_writable_output_roots",
            f"{AMT}::test_handshake_defaults_writable_output_roots_when_the_addon_omits_them",
        ),
    ),
    Revert(
        "R17: the handshake reorders the roots it was sent",
        ADDON_MANAGER,
        '            writable_output_roots=normalized_session_text_list(info.get("writable_output_roots")),',
        '            writable_output_roots=normalized_session_text_list(info.get("writable_output_roots"))[::-1],',
        (f"{AMT}::test_handshake_surfaces_writable_output_roots",),
    ),
    Revert(
        "F1: get_addon_status hardcodes an empty roots list",
        SERVER_CORE_TOOL,
        '            "writable_output_roots": result.writable_output_roots,',
        '            "writable_output_roots": [],',
        (
            f"{CORET}::test_get_addon_status_reports_the_writable_output_roots",
            f"{CORET}::test_get_addon_status_reports_no_roots_for_an_addon_that_does_not_send_them",
        ),
    ),
    Revert(
        "F1: get_addon_status reorders the roots the handshake gave it",
        SERVER_CORE_TOOL,
        '            "writable_output_roots": result.writable_output_roots,',
        '            "writable_output_roots": list(reversed(result.writable_output_roots)),',
        (f"{CORET}::test_get_addon_status_reports_the_writable_output_roots",),
    ),
    Revert(
        "F1: get_addon_status invents roots for an addon that sent none",
        SERVER_CORE_TOOL,
        '            "writable_output_roots": result.writable_output_roots,',
        '            "writable_output_roots": result.writable_output_roots or ["/invented"],',
        (f"{CORET}::test_get_addon_status_reports_no_roots_for_an_addon_that_does_not_send_them",),
    ),
    Revert(
        "defect ruling C: a payload key goes undocumented",
        SERVER_CORE_TOOL,
        '"writable_output_roots" (empty when none)',
        "writable output roots (empty when none)",
        (f"{CORET}::test_get_addon_status_documents_every_key_it_returns",),
    ),
    # --- Task 3: the session epoch reaching the agent ---
    Revert(
        "task 3: the session epoch is hardcoded instead of read off the handshake",
        SERVER_CORE_TOOL,
        '            "session_epoch": result.session_epoch,',
        '            "session_epoch": 0,',
        (f"{CORET}::test_get_addon_status_reports_the_session_epoch_and_the_open_file",),
    ),
    Revert(
        "task 3: the open .blend is hardcoded, so the payload cannot report a swap",
        SERVER_CORE_TOOL,
        '            "current_filepath": result.current_filepath,',
        '            "current_filepath": None,',
        (f"{CORET}::test_get_addon_status_reports_no_epoch_for_an_addon_that_does_not_send_one",),
    ),
    # --- Task 3: session state, the epoch, and the failure notes -------------
    Revert(
        "task 3: a completed load stops moving the epoch, so no client learns its capabilities are stale",
        ADDON_SESSION,
        "    _STATE.session_epoch += 1",
        "    _STATE.session_epoch += 0",
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
        "task 3: a FAILED load bumps the epoch, forcing a re-handshake storm on an event that changed nothing",
        ADDON_SESSION,
        '    _STATE.last_load_error = _failure_note("Loading", file_path)',
        '    _STATE.last_load_error = _failure_note("Loading", file_path)\n    _STATE.session_epoch += 1',
        (
            f"{SESSIONT}::test_a_failed_load_does_not_move_the_session_epoch",
            f"{SESSIONT}::test_get_session_info_reports_a_load_failure_without_moving_the_epoch",
        ),
    ),
    Revert(
        "task 3: a successful SAVE bumps the epoch, invalidating every client cache for nothing",
        ADDON_SESSION,
        "    written = _reported_path(file_path)",
        "    written = _reported_path(file_path)\n    _STATE.session_epoch += 1",
        (f"{SESSIONT}::test_a_successful_save_does_not_move_the_session_epoch",),
    ),
    Revert(
        "task 3: save_post trusts its argument, so save_as_mainfile(copy=True) names a file nobody has open",
        ADDON_SESSION,
        '    _STATE.current_filepath = _reported_path(getattr(bpy.data, "filepath", ""))',
        "    _STATE.current_filepath = written",
        (
            f"{SESSIONT}::test_a_save_copy_does_not_make_the_state_name_a_file_nobody_has_open",
            f"{SESSIONT}::test_a_save_copy_does_not_clear_a_failure_belonging_to_a_different_file",
        ),
    ),
    Revert(
        "task 3: a failed save records nothing, so a blocked checkpoint looks like a clean one",
        ADDON_SESSION,
        '    _STATE.last_save_error = _failure_note("Saving", file_path)',
        "    _STATE.last_save_error = None",
        (
            f"{SESSIONT}::test_a_failed_save_records_the_error_without_moving_the_epoch",
            f"{SESSIONT}::test_a_later_success_clears_the_recorded_failure",
        ),
    ),
    Revert(
        "task 3: the leaf split goes back to os.path.basename, which on posix splits on / only",
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
        "task 3: control characters survive into a client-facing note (newline = prompt-injection surface)",
        ADDON_TEXT_HYGIENE,
        '    return "".join(character for character in str(value or "") if not is_unsafe(character)).strip()',
        '    return str(value or "").strip()',
        (
            # `[newline]` and `[ansi-escape]` are deliberately **not** here, and
            # the reason is a finding rather than an omission: their expectation
            # is hygiene alone, and the leaf allowlist added in this pass refuses
            # a control character on its own - so they pass with the strip
            # reverted, and naming them would make this row a SURVIVOR. They are
            # covered by "the leaf allowlist goes away" below. The two named here
            # expect the *stripped* name (`ab.blend`), which only stripping
            # produces; reverted, they get `the requested file`.
            f"{SESSIONT}::test_a_recorded_failure_names_one_bounded_leaf_and_nothing_else[nul]",
            f"{SESSIONT}::test_a_recorded_failure_names_one_bounded_leaf_and_nothing_else[c1-control]",
        ),
    ),
    Revert(
        "task 3: the note's length bound goes away, so a 400-character name ships whole",
        ADDON_TEXT_HYGIENE,
        "    if len(text) > max_chars:",
        "    if False:",
        (f"{SESSIONT}::test_a_recorded_failure_names_one_bounded_leaf_and_nothing_else[over-long]",),
    ),
    Revert(
        "task 3: the leaf assertion stops rejecting traversal tokens and empty components",
        ADDON_TEXT_HYGIENE,
        'NOT_A_LEAF = frozenset({"", ".", ".."})',
        "NOT_A_LEAF = frozenset()",
        (
            f"{SESSIONT}::test_a_recorded_failure_names_one_bounded_leaf_and_nothing_else[dot-dot]",
            f"{SESSIONT}::test_a_recorded_failure_names_one_bounded_leaf_and_nothing_else[empty]",
            f"{SESSIONT}::test_a_recorded_failure_names_one_bounded_leaf_and_nothing_else[trailing-separator]",
        ),
    ),
    Revert(
        "task 3: a directory is named in a failure note - which for an empty path is the server's own CWD",
        ADDON_TEXT_HYGIENE,
        "        if os.path.isdir(raw):",
        "        if False:",
        (f"{SESSIONT}::test_a_directory_is_never_named_in_a_recorded_failure",),
    ),
    Revert(
        "task 3: the snapshot loses its process-unique id, reopening the epoch's ABA hole across a restart",
        ADDON_SESSION,
        '        "session_id": SESSION_ID,',
        '        "session_id": "",',
        (
            f"{SESSIONT}::test_the_snapshot_carries_a_process_unique_session_id",
            f"{SESSIONT}::test_get_addon_info_and_get_session_info_agree_about_the_session_id",
        ),
    ),
    Revert(
        "task 3: handler registration stacks duplicates, so one swap moves the epoch twice",
        ADDON_SESSION,
        "        if handler not in handler_list:",
        "        if True:",
        (f"{SESSIONT}::test_registering_twice_does_not_stack_duplicate_handlers",),
    ),
    Revert(
        "task 3: unregistering an absent handler raises, leaving the addon un-unloadable",
        ADDON_SESSION,
        "        while handler in handler_list:\n            handler_list.remove(handler)",
        "        handler_list.remove(handler)",
        (f"{SESSIONT}::test_unregistering_without_registering_is_not_an_error",),
    ),
    Revert(
        "task 3: the addon lifecycle never wires the session handlers in, so nothing maintains the epoch",
        ADDON_INIT,
        "    session.register_handlers()",
        "    pass  # handler wiring reverted",
        (f"{SESSIONT}::test_the_addon_registers_and_unregisters_the_session_handlers",),
    ),
    Revert(
        "task 3: get_session_info is absent from the dispatch table, so the poll surface cannot be polled",
        ADDON_SERVER_CORE,
        '            "get_session_info": self.get_session_info,',
        '            "get_session_info_reverted": self.get_session_info,',
        (f"{SESSIONT}::test_get_session_info_is_registered_and_read_only",),
    ),
    Revert(
        "task 3: the library summary publishes an absolute filepath, mapping the asset library out",
        ADDON_FILE_LIFECYCLE,
        '        "filepath": whole if whole is not None else client_safe_leaf(filepath),',
        '        "filepath": filepath,',
        (f"{SESSIONT}::test_the_library_summary_reports_identity_without_the_asset_library_layout",),
    ),
    Revert(
        "task 3: save_shot joins the swap set, discarding a whole batch every time a client checkpoints",
        ADDON_SERVER_CORE,
        '_SESSION_SWAP_COMMANDS = frozenset({"open_shot", "reset_session"})',
        '_SESSION_SWAP_COMMANDS = frozenset({"open_shot", "reset_session", "save_shot"})',
        (f"{SESSIONT}::test_the_session_swap_set_holds_the_commands_that_replace_the_database",),
    ),
    Revert(
        "task 3: a swap is wrapped in mutation_transaction, whose rollback would enumerate the whole new file",
        ADDON_SERVER_CORE,
        "            or cmd_type in self._SESSION_SWAP_COMMANDS\n        )",
        "        )",
        (f"{SESSIONT}::test_a_session_swap_command_never_reaches_mutation_transaction",),
    ),
    Revert(
        "task 3: get_session_info reports the dirty flag and libraries from nowhere",
        ADDON_FILE_LIFECYCLE,
        '            "is_dirty": bool(bpy.data.is_dirty),',
        '            "is_dirty": False,',
        (f"{SESSIONT}::test_get_session_info_reports_the_dirty_flag_and_the_library_summary",),
    ),
    Revert(
        "task 3: an unsaved session reports an empty string, which reads as a real path in a client's logs",
        ADDON_SESSION,
        '    reported = str(file_path or "")\n    return reported or None',
        '    return str(file_path or "")',
        (f"{SESSIONT}::test_an_unsaved_session_reports_no_filepath_rather_than_an_empty_string",),
    ),
    Revert(
        "task 3: a disable/enable cycle leaves the handler lists stacked",
        ADDON_SESSION,
        "def unregister_handlers() -> None:",
        "def unregister_handlers() -> None:\n    return",
        (f"{SESSIONT}::test_a_disable_enable_cycle_leaves_exactly_one_of_each_handler",),
    ),
    # --- Task 3: the barrier itself (plan Step 6) ----------------------------
    Revert(
        "task 3: THE BARRIER - a swap no longer ends its tick or discards the batch queued behind it",
        ADDON_SERVER_CORE,
        "                self._run_session_swap(command, client)\n                break",
        (
            "                self._execute_and_answer(command, client)\n"
            "                processed += 1\n"
            "                continue"
        ),
        (
            # Only the three the *snapshot* half alone can protect. The rest
            # are covered by the stamp half as well, so reverting the snapshot
            # alone leaves them passing. An earlier revision of this comment
            # pointed at "the compound row below", which does not exist; there
            # is no single revert that falsifies both halves at once, and
            # saying there was is how a reader stops looking for the gap.
            f"{THREADT}::test_a_failed_swap_also_discards_the_commands_queued_behind_it",
            f"{THREADT}::test_the_barrier_message_names_the_epoch_without_claiming_it_moved",
            f"{THREADT}::test_a_swap_ends_its_tick_even_when_a_fresh_command_is_already_queued",
        ),
    ),
    Revert(
        "task 3: no rejection frame is sent on EITHER barrier half, so the client is dropped in silence",
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
        "task 3: superseded commands are dropped, not answered - the hang criterion 2 names",
        ADDON_SERVER_CORE,
        "            self._discard_superseded(superseded)",
        "            superseded.clear()",
        (f"{THREADT}::test_every_command_spanning_a_swap_is_answered_on_both_sockets",),
    ),
    Revert(
        "task 3: the enqueue path stops stamping, so the mid-load window is invisible again",
        ADDON_SERVER_CORE,
        "        self._stamp_session(command)",
        "        pass  # stamping reverted",
        (
            # `..._rejected_at_dequeue` is deliberately absent: the drain loop now
            # fails CLOSED, so an unstamped command is rejected too - for a
            # different reason, but the assertion cannot tell them apart. What an
            # unstamped queue really costs is the *serviced* case below, which
            # turns into a rejection the client never asked for.
            f"{THREADT}::test_a_command_queued_after_the_swap_is_serviced_normally_under_the_stamp",
            f"{THREADT}::test_the_enqueue_path_is_the_only_producer_and_it_stamps",
        ),
    ),
    Revert(
        "task 3: the dequeue comparison goes away, so a stale stamp is never acted on",
        ADDON_SERVER_CORE,
        "            if stamp != self._session_marker():",
        "            if False:",
        (
            f"{THREADT}::test_a_command_queued_before_a_swap_that_lands_elsewhere_is_rejected_at_dequeue",
            f"{THREADT}::test_the_queue_is_snapshotted_before_the_swap_runs_not_after",
        ),
    ),
    Revert(
        "task 3: the enqueue path reaches for bpy on a client thread",
        ADDON_SERVER_CORE,
        "        self._stamp_session(command)\n        print(",
        "        self._stamp_session(command)\n        _ = bpy.data\n        print(",
        (f"{THREADT}::test_the_stamp_is_read_without_touching_bpy_on_the_client_thread",),
    ),
    Revert(
        "task 3: the barrier fires for an undispatchable swap, so one frame discards everyone batch",
        ADDON_SERVER_CORE,
        ' and self._is_dispatchable(\n                    command.get("type")\n                )',
        "",
        (f"{THREADT}::test_a_swap_command_the_addon_cannot_dispatch_discards_nobodys_batch",),
    ),
    Revert(
        "task 3: the rejection path loses its deadline, pinning Blender's main thread on a stalled peer",
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
        "task 3: the pre-swap drain goes back to `while True`, which races producers that keep refilling",
        ADDON_SERVER_CORE,
        "        for _slot in range(self._MAX_QUEUED_COMMANDS):",
        "        while True:",
        (f"{THREADT}::test_the_pre_swap_drain_is_bounded_by_an_explicit_count",),
    ),
    Revert(
        "task 3: only Exception is caught, so a blender-side abort strands the swap's own client",
        ADDON_SERVER_CORE,
        "        except BaseException as e:",
        "        except SystemExit as e:",
        (f"{THREADT}::test_the_swaps_own_client_is_answered_when_the_swap_raises_a_base_exception",),
    ),
    Revert(
        "task 3: superseded is built outside the try, so an abort mid-drain loses dequeued commands",
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
        "task 3: the queue is drained AFTER the swap, the ordering Task 2's cycle-2 correction rejected",
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
        "task 3: the rejection frame drops the machine-readable epoch, leaving a client to regex prose",
        ADDON_SERVER_CORE,
        '                    "session_epoch": epoch,',
        '                    "session_epoch_prose_only": epoch,',
        (f"{THREADT}::test_the_barrier_rejection_carries_the_epoch_as_a_first_class_field",),
    ),
    Revert(
        "task 3: a dying tick hands off to nothing, so one escaping exception kills the drain loop",
        ADDON_SERVER_CORE,
        "            self._replace_this_dying_timer()\n            raise",
        "            raise",
        (f"{THREADT}::test_an_escaping_exception_hands_the_drain_loop_to_a_fresh_timer",),
    ),
    Revert(
        "task 3: recovery resurrects a timer stop() deliberately removed",
        ADDON_SERVER_CORE,
        "        if not self.running:\n            return\n        dying = self._drain_timer",
        "        dying = self._drain_timer",
        (f"{THREADT}::test_a_stopped_server_does_not_resurrect_its_drain_timer",),
    ),
    Revert(
        "task 3: a command sent after the swap is never serviced, so the barrier wedges the server",
        ADDON_SERVER_CORE,
        (
            "            self._execute_and_answer(command, client)\n"
            "            processed += 1\n"
            "\n"
            "    def _replace_this_dying_timer"
        ),
        "            processed += 1\n\n    def _replace_this_dying_timer",
        (
            f"{THREADT}::test_a_command_that_arrives_after_the_swap_is_serviced_normally",
            f"{THREADT}::test_a_command_queued_after_the_swap_is_serviced_normally_under_the_stamp",
        ),
    ),
    Revert(
        "task 3: the harness's open_mainfile stub refuses production's own `use_scripts` spelling",
        TEST_THREADING_FILE,
        'def open_mainfile(self, filepath: str = "", use_scripts: bool = False) -> set[str]:',
        'def open_mainfile(self, filepath: str = "", _use_scripts: bool = False) -> set[str]:',
        (f"{THREADT}::test_the_open_mainfile_stub_takes_use_scripts_the_way_production_passes_it",),
    ),
    # --- Task 3: the handshake and the client-side reaction ------------------
    Revert(
        "task 3: the handshake stops carrying the session fields",
        ADDON_MANAGER,
        '            session_epoch=normalized_session_epoch(info.get("session_epoch")),',
        "            session_epoch=None,",
        (
            f"{AMT}::test_handshake_surfaces_the_session_epoch_and_the_open_file",
            f"{AMT}::test_handshake_defaults_the_session_fields_when_the_addon_omits_them",
        ),
    ),
    Revert(
        "task 3: current_filepath is parsed with `or None`, so any JSON type reaches a str|None field",
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
        "task 3: session_id is parsed with `or None`, so the ABA guard takes any JSON type off the socket",
        ADDON_MANAGER,
        '            session_id=normalized_session_id(info.get("session_id")),',
        '            session_id=info.get("session_id") or None,',
        tuple(
            f"{AMT}::test_handshake_refuses_a_session_id_that_is_not_a_string[{case}]"
            for case in ("dict", "list", "int", "bool", "float")
        ),
    ),
    Revert(
        "task 3: the handshake marker drops the session id, so a restart at the same epoch looks unchanged",
        ADDON_MANAGER,
        "        return (self.session_id, self.session_epoch)",
        "        return (None, self.session_epoch)",
        (f"{AMT}::test_handshake_surfaces_the_session_id_so_the_epoch_survives_a_restart",),
    ),
    Revert(
        "task 3: a reported session change no longer marks the cached handshake stale",
        SERVER_CONNECTION,
        "    _session_marker_stale.set()",
        "    return",
        (
            f"{CONNT}::test_a_moved_epoch_marks_the_cached_handshake_stale",
            f"{CONNT}::test_the_marker_is_read_from_a_command_result_as_well_as_the_frame",
            f"{CONNT}::test_a_barrier_rejection_read_off_the_socket_marks_the_handshake_stale",
        ),
    ),
    Revert(
        "task 3: only the epoch is compared, so a restart back to the same number looks unchanged (ABA)",
        SERVER_CONNECTION,
        "or observed == _addon_handshake.session_marker():",
        "or observed[1] == _addon_handshake.session_epoch:",
        (f"{CONNT}::test_a_restarted_addon_at_the_same_epoch_still_marks_the_handshake_stale",),
    ),
    Revert(
        "task 3: the marker is read at frame level only, so get_session_info's nested pair is missed",
        SERVER_CONNECTION,
        '    if command_type in _SESSION_REPORTING_COMMANDS:\n        sources.append(payload.get("result"))',
        "    pass  # nested result read reverted",
        (f"{CONNT}::test_the_marker_is_read_from_a_command_result_as_well_as_the_frame",),
    ),
    Revert(
        "task 3: every response invalidates the handshake, so each tool call costs two commands",
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
        "task 3: the stale flag is never cleared, so every command pays for another handshake",
        SERVER_CONNECTION,
        "    _session_marker_stale.clear()",
        "    pass  # clear() reverted",
        (f"{CONNT}::test_one_swap_costs_exactly_one_re_handshake_over_a_real_round_trip",),
    ),
    Revert(
        "task 3: the command gate goes back to the handshake cached once per process",
        SERVER_CONNECTION,
        "        handshake = refresh_handshake_if_session_changed(self)",
        "        handshake = get_last_handshake()",
        (f"{CONNT}::test_the_command_gate_reads_the_refreshed_capability_set",),
    ),
    Revert(
        "task 3: the receive path never observes the marker, so the mechanism is never reached",
        SERVER_CONNECTION,
        "            note_session_marker(response, command_type)",
        "            pass  # note_session_marker reverted",
        (f"{CONNT}::test_a_barrier_rejection_read_off_the_socket_marks_the_handshake_stale",),
    ),
    # --- Task 3 cycle 3: the repairs three critics converged on ---------------
    Revert(
        "task 3: `or` short-circuits again, so a spent budget closes every remaining peer unsent",
        ADDON_SERVER_CORE,
        "            if not self._send_bounded(client, frame, timeout):",
        "            if time.monotonic() >= deadline or not self._send_bounded(client, frame, timeout):",
        (
            f"{THREADT}::test_a_healthy_peer_queued_behind_stalled_ones_is_still_answered",
            # The distinct-peer sibling sees the same defect at the other end of
            # the batch: with the short-circuit back, every peer past the budget
            # is closed with zero send attempts, so `unattempted` is non-empty.
            f"{THREADT}::test_rejecting_a_full_queue_to_distinct_stalled_peers_is_bounded",
        ),
    ),
    Revert(
        "task 3: the send timeout goes back to a performance target used as a health threshold",
        ADDON_SERVER_CORE,
        "    _REJECTION_SEND_TIMEOUT_SECONDS = 0.25",
        "    _REJECTION_SEND_TIMEOUT_SECONDS = 0.05",
        (f"{THREADT}::test_a_peer_slower_than_a_loopback_reader_is_not_destroyed_for_it",),
    ),
    Revert(
        "task 3: an abandoned peer is written to again, paying a syscall per remaining frame",
        ADDON_SERVER_CORE,
        "            if client in abandoned:\n                continue",
        "            if False:\n                continue",
        (f"{THREADT}::test_rejecting_a_full_queue_to_a_stalled_peer_is_bounded",),
    ),
    Revert(
        "task 3: the write-lock acquisition is unbounded again, outside both of the path's own bounds",
        ADDON_SERVER_CORE,
        "        acquired = send_lock.acquire(False) if lock_timeout <= 0 else send_lock.acquire(timeout=lock_timeout)",
        "        acquired = send_lock.acquire()",
        (f"{THREADT}::test_a_malformed_frame_arriving_mid_rejection_cannot_park_the_main_thread",),
    ),
    Revert(
        "task 3: a failed timeout restore reports the frame as delivered, leaving the peer spinning",
        ADDON_SERVER_CORE,
        (
            "            print(\"Could not restore a client socket's own timeout"
            ' - closing it rather than leaving it spinning")\n'
            "            return False"
        ),
        "            pass  # restore failure ignored",
        (f"{THREADT}::test_a_socket_whose_timeout_cannot_be_restored_is_dropped_not_left_spinning",),
    ),
    Revert(
        "task 3: the drain loop fails OPEN again, so an unstamped command runs",
        ADDON_SERVER_CORE,
        "            if stamp != self._session_marker():",
        "            if stamp is not None and stamp != self._session_marker():",
        (f"{THREADT}::test_a_command_that_reached_the_queue_unstamped_is_rejected_not_run",),
    ),
    Revert(
        "task 3: ordinary responses stop carrying the marker, so Blender's own File -> Open is invisible",
        ADDON_SERVER_CORE,
        '        response["session_id"], response["session_epoch"] = self._session_marker()',
        "        pass  # per-frame marker reverted",
        (f"{THREADT}::test_an_ordinary_response_carries_the_session_marker_too",),
    ),
    Revert(
        "task 3: an aborted swap leaves the marker where it was, so mid-load stamps still match",
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
        "task 3: the dying timer is never unregistered, so one live drain timer rests on Blender alone",
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
        "task 3: a swap across a disable/enable cycle is observed and discarded",
        ADDON_SESSION,
        "    if observed != _STATE.current_filepath:\n        _STATE.session_epoch += 1",
        "    if False:\n        _STATE.session_epoch += 1",
        (f"{SESSIONT}::test_a_swap_while_the_addon_was_disabled_still_moves_the_marker",),
    ),
    Revert(
        "task 3: every registration bumps the epoch, so a plain Blender start invalidates every cache",
        ADDON_SESSION,
        "    if observed != _STATE.current_filepath:\n        _STATE.session_epoch += 1",
        "    if True:\n        _STATE.session_epoch += 1",
        (f"{SESSIONT}::test_re_enabling_on_the_same_file_does_not_move_the_marker",),
    ),
    Revert(
        "task 3: the unsafe-character set stops at C0/C1, so U+2028 and the bidi overrides survive",
        ADDON_TEXT_HYGIENE,
        'UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})',
        'UNSAFE_CATEGORIES = frozenset({"Cc"})',
        (
            # The `filepath` branch no longer depends on this set - the leaf
            # allowlist refuses `Zl` and `Cf` by not admitting them - so the two
            # path params that used to be named here now pass with it reverted.
            # `name` is the field that still depends on it, because
            # `client_safe_text` publishes what it is given.
            # `name` is the field that still depends on it, and the dependency
            # is now *which* safe string is published rather than whether one
            # is: `client_safe_leaf` strips first and allowlists second, so with
            # the set reverted the override survives the strip and the allowlist
            # refuses the leaf outright. The test therefore names the stripped
            # string rather than asserting hygiene, which any of the three
            # outcomes satisfies.
            f"{SESSIONT}::test_a_library_name_is_published_without_its_control_characters",
        ),
    ),
    Revert(
        "task 3: the link allowlist stops rejecting traversal and empty components",
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
        "task 3: the link predicate goes back to a blocklist, which is a list of the attacks already known",
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
        "task 3: the confusable check goes, so an admitted letter publishes a name that renders as another",
        ADDON_TEXT_HYGIENE,
        "    if is_confusable(leaf) or not _is_admissible_leaf(leaf):",
        "    if not _is_admissible_leaf(leaf):",
        (f"{SESSIONT}::test_a_confusable_leaf_name_is_refused_rather_than_published",),
    ),
    Revert(
        "task 3: `//` followed by a root is called relative again, so an absolute path is published whole",
        ADDON_TEXT_HYGIENE,
        "    if body[:1] in _LEAF_SEPARATORS:\n        return None",
        "    if False:\n        return None",
        (
            # Only `is_relative` depends on this clause. The *publication*
            # decision for `///Users/...` is taken one line further on, by the
            # empty-component check, and survives this revert - measured, not
            # assumed, which is why the other two nodes moved to the row below.
            f"{SESSIONT}::test_a_rooted_relative_prefix_is_not_reported_as_relative",
        ),
    ),
    Revert(
        "task 3: the gate admits one string and the publisher returns another, manufacturing what it rejected",
        ADDON_FILE_LIFECYCLE,
        '        "filepath": whole if whole is not None else client_safe_leaf(filepath),',
        '        "filepath": filepath if whole is not None else client_safe_leaf(filepath),',
        (
            f"{SESSIONT}::test_a_whole_published_link_is_the_string_the_gate_looked_at",
            f"{HOSTILE_LIB}[format character inside a component-//libs/\\u200bcanon.blend-forbidden12]",
        ),
    ),
    Revert(
        "task 3: the library summary loses the hygiene its sibling field has, on both branches",
        ADDON_FILE_LIFECYCLE,
        ('        "filepath": whole if whole is not None else client_safe_leaf(filepath),'),
        '        "filepath": filepath,',
        (
            f"{HOSTILE_LIB}[ANSI escape, relative branch-//shots/\\x1b[31mx.blend-forbidden1]",
            f"{HOSTILE_LIB}[ANSI escape, absolute branch-/mnt/studio/\\x1b[31mx.blend-forbidden2]",
            f"{HOSTILE_LIB}[500 characters, relative branch-//{'a' * 500}.blend-forbidden3]",
            f"{HOSTILE_LIB}[500 characters, absolute branch-/mnt/{'b' * 500}.blend-forbidden4]",
        ),
    ),
    Revert(
        "task 3: get_addon_status publishes the epoch without the id it is only comparable within",
        SERVER_CORE_TOOL,
        '            "session_id": result.session_id,',
        "",
        (f"{CORET}::test_get_addon_status_reports_the_session_id_the_epoch_is_only_comparable_within",),
    ),
    Revert(
        "task 3: the refresh's own response is read as news, so one swap costs two handshakes",
        SERVER_CONNECTION,
        '    if getattr(_refreshing, "active", False):\n        return',
        "    if False:\n        return",
        (f"{CONNT}::test_one_swap_costs_exactly_one_re_handshake_over_a_real_round_trip",),
    ),
    Revert(
        "task 3: a failed re-handshake clears the staleness signal permanently and never retries",
        SERVER_CONNECTION,
        "    if learned is None or None in learned:",
        "    if False:",
        (f"{CONNT}::test_a_refresh_that_fails_leaves_the_staleness_signal_standing",),
    ),
    Revert(
        "task 3: the observed pair is compared raw, so a normalized value never equals its own twin",
        SERVER_CONNECTION,
        (
            '                normalized_session_id(source.get("session_id")),\n'
            '                normalized_session_epoch(source.get("session_epoch")),'
        ),
        ('                source.get("session_id"),\n                source.get("session_epoch"),'),
        (f"{CONNT}::test_a_non_conforming_epoch_does_not_re_arm_the_flag_forever",),
    ),
    Revert(
        "task 3: any result carrying a session_epoch key trips a re-handshake, whatever command it answers",
        SERVER_CONNECTION,
        "    if command_type in _SESSION_REPORTING_COMMANDS:",
        "    if True:",
        (f"{CONNT}::test_an_ordinary_commands_result_cannot_trip_a_re_handshake",),
    ),
    Revert(
        "task 3: the producer scan goes back to the form five shapes were shown to evade",
        TEST_THREADING_FILE,
        '_ENQUEUE_METHODS = frozenset({"put", "put_nowait"})',
        '_ENQUEUE_METHODS = frozenset({"put_nowait"})',
        (f"{EVASION}[blocking put()-def sneak(self, item):\\n    self.command_queue.put(item)\\n]",),
    ),
    Revert(
        "task 3: the producer scan walks FunctionDef only, missing an async def and a lambda",
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
        "task 3: the producer scan requires the receiver spelled `<x>.command_queue` again",
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
    # ---------------------------------------------------------------------
    # Cycle-4 repairs. Added because `--list` reported 50 new nodes with no
    # row: the matrix pins test nodes, so a fix whose test nobody pinned is a
    # fix nobody proved. That gap is exactly how an unreachable `--work-dir`
    # symlink guard survived three critic cycles and a 95-row matrix.
    # ---------------------------------------------------------------------
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
        "transport: a non-loopback bind needs no opt-in, as it did before the repair",
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
    Revert(
        "task 3: the leaf allowlist goes back to a three-character blocklist",
        ADDON_TEXT_HYGIENE,
        "    return all(\n"
        "        character in LEAF_PUNCTUATION or unicodedata.category(character).startswith(LEAF_CATEGORY_PREFIXES)\n"
        "        for character in leaf\n"
        "    )",
        '    return not any(marker in leaf for marker in ("/", "\\\\", ":"))',
        (f"{SESSIONT}::test_no_character_can_smuggle_a_separator_through_a_leaf_name",),
    ),
    Revert(
        "task 3: the leaf allowlist refuses every letter, so a real file name reports as unnameable",
        ADDON_TEXT_HYGIENE,
        'LEAF_CATEGORY_PREFIXES = ("L", "N", "M")',
        'LEAF_CATEGORY_PREFIXES = ("N",)',
        (f"{SESSIONT}::test_a_confusable_check_does_not_refuse_an_ordinary_name",),
    ),
    # --- Task 3 structural pass: the latch, the abort guard, the send floor ---
    Revert(
        "task 3: an aborted swap latches nothing, so the state stays advisory and nothing reads it",
        ADDON_SESSION,
        "    _STATE.session_indeterminate = True\n    _STATE.current_filepath = None",
        "    _STATE.session_indeterminate = False\n    _STATE.current_filepath = None",
        (
            f"{SESSIONT}::test_an_aborted_swap_latches_a_state_a_client_can_read",
            f"{SESSIONT}::test_only_a_completed_load_clears_the_indeterminate_latch",
            f"{THREADT}::test_a_command_is_refused_while_the_session_is_indeterminate",
        ),
    ),
    Revert(
        "task 3: an aborted session still names the shot it was replacing, which save_shot would write over",
        ADDON_SESSION,
        "    _STATE.session_indeterminate = True\n    _STATE.current_filepath = None",
        "    _STATE.session_indeterminate = True",
        (f"{SESSIONT}::test_an_aborted_swap_latches_a_state_a_client_can_read",),
    ),
    Revert(
        "task 3: a completed load stops clearing the latch, so the addon wedges after one abort",
        ADDON_SESSION,
        "    _STATE.load_in_flight = False\n    _STATE.session_indeterminate = False",
        "    _STATE.load_in_flight = False",
        (
            f"{SESSIONT}::test_only_a_completed_load_clears_the_indeterminate_latch",
            f"{THREADT}::test_the_commands_that_report_or_repair_an_indeterminate_session_still_run",
        ),
    ),
    Revert(
        "task 3: the snapshot stops publishing the latch, so no client can see why it is being refused",
        ADDON_SESSION,
        '        "session_indeterminate": _STATE.session_indeterminate,',
        '        "session_indeterminate": False,',
        (
            f"{SESSIONT}::test_an_aborted_swap_latches_a_state_a_client_can_read",
            f"{SESSIONT}::test_only_a_completed_load_clears_the_indeterminate_latch",
        ),
    ),
    Revert(
        "task 3: get_addon_info hides the latch, so the one surface a refused client can reach says nothing",
        ADDON_SERVER_CORE,
        '            "session_indeterminate": session["session_indeterminate"],',
        '            "session_indeterminate": False,',
        (f"{SESSIONT}::test_an_aborted_swap_latches_a_state_a_client_can_read",),
    ),
    Revert(
        "task 3: the drain loop stops enforcing the latch, so a command runs against a half-replaced database",
        ADDON_SERVER_CORE,
        '            if session_is_indeterminate() and command.get("type") not in self._INDETERMINATE_SAFE_COMMANDS:',
        "            if False:",
        (f"{THREADT}::test_a_command_is_refused_while_the_session_is_indeterminate",),
    ),
    Revert(
        "task 3: the latch refuses the commands that repair and report it, wedging the addon for good",
        ADDON_SERVER_CORE,
        "    _INDETERMINATE_SAFE_COMMANDS = frozenset("
        '{"get_addon_info", "get_session_info", "open_shot", "reset_session"})',
        "    _INDETERMINATE_SAFE_COMMANDS = frozenset()",
        (f"{THREADT}::test_the_commands_that_report_or_repair_an_indeterminate_session_still_run",),
    ),
    Revert(
        "task 3: the abort guard goes back to a bare except, claiming a partial replace when no load ran",
        ADDON_SERVER_CORE,
        (
            "            mid_load = load_in_flight()\n"
            "            if mid_load:\n"
            "                mark_session_indeterminate()\n"
            '            if not receipt["answered"]:\n'
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
        "task 3: the abort guard's two concerns go back to being exclusive, so a mid-load abort never latches",
        ADDON_SERVER_CORE,
        (
            "            mid_load = load_in_flight()\n"
            "            if mid_load:\n"
            "                mark_session_indeterminate()\n"
            '            if not receipt["answered"]:\n'
            "                self._answer(command, client, "
            '{"status": "error", "message": self._abort_message(mid_load)})'
        ),
        (
            '            if not receipt["answered"]:\n'
            "                self._answer(command, client, "
            '{"status": "error", "message": self._ABORTED_BEFORE_HANDOFF})\n'
            "            elif load_in_flight():\n"
            "                mark_session_indeterminate()"
        ),
        (f"{THREADT}::test_an_abort_that_beats_the_answer_still_latches_a_load_that_was_in_flight",),
    ),
    Revert(
        "task 3: the abort message stops asking the flag, so a mid-load abort still says the database is unchanged",
        ADDON_SERVER_CORE,
        "        return self._ABORTED_MID_LOAD if mid_load else self._ABORTED_BEFORE_HANDOFF",
        "        return self._ABORTED_BEFORE_HANDOFF",
        (f"{THREADT}::test_an_abort_that_beats_the_answer_still_latches_a_load_that_was_in_flight",),
    ),
    Revert(
        "task 3: the abort message always claims a half-replaced database, re-arming the T3-18 false positive",
        ADDON_SERVER_CORE,
        "        return self._ABORTED_MID_LOAD if mid_load else self._ABORTED_BEFORE_HANDOFF",
        "        return self._ABORTED_MID_LOAD",
        (f"{THREADT}::test_an_abort_with_no_load_in_flight_still_answers_and_still_claims_nothing_moved",),
    ),
    Revert(
        "task 3: the past-budget send goes back to settimeout(0), which drops the peer it is answering",
        ADDON_SERVER_CORE,
        "    _PAST_BUDGET_SEND_TIMEOUT_SECONDS = 0.001",
        "    _PAST_BUDGET_SEND_TIMEOUT_SECONDS = 0.0",
        (f"{THREADT}::test_the_past_budget_send_never_takes_the_socket_out_of_timeout_mode",),
    ),
    Revert(
        "task 3: BlockingIOError falls through to `break` again, so EAGAIN reads as a dead client",
        ADDON_SERVER_CORE,
        "                except BlockingIOError:",
        "                except TimeoutError:",
        (f"{THREADT}::test_a_peer_whose_recv_reports_would_block_is_not_disconnected",),
    ),
    # --- Task 3 structural pass: the server boundary ---
    Revert(
        "task 3: the server boundary stops filtering control characters, as it did for three whole cycles",
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
        "task 3: `warning` goes back to carrying the addon's own error message verbatim",
        ADDON_MANAGER,
        '            warning=strip_unsafe(f"Addon handshake failed: {e}"),',
        '            warning=f"Addon handshake failed: {e}",',
        (f"{AMT}::test_a_hostile_addon_error_message_does_not_reach_the_handshake_warning",),
    ),
    Revert(
        "task 3: the list fields publish the cleaned form again, manufacturing a traversal and an exact capability",
        ADDON_MANAGER,
        "    return isinstance(element, str) and cleaned == element",
        "    return True",
        (
            f"{AMT}::test_a_root_whose_traversal_only_exists_once_cf_is_stripped_is_refused",
            f"{AMT}::test_a_capability_that_only_matches_once_cf_is_stripped_is_refused",
        ),
    ),
    Revert(
        "task 3: the structural gate goes back to allowing an end-trim, so padding synthesises a root and a capability",
        ADDON_MANAGER,
        "    return isinstance(element, str) and cleaned == element",
        "    return isinstance(element, str) and cleaned == element.strip()",
        (f"{AMT}::test_an_element_that_only_differs_by_end_whitespace_is_refused_too",),
    ),
    Revert(
        "task 3: an untrusted payload's truthiness decides whether the session is indeterminate",
        ADDON_MANAGER,
        '            session_indeterminate=info.get("session_indeterminate") is True,',
        '            session_indeterminate=info.get("session_indeterminate") is not None,',
        (f"{AMT}::test_the_handshake_reports_an_indeterminate_session_only_when_the_addon_says_so",),
    ),
    Revert(
        "task 3: the two copies of the control-character rule drift, which is the risk duplication carries",
        SERVER_TEXT_HYGIENE,
        'UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})',
        'UNSAFE_CATEGORIES = frozenset({"Cc"})',
        (f"{AMT}::test_both_sides_of_the_socket_hold_the_same_control_character_block",),
    ),
    Revert(
        "task 3: get_addon_status carries the latch under a name its docstring never mentions",
        SERVER_CORE_TOOL,
        '            "session_indeterminate": result.session_indeterminate,',
        '            "session_indeterminate_x": result.session_indeterminate,',
        (
            f"{CORET}::test_get_addon_status_documents_every_key_it_returns",
            f"{CORET}::test_get_addon_status_reports_an_indeterminate_session",
            f"{CORET}::test_get_addon_status_reports_a_healthy_session_as_determinate",
        ),
    ),
    # --- Task 3 cycle 5: the triaged repair round -----------------------------
    Revert(
        "task 3: the library name goes back through client_safe_text, which allowlists nothing",
        ADDON_FILE_LIFECYCLE,
        '        "name": client_safe_leaf(getattr(library, "name", "")),',
        '        "name": _reverted_unallowlisted_name(getattr(library, "name", "")),',
        tuple(f"{HOSTILE_LIB_NAME}[{case}]" for case in HOSTILE_LIB_NAME_IDS),
        REVERTED_LIBRARY_NAME,
    ),
    Revert(
        "task 3: is_confusable compares against the raw string, so an NFD name is called a disguise",
        ADDON_TEXT_HYGIENE,
        '    return unicodedata.normalize("NFKC", text) != unicodedata.normalize("NFC", text)',
        '    return unicodedata.normalize("NFKC", text) != text',
        (f"{SESSIONT}::test_a_decomposed_accent_is_a_real_name_not_a_disguise",),
    ),
    Revert(
        "task 3: is_confusable is made constantly False, so a compatibility disguise is published",
        ADDON_TEXT_HYGIENE,
        '    return unicodedata.normalize("NFKC", text) != unicodedata.normalize("NFC", text)',
        '    return unicodedata.normalize("NFKC", text) != unicodedata.normalize("NFKC", text)',
        (
            f"{SESSIONT}::test_the_confusable_check_still_refuses_a_compatibility_disguise_after_nfc",
            f"{SESSIONT}::test_a_confusable_leaf_name_is_refused_rather_than_published",
        ),
    ),
    Revert(
        "task 3: the refresh demands the stale pair again, so the staleness flag re-arms forever",
        SERVER_CONNECTION,
        "    if learned is None or None in learned:",
        "    if learned is None or learned != observed:",
        (f"{CONNT}::test_a_refresh_that_learns_a_newer_session_than_the_one_observed_stops_retrying",),
    ),
    Revert(
        "task 3: an abort during the pre-swap drain leaves the swap's own client with nothing",
        ADDON_SERVER_CORE,
        '            if not receipt["answered"]:',
        "            if False:",
        (
            f"{THREADT}::test_an_abort_during_the_pre_swap_drain_answers_the_swaps_own_client",
            f"{THREADT}::test_an_abort_in_the_swaps_prologue_still_answers_the_swaps_own_client",
        ),
    ),
    Revert(
        "task 3: the answer receipt goes back to a prediction the caller writes up front",
        ADDON_SERVER_CORE,
        "        try:\n            self._drain_queue_into(superseded)",
        '        try:\n            receipt["answered"] = True\n            self._drain_queue_into(superseded)',
        (f"{THREADT}::test_an_abort_in_the_swaps_prologue_still_answers_the_swaps_own_client",),
    ),
    Revert(
        "task 3: a clean load_post_fail stops accounting for its load, so it reads as an abort",
        ADDON_SESSION,
        "    _STATE.load_failures += 1\n    _STATE.load_in_flight = False",
        "    _STATE.load_failures += 1",
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
    # --- Task 3 gate round: the sixth recurrence, and the one positive signal ---
    Revert(
        "task 3: addon_version goes back to whatever the socket sent, in a field declared list[int]",
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
        "task 3: capabilities is `list(... or [])` again, which validates the container and nothing in it",
        ADDON_MANAGER,
        '            capabilities=normalized_session_text_list(info.get("capabilities")),',
        '            capabilities=list(info.get("capabilities") or []),',
        (
            f"{AMT}::test_every_handshake_field_refuses_the_same_hostile_string[capabilities]",
            f"{AMT}::test_a_hostile_element_inside_a_list_field_is_dropped_not_published[capabilities]",
            f"{AMT}::{_LIST_SCALAR}[capabilities]",
        ),
    ),
    Revert(
        "task 3: blender_version is published raw, into get_addon_status and the handshake log line",
        ADDON_MANAGER,
        '            blender_version=normalized_session_text(info.get("blender_version")),',
        '            blender_version=info.get("blender_version"),',
        (
            f"{AMT}::test_every_handshake_field_refuses_the_same_hostile_string[blender_version]",
            f"{AMT}::test_the_handshake_log_line_cannot_be_forged_by_the_addon_payload",
        ),
    ),
    Revert(
        "task 3: writable_output_roots publishes its elements raw, into the set Tasks 5 and 6 match against",
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
        "task 3: current_filepath is published raw, so the hostile string reaches get_addon_status verbatim",
        ADDON_MANAGER,
        '            current_filepath=normalized_session_text(info.get("current_filepath")),',
        '            current_filepath=info.get("current_filepath"),',
        (f"{AMT}::test_every_handshake_field_refuses_the_same_hostile_string[current_filepath]",),
    ),
    Revert(
        "task 3: session_id is published raw, the field the whole hygiene module was written for",
        ADDON_MANAGER,
        '            session_id=normalized_session_id(info.get("session_id")),',
        '            session_id=info.get("session_id"),',
        (f"{AMT}::test_every_handshake_field_refuses_the_same_hostile_string[session_id]",),
    ),
    Revert(
        "task 3: session_epoch takes any JSON value, so a string epoch is published as an epoch",
        ADDON_MANAGER,
        '            session_epoch=normalized_session_epoch(info.get("session_epoch")),',
        '            session_epoch=info.get("session_epoch"),',
        (f"{AMT}::test_every_handshake_field_refuses_the_same_hostile_string[session_epoch]",),
    ),
    Revert(
        "task 3: session_indeterminate carries the payload itself rather than a verdict about it",
        ADDON_MANAGER,
        '            session_indeterminate=info.get("session_indeterminate") is True,',
        '            session_indeterminate=info.get("session_indeterminate"),',
        (f"{AMT}::test_every_handshake_field_refuses_the_same_hostile_string[session_indeterminate]",),
    ),
    Revert(
        "task 3: load_pre stops recording that a load began, so no abort is ever indeterminate",
        ADDON_SESSION,
        "    _STATE.load_in_flight = True",
        "    _STATE.load_in_flight = False",
        (
            f"{THREADT}::test_an_aborted_swap_invalidates_the_stamps_taken_during_its_load",
            f"{THREADT}::test_an_aborted_swap_publishes_an_indeterminate_note_every_client_can_poll",
            f"{THREADT}::test_a_retry_then_an_abort_part_way_through_the_second_load_is_indeterminate",
            f"{THREADT}::test_a_swap_that_answers_normally_is_not_answered_a_second_time_by_the_guard",
            f"{THREADT}::test_a_second_abort_that_began_no_load_does_not_bump_the_marker_again",
        ),
    ),
    Revert(
        "task 3: the load_pre handler is never registered, so Blender never tells the addon a load began",
        ADDON_SESSION,
        '    ("load_pre", _on_load_pre),\n',
        "",
        (f"{THREADT}::test_a_retry_then_an_abort_part_way_through_the_second_load_is_indeterminate",),
    ),
    Revert(
        "task 3: an abort stops accounting for its own load, so the next abort latches on the strength of it",
        ADDON_SESSION,
        (
            "    # call - so leaving the flag set would make the *next* abort, however\n"
            "    # unrelated, latch on the strength of this one.\n"
            "    _STATE.load_in_flight = False"
        ),
        (
            "    # call - so leaving the flag set would make the *next* abort, however\n"
            "    # unrelated, latch on the strength of this one."
        ),
        (f"{THREADT}::test_a_second_abort_that_began_no_load_does_not_bump_the_marker_again",),
    ),
    Revert(
        "task 3: _answer stops writing the receipt, so the guard answers a client that was already answered",
        ADDON_SERVER_CORE,
        '        if receipt is not None:\n            receipt["answered"] = True\n\n',
        "",
        (f"{THREADT}::test_a_swap_that_answers_normally_is_not_answered_a_second_time_by_the_guard",),
    ),
]


def collected_nodes() -> list[str]:
    """
    Ask pytest which nodes the task's new test files actually collect.

    Returns:
        list[str]: Every collected node id, plus the two nodes Task 1 added to
            an existing file.

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

    CPython keys a `__pycache__` entry on the source's **mtime in whole seconds
    and its size**. Two rows editing the same module inside one second with
    edits of the same length therefore produce a cache hit for the *previous*
    row's bytecode, and the second row silently tests nothing - it reports
    SURVIVOR while its edit was, in fact, applied to the file on disk.

    Found the hard way: `a successful SAVE bumps the epoch` and
    `a FAILED load bumps the epoch` both append exactly
    `\n    _STATE.session_epoch += 1` to `session.py`, and the second of them
    survived in a full run while failing correctly when run alone. That is the
    same class of false credit the whole harness exists to eliminate, so it is
    fixed here rather than worked around by reordering rows.

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
        SystemExit: If the anchor text is not in the file, which would make the
            row silently test nothing.

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
    Run exactly the named nodes and report whether *every* one of them failed.

    A non-zero exit code is not the question, and using it as the answer is how
    this harness credited a row that proved nothing. Two ways that goes wrong,
    both found by a critic reviewing the matrix rather than the code it guards:

    - A revert that leaves the file unparseable makes pytest exit 4 with a
      **collection error**. The node "fails", but for an `IndentationError`
      rather than for the behaviour the row names - so the row demonstrates
      nothing about the code under test. Row A2 did exactly this, undetected
      across three critic cycles. Any `error` in the summary is now a refusal.
    - A row naming several nodes was credited when **one** of them failed,
      which is the per-file false credit this harness was built to eliminate,
      surviving at row granularity. The failure count must now equal the number
      of nodes named.

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
            nothing passed. A row whose nodes partly passed has not been proven,
            and one that errored has been proven only to be broken.

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

    # Stamped before and after, because a matrix run takes ~30 minutes and the
    # box can change underneath it. A row that "survives" on a saturated machine
    # is a timing artefact, not evidence, and this is what lets a later reader
    # tell the two apart without reconstructing the machine's history.
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
