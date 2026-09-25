"""
Regression coverage for get_character_rig_info's paging: its shared bone_names filter and its dependent-mesh page.

get_character_rig_info's own per-bone payload is heavy enough (envelope, bbone, pose
matrices, locks, constraints) that a fake bone rich enough to exercise it end to end
belongs in tests/blender_character_rigging_phase0_smoke.py, against real bpy attributes,
not here. What belongs here is the filter itself - _selected_bones moved out of posing.py
into primitives.py so both tools share one contract instead of two - and the dependent-mesh
page, which is armature-level and needs no bone at all.
"""

import sys
import types

import pytest

from conftest import load_addon

from blender_mcp.server.tools.envelope import ok

from .rig_doubles import _Matrix, _SceneObjects

# Enough bound meshes that one page of them is over the reply budget whatever a record costs.
_CROWD = 300


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


def _rig_deforming_a_crowd(monkeypatch: pytest.MonkeyPatch):
    """
    Build a bone-less armature that `_CROWD` meshes are parented to.

    Args:
        monkeypatch: Installs the fake Blender for one test.

    Returns:
        tuple: The loaded add-on's server, and the dependent meshes' names in `bpy.data` order.

    """
    rig = types.SimpleNamespace(
        name="CrowdRig",
        type="ARMATURE",
        matrix_basis=_Matrix.Identity(4),
        matrix_world=_Matrix.Identity(4),
        rotation_mode="XYZ",
        rotation_euler=(0.0, 0.0, 0.0),
        show_in_front=False,
        animation_data=None,
        data=types.SimpleNamespace(
            name="CrowdRigData",
            bones=[],
            collections_all=[],
            pose_position="POSE",
            display_type="OCTAHEDRAL",
            show_axes=False,
            axes_position=0.0,
            show_names=False,
            relation_line_position="TAIL",
            show_bone_custom_shapes=True,
            show_bone_colors=True,
            animation_data=None,
        ),
        pose=types.SimpleNamespace(bones={}),
    )
    names = [f"crowd_extra_{index:03d}_body_geo" for index in range(_CROWD)]
    meshes = [
        types.SimpleNamespace(name=name, type="MESH", modifiers=[], parent=rig, data=types.SimpleNamespace())
        for name in names
    ]
    addon, bpy = load_addon(monkeypatch, data={"objects": _SceneObjects({obj.name: obj for obj in [rig, *meshes]})})
    bpy.context.view_layer = types.SimpleNamespace(update=lambda: None)
    return addon.BlenderMCPServer(), names


def test_a_budget_cut_dependent_mesh_page_resumes_through_its_own_offset(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    The dependent-mesh page is a secondary page, and `offset` pages the bones.

    Its keys sat bare inside a nested dict, so a budget cut told the caller `offset=N` - which
    this tool accepts and spends on the bone page, while the meshes restart from zero.
    """
    server, names = _rig_deforming_a_crowd(monkeypatch)

    first = ok(
        server.get_character_rig_info("CrowdRig", dependent_meshes_limit=_CROWD, include_custom_properties=False)
    )
    kept = len(first["data"]["dependent_meshes"])
    resumed = server.get_character_rig_info(
        "CrowdRig", dependent_meshes_limit=_CROWD, dependent_meshes_offset=kept, include_custom_properties=False
    )

    assert 0 < kept < _CROWD
    assert first["data"]["dependent_meshes_next_offset"] == kept
    assert any(warning.endswith(f"continue with dependent_meshes_offset={kept}.") for warning in first["warnings"])
    assert [record["object"] for record in resumed["dependent_meshes"]] == names[kept:]
