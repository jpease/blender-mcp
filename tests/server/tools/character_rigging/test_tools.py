"""Regression coverage for the Phase 0 character-rigging surface."""

import asyncio
import sys
import types

import pytest

from conftest import load_addon
from pydantic import ValidationError

from blender_mcp.server.tools import _dispatch, character_rigging

from .rig_doubles import _Action, _FCurve, _head_rig, _SceneObjects


def _run(function, **kwargs):
    return asyncio.run(function(ctx=None, **kwargs))


def test_all_twelve_phase_zero_character_commands_are_registered() -> None:
    names = {
        "get_character_rig_info",
        "get_skinning_info",
        "create_armature",
        "patch_armature_bones",
        "mirror_armature_bones",
        "manage_bone_collections",
        "configure_armature_bones",
        "bind_mesh_to_armature",
        "set_skin_weights",
        "clean_skin_weights",
        "add_pose_bone_constraint",
        "validate_character_rig",
    }

    assert all(callable(getattr(character_rigging, name)) for name in names)
    assert set(character_rigging.mcp._tool_manager._tools) >= names


def test_character_models_reject_unknown_and_nonfinite_values() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        character_rigging.BoneBehaviorPatch(bone_name="DEF-spine", arbitrary_rna=True)  # pyright: ignore[reportCallIssue]
    with pytest.raises(ValidationError):
        character_rigging.InitialBone(name="Bone", head=(0, 0, 0), tail=(0, 1, float("inf")))
    with pytest.raises(ValidationError, match="sum to 1"):
        character_rigging.NormalizedVertexWeights(
            mesh_object_name="Body",
            vertex_index=3,
            weights={"DEF-spine": 0.8},
        )


def test_create_armature_serializes_typed_hierarchy(monkeypatch) -> None:
    calls = []

    def fake_call(command, params=None):
        calls.append((command, params))
        return {"ok": True}

    monkeypatch.setattr(_dispatch, "send_command", fake_call)
    result = _run(
        character_rigging.create_armature,
        name="HeroRig",
        collection_name="Characters",
        bones=[
            character_rigging.InitialBone(name="root", head=(0, 0, 0), tail=(0, 0, 1)),
            character_rigging.InitialBone(
                name="spine",
                head=(0, 0, 1),
                tail=(0, 0, 2),
                parent="root",
                use_connect=True,
                collections=["DEF"],
            ),
        ],
    )

    assert result["ok"] is True
    assert result["changed_objects"] == ["HeroRig"]
    assert calls[0][0] == "create_armature"
    assert calls[0][1]["bones"][1]["parent"] == "root"
    assert calls[0][1]["world_transform"]["rotation_quaternion"] == (1.0, 0.0, 0.0, 0.0)


def test_pose_constraint_is_discriminated_and_serialized(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        _dispatch,
        "send_command",
        lambda command, params=None: calls.append((command, params)) or {"ok": True},
    )

    _run(
        character_rigging.add_pose_bone_constraint,
        armature_object_name="HeroRig",
        bone_name="shin.L",
        constraint=character_rigging.IKConstraintSpec(
            name="Leg IK",
            target_object_name="HeroRig",
            subtarget="foot_ik.L",
            chain_count=2,
        ),
    )

    assert calls[0][1]["constraint"]["name"] == "Leg IK"
    assert calls[0][1]["constraint"]["type"] == "IK"
    assert calls[0][1]["constraint"]["subtarget"] == "foot_ik.L"
    assert calls[0][1]["constraint"]["chain_count"] == 2


def test_destructive_weight_policies_require_confirmation() -> None:
    with pytest.raises(Exception, match="confirm_replace_weights"):
        _run(
            character_rigging.bind_mesh_to_armature,
            armature_object_name="Rig",
            mesh_object_names=["Body"],
            replacement_policy="REPLACE",
        )


def test_destructive_collection_membership_changes_require_confirmation() -> None:
    with pytest.raises(ValidationError, match="confirm_destructive"):
        character_rigging.CollectionAssign(
            name="CTRL",
            bone_names=["hand.L"],
            replace_memberships=True,
        )
    with pytest.raises(ValidationError, match="confirm_destructive"):
        character_rigging.CollectionUnassign(name="CTRL", bone_names=["hand.L"])
    with pytest.raises(ValidationError, match="confirm_destructive"):
        character_rigging.CollectionRemove(name="CTRL")
    with pytest.raises(Exception, match="confirm_remove_orphan_groups"):
        _run(
            character_rigging.clean_skin_weights,
            mesh_object_name="Body",
            remove_orphan_groups=True,
        )


