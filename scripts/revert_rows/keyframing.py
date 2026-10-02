"""
Rows guarding one keyframe-style vocabulary, one action per ID, and the list caps stated twice.

Label prefixes: `key style:`, `action assignment:`, `action assignment control:`, `size caps:`.
"""

from .common import (
    ADDON_ACTION_ASSIGNMENT,
    ADDON_ANIMATION,
    ADDON_KEY_STYLE,
    ADDON_OBJECT_ANIMATION,
    ADDON_POSING,
    ADDON_REACH,
    ADDON_SERVER_CORE,
    ANIMT,
    CAPST,
    CRTT,
    KEYSTYLET,
    OANIMT,
    REACHT,
    ROOT,
    SERVER_ANIMATION_TOOL,
    SERVER_OBJECT_ANIMATION_TOOL,
    Revert,
)

_SWEEP = f"{CAPST}::test_every_advertised_list_cap_is_one_the_addon_enforces_at_the_same_number"
_OVER = f"{CAPST}::test_a_list_over_its_cap_is_refused"
ADDON_LIST_CAPS = ROOT / "src/blender_mcp/bundled/addon/list_caps.py"

_CAP_ROWS = [
    Revert(
        "size caps: the add-on's cap drifts one past the one the schema advertises",
        ADDON_LIST_CAPS,
        '    ("edit_keyframes", "edits"): 1_000,\n',
        '    ("edit_keyframes", "edits"): 1_001,\n',
        (_SWEEP,),
    ),
    Revert(
        # The drift the sweep exists for: a capped list on the server with no add-on row is a
        # list nothing at the socket bounds.
        "size caps: a capped list the schema advertises has no add-on row",
        ADDON_LIST_CAPS,
        '    ("probe_bone_axis", "axes"): 6,\n',
        "",
        (_SWEEP,),
    ),
    Revert(
        "size caps: the add-on stops refusing a list longer than its cap",
        ADDON_LIST_CAPS,
        "        if isinstance(value, (list, tuple, dict)) and len(value) > cap:\n",
        "        if False:\n",
        (
            f"{_OVER}[top-level]",
            f"{_OVER}[mapping-leaf]",
            f"{CAPST}::test_dispatch_refuses_an_oversized_list_before_the_handler_runs",
        ),
    ),
    Revert(
        "size caps: a capped list inside each record of a list is never reached",
        ADDON_LIST_CAPS,
        "    if isinstance(value, (list, tuple)):\n        for index, item in enumerate(value):\n",
        "    if False:\n        for index, item in enumerate(value):\n",
        (f"{_OVER}[nested-through-a-list]",),
    ),
    Revert(
        "size caps: a capped field inside a record is never reached",
        ADDON_LIST_CAPS,
        "    elif isinstance(value, dict) and steps[0] in value:\n",
        "    elif False:\n",
        (
            f"{_OVER}[nested-through-a-list]",
            f"{_OVER}[nested-in-a-mapping]",
            f"{_OVER}[mapping-leaf]",
        ),
    ),
    Revert(
        "size caps: a list exactly at its cap is refused",
        ADDON_LIST_CAPS,
        "        if isinstance(value, (list, tuple, dict)) and len(value) > cap:\n",
        "        if isinstance(value, (list, tuple, dict)) and len(value) >= cap:\n",
        (f"{CAPST}::test_a_list_at_its_cap_or_absent_passes",),
    ),
    Revert(
        "size caps: a cap row is keyed by a command the dispatcher never sees",
        ADDON_LIST_CAPS,
        '    ("edit_keyframes", "edits"): 1_000,\n',
        '    ("edit_keyframe", "edits"): 1_000,\n',
        (f"{CAPST}::test_every_cap_row_names_a_command_and_a_parameter_its_handler_takes", _SWEEP),
    ),
    Revert(
        "size caps: the schema's cap drifts from the add-on's",
        SERVER_ANIMATION_TOOL,
        "    edits: Annotated[list[KeyframeEdit], Field(min_length=1, max_length=1000)],\n",
        "    edits: Annotated[list[KeyframeEdit], Field(min_length=1, max_length=1001)],\n",
        (f"{CAPST}::test_the_sweep_reaches_top_level_nested_and_mapping_caps", _SWEEP),
    ),
    Revert(
        "size caps: dispatch runs the handler without checking any cap",
        ADDON_SERVER_CORE,
        "            refuse_oversized_lists(cmd_type, params)\n",
        "            pass\n",
        (f"{CAPST}::test_dispatch_refuses_an_oversized_list_before_the_handler_runs",),
    ),
    Revert(
        "size caps: the add-on's batched pose total drifts one past the server's",
        ADDON_POSING,
        "_MAX_KEYED_POSE_ENTRIES = 2000\n",
        "_MAX_KEYED_POSE_ENTRIES = 2001\n",
        (f"{CAPST}::test_the_batched_pose_total_is_the_one_the_addon_enforces",),
    ),
]

