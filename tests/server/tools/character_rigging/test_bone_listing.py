"""Regression coverage for what list_character_bones reports: rest axes, aim letters, and exact-name pages."""

import math
import types

import pytest

from conftest import load_addon

from .rig_doubles import (
    _Matrix,
)


def test_rest_axes_are_reported_only_when_asked_for(monkeypatch) -> None:
    bone = types.SimpleNamespace(
        name="CHAR1_head_jnt",
        parent=None,
        use_deform=True,
        matrix_local=_Matrix.Rotation(math.pi / 2, 4, "Z"),
    )
    rig = types.SimpleNamespace(
        name="CHAR1_rig",
        type="ARMATURE",
        matrix_world=_Matrix.Identity(4),
        data=types.SimpleNamespace(name="CHAR1_rigData", bones=[bone]),
        update_from_editmode=lambda: None,
    )
    addon, _bpy = load_addon(monkeypatch, data={"objects": {"CHAR1_rig": rig}})
    server = addon.BlenderMCPServer()

    plain = server.list_character_bones("CHAR1_rig")
    with_axes = server.list_character_bones("CHAR1_rig", rest_axes=True)

    assert "rest_axes" not in plain["bones"]["items"][0]
    assert "up_axis" not in plain["bones"]["items"][0]
    assert "length_axis" not in plain
    # A quarter turn about Z sends the bone's rest X to armature +Y and its Y to armature -X.
    assert with_axes["bones"]["items"][0]["rest_axes"] == [0.0, 1.0, 0.0, -1.0, 0.0, 0.0, 0.0, 0.0, 1.0]


# The rest axes a demo runbook recorded for CHAR1_head_jnt, as the rows of a 4x4 whose columns are
# the bone's own X, Y and Z: the length runs along armature +X and the bone's own -X stands up.
_CHAR1_HEAD_REST = _Matrix([[0.0, 1.0, 0.0, 0.0], [0.0, 0.0, -1.0, 0.0], [-1.0, 0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 1.0]])


_CHAR1_HEAD_AXES = [0.0, 0.0, -1.0, 1.0, 0.0, 0.0, 0.0, -1.0, 0.0]


def _char1_head_rig(monkeypatch, matrix_world):
    """
    Build the one bone the runbook misread, on a rig placed by `matrix_world`.

    Args:
        monkeypatch: The test's monkeypatch.
        matrix_world: Where the rig sits in the scene, which is what `up_axis` turns on.

    Returns:
        The server whose `bpy.data.objects` holds the rig.

    """
    bone = types.SimpleNamespace(name="CHAR1_head_jnt", parent=None, use_deform=True, matrix_local=_CHAR1_HEAD_REST)
    rig = types.SimpleNamespace(
        name="CHAR1_rig",
        type="ARMATURE",
        matrix_world=matrix_world,
        data=types.SimpleNamespace(name="CHAR1_rigData", bones=[bone]),
        update_from_editmode=lambda: None,
    )
    addon, _bpy = load_addon(monkeypatch, data={"objects": {"CHAR1_rig": rig}})
    return addon.BlenderMCPServer()


def test_the_rest_axes_are_also_named_in_the_vocabulary_an_aim_takes(monkeypatch) -> None:
    """Nine numbers left the up axis to be worked out, and the derivation is what went wrong."""
    server = _char1_head_rig(monkeypatch, _Matrix.Identity(4))

    reply = server.list_character_bones("CHAR1_rig", rest_axes=True)

    assert reply["bones"]["items"][0]["rest_axes"] == _CHAR1_HEAD_AXES
    assert reply["bones"]["items"][0]["up_axis"] == "-X"
    assert reply["length_axis"] == "Y"


def test_the_up_axis_follows_the_rig_into_the_scene_where_the_nine_numbers_cannot(monkeypatch) -> None:
    """aim_at.up_reference is a world direction, and the reported axes are armature-space."""
    server = _char1_head_rig(monkeypatch, _Matrix.Rotation(1.2, 4, "X"))

    reply = server.list_character_bones("CHAR1_rig", rest_axes=True)

    # The same bone and the same nine numbers as the upright rig; only the rig has moved, and
    # a different bone axis is now the one nearest world up.
    assert reply["bones"]["items"][0]["rest_axes"] == _CHAR1_HEAD_AXES
    assert reply["bones"]["items"][0]["up_axis"] == "-Z"


