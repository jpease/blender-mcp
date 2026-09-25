"""Regression coverage for pose resolution, goal-directed aims, and pose keyframing."""

import asyncio
import math
import sys
import types

import pytest

from conftest import load_addon
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import ValidationError

from blender_mcp.server.tools import _dispatch, character_rigging
from blender_mcp.server.tools.envelope import envelope_for

from .rig_doubles import (
    _HEAD_REST,
    _RIG_WORLD,
    _SPINE_REST,
    _Action,
    _Actions,
    _Euler,
    _FCurve,
    _head_rig,
    _Matrix,
    _PoseBone,
    _posing,
    _Quaternion,
    _skinned_mesh,
    _target_object,
    _Vector,
)


def test_aim_points_the_named_axis_at_an_object_and_leaves_position_and_scale_alone(monkeypatch) -> None:
    camera = _target_object("SH030_cam", (0.6, -4.7, 0.95))
    _server, rig, _animation, posing, _spine, head = _head_rig(monkeypatch, objects={"SH030_cam": camera})
    before = head.matrix.copy()

    aim = posing._validated_aim(
        rig,
        head,
        {"target_object_name": "SH030_cam", "track_axis": "Z", "up_axis": "-X", "up_reference": (0, 0, 1)},
    )
    matrix = posing._aim_pose_matrix(rig, head, aim)

    direction = ((rig.matrix_world.inverted() @ _Vector((0.6, -4.7, 0.95))) - before.translation).normalized()
    columns = matrix.to_3x3().col
    assert columns[2].dot(direction) == pytest.approx(1.0, abs=1e-12)
    assert columns[0].dot(direction) == pytest.approx(0.0, abs=1e-12)
    # up_axis="-X" leans the bone's -X toward world +Z, so +X leans away from it.
    assert columns[0].dot(rig.matrix_world.inverted().to_3x3() @ _Vector((0, 0, 1))) < 0.0
    # A right-handed basis orients the bone; a left-handed one would mirror it.
    assert matrix.determinant() == pytest.approx(1.0, abs=1e-12)
    assert list(matrix.translation) == pytest.approx(list(before.translation), abs=1e-12)
    assert list(matrix.to_scale()) == pytest.approx([1.0, 1.0, 1.0], abs=1e-12)


def test_aim_at_a_world_point_resolves_through_the_rig_transform(monkeypatch) -> None:
    _server, rig, _animation, posing, _spine, head = _head_rig(monkeypatch)

    aim = posing._validated_aim(rig, head, {"target_point": (0.0, -2.0, 0.75), "track_axis": "Z"})
    matrix = posing._aim_pose_matrix(rig, head, aim)

    direction = ((rig.matrix_world.inverted() @ _Vector((0.0, -2.0, 0.75))) - head.matrix.translation).normalized()
    assert matrix.to_3x3().col[2].dot(direction) == pytest.approx(1.0, abs=1e-12)


def test_aim_rejects_every_direction_it_cannot_define(monkeypatch) -> None:
    _server, rig, _animation, posing, _spine, head = _head_rig(monkeypatch)
    head_world = rig.matrix_world @ head.matrix.translation

    with pytest.raises(ValueError, match="at the head of 'CHAR1_head_jnt'"):
        posing._aim_pose_matrix(
            rig, head, posing._validated_aim(rig, head, {"target_point": tuple(head_world), "track_axis": "Z"})
        )
    forward = tuple(rig.matrix_world @ (head.matrix.translation + _Vector((0.0, 0.0, 0.5))))
    with pytest.raises(ValueError, match="parallel to the aim direction"):
        posing._aim_pose_matrix(
            rig,
            head,
            posing._validated_aim(
                rig, head, {"target_point": forward, "track_axis": "Z", "up_axis": "X", "up_reference": (0, 0, 1)}
            ),
        )
    with pytest.raises(ValueError, match="is a zero vector"):
        posing._validated_aim(
            rig, head, {"target_point": (0, -2, 0.75), "track_axis": "Z", "up_axis": "X", "up_reference": (0, 0, 0)}
        )
    with pytest.raises(ValueError, match="different bone axis than track_axis"):
        posing._validated_aim(rig, head, {"target_point": (0, -2, 0.75), "track_axis": "Z", "up_axis": "-Z"})
    with pytest.raises(ValueError, match="must be one of X, -X, Y, -Y, Z, -Z"):
        posing._validated_aim(rig, head, {"target_point": (0, -2, 0.75), "track_axis": "W"})
    with pytest.raises(ValueError, match="exactly one of target_point or target_object_name"):
        posing._validated_aim(
            rig, head, {"target_point": (0, -2, 0.75), "target_object_name": "SH030_cam", "track_axis": "Z"}
        )
    with pytest.raises(ValueError, match=r"aim_at\.target_object_name not found: SH030_cam"):
        posing._validated_aim(rig, head, {"target_object_name": "SH030_cam", "track_axis": "Z"})


def _target_armature(name, location, bone_name, head, tail) -> types.SimpleNamespace:
    """
    Build a second rig whose pose bone an aim can name.

    `PoseBone.head`/`tail` are armature-space and follow the pose, which is why the aim reads
    them through the object matrix rather than off the rest bone.

    Args:
        name: The object name.
        location: Where the rig sits in the scene.
        bone_name: The pose bone the aim may name.
        head: The bone's armature-space head.
        tail: Its armature-space tail.

    Returns:
        The stub armature object.

    """
    bone = types.SimpleNamespace(name=bone_name, head=_Vector(head), tail=_Vector(tail))
    return types.SimpleNamespace(
        name=name,
        type="ARMATURE",
        matrix_world=_Matrix.Translation(location),
        pose=types.SimpleNamespace(bones={bone_name: bone}),
    )


def _aimed_direction(rig, matrix, world_point):
    """Measure, in armature space, the direction from an aimed bone's head to a world point."""
    return ((rig.matrix_world.inverted() @ _Vector(world_point)) - matrix.translation).normalized()


# A second character standing 2 m away whose head bone sits 1.6 m up, while its object origin
# sits on the floor: aiming at the object aims at its feet.
_OTHER_ORIGIN = (2.0, 1.0, 0.0)


_OTHER_HEAD = (2.0, 1.0, 1.6)


_OTHER_TAIL = (2.0, 1.0, 1.75)


def test_an_aim_at_a_bone_looks_at_the_bone_and_not_at_the_rigs_origin(monkeypatch) -> None:
    """Two characters told to look at each other stared at each other's feet; this is why."""
    other = _target_armature("OtherRig", _OTHER_ORIGIN, "head", (0.0, 0.0, 1.6), (0.0, 0.0, 1.75))
    _server, rig, _animation, posing, _spine, head = _head_rig(monkeypatch, objects={"OtherRig": other})

    aim = posing._validated_aim(
        rig, head, {"target_object_name": "OtherRig", "target_bone_name": "head", "track_axis": "Z", "up_axis": "-X"}
    )
    matrix = posing._aim_pose_matrix(rig, head, aim)

    tracked = matrix.to_3x3().col[2]
    assert tracked.dot(_aimed_direction(rig, matrix, _OTHER_HEAD)) == pytest.approx(1.0, abs=1e-12)
    at_origin = math.degrees(math.acos(min(1.0, tracked.dot(_aimed_direction(rig, matrix, _OTHER_ORIGIN)))))
    assert at_origin > 10.0, f"aiming at the bone and at the origin differ by only {at_origin} degrees"


