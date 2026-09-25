"""
Blender background smoke coverage for rigid-body character and scale workflows.

Run with::

    blender --background --factory-startup --python tests/blender_rigid_body_character_and_scale_smoke.py
"""

# Blender runtime types are dynamic in this executable harness.

import json
import sys
import tempfile

from pathlib import Path

import bpy

sys.path.append(str(Path(__file__).resolve().parent))
from smoke_addon import load_addon

package_name = "blender_mcp_rigid_body_character_scale_smoke"
load_addon(package_name)

RigidBodyHandlersMixin = sys.modules[f"{package_name}.handlers.rigid_body"].RigidBodyHandlersMixin


class Harness(RigidBodyHandlersMixin):
    """Expose rigid-body handlers without starting the socket server."""


def add_cube(name, location):
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=location)
    obj = bpy.context.object
    obj.name = name
    return obj


handler = Harness()
scene = bpy.context.scene
scene.name = "Rigid Body Character and Scale"
scene.frame_start = 1
scene.frame_end = 5

source = bpy.context.object
source.name = "Debris Source"
render = add_cube("Render Asset", (3.0, 0.0, 1.0))

handler.configure_rigid_body_world(
    scene.name,
    body_collection_name="Physics Bodies",
    constraint_collection_name="Physics Constraints",
    cache={"frame_start": 1, "frame_end": 5, "frame_step": 1},
)

debris = handler.create_rigid_body_debris_field(
    scene.name,
    "Impact Debris",
    [{"object_name": source.name, "weight": 1.0}],
    3,
    1234,
    {"shape": "BOX", "minimum": (-1.0, -1.0, 2.0), "maximum": (1.0, 1.0, 4.0)},
    100.0,
    {
        "rotation_min_radians": (0.0, 0.0, 0.0),
        "rotation_max_radians": (0.2, 0.2, 0.2),
        "uniform_scale_min": 0.25,
        "uniform_scale_max": 0.5,
    },
)
assert debris["count"] == 3
assert len({record["object"] for record in debris["source_mapping"]}) == 3
debris_repeat = handler.create_rigid_body_debris_field(
    scene.name,
    "Impact Debris Repeat",
    [{"object_name": source.name, "weight": 1.0}],
    3,
    1234,
    {"shape": "BOX", "minimum": (-1.0, -1.0, 2.0), "maximum": (1.0, 1.0, 4.0)},
    100.0,
    {
        "rotation_min_radians": (0.0, 0.0, 0.0),
        "rotation_max_radians": (0.2, 0.2, 0.2),
        "uniform_scale_min": 0.25,
        "uniform_scale_max": 0.5,
    },
)
for first, second in zip(debris["source_mapping"], debris_repeat["source_mapping"], strict=True):
    assert first["location_world"] == second["location_world"]
    assert first["rotation_euler_xyz_radians"] == second["rotation_euler_xyz_radians"]
    assert first["uniform_scale_factor"] == second["uniform_scale_factor"]

proxy_rig = handler.create_rigid_body_proxy_rig(
    scene.name,
    "Hero Proxy Rig",
    [{"render_object_name": render.name, "approximation": "BOX", "driver": "COPY_TRANSFORMS"}],
    verification_frames=[1],
)
assert proxy_rig["mappings"][0]["render_object"] == render.name
assert bpy.data.objects[proxy_rig["mappings"][0]["proxy_object"]].rigid_body is not None

armature_data = bpy.data.armatures.new("Character Armature")
armature = bpy.data.objects.new("Character Rig", armature_data)
scene.collection.objects.link(armature)
bpy.context.view_layer.objects.active = armature
armature.select_set(True)
bpy.ops.object.mode_set(mode="EDIT")
root_bone = armature_data.edit_bones.new("hips")
root_bone.head = (0.0, 0.0, 0.0)
root_bone.tail = (0.0, 1.0, 0.0)
child_bone = armature_data.edit_bones.new("spine")
child_bone.head = (0.0, 1.0, 0.0)
child_bone.tail = (0.0, 2.0, 0.0)
child_bone.parent = root_bone
bpy.ops.object.mode_set(mode="OBJECT")

ragdoll = handler.create_ragdoll_rig(
    scene.name,
    armature.name,
    "Character Ragdoll",
    [
        {"bone_name": "hips", "shape": "BOX", "mass_weight": 2.0},
        {"bone_name": "spine", "shape": "CAPSULE", "mass_weight": 1.0},
    ],
    [
        {
            "parent_bone_name": "hips",
            "child_bone_name": "spine",
            "configuration": {"type": "POINT", "disable_collisions": True},
        }
    ],
    60.0,
)
assert len(ragdoll["bodies"]) == 2
assert abs(ragdoll["total_mass"] - 60.0) < 1e-5
release = handler.animate_rigid_body_release(
    scene.name,
    ragdoll["bodies"][1]["proxy"],
    "RELEASE",
    3,
)
assert release["keyed_frames"] == [2, 3]
released_proxy = bpy.data.objects[ragdoll["bodies"][1]["proxy"]]
pose_driver = released_proxy.constraints[ragdoll["bodies"][1]["pose_driver"]]
scene.frame_set(2)
assert pose_driver.influence > 0.999
scene.frame_set(3)
assert pose_driver.influence < 0.001
scene.frame_set(1)

bake = handler.bake_ragdoll_to_armature(
    scene.name,
    armature.name,
    [{"bone_name": item["bone"], "proxy_object_name": item["proxy"]} for item in ragdoll["bodies"]],
    1,
    2,
    action_name="Character Ragdoll Bake",
)
assert bake["action"] == "Character Ragdoll Bake"
assert all(bake["keyed_frames_by_bone"].values())

analysis = handler.analyze_rigid_body_performance(
    scene.name,
    [record["object"] for record in debris["source_mapping"]],
    sample_frames=[1, 2],
)
assert analysis["sampling"]["timing"]["evaluated_frames"] == 2

with tempfile.TemporaryDirectory() as directory:
    output = Path(directory) / "ragdoll.json"
    exported = handler.export_rigid_body_animation(
        scene.name,
        [item["proxy"] for item in ragdoll["bodies"]],
        str(output),
        "JSON",
        1,
        2,
    )
    assert exported["bytes"] > 0
    assert json.loads(output.read_text(encoding="utf-8"))["schema"] == "blender-mcp-rigid-body-animation-1"

json.dumps([debris, debris_repeat, proxy_rig, ragdoll, release, bake, analysis, exported])
print("BLENDER_RIGID_BODY_CHARACTER_SCALE_SMOKE_OK")
