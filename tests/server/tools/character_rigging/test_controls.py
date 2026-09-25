"""Regression coverage for character control, deformation, and pose workflows."""

import asyncio
import sys
import types

import pytest

from conftest import load_addon
from pydantic import ValidationError
from pydantic_core import to_json

from blender_mcp.server.tools import _dispatch, character_rigging
from blender_mcp.server.tools.envelope import REPLY_BYTE_BUDGET, ok


def _run(function, **kwargs):
    return asyncio.run(function(ctx=None, **kwargs))


def test_all_control_and_deformation_commands_are_registered() -> None:
    names = {
        "transfer_skin_weights",
        "create_ik_chain",
        "create_ik_fk_limb",
        "create_spline_ik_rig",
        "configure_bendy_bones",
        "create_rig_property_driver",
        "assign_bone_custom_shapes",
        "set_character_pose",
        "keyframe_character_pose",
        "create_shape_key_controls",
    }

    assert all(callable(getattr(character_rigging, name)) for name in names)
    assert set(character_rigging.mcp._tool_manager._tools) >= names


def test_deformation_and_pose_models_reject_ambiguous_inputs() -> None:
    with pytest.raises(ValidationError, match="non-AUTO"):
        character_rigging.BendyBonePatch(
            bone_name="spine",
            custom_handle_start="MCH-spine",
        )
    with pytest.raises(ValidationError, match="mutually exclusive"):
        character_rigging.BonePose(
            bone_name="hand.L",
            matrix=((1, 0, 0, 0), (0, 1, 0, 0), (0, 0, 1, 0), (0, 0, 0, 1)),
            location=(1, 2, 3),
        )
    with pytest.raises(ValidationError, match="identity field"):
        character_rigging.DrivenChannel(
            owner="CONSTRAINT",
            object_name="Rig",
            bone_name="shin.L",
            property_name="influence",
        )


def test_irreversible_weight_commit_requires_confirmation() -> None:
    with pytest.raises(ValueError, match="confirm_commit"):
        _run(
            character_rigging.transfer_skin_weights,
            source_mesh_name="Body.LOD0",
            target_mesh_name="Body.LOD1",
            commit=True,
        )


def test_spline_ik_requires_exactly_one_curve_source() -> None:
    with pytest.raises(ValueError, match="either curve_object_name"):
        _run(
            character_rigging.create_spline_ik_rig,
            armature_object_name="Rig",
            chain_bone_names=["spine.001", "spine.002"],
        )
    with pytest.raises(ValueError, match="either curve_object_name"):
        _run(
            character_rigging.create_spline_ik_rig,
            armature_object_name="Rig",
            chain_bone_names=["spine.001", "spine.002"],
            curve_object_name="SpineCurve",
            new_curve_name="OtherCurve",
        )


def test_ik_chain_serializes_explicit_controls(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        _dispatch,
        "send_command",
        lambda command, params=None: calls.append((command, params)) or {"ok": True},
    )

    result = _run(
        character_rigging.create_ik_chain,
        armature_object_name="Rig",
        chain_bone_names=["thigh.L", "shin.L"],
        target_control=character_rigging.ControlBoneDefinition(
            name="foot_ik.L",
            head=(0, 0, 0),
            tail=(0, 0, 0.25),
        ),
        pole_control=character_rigging.PoleControlDefinition(
            name="knee_pole.L",
            head=(0, -1, 1),
            tail=(0, -1, 1.25),
            pole_angle=1.5708,
        ),
    )

    assert result["changed_objects"] == ["Rig"]
    assert calls == [
        (
            "create_ik_chain",
            {
                "armature_object_name": "Rig",
                "chain_bone_names": ["thigh.L", "shin.L"],
                "target_control": {
                    "name": "foot_ik.L",
                    "head": (0.0, 0.0, 0.0),
                    "tail": (0.0, 0.0, 0.25),
                    "collection": "CTRL",
                },
                "pole_control": {
                    "name": "knee_pole.L",
                    "head": (0.0, -1.0, 1.0),
                    "tail": (0.0, -1.0, 1.25),
                    "collection": "CTRL",
                    "pole_angle": 1.5708,
                },
                "constraint_name": "IK",
                "iterations": 500,
                "use_stretch": False,
            },
        )
    ]


