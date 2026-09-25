"""Regression coverage for solving an IK reach and keying the result."""

import asyncio
import sys
import types

import pytest

from conftest import load_addon
from pydantic import ValidationError
from pydantic_core import to_json

from blender_mcp.server.tools import _dispatch, character_rigging
from blender_mcp.server.tools.envelope import REPLY_BYTE_BUDGET, envelope_for

from .rig_doubles import (
    _Euler,
    _FCurve,
    _Matrix,
    _PoseBone,
    _posing,
    _Quaternion,
    _Vector,
)


def _reach_bone(name, parent=None):
    """Build a plain topology node: what `_unbranched_ancestor_chain`/`_rest_ancestor_chain` need."""
    bone = types.SimpleNamespace(name=name, parent=parent, children=[])
    if parent is not None:
        parent.children.append(bone)
    return bone


def _load_reach(monkeypatch):
    addon, _bpy = load_addon(monkeypatch, data={"objects": {}})
    return sys.modules[f"{addon.__name__}.handlers.character_rigging.reach"]


def test_unbranched_ancestor_chain_stops_before_a_mid_chain_fork(monkeypatch) -> None:
    shoulder = _reach_bone("shoulder")
    arm_l = _reach_bone("arm_L", parent=shoulder)
    _reach_bone("arm_R", parent=shoulder)  # the fork: shoulder now has two children
    hand_l = _reach_bone("hand_L", parent=arm_l)
    reach = _load_reach(monkeypatch)

    assert [bone.name for bone in reach._unbranched_ancestor_chain(hand_l, 32)] == ["hand_L", "arm_L"]


def test_unbranched_ancestor_chain_stops_before_a_root_level_fork(monkeypatch) -> None:
    """A fork at the very root excludes the root itself, not just what is above it."""
    root = _reach_bone("root")
    a1 = _reach_bone("a1", parent=root)
    _reach_bone("a2", parent=root)  # root itself forks
    tip = _reach_bone("tip", parent=a1)
    reach = _load_reach(monkeypatch)

    assert [bone.name for bone in reach._unbranched_ancestor_chain(tip, 32)] == ["tip", "a1"]


def test_unbranched_ancestor_chain_includes_an_unforked_root(monkeypatch) -> None:
    root = _reach_bone("root")
    mid = _reach_bone("mid", parent=root)
    tip = _reach_bone("tip", parent=mid)
    reach = _load_reach(monkeypatch)

    assert [bone.name for bone in reach._unbranched_ancestor_chain(tip, 32)] == ["tip", "mid", "root"]


def test_unbranched_ancestor_chain_respects_max_length(monkeypatch) -> None:
    root = _reach_bone("root")
    mid = _reach_bone("mid", parent=root)
    tip = _reach_bone("tip", parent=mid)
    reach = _load_reach(monkeypatch)

    assert [bone.name for bone in reach._unbranched_ancestor_chain(tip, 2)] == ["tip", "mid"]


def test_unbranched_ancestor_chain_of_a_root_bone_is_just_that_bone(monkeypatch) -> None:
    lone = _reach_bone("lone")
    reach = _load_reach(monkeypatch)

    assert [bone.name for bone in reach._unbranched_ancestor_chain(lone, 32)] == ["lone"]


def test_rest_ancestor_chain_returns_the_exact_requested_length(monkeypatch) -> None:
    """An explicit chain_length walks past a fork an auto-resolve would have stopped before."""
    root = _reach_bone("root")
    a1 = _reach_bone("a1", parent=root)
    _reach_bone("a2", parent=root)
    tip = _reach_bone("tip", parent=a1)
    reach = _load_reach(monkeypatch)

    assert [bone.name for bone in reach._rest_ancestor_chain(tip, 3)] == ["tip", "a1", "root"]


def test_rest_ancestor_chain_refuses_a_length_past_the_root(monkeypatch) -> None:
    root = _reach_bone("root")
    tip = _reach_bone("tip", parent=root)
    reach = _load_reach(monkeypatch)

    with pytest.raises(ValueError, match=r"has only 2 ancestor\(s\); chain_length=3"):
        reach._rest_ancestor_chain(tip, 3)