def test_character_dispatch_and_read_only_contract(monkeypatch) -> None:
    addon, _bpy = load_addon(monkeypatch, data={})
    server = addon.BlenderMCPServer()
    handlers = server._build_command_handlers()
    names = {
        "get_character_rig_info",
        "get_skinning_info",
        "sample_deformed_geometry",
        "create_armature",
        "patch_armature_bones",
        "mirror_armature_bones",
        "manage_bone_collections",
        "configure_armature_bones",
        "bind_mesh_to_armature",
        "set_skin_weights",
        "clean_skin_weights",
        "add_pose_bone_constraint",
        "validate_character_rig",
    }

    assert set(handlers) >= names
    assert all(
        server.command_spec(name).read_only
        for name in (
            "get_character_rig_info",
            "get_skinning_info",
            "sample_deformed_geometry",
            "validate_character_rig",
        )
    )
    assert {name for name in names if server.command_spec(name).read_only} == {
        "get_character_rig_info",
        "get_skinning_info",
        "sample_deformed_geometry",
        "validate_character_rig",
    }


def test_hierarchy_preflight_detects_cycles_and_connected_gaps(monkeypatch) -> None:
    addon, _bpy = load_addon(monkeypatch, data={})
    handler = sys.modules[f"{addon.__name__}.handlers.character_rigging"]

    assert handler._hierarchy_cycles({"a": "b", "b": "a"}) == [["a", "b", "a"]]
    with pytest.raises(ValueError, match="head must equal"):
        handler._validate_bone_specs(
            [
                {"name": "root", "head": (0, 0, 0), "tail": (0, 0, 1)},
                {
                    "name": "child",
                    "head": (0, 0, 2),
                    "tail": (0, 0, 3),
                    "parent": "root",
                    "use_connect": True,
                },
            ],
            set(),
        )


def test_patch_preflight_renames_child_parent_and_rejects_orphans(monkeypatch) -> None:
    addon, _bpy = load_addon(monkeypatch, data={})
    handler = sys.modules[f"{addon.__name__}.handlers.character_rigging"]
    specs = [
        {"name": "root", "head": (0, 0, 0), "tail": (0, 0, 1), "parent": None},
        {"name": "child", "head": (0, 0, 1), "tail": (0, 0, 2), "parent": "root"},
    ]

    final, renamed, deleted = handler._apply_patch_to_specs(
        specs,
        [{"operation": "RENAME", "bone_name": "root", "new_name": "pelvis", "reference_policy": "UPDATE"}],
    )

    assert renamed == {"root": "pelvis"}
    assert deleted == []
    assert next(item for item in final if item["name"] == "child")["parent"] == "pelvis"
    with pytest.raises(ValueError, match="reparenting or deleting"):
        handler._apply_patch_to_specs(
            specs,
            [{"operation": "DELETE", "bone_name": "root", "reference_policy": "ERROR"}],
        )


def test_patch_preflight_accepts_child_before_parent_creation(monkeypatch) -> None:
    addon, _bpy = load_addon(monkeypatch, data={})
    handler = sys.modules[f"{addon.__name__}.handlers.character_rigging"]

    final, renamed, deleted = handler._apply_patch_to_specs(
        [],
        [
            {
                "operation": "CREATE",
                "name": "child",
                "head": (0, 0, 1),
                "tail": (0, 0, 2),
                "parent": "root",
                "use_connect": True,
            },
            {
                "operation": "CREATE",
                "name": "root",
                "head": (0, 0, 0),
                "tail": (0, 0, 1),
            },
        ],
    )

    handler._validate_bone_specs(final, set())
    assert renamed == {}
    assert deleted == []


_POSE = ({"bone_name": "CHAR1_head_jnt", "rotation_euler": (0.0, 0.44, 0.0)},)


def _rig_driven_by_root_motion(monkeypatch):
    """
    Load the head rig already driven by an action holding two location keys.

    That is the shot the guard exists for: `keyframe_object_transform` keyed the root motion
    into `CHAR1_sh030_root` first, and the pose call arrives next naming an action of its own.

    Args:
        monkeypatch: The test's monkeypatch.

    Returns:
        tuple: the server, the rig's animation data, and the root-motion action.

    """
    server, rig, animation, _posing, _spine, _head = _head_rig(monkeypatch)
    root_motion = _Action("CHAR1_sh030_root")
    root_motion.fcurves.append(_FCurve("location", 0, [(1.0, 0.0), (24.0, 5.0)]))
    animation.action = root_motion
    # Blender's animation_data_create() both creates the block and hangs it off the ID, which
    # is what the guard reads: it never creates animation data of its own to ask the question.
    rig.animation_data = animation
    return server, animation, root_motion


