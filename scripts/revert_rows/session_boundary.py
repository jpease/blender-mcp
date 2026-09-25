"""
Rows guarding what crosses the session boundary.

The allowlist a published leaf name is held to, the latch, the abort guard and the send
floor, the server boundary, and the library name.

Label prefixes: `text hygiene:`, `session:`, `barrier:`, `handshake:`, `get_addon_status:`,
`rehandshake:`.
"""

from .common import (
    ADDON_BLEND_FILES,
    ADDON_MANAGER,
    ADDON_SERVER_CORE,
    ADDON_SESSION,
    ADDON_TEXT_HYGIENE,
    AMT,
    CONNT,
    CORET,
    HOSTILE_LIB_NAME,
    HOSTILE_LIB_NAME_IDS,
    SERVER_CONNECTION,
    SERVER_CORE_TOOL,
    SERVER_TEXT_HYGIENE,
    SESSIONT,
    THREADT,
    Revert,
)

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

ROWS: list[Revert] = [
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
]