def _rest_bone(name, head, tail, parent=None):
    bone = types.SimpleNamespace(
        name=name,
        parent=parent,
        children=[],
        head_local=_Vector(head),
        tail_local=_Vector(tail),
        length=(_Vector(tail) - _Vector(head)).length,
    )
    if parent is not None:
        parent.children.append(bone)
    return bone


def _load_reach_with_math(monkeypatch):
    reach = _load_reach(monkeypatch)
    mathutils = sys.modules["mathutils"]
    for name, value in (("Matrix", _Matrix), ("Vector", _Vector), ("Quaternion", _Quaternion), ("Euler", _Euler)):
        monkeypatch.setattr(mathutils, name, value, raising=False)
    return reach


def test_synthesize_pole_finds_the_bend_side_of_a_bent_two_bone_chain(monkeypatch) -> None:
    """
    A bent 2-bone chain, and the round-2 pole-synthesis draft it broke.

    That draft picked the pole reference as `chain[len(chain) // 2]`, which for a 2-bone
    chain is the ROOT bone itself - offset-from-root is then always zero, so it refused
    every 2-bone reach, bent or not. This fixture (upper_arm straight down, forearm bent 90
    degrees to +X - an elbow) is exactly the case that bug broke; the fixed reference is the
    walk [tip_tail, *(bone.head_local for bone in chain)]'s midpoint, which lands on the elbow.
    """
    reach = _load_reach_with_math(monkeypatch)
    upper_arm = _rest_bone("upper_arm", (0, 0, 0), (0, 0, -1))
    forearm = _rest_bone("forearm", (0, 0, -1), (0.8, 0, -1), parent=upper_arm)
    armature = types.SimpleNamespace(matrix_world=_Matrix.Identity(4))

    pole = reach._synthesize_pole(armature, [forearm, upper_arm])

    assert list(pole) == pytest.approx([-1.4055638569974545, 0.0, -2.124451085597964], abs=1e-9)


def test_synthesize_pole_refuses_a_straight_two_bone_rest_chain(monkeypatch) -> None:
    reach = _load_reach_with_math(monkeypatch)
    upper_arm = _rest_bone("upper_arm", (0, 0, 0), (0, 0, -1))
    forearm = _rest_bone("forearm", (0, 0, -1), (0, 0, -2), parent=upper_arm)
    armature = types.SimpleNamespace(matrix_world=_Matrix.Identity(4))

    with pytest.raises(ValueError, match="rest pose is straight"):
        reach._synthesize_pole(armature, [forearm, upper_arm])


def test_synthesize_pole_refuses_a_single_bone_chain(monkeypatch) -> None:
    """A single bone has no distinct middle joint, so no bend direction can be inferred."""
    reach = _load_reach_with_math(monkeypatch)
    lone = _rest_bone("lone", (0, 0, 0), (0, 0, 1))
    armature = types.SimpleNamespace(matrix_world=_Matrix.Identity(4))

    with pytest.raises(ValueError, match="rest pose is straight"):
        reach._synthesize_pole(armature, [lone])


def test_synthesize_pole_refuses_a_chain_whose_root_and_tip_coincide(monkeypatch) -> None:
    reach = _load_reach_with_math(monkeypatch)
    upper_arm = _rest_bone("upper_arm", (0, 0, 0), (0, 0, 0))
    armature = types.SimpleNamespace(matrix_world=_Matrix.Identity(4))

    with pytest.raises(ValueError, match="root and tip coincide"):
        reach._synthesize_pole(armature, [upper_arm])


def test_synthesize_pole_converts_through_the_armatures_world_matrix(monkeypatch) -> None:
    reach = _load_reach_with_math(monkeypatch)
    upper_arm = _rest_bone("upper_arm", (0, 0, 0), (0, 0, -1))
    forearm = _rest_bone("forearm", (0, 0, -1), (0.8, 0, -1), parent=upper_arm)
    armature = types.SimpleNamespace(matrix_world=_Matrix.Translation((5.0, 0.0, 0.0)))

    pole = reach._synthesize_pole(armature, [forearm, upper_arm])

    assert pole[0] == pytest.approx(-1.4055638569974545 + 5.0, abs=1e-9)


