"""
Rows guarding one keyframe-style vocabulary, and one action per ID.

Label prefixes: `key style:`, `action assignment:`, `action assignment control:`.
"""

from .common import ADDON_ACTION_ASSIGNMENT, ADDON_KEY_STYLE, ADDON_OBJECT_ANIMATION, CRTT, KEYSTYLET, OANIMT, Revert

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
]