def test_pose_keyframe_serializes_typed_channels(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        _dispatch,
        "send_command",
        lambda command, params=None: calls.append((command, params)) or {"ok": True},
    )

    _run(
        character_rigging.keyframe_character_pose,
        armature_object_name="Rig",
        action_name="Walk",
        frame=12.5,
        poses=[
            character_rigging.BonePose(
                bone_name="root",
                location=(0, 1, 0),
                rotation_quaternion=(1, 0, 0, 0),
            )
        ],
        space="WORLD",
        action_policy="REUSE",
    )

    assert calls[0][0] == "keyframe_character_pose"
    assert calls[0][1]["poses"][0]["rotation_quaternion"] == (1.0, 0.0, 0.0, 0.0)
    assert calls[0][1]["space"] == "WORLD"
    assert calls[0][1]["frame"] == pytest.approx(12.5)


def test_shape_key_control_modes_are_typed_and_serialized(monkeypatch) -> None:
    with pytest.raises(ValidationError, match="must be unique"):
        character_rigging.CorrectiveShapeKeyControl(
            shape_key_name="ElbowCorrective",
            inputs=[
                character_rigging.CorrectivePropertyInput(property_name="bend"),
                character_rigging.CorrectivePropertyInput(property_name="bend"),
            ],
        )

    calls = []
    monkeypatch.setattr(
        _dispatch,
        "send_command",
        lambda command, params=None: calls.append((command, params)) or {"ok": True},
    )
    _run(
        character_rigging.create_shape_key_controls,
        mesh_object_name="Body",
        armature_object_name="Rig",
        property_owner="POSE_BONE",
        property_bone_name="settings",
        controls=[
            character_rigging.DirectShapeKeyControl(
                shape_key_name="Smile",
                property_name="smile",
            ),
            character_rigging.SignedShapeKeyControl(
                positive_shape_key_name="SmileWide",
                negative_shape_key_name="Frown",
                property_name="expression",
            ),
            character_rigging.CorrectiveShapeKeyControl(
                shape_key_name="ElbowCorrective",
                inputs=[
                    character_rigging.CorrectivePropertyInput(property_name="bend"),
                    character_rigging.CorrectivePropertyInput(property_name="twist"),
                ],
            ),
        ],
    )

    assert [item["mode"] for item in calls[0][1]["controls"]] == ["DIRECT", "SIGNED", "CORRECTIVE"]


def test_dispatch_exposes_complete_character_surface(monkeypatch) -> None:
    addon, _bpy = load_addon(monkeypatch, data={})
    server = addon.BlenderMCPServer()
    handlers = server._build_command_handlers()
    new_commands = {
        "transfer_skin_weights",
        "create_ik_chain",
        "create_ik_fk_limb",
        "create_spline_ik_rig",
        "configure_bendy_bones",
        "create_rig_property_driver",
        "assign_bone_custom_shapes",
        "set_character_pose",
        "keyframe_character_pose",
        "create_shape_key_controls",
    }

    assert set(handlers) >= new_commands
    assert not {name for name in new_commands if server.command_spec(name).read_only}


def _fake_armature(monkeypatch, bones, *, name="HeroRig", obj_type="ARMATURE", pose_bones=None):
    """Build an addon loaded against a fake `bpy` holding one armature object."""
    flushes = []
    armature = types.SimpleNamespace(
        name=name,
        type=obj_type,
        data=types.SimpleNamespace(name=f"{name}Data", bones=list(bones)),
        pose=types.SimpleNamespace(bones=dict(pose_bones or {})),
        update_from_editmode=lambda: flushes.append(name),
    )
    addon, _bpy = load_addon(monkeypatch, data={"objects": {name: armature}})
    return addon.BlenderMCPServer(), flushes


def _bone(name, parent=None, use_deform=True):
    return types.SimpleNamespace(name=name, parent=parent, use_deform=use_deform)