def _reach_chain_module(monkeypatch, rest_bones):
    addon, _bpy = load_addon(monkeypatch, data={"objects": {}})
    reach = sys.modules[f"{addon.__name__}.handlers.character_rigging.reach"]
    armature = types.SimpleNamespace(data=types.SimpleNamespace(bones=rest_bones))
    return reach, armature


def test_resolve_reach_chain_refuses_an_unknown_tip_bone(monkeypatch) -> None:
    reach, armature = _reach_chain_module(monkeypatch, {})

    with pytest.raises(ValueError, match="Pose bone not found: forearm"):
        reach._resolve_reach_chain(armature, {"tip_bone": "forearm"}, {})


def test_resolve_reach_chain_reports_resolved_when_chain_length_is_omitted(monkeypatch) -> None:
    root = _reach_bone("upper_arm")
    tip = _reach_bone("forearm", parent=root)
    reach, armature = _reach_chain_module(monkeypatch, {"upper_arm": root, "forearm": tip})

    chain, source = reach._resolve_reach_chain(armature, {"tip_bone": "forearm"}, {})

    assert [bone.name for bone in chain] == ["forearm", "upper_arm"]
    assert source == "resolved"


def test_resolve_reach_chain_reports_explicit_when_chain_length_is_given(monkeypatch) -> None:
    root = _reach_bone("upper_arm")
    tip = _reach_bone("forearm", parent=root)
    reach, armature = _reach_chain_module(monkeypatch, {"upper_arm": root, "forearm": tip})

    chain, source = reach._resolve_reach_chain(armature, {"tip_bone": "forearm", "chain_length": 1}, {})

    assert [bone.name for bone in chain] == ["forearm"]
    assert source == "explicit"


def test_resolve_reach_chain_refuses_a_bone_already_claimed_by_an_earlier_reach(monkeypatch) -> None:
    root = _reach_bone("upper_arm")
    tip = _reach_bone("forearm", parent=root)
    reach, armature = _reach_chain_module(monkeypatch, {"upper_arm": root, "forearm": tip})
    already_captured = {"upper_arm": object()}

    with pytest.raises(ValueError, match=r"Bones claimed by more than one reach: \['upper_arm'\]"):
        reach._resolve_reach_chain(armature, {"tip_bone": "forearm"}, already_captured)


def test_resolved_reach_target_refuses_an_unknown_object_name(monkeypatch) -> None:
    addon, _bpy = load_addon(monkeypatch, data={"objects": {}})
    reach = sys.modules[f"{addon.__name__}.handlers.character_rigging.reach"]

    with pytest.raises(ValueError, match="target_point object not found: SH030_cam"):
        reach._resolved_reach_target(None, "SH030_cam", "target_point")


class _FakeObjects(dict):
    """The slice of `bpy.data.objects` the reach helpers touch: `new`, `get` and `remove`."""

    def new(self, name, _data):
        self[name] = types.SimpleNamespace(name=name, location=None)
        return self[name]

    def remove(self, obj, do_unlink=True) -> None:
        del self[obj.name]


def _reach_geometry_module(monkeypatch, objects):
    addon, bpy = load_addon(monkeypatch, data={"objects": objects})
    bpy.context.collection = types.SimpleNamespace(objects=types.SimpleNamespace(link=lambda _obj: None))
    return sys.modules[f"{addon.__name__}.handlers.character_rigging.reach"]


def test_a_reach_whose_pole_cannot_be_resolved_removes_the_targets_scratch_empty(monkeypatch) -> None:
    """The target's Empty is created before the pole is resolved, so a refusal must undo it."""
    objects = _FakeObjects()
    reach = _reach_geometry_module(monkeypatch, objects)

    with pytest.raises(ValueError, match="pole_target_point object not found: elbow_pole"):
        reach._resolve_reach_geometry(
            types.SimpleNamespace(),
            {"target_point": (0.6, -0.1, 1.1), "pole_target_object_name": "elbow_pole"},
            [],
        )

    assert list(objects) == []


class _PickyConstraint:
    """A constraint whose RNA refuses a wrong-typed value rather than coercing it, as Blender's does."""

    def __setattr__(self, name, value) -> None:
        if name == "pole_target" and not isinstance(value, types.SimpleNamespace):
            raise TypeError("bpy_struct: Constraint.pole_target expects an Object type")
        object.__setattr__(self, name, value)