def test_a_bone_target_can_name_the_tail_or_the_centre_of_the_bone(monkeypatch) -> None:
    other = _target_armature("OtherRig", _OTHER_ORIGIN, "head", (0.0, 0.0, 1.6), (0.0, 0.0, 1.75))
    _server, rig, _animation, posing, _spine, head = _head_rig(monkeypatch, objects={"OtherRig": other})

    for position, expected in (("TAIL", _OTHER_TAIL), ("CENTER", (2.0, 1.0, 1.675))):
        aim = posing._validated_aim(
            rig,
            head,
            {
                "target_object_name": "OtherRig",
                "target_bone_name": "head",
                "target_bone_position": position,
                "track_axis": "Z",
                "up_axis": "-X",
            },
        )
        matrix = posing._aim_pose_matrix(rig, head, aim)
        assert matrix.to_3x3().col[2].dot(_aimed_direction(rig, matrix, expected)) == pytest.approx(1.0, abs=1e-12)


def test_a_bone_target_the_scene_cannot_supply_is_refused_by_name(monkeypatch) -> None:
    other = _target_armature("OtherRig", _OTHER_ORIGIN, "head", (0.0, 0.0, 1.6), (0.0, 0.0, 1.75))
    camera = _target_object("SH030_cam", (0.6, -4.7, 0.95))
    _server, rig, _animation, posing, _spine, head = _head_rig(
        monkeypatch, objects={"OtherRig": other, "SH030_cam": camera}
    )

    with pytest.raises(ValueError, match=r"aim_at\.target_bone_name not found on 'OtherRig': neck"):
        posing._validated_aim(
            rig, head, {"target_object_name": "OtherRig", "target_bone_name": "neck", "track_axis": "Z"}
        )
    with pytest.raises(ValueError, match="needs an armature to live on"):
        posing._validated_aim(
            rig, head, {"target_object_name": "SH030_cam", "target_bone_name": "head", "track_axis": "Z"}
        )
    with pytest.raises(ValueError, match="names a point on a bone"):
        posing._validated_aim(
            rig, head, {"target_object_name": "OtherRig", "target_bone_position": "TAIL", "track_axis": "Z"}
        )


def test_one_axis_named_twice_is_refused_with_the_axes_that_are_still_free(monkeypatch) -> None:
    """
    The tools' own instruction produced this refusal, so the refusal has to carry the remedy.

    `length_axis` is `Y` for every bone and an upright bone's `up_axis` is `Y` as well, so
    "pass both straight through" named one axis twice. Naming the two axes that are left, and
    where each points, is the difference between a rule and a next call.
    """
    _server, rig, _animation, posing, _spine, head = _head_rig(monkeypatch)

    with pytest.raises(ValueError) as refusal:
        posing._validated_aim(rig, head, {"target_point": (0.0, -2.0, 0.75), "track_axis": "Y", "up_axis": "Y"})

    message = str(refusal.value)
    assert "both the Y axis" in message
    # This bone's own X runs along world +X at rest and its Z along world -Y, through a rig that
    # is itself turned 0.55 rad about Z - the part a caller cannot read off `rest_axes`.
    assert "X points +X" in message, message
    assert "Z points -Y" in message, message


def test_posing_an_aim_lands_it_on_the_target_after_the_parent_has_moved(monkeypatch) -> None:
    camera = _target_object("SH030_cam", (0.6, -4.7, 0.95))
    server, rig, _animation, _posing, spine, head = _head_rig(monkeypatch, objects={"SH030_cam": camera})

    reply = server.set_character_pose(
        "CHAR1_rig",
        [
            {"bone_name": spine.name, "rotation_euler": (0.0, 0.4, 0.0)},
            {"bone_name": head.name, "aim_at": {"target_object_name": "SH030_cam", "track_axis": "Z", "up_axis": "-X"}},
        ],
    )

    posed = head.matrix
    direction = ((rig.matrix_world.inverted() @ _Vector((0.6, -4.7, 0.95))) - posed.translation).normalized()
    assert posed.to_3x3().normalized().col[2].dot(direction) == pytest.approx(1.0, abs=1e-12)
    assert reply["bones"][1]["channels"] == ["aim_at"]


def test_a_minimal_arc_aim_past_the_flip_angle_is_refused_rather_than_rolled_arbitrarily(monkeypatch) -> None:
    _server, rig, _animation, posing, _spine, head = _head_rig(monkeypatch)
    # The head's rest turns its +Z onto armature -Y, so a target behind him is a half turn away.
    behind = tuple(rig.matrix_world @ (head.matrix.translation + _Vector((0.0, 2.0, 0.0))))

    with pytest.raises(ValueError, match="without an up reference"):
        aim = posing._validated_aim(rig, head, {"target_point": behind, "track_axis": "Z"})
        posing._aim_pose_matrix(rig, head, aim)

    # The same swing is well defined once the roll is pinned.
    matrix = posing._aim_pose_matrix(
        rig,
        head,
        posing._validated_aim(rig, head, {"target_point": behind, "track_axis": "Z", "up_axis": "Y"}),
    )
    direction = ((rig.matrix_world.inverted() @ _Vector(behind)) - head.matrix.translation).normalized()
    assert matrix.to_3x3().col[2].dot(direction) == pytest.approx(1.0, abs=1e-12)
    assert matrix.to_3x3().col[1].dot(rig.matrix_world.inverted().to_3x3() @ _Vector((0, 0, 1))) > 0.0


def test_rotate_resolves_named_axes_and_vectors_in_degrees(monkeypatch) -> None:
    _server, _rig, _animation, posing, _spine, head = _head_rig(monkeypatch)

    named = posing._validated_rotate({"axis": "-Y", "degrees": 60.160568}, head.name)
    vector = posing._validated_rotate({"axis": (0.0, -2.0, 0.0), "degrees": 60.160568}, head.name)

    assert list(named["axis"]) == [0.0, -1.0, 0.0]
    assert list(vector["axis"]) == pytest.approx([0.0, -1.0, 0.0])
    # The runbook's elbow value is -1.05 rad about the bone's +Y, which is +1.05 about -Y.
    assert named["angle"] == pytest.approx(1.05, abs=1e-6)
    assert named["relative"] is False
    with pytest.raises(ValueError, match=r"rotate\.axis for 'CHAR1_head_jnt' must be a non-zero vector"):
        posing._validated_rotate({"axis": (0.0, 0.0, 0.0), "degrees": 10.0}, head.name)
    with pytest.raises(ValueError, match="requires degrees"):
        posing._validated_rotate({"axis": "Y"}, head.name)


def test_relative_rotate_composes_while_the_default_replaces(monkeypatch) -> None:
    server, _rig, _animation, _posing, _spine, head = _head_rig(monkeypatch)

    server.set_character_pose("CHAR1_rig", [{"bone_name": head.name, "rotate": {"axis": "Z", "degrees": 30.0}}])
    server.set_character_pose("CHAR1_rig", [{"bone_name": head.name, "rotate": {"axis": "Z", "degrees": 30.0}}])
    replaced = head.matrix_basis.to_quaternion()
    server.set_character_pose(
        "CHAR1_rig", [{"bone_name": head.name, "rotate": {"axis": "Z", "degrees": 30.0, "relative": True}}]
    )
    composed = head.matrix_basis.to_quaternion()

    assert 2.0 * math.acos(min(1.0, abs(replaced[0]))) == pytest.approx(math.radians(30.0), abs=1e-9)
    assert 2.0 * math.acos(min(1.0, abs(composed[0]))) == pytest.approx(math.radians(60.0), abs=1e-9)


