"""
Bending a bone chain until its tip reaches a world point, and keying the result.

A reach is not a pose: it is solved, by standing a temporary IK constraint and its targets up
in the scene, reading the depsgraph's answer, and taking the helpers back down on every path
out. That machinery - the chain resolution, the synthesized pole, the hinge clamp, the
convergence verdict - serves `solve_bone_reach` and `keyframe_bone_reach` alone, and nothing in
ordinary posing calls into it. It sits here so the pose-writing module is not read as if it
also had to know about IK, and it depends on `posing` one way: it borrows that module's pose
application, key writing and restore managers, and is imported by none of it.
"""

import contextlib
import math
import uuid

from typing import NamedTuple

import bpy

from ..action_assignment import assign_named_action
from ..key_style import KeyStyle
from .axes import _AIM_MIN_LENGTH, _AIM_MIN_RESIDUAL, _AXIS_INDEX
from .posing import (
    _action_reply,
    _apply_pose_specs,
    _place_playhead,
    _posable_armature,
    _style_written_keys,
    _write_pose_keys,
    restored_action_assignment,
    restored_bone_pose,
    restored_playhead,
)
from .primitives import _finite, _required_name, _vector_tuple

# Longest chain solve_bone_reach will auto-resolve or accept explicitly - matches
# BoneReach.chain_length's Field(le=32) on the server side.
_MAX_REACH_CHAIN = 32


# Names for the scratch Empty objects and IK constraint solve_bone_reach creates for one
# reach's evaluation. Never left behind: removed before the call returns on every path,
# success or failure (mutation_transaction's rollback additionally covers the Empties, as
# ordinary created objects, if an exception unwinds past their own try/finally).
_REACH_HELPER_PREFIX = "__solve_bone_reach__"


# How close tip_bone's tail must land to the target before a reach counts as converged.
# 0.1 mm is below what a 24-frame shot shows and above what a 500-iteration Blender IK solve
# leaves on a bent chain, so it separates "solved" from "the solver stopped short" without
# calling ordinary solver residue a failure. A shot needing more or less says so per call.
_DEFAULT_REACH_TOLERANCE_M = 1e-4


def _validated_tolerance(tolerance_m):
    """
    Read a reach's convergence tolerance, refusing one that names no precision.

    Args:
        tolerance_m: The caller's tolerance, in metres.

    Returns:
        float: The tolerance every reach in the call is judged against.

    Raises:
        ValueError: If it is not finite, or not greater than zero - `converged` would then be
            a claim about nothing.

    """
    tolerance_m = _finite(tolerance_m, "tolerance_m")
    if tolerance_m <= 0.0:
        raise ValueError(f"tolerance_m must be greater than 0 metres, not {tolerance_m}")
    return tolerance_m


def _rest_ancestor_chain(tip, length):
    """
    Build an exact-length ancestor run above tip, root-most last, ignoring branching.

    Args:
        tip: The rest bone (`armature.data.bones[...]`) the reach targets.
        length: The exact chain length the caller asked for explicitly.

    Returns:
        list: `length` `bpy.types.Bone`s, tip first.

    Raises:
        ValueError: If tip has fewer than `length - 1` ancestors.

    """
    chain = [tip]
    bone = tip
    for _step in range(length - 1):
        if bone.parent is None:
            raise ValueError(f"'{tip.name}' has only {len(chain)} ancestor(s); chain_length={length} exceeds them")
        chain.append(bone.parent)
        bone = bone.parent
    return chain


def _unbranched_ancestor_chain(tip, max_length):
    """
    Tip-to-root ancestor run, stopping before the first fork (2+ children) or the last root.

    Args:
        tip: The rest bone the reach targets.
        max_length: The longest chain this will return.

    Returns:
        list: 1 to `max_length` `bpy.types.Bone`s, tip first.

    """
    chain = [tip]
    bone = tip
    while len(chain) < max_length:
        parent = bone.parent
        if parent is None or len(parent.children) > 1:
            break
        chain.append(parent)
        bone = parent
    return chain