def test_a_constraint_value_blender_refuses_removes_the_constraint_it_already_added(monkeypatch) -> None:
    """`constraints.new` lands the constraint on the rig before its fields are written."""
    reach = _load_reach(monkeypatch)
    constraints = []
    tip_pose_bone = types.SimpleNamespace(
        constraints=types.SimpleNamespace(
            new=lambda type: constraints.append(_PickyConstraint()) or constraints[-1],
            remove=constraints.remove,
        )
    )

    with pytest.raises(TypeError, match="expects an Object type"):
        reach._configured_reach_constraint(tip_pose_bone, {}, types.SimpleNamespace(), "not-an-object", 2)

    assert constraints == []


def test_bone_reach_requires_exactly_one_target_form() -> None:
    with pytest.raises(ValidationError, match="Supply exactly one of target_point or target_object_name"):
        character_rigging.BoneReach(tip_bone="forearm.L")
    with pytest.raises(ValidationError, match="Supply exactly one of target_point or target_object_name"):
        character_rigging.BoneReach(tip_bone="forearm.L", target_point=(1, 2, 3), target_object_name="SH030_cam")


def test_bone_reach_allows_at_most_one_pole_form() -> None:
    with pytest.raises(ValidationError, match="Supply at most one of pole_target_point or pole_target_object_name"):
        character_rigging.BoneReach(
            tip_bone="forearm.L",
            target_point=(1, 2, 3),
            pole_target_point=(0, 0, 0),
            pole_target_object_name="elbow_pole",
        )


def test_solve_bone_reach_forwards_reaches_and_omits_unset_optional_fields(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        _dispatch,
        "send_command",
        lambda command, params=None: calls.append((command, params)) or {"ok": True},
    )
    reach = character_rigging.BoneReach(tip_bone="forearm.L", target_point=(0.6, -0.1, 1.1))

    asyncio.run(character_rigging.solve_bone_reach(ctx=None, armature_object_name="CHAR1_rig", reaches=[reach]))

    command, params = calls[0]
    assert command == "solve_bone_reach"
    assert params["armature_object_name"] == "CHAR1_rig"
    assert params["reaches"] == [
        {
            "tip_bone": "forearm.L",
            "target_point": (0.6, -0.1, 1.1),
            "pole_angle_degrees": 0.0,
            "use_stretch": False,
            "iterations": 500,
        }
    ]
    assert params["detail"] is False


class _ConstraintStack(list):
    """The slice of `pose_bone.constraints` a reach drives: `new`, `remove` and iteration."""

    def new(self, type) -> types.SimpleNamespace:
        self.append(types.SimpleNamespace(type=type))
        return self[-1]


class _ScratchEmpty:
    """A helper Empty whose `location` write lands in `matrix_world`, as Blender's does."""

    def __init__(self, name) -> None:
        self.name = name
        self.matrix_world = _Matrix.Identity(4)

    @property
    def location(self) -> _Vector:
        return self.matrix_world.translation

    @location.setter
    def location(self, value) -> None:
        self.matrix_world = _Matrix.Translation(tuple(value))


class _ReachObjects(dict):
    """`bpy.data.objects` for a reach: the rig, plus the scratch Empties the solve creates."""

    def new(self, name, _data) -> _ScratchEmpty:
        self[name] = _ScratchEmpty(name)
        return self[name]

    def remove(self, obj, do_unlink=True) -> None:
        del self[obj.name]


class _ReachPoseBone(_PoseBone):
    """A pose bone carrying the armature-space head/tail and constraint stack a reach reads."""

    def __init__(self, name, head, tail, parent=None) -> None:
        super().__init__(name, rest_relative=_Matrix.Translation(head), parent=parent)
        self._head = _Vector(head)
        self._tail = _Vector(tail)
        self.constraints = _ConstraintStack()

    @property
    def head(self) -> _Vector:
        """Placed outright rather than composed: a reach reads where the chain already sits."""
        return self._head

    @property
    def tail(self) -> _Vector:
        """The chain's far end, which is the point a reach solves onto its target."""
        return self._tail


