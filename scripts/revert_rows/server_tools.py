"""
Rows guarding the server-side tools and the per-reply byte budget.

The file-lifecycle and linking tools, the one dispatch every tool shares, the catalog, and
the budget that bounds every reply.

Label prefixes: `server tools:`, `animation:`, `pose:`, `pose control:`, `reply budget:`.
"""

from .common import (
    ADDON_ANIMATION,
    ADDON_POSING,
    ANIMT,
    BUNT,
    DISPT,
    ENVT,
    POSET,
    SERVER_ANIMATION_TOOL,
    SERVER_BUNDLES,
    SERVER_DISPATCH,
    SERVER_DOCUMENTATION,
    SERVER_ENVELOPE,
    SERVER_FILE_LIFECYCLE_TOOL,
    SFLT,
    TDT,
    TEST_BUNDLES_FILE,
    Revert,
)

# One tool per module that once re-wrapped the shared dispatch's failures in its own prefixed
# `except Exception`; each reaches the client only through `_dispatch.send_command`, so reverting
# that one mapping reverts what these tools report.
_WRAPPER_FREE_TOOLS = (
    "create_primitive_object",
    "mesh_extrude",
    "nd_boolean",
    "nd_capture_utils",
    "sync_data_name",
    "get_mesh_data",
    "get_viewport_screenshot",
    "get_integration_status",
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

ROWS: list[Revert] = [
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
        "server tools: reset_session's confirm_reset default is unpinned to True",
        SERVER_FILE_LIFECYCLE_TOOL,
        "async def reset_session(ctx: Context, confirm_reset: bool = False) -> dict:",
        "async def reset_session(ctx: Context, confirm_reset: bool = True) -> dict:",
        (f"{SFLT}::test_reset_session_defaults",),
    ),
    Revert(
        "server tools: reset_session does not forward confirm_reset",
        SERVER_FILE_LIFECYCLE_TOOL,
        '    return await call_blender("reset_session", {"confirm_reset": confirm_reset})',
        '    return await call_blender("reset_session", {"confirm_reset": False})',
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
        "server tools: link_canon_library does not forward detail",
        SERVER_FILE_LIFECYCLE_TOOL,
        '            "scene_name": scene_name,\n            "detail": detail,\n',
        '            "scene_name": scene_name,\n            "detail": False,\n',
        (f"{SFLT}::test_link_canon_library_forwards_every_parameter",),
    ),
    Revert(
        "server tools: link_canon_library's relative=False default is unpinned to True",
        SERVER_FILE_LIFECYCLE_TOOL,
        "    relative: bool = False,\n    scene_name: str | None = None,\n    detail: bool = False,\n) -> dict:",
        "    relative: bool = True,\n    scene_name: str | None = None,\n    detail: bool = False,\n) -> dict:",
        (f"{SFLT}::test_link_canon_library_defaults",),
    ),
    Revert(
        "server tools: create_override drops scene_name before forwarding it",
        SERVER_FILE_LIFECYCLE_TOOL,
        '        {"collection_uid": collection_uid, "scene_name": scene_name, "detail": detail},',
        '        {"collection_uid": collection_uid, "scene_name": None, "detail": detail},',
        (f"{SFLT}::test_create_override_forwards_every_parameter",),
    ),
    Revert(
        "server tools: create_override's scene_name=None default is unpinned",
        SERVER_FILE_LIFECYCLE_TOOL,
        "    ctx: Context, collection_uid: int, scene_name: str | None = None, detail: bool = False\n",
        '    ctx: Context, collection_uid: int, scene_name: str | None = "Scene", detail: bool = False\n',
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
        '{"library_uids": library_uids, "confirm_unlink": confirm_unlink, "purge_orphans": purge_orphans},',
        '{"library_uids": library_uids, "confirm_unlink": confirm_unlink, "purge_orphans": False},',
        (f"{SFLT}::test_unlink_libraries_forwards_every_parameter",),
    ),
    Revert(
        "server tools: unlink_libraries' confirm_unlink=False default is unpinned to True",
        SERVER_FILE_LIFECYCLE_TOOL,
        "    confirm_unlink: bool = False,\n    purge_orphans: bool = False,\n) -> dict:",
        "    confirm_unlink: bool = True,\n    purge_orphans: bool = False,\n) -> dict:",
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
        (
            f"{DISPT}::test_an_operation_failure_reaches_the_client_as_blenders_own_message",
            *(
                f"{DISPT}::test_a_tool_reports_blenders_refusal_as_blender_worded_it[{tool}]"
                for tool in _WRAPPER_FREE_TOOLS
            ),
        ),
    ),
    Revert(
        "server tools: a transport failure loses the reconnect advice",
        SERVER_DISPATCH,
        '        raise ToolError(f"{exc} {_RETRY_HINT}") from exc\n',
        "        raise ToolError(str(exc)) from exc\n",
        (
            f"{DISPT}::test_a_transport_failure_says_the_connection_was_dropped_and_is_worth_a_retry",
            *(
                f"{DISPT}::test_a_tool_keeps_the_retry_hint_on_a_dropped_connection[{tool}]"
                for tool in _WRAPPER_FREE_TOOLS
            ),
        ),
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
        "SHOT_MODE_BYTE_CEILING = 277_769",
        # One byte below the *measured* payload (277,769). The ceiling sits exactly on it now, but
        # a ceiling with headroom would let a revert to itself-minus-one pass and prove nothing.
        "SHOT_MODE_BYTE_CEILING = 277_768",
        (f"{BUNT}::test_shot_mode_payload_stays_under_its_ceiling",),
    ),
    Revert(
        "server tools: the default ceiling reverted one byte below the measured payload",
        TEST_BUNDLES_FILE,
        "DEFAULT_MODE_BYTE_CEILING = 92_624",
        # Same rule: one byte below the measured core payload (92,624), not below the ceiling.
        "DEFAULT_MODE_BYTE_CEILING = 92_623",
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
        "server tools: confirm_free no longer marks a tool destructive",
        SERVER_DOCUMENTATION,
        '        "confirm_free",\n',
        "",
        (f"{TDT}::test_a_flag_that_frees_a_cache_or_replaces_a_file_marks_its_tool_destructive[confirm_free]",),
    ),
    Revert(
        "server tools: confirm_overwrite no longer marks a tool destructive",
        SERVER_DOCUMENTATION,
        '        "confirm_overwrite",\n',
        "",
        (f"{TDT}::test_a_flag_that_frees_a_cache_or_replaces_a_file_marks_its_tool_destructive[confirm_overwrite]",),
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
        "animation: a cycled curve reports no period, the way it did when a shot's arms stopped striding",
        ADDON_ANIMATION,
        '        "period_frames": period,\n',
        '        "period_frames": None,\n',
        (f"{ANIMT}::test_a_cycle_reports_the_period_each_curve_will_actually_repeat",),
    ),
    Revert(
        "animation: a finite cycle count stops the repeat without saying at which frame",
        ADDON_ANIMATION,
        '    if cycles_after and mode_after != "NONE":\n',
        "    if False:\n",
        (f"{ANIMT}::test_a_finite_cycle_count_says_where_the_repeat_stops",),
    ),
    Revert(
        "pose: a key landing past an existing cycle stretches its period silently again",
        ADDON_POSING,
        "                _cycle_extension_warnings(action, prepared, frame)\n"
        "                + _inert_rotation_warnings(prepared, space, witnesses)\n",
        "                []\n                + _inert_rotation_warnings(prepared, space, witnesses)\n",
        (f"{POSET}::test_keying_past_a_cycle_says_the_period_it_just_changed",),
    ),
    Revert(
        "pose control: every key on a cyclic curve warns, not only one landing outside the cycle",
        ADDON_POSING,
        "            if first - _FRAME_TOLERANCE <= frame <= last + _FRAME_TOLERANCE:\n",
        "            if False:\n",
        (f"{POSET}::test_keying_inside_an_existing_cycle_warns_about_nothing",),
    ),
    Revert(
        "pose: the per-bone cycle notices are unbounded, so a 500-bone pose spends the reply budget on them",
        ADDON_POSING,
        "    for bone in affected[:_MAX_CYCLE_WARNINGS]:\n",
        "    for bone in affected:\n",
        (f"{POSET}::test_a_whole_rig_keyed_past_its_cycles_counts_the_bones_it_cannot_name",),
    ),
    Revert(
        "animation: a cut cycle page says only to narrow the scope, never which parameter narrows it",
        SERVER_ANIMATION_TOOL,
        "warnings=[] if data_path_prefix else [_UNSCOPED_CYCLE_WARNING],\n",
        "warnings=[],\n",
        (f"{ANIMT}::test_an_unscoped_cycle_names_the_parameter_that_narrows_it",),
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
]