def test_a_roll_that_moves_nothing_measurable_warns_and_quotes_what_it_measured(monkeypatch) -> None:
    """
    The silent failure the runbook hit twice: the call succeeds, the keys land, nothing bends.

    Blender builds every bone along its own +Y, so a LOCAL rotation about Y turns the bone about
    the line through its head and tail. This head bone carries no child and no skin, so there is
    nothing off that line for the roll to move - and the notice has to say the travel it
    measured, because a warning whose evidence cannot be checked is the one an agent learns to
    skip.
    """
    server, _rig, _animation, _posing, _spine, head = _head_rig(monkeypatch)
    rest_direction = head.matrix.to_3x3().col[1].copy()
    rest_origin = head.matrix.translation.copy()

    rolled = server.set_character_pose(
        "CHAR1_rig", [{"bone_name": head.name, "rotate": {"axis": "-Y", "degrees": 60.0}}]
    )
    rolled_direction = head.matrix.to_3x3().col[1].copy()
    rolled_origin = head.matrix.translation.copy()
    bent = server.set_character_pose("CHAR1_rig", [{"bone_name": head.name, "rotate": {"axis": "Z", "degrees": 60.0}}])

    assert rolled_direction.dot(rest_direction) == pytest.approx(1.0, abs=1e-12)
    assert (rolled_origin - rest_origin).length == pytest.approx(0.0, abs=1e-12)
    assert len(rolled["warnings"]) == 1
    notice = rolled["warnings"][0]
    assert head.name in notice
    assert "length axis" in notice
    assert "moves the furthest thing measured by 0 m" in notice, notice
    # The same call about a perpendicular axis does move the bone, and says nothing.
    assert head.matrix.to_3x3().col[1].dot(rest_direction) == pytest.approx(math.cos(math.radians(60.0)), abs=1e-12)
    assert bent["warnings"] == []


def test_a_roll_that_swings_an_offset_child_bone_says_nothing(monkeypatch) -> None:
    """
    Finding 4's acceptance criterion: a warning that fires has to mean something moved wrong.

    A head bone's length axis is the character's up, so a roll about it is the head turn. On one
    real rig 30 degrees moved the bone's tail 0.000 cm and the face 6.47 cm, and the notice
    called that a mistake - which taught the agent that these warnings were noise, and it then
    dismissed a correct, quantitative cycle warning and lost a thirteen-key walk. The control
    bone here hangs 0.28 m off the head's length axis, so the roll carries it 0.145 m: measured
    motion, and nothing to warn about.
    """
    spine = _PoseBone("CHAR1_spine03_skn_jnt", rest_relative=_SPINE_REST)
    head = _PoseBone("CHAR1_head_jnt", rest_relative=_HEAD_REST, parent=spine)
    control = _PoseBone("CHAR1_face_ctrl", rest_relative=_HEAD_REST, parent=head, use_deform=False)
    server, _rig, _animation, _module = _posing(monkeypatch, [spine, head, control], matrix_world=_RIG_WORLD)
    roll = {"rotate": {"axis": "Y", "degrees": 30.0}}
    rest_control = control.matrix.translation.copy()

    carrier = server.set_character_pose("CHAR1_rig", [{"bone_name": "CHAR1_head_jnt", **roll}])
    travelled = (control.matrix.translation - rest_control).length
    server.set_character_pose("CHAR1_rig", [{"bone_name": "CHAR1_head_jnt", "rotate": {"axis": "Y", "degrees": 0.0}}])
    barren = server.set_character_pose("CHAR1_rig", [{"bone_name": "CHAR1_face_ctrl", **roll}])

    # The measurement, not the opinion: the child really did travel, and by about the chord a
    # 0.28 m radius turns through at 30 degrees.
    assert travelled == pytest.approx(2.0 * 0.28 * math.sin(math.radians(15.0)), abs=1e-6)
    assert carrier["warnings"] == [], "a roll that carried a child bone 14 cm was called inert"
    # The leaf control carries nothing at all, and that one is still worth saying.
    assert len(barren["warnings"]) == 1
    assert "deforms no geometry" in barren["warnings"][0]


def test_a_roll_that_carries_skinned_vertices_off_the_axis_says_nothing(monkeypatch) -> None:
    """
    The mesh is the only witness a leaf deform bone has, so it is the only one that can clear it.

    A jaw or a head bone often has no child bone at all: every part of it an audience sees is
    skin. Reading the rest hierarchy alone cannot tell that from a relay bone, which is exactly
    how the notice came to fire on correct poses, so the weighted vertices are measured too.
    """
    spine = _PoseBone("CHAR1_spine03_skn_jnt", rest_relative=_SPINE_REST)
    head = _PoseBone("CHAR1_head_jnt", rest_relative=_HEAD_REST, parent=spine)
    server, rig, _animation, _module = _posing(monkeypatch, [spine, head], matrix_world=_RIG_WORLD)
    # Two vertices set 9 cm and 5 cm off the head's own world length axis, so the roll carries
    # them 4.7 cm and 2.6 cm - which is the face travelling while the tail stays put.
    origin = rig.matrix_world @ head.bone.head_local
    face = _skinned_mesh(
        "CHAR1_face_msh",
        rig,
        {"CHAR1_head_jnt": [tuple(origin + _Vector((0.09, 0.0, 0.0))), tuple(origin + _Vector((0.0, 0.05, 0.0)))]},
    )
    sys.modules["bpy"].data.objects["CHAR1_face_msh"] = face

    rolled = server.set_character_pose(
        "CHAR1_rig", [{"bone_name": "CHAR1_head_jnt", "rotate": {"axis": "Y", "degrees": 30.0}}]
    )

    assert rolled["warnings"] == [], "a roll that carries skinned geometry is the turn the caller asked for"


def test_a_deforming_bone_with_no_reachable_mesh_says_what_it_did_not_measure(monkeypatch) -> None:
    """
    Not proving the skin stays put is not the same as proving it moves, and the notice says which.

    The old wording asserted the tail and every child head "stay exactly where they are" and
    then talked about the mesh without ever reading one. Here there is no mesh to read, so the
    notice reports the hierarchy result it did measure and names the skin as unmeasured.
    """
    server, _rig, _animation, _posing, _spine, head = _head_rig(monkeypatch)

    rolled = server.set_character_pose(
        "CHAR1_rig", [{"bone_name": head.name, "rotate": {"axis": "Y", "degrees": 30.0}}]
    )

    notice = rolled["warnings"][0]
    assert "no mesh bound to this armature carries a vertex group named after it" in notice, notice
    assert "nothing about the skin was measured here" in notice
    assert "stay exactly where they are" not in notice, "the measurement never covered the skin"