# A bent two-bone arm: a 1.0 m upper arm straight down from the origin, then a 0.8 m forearm
# out along +X. Bent, so a pole can be synthesized from it; 1.8 m of total reach, so a target
# can be placed unambiguously inside or outside it.
_ARM_SHOULDER = (0.0, 0.0, 0.0)


_ARM_ELBOW = (0.0, 0.0, -1.0)


_ARM_WRIST = (0.8, 0.0, -1.0)


_ARM_REACH_M = 1.8


_ARM_POLE = (2.0, 0.0, -1.0)


def _reach_rig(monkeypatch, *, solved_tail=_ARM_WRIST, matrix_world=None):
    """
    Load the addon against the two-bone arm, with the tip's tail already at solved_tail.

    The fake `bpy` runs no IK, which is the point: the tip's tail stays exactly where this
    fixture put it, so every reported distance is one the test chose rather than one a
    solver produced. `tests/blender_character_posing_smoke.py` measures the real solve.

    Args:
        monkeypatch: The test's monkeypatch.
        solved_tail: Where the reach's tip tail sits once the constraint has been evaluated.
        matrix_world: The rig's object matrix; identity when omitted.

    Returns:
        tuple: the server, the `bpy` stub - whose `data.objects` also holds any scratch Empty
        a reach failed to clean up - the rig, and its animation data.

    """
    shoulder_rest = _rest_bone("upper_arm", _ARM_SHOULDER, _ARM_ELBOW)
    forearm_rest = _rest_bone("forearm", _ARM_ELBOW, _ARM_WRIST, parent=shoulder_rest)
    upper_arm = _ReachPoseBone("upper_arm", _ARM_SHOULDER, _ARM_ELBOW)
    forearm = _ReachPoseBone("forearm", _ARM_ELBOW, solved_tail, parent=upper_arm)
    server, rig, animation, _posing_module = _posing(monkeypatch, [upper_arm, forearm], matrix_world=matrix_world)
    rig.data.bones = {"upper_arm": shoulder_rest, "forearm": forearm_rest}
    bpy = sys.modules["bpy"]
    bpy.data.objects = _ReachObjects(bpy.data.objects)
    bpy.context.collection = types.SimpleNamespace(objects=types.SimpleNamespace(link=lambda _obj: None))
    return server, bpy, rig, animation


def _solve(server, target, **kwargs):
    """Solve the two-bone arm's one reach at a world target, with an explicit pole."""
    reach = {"tip_bone": "forearm", "target_point": target, "pole_target_point": _ARM_POLE}
    return server.solve_bone_reach("CHAR1_rig", [reach], **kwargs)


def test_a_reach_inside_its_tolerance_reports_converged_and_says_nothing_else(monkeypatch) -> None:
    """A solve that landed is the quiet case: the caller needs no notice to act on."""
    server, _bpy, _rig, _animation = _reach_rig(monkeypatch)

    reply = _solve(server, (0.80005, 0.0, -1.0))

    entry = reply["reaches"][0]
    assert entry["achieved_error_m"] == pytest.approx(5e-5, abs=1e-12)
    assert entry["converged"] is True
    assert entry["out_of_reach"] is False
    assert reply["warnings"] == []
    # Echoed once for the whole call, not repeated per reach.
    assert reply["tolerance_m"] == pytest.approx(1e-4, abs=0.0)
    assert "tolerance_m" not in entry


def test_a_tighter_tolerance_turns_the_same_solve_into_a_miss(monkeypatch) -> None:
    """The tolerance is the caller's to state: the same geometry passes or fails on it."""
    server, _bpy, _rig, _animation = _reach_rig(monkeypatch)

    reply = _solve(server, (0.80005, 0.0, -1.0), tolerance_m=1e-6)

    assert reply["reaches"][0]["converged"] is False
    assert reply["tolerance_m"] == pytest.approx(1e-6, abs=0.0)


