"""
Blender background smoke coverage for advanced rigid-body workflows.

Run with::

    blender --background --factory-startup --python tests/blender_rigid_body_workflows_smoke.py
"""

# Blender runtime types are dynamic in this executable harness.

import itertools
import json
import math
import sys

from pathlib import Path

import bpy
import mathutils

sys.path.append(str(Path(__file__).resolve().parent))
from smoke_addon import load_addon

package_name = "blender_mcp_rigid_body_workflow_smoke"
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


def _test_bake_keys_a_turn_the_short_way(handler, scene):
    """
    Bake a body turning 40 degrees a frame through +-180 and read the curve between the keys.

    The `matrix_world` setter spells each frame's orientation with w >= 0, so the frames either
    side of the half turn (160 and 200 degrees) came back on opposite hemispheres and the baked
    curve between two correct poses spun 320 degrees the wrong way.
    """
    frames = range(1, 11)
    pivot = bpy.data.objects.new("Turn Pivot", None)
    pivot.location = (20.0, 0.0, 0.0)
    scene.collection.objects.link(pivot)
    for frame in frames:
        pivot.rotation_euler = (0.0, 0.0, math.radians(40.0 * (frame - 1)))
        pivot.keyframe_insert(data_path="rotation_euler", index=2, frame=frame)
    body = add_cube("Turning Body", (0.0, 0.0, 0.0))
    body.parent = pivot
    handler.add_rigid_bodies(scene.name, [body.name], "ACTIVE")
    body.rigid_body.kinematic = True

    baked = handler.bake_rigid_bodies_to_keyframes(scene.name, [body.name], frames[0], frames[-1])
    output = bpy.data.objects[baked["output_objects"][0]]
    keyed = [mathutils.Quaternion(_channel_at(output, "rotation_quaternion", frame)) for frame in frames]
    for frame, value in zip(frames, keyed, strict=True):
        asked = mathutils.Quaternion((0.0, 0.0, 1.0), math.radians(40.0 * (frame - 1)))
        assert _arc(value, asked) < 1e-4, f"baked key at {frame} is not the simulated pose"
    for before, after in itertools.pairwise(keyed):
        assert before.dot(after) >= 0.0, f"adjacent baked keys {before} and {after} sit on opposite hemispheres"
    midway = mathutils.Quaternion(_channel_at(output, "rotation_quaternion", 5.5))
    detour = _arc(keyed[4], midway) + _arc(midway, keyed[5]) - _arc(keyed[4], keyed[5])
    assert detour < 1e-3, f"the baked curve leaves the 40-degree arc between frames 5 and 6 by {math.degrees(detour)}"


def _turn_about_z(degrees):
    """Build a turn about Z from its components: the axis-angle constructor folds angles past 180 degrees."""
    half = math.radians(degrees) / 2.0
    return mathutils.Quaternion((math.cos(half), 0.0, 0.0, math.sin(half)))


def _test_release_preroll_turns_the_way_of_the_spin(handler, scene):
    """
    Release a body at 185 degrees of yaw spinning about world Z and read its two keys.

    The `matrix_world` setter spells each key with w >= 0: a 10-degree pre-roll from 175 to 185
    degrees came back as q and -q, so the kinematic pre-roll turned 350 degrees the other way
    and released the body spinning backwards. The release key must be the prior key turned by
    the spin - the short way under a half turn, the long way past one, about world Z for a body
    whose parent tilts its channel space, and by the remainder in the spin's direction past a
    whole turn, which two keys cannot hold.
    """
    fps = scene.render.fps / scene.render.fps_base
    yaw = mathutils.Quaternion((0.0, 0.0, 1.0), math.radians(185.0))
    tilted = bpy.data.objects.new("Release Tilt", None)
    # Tilted past 90 degrees, so the parent's Z leans against world Z: a spin turned about the
    # channel's own axis instead of the world's would pick the other spelling.
    tilted.rotation_euler = (math.radians(150.0), 0.0, math.radians(30.0))
    tilted.location = (40.0, 0.0, 0.0)
    scene.collection.objects.link(tilted)
    bpy.context.view_layer.update()
    cases = ((10.0, None), (200.0, None), (370.0, None), (200.0, tilted))
    for index, (degrees, parent) in enumerate(cases):
        body = add_cube(f"Spinning Release {index}", (30.0, 3.0 * index, 0.0))
        body.parent = parent
        body.matrix_world = mathutils.Matrix.LocRotScale(tuple(body.matrix_world.translation), yaw, None)
        handler.add_rigid_bodies(scene.name, [body.name], "ACTIVE")
        body.rigid_body.kinematic = True
        handler.animate_rigid_body_release(
            scene.name, body.name, "RELEASE", 3, angular_velocity=(0.0, 0.0, math.radians(degrees) * fps)
        )
        space = parent.matrix_world.to_quaternion() if parent else mathutils.Quaternion()
        prior, release = (space @ mathutils.Quaternion(_channel_at(body, "rotation_quaternion", f)) for f in (2, 3))
        assert _arc(release, yaw) < 1e-4, f"case {index}: the release key is not the transition pose"
        kept = degrees % 360.0
        turned = _turn_about_z(kept) @ prior
        assert all(math.isclose(a, b, abs_tol=1e-5) for a, b in zip(release, turned, strict=True)), (
            f"case {index}: release key {release} is not prior key {prior} turned {kept} degrees about Z"
        )
        midway = space @ mathutils.Quaternion(_channel_at(body, "rotation_quaternion", 2.5))
        halfway = _turn_about_z(kept / 2.0) @ prior
        assert _arc(midway, halfway) < 1e-3, f"case {index}: the pre-roll midway is not {kept / 2} degrees on"