# The four keying tools' `assign_action=False`: the clip takes the keys, the ID keeps its own.
_UNASSIGNED_CLIP = (
    f"{CRTT}::test_an_unassigned_clip_is_keyed_while_the_rig_keeps_its_root_motion",
    f"{CRTT}::test_an_unassigned_clip_leaves_a_rig_that_had_no_animation_data_without_any",
    f"{REACHT}::test_an_unassigned_reach_clip_keys_without_displacing_the_root_motion",
    f"{OANIMT}::test_an_unassigned_clip_hands_the_object_back_its_action_and_transform",
    f"{OANIMT}::test_an_unassigned_clip_leaves_an_object_with_no_animation_data_without_any",
)

_KEYS_BACK = f"{OANIMT}::test_a_batch_failing_part_way_hands_a_reused_action_back_its_keys"

ROWS: list[Revert] = [
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
    # --- a clip keyed with assign_action=False leaves the ID on its own action ---------------
    Revert(
        # Restoring only on error is today's assign_action=True contract; under False the
        # rig is left on the clip, which is exactly the displacement the field exists to avoid.
        "action assignment: an unassigned clip leaves the ID driven by the clip",
        ADDON_ACTION_ASSIGNMENT,
        "    if not only_on_error:\n        refusal = restore()\n",
        "    if False:\n        refusal = restore()\n",
        _UNASSIGNED_CLIP,
    ),
    Revert(
        "action assignment: borrowing the assignment leaves an empty animation block on the ID",
        ADDON_ACTION_ASSIGNMENT,
        "            id_owner.animation_data_clear()\n",
        "            pass\n",
        (_UNASSIGNED_CLIP[1], _UNASSIGNED_CLIP[4]),
    ),
    Revert(
        "action assignment: an unassigned pose clip is still refused for displacing the root motion",
        ADDON_POSING,
        "                confirm_displace=confirm_displace_action or not assign_action,\n",
        "                confirm_displace=confirm_displace_action,\n",
        (_UNASSIGNED_CLIP[0],),
    ),
    Revert(
        "action assignment: an unassigned reach clip is still refused for displacing the root motion",
        ADDON_REACH,
        "                confirm_displace=confirm_displace_action or not assign_action,\n",
        "                confirm_displace=confirm_displace_action,\n",
        (_UNASSIGNED_CLIP[2],),
    ),
    Revert(
        "action assignment: an unassigned object clip is still refused for displacing the root motion",
        ADDON_OBJECT_ANIMATION,
        "confirm_displace=confirm_displace or not assign_action\n",
        "confirm_displace=confirm_displace\n",
        (_UNASSIGNED_CLIP[3],),
    ),
    Revert(
        "action assignment: an unassigned object clip leaves the object on the keyed values",
        ADDON_OBJECT_ANIMATION,
        "    if not assign_action:\n        borrowed.enter_context(_restored_object_transform(owner))\n",
        "    if False:\n        borrowed.enter_context(_restored_object_transform(owner))\n",
        (_UNASSIGNED_CLIP[3],),
    ),
    Revert(
        "action assignment: edit_keyframes assigns the clip it was told only to key",
        ADDON_ANIMATION,
        "        if action_name and assign_action:\n",
        "        if action_name:\n",
        (f"{ANIMT}::test_an_unassigned_clip_is_edited_without_touching_the_active_action",),
    ),
    Revert(
        "action assignment: edit_keyframes forwards an unassigned clip that names no action",
        SERVER_ANIMATION_TOOL,
        "    if not assign_action and action_name is None:\n",
        "    if False:\n",
        (f"{ANIMT}::test_an_unassigned_clip_without_a_name_is_refused_before_the_socket",),
    ),
    Revert(
        "action assignment: keyframe_object_transform forwards an unassigned clip that names no action",
        SERVER_OBJECT_ANIMATION_TOOL,
        "    if not assign_action and action_name is None:\n",
        "    if False:\n",
        (f"{OANIMT}::test_an_unassigned_clip_without_a_name_is_refused_before_the_socket",),
    ),
    Revert(
        # Reported as data rather than as a warning on the first step of key-then-ADD_STRIP; a
        # count that is never zero hides the one clip the next save drops.
        "action assignment: a clip nothing uses is reported as having a user",
        ADDON_ANIMATION,
        '            "keyed_action_users": selected.users,\n',
        '            "keyed_action_users": max(selected.users, 1),\n',
        (f"{ANIMT}::test_an_unassigned_clip_is_edited_without_touching_the_active_action",),
    ),
    # --- one guard, one field: every tool that assigns an action asks before displacing keys ---
    Revert(
        "action assignment control: edit_keyframes and manage_animation_action displace keyed motion unasked",
        ADDON_ANIMATION,
        "    if not confirm_displace:\n        refuse_displacement(owner, action.name)\n",
        "    if False:\n        refuse_displacement(owner, action.name)\n",
        (f"{ANIMT}::test_every_assigning_tool_refuses_to_displace_an_action_that_holds_keys",),
    ),
    Revert(
        "action assignment control: a bake displaces the keyed motion it may be baking from, unasked",
        ADDON_ANIMATION,
        "        if not confirm_displace_action:\n            # Asked before sampling",
        "        if False:\n            # Asked before sampling",
        (f"{ANIMT}::test_a_bake_refuses_to_displace_keyed_motion_before_it_samples",),
    ),
    Revert(
        # The old animation-tool guard refused displacing any action at all; the keying tools'
        # rule, now everyone's, refuses only one that holds keys.
        "action assignment control: displacing an empty action is refused as if it held keys",
        ADDON_ACTION_ASSIGNMENT,
        "    if not held:\n",
        "    if False:\n",
        (f"{ANIMT}::test_displacing_an_action_with_no_keys_needs_no_confirmation",),
    ),
    # --- a slot the restore cannot put back is reported, never swallowed ---
    Revert(
        "action assignment: a slot the restore could not put back is dropped from the reply",
        ADDON_ACTION_ASSIGNMENT,
        "            warnings.append(refusal)\n",
        "            pass\n",
        (f"{ANIMT}::test_a_slot_the_restore_cannot_put_back_is_reported_not_swallowed",),
    ),
    Revert(
        "action assignment: a slot refused while unwinding an error is lost behind that error",
        ADDON_ACTION_ASSIGNMENT,
        '            raise RuntimeError(f"{original}. {refusal}") from original\n',
        "            raise\n",
        (f"{ANIMT}::test_a_slot_refused_while_unwinding_an_error_travels_with_that_error",),
    ),
    # --- a keying call that fails part way hands a reused action back its keys ---
    Revert(
        "action assignment: a failed batch leaves its earlier keys in the reused action",
        ADDON_ACTION_ASSIGNMENT,
        "            _restore_keys(id_owner, action, slot, paths, snapshot)\n",
        "            pass\n",
        (_KEYS_BACK,),
    ),
    Revert(
        "action assignment: keyframe_object_transform keys without a snapshot to fail back to",
        ADDON_OBJECT_ANIMATION,
        "                borrowed.enter_context(restored_keys_on_error(owner, data_paths))\n",
        "                pass\n",
        (_KEYS_BACK,),
    ),
    Revert(
        "action assignment: a curve the failed batch created is left in the reused action",
        ADDON_ACTION_ASSIGNMENT,
        "                collection.remove(curve)\n                continue\n",
        "                continue\n",
        (_KEYS_BACK,),
    ),
    Revert(
        "action assignment: a curve the failed batch overwrote keeps the overwritten keys",
        ADDON_ACTION_ASSIGNMENT,
        "            _restore_curve(curve, state)\n",
        "            pass\n",
        (_KEYS_BACK,),
    ),
    *_CAP_ROWS,
]
