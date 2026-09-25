"""
Rows guarding posing replies, the pose reaching the file, and the reach solver.

Label prefixes: `pose:`, `pose control:`.
"""

from .common import ADDON_AXES, ADDON_POSING, ADDON_REACH, CTRLT, LISTT, POSET, REACHT, SERVER_POSING_TOOL, Revert

ROWS: list[Revert] = [
    # --- a pose reply names what it changed; the matrices are the part that is trimmed ---
    Revert(
        "pose: the pose matrix is published at full float precision",
        ADDON_POSING,
        "    return [[round(float(value), _POSE_MATRIX_DECIMALS) for value in row] for row in matrix]\n",
        "    return [[float(value) for value in row] for row in matrix]\n",
        (f"{CTRLT}::test_pose_report_rounds_the_result_and_omits_the_pre_call_matrix",),
    ),
    Revert(
        "pose: detail is ignored, so the pre-call matrix and full precision are unreachable",
        ADDON_POSING,
        "        if detail:\n"
        '            record["before_pose_matrix"] = _matrix_list(before[pose_bone.name])\n'
        '            record["after_pose_matrix"] = _matrix_list(pose_bone.matrix)\n'
        "        else:\n"
        '            record["after_pose_matrix"] = _rounded_matrix_list(pose_bone.matrix)\n',
        '        record["after_pose_matrix"] = _rounded_matrix_list(pose_bone.matrix)\n',
        (
            f"{CTRLT}::test_pose_detail_restores_the_pre_call_matrix_and_full_precision",
            f"{CTRLT}::test_keyframe_detail_reports_the_pose_that_was_keyed",
        ),
    ),
    Revert(
        "pose: a record names the transform channels but not the custom properties it set",
        ADDON_POSING,
        '    channels.extend(f\'["{name}"]\' for name in sorted(spec.get("custom_properties", {})))\n',
        "",
        (f"{CTRLT}::test_pose_record_names_the_channels_and_custom_properties_the_call_set",),
    ),
    Revert(
        # `solve_bone_reach` reports `changed_bones` from the same records with the same
        # spelling, so the anchor reaches up to `"space"`, which only `set_character_pose` sends.
        "pose: changed_bones is dropped, so a shortened page of records is all the agent gets",
        ADDON_POSING,
        '            "space": space,\n'
        "            # Complete, and cheap enough to stay complete: the per-bone records are what the\n"
        "            # reply budget shortens, so this is what still names every bone the call posed.\n"
        '            "changed_bones": [record["bone"] for record in records],\n',
        '            "space": space,\n',
        (f"{CTRLT}::test_the_budget_shortens_pose_records_but_never_the_changed_bone_names",),
    ),
    Revert(
        "pose: a keyframed pose reports every bone's matrices whether or not they were asked for",
        ADDON_POSING,
        "    if detail:\n"
        "        # The pose is restored before this returns, so these matrices describe what was keyed at\n"
        "        # each requested frame, not what the rig is holding now.\n"
        '        reply["bones"] = keyed["records"]\n',
        '    reply["bones"] = keyed["records"]\n',
        (f"{CTRLT}::test_keyframed_pose_names_every_bone_and_reports_no_matrices_by_default",),
    ),
    Revert(
        "pose: keyframe_character_pose does not forward detail",
        SERVER_POSING_TOOL,
        # `keyframe_bone_reach` ends its payload with the same two lines, so the anchor reaches
        # up to `space`, which only `keyframe_character_pose` sends.
        '            "space": space,\n'
        '            "keying_policy": keying_policy,\n'
        '            "interpolation": interpolation,\n'
        '            "handle_left": handle_left,\n'
        '            "handle_right": handle_right,\n'
        '            "easing": easing,\n'
        '            "action_policy": action_policy,\n'
        '            "confirm_displace_action": confirm_displace_action,\n'
        '            "action_slot_identifier": action_slot_identifier,\n'
        '            "detail": detail,\n',
        '            "space": space,\n'
        '            "keying_policy": keying_policy,\n'
        '            "interpolation": interpolation,\n'
        '            "handle_left": handle_left,\n'
        '            "handle_right": handle_right,\n'
        '            "easing": easing,\n'
        '            "action_policy": action_policy,\n'
        '            "confirm_displace_action": confirm_displace_action,\n'
        '            "action_slot_identifier": action_slot_identifier,\n'
        '            "detail": False,\n',
        (f"{CTRLT}::test_pose_tools_forward_the_detail_flag",),
    ),
    # --- the pose an agent authors reaches the file, and a child is solved against its parent ---
    Revert(
        # The restore is conditional on the block having raised: a call that authored keys has
        # to leave its own action assigned, or Blender drops it at save. Turning the `except`
        # into a `finally` is exactly the unconditional hand-back that discards the work.
        "pose: the keyed action is unassigned again, so Blender drops it at save",
        ADDON_POSING,
        "    try:\n"
        "        yield previous_action\n"
        "    except BaseException:\n"
        "        animation.action = previous_action\n"
        "        if previous_action is not None and previous_slot is not None:\n"
        "            # Assigning an action resets the slot, and a slot Blender no longer considers\n"
        "            # suitable is its refusal to make, not this unwind's to force.\n"
        "            with contextlib.suppress(Exception):\n"
        "                animation.action_slot = previous_slot\n"
        "        raise\n",
        "    try:\n"
        "        yield previous_action\n"
        "    finally:\n"
        "        animation.action = previous_action\n"
        "        if previous_action is not None and previous_slot is not None:\n"
        "            with contextlib.suppress(Exception):\n"
        "                animation.action_slot = previous_slot\n",
        (
            f"{POSET}::test_keying_leaves_the_rig_driven_by_the_action_it_authored",
            f"{POSET}::test_keying_reports_the_action_it_displaced",
        ),
    ),
    Revert(
        "pose: an absolute-space target is built before the parent this call moves",
        ADDON_POSING,
        '        elif space == _PARENT_RELATIVE_SPACE and "aim_at" not in spec:\n',
        '        elif "aim_at" not in spec:\n',
        (f"{POSET}::test_an_absolute_space_child_is_resolved_against_the_parent_this_call_moved",),
    ),
    Revert(
        "pose: aim_at resolves to nothing, so the bone keeps the rotation it had",
        ADDON_POSING,
        '    if "aim_at" in spec:\n'
        "        # An aim is stated in the scene, not in the call's space, and lands in armature space.\n"
        '        return _aim_pose_matrix(armature, pose_bone, spec["aim_at"]), "POSE"\n',
        "",
        (
            f"{POSET}::test_posing_an_aim_lands_it_on_the_target_after_the_parent_has_moved",
            f"{POSET}::test_a_keyed_aim_takes_the_short_way_round_from_the_previous_key",
            f"{POSET}::test_a_keyed_euler_aim_stays_on_the_previous_keys_branch",
        ),
    ),
    Revert(
        "pose: a keyed aim is spelled without regard to the previous key, so it spins between them",
        ADDON_POSING,
        '                if "aim_at" in spec and path in _ROTATION_CHANNEL_WIDTH:\n'
        "                    _match_previous_rotation(action, pose_bone, path, frame)\n",
        "",
        (
            f"{POSET}::test_a_keyed_aim_takes_the_short_way_round_from_the_previous_key",
            f"{POSET}::test_a_keyed_euler_aim_stays_on_the_previous_keys_branch",
        ),
    ),
    Revert(
        "pose: rest axes are withheld even when the caller asks for them",
        ADDON_POSING,
        "            if rest_axes:",
        "            if False:",
        (f"{LISTT}::test_rest_axes_are_reported_only_when_asked_for",),
    ),
    # --- the nine numbers carry their own conclusion ---
    #
    # A runbook read a head bone's rest axes, concluded `up_axis: "-X"`, aimed with it and
    # shipped a head tilted 90 degrees. Measured on a synthetic bone carrying those exact axes
    # (`scripts/blender_probes/pose_axis_frames.py`), "-X" is right and the conventions agree:
    # `rest_axes`, `aim_at.track_axis` and `aim_at.up_axis` all name the bone's own axes. What
    # the reply could not support is the derivation itself - `up_reference` is a world
    # direction, the nine numbers are armature-space, and the rig's object matrix is nowhere in
    # the reply - so the answer is reported rather than left to be worked out.
    Revert(
        "pose: rest axes are handed back as nine numbers with the up axis left to be derived",
        ADDON_POSING,
        "                aim_axes = _rest_aim_axes(armature, bone)\n"
        '                item["up_axis"] = aim_axes["+Z"]\n'
        '                item["aim_axis_for_world"] = aim_axes\n',
        "",
        (f"{LISTT}::test_the_rest_axes_are_also_named_in_the_vocabulary_an_aim_takes",),
    ),
    Revert(
        "pose: the bone's length axis is left unsaid, so which letter aims the bone is folklore",
        ADDON_POSING,
        '        if rest_axes:\n            reply["length_axis"] = _LENGTH_AXIS\n',
        "",
        (f"{LISTT}::test_the_rest_axes_are_also_named_in_the_vocabulary_an_aim_takes",),
    ),
    Revert(
        "pose: the up axis is read in armature space, ignoring where the rig sits in the scene",
        ADDON_AXES,
        "    rest = armature.matrix_world.to_3x3() @ bone.matrix_local.to_3x3()\n",
        "    rest = bone.matrix_local.to_3x3()\n",
        (f"{LISTT}::test_the_up_axis_follows_the_rig_into_the_scene_where_the_nine_numbers_cannot",),
    ),
    Revert(
        "pose: a rig with no direction left in it still has an up axis guessed for it",
        ADDON_AXES,
        "    if not units:\n        return None\n",
        "    if not units:\n        return _LENGTH_AXIS\n",
        # The derivation that guessed is `_nearest_rest_axis`, which every world direction -
        # `up_axis` among them - is answered from, so the node making the claim widened with it.
        (f"{LISTT}::test_a_rig_scaled_to_nothing_names_no_axis_for_any_direction",),
    ),
    Revert(
        "pose: an aim's up axis is not made perpendicular, so the basis shears",
        ADDON_AXES,
        "    columns = {track_letter: direction * track_sign, up_letter: residual.normalized() * up_sign}\n",
        "    columns = {track_letter: direction * track_sign, up_letter: up_pose * up_sign}\n",
        (f"{POSET}::test_aim_points_the_named_axis_at_an_object_and_leaves_position_and_scale_alone",),
    ),
    Revert(
        "pose: an aim reads its world target as if the rig were at the origin",
        ADDON_AXES,
        "    world_to_pose = armature.matrix_world.inverted()\n",
        "    world_to_pose = mathutils.Matrix.Identity(4)\n",
        (
            f"{POSET}::test_aim_at_a_world_point_resolves_through_the_rig_transform",
            f"{POSET}::test_aim_points_the_named_axis_at_an_object_and_leaves_position_and_scale_alone",
        ),
    ),
    Revert(
        "pose: an aim target on the bone head is normalised instead of refused",
        ADDON_AXES,
        "    if distance <= _AIM_MIN_DISTANCE:\n",
        "    if False:\n",
        (f"{POSET}::test_aim_rejects_every_direction_it_cannot_define",),
    ),
    Revert(
        "pose: a minimal-arc aim accepts a half turn and rolls the bone arbitrarily",
        ADDON_AXES,
        "        if swing > _AIM_MAX_MINIMAL_ARC:\n",
        "        if False:\n",
        (f"{POSET}::test_a_minimal_arc_aim_past_the_flip_angle_is_refused_rather_than_rolled_arbitrarily",),
    ),
    Revert(
        "pose: rotate reads its angle as radians, so a degree value under-rotates",
        ADDON_AXES,
        '"angle": math.radians(degrees),',
        '"angle": degrees,',
        (f"{POSET}::test_rotate_resolves_named_axes_and_vectors_in_degrees",),
    ),
    Revert(
        "pose: relative rotate replaces the rotation instead of composing with it",
        ADDON_POSING,
        '        rotation = (delta @ mathutils.Quaternion(rotation)) if record["relative"] else delta\n',
        "        rotation = delta\n",
        (f"{POSET}::test_relative_rotate_composes_while_the_default_replaces",),
    ),
    Revert(
        "pose: a resolved rotation keys no channel at all, so an aim writes no curves",
        ADDON_POSING,
        '_ROTATION_CHANNELS = ("rotation_euler", "rotation_quaternion", "rotation_axis_angle", "rotate", "aim_at")\n',
        '_ROTATION_CHANNELS = ("rotation_euler", "rotation_quaternion", "rotation_axis_angle")\n',
        (
            f"{POSET}::test_a_resolved_rotation_keys_only_the_bones_native_channel[QUATERNION-rotation_quaternion]",
            f"{POSET}::test_a_resolved_rotation_keys_only_the_bones_native_channel[XYZ-rotation_euler]",
            f"{POSET}::test_a_resolved_rotation_keys_only_the_bones_native_channel[AXIS_ANGLE-rotation_axis_angle]",
        ),
    ),
    Revert(
        # `restored_bone_pose` owns every hand-back now, so the failure path is the branch that
        # runs `restore()` before re-raising; dropping it leaves the last solved pose on the rig.
        "pose: a failed keying call leaves the half-applied pose on the rig",
        ADDON_POSING,
        "    try:\n        yield\n    except BaseException:\n        restore()\n        raise\n",
        "    try:\n        yield\n    except BaseException:\n        raise\n",
        (f"{POSET}::test_a_failed_key_hands_the_rig_back_as_it_arrived",),
    ),
    Revert(
        "pose: keying accepts a roll-preserving aim, so two frames key two different rolls",
        ADDON_POSING,
        '        if "aim_at" in spec and spec["aim_at"]["up"] is None:\n',
        "        if False:\n",
        (f"{POSET}::test_keying_an_aim_without_an_up_reference_is_refused",),
    ),
    # --- solve_bone_reach says whether it converged, and why not ---
    Revert(
        "pose: a bone reach reports itself converged whatever it achieved",
        ADDON_REACH,
        '        converged=measured["achieved_error_m"] <= tolerance_m,\n',
        "        converged=True,\n",
        (
            f"{REACHT}::test_a_tighter_tolerance_turns_the_same_solve_into_a_miss",
            f"{REACHT}::test_a_reachable_target_the_solve_stalled_short_of_warns_without_blaming_the_rig",
            f"{REACHT}::test_a_target_beyond_the_chains_reach_is_reported_as_unreachable",
            f"{REACHT}::test_a_missed_reach_still_warns_after_the_envelope_has_shortened_the_reply",
        ),
    ),
    Revert(
        "pose: a bone reach cannot tell an unreachable target from a stalled solve",
        ADDON_REACH,
        '        out_of_reach=measured["target_distance_m"] > measured["chain_reach_m"],\n',
        "        out_of_reach=False,\n",
        (f"{REACHT}::test_a_target_beyond_the_chains_reach_is_reported_as_unreachable",),
    ),
    Revert(
        "pose: a bone reach sums rest bone lengths, ignoring the rig's world scale",
        ADDON_REACH,
        "    matrix = armature.matrix_world\n"
        "    return sum((matrix @ bone.tail_local - matrix @ bone.head_local).length for bone in rest_chain)\n",
        "    return sum(bone.length for bone in rest_chain)\n",
        (f"{REACHT}::test_the_chains_reach_is_measured_in_world_space_not_in_rest_bone_lengths",),
    ),
    Revert(
        "pose: a missed bone reach reports its numbers but raises no warning",
        ADDON_REACH,
        '            "warnings": [\n'
        "                warning\n"
        "                for warning in (_reach_convergence_warning(solution, tolerance_m) for solution in solutions)\n"
        "                if warning is not None\n"
        "            ],\n",
        '            "warnings": [],\n',
        (
            f"{REACHT}::test_a_reachable_target_the_solve_stalled_short_of_warns_without_blaming_the_rig",
            f"{REACHT}::test_a_target_beyond_the_chains_reach_is_reported_as_unreachable",
            f"{REACHT}::test_a_missed_reach_still_warns_after_the_envelope_has_shortened_the_reply",
        ),
    ),
    Revert(
        "pose: a bone reach takes tolerance_m as given, so 0 or NaN reaches the solve",
        ADDON_REACH,
        # Both reach tools read the tolerance through `_validated_tolerance`, so reverting the
        # one helper is what lets 0 or NaN reach either solve.
        '    tolerance_m = _finite(tolerance_m, "tolerance_m")\n'
        "    if tolerance_m <= 0.0:\n"
        '        raise ValueError(f"tolerance_m must be greater than 0 metres, not {tolerance_m}")\n'
        "    return tolerance_m\n",
        "    return float(tolerance_m)\n",
        tuple(
            f"{REACHT}::test_a_tolerance_that_names_no_precision_is_refused_before_the_rig_is_touched[{case}]"
            for case in ("0.0", "-0.0001", "nan", "inf")
        ),
    ),
    Revert(
        "pose: a bone reach never says which tolerance it judged the solve against",
        ADDON_REACH,
        '            "tolerance_m": tolerance_m,\n',
        "",
        (
            f"{REACHT}::test_a_reach_inside_its_tolerance_reports_converged_and_says_nothing_else",
            f"{REACHT}::test_a_tighter_tolerance_turns_the_same_solve_into_a_miss",
        ),
    ),
    Revert(
        "pose: every missed bone reach is blamed on the target being out of reach",
        ADDON_REACH,
        "    if solution.out_of_reach:\n",
        "    if True:\n",
        (f"{REACHT}::test_a_reachable_target_the_solve_stalled_short_of_warns_without_blaming_the_rig",),
    ),
    Revert(
        "pose: a converged bone reach warns anyway, so every solve carries a notice",
        ADDON_REACH,
        "    if solution.converged:\n        return None\n",
        "    if False:\n        return None\n",
        (f"{REACHT}::test_a_reach_inside_its_tolerance_reports_converged_and_says_nothing_else",),
    ),
    # --- the chain, pole and target resolution both reach tools share ---
    Revert(
        # A chain that walks through a fork picks up a bone the IK solver will then drive
        # sideways: the reach bends the other arm as well as the one it was asked about.
        "pose: an auto-resolved chain walks straight through a fork",
        ADDON_REACH,
        "        if parent is None or len(parent.children) > 1:\n",
        "        if parent is None:\n",
        (
            f"{REACHT}::test_unbranched_ancestor_chain_stops_before_a_mid_chain_fork",
            f"{REACHT}::test_unbranched_ancestor_chain_stops_before_a_root_level_fork",
        ),
    ),
    Revert(
        "pose: an auto-resolved chain ignores the cap and runs to the root",
        ADDON_REACH,
        "    while len(chain) < max_length:\n",
        "    while True:\n",
        (f"{REACHT}::test_unbranched_ancestor_chain_respects_max_length",),
    ),
    Revert(
        # The deliberate opposite of the fork row: stopping one bone short of an unforked root
        # is equally wrong, and a leg rooted at the hips loses the hip bone that carries it.
        "pose control: an auto-resolved chain stops one short of an unforked root",
        ADDON_REACH,
        "        chain.append(parent)\n",
        "        if parent.parent is None:\n            break\n        chain.append(parent)\n",
        (f"{REACHT}::test_unbranched_ancestor_chain_includes_an_unforked_root",),
    ),
    Revert(
        # The tip seeds its own chain, so a root bone still resolves to a one-bone reach
        # rather than to nothing the solver can drive.
        "pose: a resolved chain leaves out the tip bone it was asked to solve",
        ADDON_REACH,
        "    chain = [tip]\n    bone = tip\n    while len(chain) < max_length:\n",
        "    chain = []\n    bone = tip\n    while len(chain) < max_length:\n",
        (f"{REACHT}::test_unbranched_ancestor_chain_of_a_root_bone_is_just_that_bone",),
    ),
    Revert(
        # An explicit chain_length is an assertion about the rig, and it is the only way past a
        # fork the auto-resolve stops at; one bone short is a chain that cannot reach.
        "pose: an explicit chain_length resolves one bone short",
        ADDON_REACH,
        "    for _step in range(length - 1):\n",
        "    for _step in range(length - 2):\n",
        (f"{REACHT}::test_rest_ancestor_chain_returns_the_exact_requested_length",),
    ),
    Revert(
        "pose: a chain_length past the root is silently shortened instead of refused",
        ADDON_REACH,
        "        if bone.parent is None:\n"
        "            raise ValueError(f\"'{tip.name}' has only {len(chain)} ancestor(s); "
        'chain_length={length} exceeds them")\n',
        "        if bone.parent is None:\n            break\n",
        (f"{REACHT}::test_rest_ancestor_chain_refuses_a_length_past_the_root",),
    ),
    Revert(
        # The round-2 draft this row pins: taking `chain[len(chain) // 2]` as the pole reference
        # is the ROOT bone on a two-bone chain, so offset-from-root is zero and every elbow and
        # knee is refused as "straight".
        "pose: pole synthesis takes the chain's root as its bend reference",
        ADDON_REACH,
        "    joints = [tip_tail, *(bone.head_local for bone in chain)]\n    mid = joints[len(joints) // 2]\n",
        "    mid = chain[len(chain) // 2].head_local\n",
        (f"{REACHT}::test_synthesize_pole_finds_the_bend_side_of_a_bent_two_bone_chain",),
    ),
    Revert(
        # A straight rest chain names no bend direction, so a synthesized pole would be noise
        # pointing wherever float error happened to land: refuse and say to supply one.
        "pose: a straight rest chain has a pole guessed from float noise instead of refusing",
        ADDON_REACH,
        "    if projected.length <= _AIM_MIN_RESIDUAL:\n",
        "    if False:\n",
        (
            f"{REACHT}::test_synthesize_pole_refuses_a_straight_two_bone_rest_chain",
            f"{REACHT}::test_synthesize_pole_refuses_a_single_bone_chain",
        ),
    ),
    Revert(
        "pose: a chain whose root and tip coincide is normalised instead of refused",
        ADDON_REACH,
        "    if axis.length <= _AIM_MIN_LENGTH:\n",
        "    if False:\n",
        (f"{REACHT}::test_synthesize_pole_refuses_a_chain_whose_root_and_tip_coincide",),
    ),
    Revert(
        # Rest bones are armature-space; the pole is handed to an IK constraint as a world
        # point, so a rig anywhere but the origin bends towards a point beside the character.
        "pose: a synthesized pole is reported in armature space as if it were world space",
        ADDON_REACH,
        "    return armature.matrix_world @ pole_local\n",
        "    return pole_local\n",
        (f"{REACHT}::test_synthesize_pole_converts_through_the_armatures_world_matrix",),
    ),
    Revert(
        "pose: a reach on a bone the rig does not have is solved instead of refused",
        ADDON_REACH,
        '    if rest_tip is None:\n        raise ValueError(f"Pose bone not found: {tip_name}")\n',
        '    if False:\n        raise ValueError(f"Pose bone not found: {tip_name}")\n',
        (f"{REACHT}::test_resolve_reach_chain_refuses_an_unknown_tip_bone",),
    ),
    Revert(
        # chain_length_source is how a caller learns whether the chain it got was the one it
        # asked for or one this handler inferred; swapping the two labels keeps both reports
        # present and makes both of them lies.
        "pose: a reach mislabels whether its chain length was inferred or given",
        ADDON_REACH,
        '        chain_length_source = "resolved"\n'
        "    else:\n"
        "        rest_chain = _rest_ancestor_chain(rest_tip, requested_length)\n"
        '        chain_length_source = "explicit"\n',
        '        chain_length_source = "explicit"\n'
        "    else:\n"
        "        rest_chain = _rest_ancestor_chain(rest_tip, requested_length)\n"
        '        chain_length_source = "resolved"\n',
        (
            f"{REACHT}::test_resolve_reach_chain_reports_resolved_when_chain_length_is_omitted",
            f"{REACHT}::test_resolve_reach_chain_reports_explicit_when_chain_length_is_given",
        ),
    ),
    Revert(
        # Two reaches solving one bone to two targets is ambiguous; without the refusal the
        # later reach silently wins and the earlier one reports a pose it did not get.
        "pose: two reaches may claim the same bone, and the later one silently wins",
        ADDON_REACH,
        '    if overlap:\n        raise ValueError(f"Bones claimed by more than one reach: {overlap}")\n',
        '    if False:\n        raise ValueError(f"Bones claimed by more than one reach: {overlap}")\n',
        (f"{REACHT}::test_resolve_reach_chain_refuses_a_bone_already_claimed_by_an_earlier_reach",),
    ),
    Revert(
        "pose: a reach target naming an object that is not there resolves to None",
        ADDON_REACH,
        '        if obj is None:\n            raise ValueError(f"{label} object not found: {object_name}")\n',
        '        if False:\n            raise ValueError(f"{label} object not found: {object_name}")\n',
        (f"{REACHT}::test_resolved_reach_target_refuses_an_unknown_object_name",),
    ),
    Revert(
        # The target's scratch Empty exists before the pole is resolved and before
        # `_solve_one_reach`'s own try/finally starts, so an unresolvable pole strands it in
        # the file under a `__solve_bone_reach__` name nobody will recognise.
        "pose: a reach refused over its pole strands the target's scratch Empty in the file",
        ADDON_REACH,
        "        if target_is_temp:\n            bpy.data.objects.remove(target_obj, do_unlink=True)\n        raise\n",
        "        raise\n",
        (f"{REACHT}::test_a_reach_whose_pole_cannot_be_resolved_removes_the_targets_scratch_empty",),
    ),
    Revert(
        # `constraints.new` lands the constraint on the rig before any field is written, so a
        # value Blender's RNA refuses would leave a live IK constraint on the tip bone.
        "pose: a constraint value Blender refuses leaves the IK constraint live on the rig",
        ADDON_REACH,
        "    except Exception:\n"
        "        # The constraint is on the rig from `new()` onwards, and the caller's own try/finally\n"
        "        # only covers a constraint this function returned. A value Blender's RNA refuses must\n"
        "        # not leave a live IK constraint behind - the same reason `add_pose_bone_constraint`\n"
        "        # removes a constraint it created but could not configure.\n"
        "        tip_pose_bone.constraints.remove(constraint)\n"
        "        raise\n",
        "    except Exception:\n        raise\n",
        (f"{REACHT}::test_a_constraint_value_blender_refuses_removes_the_constraint_it_already_added",),
    ),
    Revert(
        # Neither form given is a reach with nowhere to go; both given is two answers to one
        # question. The schema cannot express "exactly one", so the model has to.
        "pose: a reach naming no target, or two, is accepted by the schema",
        SERVER_POSING_TOOL,
        "            BoneReach: This model, unchanged.\n"
        "\n"
        "        Raises:\n"
        "            ValueError: If neither or both target forms are given.\n"
        "\n"
        '        """\n'
        "        if (self.target_point is None) == (self.target_object_name is None):\n"
        '            raise ValueError("Supply exactly one of target_point or target_object_name")\n',
        "            BoneReach: This model, unchanged.\n"
        "\n"
        "        Raises:\n"
        "            ValueError: If neither or both target forms are given.\n"
        "\n"
        '        """\n',
        (f"{REACHT}::test_bone_reach_requires_exactly_one_target_form",),
    ),
    Revert(
        "pose: a reach naming two pole targets is accepted by the schema",
        SERVER_POSING_TOOL,
        "        if self.pole_target_point is not None and self.pole_target_object_name is not None:\n"
        '            raise ValueError("Supply at most one of pole_target_point or pole_target_object_name")\n',
        "",
        (f"{REACHT}::test_bone_reach_allows_at_most_one_pole_form",),
    ),
    Revert(
        # An unset optional field sent as null is not the same request as one left out: the
        # handler reads `reach.get("pole_target")` and an explicit None would stop pole
        # synthesis from ever running.
        "pose: a reach sends every optional field as null instead of omitting it",
        SERVER_POSING_TOOL,
        '            "reaches": dump_inputs(reaches),\n'
        '            "tolerance_m": tolerance_m,\n'
        '            "detail": detail,\n',
        '            "reaches": [reach.model_dump() for reach in reaches],\n'
        '            "tolerance_m": tolerance_m,\n'
        '            "detail": detail,\n',
        (f"{REACHT}::test_solve_bone_reach_forwards_reaches_and_omits_unset_optional_fields",),
    ),
    # --- a reach that raises part way through hands the rig back the action it arrived on ---
    Revert(
        # The deliberate opposite of "the keyed action is unassigned again": that row proves
        # restoring unconditionally is caught, this one proves never restoring is caught too.
        # `object_state` does not snapshot `animation_data.action`, so nothing else in the
        # transaction puts the displaced action back when the solve raises mid-range.
        "pose: a reach that raises keeps the action it assigned, displacement and all",
        ADDON_POSING,
        "    except BaseException:\n"
        "        animation.action = previous_action\n"
        "        if previous_action is not None and previous_slot is not None:\n",
        "    except BaseException:\n        if False:\n",
        (f"{REACHT}::test_a_reach_that_fails_part_way_through_hands_back_the_action_it_arrived_on",),
    ),
]