def test_a_bounded_vertex_scan_says_its_radius_is_a_floor(monkeypatch) -> None:
    """
    A million-vertex body cannot be walked once a frame, so the scan stops - and discloses it.

    Every vertex here sits on the bone's own length axis, so the roll moves none of them and the
    warning is correct; what the notice must not do is present a bounded maximum as if it had
    read the whole mesh.
    """
    spine = _PoseBone("CHAR1_spine03_skn_jnt", rest_relative=_SPINE_REST)
    head = _PoseBone("CHAR1_head_jnt", rest_relative=_HEAD_REST, parent=spine, length=0.2)
    server, rig, _animation, module = _posing(monkeypatch, [spine, head], matrix_world=_RIG_WORLD)
    # Every vertex placed exactly on the bone's own world length axis, so each contributes a
    # zero radius and the roll really does move none of them.
    origin = rig.matrix_world @ head.bone.head_local
    along = ((rig.matrix_world @ head.bone.tail_local) - origin).normalized()
    on_axis = [tuple(origin + along * (0.001 * index)) for index in range(module._MAX_TWIST_WEIGHTED_VERTICES + 1)]
    sys.modules["bpy"].data.objects["CHAR1_face_msh"] = _skinned_mesh(
        "CHAR1_face_msh", rig, {"CHAR1_head_jnt": on_axis}
    )

    rolled = server.set_character_pose(
        "CHAR1_rig", [{"bone_name": "CHAR1_head_jnt", "rotate": {"axis": "Y", "degrees": 30.0}}]
    )

    notice = rolled["warnings"][0]
    assert f"{module._MAX_TWIST_WEIGHTED_VERTICES} vertices weighted to it across 1 bound mesh(es)" in notice, notice
    assert "stopped on its own bound, so this is a floor" in notice


def test_a_whole_rig_rolled_about_its_own_length_counts_the_bones_it_cannot_name(monkeypatch) -> None:
    """Warnings are lifted whole and never paged, so one per posed bone would spend the budget."""
    bones = [_PoseBone(f"CHAR1_roll_{index:02d}") for index in range(9)]
    server, _rig, _animation, _module = _posing(monkeypatch, bones)

    reply = server.set_character_pose(
        "CHAR1_rig", [{"bone_name": bone.name, "rotate": {"axis": "Y", "degrees": 45.0}} for bone in bones]
    )

    assert len(reply["warnings"]) == 5, reply["warnings"]
    assert sum(f"Bone '{bone.name}'" in reply["warnings"][0] for bone in bones) == 1
    summary = reply["warnings"][-1]
    assert summary.startswith("5 further bone(s) were rolled about their own length axis")
    assert "CHAR1_roll_04" in summary and "and 1 more" in summary


def _probe_rig(monkeypatch):
    """Build a spine, a head turned a quarter turn about X, and a control 0.28 m off its axis."""
    spine = _PoseBone("CHAR1_spine03_skn_jnt", rest_relative=_SPINE_REST)
    head = _PoseBone("CHAR1_head_jnt", rest_relative=_HEAD_REST, parent=spine)
    control = _PoseBone("CHAR1_face_ctrl", rest_relative=_HEAD_REST, parent=head, use_deform=False)
    server, rig, _animation, module = _posing(monkeypatch, [spine, head, control], matrix_world=_RIG_WORLD)
    return server, rig, module, head, control


def test_the_probe_separates_the_axis_that_swings_a_bone_from_the_one_that_only_rolls_it(monkeypatch) -> None:
    """
    The whole point of the tool: rest_axes names directions, and only a witness names motion.

    Blender builds every bone along its own +Y, so a LOCAL turn about Y carries nothing that
    sits on that line - here the bone's own tail. The other two swing it through a chord of
    2*length*sin(theta/2), and the probe has to show that difference rather than assert it.
    """
    server, _rig, _animation, _module, _spine, head = _head_rig(monkeypatch)

    reply = server.probe_bone_axis("CHAR1_rig", head.name, ["X", "Y", "Z"])

    travel = {record["axis"]: record["travel_m"] for record in reply["axes"]}
    swing = 2.0 * head.length * math.sin(math.radians(15.0) / 2.0)
    assert travel["Y"] == pytest.approx(0.0, abs=1e-9), "the length axis moved the witness"
    assert travel["X"] == pytest.approx(swing, abs=1e-6)
    assert travel["Z"] == pytest.approx(swing, abs=1e-6)
    assert travel["X"] > travel["Y"] * 100.0 + 1e-3
    # With no descendant to read it through, the probe says so rather than guessing a witness.
    assert reply["witness_bone"] == head.name
    assert reply["witness_bone_source"] == "probed_bone"
    assert reply["bone_length_m"] == pytest.approx(head.length, abs=1e-6)


def test_the_sign_of_a_reference_component_follows_the_sign_of_the_turn(monkeypatch) -> None:
    """
    "Which way" is the half of the answer a magnitude cannot carry.

    Whether a wrist roll turns the palm outward or inward is a sign against a direction the
    caller names, so reversing the turn has to reverse the number - otherwise the reply says
    only that something moved, which the caller already knew.
    """
    server, _rig, _animation, _module, _spine, head = _head_rig(monkeypatch)
    directions = {"camera_right": (1.0, 0.0, 0.0)}

    forward = server.probe_bone_axis("CHAR1_rig", head.name, ["X"], degrees=15.0, reference_directions=directions)
    backward = server.probe_bone_axis("CHAR1_rig", head.name, ["X"], degrees=-15.0, reference_directions=directions)

    ahead = forward["axes"][0]["reference_components_m"]["camera_right"]
    behind = backward["axes"][0]["reference_components_m"]["camera_right"]
    assert abs(ahead) > 1e-3, "the reference direction is perpendicular to the travel; it proves nothing"
    assert abs(behind) > 1e-3
    assert ahead * behind < 0.0, f"reversing the turn left the component's sign alone: {ahead} and {behind}"
    # Turning about -X is the same rotation as turning about +X the other way, so these two are
    # the same measurement spelled differently and have to agree exactly, not merely in sign.
    negated = server.probe_bone_axis("CHAR1_rig", head.name, ["-X"], degrees=15.0, reference_directions=directions)
    assert negated["axes"][0]["reference_components_m"]["camera_right"] == pytest.approx(behind, abs=1e-9)


def test_the_probe_defaults_to_the_farthest_descendant_and_names_how_it_chose(monkeypatch) -> None:
    """A shoulder read at the shoulder answers almost nothing; the lever arm is the hand."""
    server, _rig, _module, head, control = _probe_rig(monkeypatch)

    chosen = server.probe_bone_axis("CHAR1_rig", head.name, ["X"])
    named = server.probe_bone_axis("CHAR1_rig", head.name, ["X"], witness_bone_name=head.name)

    assert chosen["witness_bone"] == control.name
    assert chosen["witness_bone_source"] == "farthest_descendant"
    assert named["witness_bone"] == head.name
    assert named["witness_bone_source"] == "explicit"
    assert chosen["axes"][0]["travel_m"] > named["axes"][0]["travel_m"] * 2.0, (
        "the farther witness must report the larger travel, or the default buys nothing"
    )


def _channel_values(pose_bone):
    """Flatten a pose bone's own channel delta, so a restore can be compared value by value."""
    return [value for row in pose_bone.matrix_basis.rows for value in row]