class _SliderBone:
    """A pose bone's ID property bag, plus the UI data a bounded slider carries."""

    def __init__(self, name, properties, bounds=None) -> None:
        self.name = name
        self._properties = dict(properties)
        self._bounds = dict(bounds or {})

    def keys(self):
        # Blender's own bag lists `_RNA_UI` on files old enough to carry it.
        return [*self._properties, "_RNA_UI"]

    def __getitem__(self, key):
        return self._properties[key]

    def id_properties_ui(self, key):
        if key not in self._properties:
            raise KeyError(key)
        return types.SimpleNamespace(as_dict=lambda: dict(self._bounds.get(key, {})))


def test_bone_listing_is_registered_read_only_and_paginates(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        _dispatch,
        "send_command",
        lambda command, params=None: calls.append((command, params)) or {"ok": True},
    )

    _run(character_rigging.list_character_bones, armature_object_name="HeroRig", limit=200, offset=200)

    expected = {
        "armature_object_name": "HeroRig",
        "limit": 200,
        "offset": 200,
        "rest_axes": False,
        "bone_names": None,
        "custom_properties": False,
        "property_offset": 0,
        "deformed_meshes": False,
        "mesh_offset": 0,
    }
    assert calls == [("list_character_bones", expected)]
    advertised = character_rigging.mcp._tool_manager._tools["list_character_bones"].parameters["properties"]
    assert advertised["limit"]["maximum"] == 200

    calls.clear()
    _run(character_rigging.list_character_bones, armature_object_name="HeroRig", bone_names=["CHAR1_head_jnt"])
    assert calls[0][1]["bone_names"] == ["CHAR1_head_jnt"]

    addon, _bpy = load_addon(monkeypatch, data={})
    server = addon.BlenderMCPServer()
    assert "list_character_bones" in server._build_command_handlers()
    assert server.command_spec("list_character_bones").read_only


def test_bone_listing_validates_the_armature_object(monkeypatch) -> None:
    mesh_server, _flushes = _fake_armature(monkeypatch, [_bone("root")], obj_type="MESH")

    with pytest.raises(ValueError, match="is not an armature"):
        mesh_server.list_character_bones("HeroRig")
    with pytest.raises(ValueError, match="Object not found"):
        mesh_server.list_character_bones("Missing")


def test_bone_listing_bounds_the_requested_page(monkeypatch) -> None:
    server, _flushes = _fake_armature(monkeypatch, [_bone("root")])

    with pytest.raises(ValueError, match="bone_limit must be in"):
        server.list_character_bones("HeroRig", limit=0)
    with pytest.raises(ValueError, match="bone_limit must be in"):
        server.list_character_bones("HeroRig", limit=201)
    with pytest.raises(ValueError, match="bone_offset must be non-negative"):
        server.list_character_bones("HeroRig", offset=-1)


def test_bone_listing_reports_parents_deform_flags_and_continuation(monkeypatch) -> None:
    root = _bone("root")
    spine = _bone("DEF-spine", parent=root)
    ctrl = _bone("CTRL-head", parent=spine, use_deform=False)
    server, flushes = _fake_armature(monkeypatch, [root, spine, ctrl])

    first = server.list_character_bones("HeroRig", limit=2)

    assert first["armature_object"] == "HeroRig"
    assert first["bones"]["items"] == [
        {"name": "root", "parent": None, "deform": True},
        {"name": "DEF-spine", "parent": "root", "deform": True},
    ]
    assert (first["bones"]["total"], first["bones"]["truncated"], first["bones"]["next_offset"]) == (3, True, 2)

    second = server.list_character_bones("HeroRig", limit=2, offset=first["bones"]["next_offset"])

    assert second["bones"]["items"] == [{"name": "CTRL-head", "parent": "DEF-spine", "deform": False}]
    assert (second["bones"]["truncated"], second["bones"]["next_offset"]) == (False, None)
    assert flushes == ["HeroRig", "HeroRig"]