def test_a_reachable_target_the_solve_stalled_short_of_warns_without_blaming_the_rig(monkeypatch) -> None:
    """Naming the cause is the point: this one is worth retrying, an out-of-reach one is not."""
    server, _bpy, _rig, _animation = _reach_rig(monkeypatch)

    reply = _solve(server, (0.8, 0.0, -1.0005))

    entry = reply["reaches"][0]
    assert entry["converged"] is False
    assert entry["out_of_reach"] is False
    assert entry["target_distance_m"] < entry["chain_reach_m"]
    warning = reply["warnings"][0]
    assert "'forearm'" in warning
    assert "achieved_error_m 0.0005" in warning
    assert "tolerance_m 0.0001" in warning
    assert "within the chain's range" in warning
    assert "solve stalled short of it" in warning
    assert "out of reach" not in warning


def test_a_target_beyond_the_chains_reach_is_reported_as_unreachable(monkeypatch) -> None:
    """No pose of this chain reaches 5 m out, so retrying the solve is the wrong next move."""
    server, _bpy, _rig, _animation = _reach_rig(monkeypatch)

    reply = _solve(server, (5.0, 0.0, 0.0))

    entry = reply["reaches"][0]
    assert entry["converged"] is False
    assert entry["out_of_reach"] is True
    assert entry["chain_reach_m"] == pytest.approx(_ARM_REACH_M, abs=1e-12)
    assert entry["target_distance_m"] == pytest.approx(5.0, abs=1e-12)
    warning = reply["warnings"][0]
    assert "'forearm'" in warning
    assert "out of reach" in warning
    assert "target_distance_m 5" in warning
    assert "chain_reach_m 1.8" in warning


def test_the_chains_reach_is_measured_in_world_space_not_in_rest_bone_lengths(monkeypatch) -> None:
    """A rig scaled x2 reaches twice as far, so rest lengths alone would call a hit a miss."""
    doubled = _Matrix([[2.0, 0.0, 0.0, 0.0], [0.0, 2.0, 0.0, 0.0], [0.0, 0.0, 2.0, 0.0], [0.0, 0.0, 0.0, 1.0]])
    server, _bpy, _rig, _animation = _reach_rig(monkeypatch, matrix_world=doubled)

    # 2.5 m out: past the 1.8 m of rest bone, inside the 3.6 m the scaled rig actually spans.
    entry = _solve(server, (2.5, 0.0, 0.0))["reaches"][0]

    assert entry["chain_reach_m"] == pytest.approx(2.0 * _ARM_REACH_M, abs=1e-12)
    assert entry["out_of_reach"] is False


@pytest.mark.parametrize("tolerance", [0.0, -1e-4, float("nan"), float("inf")])
def test_a_tolerance_that_names_no_precision_is_refused_before_the_rig_is_touched(monkeypatch, tolerance) -> None:
    """A bad tolerance must not leave a half-solved pose, a live IK constraint or a stray Empty."""
    server, bpy, _rig, _animation = _reach_rig(monkeypatch)
    before = _matrix_rows(bpy.data.objects["CHAR1_rig"].pose.bones["forearm"])

    with pytest.raises(ValueError, match="tolerance_m must be"):
        _solve(server, (0.8, 0.0, -1.0), tolerance_m=tolerance)

    rig = bpy.data.objects["CHAR1_rig"]
    assert list(bpy.data.objects) == ["CHAR1_rig"]
    assert list(rig.pose.bones["forearm"].constraints) == []
    assert _matrix_rows(rig.pose.bones["forearm"]) == before


def _matrix_rows(pose_bone) -> list:
    """Read the pose bone's basis out as plain numbers, so a comparison is by value."""
    return [list(row) for row in pose_bone.matrix_basis.rows]


def _long_reach_rig(monkeypatch, length):
    """
    Load the addon against a straight chain of `length` bones, tip last.

    Args:
        monkeypatch: The test's monkeypatch.
        length: How many bones the chain carries.

    Returns:
        tuple: the server, and the tip bone's name.

    """
    rest_bones = {}
    pose_bones = []
    rest_parent = None
    pose_parent = None
    for index in range(length):
        name = f"DEF-tentacle_segment_{index:03d}"
        head = (0.0, 0.0, -0.1 * index)
        tail = (0.0, 0.0, -0.1 * (index + 1))
        rest_parent = _rest_bone(name, head, tail, parent=rest_parent)
        rest_bones[name] = rest_parent
        pose_parent = _ReachPoseBone(name, head, tail, parent=pose_parent)
        pose_bones.append(pose_parent)
    server, rig, _animation, _posing_module = _posing(monkeypatch, pose_bones)
    rig.data.bones = rest_bones
    bpy = sys.modules["bpy"]
    bpy.data.objects = _ReachObjects(bpy.data.objects)
    bpy.context.collection = types.SimpleNamespace(objects=types.SimpleNamespace(link=lambda _obj: None))
    return server, pose_bones[-1].name