def test_the_probe_hands_the_pose_back_untouched(monkeypatch) -> None:
    """
    The command is read-only, so it skips `mutation_transaction` and the restore is all there is.

    A probe that left a 15-degree trial turn on a bone would corrupt the pose it was called to
    explain, and nothing downstream would put it back.
    """
    server, _rig, _animation, _module, _spine, head = _head_rig(monkeypatch)
    server.set_character_pose("CHAR1_rig", [{"bone_name": head.name, "rotate": {"axis": "Z", "degrees": 22.0}}])
    before = _channel_values(head)

    server.probe_bone_axis("CHAR1_rig", head.name, ["X", "Y", "Z"], degrees=40.0)

    assert _channel_values(head) == pytest.approx(before, abs=1e-12)


def test_a_probe_that_raises_part_way_through_still_hands_the_pose_back(monkeypatch) -> None:
    """The failure mode a read-only command has no transaction to cover: a half-applied trial turn."""
    server, _rig, _animation, _module, _spine, head = _head_rig(monkeypatch)
    server.set_character_pose("CHAR1_rig", [{"bone_name": head.name, "rotate": {"axis": "Z", "degrees": 22.0}}])
    before = _channel_values(head)
    updates = []

    def failing_update():
        updates.append(None)
        # Fourth update: the second axis has been written and the scene is being re-solved.
        if len(updates) == 4:
            raise RuntimeError("depsgraph blew up mid-probe")

    sys.modules["bpy"].context.view_layer.update = failing_update

    with pytest.raises(RuntimeError, match="mid-probe"):
        server.probe_bone_axis("CHAR1_rig", head.name, ["X", "Z"], degrees=40.0)

    assert _channel_values(head) == pytest.approx(before, abs=1e-12)


def test_a_probe_refuses_a_direction_that_names_no_direction_by_name(monkeypatch) -> None:
    """Six named directions in and one silently ignored is a wrong answer nobody can see."""
    server, _rig, _animation, _module, _spine, head = _head_rig(monkeypatch)

    with pytest.raises(ValueError, match=r"reference_directions\['up'\] must be a non-zero vector"):
        server.probe_bone_axis(
            "CHAR1_rig", head.name, ["X"], reference_directions={"camera_right": (1, 0, 0), "up": (0, 0, 0)}
        )
    with pytest.raises(ValueError, match=r"reference_directions\['up'\]\[2\]"):
        server.probe_bone_axis("CHAR1_rig", head.name, ["X"], reference_directions={"up": (0, 0, float("inf"))})


def test_a_probe_refuses_a_repeated_axis_and_an_unknown_one_before_touching_the_bone(monkeypatch) -> None:
    """Two identical probes answer the same number twice; a bad letter must not reach the rig."""
    server, _rig, _animation, _module, _spine, head = _head_rig(monkeypatch)
    before = [list(row) for row in head.matrix_basis.rows]

    with pytest.raises(ValueError, match="Duplicate probe axes: X"):
        server.probe_bone_axis("CHAR1_rig", head.name, ["X", "X"])
    with pytest.raises(ValueError, match="must be one of X, -X, Y, -Y, Z, -Z"):
        server.probe_bone_axis("CHAR1_rig", head.name, ["W"])
    with pytest.raises(ValueError, match="too small to measure"):
        server.probe_bone_axis("CHAR1_rig", head.name, ["X"], degrees=0.0)
    with pytest.raises(ValueError, match="Pose bone not found: CHAR1_nope"):
        server.probe_bone_axis("CHAR1_rig", head.name, ["X"], witness_bone_name="CHAR1_nope")

    assert [list(row) for row in head.matrix_basis.rows] == before


@pytest.mark.parametrize(
    ("spec", "space", "warns"),
    [
        ({"rotation_euler": (0.0, -1.05, 0.0)}, "LOCAL", True),
        ({"rotation_axis_angle": (1.05, 0.0, 1.0, 0.0)}, "LOCAL", True),
        ({"rotation_quaternion": (math.cos(0.525), 0.0, math.sin(0.525), 0.0)}, "LOCAL", True),
        ({"rotate": {"axis": (0.0, -2.0, 0.0), "degrees": 60.0}}, "LOCAL", True),
        ({"rotate": {"axis": "-Y", "degrees": 60.0}}, "LOCAL_WITH_PARENT", True),
        # Two Euler components compose into an axis this cannot read off, so it stays quiet.
        ({"rotation_euler": (0.3, -1.05, 0.0)}, "LOCAL", False),
        # Under POSE the letter names the armature's axis, which says nothing about this bone.
        ({"rotate": {"axis": "-Y", "degrees": 60.0}}, "POSE", False),
        # Too small to be a roll anyone meant; re-applying a pose must not accuse the caller.
        ({"rotate": {"axis": "Y", "degrees": 0.1}}, "LOCAL", False),
        ({"rotate": {"axis": "Z", "degrees": 60.0}}, "LOCAL", False),
    ],
)
def test_the_inert_rotation_notice_reads_every_spelling_of_one_axis_and_only_bone_local_space(
    monkeypatch, spec, space, warns
) -> None:
    server, _rig, _animation, _posing, _spine, head = _head_rig(monkeypatch, rotation_mode="XYZ")

    reply = server.set_character_pose("CHAR1_rig", [{"bone_name": head.name, **spec}], space=space)

    named = [warning for warning in reply["warnings"] if "length axis" in warning]
    assert bool(named) is warns


@pytest.mark.parametrize(
    ("rotation_mode", "expected"),
    [("QUATERNION", "rotation_quaternion"), ("XYZ", "rotation_euler"), ("AXIS_ANGLE", "rotation_axis_angle")],
)
def test_a_resolved_rotation_keys_only_the_bones_native_channel(monkeypatch, rotation_mode, expected) -> None:
    _server, _rig, _animation, posing, _spine, head = _head_rig(monkeypatch, rotation_mode=rotation_mode)

    for spec in ({"aim_at": {}}, {"rotate": {}}, {"rotation_euler": (0, 0, 0)}):
        assert posing._pose_key_paths(head, spec) == [expected]
    assert posing._pose_key_paths(head, {"matrix": ()}) == ["location", "scale", expected]
    assert posing._pose_key_paths(head, {"aim_at": {}, "location": (0, 0, 0)}) == ["location", expected]


def test_an_absolute_space_child_is_resolved_against_the_parent_this_call_moved(monkeypatch) -> None:
    for space in ("POSE", "WORLD", "LOCAL_WITH_PARENT"):
        server, _rig, _animation, _posing, spine, head = _head_rig(monkeypatch)
        posed_head = head.matrix.copy()
        # A rotation-only entry keeps the bone where the pose puts it, so any translation left
        # in matrix_basis is compensation for a parent read before it moved - and nothing keys it.
        server.set_character_pose(
            "CHAR1_rig",
            [
                {"bone_name": spine.name, "rotation_euler": (0.0, 0.4, 0.0)},
                {"bone_name": head.name, "rotation_quaternion": tuple(posed_head.to_quaternion())},
            ],
            space=space,
        )

        assert head.matrix_basis.translation.length == pytest.approx(0.0, abs=1e-9), space
        assert list(head.matrix_basis.to_scale()) == pytest.approx([1.0, 1.0, 1.0], abs=1e-9), space