handler = Harness()
scene = bpy.context.scene
scene.name = "Rigid Body Workflows"
scene.frame_start = 1
scene.frame_end = 20

root = bpy.context.object
root.name = "Compound Root"
child_a = add_cube("Compound Child A", (-0.6, 0.0, 0.0))
child_b = add_cube("Compound Child B", (0.6, 0.0, 0.0))
body_a = add_cube("Chain A", (0.0, 3.0, 2.0))
body_b = add_cube("Chain B", (0.0, 3.0, 0.5))
floor = add_cube("Animated Floor", (0.0, 0.0, -2.0))
shard_a = add_cube("Shard A", (4.0, 0.0, 0.0))
shard_b = add_cube("Shard B", (5.1, 0.0, 0.0))

handler.configure_rigid_body_world(
    scene.name,
    body_collection_name="Physics Bodies",
    constraint_collection_name="Physics Constraints",
    cache={"frame_start": 1, "frame_end": 20, "frame_step": 1},
)
handler.add_rigid_bodies(scene.name, [body_a.name, body_b.name], "ACTIVE")

compound = handler.create_compound_rigid_body(
    scene.name,
    root.name,
    [child_a.name, child_b.name],
    total_mass=5.0,
)
assert compound["root_rigid_body"]["collision_shape"] == "COMPOUND"
assert child_a.parent == root and child_b.parent == root

network = handler.create_rigid_body_constraint_network(
    scene.name,
    "Chain Network",
    [body_a.name, body_b.name],
    {"type": "POINT", "disable_collisions": True},
    edges=[{"object1_name": body_a.name, "object2_name": body_b.name}],
)
assert len(network["edges"]) == 1

chain = handler.create_rigid_body_chain(
    scene.name,
    "Mechanical Link",
    [child_a.name, child_b.name],
    {"type": "HINGE", "angular_z": {"use_limit": True, "lower": -0.5, "upper": 0.5}},
)
assert len(chain["edges"]) == 1

fracture = handler.prepare_fracture_rigid_bodies(
    scene.name,
    [shard_a.name, shard_b.name],
    density=100.0,
)
assert fracture["total_mass"] > 0.0

floor.location.x = -1.0
floor.keyframe_insert(data_path="location", frame=1)
floor.location.x = 1.0
floor.keyframe_insert(data_path="location", frame=10)
passive = handler.setup_animated_passive_collider(
    scene.name,
    floor.name,
    "BOX",
    sample_frames=[1, 10],
)
assert passive["rigid_body"]["type"] == "PASSIVE"
assert passive["rigid_body"]["kinematic"] is True

force_fields = handler.configure_rigid_body_force_fields(
    scene.name,
    "Rigid Body Forces",
    [
        {
            "object_name": "Simulation Wind",
            "field_type": "WIND",
            "create_if_missing": True,
            "location": (0.0, 0.0, 2.0),
            "rotation_euler": (0.0, 0.0, 0.0),
            "strength": 2.0,
        }
    ],
    create_collection=True,
    weights={"wind": 0.5},
)
assert force_fields["fields"][0]["settings"]["type"] == "WIND"