def test_a_missed_reach_still_warns_after_the_envelope_has_shortened_the_reply(monkeypatch) -> None:
    """
    The notice has to outlive budget fitting, or the longest calls lose it exactly when it matters.

    A 32-bone chain overruns the reply budget, so `ok()` cuts the per-bone page down and adds
    its own shortening notice. The convergence warning rides in the handler's reply rather
    than being appended to the finished envelope, which is what keeps it in the list.
    """
    server, tip = _long_reach_rig(monkeypatch, 32)
    payload = server.solve_bone_reach(
        "CHAR1_rig", [{"tip_bone": tip, "target_point": (9.0, 0.0, 0.0), "pole_target_point": _ARM_POLE}], detail=True
    )

    reply = envelope_for(payload, changed_objects=[])

    assert len(to_json(reply, fallback=str, indent=2)) <= REPLY_BYTE_BUDGET
    kept = len(reply["data"]["reaches"][0]["bones"])
    assert 0 < kept < 32, "the reply must have been shortened for this to prove anything"
    assert any("was shortened to" in warning for warning in reply["warnings"])
    assert any(f"Reach '{tip}' did not converge" in warning for warning in reply["warnings"])
    assert "warnings" not in reply["data"]


def _slot(identifier):
    return types.SimpleNamespace(identifier=identifier)


def _rig_keying_over_root_motion(monkeypatch):
    """
    Load the two-bone arm already driven by an action holding keys, slot included.

    That is the state a reach arrives in: `keyframe_object_transform` keyed the root travel
    into one action first, and the reach names an action of its own.

    Args:
        monkeypatch: The test's monkeypatch.

    Returns:
        tuple: the server, the rig, its animation data, the root-motion action and its slot.

    """
    server, bpy, rig, animation = _reach_rig(monkeypatch)
    root_motion = bpy.data.actions.new("CHAR1_sh030_root")
    root_motion.fcurves.append(_FCurve("location", 0, [(1.0, 0.0), (24.0, 5.0)]))
    root_slot = _slot("OBCHAR1_rig")
    root_motion.slots = (root_slot,)
    animation.action = root_motion
    animation.action_slot = root_slot
    # Blender's animation_data_create() both creates the block and hangs it off the ID, which
    # is what the displacement guard reads.
    rig.animation_data = animation
    reach_action = bpy.data.actions.new("CHAR1_sh030_reach")
    reach_action.slots = (_slot("OBreach"),)
    return server, rig, animation, root_motion, root_slot


def _plant(frames, target=_ARM_WRIST):
    """One reach planting the wrist on a fixed world point across several frames."""
    return {
        "tip_bone": "forearm",
        "pole_target_point": _ARM_POLE,
        "keys": [{"frame": float(frame), "target_point": list(target)} for frame in frames],
    }


def test_a_reach_that_fails_part_way_through_hands_back_the_action_it_arrived_on(monkeypatch) -> None:
    """
    A half-keyed reach left the rig pointed at its own action, with the displacement standing.

    Nothing else covers it: the object-state snapshot does not record `animation_data.action`,
    so the root motion simply stopped driving the character and the only symptom was a shot
    that had stopped moving.
    """
    server, rig, animation, root_motion, root_slot = _rig_keying_over_root_motion(monkeypatch)
    forearm = rig.pose.bones["forearm"]
    first_frame_only = forearm.keyframe_insert
    forearm.keyframe_insert = lambda data_path, frame, group=None: (
        frame <= 1.0 and first_frame_only(data_path, frame, group)
    )

    with pytest.raises(RuntimeError, match="Could not insert key"):
        server.keyframe_bone_reach("CHAR1_rig", "CHAR1_sh030_reach", [_plant((1.0, 2.0))], confirm_displace_action=True)

    assert animation.action is root_motion
    assert animation.action_slot is root_slot