def test_bone_listing_reports_the_sliders_a_pose_call_has_to_name(monkeypatch) -> None:
    """
    A rig's sliders are pose-bone custom properties, and nothing in `shot` mode reported them.

    `keyframe_character_pose` takes these names and refuses one the bone does not carry, so an
    agent that cannot enumerate them has to be handed the list in prose - which is how a
    45-composite, 199-direct face rig ended up documented by hand beside the file.
    `get_character_rig_info` does report them and is a `character-rigging` tool, absent from
    every posing-only process.
    """
    head = _SliderBone(
        "head",
        {"expr_smile": 0.0, "expr_squint": 0.25, "label": "face"},
        bounds={
            "expr_smile": {"min": 0.0, "max": 1.0, "description": "smile"},
            # What Blender 5.2 answers for a float property nobody bounded: its own spelling of
            # "unbounded", which is not a slider range and is not worth two fields a property.
            "expr_squint": {"min": -3.4028234663852886e38, "max": 3.4028234663852886e38},
        },
    )
    server, _flushes = _fake_armature(monkeypatch, [_bone("head")], pose_bones={"head": head})

    quiet = server.list_character_bones("HeroRig")
    reported = server.list_character_bones("HeroRig", custom_properties=True)["bones"]["items"][0]

    assert "custom_properties" not in quiet["bones"]["items"][0], "the names cost bytes; they are opt-in"
    assert reported["custom_properties"] == [
        {"name": "expr_smile", "value": 0.0, "min": 0.0, "max": 1.0},
        {"name": "expr_squint", "value": 0.25},
        {"name": "label", "value": "face"},
    ]
    assert (reported["custom_property_count"], reported["custom_property_next_offset"]) == (3, None)


def _skin(name, *, modifier_for=None, parent=None, parent_type="OBJECT", show_viewport=True):
    """Build a mesh bound to a rig by modifier, by ARMATURE parenting, by both, or by neither."""
    modifiers = (
        [types.SimpleNamespace(type="ARMATURE", object=modifier_for, show_viewport=show_viewport)]
        if modifier_for is not None
        else []
    )
    return types.SimpleNamespace(name=name, type="MESH", modifiers=modifiers, parent=parent, parent_type=parent_type)


def test_bone_listing_names_the_meshes_the_rig_actually_deforms(monkeypatch) -> None:
    """
    Which meshes a rig moves was reachable only by moving a camera to frame them.

    `frame_camera_on_objects(armature_names=...)` reports `armature_meshes` as a side effect of
    a mutation, and `get_character_rig_info` is a `character-rigging` tool absent from a posing
    process. A bone's `deform` flag says a bone deforms something, never what - and the set is
    the prerequisite for naming a mesh to `sample_deformed_geometry`.
    """
    server, _flushes = _fake_armature(monkeypatch, [_bone("head")])
    bpy = sys.modules["bpy"]
    armature = bpy.data.objects["HeroRig"]
    other_rig = types.SimpleNamespace(name="PropRig", type="ARMATURE")
    scene_objects = [
        _skin("body", modifier_for=armature),
        _skin("hair", parent=armature, parent_type="ARMATURE"),
        _skin("coat", modifier_for=armature, parent=armature, parent_type="ARMATURE"),
        _skin("brows", modifier_for=armature, show_viewport=False),
        # Parented for transport, not deformation: it rides the rig without being skinned to it.
        _skin("glasses", parent=armature),
        _skin("crate", modifier_for=other_rig),
        armature,
    ]
    bpy.context.scene.objects = scene_objects

    quiet = server.list_character_bones("HeroRig")
    listed = server.list_character_bones("HeroRig", deformed_meshes=True)["deformed_meshes"]

    assert "deformed_meshes" not in quiet, "an extra section costs bytes; it is opt-in"
    assert listed["items"] == [
        {"object": "body", "binding": "MODIFIER", "modifier_enabled": True},
        {"object": "hair", "binding": "PARENT", "modifier_enabled": None},
        {"object": "coat", "binding": "BOTH", "modifier_enabled": True},
        # The answer to "it is bound, so why does it not move?", which a name list cannot give.
        {"object": "brows", "binding": "MODIFIER", "modifier_enabled": False},
    ]
    assert (listed["total"], listed["truncated"], listed["next_offset"]) == (4, False, None)


