"""
Blender 5.1+ background smoke coverage for Phase 0 character-rigging handlers.

Run with::

    blender --background --factory-startup --python tests/blender_character_rigging_phase0_smoke.py
"""

# Blender's runtime types are intentionally dynamic in this executable harness.

import json
import math
import sys
import tempfile

from pathlib import Path

import bpy

sys.path.append(str(Path(__file__).resolve().parent))
from smoke_addon import load_addon

package_name = "blender_mcp_character_rigging_smoke"
load_addon(package_name)

CharacterRiggingHandlersMixin = sys.modules[f"{package_name}.handlers.character_rigging"].CharacterRiggingHandlersMixin


class Harness(CharacterRiggingHandlersMixin):
    """Expose character-rigging handlers without starting the socket server."""


def mesh_object(name):
    mesh = bpy.data.meshes.new(f"{name} Mesh")
    mesh.from_pydata(
        [(-0.5, 0, 0), (0.5, 0, 0), (-0.5, 0, 2), (0.5, 0, 2)],
        [(0, 1), (1, 3), (3, 2), (2, 0)],
        [(0, 1, 3, 2)],
    )
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


handler = Harness()
created = handler.create_armature(
    "HeroRig",
    "Characters",
    bones=[
        {
            "name": "root",
            "head": (0, 0, 0),
            "tail": (0, 0, 1),
            "collections": ["DEF"],
        },
        {
            "name": "arm.L",
            "head": (0, 0, 1),
            "tail": (1, 0, 1),
            "parent": "root",
            "collections": ["DEF"],
        },
        {
            "name": "target.L",
            "head": (1, 0, 1),
            "tail": (1, 0, 2),
            "use_deform": False,
            "collections": ["CTRL"],
        },
    ],
)
assert created["bones"] == ["root", "arm.L", "target.L"]

body = mesh_object("Body")
binding = handler.bind_mesh_to_armature("HeroRig", [body.name], method="EMPTY_GROUPS")
assert binding["bindings"][0]["modifier"]["target"] == "HeroRig"

unrelated = mesh_object("Unrelated")
unrelated.vertex_groups.new(name="arm.L")

bpy.ops.mesh.primitive_cube_add(size=2.0, location=(0.0, 0.0, 1.0))
auto_body = bpy.context.object
auto_body.name = "Auto Body"
automatic = handler.bind_mesh_to_armature(
    "HeroRig",
    [auto_body.name],
    method="AUTOMATIC",
    replacement_policy="REPLACE",
    confirm_replace_weights=True,
)
assert automatic["bindings"][0]["parent"] is None
assert len([modifier for modifier in auto_body.modifiers if modifier.type == "ARMATURE"]) == 1
handler.set_skin_weights(
    assignments=[
        {
            "mesh_object_name": body.name,
            "group_name": "root",
            "vertex_indices": [0, 1],
            "weight": 1.0,
            "mode": "REPLACE",
        },
        {
            "mesh_object_name": body.name,
            "group_name": "arm.L",
            "vertex_indices": [2, 3],
            "weight": 1.0,
            "mode": "REPLACE",
        },
    ]
)
skin = handler.get_skinning_info("HeroRig", [body.name])
assert skin["meshes"][0]["unweighted_vertices"] == []

renamed = handler.patch_armature_bones(
    "HeroRig",
    [
        {
            "operation": "RENAME",
            "bone_name": "arm.L",
            "new_name": "upper_arm.L",
            "reference_policy": "UPDATE",
        }
    ],
)
assert renamed["renamed_bones"] == {"arm.L": "upper_arm.L"}
assert body.vertex_groups.get("upper_arm.L") is not None
assert unrelated.vertex_groups.get("arm.L") is not None
assert unrelated.vertex_groups.get("upper_arm.L") is None

body.vertex_groups.new(name="Noop")
no_change = handler.set_skin_weights(
    assignments=[
        {
            "mesh_object_name": body.name,
            "group_name": "Noop",
            "vertex_indices": [0],
            "weight": 0.0,
            "mode": "REPLACE",
        }
    ]
)
assert no_change["changed_objects"] == []
body.vertex_groups.new(name="target.L")

configured = handler.configure_armature_bones(
    "HeroRig",
    pose_bone_patches=[{"bone_name": "upper_arm.L", "lock_scale": (True, True, True)}],
)
assert configured["changes"][0]["new"] == [True, True, True]
# A local rig's writes are durable, so nothing is warned about.
assert configured["warnings"] == []

collection_result = handler.manage_bone_collections(
    "HeroRig",
    [
        {"operation": "CREATE", "name": "MCH"},
        {"operation": "ASSIGN", "name": "MCH", "bone_names": ["target.L"]},
    ],
)
assert any(item["name"] == "MCH" for item in collection_result["collections"])

constraint = handler.add_pose_bone_constraint(
    "HeroRig",
    "upper_arm.L",
    {
        "type": "COPY_LOCATION",
        "name": "Follow Target",
        "target_object_name": "HeroRig",
        "subtarget": "target.L",
        "influence": 0.5,
    },
)
assert constraint["constraint"]["type"] == "COPY_LOCATION"

