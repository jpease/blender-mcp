"""
Rows guarding every handshake field's refusal of a hostile value, and `load_pre`.

Label prefixes: `handshake:`, `session:`, `barrier:`.
"""

from .common import ADDON_MANAGER, ADDON_SERVER_CORE, ADDON_SESSION, AMT, LIST_SCALAR, THREADT, Revert

ROWS: list[Revert] = [
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
            f"{AMT}::{LIST_SCALAR}[capabilities]",
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
            f"{AMT}::{LIST_SCALAR}[writable_output_roots]",
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
]
