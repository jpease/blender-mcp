"""
Rows guarding the notice each add-on reply carries when the scene changed between commands.

The add-on half counts undos and redos, flags an outside edit, and attaches one warning to the
next reply; the server half lifts that warning into the envelope, including for the tools that
build their envelope by hand from a reply's fields or from several replies.

Label prefixes: `scene watch:`.
"""

from .common import (
    ADDON_INIT,
    ADDON_LIQUID_SIMULATION,
    ADDON_SCENE_WATCH,
    ADDON_SERVER_CORE,
    RENDT,
    RNT,
    SERVER_APP,
    SERVER_CORE_TOOL,
    SERVER_ENVELOPE,
    SERVER_IMAGE_CAPTURE,
    SERVER_POLYHAVEN_TOOL,
    SERVER_RENDERING_TOOL,
    SERVER_SKETCHFAB_TOOL,
    SIT,
    SWT,
    Revert,
)

_COUNTS = f"{SWT}::test_undo_and_redo_between_commands_are_counted_and_a_load_resets_them"
_QUIET = f"{SWT}::test_edits_during_a_command_or_within_the_quiet_period_are_ignored"
_FIVE = f"{SWT}::test_an_outside_edit_names_five_distinct_objects_and_counts_the_rest"
_ONCE = f"{SWT}::test_the_notice_is_given_once_and_the_state_resets_after_the_reply"
_NO_OBJECT = f"{SWT}::test_an_edit_to_no_object_still_flags_the_scene_without_naming_anything"
_SELECTION = f"{SWT}::test_a_flagless_scene_update_such_as_a_selection_is_not_an_edit"
_JOB = f"{SWT}::test_an_object_an_async_job_is_writing_is_not_an_outside_edit"
_LIQUID = f"{SWT}::test_the_liquid_bake_marks_its_domain_with_the_watchs_job_marker"
_TRANSACTED = f"{SWT}::test_a_transacted_command_reply_carries_the_notice_beside_its_own"
_ERROR = f"{SWT}::test_an_error_reply_carries_the_notice_in_its_message"
_SWAPS = tuple(
    f"{SWT}::test_a_session_swap_resets_the_state_without_a_notice[{swap}]" for swap in ("open_shot", "reset_session")
)
_HANDSHAKE = f"{SWT}::test_the_handshake_neither_carries_nor_consumes_the_notice"
_FAILURE_SHAPE = f"{SWT}::test_a_read_only_failure_shape_carries_the_notice_in_its_error_text"
_REGISTRATION = f"{SWT}::test_registration_is_idempotent_and_unregistration_removes_every_handler"
_ADDON_WIRING = f"{SWT}::test_the_addon_registers_and_unregisters_the_watch"


def _lift(case: str) -> str:
    return f"{RNT}::test_every_replys_notice_reaches_the_envelope_once_and_leaves_the_data[{case}]"


_REFUSAL = f"{RNT}::test_a_refusal_built_from_the_status_probe_still_carries_its_notice"
_ANIMATION = f"{RENDT}::test_orchestrated_animation_reports_every_replys_warnings_once"
_INSTRUCTIONS = f"{SIT}::test_the_instructions_say_an_outside_change_is_one_coarse_signal"