def test_a_rig_deforming_more_meshes_than_one_page_is_resumable(monkeypatch) -> None:
    """`truncated` with nowhere to resume from is the one paging shape the envelope forbids."""
    server, _flushes = _fake_armature(monkeypatch, [_bone("head")])
    bpy = sys.modules["bpy"]
    armature = bpy.data.objects["HeroRig"]
    bpy.context.scene.objects = [_skin(f"part_{index:03d}", modifier_for=armature) for index in range(205)]

    first = server.list_character_bones("HeroRig", deformed_meshes=True)["deformed_meshes"]
    resumed = server.list_character_bones("HeroRig", deformed_meshes=True, mesh_offset=first["next_offset"])[
        "deformed_meshes"
    ]

    assert (first["total"], first["truncated"], first["next_offset"]) == (205, True, 200)
    assert len(first["items"]) == 200
    assert [item["object"] for item in resumed["items"]] == [f"part_{index:03d}" for index in range(200, 205)]
    assert (resumed["truncated"], resumed["next_offset"]) == (False, None)
    with pytest.raises(ValueError, match="mesh_offset must be a non-negative integer"):
        server.list_character_bones("HeroRig", deformed_meshes=True, mesh_offset=-1)


def test_a_bone_carrying_more_sliders_than_one_page_is_resumable(monkeypatch) -> None:
    """One face control bone can hold 199 properties, which no 8 KiB reply carries whole."""
    names = [f"sk_{index:03d}" for index in range(45)]
    face = _SliderBone("face_ctrl", dict.fromkeys(names, 0.0))
    server, _flushes = _fake_armature(monkeypatch, [_bone("face_ctrl")], pose_bones={"face_ctrl": face})

    first = server.list_character_bones("HeroRig", custom_properties=True)["bones"]["items"][0]
    resumed = server.list_character_bones(
        "HeroRig", custom_properties=True, property_offset=first["custom_property_next_offset"]
    )["bones"]["items"][0]

    assert [record["name"] for record in first["custom_properties"]] == names[:40]
    assert (first["custom_property_count"], first["custom_property_next_offset"]) == (45, 40)
    assert [record["name"] for record in resumed["custom_properties"]] == names[40:]
    assert resumed["custom_property_next_offset"] is None
    with pytest.raises(ValueError, match="property_offset must be a non-negative integer"):
        server.list_character_bones("HeroRig", custom_properties=True, property_offset=-1)


# Full-precision floats, as Blender hands a pose matrix back: the rounding is only visible on
# values whose seventh decimal is not zero.
_REST_MATRIX = (
    (1.0, 0.0, 0.0, 0.0),
    (0.0, 1.0, 0.0, 0.3333333333333333),
    (0.0, 0.0, 1.0, 0.0),
    (0.0, 0.0, 0.0, 1.0),
)
_POSED_MATRIX = (
    (0.8660254037844387, -0.49999999999999994, 0.0, 0.0),
    (0.49999999999999994, 0.8660254037844387, 0.0, 0.0),
    (0.0, 0.0, 1.0, 0.12345678901234567),
    (0.0, 0.0, 0.0, 1.0),
)
_ROUNDED_POSED_MATRIX = [
    [0.866025, -0.5, 0.0, 0.0],
    [0.5, 0.866025, 0.0, 0.0],
    [0.0, 0.0, 1.0, 0.123457],
    [0.0, 0.0, 0.0, 1.0],
]


