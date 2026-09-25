"""
Regression coverage for the bone_names filter shared by list_character_bones and get_character_rig_info.

get_character_rig_info's own per-bone payload is heavy enough (envelope, bbone, pose
matrices, locks, constraints) that a fake bone rich enough to exercise it end to end
belongs in tests/blender_character_rigging_phase0_smoke.py, against real bpy attributes,
not here. What belongs here is the filter itself: _selected_bones moved out of posing.py
into primitives.py so both tools share one contract instead of two.
"""

import sys
import types

import pytest

from conftest import load_addon


def _rig_with_bones(monkeypatch: pytest.MonkeyPatch, *names: str):
    """Build a stub armature carrying the named rest bones, in the given order."""
    bones = [types.SimpleNamespace(name=name) for name in names]
    rig = types.SimpleNamespace(
        name="CHAR1_rig",
        type="ARMATURE",
        data=types.SimpleNamespace(name="CHAR1_rigData", bones=bones),
    )
    addon, _bpy = load_addon(monkeypatch, data={"objects": {"CHAR1_rig": rig}})
    primitives = sys.modules[f"{addon.__name__}.handlers.character_rigging.primitives"]
    return primitives, rig


def test_selected_bones_returns_every_bone_in_armature_order_when_unfiltered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primitives, rig = _rig_with_bones(monkeypatch, "root", "spine", "head")

    assert [bone.name for bone in primitives._selected_bones(rig, None)] == ["root", "spine", "head"]


def test_selected_bones_returns_named_bones_in_armature_order_not_request_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_character_rig_info's bone_names must page identically to list_character_bones's."""
    primitives, rig = _rig_with_bones(monkeypatch, *(f"CHAR1_bone_{index:03d}" for index in range(180)))

    filtered = primitives._selected_bones(rig, ["CHAR1_bone_177", "CHAR1_bone_004"])

    assert [bone.name for bone in filtered] == ["CHAR1_bone_004", "CHAR1_bone_177"]


def test_selected_bones_refuses_an_unknown_name(monkeypatch: pytest.MonkeyPatch) -> None:
    primitives, rig = _rig_with_bones(monkeypatch, "head", "spine")

    with pytest.raises(ValueError, match=r"Bones not found in armature 'CHAR1_rig': \['hed'\]"):
        primitives._selected_bones(rig, ["head", "hed"])