ROWS: list[Revert] = [
    Revert(
        "scene watch: an undo between commands is not counted",
        ADDON_SCENE_WATCH,
        "        _WATCH.undos += 1\n",
        "        pass\n",
        (_COUNTS, _ONCE, _TRANSACTED, _HANDSHAKE, _FAILURE_SHAPE),
    ),
    Revert(
        "scene watch: a redo between commands is not counted",
        ADDON_SCENE_WATCH,
        "        _WATCH.redos += 1\n",
        "        pass\n",
        (_COUNTS, _ERROR),
    ),
    Revert(
        "scene watch: a load leaves the previous file's changes pending",
        ADDON_SCENE_WATCH,
        '        _unused: Blender passes a second positional argument, always None.\n\n    """\n    _WATCH.reset()\n',
        '        _unused: Blender passes a second positional argument, always None.\n\n    """\n',
        (_COUNTS,),
    ),
    Revert(
        "scene watch: an update during a command is taken for an outside edit",
        ADDON_SCENE_WATCH,
        "    if _WATCH.executing:\n        return True\n",
        "",
        (_QUIET,),
    ),
    Revert(
        "scene watch: a command's own re-evaluation just after it answered is taken for an outside edit",
        ADDON_SCENE_WATCH,
        "    return _WATCH.command_ended_at is not None and time.monotonic() - _WATCH.command_ended_at "
        "< QUIET_PERIOD_SECONDS\n",
        "    return False\n",
        (_QUIET,),
    ),
    Revert(
        "scene watch: every edited object is named, however many",
        ADDON_SCENE_WATCH,
        "    if len(_WATCH.named_objects) < _NAMED_OBJECTS_LIMIT:\n",
        "    if True:\n",
        (_FIVE,),
    ),
    Revert(
        "scene watch: an object edited twice is named and counted twice",
        ADDON_SCENE_WATCH,
        '    if original.id_type != "OBJECT" or original.session_uid in _WATCH.edited_objects:\n',
        '    if original.id_type != "OBJECT":\n',
        (_FIVE,),
    ),
    Revert(
        "scene watch: a selection's flagless Scene update is taken for an edit",
        ADDON_SCENE_WATCH,
        '    if original.id_type == "SCENE" and not any(getattr(update, flag) for flag in _UPDATE_FLAGS):\n',
        "    if False:\n",
        (_SELECTION,),
    ),
    Revert(
        "scene watch: an object an async job is writing is taken for an outside edit",
        ADDON_SCENE_WATCH,
        '    return not (original.id_type == "OBJECT" and original.get(JOB_MARKER_KEY))\n',
        "    return True\n",
        (_JOB,),
    ),
    Revert(
        "scene watch: the liquid bake marks its domain under a key the watch does not read",
        ADDON_LIQUID_SIMULATION,
        "from ...scene_watch import JOB_MARKER_KEY as _PENDING_BAKE_KEY\n",
        '_PENDING_BAKE_KEY = "blendermcp_liquid_pending"\n',
        (_LIQUID,),
    ),
    Revert(
        "scene watch: an edit flag hides the undo that re-evaluated every object",
        ADDON_SCENE_WATCH,
        "    if _WATCH.undos or _WATCH.redos:\n",
        "    if (_WATCH.undos or _WATCH.redos) and not _WATCH.edited:\n",
        (_ONCE,),
    ),
    Revert(
        "scene watch: an outside edit to no object raises no notice",
        ADDON_SCENE_WATCH,
        "    if not _WATCH.edited:\n        return None\n",
        "    if not _WATCH.edited or not _WATCH.named_objects:\n        return None\n",
        (_NO_OBJECT,),
    ),
    Revert(
        "scene watch: a notice already given is given again on the next reply",
        ADDON_SCENE_WATCH,
        "    else:\n        return response\n    _WATCH.reset()\n    return response\n",
        "    else:\n        return response\n    return response\n",
        (_ONCE, _ERROR),
    ),
    Revert(
        "scene watch: an error reply drops the notice",
        ADDON_SCENE_WATCH,
        "        response = {**response, \"message\": f\"{response.get('message', '')} {notice}\".strip()}\n",
        "        return response\n",
        (_ERROR,),
    ),
    Revert(
        "scene watch: a failure-shaped result keeps the notice only where the server never reads it",
        ADDON_SCENE_WATCH,
        '        if result.get("error") and not result.get("cancelled"):\n',
        "        if False:\n",
        (_FAILURE_SHAPE,),
    ),
    Revert(
        "scene watch: a session swap reports the changes the file it replaced carried",
        ADDON_SCENE_WATCH,
        "    if session_swap:\n        _WATCH.reset()\n        return response\n",
        "",
        _SWAPS,
    ),
    Revert(
        "scene watch: the server's own handshake consumes the notice",
        ADDON_SCENE_WATCH,
        "    if cmd_type in _SERVER_PROBES:\n        return response\n",
        "",
        (_HANDSHAKE,),
    ),
    Revert(
        "scene watch: replies are never annotated",
        ADDON_SERVER_CORE,
        "        return scene_watch.annotated_response(cmd_type, response, "
        "session_swap=self.command_spec(cmd_type).session_swap)\n",
        "        return response\n",
        (_COUNTS, _TRANSACTED, _ERROR, _FAILURE_SHAPE),
    ),
    Revert(
        "scene watch: a running command is not marked, so its own updates are outside edits",
        ADDON_SERVER_CORE,
        "        scene_watch.command_started()\n",
        "",
        (_QUIET,),
    ),
    Revert(
        "scene watch: registering twice attaches each handler twice",
        ADDON_SCENE_WATCH,
        "        if handler not in handler_list:\n            handler_list.append(handler)\n",
        "        handler_list.append(handler)\n",
        (_REGISTRATION,),
    ),
    Revert(
        "scene watch: unregistering leaves the handlers attached",
        ADDON_SCENE_WATCH,
        '    """Detach the handlers, including any a previous cycle stacked."""\n',
        '    """Detach the handlers, including any a previous cycle stacked."""\n    return\n',
        (_REGISTRATION, _ADDON_WIRING),
    ),
    Revert(
        "scene watch: the add-on never attaches the watch",
        ADDON_INIT,
        "    scene_watch.register_handlers()\n",
        "    pass\n",
        (_ADDON_WIRING,),
    ),
    Revert(
        "scene watch: the add-on leaves the watch attached when it unregisters",
        ADDON_INIT,
        "    scene_watch.unregister_handlers()\n",
        "",
        (_ADDON_WIRING,),
    ),
    Revert(
        "scene watch: the envelope keeps a reply's warnings inside its data",
        SERVER_ENVELOPE,
        '    if isinstance(data, dict) and isinstance(data.get("warnings"), list):\n',
        "    if False:\n",
        (_lift("read-only call_blender"), _lift("mutating call_blender")),
    ),
    Revert(
        "scene watch: get_polyhaven_categories drops the status probe's notice",
        SERVER_POLYHAVEN_TOOL,
        '            warnings=[*status.get("warnings", []), *result.get("warnings", [])],\n',
        '            warnings=result.get("warnings", []),\n',
        (_lift("get_polyhaven_categories"),),
    ),
    Revert(
        "scene watch: the disabled-integration refusal drops the status probe's notice",
        SERVER_POLYHAVEN_TOOL,
        '                        *status.get("warnings", []),\n',
        "",
        (_REFUSAL,),
    ),
    Revert(
        "scene watch: list_polyhaven_assets drops its reply's notice",
        SERVER_POLYHAVEN_TOOL,
        '            },\n            warnings=result.get("warnings", []),\n',
        "            },\n",
        (_lift("list_polyhaven_assets"),),
    ),
    Revert(
        "scene watch: search_sketchfab_models drops its reply's notice",
        SERVER_SKETCHFAB_TOOL,
        '            },\n            warnings=result.get("warnings", []),\n',
        "            },\n",
        (_lift("search_sketchfab_models"),),
    ),
    Revert(
        "scene watch: get_sketchfab_model_preview drops its reply's notice",
        SERVER_SKETCHFAB_TOOL,
        'ok(metadata, warnings=result.get("warnings", []))',
        "ok(metadata)",
        (_lift("get_sketchfab_model_preview"),),
    ),
    Revert(
        "scene watch: an image tool's envelope drops its reply's notice",
        SERVER_IMAGE_CAPTURE,
        'ok(metadata(result), warnings=list(result.get("warnings") or []))',
        "ok(metadata(result))",
        (_lift("get_viewport_screenshot"), _lift("inspect_render_output")),
    ),
    Revert(
        "scene watch: get_integration_status drops every provider's notice",
        SERVER_CORE_TOOL,
        "        warnings=notices,\n",
        "",
        (_lift("get_integration_status (all)"),),
    ),
    Revert(
        "scene watch: an orchestrated animation reports only the last frame's warnings",
        SERVER_RENDERING_TOOL,
        '                    *(warning for reply in replies for warning in reply.get("warnings") or []),\n',
        '                    *((replies[-1].get("warnings") or []) if replies else []),\n',
        (_ANIMATION,),
    ),
    Revert(
        "scene watch: an orchestrated animation drops the plan's warnings",
        SERVER_RENDERING_TOOL,
        '                    *plan.get("warnings", []),\n',
        "",
        (_ANIMATION,),
    ),
    Revert(
        "scene watch: an orchestrated animation drops the persist call's warnings",
        SERVER_RENDERING_TOOL,
        '        persist_warnings = list(persist_reply.get("warnings") or [])\n',
        "        persist_warnings = []\n",
        (_ANIMATION,),
    ),
    Revert(
        "scene watch: the instructions never say the notice is one coarse, per-add-on signal",
        SERVER_APP,
        "   - A warning also flags an undo, redo or outside edit since the last command any session sent\n",
        "   - A warning also flags an undo or redo\n",
        (_INSTRUCTIONS,),
    ),
]