def _synthesize_pole(armature, chain):
    """
    Infer a pole point from the chain's REST-pose bend, in world space.

    The chain's middle joint - found by walking tip_tail, then every chain bone's head in
    order down to root_head, and taking the one at the midpoint of that list - is projected
    off the straight root-to-tip line; what is left of it is the bend direction, scaled out
    by the chain's total rest length. A chain with only one bone, or whose rest pose is
    dead straight, has no such direction and is refused rather than guessed.

    Args:
        armature: The armature object.
        chain: The reach's resolved chain, tip first, as returned by
            `_unbranched_ancestor_chain` or `_rest_ancestor_chain`.

    Returns:
        mathutils.Vector: A world-space point off to the bend side of the chain.

    Raises:
        ValueError: If the chain's root and tip coincide, or its rest pose is straight.

    """
    root_head = chain[-1].head_local
    tip_tail = chain[0].tail_local
    axis = tip_tail - root_head
    if axis.length <= _AIM_MIN_LENGTH:
        raise ValueError(f"'{chain[0].name}' chain root and tip coincide; supply pole_target_point explicitly")
    axis = axis.normalized()
    joints = [tip_tail, *(bone.head_local for bone in chain)]
    mid = joints[len(joints) // 2]
    offset = mid - root_head
    projected = offset - axis * offset.dot(axis)
    if projected.length <= _AIM_MIN_RESIDUAL:
        raise ValueError(
            f"'{chain[0].name}' chain's rest pose is straight; no natural pole direction can "
            "be inferred - supply pole_target_point or pole_target_object_name"
        )
    projected = projected.normalized()
    pole_local = mid + projected * sum(bone.length for bone in chain)
    return armature.matrix_world @ pole_local


def _reach_helper_object(location):
    """Create a scratch Empty at a world point, for an IK target/pole with no named object."""
    empty = bpy.data.objects.new(f"{_REACH_HELPER_PREFIX}{uuid.uuid4().hex}", None)
    bpy.context.collection.objects.link(empty)
    empty.location = location
    return empty


def _resolved_reach_target(point, object_name, label):
    """
    Resolve an existing named object, or a scratch Empty at an explicit world point.

    Args:
        point: A raw world-space point, or None.
        object_name: An existing object's name, or None. Exactly one of point/object_name is
            non-None; the caller has already enforced that (BoneReach's own validator, for
            target_point/target_object_name, and solve_bone_reach's own pole handling, for
            pole_target_point/pole_target_object_name).
        label: What this target is, for the not-found message.

    Returns:
        tuple: `(object, is_temporary)`. is_temporary is True for a scratch Empty this
            function created, which the caller must remove once the IK solve has read it.

    Raises:
        ValueError: If object_name does not name an existing object.

    """
    if object_name is not None:
        obj = bpy.data.objects.get(object_name)
        if obj is None:
            raise ValueError(f"{label} object not found: {object_name}")
        return obj, False
    return _reach_helper_object(_vector_tuple(point, label)), True


def _configured_reach_constraint(tip_pose_bone, reach, target_obj, pole_obj, chain_count):
    """Add and configure one reach's temporary IK constraint on tip_pose_bone."""
    fields = {
        "name": f"{_REACH_HELPER_PREFIX}{uuid.uuid4().hex}",
        "target": target_obj,
        "pole_target": pole_obj,
        "chain_count": chain_count,
        "pole_angle": math.radians(reach.get("pole_angle_degrees", 0.0)),
        "use_stretch": bool(reach.get("use_stretch", False)),
        "iterations": int(reach.get("iterations", 500)),
        # tip_bone's TAIL is what the model promises reaches target, not just its rotation.
        "use_tail": True,
    }
    constraint = tip_pose_bone.constraints.new(type="IK")
    try:
        for field, value in fields.items():
            setattr(constraint, field, value)
    except Exception:
        # The constraint is on the rig from `new()` onwards, and the caller's own try/finally
        # only covers a constraint this function returned. A value Blender's RNA refuses must
        # not leave a live IK constraint behind - the same reason `add_pose_bone_constraint`
        # removes a constraint it created but could not configure.
        tip_pose_bone.constraints.remove(constraint)
        raise
    return constraint


def _resolve_reach_chain(armature, reach, captured):
    """Resolve the rest-bone chain a reach's tip_bone/chain_length imply, refusing any overlap."""
    tip_name = _required_name(reach.get("tip_bone"), "tip_bone")
    rest_tip = armature.data.bones.get(tip_name)
    if rest_tip is None:
        raise ValueError(f"Pose bone not found: {tip_name}")
    requested_length = reach.get("chain_length")
    if requested_length is None:
        rest_chain = _unbranched_ancestor_chain(rest_tip, _MAX_REACH_CHAIN)
        chain_length_source = "resolved"
    else:
        rest_chain = _rest_ancestor_chain(rest_tip, requested_length)
        chain_length_source = "explicit"
    overlap = sorted(name for name in (bone.name for bone in rest_chain) if name in captured)
    if overlap:
        raise ValueError(f"Bones claimed by more than one reach: {overlap}")
    return rest_chain, chain_length_source


def _resolved_reach_pole(armature, reach, rest_chain):
    """Resolve the reach's pole object, synthesizing one from the rest bend when none is named."""
    pole_point = reach.get("pole_target_point")
    pole_object_name = reach.get("pole_target_object_name")
    if pole_object_name is not None or pole_point is not None:
        pole_obj, pole_is_temp = _resolved_reach_target(pole_point, pole_object_name, "pole_target_point")
        return pole_obj, pole_is_temp, "explicit"
    return _reach_helper_object(tuple(_synthesize_pole(armature, rest_chain))), True, "resolved"


def _resolve_reach_geometry(armature, reach, rest_chain):
    """Resolve the reach's target and pole objects, and where the pole came from."""
    target_obj, target_is_temp = _resolved_reach_target(
        reach.get("target_point"), reach.get("target_object_name"), "target_point"
    )
    try:
        pole_obj, pole_is_temp, pole_source = _resolved_reach_pole(armature, reach, rest_chain)
    except Exception:
        # A scratch Empty for the target already exists by the time the pole is resolved, and
        # `_solve_one_reach`'s try/finally has not started yet. An unresolvable pole - an
        # unknown object, or a rest chain too straight to infer one from - must not strand it.
        if target_is_temp:
            bpy.data.objects.remove(target_obj, do_unlink=True)
        raise
    return target_obj, target_is_temp, pole_obj, pole_is_temp, pole_source


def _world_chain_reach(armature, rest_chain):
    """
    Measure how far a rest chain can extend, in the world space the reach is reported in.

    Args:
        armature: The armature object the chain belongs to, for its world matrix - a scaled
            rig's bones are longer or shorter in the scene than their rest lengths say.
        rest_chain: The reach's rest bones, tip first.

    Returns:
        float: The sum of the chain bones' world-space lengths - the straight-line distance
        from the chain's root head that the chain can span with every joint extended.

    """
    matrix = armature.matrix_world
    return sum((matrix @ bone.tail_local - matrix @ bone.head_local).length for bone in rest_chain)


def _reach_convergence_warning(solution, tolerance_m):
    """
    Say why one reach missed its tolerance, separating an unreachable target from a stall.

    Args:
        solution: One `ReachSolution`.
        tolerance_m: The convergence tolerance this call asked for, in metres.

    Returns:
        str | None: A notice naming the tip bone, the achieved error, the tolerance and the
        cause, or None when the reach converged and there is nothing to act on.

    """
    if solution.converged:
        return None
    measured = solution.measurements
    missed = (
        f"Reach '{solution.chain[0].name}' did not converge: achieved_error_m "
        f"{measured['achieved_error_m']:.6g} exceeds tolerance_m {tolerance_m:.6g}"
    )
    if solution.out_of_reach:
        return (
            f"{missed}. The target is out of reach: target_distance_m "
            f"{measured['target_distance_m']:.6g} is beyond chain_reach_m {measured['chain_reach_m']:.6g}. "
            "Move the target closer, lengthen the chain with chain_length, or move the armature."
        )
    return (
        f"{missed}. The target is within the chain's range (target_distance_m "
        f"{measured['target_distance_m']:.6g} of chain_reach_m {measured['chain_reach_m']:.6g}), so the "
        "solve stalled short of it: raise iterations, supply a pole_target_point, or loosen tolerance_m."
    )


def _reach_measurements(armature, rest_chain, tip_pose_bone, target_obj):
    """
    Measure where the solved tip landed, and how the target sits against the chain's reach.

    Args:
        armature: The armature object being posed, for its world matrix and pose bones.
        rest_chain: The reach's rest bones, tip first.
        tip_pose_bone: The chain's tip pose bone, with the IK constraint still evaluated.
        target_obj: The object (or scratch Empty) the reach is solving towards.

    Returns:
        dict: target_world, head_world, tail_world, achieved_error_m, chain_reach_m and
        target_distance_m, every one of them in world space.

    """
    matrix_world = armature.matrix_world
    target_point = target_obj.matrix_world.translation
    tail_point = matrix_world @ tip_pose_bone.tail
    # The chain's root head is where the chain is anchored, so the distance a target sits at
    # is measured from there - not from the tip, which the solve has already moved.
    root_head_point = matrix_world @ armature.pose.bones[rest_chain[-1].name].head
    return {
        "target_world": list(target_point),
        "head_world": list(matrix_world @ tip_pose_bone.head),
        "tail_world": list(tail_point),
        "achieved_error_m": (tail_point - target_point).length,
        "chain_reach_m": _world_chain_reach(armature, rest_chain),
        "target_distance_m": (target_point - root_head_point).length,
    }


def _validated_reach_hinge(reach, rest_chain):
    """
    Read and check a reach's optional hinge without applying it.

    Separate from applying it so a multi-frame keying call can refuse a bad hinge before it
    keys the first frame, rather than partway through the range.

    Args:
        reach: The reach entry, read for its optional `hinge`.
        rest_chain: The reach's resolved rest bones, tip first.

    Returns:
        tuple | None: `(bone_name, axis, min_radians, max_radians)`, or None when the reach
        asked for no hinge.

    Raises:
        ValueError: If the hinge names a bone outside this reach's chain - limiting a bone
            the solve does not drive would silently do nothing - an unknown axis, or an
            inverted limit interval.

    """
    hinge = reach.get("hinge")
    if hinge is None:
        return None
    bone_name = _required_name(hinge.get("bone_name"), "hinge.bone_name")
    chain_names = [bone.name for bone in rest_chain]
    if bone_name not in chain_names:
        raise ValueError(f"hinge.bone_name '{bone_name}' is not in this reach's chain: {chain_names}")
    axis = str(hinge.get("axis", "")).upper()
    if axis not in _AXIS_INDEX:
        raise ValueError(f"hinge.axis must be X, Y or Z, not {axis!r}")
    minimum = _finite(hinge.get("min_degrees"), "hinge.min_degrees")
    maximum = _finite(hinge.get("max_degrees"), "hinge.max_degrees")
    if minimum > maximum:
        raise ValueError("hinge.min_degrees must not exceed hinge.max_degrees")
    return bone_name, axis, math.radians(minimum), math.radians(maximum)


@contextlib.contextmanager
def _reach_hinge(armature, hinge):
    """
    Constrain one chain bone to a single rotation axis for the duration of the solve.

    A knee has one axis and one sign; an unconstrained IK solver will invert it to save the
    solve an iteration, which is how a walk cycle ends up with a backwards leg. The limit is
    temporary: every value it overwrote is back before the block ends.

    Args:
        armature: The armature being solved.
        hinge: `_validated_reach_hinge` output, or None when the reach asked for no hinge.

    """
    if hinge is None:
        yield None
        return
    bone_name, axis, minimum, maximum = hinge
    pose_bone = armature.pose.bones[bone_name]
    lowered = axis.lower()
    fields = {
        f"use_ik_limit_{lowered}": True,
        f"ik_min_{lowered}": minimum,
        f"ik_max_{lowered}": maximum,
        # Locking the other two axes is what makes this a hinge rather than a limited ball
        # joint: a knee that can still twist reads as broken just as fast as one that bends
        # backwards.
        **{f"lock_ik_{other.lower()}": True for other in _AXIS_INDEX if other != axis},
    }
    restore = {field: getattr(pose_bone, field) for field in fields}
    for field, value in fields.items():
        setattr(pose_bone, field, value)
    try:
        yield pose_bone
    finally:
        for field, value in restore.items():
            setattr(pose_bone, field, value)


@contextlib.contextmanager
def _reach_constraint(tip_pose_bone, reach, target_obj, pole_obj, chain_count):
    """
    Drive one chain from a temporary IK constraint, and never leave it live on the rig.

    Args:
        tip_pose_bone: The chain's tip pose bone, which carries the constraint.
        reach: The reach entry, read for pole angle, stretch and iteration count.
        target_obj: The object the tip's tail solves towards.
        pole_obj: The object the chain bends towards.
        chain_count: How many bones up the chain the solve drives.

    """
    constraint = _configured_reach_constraint(tip_pose_bone, reach, target_obj, pole_obj, chain_count)
    try:
        yield constraint
    finally:
        tip_pose_bone.constraints.remove(constraint)


class ReachSolution(NamedTuple):
    """
    Everything one solved reach knows, before any tool decides what to report of it.

    `solve_bone_reach` and `keyframe_bone_reach` report overlapping but different subsets of
    this. Returning a wire-shaped dict made the reply format the solver's business, and both
    tools re-projections of a dict neither of them owned: one grafted an extra key onto it,
    one narrowed it back down, and a third reached into `solved[0][1]["pole_source"]`.
    """

    chain: tuple
    chain_length_source: str
    pole_source: str
    measurements: dict
    converged: bool
    out_of_reach: bool
    captured: dict


def _solve_one_reach(armature, reach, rest_chain, chain_length_source, hinge, tolerance_m):
    """
    Temporarily IK-solve one reach and capture the pose its chain was left holding.

    Args:
        armature: The armature object being posed.
        reach: One validated reach entry, as a dict, read for its target, pole and solver
            settings.
        rest_chain: The reach's resolved rest bones, tip first.
        chain_length_source: "explicit" or "resolved", for the reply to repeat.
        hinge: `_validated_reach_hinge` output, or None.
        tolerance_m: How close tip_bone's tail must land to the target to count as converged.

    Returns:
        ReachSolution: the chain, where the pole came from, the world measurements, whether
        the solve landed, and each chain bone's evaluated pose-space matrix.

    Raises:
        ValueError: If an explicit target or pole names an object that does not exist, or an
            omitted pole cannot be synthesized from a straight or degenerate rest chain.

    """
    target_obj, target_is_temp, pole_obj, pole_is_temp, pole_source = _resolve_reach_geometry(
        armature, reach, rest_chain
    )
    tip_pose_bone = armature.pose.bones[rest_chain[0].name]
    try:
        with (
            _reach_hinge(armature, hinge),
            _reach_constraint(tip_pose_bone, reach, target_obj, pole_obj, len(rest_chain)),
        ):
            bpy.context.view_layer.update()
            captured = {bone.name: armature.pose.bones[bone.name].matrix.copy() for bone in rest_chain}
            measured = _reach_measurements(armature, rest_chain, tip_pose_bone, target_obj)
    finally:
        if target_is_temp:
            bpy.data.objects.remove(target_obj, do_unlink=True)
        if pole_is_temp:
            bpy.data.objects.remove(pole_obj, do_unlink=True)
        bpy.context.view_layer.update()
    return ReachSolution(
        chain=tuple(rest_chain),
        chain_length_source=chain_length_source,
        pole_source=pole_source,
        measurements=measured,
        converged=measured["achieved_error_m"] <= tolerance_m,
        out_of_reach=measured["target_distance_m"] > measured["chain_reach_m"],
        captured=captured,
    )


def _apply_captured_matrices(armature, captured, detail=False):
    """
    Apply pose matrices this call evaluated itself, without re-checking Blender's own output.

    The reach paths used to flatten every captured matrix to sixteen floats, rebuild a
    client-shaped pose entry from them and call `set_character_pose`, which re-resolved the
    armature by name, re-checked its pose position and ran a finiteness test over all sixteen
    - per bone, per frame. None of that was validating client input: the matrices came out of
    the depsgraph moments earlier. `set_character_pose` keeps that validation for callers who
    really do hand it numbers.

    Args:
        armature: The armature being posed.
        captured: `{bone_name: pose-space matrix}`, as the solve evaluated them.
        detail: Report each bone's pre-call matrix too, and neither matrix rounded.

    Returns:
        tuple: the `(pose_bone, spec, matrix)` triples the key writer takes, and one record
        per posed bone.

    """
    prepared = [
        (armature.pose.bones[name], {"bone_name": name, "matrix": matrix}, matrix) for name, matrix in captured.items()
    ]
    return prepared, _apply_pose_specs(armature, prepared, "POSE", detail=detail)


def _prepared_keyed_reaches(armature, reaches):
    """
    Resolve every keyed reach's chain, frames and hinge before a single key is written.

    Everything that can be known without moving the rig is checked here, because the
    alternative is a call that keys frames 1 to 7 and then refuses frame 8 over a misspelled
    bone, leaving the action holding half a move.

    Args:
        armature: The armature being keyed.
        reaches: The raw keyed-reach entries.

    Returns:
        list[dict]: Per reach: `spec` (the reach's solver fields, without its keys),
        `rest_chain`, `chain_length_source`, `hinge` (validated once here, not once per
        frame), and `keys`, mapping frame to that frame's target fields.

    Raises:
        ValueError: If a reach names no keys, two reaches claim the same bone, a chain or
            hinge does not resolve, or a key names a target object that does not exist.

    """
    claimed = {}
    prepared = []
    for index, reach in enumerate(reaches):
        if not isinstance(reach, dict):
            raise ValueError(f"reaches[{index}] must be an object")
        rest_chain, chain_length_source = _resolve_reach_chain(armature, reach, claimed)
        for bone in rest_chain:
            claimed[bone.name] = index
        hinge = _validated_reach_hinge(reach, rest_chain)
        keys = reach.get("keys") or []
        if not keys:
            raise ValueError(f"reaches[{index}] ('{rest_chain[0].name}') must supply at least one key")
        frames = {}
        for key_index, key in enumerate(keys):
            label = f"reaches[{index}].keys[{key_index}]"
            frame = _finite(key.get("frame"), f"{label}.frame")
            if frame in frames:
                raise ValueError(f"{label}: frame {frame} is keyed twice in one reach")
            target_object = key.get("target_object_name")
            if target_object is not None and bpy.data.objects.get(target_object) is None:
                raise ValueError(f"{label}: target object not found: {target_object}")
            if target_object is None and key.get("target_point") is None:
                raise ValueError(f"{label} must supply exactly one of target_point or target_object_name")
            frames[frame] = {
                key_name: key[key_name] for key_name in ("target_point", "target_object_name") if key_name in key
            }
        spec = {name: value for name, value in reach.items() if name != "keys"}
        prepared.append(
            {
                "spec": spec,
                "rest_chain": rest_chain,
                "chain_length_source": chain_length_source,
                "hinge": hinge,
                "keys": frames,
            }
        )
    return prepared


def _solved_reach_frame(armature, prepared, frame, tolerance_m):
    """
    Move the playhead to one frame and solve every reach that has a key there.

    The playhead move is what makes the solve correct: the chain's parents - the hips, the
    root - then hold whatever the action already says at this frame, so the IK solves against
    the body's real position rather than against frame one's.

    Args:
        armature: The armature being keyed.
        prepared: `_prepared_keyed_reaches` output.
        frame: The frame to solve, subframe included.
        tolerance_m: The convergence tolerance, in metres.

    Returns:
        tuple: `captured` (pose-space matrices per bone) and a list of
        `(reach_index, ReachSolution)` for the reaches keyed at this frame.

    """
    _place_playhead(bpy.context.scene, frame)
    # The solve reads the evaluated parent pose, and `frame_set` alone does not guarantee the
    # depsgraph has caught up with an action assigned moments ago in this same call.
    bpy.context.view_layer.update()
    captured = {}
    solved = []
    for index, entry in enumerate(prepared):
        key = entry["keys"].get(frame)
        if key is None:
            continue
        solution = _solve_one_reach(
            armature,
            {**entry["spec"], **key},
            entry["rest_chain"],
            entry["chain_length_source"],
            entry["hinge"],
            tolerance_m,
        )
        captured.update(solution.captured)
        solved.append((index, solution))
    return captured, solved


def _reach_frame_record(solution, frame):
    """
    Narrow one frame's solve to what the reply reports per frame.

    Args:
        solution: One `ReachSolution`.
        frame: The frame it was solved at.

    Returns:
        dict: frame, target_world, tail_world, achieved_error_m, converged, chain_reach_m,
        target_distance_m and out_of_reach.

    """
    measured = solution.measurements
    return {
        "frame": frame,
        "target_world": measured["target_world"],
        "tail_world": measured["tail_world"],
        "achieved_error_m": measured["achieved_error_m"],
        "converged": solution.converged,
        "chain_reach_m": measured["chain_reach_m"],
        "target_distance_m": measured["target_distance_m"],
        "out_of_reach": solution.out_of_reach,
    }


def _keyed_reach_warning(solution, frame, tolerance_m):
    """
    Name the frame a reach missed its tolerance on.

    Args:
        solution: One `ReachSolution`.
        frame: The frame it was solved at.
        tolerance_m: The tolerance it was judged against.

    Returns:
        str | None: The frame-prefixed notice, or None when that frame converged.

    """
    warning = _reach_convergence_warning(solution, tolerance_m)
    return None if warning is None else f"Frame {frame:g}: {warning}"


def _solved_reach_record(solution, by_bone):
    """
    Report one immediately-applied reach: what it achieved, and the bones it left posed.

    Args:
        solution: One `ReachSolution`.
        by_bone: `_apply_captured_matrices`' records, keyed by bone name.

    Returns:
        dict: tip_bone, chain_bones, chain_length, chain_length_source, pole_source, the
        world measurements, converged, out_of_reach and this chain's per-bone records.

    """
    return {
        "tip_bone": solution.chain[0].name,
        "chain_bones": [bone.name for bone in solution.chain],
        "chain_length": len(solution.chain),
        "chain_length_source": solution.chain_length_source,
        "pole_source": solution.pole_source,
        **solution.measurements,
        "converged": solution.converged,
        "out_of_reach": solution.out_of_reach,
        "bones": [by_bone[bone.name] for bone in solution.chain],
    }


def _keyed_reach_record(entry, solved):
    """
    Report one keyed reach: its resolved chain, and what each of its frames achieved.

    Args:
        entry: The `_prepared_keyed_reaches` record for this reach.
        solved: `(frame, ReachSolution)` for every frame this reach was solved at.

    Returns:
        dict: tip_bone, chain_bones, chain_length, chain_length_source, pole_source and
        keys - one `_reach_frame_record` per frame, in frame order.

    """
    rest_chain = entry["rest_chain"]
    return {
        "tip_bone": rest_chain[0].name,
        "chain_bones": [bone.name for bone in rest_chain],
        "chain_length": len(rest_chain),
        "chain_length_source": entry["chain_length_source"],
        # Every frame resolves the pole the same way, so the first frame's answer is the
        # reach's answer; a reach with no frames cannot happen (the preparation refuses it).
        "pole_source": solved[0][1].pole_source if solved else None,
        "keys": [_reach_frame_record(solution, frame) for frame, solution in solved],
    }


def _key_reach_frames(armature, action, prepared, keying_policy, style, tolerance_m, detail):
    """
    Solve and key every frame any reach asked for, in ascending order.

    Args:
        armature: The armature being keyed.
        action: The action every key lands in, already assigned to the rig.
        prepared: `_prepared_keyed_reaches` output.
        keying_policy: INSERT or REPLACE, passed through to `_write_pose_keys`.
        style: The `KeyStyle` every key this call writes is shaped with.
        tolerance_m: The convergence tolerance, in metres.
        detail: Whether to capture per-bone matrices at Blender's own precision.

    Returns:
        dict: frames (ascending), changed_keys, changed_bones, styled (how many points were
        styled), reaches (one record per requested reach) and warnings (one per frame that
        did not converge).

    """
    frames = sorted({frame for entry in prepared for frame in entry["keys"]})
    changed_keys = []
    changed_bones = []
    styled = 0
    solved_by_reach = {index: [] for index in range(len(prepared))}
    for frame in frames:
        captured, solved = _solved_reach_frame(armature, prepared, frame, tolerance_m)
        specs, _records = _apply_captured_matrices(armature, captured, detail)
        written = _write_pose_keys(action, specs, frame, keying_policy)
        styled += _style_written_keys(action, written, style)
        changed_keys.extend(written)
        changed_bones.extend(name for name in captured if name not in changed_bones)
        for index, solution in solved:
            solved_by_reach[index].append((frame, solution))
    return {
        "frames": frames,
        "changed_keys": changed_keys,
        "changed_bones": changed_bones,
        "styled": styled,
        "reaches": [_keyed_reach_record(entry, solved_by_reach[index]) for index, entry in enumerate(prepared)],
        # A frame that missed says so in its own `converged` flag and here: the envelope
        # lifts warnings, so a caller reading only those still learns the foot did not land.
        "warnings": [
            warning
            for results in solved_by_reach.values()
            for frame, solution in results
            for warning in [_keyed_reach_warning(solution, frame, tolerance_m)]
            if warning is not None
        ],
    }


def _keyed_reach_reply(armature, animation, action, previous_action, keyed, tolerance_m, keying_policy):
    """
    Describe what one multi-frame reach keying call left behind.

    Args:
        armature: The keyed armature object.
        animation: Its `animation_data`, read for the assignment this call ended on.
        action: The action that was authored.
        previous_action: The action that drove the rig before, or None.
        keyed: `_key_reach_frames` output.
        tolerance_m: The tolerance every frame was judged against.
        keying_policy: The policy the caller asked for.

    Returns:
        dict: The handler reply, with one warning per frame that did not converge.

    """
    reply = _action_reply(
        armature,
        animation,
        action,
        previous_action,
        keying_policy,
        keyed["changed_bones"],
        keyed["changed_keys"],
        keyed["styled"],
    )
    reply["tolerance_m"] = tolerance_m
    reply["keyed_frames"] = keyed["frames"]
    reply["reaches"] = keyed["reaches"]
    reply["warnings"] = keyed["warnings"]
    return reply


class BoneReachHandlersMixin:
    """Solve and key an IK reach: bend a chain until its tip lands on a world point."""

    def solve_bone_reach(self, armature_object_name, reaches, tolerance_m=_DEFAULT_REACH_TOLERANCE_M, detail=False):
        """Bend one or more unbranched chains so each tip_bone's tail reaches a point."""
        tolerance_m = _validated_tolerance(tolerance_m)
        armature = _posable_armature(armature_object_name, "solve a bone reach")
        if not reaches:
            raise ValueError("At least one reach entry is required")
        captured = {}
        solutions = []
        for reach in reaches:
            # `captured` doubles as the claim register: a chain overlapping an earlier reach's
            # is refused here rather than letting the later solve silently win.
            rest_chain, chain_length_source = _resolve_reach_chain(armature, reach, captured)
            hinge = _validated_reach_hinge(reach, rest_chain)
            solution = _solve_one_reach(armature, reach, rest_chain, chain_length_source, hinge, tolerance_m)
            captured.update(solution.captured)
            solutions.append(solution)
        _specs, records = _apply_captured_matrices(armature, captured, detail)
        by_bone = {record["bone"]: record for record in records}
        return {
            "armature_object": armature.name,
            "tolerance_m": tolerance_m,
            "changed_bones": [record["bone"] for record in records],
            "reaches": [_solved_reach_record(solution, by_bone) for solution in solutions],
            "changed_objects": [armature.name],
            # A reach that missed says so here as well as in its own `converged` flag: the
            # envelope lifts these, so a caller reading only the warnings still sees it.
            "warnings": [
                warning
                for warning in (_reach_convergence_warning(solution, tolerance_m) for solution in solutions)
                if warning is not None
            ],
        }

    def keyframe_bone_reach(
        self,
        armature_object_name,
        action_name,
        reaches,
        tolerance_m=_DEFAULT_REACH_TOLERANCE_M,
        keying_policy="REPLACE",
        interpolation="BEZIER",
        handle_left="AUTO_CLAMPED",
        handle_right="AUTO_CLAMPED",
        easing=None,
        action_policy="ENSURE",
        confirm_displace_action=False,
        action_slot_identifier=None,
        detail=False,
    ):
        """Solve each reach at each of its frames against the evaluated body pose, and key it."""
        armature = _posable_armature(armature_object_name, "key a bone reach")
        tolerance_m = _validated_tolerance(tolerance_m)
        if keying_policy not in {"INSERT", "REPLACE"}:
            raise ValueError("keying_policy must be INSERT or REPLACE; use keyframe_character_pose to remove keys")
        style = KeyStyle(interpolation, handle_left, handle_right, easing)
        style.validate()
        prepared = _prepared_keyed_reaches(armature, list(reaches or ()))
        scene = bpy.context.scene
        animation = armature.animation_data_create()
        with (
            restored_playhead(scene),
            restored_action_assignment(animation) as previous_action,
            restored_bone_pose(armature, [bone.name for entry in prepared for bone in entry["rest_chain"]]),
        ):
            action = assign_named_action(
                armature,
                action_name,
                action_policy,
                action_slot_identifier,
                confirm_displace=confirm_displace_action,
            )
            keyed = _key_reach_frames(armature, action, prepared, keying_policy, style, tolerance_m, detail)
        return _keyed_reach_reply(armature, animation, action, previous_action, keyed, tolerance_m, keying_policy)
