"""
Rows guarding the marked resend: a command whose outcome is unknown runs once, however often it is sent.

The add-on half keeps the replies to mutating commands by request id, with the session each ran
under, and answers a frame marked as a resend of that id under that session from them; any other
resend it refuses without running. The server half remembers the id and session of a call whose
outcome it never learned and resends an identical call under them, marked, but only to an add-on
whose handshake advertises `marked_resend`, and refuses it unsent once the session has moved.
What the add-on does against a real database is proven by `tests/blender_reply_cache_smoke.py`,
which no pytest node runs. The receive path's rows sit here too, since it reads every reply.

Label prefixes: `reply cache:`, `transport:`, `transport control:`.
"""

from .common import (
    ADDON_MANAGER,
    ADDON_REPLY_CACHE,
    ADDON_SERVER_CORE,
    AMT,
    CONNT,
    DISPT,
    REPLYCACHET,
    SERVER_CONNECTION,
    SERVER_DISPATCH,
    Revert,
)

_RUNS_ONCE = f"{REPLYCACHET}::test_a_resent_id_runs_the_handler_once_and_is_answered_with_the_first_reply"
_NEW_ID = f"{REPLYCACHET}::test_a_new_id_with_the_same_params_runs_again"
_EVICTED = f"{REPLYCACHET}::test_a_resend_whose_reply_was_evicted_is_refused_rather_than_run_again"
_READ_ONLY = f"{REPLYCACHET}::test_a_resent_read_is_refused_because_its_reply_is_not_kept"
_RESTART = f"{REPLYCACHET}::test_the_cache_survives_replacing_the_server"
_EPOCH = f"{REPLYCACHET}::test_a_resend_after_another_file_opened_is_refused_rather_than_run_in_it"
_OTHER_SESSION = (
    f"{REPLYCACHET}::test_a_resend_naming_another_session_than_its_first_run_is_refused[another-session]",
    f"{REPLYCACHET}::test_a_resend_naming_another_session_than_its_first_run_is_refused[no-session]",
)
_SWAP = f"{REPLYCACHET}::test_a_resent_file_swap_is_answered_from_the_cache"
_OVERSIZED = f"{REPLYCACHET}::test_an_oversized_reply_is_not_cached"
_IMAGE = f"{REPLYCACHET}::test_an_image_reply_is_not_cached"
_NOTICE = f"{REPLYCACHET}::test_a_replay_neither_consumes_nor_repeats_a_pending_scene_notice"
_ADVERTISED = f"{REPLYCACHET}::test_the_handshake_advertises_marked_resend"
_BOUNDS = f"{REPLYCACHET}::test_the_cache_keeps_sixty_four_replies_and_no_more_bytes_than_the_reply_cap"

_CARRIES_ID = f"{DISPT}::test_a_transport_error_names_the_request_it_was_sent_under_and_whether_it_left"
_SAME_ID = f"{DISPT}::test_an_identical_call_after_an_unknown_outcome_resends_under_the_same_id"
_MARKED = f"{DISPT}::test_a_resend_is_marked_with_the_session_its_first_attempt_was_sent_under"
_SESSION_MOVED = f"{DISPT}::test_a_resend_after_the_session_moved_is_refused_without_sending_it"
_HINT = f"{DISPT}::test_the_hint_says_an_identical_retry_is_answered_or_refused_when_the_addon_marks_resends"
_OTHER_PARAMS = f"{DISPT}::test_a_call_with_other_params_after_an_unknown_outcome_gets_a_new_id"
_KEY_ORDER = f"{DISPT}::test_the_same_params_in_another_key_order_are_the_same_call"
_NO_CAPABILITY = f"{DISPT}::test_without_the_capability_an_identical_call_gets_a_new_id_and_the_old_hint"
_EXPIRY = f"{DISPT}::test_a_remembered_id_expires_ten_minutes_after_its_last_unknown_outcome"
_LRU = f"{DISPT}::test_the_remembered_ids_are_evicted_least_recently_used_first_past_sixty_four"
_ANSWER_REPLY = f"{DISPT}::test_an_answer_forgets_the_id_so_a_later_identical_call_runs_anew[reply]"
_ANSWER_REFUSAL = f"{DISPT}::test_an_answer_forgets_the_id_so_a_later_identical_call_runs_anew[refusal]"
_UNSENT_RESEND = f"{DISPT}::test_a_resend_that_never_left_the_socket_keeps_the_id"