def test_a_local_space_pose_is_the_channel_value_whatever_the_parent_did(monkeypatch) -> None:
    server, _rig, _animation, _posing, spine, head = _head_rig(monkeypatch)
    turn = (0.0, 0.44, 0.0)

    server.set_character_pose(
        "CHAR1_rig",
        [
            {"bone_name": spine.name, "rotation_euler": (0.0, 0.4, 0.0)},
            {"bone_name": head.name, "rotation_euler": turn},
        ],
    )

    # LOCAL is the bone's own channel delta: the spine moving in the same call cannot touch it.
    expected = _Matrix.LocRotScale((0.0, 0.0, 0.0), _Euler(turn, "XYZ").to_quaternion(), (1.0, 1.0, 1.0))
    assert [value for row in head.matrix_basis.rows for value in row] == pytest.approx(
        [value for row in expected.rows for value in row], abs=1e-15
    )


def test_keying_leaves_the_rig_driven_by_the_action_it_authored(monkeypatch) -> None:
    server, _rig, animation, _posing, _spine, head = _head_rig(monkeypatch)

    reply = server.keyframe_character_pose(
        "CHAR1_rig", "CHAR1_sh030_motion", 1.0, [{"bone_name": head.name, "rotation_euler": (0, 0.44, 0)}]
    )

    assert animation.action.name == "CHAR1_sh030_motion"
    assert reply["assigned_action"] == "CHAR1_sh030_motion"
    assert "unassigned_action" not in reply
    # The envelope's own vocabulary: a changed resource is a plain datablock name, as it is in
    # every other handler. A record here instead was a shape the client had to special-case.
    assert reply["changed_resources"] == ["CHAR1_sh030_motion"]


def test_keying_reports_the_action_it_displaced(monkeypatch) -> None:
    server, _rig, animation, _posing, _spine, head = _head_rig(monkeypatch)
    animation.action = _Action("CHAR1_idle")

    reply = server.keyframe_character_pose(
        "CHAR1_rig", "CHAR1_sh030_motion", 1.0, [{"bone_name": head.name, "rotation_euler": (0, 0.44, 0)}]
    )

    assert reply["unassigned_action"] == "CHAR1_idle"
    assert reply["assigned_action"] == "CHAR1_sh030_motion"
    assert animation.action.name == "CHAR1_sh030_motion"


def test_a_failed_key_hands_the_rig_back_as_it_arrived(monkeypatch) -> None:
    server, _rig, animation, _posing, _spine, head = _head_rig(monkeypatch)
    head.insert_fails = True
    before = [value for row in head.matrix_basis.rows for value in row]

    with pytest.raises(RuntimeError, match="Could not insert key"):
        server.keyframe_character_pose(
            "CHAR1_rig", "CHAR1_sh030_motion", 1.0, [{"bone_name": head.name, "rotation_euler": (0, 0.44, 0)}]
        )

    assert animation.action is None
    assert [value for row in head.matrix_basis.rows for value in row] == pytest.approx(before)


def test_keying_an_aim_without_an_up_reference_is_refused(monkeypatch) -> None:
    camera = _target_object("SH030_cam", (0.6, -4.7, 0.95))
    server, _rig, animation, _posing, _spine, head = _head_rig(monkeypatch, objects={"SH030_cam": camera})

    with pytest.raises(ValueError, match="requires up_axis and up_reference when keying"):
        server.keyframe_character_pose(
            "CHAR1_rig",
            "CHAR1_sh030_motion",
            1.0,
            [{"bone_name": head.name, "aim_at": {"target_object_name": "SH030_cam", "track_axis": "Z"}}],
        )

    assert animation.action is None


def _aim_key(server, head, frame, target, mode="QUATERNION"):
    return server.keyframe_character_pose(
        "CHAR1_rig",
        "CHAR1_sh030_motion",
        frame,
        [{"bone_name": head.name, "aim_at": {"target_point": target, "track_axis": "Z", "up_axis": "-X"}}],
        action_policy="REUSE",
    )


def test_a_keyed_aim_takes_the_short_way_round_from_the_previous_key(monkeypatch) -> None:
    server, rig, animation, posing, _spine, head = _head_rig(monkeypatch)
    action = _Action("CHAR1_sh030_motion")
    sys.modules["bpy"].data.actions["CHAR1_sh030_motion"] = action
    aim = posing._validated_aim(rig, head, {"target_point": (0.0, -2.0, 0.75), "track_axis": "Z", "up_axis": "-X"})
    head.matrix = posing._aim_pose_matrix(rig, head, aim)
    reached = list(head.rotation_quaternion)
    head.matrix_basis = _Matrix.Identity(4)
    # The previous key holds the same orientation spelled with the opposite sign, which is the
    # spelling that interpolates the long way round.
    action.fcurves = [
        _FCurve(f'pose.bones["{head.name}"].rotation_quaternion', index, [(1.0, -value)])
        for index, value in enumerate(reached)
    ]

    _aim_key(server, head, 13.0, (0.0, -2.0, 0.75))

    keyed = next(values for path, frame, _group, values in head.keyed if path == "rotation_quaternion")
    assert sum(left * right for left, right in zip(keyed, [-value for value in reached], strict=True)) > 0.0
    assert keyed == pytest.approx([-value for value in reached], abs=1e-9)
    assert animation.action is action


def test_a_keyed_euler_aim_stays_on_the_previous_keys_branch(monkeypatch) -> None:
    camera = _target_object("SH030_cam", (0.6, -4.7, 0.95))
    server, rig, _animation, posing, _spine, head = _head_rig(
        monkeypatch, objects={"SH030_cam": camera}, rotation_mode="XYZ"
    )
    action = _Action("CHAR1_sh030_motion")
    sys.modules["bpy"].data.actions["CHAR1_sh030_motion"] = action
    aim = posing._validated_aim(rig, head, {"target_object_name": "SH030_cam", "track_axis": "Z", "up_axis": "-X"})
    head.matrix = posing._aim_pose_matrix(rig, head, aim)
    natural = list(head.rotation_euler)
    head.matrix_basis = _Matrix.Identity(4)
    # The previous key sits a whole turn away on every axis: the same orientation, a branch over.
    previous = [value + 2.0 * math.pi for value in natural]
    action.fcurves = [
        _FCurve(f'pose.bones["{head.name}"].rotation_euler', index, [(1.0, value)])
        for index, value in enumerate(previous)
    ]

    server.keyframe_character_pose(
        "CHAR1_rig",
        "CHAR1_sh030_motion",
        13.0,
        [{"bone_name": head.name, "aim_at": {"target_object_name": "SH030_cam", "track_axis": "Z", "up_axis": "-X"}}],
        action_policy="REUSE",
    )

    keyed = next(values for path, _frame, _group, values in head.keyed if path == "rotation_euler")
    jump = max(abs(value - reference) for value, reference in zip(keyed, previous, strict=True))
    naive = max(abs(value - reference) for value, reference in zip(natural, previous, strict=True))
    assert naive == pytest.approx(2.0 * math.pi, abs=1e-9)
    assert math.degrees(jump) < 1e-6


def _cyclic_action(bone_names, extent=(1.0, 25.0)):
    """Install an action whose rotation curves for these bones already carry a Cycles modifier."""
    action = _Action("CHAR1_sh030_motion")
    sys.modules["bpy"].data.actions["CHAR1_sh030_motion"] = action
    action.fcurves = [
        _FCurve(
            f'pose.bones["{name}"].rotation_quaternion',
            index,
            [(extent[0], 0.0), (extent[1], 0.0)],
            modifiers=[types.SimpleNamespace(type="CYCLES")],
        )
        for name in bone_names
        for index in range(4)
    ]
    return action


