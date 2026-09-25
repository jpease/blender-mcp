"""
Rows guarding what a client's arguments are held to.

The preflight parameter gate, the strict argument models, the lighting preview's own preflight,
and the dispatch-surface snapshot.

Label prefixes: `handshake:`, `transport:`, `transport control:`, `strict args:`, `lighting:`,
`addon surface:`.
"""

from .common import (
    ADDON_CAPABILITY_INTROSPECTION,
    ADDON_MANAGER,
    AMT,
    CAPT,
    CONNT,
    LIGHTT,
    SERVER_CONNECTION,
    SERVER_LIGHTING_CONSTRUCTION_TOOL,
    SERVER_LIGHTING_RENDERING_TOOL,
    SERVER_STRICT_ARGS,
    SERVER_TOOLS_INIT,
    STRICTT,
    SURFT,
    TEST_ADDON_SURFACE_FILE,
    Revert,
)

# Named because inline the parametrized node ids pass the line limit.
STUDIO_PREFLIGHT = "test_create_studio_lighting_refuses_a_bad_preview_before_building_the_rig"

ROWS: list[Revert] = [
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
        # was being written - neutralise the call and every node in the file but its two guards
        # goes red, the non-finite ones included; these four are the unknown-key half.
        "strict args: the hardening pass never runs, so an unknown top-level argument is dropped again",
        SERVER_TOOLS_INIT,
        "\nharden_tool_arguments(mcp)\n",
        "\nif False:\n    harden_tool_arguments(mcp)\n",
        (
            f"{STRICTT}::test_a_misspelled_argument_is_refused_instead_of_dropped",
            f"{STRICTT}::test_the_wrongly_nested_patch_from_the_incident_is_refused",
            f"{STRICTT}::test_every_registered_tool_refuses_an_unknown_argument[all]",
            f"{STRICTT}::test_every_registered_tool_refuses_an_unknown_argument[None]",
        ),
    ),
    # --- ... and a non-finite float in any top-level argument, as StrictModel does in a payload ---
    Revert(
        # Only `extra` is written, as before the fix: the unknown-key nodes above stay green, so
        # what goes red is the non-finite half alone, not the whole hardening pass.
        "strict args: the hardening pass forbids unknown keys but leaves NaN and infinity accepted",
        SERVER_STRICT_ARGS,
        "        arg_model.model_config.update(strict)\n",
        '        arg_model.model_config["extra"] = "forbid"\n',
        (
            f"{STRICTT}::test_every_float_argument_in_the_catalog_refuses_a_non_finite_value[nan]",
            f"{STRICTT}::test_every_float_argument_in_the_catalog_refuses_a_non_finite_value[inf]",
            f"{STRICTT}::test_every_float_argument_in_the_catalog_refuses_a_non_finite_value[-inf]",
            f"{STRICTT}::test_create_primitive_object_refuses_a_non_finite_vector_before_dispatch[location]",
            f"{STRICTT}::test_create_primitive_object_refuses_a_non_finite_vector_before_dispatch[rotation]",
            f"{STRICTT}::test_create_primitive_object_refuses_a_non_finite_vector_before_dispatch[dimensions]",
        ),
    ),
    # --- create_studio_lighting checks its preview's arguments before it builds the rig ---
    Revert(
        # The preview still refuses every one of these, but only after the rig's three lights
        # exist, and the rig's handler then refuses the corrected retry's member names.
        "lighting: create_studio_lighting builds its rig before checking the preview's arguments",
        SERVER_LIGHTING_CONSTRUCTION_TOOL,
        "    validate_preview_arguments("
        "preview_engine, frame, preview_samples, confirm_long_render, preview_output_path)\n",
        "",
        (
            f"{LIGHTT}::{STUDIO_PREFLIGHT}[relative-path]",
            f"{LIGHTT}::{STUDIO_PREFLIGHT}[not-png]",
            f"{LIGHTT}::{STUDIO_PREFLIGHT}[unconfirmed-cycles-samples]",
            f"{LIGHTT}::{STUDIO_PREFLIGHT}[frame-beyond-blender]",
        ),
    ),
    Revert(
        # Only Blender refused an out-of-range frame, so the rig was built before it said so.
        "lighting: the preview preflight lets a frame beyond Blender's range through to the rig",
        SERVER_LIGHTING_RENDERING_TOOL,
        "    if not -_MAX_FRAME <= frame <= _MAX_FRAME:\n"
        '        raise ToolError(f"frame must be an integer in [{-_MAX_FRAME}, {_MAX_FRAME}]")\n',
        "",
        (f"{LIGHTT}::{STUDIO_PREFLIGHT}[frame-beyond-blender]",),
    ),
    # --- a dispatch-surface change cannot reach a user behind an unchanged protocol number ---
    Revert(
        # The anchor carries the current number, so `just anchors` reports this row BROKEN on the
        # next protocol bump. That is the cheapest possible reminder that the row's claim - the
        # surface moved and the number did not - has to be re-pointed at the new pair.
        "addon surface: the dispatch table moved while the protocol number stayed where it was",
        ADDON_MANAGER,
        "EXPECTED_ADDON_PROTOCOL_VERSION = 48",
        "EXPECTED_ADDON_PROTOCOL_VERSION = 47",
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
]