_HANDSHAKE = f"{AMT}::test_the_handshake_advertises_marked_resend_only_when_the_addon_says_true"
_READ_ONLY_PARSE = f"{AMT}::test_the_handshake_reads_only_a_list_of_command_names_as_read_only"

_RECONNECT = f"{DISPT}::test_a_reconnect_that_fails_is_reported_as_never_sent_and_worth_a_retry"
_NO_CONNECTION = f"{DISPT}::test_no_connection_at_all_is_reported_as_never_sent_and_worth_a_retry"
_READ_RETRY = f"{DISPT}::test_a_read_only_command_that_was_sent_and_lost_is_worth_a_retry"
_MUTATING_INSPECT = f"{DISPT}::test_a_mutating_command_lost_after_sending_still_asks_for_inspection"
_UNNAMED_READS = f"{DISPT}::test_an_addon_that_does_not_name_its_reads_gets_the_cautious_hint"
_NAMES_READS = f"{REPLYCACHET}::test_the_handshake_names_the_commands_that_never_mutate"

_SPREAD_FRAME = f"{CONNT}::test_a_frame_spread_over_many_recvs_arrives_whole_and_the_next_one_still_follows"
_TWO_FRAMES = f"{CONNT}::test_two_frames_in_one_recv_are_not_glued_together"

# Every node that sends an identical call after an unknown outcome and expects the first id back.
_REUSES_THE_ID = (_SAME_ID, _KEY_ORDER, _LRU, _ANSWER_REPLY, _ANSWER_REFUSAL, _UNSENT_RESEND, _EXPIRY)
# Every node whose resend the add-on cannot match to a held reply, and so must refuse unrun.
_REFUSED_UNRUN = (_EVICTED, _READ_ONLY, _EPOCH, *_OTHER_SESSION, _OVERSIZED, _IMAGE)

