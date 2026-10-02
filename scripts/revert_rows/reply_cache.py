"""
Rows guarding the same-id resend: a command whose outcome is unknown runs once, however often it is sent.

The add-on half keeps the replies to mutating commands by request id and answers a resent id from
them; the server half remembers the id of a call whose outcome it never learned and resends an
identical call under it, but only to an add-on whose handshake advertises `idempotent_resend`.
What the add-on does against a real database is proven by `tests/blender_reply_cache_smoke.py`,
which no pytest node runs.

Label prefixes: `reply cache:`.
"""

from .common import (
    ADDON_MANAGER,
    ADDON_REPLY_CACHE,
    ADDON_SERVER_CORE,
    AMT,
    DISPT,
    REPLYCACHET,
    SERVER_CONNECTION,
    SERVER_DISPATCH,
    Revert,
)

_RUNS_ONCE = f"{REPLYCACHET}::test_a_resent_id_runs_the_handler_once_and_is_answered_with_the_first_reply"
_NEW_ID = f"{REPLYCACHET}::test_a_new_id_with_the_same_params_runs_again"
_READ_ONLY = f"{REPLYCACHET}::test_a_read_only_command_is_run_again_rather_than_cached"
_RESTART = f"{REPLYCACHET}::test_the_cache_survives_replacing_the_server"
_EPOCH = f"{REPLYCACHET}::test_an_epoch_move_clears_the_cache"
_OVERSIZED = f"{REPLYCACHET}::test_an_oversized_reply_is_not_cached"
_IMAGE = f"{REPLYCACHET}::test_an_image_reply_is_not_cached"
_NOTICE = f"{REPLYCACHET}::test_a_replay_neither_consumes_nor_repeats_a_pending_scene_notice"
_ADVERTISED = f"{REPLYCACHET}::test_the_handshake_advertises_idempotent_resend"
_BOUNDS = f"{REPLYCACHET}::test_the_cache_keeps_sixty_four_replies_and_no_more_bytes_than_the_reply_cap"

_CARRIES_ID = f"{DISPT}::test_a_transport_error_names_the_request_it_was_sent_under_and_whether_it_left"
_SAME_ID = f"{DISPT}::test_an_identical_call_after_an_unknown_outcome_resends_under_the_same_id"
_HINT = f"{DISPT}::test_the_outcome_unknown_hint_says_an_identical_retry_is_safe_when_the_addon_dedupes"
_OTHER_PARAMS = f"{DISPT}::test_a_call_with_other_params_after_an_unknown_outcome_gets_a_new_id"
_KEY_ORDER = f"{DISPT}::test_the_same_params_in_another_key_order_are_the_same_call"
_NO_CAPABILITY = f"{DISPT}::test_without_the_capability_an_identical_call_gets_a_new_id_and_the_old_hint"
_EXPIRY = f"{DISPT}::test_a_remembered_id_expires_ten_minutes_after_its_last_unknown_outcome"
_LRU = f"{DISPT}::test_the_remembered_ids_are_evicted_least_recently_used_first_past_sixty_four"
_ANSWER_REPLY = f"{DISPT}::test_an_answer_forgets_the_id_so_a_later_identical_call_runs_anew[reply]"
_ANSWER_REFUSAL = f"{DISPT}::test_an_answer_forgets_the_id_so_a_later_identical_call_runs_anew[refusal]"
_UNSENT_RESEND = f"{DISPT}::test_a_resend_that_never_left_the_socket_keeps_the_id"

_HANDSHAKE = f"{AMT}::test_the_handshake_advertises_idempotent_resend_only_when_the_addon_says_true"

# Every node that sends an identical call after an unknown outcome and expects the first id back.
_REUSES_THE_ID = (_SAME_ID, _KEY_ORDER, _LRU, _ANSWER_REPLY, _ANSWER_REFUSAL, _UNSENT_RESEND, _EXPIRY)

ROWS: list[Revert] = [
    # --- the add-on answers a resent id from the first run ---
    Revert(
        "reply cache: a resent id is never found, so the command runs again",
        ADDON_REPLY_CACHE,
        "    payload = _CACHE.entries.get(request_id)\n",
        "    payload = None\n",
        (_RUNS_ONCE, _RESTART, _NOTICE),
    ),
    Revert(
        "reply cache control: any id is answered with the latest reply, so new work is never run",
        ADDON_REPLY_CACHE,
        (
            "    payload = _CACHE.entries.get(request_id)\n"
            "    if payload is not None:\n"
            "        _CACHE.entries.move_to_end(request_id)\n"
            "    return payload\n"
        ),
        "    return next(reversed(_CACHE.entries.values()), None)\n",
        (_NEW_ID,),
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
        "reply cache: a reply written against another file still answers after the epoch moves",
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
        "reply cache: the handshake stops advertising idempotent_resend",
        ADDON_SERVER_CORE,
        '            "idempotent_resend": True,',
        '            "idempotent_resend": False,',
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
    # --- the transport error says which id was sent, and whether it left ---
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
        "        command_id = request_id or uuid.uuid4().hex",
        "        command_id = uuid.uuid4().hex",
        (_CARRIES_ID, *_REUSES_THE_ID),
    ),
    # --- the server resends an identical call under the first id ---
    Revert(
        "reply cache: the dispatch never resends under a remembered id",
        SERVER_DISPATCH,
        "    request_id = _resend_ids.take(key) if key is not None else None",
        "    request_id = None",
        _REUSES_THE_ID,
    ),
    Revert(
        "reply cache: the hint still says to inspect before retrying when a retry is safe",
        SERVER_DISPATCH,
        '            raise ToolError(f"{exc} {_RESEND_HINT}") from exc',
        '            raise ToolError(f"{exc} {_OUTCOME_UNKNOWN_HINT}") from exc',
        (_HINT,),
    ),
    Revert(
        "reply cache: an id is resent to an add-on that would run it twice",
        SERVER_DISPATCH,
        "    if handshake is None or not handshake.idempotent_resend:",
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
            "            _resend_ids.remember(key, request_id)\n"
        ),
        "            pass\n",
        (_UNSENT_RESEND,),
    ),
    # --- the handshake field the server gates on ---
    Revert(
        "reply cache: a truthy non-boolean idempotent_resend is believed",
        ADDON_MANAGER,
        '            idempotent_resend=info.get("idempotent_resend") is True,',
        '            idempotent_resend=bool(info.get("idempotent_resend")),',
        (_HANDSHAKE,),
    ),
    Revert(
        "reply cache: the handshake never reads idempotent_resend",
        ADDON_MANAGER,
        '            idempotent_resend=info.get("idempotent_resend") is True,',
        "            idempotent_resend=False,",
        (_HANDSHAKE,),
    ),
]
