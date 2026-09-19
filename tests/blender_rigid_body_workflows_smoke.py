"""
Blender background smoke coverage for advanced rigid-body workflows.

Run with::

    blender --background --factory-startup --python tests/blender_rigid_body_workflows_smoke.py
"""

# Blender runtime types are dynamic in this executable harness.

import importlib.util
import json
import sys

from pathlib import Path

import bpy

addon_path = Path(__file__).resolve().parents[1] / "src" / "blender_mcp" / "bundled" / "addon" / "__init__.py"
package_name = "blender_mcp_rigid_body_workflow_smoke"
spec = importlib.util.spec_from_file_location(
    package_name,
    addon_path,
    submodule_search_locations=[str(addon_path.parent)],
)
assert spec is not None and spec.loader is not None
addon = importlib.util.module_from_spec(spec)
sys.modules[package_name] = addon
spec.loader.exec_module(addon)

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

constraint_name = network["edges"][0]["constraint"]
removed_constraint = handler.remove_rigid_body_components(
    scene.name,
    "CONSTRAINT_SETTINGS",
    object_names=[constraint_name],
)
assert removed_constraint["removed"] == [constraint_name]
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
assert removed_helper["removed"] == ["Simulation Wind"]
removed_world = handler.remove_rigid_body_components(
    scene.name,
    "WORLD",
    confirm_destructive=True,
)
assert removed_world["removed"] == [scene.name]

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