ROWS: list[Revert] = [
    # --- the add-on answers a marked resend from the first run, or refuses it unrun ---
    Revert(
        "reply cache: a resent id is never found, so every resend is refused",
        ADDON_REPLY_CACHE,
        "    entry = _CACHE.entries.get(request_id)\n",
        "    entry = None\n",
        (_RUNS_ONCE, _RESTART, _NOTICE, _SWAP, _BOUNDS),
    ),
    Revert(
        "reply cache control: a resend is answered with the latest reply, whichever command wrote it",
        ADDON_REPLY_CACHE,
        (
            "    entry = _CACHE.entries.get(request_id)\n"
            "    if entry is not None:\n"
            "        _CACHE.entries.move_to_end(request_id)\n"
            "    return entry\n"
        ),
        "    return next(reversed(_CACHE.entries.values()), None)\n",
        (_EVICTED, _BOUNDS),
    ),
    Revert(
        "reply cache control: every frame is taken for a resend, so no new request ever runs",
        ADDON_SERVER_CORE,
        (
            "        if self._RESEND_KEY not in command:\n"
            "            return False\n"
            "        marker = self._session_marker()\n"
            "        sent_under = self._resend_marker(command[self._RESEND_KEY])\n"
        ),
        (
            "        marker = self._session_marker()\n"
            "        sent_under = self._resend_marker(command.get(self._RESEND_KEY))\n"
        ),
        (_NEW_ID,),
    ),
    Revert(
        # The defect this file exists for: an evicted reply, or a file swapped in since, let the
        # resent id run again - the cube was created twice, or created in the other file.
        "reply cache: a resend the add-on cannot match to a held reply runs as a new request",
        ADDON_SERVER_CORE,
        (
            "        reason = self._RESEND_NOT_HELD_REASON if sent_under == marker else self._RESEND_MOVED_REASON\n"
            '        logger.info("Refusing resent request %s: %s", command.get("id"), reason)\n'
            '        self._answer(command, client, {"status": "error", '
            '"message": self._RESEND_REFUSAL.format(reason=reason)})\n'
            "        return True\n"
        ),
        "        return False\n",
        _REFUSED_UNRUN,
    ),
    Revert(
        "reply cache: a held reply answers a resend naming another session than the one it ran under",
        ADDON_SERVER_CORE,
        "        if held is not None and held.ran_under == sent_under:",
        "        if held is not None:",
        _OTHER_SESSION,
    ),
    Revert(
        "reply cache: a swap's reply is filed under the session it opened, so its resend is refused",
        ADDON_SERVER_CORE,
        "        cache = ran_under if self._caches_reply(command) else None",
        "        cache = self._session_marker() if self._caches_reply(command) else None",
        (_SWAP,),
    ),
    Revert(
        "reply cache: every refused resend blames a session change, though most met an evicted reply",
        ADDON_SERVER_CORE,
        "        reason = self._RESEND_NOT_HELD_REASON if sent_under == marker else self._RESEND_MOVED_REASON",
        "        reason = self._RESEND_MOVED_REASON",
        (_EVICTED, _OVERSIZED, _IMAGE),
    ),
    Revert(
        "reply cache control: every refused resend blames an evicted reply, though the file was replaced",
        ADDON_SERVER_CORE,
        "        reason = self._RESEND_NOT_HELD_REASON if sent_under == marker else self._RESEND_MOVED_REASON",
        "        reason = self._RESEND_NOT_HELD_REASON",
        (_EPOCH,),
    ),
    Revert(
        "reply cache: a read is cached too, so a resent read answers with the scene as it was",
        ADDON_SERVER_CORE,
        '        return not self.is_read_only_command(command.get("type"), command.get("params", {}))',
        "        return True",
        (_READ_ONLY,),
    ),
    Revert(
        "reply cache: Stop/Start Server empties the cache, the very restart that prompts the resend",
        ADDON_SERVER_CORE,
        "        self._handlers_by_gate: dict[tuple[bool, ...], Mapping[str, Callable[..., object]]] = {}\n",
        (
            "        self._handlers_by_gate: dict[tuple[bool, ...], Mapping[str, Callable[..., object]]] = {}\n"
            "        reply_cache._CACHE.entries.clear()\n"
        ),
        (_RESTART,),
    ),
    Revert(
        "reply cache: a reply written against another file still answers a resend after the epoch moves",
        ADDON_REPLY_CACHE,
        "        if self.marker != marker:",
        "        if self.marker is None:",
        (_EPOCH,),
    ),
    Revert(
        "reply cache: the size-limit error frame is cached in place of the reply the client never got",
        ADDON_SERVER_CORE,
        '        if cache and faithful and reply_cache.caches_result(response.get("result")):',
        '        if cache and reply_cache.caches_result(response.get("result")):',
        (_OVERSIZED,),
    ),
    Revert(
        "reply cache: an inline image reply is cached",
        ADDON_REPLY_CACHE,
        '    return not (isinstance(result, dict) and "image_base64" in result)',
        "    return True",
        (_IMAGE,),
    ),
    Revert(
        "reply cache: a replay consumes the pending scene notice and attaches it to the old reply",
        ADDON_SERVER_CORE,
        '        self._answer(command, client, {**json.loads(cached), "replayed": True})',
        (
            "        self._answer(command, client, scene_watch.annotated_response("
            'command.get("type"), {**json.loads(cached), "replayed": True}, session_swap=False))'
        ),
        (_NOTICE,),
    ),
    Revert(
        "reply cache: the handshake stops advertising marked_resend",
        ADDON_SERVER_CORE,
        '            "marked_resend": True,',
        '            "marked_resend": False,',
        (_ADVERTISED,),
    ),
    Revert(
        "reply cache: the add-on still advertises idempotent_resend, which an older server reads as leave "
        "to resend unmarked",
        ADDON_SERVER_CORE,
        '            "marked_resend": True,\n',
        '            "marked_resend": True,\n            "idempotent_resend": True,\n',
        (_ADVERTISED,),
    ),
    Revert(
        "reply cache: the entry bound is dropped",
        ADDON_REPLY_CACHE,
        "(len(_CACHE.entries) >= CAPACITY or _CACHE.total_bytes + len(payload) > max_bytes):",
        "(_CACHE.total_bytes + len(payload) > max_bytes):",
        (_BOUNDS,),
    ),
    Revert(
        "reply cache: the byte bound is dropped",
        ADDON_REPLY_CACHE,
        "(len(_CACHE.entries) >= CAPACITY or _CACHE.total_bytes + len(payload) > max_bytes):",
        "(len(_CACHE.entries) >= CAPACITY):",
        (_BOUNDS,),
    ),
    Revert(
        "reply cache: a reply over the cap on its own empties the cache to make room for it",
        ADDON_REPLY_CACHE,
        "    if not isinstance(request_id, str) or len(payload) > max_bytes:",
        "    if not isinstance(request_id, str):",
        (_BOUNDS,),
    ),
    # --- the transport error says which id was sent, and whether it left; a resend is marked ---
    Revert(
        "reply cache: a transport error no longer carries the id it was sent under",
        SERVER_CONNECTION,
        "            exc.request_id = command_id\n",
        "",
        (_CARRIES_ID, _SAME_ID),
    ),
    Revert(
        "reply cache: a send that failed before its newline claims the command was sent",
        SERVER_CONNECTION,
        "    sent = False\n",
        "",
        (_CARRIES_ID,),
    ),
    Revert(
        "reply cache: the connection ignores the id it is asked to resend under",
        SERVER_CONNECTION,
        "        command_id = resend.request_id if resend is not None else uuid.uuid4().hex",
        "        command_id = uuid.uuid4().hex",
        (_CARRIES_ID, *_REUSES_THE_ID),
    ),
    Revert(
        "reply cache: a resend goes out unmarked, so the add-on runs it as a new request once its reply is gone",
        SERVER_CONNECTION,
        (
            "        if resend is not None:\n"
            "            session_id, session_epoch = resend.session_marker\n"
            '            command["resend"] = {"session_id": session_id, "session_epoch": session_epoch}\n'
        ),
        "",
        (_MARKED,),
    ),
    # --- the server resends an identical call under the first id, marked, or refuses it ---
    Revert(
        "reply cache: the dispatch never resends under a remembered id",
        SERVER_DISPATCH,
        "        resend = _resend_ids.take(key) if key is not None else None",
        "        resend = None",
        (*_REUSES_THE_ID, _MARKED, _SESSION_MOVED),
    ),
    Revert(
        "reply cache: a resend goes out after the session moved, to a file its first attempt was never sent to",
        SERVER_DISPATCH,
        "        elif resend.session_marker != _session_marker():",
        "        elif False:",
        (_SESSION_MOVED,),
    ),
    Revert(
        "reply cache: the hint still says to inspect before retrying when a retry is answered or refused",
        SERVER_DISPATCH,
        '            raise ToolError(f"{exc} {_RESEND_HINT}") from exc',
        '            raise ToolError(f"{exc} {_OUTCOME_UNKNOWN_HINT}") from exc',
        (_HINT,),
    ),
    Revert(
        "reply cache: an id is resent to an add-on that would run it as a new request",
        SERVER_DISPATCH,
        "    if handshake is None or not handshake.marked_resend:",
        "    if handshake is None:",
        (_NO_CAPABILITY,),
    ),
    Revert(
        "reply cache control: a call with other params is resent under another call's id",
        SERVER_DISPATCH,
        '        return command, json.dumps(params or {}, sort_keys=True, separators=(",", ":"))',
        '        return command, ""',
        (_OTHER_PARAMS,),
    ),
    Revert(
        "reply cache: the same params in another key order count as another call",
        SERVER_DISPATCH,
        '        return command, json.dumps(params or {}, sort_keys=True, separators=(",", ":"))',
        '        return command, json.dumps(params or {}, sort_keys=False, separators=(",", ":"))',
        (_KEY_ORDER,),
    ),
    Revert(
        "reply cache: a remembered id never expires",
        SERVER_DISPATCH,
        "        if self._clock() >= expires_at:",
        "        if False:",
        (_EXPIRY,),
    ),
    Revert(
        "reply cache: past sixty-four ids the newest is dropped instead of the least recently used",
        SERVER_DISPATCH,
        "                self._ids.popitem(last=False)",
        "                self._ids.popitem(last=True)",
        (_LRU,),
    ),
    Revert(
        "reply cache: a resend leaves its id remembered, so a deliberate repeat after the answer is a replay",
        SERVER_DISPATCH,
        "            entry = self._ids.pop(key, None)",
        "            entry = self._ids.get(key)",
        (_ANSWER_REPLY, _ANSWER_REFUSAL),
    ),
    Revert(
        "reply cache: a resend that never left the socket forgets the id the first attempt may have run under",
        SERVER_DISPATCH,
        (
            "            # This resend never left, and the attempt before it may still have run.\n"
            "            _resend_ids.remember(key, resend)\n"
        ),
        "            pass\n",
        (_UNSENT_RESEND,),
    ),
    # --- the handshake field the server gates on ---
    Revert(
        "reply cache: a truthy non-boolean marked_resend is believed",
        ADDON_MANAGER,
        '            marked_resend=info.get("marked_resend") is True,',
        '            marked_resend=bool(info.get("marked_resend")),',
        (_HANDSHAKE,),
    ),
    Revert(
        "reply cache: the handshake never reads marked_resend",
        ADDON_MANAGER,
        '            marked_resend=info.get("marked_resend") is True,',
        "            marked_resend=False,",
        (_HANDSHAKE,),
    ),
    Revert(
        "reply cache: the earlier idempotent_resend is still believed, so an add-on that runs evicted resends "
        "is sent them",
        ADDON_MANAGER,
        '            marked_resend=info.get("marked_resend") is True,',
        '            marked_resend=info.get("marked_resend") is True or info.get("idempotent_resend") is True,',
        (_HANDSHAKE,),
    ),
    # --- every reply is read in one pass however many recvs it spans ---
    Revert(
        "transport: the terminator search skips the chunk just appended, so a reply spread over recvs never ends",
        SERVER_CONNECTION,
        "            searched = len(buffer)\n            buffer += chunk\n",
        "            buffer += chunk\n            searched = len(buffer)\n",
        (_SPREAD_FRAME, _TWO_FRAMES),
    ),
    Revert(
        "transport: bytes carried over from the last recv are not searched, so a frame already held waits for more",
        SERVER_CONNECTION,
        '        end = buffer.find(b"\\n")\n',
        "        end = -1\n",
        (_TWO_FRAMES,),
    ),
    # --- what the agent is told when nothing reached Blender, or only a read was lost ---
    Revert(
        "transport: a reconnect that fails is a bare ConnectionError again, so neither hint is given",
        SERVER_CONNECTION,
        "            raise BlenderCommandNotSentError(\n"
        '                "Not connected to Blender: the connection was lost and could not be reopened"\n',
        "            raise ConnectionError(\n"
        '                "Not connected to Blender: the connection was lost and could not be reopened"\n',
        (_RECONNECT,),
    ),
    Revert(
        "transport: the first connection failing is an untyped Exception again",
        SERVER_CONNECTION,
        '            raise BlenderCommandNotSentError("Could not connect to Blender. Make sure',
        '            raise Exception("Could not connect to Blender. Make sure',
        (_NO_CONNECTION,),
    ),
    Revert(
        "transport: a lost read-only command is told to inspect the scene like a mutation",
        SERVER_DISPATCH,
        '        if _is_read_only(command):\n            raise ToolError(f"{exc} {_READ_ONLY_RETRY_HINT}") from exc\n',
        "",
        (_READ_RETRY,),
    ),
    Revert(
        # The dangerous direction: calling every command a read tells an agent to repeat a mutation.
        "transport control: every command is treated as read-only",
        SERVER_DISPATCH,
        "    return handshake is not None and command in handshake.read_only_commands",
        "    return True",
        (_MUTATING_INSPECT, _UNNAMED_READS),
    ),
    Revert(
        "reply cache: the handshake names every command read-only",
        ADDON_SERVER_CORE,
        '            "read_only_commands": sorted(name for name in handlers if self.command_spec(name).read_only),',
        '            "read_only_commands": sorted(handlers),',
        (_NAMES_READS,),
    ),
    Revert(
        "reply cache: a malformed read_only_commands field is read as names",
        ADDON_MANAGER,
        "    if not isinstance(value, list) or not all(isinstance(name, str) for name in value):",
        "    if not isinstance(value, (list, str)):",
        (_READ_ONLY_PARSE,),
    ),
]