release = handler.animate_rigid_body_release(
    scene.name,
    body_a.name,
    "RELEASE",
    3,
    linear_velocity=(1.0, 0.0, 0.0),
)
assert release["keyed_frames"] == [2, 3]

sample = handler.sample_rigid_body_simulation(
    scene.name,
    [body_a.name, body_b.name],
    {"frames": [1, 2]},
)
assert sample["evaluated_frames"] == [1, 2]
assert sample["timeline_restored"]["frame"] == 1

cache = handler.manage_rigid_body_cache(scene.name, action="INSPECT")
assert cache["operator_scope"] if "operator_scope" in cache else cache["action"] == "INSPECT"
configured_cache = handler.manage_rigid_body_cache(
    scene.name,
    action="CONFIGURE",
    settings={"frame_start": 1, "frame_end": 10, "frame_step": 1},
)
assert configured_cache["point_cache_after"]["frame_end"] == 10
cache_bake = handler.manage_rigid_body_cache(
    scene.name,
    action="BAKE",
    confirm_bake=True,
    max_frame_steps=10,
)
assert cache_bake["point_cache_after"]["is_baked"] is True
simulated = [obj for obj in scene.objects if obj.rigid_body is not None]
assert cache_bake["changed_objects"] == []
assert cache_bake["simulated_objects"]["total"] == len(simulated)
assert cache_bake["simulated_objects"]["names"] == [obj.name for obj in simulated][:10]
cache_free = handler.manage_rigid_body_cache(
    scene.name,
    action="FREE",
    confirm_free=True,
)
assert cache_free["point_cache_after"]["is_baked"] is False
cache_calculation = handler.manage_rigid_body_cache(
    scene.name,
    action="CALCULATE_TO_FRAME",
    calculate_frame=10,
    max_frame_steps=10,
)
assert cache_calculation["frame_steps"] == 10
cache_from_memory = handler.manage_rigid_body_cache(
    scene.name,
    action="BAKE_FROM_CACHE",
    confirm_bake=True,
    max_frame_steps=10,
)
assert cache_from_memory["point_cache_after"]["is_baked"] is True
handler.manage_rigid_body_cache(scene.name, action="FREE", confirm_free=True)

baked = handler.bake_rigid_bodies_to_keyframes(
    scene.name,
    [body_b.name],
    1,
    2,
    output_collection_name="Rigid Body Bakes",
)
assert len(baked["created_duplicates"]) == 1
assert baked["source_rigid_bodies_retained"] is True
_test_bake_keys_a_turn_the_short_way(handler, scene)
_test_release_preroll_turns_the_way_of_the_spin(handler, scene)

constraint_name = network["edges"][0]["constraint"]
removed_constraint = handler.remove_rigid_body_components(
    scene.name,
    "CONSTRAINT_SETTINGS",
    object_names=[constraint_name],
)
assert removed_constraint["removed"]["names"] == [constraint_name]
removed_body = handler.remove_rigid_body_components(
    scene.name,
    "BODY_SETTINGS",
    object_names=[shard_a.name],
)
assert shard_a.name in scene.objects and removed_body["mesh_objects_retained"] is True
removed_helper = handler.remove_rigid_body_components(
    scene.name,
    "TAGGED_HELPERS",
    object_names=["Simulation Wind"],
    confirm_destructive=True,
)
assert removed_helper["removed"]["names"] == ["Simulation Wind"]
assert removed_helper["removed"]["total"] == 1 and removed_helper["changed_objects"] == []
removed_world = handler.remove_rigid_body_components(
    scene.name,
    "WORLD",
    confirm_destructive=True,
)
assert removed_world["removed"]["names"] == [scene.name]

json.dumps(
    [
        compound,
        network,
        chain,
        fracture,
        passive,
        force_fields,
        release,
        sample,
        cache,
        configured_cache,
        cache_bake,
        cache_free,
        cache_calculation,
        cache_from_memory,
        baked,
        removed_constraint,
        removed_body,
        removed_helper,
        removed_world,
    ]
)
print("BLENDER_RIGID_BODY_WORKFLOWS_SMOKE_OK")