action = bpy.data.actions.new("Rig Action")
action.slots.new(id_type="OBJECT", name="Other Slot")
rig_slot = action.slots.new(id_type="OBJECT", name="Hero Slot")
rig_slot_identifier = rig_slot.identifier
rig = bpy.data.objects["HeroRig"]
rig.animation_data_create()
rig.animation_data.action = action
rig.animation_data.action_slot = rig_slot
layer = action.layers.new("Pose")
strip = layer.strips.new(type="KEYFRAME")
channelbag = strip.channelbag(rig_slot, ensure=True)
curve = channelbag.fcurves.new(data_path='pose.bones["target.L"].location', index=0)
curve.keyframe_points.insert(1.0, 0.0)

action_constraint = handler.add_pose_bone_constraint(
    "HeroRig",
    "root",
    {
        "type": "ACTION",
        "name": "Driven Action",
        "target_object_name": "HeroRig",
        "subtarget": "target.L",
        "action_name": action.name,
        "action_slot_identifier": rig_slot_identifier,
        "frame_start": 1,
        "frame_end": 10,
    },
)
assert action_constraint["constraint"]["action_slot"] == rig_slot_identifier

rollback_constraint = body.constraints.new(type="COPY_LOCATION")
rollback_constraint.name = "Rollback Target"
rollback_constraint.target = rig
rollback_constraint.subtarget = "target.L"
# `structure`, not `references` and not the package: `_rename_references` is defined in
# `references` but called through `structure`'s own module-global binding, so patching it
# anywhere else would not intercept anything.
character_module = sys.modules[f"{package_name}.handlers.character_rigging.structure"]
rename_references = character_module._rename_references


def fail_after_reference_updates(*args):
    rename_references(*args)
    raise RuntimeError("forced external reference failure")


character_module._rename_references = fail_after_reference_updates
try:
    try:
        handler.patch_armature_bones(
            "HeroRig",
            [
                {
                    "operation": "RENAME",
                    "bone_name": "target.L",
                    "new_name": "target.FAIL",
                    "reference_policy": "UPDATE",
                }
            ],
            confirm_animated_rest_changes=True,
        )
    except RuntimeError as exc:
        assert "forced external reference failure" in str(exc)
    else:
        raise AssertionError("Expected the injected reference-update failure")
finally:
    character_module._rename_references = rename_references

assert rig.data.bones.get("target.L") is not None
assert rig.data.bones.get("target.FAIL") is None
assert body.vertex_groups.get("target.L") is not None
assert body.vertex_groups.get("target.FAIL") is None
assert rollback_constraint.subtarget == "target.L"
assert rig.pose.bones["root"].constraints["Driven Action"].subtarget == "target.L"
assert rig.pose.bones["root"].constraints["Driven Action"].action_slot.identifier == rig_slot_identifier
restored_action = rig.animation_data.action
restored_slot = next(slot for slot in restored_action.slots if slot.identifier == rig_slot_identifier)
restored_curves = [
    item
    for action_layer in restored_action.layers
    for action_strip in action_layer.strips
    for bag in action_strip.channelbags
    for item in bag.fcurves
]
assert [item.data_path for item in restored_curves] == ['pose.bones["target.L"].location']

handler.patch_armature_bones(
    "HeroRig",
    [
        {
            "operation": "CREATE",
            "name": "delete_me",
            "head": (0, 1, 0),
            "tail": (0, 1, 1),
            "collections": ["MCH"],
        }
    ],
    confirm_animated_rest_changes=True,
)
body.vertex_groups.new(name="delete_me")
delete_constraint = body.constraints.new(type="COPY_LOCATION")
delete_constraint.name = "Deleted Bone Target"
delete_constraint.target = rig
delete_constraint.subtarget = "delete_me"
delete_curve = (
    next(iter(restored_action.layers))
    .strips[0]
    .channelbag(restored_slot)
    .fcurves.new(
        data_path='pose.bones["delete_me"].location',
        index=0,
    )
)
delete_curve.keyframe_points.insert(1.0, 0.0)
rig["driver_source"] = 0.0
driver_curve = rig.driver_add('["driver_source"]')
driver_variable = driver_curve.driver.variables.new()
driver_variable.type = "TRANSFORMS"
driver_target = driver_variable.targets[0]
driver_target.id = rig
driver_target.bone_target = "delete_me"
driver_target.transform_type = "LOC_X"
driver_target.transform_space = "LOCAL_SPACE"

deleted = handler.patch_armature_bones(
    "HeroRig",
    [
        {
            "operation": "DELETE",
            "bone_name": "delete_me",
            "reference_policy": "REMOVE_REFERENCES",
        }
    ],
    confirm_animated_rest_changes=True,
)
assert deleted["deleted_bones"] == ["delete_me"]
assert body.vertex_groups.get("delete_me") is None
assert delete_constraint.subtarget == ""
current_action = rig.animation_data.action
current_paths = [
    item.data_path
    for action_layer in current_action.layers
    for action_strip in action_layer.strips
    for bag in action_strip.channelbags
    for item in bag.fcurves
]
assert 'pose.bones["delete_me"].location' not in current_paths
assert driver_curve.driver.variables[0].targets[0].bone_target == ""

