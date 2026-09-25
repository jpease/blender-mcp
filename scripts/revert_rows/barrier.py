"""
Rows guarding the file-swap barrier and the client's reaction to it.

The barrier itself, the handshake and re-handshake, send liveness, the fail-closed drain,
the producer scan, and how a reduced path says which rule reduced it.

Label prefixes: `barrier:`, `handshake:`, `rehandshake:`, `session:`, `text hygiene:`,
`get_addon_status:`.
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
    EVASION,
    FLT,
    HOSTILE_LIB,
    NFKC_BACKSLASH_LIB,
    SERVER_CONNECTION,
    SERVER_CORE_TOOL,
    SESSIONT,
    TEST_THREADING_FILE,
    THREADT,
    Revert,
)

ROWS: list[Revert] = [
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
        "text hygiene: the link allowlist admits any character, so a hidden or foreign one ships inside a link",
        ADDON_TEXT_HYGIENE,
        "    return component not in NOT_A_LINK_COMPONENT and set(component) <= LINK_COMPONENT_ALLOWED",
        "    return component not in NOT_A_LINK_COMPONENT",
        (
            f"{HOSTILE_LIB}[zero-width-hidden traversal-//.\\u200b./.\\u200b./clients/acme/canon.blend-forbidden11]",
            f"{HOSTILE_LIB}[format character inside a component-//libs/\\u200bcanon.blend-forbidden12]",
            f"{SESSIONT}::test_a_link_published_whole_names_nothing_above_its_own_shot",
            f"{SESSIONT}::test_a_contained_link_with_a_character_outside_the_allowlist_is_an_unsafe_component"
            "[zero-width space]",
            f"{SESSIONT}::test_a_contained_link_with_a_character_outside_the_allowlist_is_an_unsafe_component"
            "[accented directory]",
            f"{SESSIONT}::test_a_contained_link_with_a_character_outside_the_allowlist_is_an_unsafe_component"
            "[CJK directory]",
        ),
    ),
    Revert(
        "text hygiene: the link predicate goes back to a blocklist, which is a list of the attacks already known",
        ADDON_TEXT_HYGIENE,
        "    return component not in NOT_A_LINK_COMPONENT and set(component) <= LINK_COMPONENT_ALLOWED",
        '    return component not in NOT_A_LINK_COMPONENT and not any(m in component for m in ("/", "\\\\", ":"))',
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
            # `is_relative` and the reason code depend on this clause alone; containment
            # still keeps `///Users/...` from being published. See `_ROOTED_TWICE`.
            f"{SESSIONT}::test_a_rooted_relative_prefix_is_not_reported_as_relative",
            f"{SESSIONT}::test_each_refusal_the_publisher_makes_has_its_own_reason_code",
        ),
    ),
    Revert(
        "text hygiene: the publisher returns Blender's spelling instead of the link it derived",
        ADDON_BLEND_FILES,
        '        return {key: whole, f"{key}_redacted": False, f"{key}_redaction_reason": None}',
        '        return {key: text, f"{key}_redacted": False, f"{key}_redaction_reason": None}',
        (
            f"{SESSIONT}::test_an_absolute_library_inside_the_tree_is_published_as_a_link_from_the_shot",
            f"{SESSIONT}::test_a_directory_inside_the_tree_is_published_whole_with_its_trailing_separator",
        ),
    ),
    Revert(
        "text hygiene: the library summary loses the hygiene its sibling field has, on both branches",
        ADDON_BLEND_FILES,
        "        **published_path_fields(filepath, frame=frame),",
        '        "filepath": filepath,\n'
        '        "filepath_redacted": False,\n'
        '        "filepath_redaction_reason": None,',
        (
            f"{HOSTILE_LIB}[ANSI escape, relative branch-//shots/\\x1b[31mx.blend-forbidden1]",
            f"{HOSTILE_LIB}[ANSI escape, absolute branch-/mnt/studio/\\x1b[31mx.blend-forbidden2]",
            f"{HOSTILE_LIB}[500 characters, relative branch-//{'a' * 500}.blend-forbidden3]",
            f"{HOSTILE_LIB}[500 characters, absolute branch-/mnt/{'b' * 500}.blend-forbidden4]",
        ),
    ),
    # --- a reduced path says so, and says which rule reduced it ---
    #
    # `published_path_fields` is the one publisher every reply reports a path through, and it
    # returns three fields rather than one because `"canon.blend"` alone is indistinguishable
    # from a broken link. The flag is only worth its bytes while it tracks the publisher on
    # both branches, the reason codes only while each refusal keeps its own, and neither may
    # take over what `is_missing` alone reports.
    Revert(
        "text hygiene: a path published exactly as it came is flagged as a redaction, so the flag is constant",
        ADDON_BLEND_FILES,
        '        return {key: whole, f"{key}_redacted": False, f"{key}_redaction_reason": None}',
        '        return {key: whole, f"{key}_redacted": True, f"{key}_redaction_reason": None}',
        (f"{SESSIONT}::test_a_library_inside_the_project_tree_is_published_whole_and_says_it_was_not_reduced",),
    ),
    Revert(
        "text hygiene: a leaf is published unflagged, so a deliberate reduction reads as a broken link again",
        ADDON_BLEND_FILES,
        '        f"{key}_redacted": True,',
        '        f"{key}_redacted": False,',
        (
            f"{SESSIONT}::test_a_library_outside_the_project_tree_reports_its_leaf_as_a_redaction_not_as_a_defect",
            f"{SESSIONT}::test_every_reduced_library_filepath_is_flagged_with_a_stable_reason",
        ),
    ),
    Revert(
        "text hygiene: breakage is reported only for the libraries whose path was publishable",
        ADDON_BLEND_FILES,
        '        "is_missing": bool(getattr(library, "is_missing", False)),',
        '        "is_missing": bool(getattr(library, "is_missing", False))\n'
        '        and not published_path_fields(filepath, frame=frame)["filepath_redacted"],',
        (f"{SESSIONT}::test_a_missing_link_reports_is_missing_whether_or_not_its_path_was_redacted",),
    ),
    Revert(
        "text hygiene: every refusal inside the tree reports OUTSIDE_ROOTS, so `too long` reads as `not yours to see`",
        ADDON_BLEND_FILES,
        "    if len(whole) > MAX_REPORTED_LINK_CHARS:\n"
        "        return None, REDACTION_TOO_LONG\n"
        "    if not all(admissible_link_component(part) for part in parts):\n"
        "        return None, REDACTION_UNSAFE_COMPONENT\n",
        "    if len(whole) > MAX_REPORTED_LINK_CHARS or not all(admissible_link_component(part) for part in parts):\n"
        "        return None, REDACTION_OUTSIDE_ROOTS\n",
        (
            f"{SESSIONT}::test_each_refusal_the_publisher_makes_has_its_own_reason_code",
            f"{SESSIONT}::test_a_contained_link_with_a_character_outside_the_allowlist_is_an_unsafe_component"
            "[zero-width space]",
        ),
    ),
    Revert(
        "text hygiene: an unresolvable path is called outside the roots, as though it had been placed",
        ADDON_BLEND_FILES,
        "    if candidate is None:\n        return None, REDACTION_UNRESOLVABLE",
        "    if candidate is None:\n        return None, REDACTION_OUTSIDE_ROOTS",
        (
            f"{SESSIONT}::test_each_refusal_the_publisher_makes_has_its_own_reason_code",
            f"{SESSIONT}::test_a_relative_link_in_a_session_never_saved_is_unresolvable",
        ),
    ),
    Revert(
        "text hygiene: a withheld directory reports its containment verdict and hides that its leaf went too",
        ADDON_BLEND_FILES,
        '        f"{key}_redaction_reason": REDACTION_DIRECTORY if is_directory else reason,',
        '        f"{key}_redaction_reason": reason,',
        (f"{SESSIONT}::test_each_refusal_the_publisher_makes_has_its_own_reason_code",),
    ),
    Revert(
        "text hygiene: containment is skipped, so any path that resolves is published whole",
        ADDON_BLEND_FILES,
        "    if not frame.trees or not inside_roots(candidate, frame.trees):\n"
        "        return None, REDACTION_OUTSIDE_ROOTS",
        "    if False:\n        return None, REDACTION_OUTSIDE_ROOTS",
        (
            f"{SESSIONT}::test_a_link_that_climbs_out_of_the_roots_is_withheld_as_outside_roots",
            f"{SESSIONT}::test_a_symlink_out_of_the_tree_is_judged_by_where_it_leads",
            f"{SESSIONT}::test_the_library_summary_reports_identity_without_the_asset_library_layout",
            f"{SESSIONT}::test_a_library_outside_the_project_tree_reports_its_leaf_as_a_redaction_not_as_a_defect",
            f"{HOSTILE_LIB}[traversal out of the shot-//../../../clients/acme-merger/lib/canon.blend-forbidden0]",
        ),
    ),
    Revert(
        "text hygiene: no tree reads as every tree, so a session never saved publishes any absolute path",
        ADDON_BLEND_FILES,
        "    if not frame.trees or not inside_roots(candidate, frame.trees):",
        "    if not inside_roots(candidate, frame.trees):",
        (f"{SESSIONT}::test_a_session_never_saved_publishes_an_in_root_path_absolute",),
    ),
    Revert(
        "text hygiene: the configured roots are ignored, so a canon folder beside the shot is withheld",
        ADDON_BLEND_FILES,
        "    return PathFrame(blend_directory, link_directory, roots or ((link_directory,) if link_directory else ()))",
        "    return PathFrame(blend_directory, link_directory, (link_directory,) if link_directory else ())",
        (
            f"{SESSIONT}::test_a_canon_folder_beside_the_shot_inside_the_roots_is_published_with_its_parent_step",
            f"{SESSIONT}::test_an_absolute_library_inside_the_tree_is_published_as_a_link_from_the_shot",
            f"{SESSIONT}::test_a_session_never_saved_publishes_an_in_root_path_absolute",
            f"{FLT}::test_save_shot_records_each_ingredient_as_a_link_from_the_file_it_writes",
        ),
    ),
    Revert(
        "text hygiene: symlinks are not followed, so a link to the vault inside the shot reads as the shot's",
        ADDON_BLEND_FILES,
        "        return canonical_path(os.path.normpath(text))",
        "        return os.path.normpath(text)",
        (f"{SESSIONT}::test_a_symlink_out_of_the_tree_is_judged_by_where_it_leads",),
    ),
    Revert(
        "text hygiene: a // path in a session never saved resolves against the working directory",
        ADDON_BLEND_FILES,
        "        if body is None or not blend_directory:\n            return None",
        "        if body is None:\n            return None",
        (f"{SESSIONT}::test_a_relative_link_in_a_session_never_saved_is_unresolvable",),
    ),
    Revert(
        "text hygiene: a trailing separator is dropped, so a render-output directory reads as a file prefix",
        ADDON_BLEND_FILES,
        '    trailing = text.endswith(("/", os.sep))',
        "    trailing = False",
        (f"{SESSIONT}::test_a_directory_inside_the_tree_is_published_whole_with_its_trailing_separator",),
    ),
    Revert(
        "text hygiene: an unset path is reduced like any other, so `nothing is set here` reads as a hidden one",
        ADDON_BLEND_FILES,
        "    if blank_is_unset and not text.strip():\n"
        '        return {key: "", f"{key}_redacted": False, f"{key}_redaction_reason": None}\n',
        "",
        (f"{SESSIONT}::test_an_unset_path_is_published_empty_and_is_not_a_redaction",),
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
        # The row's node was renamed away to a test of the *raising* path, which this guard is
        # not on, so the row ran nothing and survived. This is the test of the guard itself:
        # a re-handshake that completes and reports half a session pair.
        (f"{CONNT}::test_a_refresh_that_reports_no_usable_session_leaves_the_staleness_signal_standing",),
    ),
    Revert(
        "rehandshake: a re-handshake whose round trip dies clears the staleness signal for good",
        SERVER_CONNECTION,
        '        logger.warning(f"Re-handshake never completed ({exc}); staying stale so the next command retries")\n'
        "        _session_marker_stale.set()\n",
        '        logger.warning(f"Re-handshake never completed ({exc}); staying stale so the next command retries")\n',
        (f"{CONNT}::test_a_refresh_whose_round_trip_dies_leaves_the_staleness_signal_standing",),
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
]