class _FakeMatrix:
    """
    Stand-in for a `mathutils.Matrix`: the pose path builds one, copies it, and lists its rows.

    `decompose` reports an untransformed basis so a channel spec contributes only what it set,
    `LocRotScale` composes what that spec asked for, and no pose matrix here is singular.
    """

    def __init__(self, rows) -> None:
        self.rows = [tuple(float(value) for value in row) for row in rows]

    @staticmethod
    def LocRotScale(location, _rotation, scale) -> "_FakeMatrix":  # ruff: ignore[invalid-function-name]
        return _FakeMatrix(
            [
                (scale[0], 0.0, 0.0, location[0]),
                (0.0, scale[1], 0.0, location[1]),
                (0.0, 0.0, scale[2], location[2]),
                (0.0, 0.0, 0.0, 1.0),
            ]
        )

    @staticmethod
    def decompose() -> tuple:
        return (0.0, 0.0, 0.0), (1.0, 0.0, 0.0, 0.0), (1.0, 1.0, 1.0)

    @staticmethod
    def determinant() -> float:
        return 1.0

    def __iter__(self):
        return iter(self.rows)

    def copy(self) -> "_FakeMatrix":
        return _FakeMatrix(self.rows)


class _FakePoseBone:
    """One pose bone that records the matrix, custom properties, and keys the handler writes."""

    def __init__(self, name, matrix=_REST_MATRIX, custom_properties=None) -> None:
        self.name = name
        self.matrix = _FakeMatrix(matrix)
        self.matrix_basis = _FakeMatrix(_REST_MATRIX)
        self.parent_recursive = ()
        self.rotation_mode = "QUATERNION"
        self.custom_properties = dict(custom_properties or {})
        self.keyed = []

    def __contains__(self, key) -> bool:
        return key in self.custom_properties

    def __getitem__(self, key):
        return self.custom_properties[key]

    def __setitem__(self, key, value) -> None:
        self.custom_properties[key] = value

    def keyframe_insert(self, data_path, frame, group) -> bool:
        self.keyed.append((data_path, frame, group))
        return True


class _FakeActions(dict):
    """`bpy.data.actions`: lookup by name plus creation of a slotless, curveless action."""

    def new(self, name) -> types.SimpleNamespace:
        self[name] = types.SimpleNamespace(name=name, slots=())
        return self[name]


# `Object.convert_space`: no bone in these tests has a parent, so pose space is the space given.
def _pose_space_matrix(matrix, **_unused) -> _FakeMatrix:
    return matrix


def _posing_server(monkeypatch, pose_bones):
    """
    Load the addon against a fake rig whose pose bones can be posed and keyed.

    Args:
        monkeypatch: The test's monkeypatch.
        pose_bones: The `_FakePoseBone`s the rig carries.

    Returns:
        The server exposing the pose handlers.

    """
    armature = types.SimpleNamespace(
        name="HeroRig",
        type="ARMATURE",
        data=types.SimpleNamespace(name="HeroRigData", pose_position="POSE", bones=[]),
        pose=types.SimpleNamespace(bones={bone.name: bone for bone in pose_bones}),
        convert_space=_pose_space_matrix,
        animation_data_create=lambda: types.SimpleNamespace(action=None, action_slot=None, action_suitable_slots=()),
    )
    addon, bpy = load_addon(monkeypatch, data={"objects": {"HeroRig": armature}, "actions": _FakeActions()})
    bpy.context.view_layer = types.SimpleNamespace(update=lambda: None)
    bpy.context.scene.frame_current = 1
    bpy.context.scene.frame_set = lambda *_args, **_kwargs: None
    mathutils = sys.modules["mathutils"]
    monkeypatch.setattr(mathutils, "Matrix", _FakeMatrix, raising=False)
    monkeypatch.setattr(mathutils, "Vector", tuple, raising=False)
    return addon.BlenderMCPServer()


def test_pose_report_rounds_the_result_and_omits_the_pre_call_matrix(monkeypatch) -> None:
    server = _posing_server(monkeypatch, [_FakePoseBone("spine")])

    reply = server.set_character_pose("HeroRig", [{"bone_name": "spine", "matrix": _POSED_MATRIX}], space="POSE")

    assert reply["bones"] == [{"bone": "spine", "channels": ["matrix"], "after_pose_matrix": _ROUNDED_POSED_MATRIX}]
    assert reply["changed_bones"] == ["spine"]