def test_each_world_direction_is_given_the_bone_axis_that_already_points_that_way(monkeypatch) -> None:
    """
    The table an aim is chosen from, and the one the reply could not otherwise support.

    The rig is laid over 1.2 rad about X, so four of the six answers differ from the same bone's
    armature-space reading - which is exactly the derivation a caller would otherwise do off
    `rest_axes`, and get wrong.
    """
    server = _char1_head_rig(monkeypatch, _Matrix.Rotation(1.2, 4, "X"))

    item = server.list_character_bones("CHAR1_rig", rest_axes=True)["bones"]["items"][0]

    assert item["aim_axis_for_world"] == {"+X": "Y", "-X": "-Y", "+Y": "X", "-Y": "-X", "+Z": "-Z", "-Z": "Z"}
    # up_axis is this table's "+Z" entry, read off the same derivation rather than beside it.
    assert item["up_axis"] == item["aim_axis_for_world"]["+Z"]


def test_the_same_bone_standing_upright_names_different_axes_for_the_same_directions(monkeypatch) -> None:
    """Only the rig's object matrix differs, and it is the half of the answer rest_axes omits."""
    server = _char1_head_rig(monkeypatch, _Matrix.Identity(4))

    item = server.list_character_bones("CHAR1_rig", rest_axes=True)["bones"]["items"][0]

    assert item["rest_axes"] == _CHAR1_HEAD_AXES
    assert item["aim_axis_for_world"]["+Y"] == "-Z"
    assert item["aim_axis_for_world"]["+Z"] == "-X"


def test_a_rig_scaled_to_nothing_names_no_axis_for_any_direction(monkeypatch) -> None:
    """No axis points anywhere, so there is no answer to give and none is invented."""
    flattened = _Matrix([[0.0, 0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 1.0]])
    server = _char1_head_rig(monkeypatch, flattened)

    item = server.list_character_bones("CHAR1_rig", rest_axes=True)["bones"]["items"][0]

    assert item["up_axis"] is None
    assert set(item["aim_axis_for_world"]) == {"+X", "-X", "+Y", "-Y", "+Z", "-Z"}
    assert set(item["aim_axis_for_world"].values()) == {None}


def _rig_with_bones(monkeypatch, *names: str):
    """
    Build a stub armature carrying the named rest bones.

    Args:
        monkeypatch: The test's monkeypatch.
        *names: Bone names, in armature order.

    Returns:
        The server whose `bpy.data.objects` holds the rig.

    """
    bones = [
        types.SimpleNamespace(name=name, parent=None, use_deform=True, matrix_local=_Matrix.Rotation(0.0, 4, "Z"))
        for name in names
    ]
    rig = types.SimpleNamespace(
        name="CHAR1_rig",
        type="ARMATURE",
        matrix_world=_Matrix.Identity(4),
        data=types.SimpleNamespace(name="CHAR1_rigData", bones=bones),
        update_from_editmode=lambda: None,
    )
    addon, _bpy = load_addon(monkeypatch, data={"objects": {"CHAR1_rig": rig}})
    return addon.BlenderMCPServer()


def test_named_bones_are_returned_in_one_page_instead_of_paged_to(monkeypatch) -> None:
    """Reading three bones' axes off a production rig took six calls; naming them takes one."""
    server = _rig_with_bones(monkeypatch, *(f"CHAR1_bone_{index:03d}" for index in range(180)))

    filtered = server.list_character_bones("CHAR1_rig", rest_axes=True, bone_names=["CHAR1_bone_177", "CHAR1_bone_004"])

    assert [item["name"] for item in filtered["bones"]["items"]] == ["CHAR1_bone_004", "CHAR1_bone_177"]
    assert filtered["bones"]["total"] == 2
    assert filtered["bones"]["truncated"] is False
    assert filtered["bones"]["next_offset"] is None
    assert all("rest_axes" in item for item in filtered["bones"]["items"])


def test_an_unknown_bone_name_is_refused_rather_than_silently_dropped(monkeypatch) -> None:
    """A caller asking for three bones and receiving two would pose the wrong one."""
    server = _rig_with_bones(monkeypatch, "CHAR1_head_jnt", "CHAR1_spine03_skn_jnt")

    with pytest.raises(ValueError, match=r"Bones not found in armature 'CHAR1_rig': \['CHAR1_hed_jnt'\]"):
        server.list_character_bones("CHAR1_rig", bone_names=["CHAR1_head_jnt", "CHAR1_hed_jnt"])


@pytest.mark.parametrize("value", [[], ["  "], [7], "CHAR1_head_jnt"])
def test_a_malformed_bone_name_filter_is_refused(monkeypatch, value) -> None:
    """The filter decides which bones are read, so a wrong shape must not read the whole rig."""
    server = _rig_with_bones(monkeypatch, "CHAR1_head_jnt")

    with pytest.raises(ValueError, match="bone_names"):
        server.list_character_bones("CHAR1_rig", bone_names=value)


def test_no_filter_still_lists_every_bone(monkeypatch) -> None:
    """The filter is opt-in: omitting it must not narrow anything."""
    server = _rig_with_bones(monkeypatch, "CHAR1_head_jnt", "CHAR1_spine03_skn_jnt")

    assert server.list_character_bones("CHAR1_rig")["bones"]["total"] == 2
