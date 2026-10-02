"""
Blender background smoke coverage for rigid-body character and scale workflows.

Run with::

    blender --background --factory-startup --python tests/blender_rigid_body_character_and_scale_smoke.py
"""

# Blender runtime types are dynamic in this executable harness.

import itertools
import json
import math
import sys
import tempfile

from pathlib import Path

import bpy
import mathutils

sys.path.append(str(Path(__file__).resolve().parent))
from smoke_addon import load_addon

package_name = "blender_mcp_rigid_body_character_scale_smoke"
load_addon(package_name)

RigidBodyHandlersMixin = sys.modules[f"{package_name}.handlers.rigid_body"].RigidBodyHandlersMixin
_action_fcurves = sys.modules[f"{package_name}.handlers.object_animation"]._action_fcurves


class Harness(RigidBodyHandlersMixin):
    """Expose rigid-body handlers without starting the socket server."""


def add_cube(name, location):
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=location)
    obj = bpy.context.object
    obj.name = name
    return obj


def _channel_at(obj, data_path, frame):
    _action, curves = _action_fcurves(obj)
    by_index = {curve.array_index: curve for curve in curves if curve.data_path == data_path}
    return tuple(by_index[index].evaluate(frame) for index in range(len(by_index)))


def _arc(first, second):
    """Measure the angle between two orientations, whichever hemisphere either is spelled in."""
    return 2.0 * math.acos(min(1.0, abs(first.normalized().dot(second.normalized()))))


def _test_ragdoll_bake_keys_a_turn_the_short_way(handler, scene):
    """
    Bake a bone onto a proxy turning 40 degrees a frame through +-180 and read the curve between keys.

    The `pose_bone.matrix` setter spells each frame's orientation with w >= 0, so the frames
    either side of the half turn (160 and 200 degrees) came back on opposite hemispheres and the
    curve between two correct poses spun the long way. Key reduction keeps only some frames, so
    the kept keys must share one branch however far apart they are.
    """
    frames = range(1, 11)
    rig = bpy.data.objects.new("Turning Rig", bpy.data.armatures.new("Turning Armature"))
    scene.collection.objects.link(rig)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode="EDIT")
    bone = rig.data.edit_bones.new("turn")
    bone.head = (0.0, 0.0, 0.0)
    bone.tail = (0.0, 1.0, 0.0)
    bpy.ops.object.mode_set(mode="OBJECT")
    proxy = add_cube("Turning Proxy", (50.0, 0.0, 0.0))
    for frame in frames:
        proxy.rotation_euler = (0.0, 0.0, math.radians(40.0 * (frame - 1)))
        proxy.keyframe_insert(data_path="rotation_euler", index=2, frame=frame)
    handler.add_rigid_bodies(scene.name, [proxy.name], "ACTIVE")
    proxy.rigid_body.kinematic = True

    path = 'pose.bones["turn"].rotation_quaternion'
    for reduce_keys in (False, True):
        bake = handler.bake_ragdoll_to_armature(
            scene.name,
            rig.name,
            [{"bone_name": "turn", "proxy_object_name": proxy.name}],
            frames[0],
            frames[-1],
            action_name=f"Turning Bake {reduce_keys}",
            reduce_keys=reduce_keys,
        )
        keyed_frames = bake["keyed_frames_by_bone"]["turn"]
        assert reduce_keys or keyed_frames == list(frames), keyed_frames
        keyed = [mathutils.Quaternion(_channel_at(rig, path, frame)) for frame in keyed_frames]
        for frame, value in zip(keyed_frames, keyed, strict=True):
            asked = mathutils.Quaternion((0.0, 0.0, 1.0), math.radians(40.0 * (frame - 1)))
            assert _arc(value, asked) < 1e-4, f"reduce={reduce_keys}: bone key at {frame} is not the proxy pose"
        for (first_frame, first), (second_frame, second) in itertools.pairwise(zip(keyed_frames, keyed, strict=True)):
            assert first.dot(second) >= 0.0, (
                f"reduce={reduce_keys}: bone keys at {first_frame} and {second_frame} sit on opposite hemispheres"
            )
            midway = mathutils.Quaternion(_channel_at(rig, path, (first_frame + second_frame) / 2.0))
            detour = _arc(first, midway) + _arc(midway, second) - _arc(first, second)
            assert detour < 1e-3, (
                f"reduce={reduce_keys}: the curve leaves the arc between {first_frame} and {second_frame} "
                f"by {math.degrees(detour)} degrees"
            )


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
_test_ragdoll_bake_keys_a_turn_the_short_way(handler, scene)

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