def test_pose_detail_restores_the_pre_call_matrix_and_full_precision(monkeypatch) -> None:
    server = _posing_server(monkeypatch, [_FakePoseBone("spine")])

    reply = server.set_character_pose(
        "HeroRig", [{"bone_name": "spine", "matrix": _POSED_MATRIX}], space="POSE", detail=True
    )

    assert reply["bones"] == [
        {
            "bone": "spine",
            "channels": ["matrix"],
            "before_pose_matrix": [list(row) for row in _REST_MATRIX],
            "after_pose_matrix": [list(row) for row in _POSED_MATRIX],
        }
    ]


def test_pose_record_names_the_channels_and_custom_properties_the_call_set(monkeypatch) -> None:
    server = _posing_server(monkeypatch, [_FakePoseBone("hand.L", custom_properties={"ik_blend": 0.0})])

    reply = server.set_character_pose(
        "HeroRig",
        [
            {
                "bone_name": "hand.L",
                "location": (0.1, 0.0, 0.0),
                "scale": (2.0, 2.0, 2.0),
                "custom_properties": {"ik_blend": 1.0},
            }
        ],
    )

    assert reply["bones"][0]["channels"] == ["location", "scale", '["ik_blend"]']


def test_the_budget_shortens_pose_records_but_never_the_changed_bone_names(monkeypatch) -> None:
    bones = [_FakePoseBone(f"DEF-spine.{index:03d}") for index in range(40)]
    server = _posing_server(monkeypatch, bones)

    payload = server.set_character_pose(
        "HeroRig", [{"bone_name": bone.name, "matrix": _POSED_MATRIX} for bone in bones], space="POSE"
    )
    reply = ok(payload, changed_objects=payload["changed_objects"])

    assert len(to_json(reply, fallback=str, indent=2)) <= REPLY_BYTE_BUDGET
    assert reply["data"]["changed_bones"] == [bone.name for bone in bones]
    assert 0 < len(reply["data"]["bones"]) < len(bones)
    assert reply["warnings"][0].startswith(f"bones was shortened to {len(reply['data']['bones'])} of 40 records")


def test_keyframed_pose_names_every_bone_and_reports_no_matrices_by_default(monkeypatch) -> None:
    server = _posing_server(monkeypatch, [_FakePoseBone("spine"), _FakePoseBone("hand.L")])
    poses = [{"bone_name": "spine", "matrix": _POSED_MATRIX}, {"bone_name": "hand.L", "matrix": _POSED_MATRIX}]

    reply = server.keyframe_character_pose("HeroRig", "Walk", 3.0, poses, space="POSE")

    assert reply["changed_bones"] == ["spine", "hand.L"]
    assert "bones" not in reply
    assert {entry["data_path"] for entry in reply["changed_keys"]} == {
        "location",
        "rotation_quaternion",
        "scale",
    }


def test_keyframe_detail_reports_the_pose_that_was_keyed(monkeypatch) -> None:
    server = _posing_server(monkeypatch, [_FakePoseBone("spine")])

    reply = server.keyframe_character_pose(
        "HeroRig", "Walk", 3.0, [{"bone_name": "spine", "matrix": _POSED_MATRIX}], space="POSE", detail=True
    )

    assert reply["bones"] == [
        {
            "bone": "spine",
            "channels": ["matrix"],
            "before_pose_matrix": [list(row) for row in _REST_MATRIX],
            "after_pose_matrix": [list(row) for row in _POSED_MATRIX],
        }
    ]


def test_pose_tools_forward_the_detail_flag(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        _dispatch,
        "send_command",
        lambda command, params=None: calls.append((command, params)) or {"ok": True},
    )
    pose = character_rigging.BonePose(bone_name="root", location=(0, 1, 0))

    _run(character_rigging.set_character_pose, armature_object_name="Rig", poses=[pose])
    _run(
        character_rigging.keyframe_character_pose,
        armature_object_name="Rig",
        action_name="Walk",
        frame=1.0,
        poses=[pose],
        detail=True,
    )

    assert calls[0][1]["detail"] is False
    assert calls[1][1]["detail"] is True