def test_keying_a_pose_refuses_to_displace_an_action_that_holds_keys(monkeypatch) -> None:
    """Displacing the root motion is how a shot's characters end up standing still."""
    server, animation, root_motion = _rig_driven_by_root_motion(monkeypatch)

    with pytest.raises(ValueError, match="CHAR1_sh030_root"):
        server.keyframe_character_pose("CHAR1_rig", "CHAR1_sh030_pose", 1.0, list(_POSE))

    assert animation.action is root_motion
    # A refusal that had already made the new action would leave a stray empty action behind.
    assert sys.modules["bpy"].data.actions.get("CHAR1_sh030_pose") is None


def test_confirming_the_displacement_moves_the_rig_onto_the_new_action(monkeypatch) -> None:
    """The caller may well mean it - the confirmation is what says so."""
    server, animation, _root_motion = _rig_driven_by_root_motion(monkeypatch)

    reply = server.keyframe_character_pose("CHAR1_rig", "CHAR1_sh030_pose", 1.0, list(_POSE), confirm_displace_action=True)

    assert reply["assigned_action"] == "CHAR1_sh030_pose"
    assert reply["unassigned_action"] == "CHAR1_sh030_root"
    assert animation.action.name == "CHAR1_sh030_pose"


def test_ensure_keys_into_an_existing_action_where_create_refuses_it(monkeypatch) -> None:
    """ENSURE is the default because keying a second frame of the same shot must not be a new action."""
    server, _rig, _animation, _posing, _spine, _head = _head_rig(monkeypatch)
    actions = sys.modules["bpy"].data.actions
    existing = actions.new("CHAR1_sh030_pose")

    reply = server.keyframe_character_pose("CHAR1_rig", "CHAR1_sh030_pose", 1.0, list(_POSE))

    assert reply["action"] == "CHAR1_sh030_pose"
    assert actions["CHAR1_sh030_pose"] is existing
    with pytest.raises(ValueError, match="Action already exists"):
        server.keyframe_character_pose("CHAR1_rig", "CHAR1_sh030_pose", 2.0, list(_POSE), action_policy="CREATE")


class _BoneCollections(list):
    """`Armature.collections_all`, doubling as `Armature.collections` for the root-level edits."""

    def get(self, name, default=None):
        return next((collection for collection in self if collection.name == name), default)

    def new(self, name):
        collection = types.SimpleNamespace(
            name=name,
            parent=None,
            index=len(self),
            child_number=len(self),
            is_visible=True,
            is_visible_effectively=True,
            is_solo=False,
            bones=[],
        )
        self.append(collection)
        return collection


class _ArmatureData:
    """The armature datablock a rest-data edit copies, edits, and swaps onto every user."""

    is_editable = True

    def __init__(self, name):
        self.name = name
        self.bones = []
        self.collections_all = _BoneCollections()
        self.collections = self.collections_all

    def copy(self):
        return _ArmatureData(self.name)


def test_a_rest_edit_on_widely_shared_armature_data_counts_its_users_and_names_only_the_rig(monkeypatch) -> None:
    """A dozen rigs sharing one armature are counted and sampled; the rig the caller named is the change."""
    shared = _ArmatureData("Crowd Skeleton")
    rigs = [types.SimpleNamespace(name=f"Crowd_{index:02d}", type="ARMATURE", data=shared) for index in range(12)]
    body = types.SimpleNamespace(name="Body", type="MESH", data=types.SimpleNamespace(name="Body Mesh"))
    objects = _SceneObjects({obj.name: obj for obj in [*reversed(rigs), body]})
    armatures = types.SimpleNamespace(remove=lambda _datablock, do_unlink=False: None)
    addon, _bpy = load_addon(monkeypatch, data={"objects": objects, "armatures": armatures})

    result = addon.BlenderMCPServer().manage_bone_collections("Crowd_07", [{"operation": "CREATE", "name": "MCH"}])

    edited = rigs[7].data
    assert edited is not shared
    assert all(rig.data is edited for rig in rigs)
    assert result["data_users_changed"] == {
        "total": 12,
        "by_type": {"ARMATURE": 12},
        "limit": 10,
        "returned_count": 10,
        "truncated": True,
        "names": [f"Crowd_{index:02d}" for index in range(10)],
    }
    assert result["changed_objects"] == ["Crowd_07"]
    assert result["changed_resources"] == ["Crowd Skeleton"]
