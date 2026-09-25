"""
Rows guarding a cycle's period, a roll's travel, and the bone-axis probe.

Label prefixes: `animation:`, `pose:`.
"""

from .common import ADDON_ANIMATION, ADDON_POSING, ANIMT, POSET, SERVER_ANIMATION_TOOL, Revert

ROWS: list[Revert] = [
    # --- a cycle's period is readable without deleting the cycle to produce it ---
    Revert(
        # Before INSPECT existed the only reply carrying a period was the one that deleted the
        # cycle to produce it: read the number, then cycle the action again - three calls, and a
        # window in which the shot was not looping at all. Folding INSPECT into the REMOVE
        # branch is that dance written back.
        "animation: reading a cycle's period deletes the cycle to produce it again",
        ADDON_ANIMATION,
        '            elif operation == "REMOVE":\n',
        '            elif operation in {"REMOVE", "INSPECT"}:\n',
        (f"{ANIMT}::test_inspect_reports_the_period_without_destroying_the_cycle",),
    ),
    Revert(
        # "This curve carries no cycle" is exactly what an INSPECT caller is asking, and a flag
        # asserted rather than read answers it wrongly while looking right.
        "animation: every inspected curve is reported as carrying a cycle",
        ADDON_ANIMATION,
        '        record["has_cycles_modifier"] = modifier is not None\n',
        '        record["has_cycles_modifier"] = True\n',
        (f"{ANIMT}::test_inspect_reports_a_curve_that_carries_no_cycle_where_remove_omits_it",),
    ),
    Revert(
        # SET reports back the modes and counts the caller asked for, which is honest only
        # because the call just wrote them. An INSPECT echoing the same arguments reports a
        # cycle nobody authored - this tool's defaults, dressed as the curve's state.
        "animation: an inspection echoes this call's argument defaults instead of the live modifier",
        ADDON_ANIMATION,
        '    if operation == "INSPECT":\n        modes, cycles, restricted = _live_cycle_state(modifier)\n',
        "    if False:\n        modes, cycles, restricted = _live_cycle_state(modifier)\n",
        (f"{ANIMT}::test_inspect_reads_the_modifier_that_is_there_not_this_calls_defaults",),
    ),
    Revert(
        # A restricted range is something to write and INSPECT writes nothing, but
        # expected_period_frames is an assertion about what is already there - the one
        # cycle-describing argument an inspection can honour. Refusing it the way REMOVE does
        # puts the measurement back behind a write.
        "animation: INSPECT refuses the expected period it is able to assert",
        ADDON_ANIMATION,
        '        if operation == "REMOVE" and (restricted is not None or expected_period_frames is not None):\n',
        '        if operation != "SET" and (restricted is not None or expected_period_frames is not None):\n',
        (f"{ANIMT}::test_inspect_asserts_an_expected_period_and_still_writes_nothing",),
    ),
    Revert(
        # The two measured notices are pure readings of the records: "these curves do not share
        # one period" is the diagnosis an inspection came for, and asking for it must not
        # require writing a modifier first.
        "animation: an inspection warns about nothing, the way a removal does",
        ADDON_ANIMATION,
        '    if operation == "INSPECT":\n        return [warning for warning in measured if warning is not None]\n',
        '    if operation == "INSPECT":\n        return []\n',
        (f"{ANIMT}::test_inspect_says_when_the_selected_curves_do_not_share_one_period",),
    ),
    Revert(
        # The tool-side half of the same rule: INSPECT creates nothing, so every argument that
        # describes a new cycle is a typo - bar the one that only asserts a measurement.
        "animation: the cycle tool refuses an expected period under INSPECT as well as REMOVE",
        SERVER_ANIMATION_TOOL,
        '                ("expected_period_frames", operation == "REMOVE" and expected_period_frames is not None),\n',
        '                ("expected_period_frames", expected_period_frames is not None),\n',
        (f"{ANIMT}::test_the_cycle_tool_lets_inspect_assert_a_period_and_refuses_what_it_cannot_write",),
    ),
    # --- an edited key that redefines a cycled curve's period says so, once, measured first ---
    Revert(
        # The third way into the trap the two keying tools already guard, and the quietest:
        # the key lands, every field of the reply reads as success, and the curve's period has
        # become the distance to the new frame.
        "animation: an edited key past a cycle's extent is written in silence",
        ADDON_ANIMATION,
        "        warnings = _edit_cycle_warnings(bag, expanded)\n",
        "        warnings = []\n",
        (f"{ANIMT}::test_an_edited_key_outside_a_cycle_reports_the_period_it_redefines",),
    ),
    Revert(
        # A notice on every key of a cycled curve is noise, and noise is what teaches an agent
        # to skip the one notice that was measured.
        "animation: every key on a cycled curve warns, not only one landing outside the cycle",
        ADDON_ANIMATION,
        "        if first - _KEY_FRAME_TOLERANCE <= frame <= last + _KEY_FRAME_TOLERANCE:\n",
        "        if False:\n",
        (f"{ANIMT}::test_an_edited_key_inside_the_cycle_or_off_a_cycled_curve_stays_quiet",),
    ),
    Revert(
        # Measured after the loop instead of before it, the batch's own inserts have already
        # stretched the extent every frame is judged against, so the two keys that stretched it
        # both read as comfortably inside a cycle that did not exist when the call began.
        "animation: the cycle notice is measured after the batch has finished inserting",
        ADDON_ANIMATION,
        '            "warnings": warnings,\n',
        '            "warnings": _edit_cycle_warnings(bag, expanded),\n',
        (f"{ANIMT}::test_the_cycle_notice_is_measured_before_the_batch_starts_inserting",),
    ),
    # --- a roll is judged by the travel it was measured to cause, not by the axis it names ---
    Revert(
        # The unmeasured notice this replaced: on one real rig a 30-degree head roll moved the
        # bone's tail 0.000 cm and the face 6.47 cm, and the notice called that a mistake -
        # which taught the agent these warnings were noise, and it then dismissed a correct,
        # quantitative cycle warning and lost a thirteen-key walk.
        "pose: a roll is called inert from the axis alone, without measuring what it carries",
        ADDON_POSING,
        (
            '        if travel_m > max(_TWIST_TRAVEL_FLOOR_M, measured["length_m"] * _TWIST_TRAVEL_FRACTION):\n'
            "            continue\n"
        ),
        "        if False:\n            continue\n",
        (f"{POSET}::test_a_roll_that_swings_an_offset_child_bone_says_nothing",),
    ),
    Revert(
        # A jaw or a head bone often has no child bone at all: every part of it an audience sees
        # is skin, so the rest hierarchy alone cannot tell it from a relay bone that really does
        # carry nothing.
        "pose: the roll radius is read off the rest hierarchy alone, ignoring the skin",
        ADDON_POSING,
        "    skinned, weighted, bound_meshes, bounded = _skinned_twist_radius(pose_bone, meshes, origin, axis)\n",
        "    skinned, weighted, bound_meshes, bounded = 0.0, 0, 0, False\n",
        (f"{POSET}::test_a_roll_that_carries_skinned_vertices_off_the_axis_says_nothing",),
    ),
    Revert(
        # Not proving the skin stays put is not the same as proving it moves. With no mesh bound
        # under this bone's name there is nothing to read, and the notice has to say so rather
        # than report the zero vertices it never looked at as a measurement.
        "pose: a roll notice reports a vertex count it never took instead of naming the skin unmeasured",
        ADDON_POSING,
        '    if witnesses["meshes"] == 0:\n',
        "    if False:\n",
        (f"{POSET}::test_a_deforming_bone_with_no_reachable_mesh_says_what_it_did_not_measure",),
    ),
    Revert(
        # The judgement is made per rolled bone and a call may pose 500 of them, so a
        # million-vertex body cannot be walked once per roll. Unbounded, the scan also stops
        # disclosing that its radius is a floor rather than a maximum.
        "pose: the twist vertex scan is unbounded, so it never reports its radius as a floor",
        ADDON_POSING,
        "            if examined >= _MAX_TWIST_VERTICES or weighted >= _MAX_TWIST_WEIGHTED_VERTICES:\n",
        "            if False:\n",
        (f"{POSET}::test_a_bounded_vertex_scan_says_its_radius_is_a_floor",),
    ),
    Revert(
        # Warnings are lifted whole into the envelope and never paged, so one notice per posed
        # bone spends the reply budget on them - the same bound, and the same revert, as the
        # per-bone cycle notices above.
        "pose: the roll notices are unbounded, so a 500-bone pose spends the reply budget on them",
        ADDON_POSING,
        "        for pose_bone, degrees, travel_m, measured in silent[:_MAX_CYCLE_WARNINGS]\n",
        "        for pose_bone, degrees, travel_m, measured in silent\n",
        (f"{POSET}::test_a_whole_rig_rolled_about_its_own_length_counts_the_bones_it_cannot_name",),
    ),
    # --- the probe answers which axis moves a bone, and hands back the pose it borrowed ---
    Revert(
        # A witness read at its head sits on the axis of every turn about the probed bone, so
        # every axis reports the same zero and the reply cannot tell a roll from a swing - which
        # is the one distinction this tool exists to draw.
        "pose: the probe reads its witness at the head, which no turn of the bone moves",
        ADDON_POSING,
        '    if position == "TAIL":\n        return to_world @ witness.tail\n',
        '    if position == "TAIL":\n        return to_world @ witness.head\n',
        (f"{POSET}::test_the_probe_separates_the_axis_that_swings_a_bone_from_the_one_that_only_rolls_it",),
    ),
    Revert(
        # The sign is the half of the answer a magnitude cannot carry: it is what says which way
        # round to roll a wrist to turn the palm outward, rather than only that it moved.
        "pose: a reference component is published as a magnitude, so it cannot say which way",
        ADDON_POSING,
        "            name: round(travel.dot(direction), _PROBE_DECIMALS) for name, direction in references.items()\n",
        (
            "            name: round(abs(travel.dot(direction)), _PROBE_DECIMALS)"
            " for name, direction in references.items()\n"
        ),
        (f"{POSET}::test_the_sign_of_a_reference_component_follows_the_sign_of_the_turn",),
    ),
    Revert(
        # A shoulder read at the shoulder answers almost nothing: the lever arm is the hand, so
        # the default witness is the farthest descendant and the probed bone itself is the
        # fallback for a bone that carries no descendant at all.
        "pose: the probe reads every bone at itself instead of through the farthest thing it carries",
        ADDON_POSING,
        '    if not descendants:\n        return pose_bone, "probed_bone"\n',
        '    if True:\n        return pose_bone, "probed_bone"\n',
        (f"{POSET}::test_the_probe_defaults_to_the_farthest_descendant_and_names_how_it_chose",),
    ),
    Revert(
        # `set_character_pose`'s pose is its deliverable, so it keeps it; a probe's turn is
        # scaffolding for the measurement. Keeping it corrupts the pose the call was made to
        # explain, and the command is read-only, so no transaction puts it back.
        "pose: a probe keeps its last trial turn the way a pose call keeps its pose",
        ADDON_POSING,
        "                with restored_bone_pose(armature, [pose_bone.name]):\n",
        "                with restored_bone_pose(armature, [pose_bone.name], only_on_error=True):\n",
        (f"{POSET}::test_the_probe_hands_the_pose_back_untouched",),
    ),
    Revert(
        # The same unwind the keying rows revert, reached from the read-only side: a probe runs
        # outside `mutation_transaction`, so this except branch is the only thing that hands the
        # rig back when the depsgraph raises mid-measurement.
        "pose: a probe that raises part way through leaves its trial turn on the bone",
        ADDON_POSING,
        "    try:\n        yield\n    except BaseException:\n        restore()\n        raise\n",
        "    try:\n        yield\n    except BaseException:\n        raise\n",
        (f"{POSET}::test_a_probe_that_raises_part_way_through_still_hands_the_pose_back",),
    ),
    Revert(
        # Six named directions in and one silently ignored is a wrong answer nobody can see:
        # a zero vector normalises to nothing, and every component measured along it is zero.
        "pose: a reference direction naming no direction is normalised instead of refused",
        ADDON_POSING,
        "        if vector.length <= _AIM_MIN_LENGTH:\n",
        "        if False:\n",
        (f"{POSET}::test_a_probe_refuses_a_direction_that_names_no_direction_by_name",),
    ),
    Revert(
        # Two identical probes report the same travel twice and answer nothing, while costing
        # the rig a second trial turn.
        "pose: a probe accepts the same axis twice and measures it twice",
        ADDON_POSING,
        '    _unique_names(listed, "probe axes")\n',
        "",
        (f"{POSET}::test_a_probe_refuses_a_repeated_axis_and_an_unknown_one_before_touching_the_bone",),
    ),
]