def test_keying_past_a_cycle_says_the_period_it_just_changed(monkeypatch) -> None:
    """
    The trap that broke a shot's arms: a later gesture silently restretched a 24-frame loop.

    The arms' curves already carried a Cycles modifier over frames 1-25 when a high-five was
    keyed at 162. Blender did exactly what it was asked - the modifier repeats its own curve's
    key extent, so the extent, and the period, became 161 frames - and the arms drifted for
    the rest of the shot instead of striding. Extending a cycle on purpose is legitimate, so
    this must warn and key, never refuse.
    """
    server, _rig, _animation, _posing, _spine, head = _head_rig(monkeypatch)
    _cyclic_action([head.name])

    reply = server.keyframe_character_pose(
        "CHAR1_rig",
        "CHAR1_sh030_motion",
        162.0,
        [{"bone_name": head.name, "rotation_euler": (0.44, 0.0, 0.0)}],
        action_policy="REUSE",
    )

    assert [frame for _path, frame, _group, _values in head.keyed] == [162.0], "the key was refused, not warned about"
    # One bone, four channels, one warning - and it names what changed, not that something did.
    assert len(reply["warnings"]) == 1, reply["warnings"]
    warning = reply["warnings"][0]
    assert f"Bone '{head.name}'" in warning
    assert "frame 162" in warning
    assert "frames 1-25" in warning
    assert "becomes 161 frames instead of 24" in warning
    # Lifted by the envelope, so it survives the page of keys being cut to fit the budget.
    assert warning in envelope_for(reply, changed_objects=[])["warnings"]


def test_keying_inside_an_existing_cycle_warns_about_nothing(monkeypatch) -> None:
    """A key at frame 12 of a 1-25 loop changes no period; warning about it would train the eye off."""
    server, _rig, _animation, _posing, _spine, head = _head_rig(monkeypatch)
    _cyclic_action([head.name])

    reply = server.keyframe_character_pose(
        "CHAR1_rig",
        "CHAR1_sh030_motion",
        12.0,
        [{"bone_name": head.name, "rotation_euler": (0.44, 0.0, 0.0)}],
        action_policy="REUSE",
    )

    assert reply["warnings"] == []


def test_a_whole_rig_keyed_past_its_cycles_counts_the_bones_it_cannot_name(monkeypatch) -> None:
    """
    Warnings are lifted whole and never paged, so one per posed bone would spend the budget.

    A 500-bone pose is a legal call. Naming the first few and counting the rest keeps the
    notice bounded while still saying which bones to look at first.
    """
    bones = [_PoseBone(f"CHAR1_bone_{index:02d}") for index in range(9)]
    server, _rig, _animation, _posing_module = _posing(monkeypatch, bones)
    _cyclic_action([bone.name for bone in bones])

    reply = server.keyframe_character_pose(
        "CHAR1_rig",
        "CHAR1_sh030_motion",
        162.0,
        [{"bone_name": bone.name, "rotation_euler": (0.44, 0.0, 0.0)} for bone in bones],
        action_policy="REUSE",
    )

    assert len(reply["warnings"]) == 5, reply["warnings"]
    assert sum(f"Bone '{bone.name}'" in reply["warnings"][0] for bone in bones) == 1
    summary = reply["warnings"][-1]
    assert summary.startswith("5 further bone(s) keyed at frame 162")
    assert "CHAR1_bone_04" in summary and "and 1 more" in summary


def _batched_aim_rig(monkeypatch):
    """
    Build a rig whose aim target moves with the playhead, so a per-frame aim differs from a stale one.

    Args:
        monkeypatch: The test's monkeypatch.

    Returns:
        tuple: the server, the rig, the head bone, the scene, and `{frame: target location}`.

    """
    camera = _target_object("SH030_cam", (0.0, 0.0, 0.0))
    server, rig, _animation, _posing_module, _spine, head = _head_rig(monkeypatch, objects={"SH030_cam": camera})
    track = {1.0: (3.0, 0.0, 0.9), 5.0: (0.0, 3.0, 0.9), 9.0: (-3.0, 0.0, 0.9)}
    scene = sys.modules["bpy"].context.scene

    def move(frame, subframe=0.0) -> None:
        scene.frame_current = frame + subframe
        camera.matrix_world = _Matrix.Translation(track[frame + subframe])

    scene.frame_set = move
    move(1.0)
    return server, rig, head, scene, track


def _aim_pose(bone_name):
    return {"bone_name": bone_name, "aim_at": {"target_object_name": "SH030_cam", "track_axis": "Z", "up_axis": "-X"}}


def test_a_batched_call_keys_every_frame_at_the_target_that_frame_holds(monkeypatch) -> None:
    """
    Thirteen keys of a stride were thirteen round trips, and each solved against one frame.

    The frames are keyed in ascending order with the playhead on each of them, so an aim at a
    moving target keys three different looks rather than three copies of the one the playhead
    happened to be showing.
    """
    server, rig, head, scene, track = _batched_aim_rig(monkeypatch)

    reply = server.keyframe_character_pose(
        "CHAR1_rig",
        "CHAR1_sh030_motion",
        keys=[{"frame": frame, "poses": [_aim_pose(head.name)]} for frame in (9.0, 1.0, 5.0)],
    )

    assert reply["keyed_frames"] == [1.0, 5.0, 9.0]
    assert reply["changed_bones"] == [head.name]
    assert [entry["frame"] for entry in reply["changed_keys"]] == [1.0, 5.0, 9.0]
    assert scene.frame_current == pytest.approx(1.0), "the playhead was left where the last key was written"
    keyed = {frame: values for _path, frame, _group, values in head.keyed}
    for frame, target in track.items():
        head.rotation_quaternion = _Quaternion(keyed[frame])
        direction = ((rig.matrix_world.inverted() @ _Vector(target)) - head.matrix.translation).normalized()
        landed = head.matrix.to_3x3().normalized().col[2].dot(direction)
        assert landed == pytest.approx(1.0, abs=1e-9), f"frame {frame} was keyed aiming somewhere else"


def test_a_batched_call_restores_the_pose_it_borrowed_for_every_frame(monkeypatch) -> None:
    server, _rig, head, _scene, _track = _batched_aim_rig(monkeypatch)
    before = [value for row in head.matrix_basis.rows for value in row]

    server.keyframe_character_pose(
        "CHAR1_rig",
        "CHAR1_sh030_motion",
        keys=[{"frame": frame, "poses": [_aim_pose(head.name)]} for frame in (1.0, 5.0, 9.0)],
    )

    assert [value for row in head.matrix_basis.rows for value in row] == pytest.approx(before)


def test_a_single_frame_call_still_reports_exactly_what_it_did_before(monkeypatch) -> None:
    """The batched form is an addition: the one-frame reply keeps its shape, plus keyed_frames."""
    server, _rig, _animation, _posing_module, _spine, head = _head_rig(monkeypatch)

    reply = server.keyframe_character_pose(
        "CHAR1_rig", "CHAR1_sh030_motion", 3.0, [{"bone_name": head.name, "location": (0.1, 0.0, 0.0)}], detail=True
    )

    assert reply["keyed_frames"] == [3.0]
    assert reply["changed_keys"] == [{"bone": head.name, "data_path": "location", "frame": 3.0}]
    assert set(reply["bones"][0]) == {"bone", "channels", "before_pose_matrix", "after_pose_matrix"}