mirrored = handler.mirror_armature_bones("HeroRig", ["upper_arm.L", "target.L"])
assert mirrored["source_to_target"] == {"upper_arm.L": "upper_arm.R", "target.L": "target.R"}

patched = handler.patch_armature_bones(
    "HeroRig",
    [
        {
            "operation": "CREATE",
            "name": "head",
            "head": (0, 0, 1),
            "tail": (0, 0, 2),
            "parent": "root",
            "use_connect": True,
            "collections": ["DEF"],
        }
    ],
    confirm_animated_rest_changes=True,
)
assert "head" in patched["bone_names"]

child_first = handler.patch_armature_bones(
    "HeroRig",
    [
        {
            "operation": "CREATE",
            "name": "finger_tip.L",
            "head": (2, 0, 1),
            "tail": (3, 0, 1),
            "parent": "finger_base.L",
            "use_connect": True,
            "collections": ["DEF"],
        },
        {
            "operation": "CREATE",
            "name": "finger_base.L",
            "head": (1, 0, 1),
            "tail": (2, 0, 1),
            "parent": "upper_arm.L",
            "use_connect": True,
            "collections": ["DEF"],
        },
    ],
    confirm_animated_rest_changes=True,
)
assert bpy.data.objects["HeroRig"].data.bones["finger_tip.L"].parent.name == "finger_base.L"

cleaned = handler.clean_skin_weights(body.name, "HeroRig", normalize="DEFORM")
assert cleaned["residual_unweighted_vertices"] == []
inspection = handler.get_character_rig_info("HeroRig")
assert inspection["bones"]["total"] == 8
named_inspection = handler.get_character_rig_info("HeroRig", bone_names=["finger_tip.L", "upper_arm.L"])
assert named_inspection["bones"]["total"] == 2
assert {item["name"] for item in named_inspection["bones"]["items"]} == {"finger_tip.L", "upper_arm.L"}
assert {record["name"] for record in named_inspection["pose_bones"]} == {"finger_tip.L", "upper_arm.L"}
validation = handler.validate_character_rig(["HeroRig"], [body.name], frames=[1])
assert validation["summary"]["errors"] == 0
assert bpy.context.scene.frame_current == 1
json.dumps(
    [
        created,
        binding,
        skin,
        configured,
        collection_result,
        constraint,
        action_constraint,
        mirrored,
        patched,
        child_first,
        cleaned,
        inspection,
        named_inspection,
        validation,
    ]
)

# A custom property written on a library override reads back correctly in-session and is gone
# after save and reopen: Blender carries no ID property write into an override. Only a warning
# can tell a caller that, so the warning is asserted against the reopened value that proves it.
override_root = Path(tempfile.mkdtemp(prefix="blender_mcp_override_smoke_"))
source_path = override_root / "source.blend"
shot_path = override_root / "shot.blend"

bpy.ops.wm.read_factory_settings(use_empty=True)
handler.create_armature(
    "LinkRig",
    "LinkColl",
    bones=[{"name": "bone", "head": (0, 0, 0), "tail": (0, 0, 1)}],
)
bpy.data.objects["LinkRig"].pose.bones["bone"]["grip"] = 0.1
bpy.ops.wm.save_as_mainfile(filepath=str(source_path))

bpy.ops.wm.read_factory_settings(use_empty=True)
# `libraries.load` is a context manager at runtime; bpy's stub declares it returning None.
with bpy.data.libraries.load(str(source_path), link=True) as (_source, target):  # pyright: ignore[reportGeneralTypeIssues]
    target.collections = ["LinkColl"]
bpy.data.collections["LinkColl"].override_hierarchy_create(
    bpy.context.scene,
    bpy.context.scene.view_layers[0],
    do_fully_editable=True,
)
overridden = next(obj for obj in bpy.context.scene.objects if obj.type == "ARMATURE")
assert overridden.override_library is not None
override_result = handler.configure_armature_bones(
    overridden.name,
    pose_bone_patches=[{"bone_name": "bone", "custom_properties": {"grip": 0.75}}],
)
assert len(override_result["warnings"]) == 1
assert "library override" in override_result["warnings"][0]
# bpy's stub loses the ID-property protocol, so a custom property reads as None to the checker.
assert math.isclose(float(overridden.pose.bones["bone"]["grip"]), 0.75, abs_tol=1e-6)  # pyright: ignore[reportArgumentType]

bpy.ops.wm.save_as_mainfile(filepath=str(shot_path))
bpy.ops.wm.open_mainfile(filepath=str(shot_path))
reopened = next(obj for obj in bpy.context.scene.objects if obj.type == "ARMATURE")
# The library's own value, not the 0.75 written: the bare write did not survive.
assert math.isclose(float(reopened.pose.bones["bone"]["grip"]), 0.1, abs_tol=1e-6), reopened.pose.bones["bone"]["grip"]  # pyright: ignore[reportArgumentType]
json.dumps(override_result)

print("BLENDER_CHARACTER_RIGGING_PHASE0_SMOKE_OK")
