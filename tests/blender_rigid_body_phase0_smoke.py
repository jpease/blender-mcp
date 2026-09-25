"""
Blender 5.1+ background smoke coverage for Phase 0 rigid-body handlers.

Run with::

    blender --background --factory-startup --python tests/blender_rigid_body_phase0_smoke.py
"""

# Blender's runtime types are intentionally dynamic in this executable harness.

import json
import sys

from pathlib import Path

import bpy

sys.path.append(str(Path(__file__).resolve().parent))
from smoke_addon import load_addon

package_name = "blender_mcp_rigid_body_smoke"
load_addon(package_name)

RigidBodyHandlersMixin = sys.modules[f"{package_name}.handlers.rigid_body"].RigidBodyHandlersMixin


class Harness(RigidBodyHandlersMixin):
    """Expose rigid-body handlers without starting the socket server."""


handler = Harness()
scene = bpy.context.scene
scene.name = "Rigid Body Smoke"
first = bpy.data.objects.get("Cube")
first.name = "Active Crate"
bpy.ops.mesh.primitive_cube_add(size=4.0, location=(0.0, 0.0, -3.0))
floor = bpy.context.object
floor.name = "Passive Floor"
bpy.ops.mesh.primitive_cube_add(size=1.5, location=(4.0, 0.0, 0.0))
visual = bpy.context.object
visual.name = "Render Crate"

world = handler.configure_rigid_body_world(
    scene.name,
    body_collection_name="Physics Bodies",
    constraint_collection_name="Physics Constraints",
    world={"substeps_per_frame": 12, "solver_iterations": 20},
    gravity=(0.0, 0.0, -9.81),
    cache={"frame_start": 1, "frame_end": 120, "frame_step": 1},
)
assert world["world"]["body_collection"] == "Physics Bodies"

active = handler.add_rigid_bodies(
    scene.name,
    [first.name],
    "ACTIVE",
    settings={"collision_shape": "CONVEX_HULL", "mass": 2.0},
)
passive = handler.add_rigid_bodies(
    scene.name,
    [floor.name],
    "PASSIVE",
    settings={"collision_shape": "BOX"},
)
assert active["created"] == [first.name]
assert passive["created"] == [floor.name]

configured = handler.configure_rigid_bodies(
    scene.name,
    [{"object_name": first.name, "settings": {"friction": 0.7, "linear_damping": 0.1}}],
)
assert abs(configured["changes"][0]["changes"]["friction"]["new"] - 0.7) < 1e-6

mass = handler.set_rigid_body_mass(scene.name, [{"object_name": first.name, "density": 250.0}])
assert mass["assignments"][0]["evaluated_world_volume"] > 0

layers = handler.set_rigid_body_collision_layers(
    scene.name,
    [
        {"object_name": first.name, "profile": "HERO"},
        {"object_name": floor.name, "layers": [1, 2]},
    ],
)
assert layers["layers"][0]["new"] == [2]

constraint = handler.create_rigid_body_constraint(
    scene.name,
    "Crate Pin",
    first.name,
    floor.name,
    {"location": (0.0, 0.0, 0.0), "axis": (0.0, 0.0, 1.0)},
    {
        "type": "HINGE",
        "disable_collisions": True,
        "angular_z": {"use_limit": True, "lower": -0.5, "upper": 0.5},
    },
)
assert constraint["axis_convention"] == "LOCAL_Z"
constraint_update = handler.configure_rigid_body_constraint(
    scene.name,
    "Crate Pin",
    {
        "type": "HINGE",
        "use_breaking": True,
        "breaking_threshold": 50.0,
        "angular_z": {"use_limit": True, "lower": -0.25, "upper": 0.25},
    },
)
assert constraint_update["constraint"]["use_breaking"] is True

proxy = handler.create_rigid_body_collision_proxy(
    scene.name,
    visual.name,
    "Crate Proxy",
    "Physics Proxies",
    "BOX",
    "PASSIVE",
)
assert proxy["proxy"] == "Crate Proxy"
convex_proxy = handler.create_rigid_body_collision_proxy(
    scene.name,
    visual.name,
    "Floor Hull",
    "Physics Proxies",
    "CONVEX_HULL",
    "PASSIVE",
)
assert convex_proxy["collision_shape"] == "CONVEX_HULL"

scene_info = handler.get_rigid_body_scene_info(scene.name)
object_info = handler.get_rigid_body_object_info([first.name, floor.name, proxy["proxy"]])
constraint_info = handler.get_rigid_body_constraint_info(scene.name)
validation = handler.validate_rigid_body_setup(scene.name)
assert scene_info["member_counts"]["total"] == 4
assert len(object_info["objects"]) == 3
assert constraint_info["page"]["total"] == 1
assert validation["bodies_checked"] == 4
json.dumps(
    [
        world,
        active,
        passive,
        configured,
        mass,
        layers,
        constraint,
        constraint_update,
        proxy,
        convex_proxy,
        scene_info,
        object_info,
        constraint_info,
        validation,
    ]
)

print("BLENDER_RIGID_BODY_PHASE0_SMOKE_OK")