def test_a_batched_detail_record_names_the_frame_it_describes(monkeypatch) -> None:
    """A record per bone per frame is unreadable without the frame; one frame carries its own."""
    server, _rig, head, _scene, _track = _batched_aim_rig(monkeypatch)

    reply = server.keyframe_character_pose(
        "CHAR1_rig",
        "CHAR1_sh030_motion",
        keys=[{"frame": frame, "poses": [_aim_pose(head.name)]} for frame in (1.0, 5.0)],
        detail=True,
    )

    assert [record["frame"] for record in reply["bones"]] == [1.0, 5.0]


def test_the_two_call_shapes_are_exclusive_and_named_in_the_refusal(monkeypatch) -> None:
    server, _rig, _animation, _posing_module, _spine, head = _head_rig(monkeypatch)
    pose = [{"bone_name": head.name, "location": (0.1, 0.0, 0.0)}]

    with pytest.raises(ValueError, match="exactly one of frame with poses"):
        server.keyframe_character_pose("CHAR1_rig", "CHAR1_sh030_motion")
    with pytest.raises(ValueError, match="exactly one of frame with poses"):
        server.keyframe_character_pose(
            "CHAR1_rig", "CHAR1_sh030_motion", 1.0, pose, keys=[{"frame": 2.0, "poses": pose}]
        )
    with pytest.raises(ValueError, match="requires both frame and poses"):
        server.keyframe_character_pose("CHAR1_rig", "CHAR1_sh030_motion", 1.0)


def _load_posing(monkeypatch):
    addon, _bpy = load_addon(monkeypatch, data={"objects": {}, "actions": _Actions()})
    return sys.modules[f"{addon.__name__}.handlers.character_rigging.posing"]


def test_a_batch_that_repeats_a_frame_or_outgrows_one_call_is_refused(monkeypatch) -> None:
    """A frame keyed twice in one call would key whichever pose the list happened to end on."""
    posing = _load_posing(monkeypatch)
    pose = [{"bone_name": "CHAR1_head_jnt", "location": (0.1, 0.0, 0.0)}]

    with pytest.raises(ValueError, match=r"keys names the same frame more than once: \[2.0\]"):
        posing._pose_key_requests(None, None, [{"frame": 2.0, "poses": pose}, {"frame": 2.0, "poses": pose}])
    with pytest.raises(ValueError, match="more than the 2000 one call may apply"):
        posing._pose_key_requests(None, None, [{"frame": float(index), "poses": pose * 9} for index in range(250)])
    with pytest.raises(ValueError, match=r"keys\[1\] requires at least one pose entry"):
        posing._pose_key_requests(None, None, [{"frame": 1.0, "poses": pose}, {"frame": 2.0, "poses": []}])


def test_the_batched_form_reaches_the_handler_with_its_frames_intact(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        _dispatch,
        "send_command",
        lambda command, params=None: calls.append((command, params)) or {"ok": True},
    )
    pose = character_rigging.BonePose(bone_name="hand.L", location=(0.1, 0.0, 0.0))

    asyncio.run(
        character_rigging.keyframe_character_pose(
            ctx=None,
            armature_object_name="my_rig",
            action_name="Walk",
            keys=[character_rigging.PoseKeyframe(frame=frame, poses=[pose]) for frame in (1.0, 13.0)],
        )
    )

    params = calls[0][1]
    assert params["frame"] is None
    assert params["poses"] is None
    assert params["keys"] == [
        {"frame": 1.0, "poses": [{"bone_name": "hand.L", "location": (0.1, 0.0, 0.0)}]},
        {"frame": 13.0, "poses": [{"bone_name": "hand.L", "location": (0.1, 0.0, 0.0)}]},
    ]


def test_the_tool_refuses_both_call_shapes_and_a_repeated_frame() -> None:
    pose = character_rigging.BonePose(bone_name="hand.L", location=(0.1, 0.0, 0.0))
    key = character_rigging.PoseKeyframe(frame=1.0, poses=[pose])

    with pytest.raises(ToolError, match="neither form was supplied in full"):
        asyncio.run(
            character_rigging.keyframe_character_pose(ctx=None, armature_object_name="my_rig", action_name="Walk")
        )
    with pytest.raises(ToolError, match="not both"):
        asyncio.run(
            character_rigging.keyframe_character_pose(
                ctx=None, armature_object_name="my_rig", action_name="Walk", frame=1.0, poses=[pose], keys=[key]
            )
        )
    with pytest.raises(ToolError, match=r"keys names the same frame more than once: \[1.0\]"):
        asyncio.run(
            character_rigging.keyframe_character_pose(
                ctx=None, armature_object_name="my_rig", action_name="Walk", keys=[key, key]
            )
        )


def test_a_bone_position_without_a_bone_is_refused_by_the_schema() -> None:
    with pytest.raises(ValidationError, match="target_bone_position names a point on target_bone_name"):
        character_rigging.BoneAim(target_object_name="OtherRig", target_bone_position="TAIL", track_axis="Z")
    with pytest.raises(ValidationError, match="target_bone_name names a bone on target_object_name"):
        character_rigging.BoneAim(target_point=(1.0, 0.0, 0.0), target_bone_name="head", track_axis="Z")


def _override_rig(monkeypatch, *, overridden):
    """Build a rig carrying one bone with a custom property, either local or a library override."""
    bone = _PoseBone("CHAR1_head_jnt")
    bone.custom_properties["ik_blend"] = 0.0
    server, rig, _animation, _posing_module = _posing(monkeypatch, [bone])
    rig.override_library = types.SimpleNamespace(properties=[]) if overridden else None
    return server, bone


def test_a_custom_property_written_onto_a_library_override_says_it_will_not_survive(monkeypatch) -> None:
    """The write reads back correctly in-session and is the library's value again after reopen."""
    server, bone = _override_rig(monkeypatch, overridden=True)

    reply = server.set_character_pose("CHAR1_rig", [{"bone_name": bone.name, "custom_properties": {"ik_blend": 1.0}}])

    assert len(reply["warnings"]) == 1, reply["warnings"]
    assert "library override" in reply["warnings"][0]
    assert bone.name in reply["warnings"][0]


def test_a_local_rig_is_not_warned_about_its_own_custom_properties(monkeypatch) -> None:
    server, bone = _override_rig(monkeypatch, overridden=False)

    reply = server.set_character_pose("CHAR1_rig", [{"bone_name": bone.name, "custom_properties": {"ik_blend": 1.0}}])

    assert reply["warnings"] == []


def test_keying_a_custom_property_onto_an_override_is_not_warned_about(monkeypatch) -> None:
    """A keyed value lands in the action, which is local data, and reopens as written."""
    server, bone = _override_rig(monkeypatch, overridden=True)

    reply = server.keyframe_character_pose(
        "CHAR1_rig", "CHAR1_sh030_motion", 1.0, [{"bone_name": bone.name, "custom_properties": {"ik_blend": 1.0}}]
    )

    assert reply["warnings"] == []
    assert reply["changed_keys"] == [{"bone": bone.name, "data_path": '["ik_blend"]', "frame": 1.0}]
