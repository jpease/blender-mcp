"""Run with Blender 5.1+: blender --background --factory-startup --python this_file.py."""

# This executable Blender harness intentionally uses module-level setup and
# Blender's dynamically typed object return values.
# ruff: file-ignore[float-equality-comparison]

from __future__ import annotations

import sys
import tempfile

from pathlib import Path

import bpy

sys.path.append(str(Path(__file__).resolve().parent))
from smoke_addon import load_addon

PACKAGE_NAME = "blender_mcp_liquid_workflows_smoke"
load_addon(PACKAGE_NAME)
LiquidHandlersMixin = sys.modules[f"{PACKAGE_NAME}.handlers.liquid"].LiquidHandlersMixin
TextureHandlers = sys.modules[f"{PACKAGE_NAME}.handlers.texture"].TextureHandlers


class Harness(LiquidHandlersMixin, TextureHandlers):
    """Expose every liquid workflow handler without starting a socket server."""


def cube(name: str, size: float, location: tuple[float, float, float]):
    half = size * 0.5
    vertices = [
        (-half, -half, -half),
        (half, -half, -half),
        (half, half, -half),
        (-half, half, -half),
        (-half, -half, half),
        (half, -half, half),
        (half, half, half),
        (-half, half, half),
    ]
    faces = [(0, 1, 2, 3), (4, 7, 6, 5), (0, 4, 5, 1), (1, 5, 6, 2), (2, 6, 7, 3), (4, 0, 3, 7)]
    mesh = bpy.data.meshes.new(f"{name} Mesh")
    mesh.from_pydata(vertices, [], faces)
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    obj.location = location
    return obj


handler = Harness()

# --- mesh/animation/guides/force-fields/materials/simulation/lifecycle workflow ---

cache_path = tempfile.mkdtemp(prefix="blendermcp-liquid-workflows-")
domain = handler.create_liquid_domain(
    scene_name="Scene",
    cache_directory=cache_path,
    new_object_name="Workflow Domain",
    dimensions=(3.0, 3.0, 3.0),
    resolution_max=24,
)
source = cube("Workflow Flow", 0.5, (0.0, 0.0, 0.5))
flow = handler.add_liquid_flow(
    object_name=source.name,
    domain_object_name=domain["object"],
    behavior="INFLOW",
    settings={"use_inflow": True, "subframes": 1},
)
mesh = handler.configure_liquid_mesh(
    domain["object"],
    domain["modifier"],
    {"use_mesh": True, "mesh_scale": 2, "mesh_particle_radius": 2.0, "use_speed_vectors": True},
)
particles = handler.configure_liquid_secondary_particles(
    domain["object"],
    domain["modifier"],
    {"use_spray_particles": True, "sndparticle_sampling_wavecrest": 10},
)
diffusion = handler.configure_liquid_diffusion(domain["object"], domain["modifier"], {"preset": "WATER"})
animation = handler.animate_liquid_flow(
    source.name,
    flow["modifier"],
    domain["object"],
    [
        {"frame": 1.0, "use_inflow": True, "interpolation": "CONSTANT"},
        {"frame": 10.0, "use_inflow": False, "interpolation": "CONSTANT"},
    ],
    subframes=2,
)
guide_object = cube("Workflow Guide", 0.75, (0.0, 0.0, 0.0))
guide = handler.create_liquid_guide(
    domain["object"],
    domain["modifier"],
    guide_object.name,
    source="EFFECTOR",
)
forces = handler.configure_liquid_force_fields(
    "Scene",
    domain["object"],
    domain["modifier"],
    [
        {
            "object_name": "Workflow Wind",
            "field_type": "WIND",
            "create_if_missing": True,
            "location": (0.0, 0.0, 1.0),
            "rotation_euler": (0.0, 0.0, 0.0),
            "strength": 2.0,
            "shape": "POINT",
            "falloff_type": "SPHERE",
            "noise": 0.0,
            "seed": 1,
            "use_min_distance": False,
            "distance_min": 0.0,
            "use_max_distance": False,
            "distance_max": 0.0,
        }
    ],
    "Workflow Forces",
    create_collection=True,
    weights={"wind": 1.0},
)
material = handler.create_liquid_material(
    domain["object"],
    domain["modifier"],
    "Workflow Water",
    {"preset": "WATER"},
)
cache = handler.manage_liquid_cache(domain["object"], domain["modifier"], action="STATUS")
sample = handler.sample_liquid_simulation(domain["object"], domain["modifier"], [1], timeout_seconds=15.0)
throwaway = cube("Workflow Throwaway", 0.25, (0.0, 0.0, 0.0))
throwaway_effector = handler.add_liquid_effector(
    throwaway.name,
    domain["object"],
    modifier_name="Throwaway Effector",
)
removed = handler.remove_fluid_components(
    [{"object_name": throwaway.name, "modifier_name": throwaway_effector["modifier"]}]
)

assert mesh["estimated_output"]["estimated_longest_axis_resolution"] == 48
assert particles["enabled_particle_types"] == ["SPRAY"]
assert diffusion["represented_kinematic_viscosity_m2_s"] == 1e-6
assert len(animation["keyframes"]) == 2
assert guide["settings"]["guide_source"] == "EFFECTOR"
assert forces["force_fields"][0]["field"]["type"] == "WIND"
assert material["created"] is True
assert material["assignment"]["slot_index"] == 0
assert material["material"] in [slot.name for slot in bpy.data.objects[domain["object"]].data.materials]
assert bpy.data.materials[material["material"]].node_tree.nodes["PBR Volume Absorption"]
assert cache["cache"]["configuration"]["cache_type"] == "REPLAY"
assert sample["timeline_restored"]["frame"] == 1
assert removed["removed"][0]["fluid_type"] == "EFFECTOR"

# --- proxy/variant/delivery workflow ---

delivery_domain_cache = tempfile.mkdtemp(prefix="blendermcp-liquid-workflows-delivery-source-")
variant_cache = tempfile.mkdtemp(prefix="blendermcp-liquid-workflows-delivery-variant-")
delivery_domain = handler.create_liquid_domain(
    scene_name="Scene",
    cache_directory=delivery_domain_cache,
    new_object_name="Delivery Domain",
    dimensions=(4.0, 4.0, 4.0),
    resolution_max=24,
)
delivery_source = cube("Delivery Source", 1.0, (0.5, -0.5, 0.25))
proxy = handler.create_liquid_proxy_rig(
    "Scene",
    delivery_source.name,
    "Delivery Proxy",
    delivery_domain["object"],
    delivery_domain["modifier"],
    "EFFECTOR",
    geometry="BOX",
    validation_frames=[1, 2],
)
capsule = handler.create_liquid_proxy_rig(
    "Scene",
    delivery_source.name,
    "Delivery Capsule Proxy",
    delivery_domain["object"],
    delivery_domain["modifier"],
    "EFFECTOR",
    geometry="CAPSULE",
    driver="PARENT",
)
convex = handler.create_liquid_proxy_rig(
    "Scene",
    delivery_source.name,
    "Delivery Hull Proxy",
    delivery_domain["object"],
    delivery_domain["modifier"],
    "EFFECTOR",
    geometry="CONVEX_HULL",
)
decimated = handler.create_liquid_proxy_rig(
    "Scene",
    delivery_source.name,
    "Delivery Decimated Proxy",
    delivery_domain["object"],
    delivery_domain["modifier"],
    "FLOW",
    geometry="DECIMATED",
)
supplied_object = cube("Delivery Supplied Proxy", 0.75, (0.0, 0.0, 0.0))
supplied = handler.create_liquid_proxy_rig(
    "Scene",
    delivery_source.name,
    supplied_object.name,
    delivery_domain["object"],
    delivery_domain["modifier"],
    "EFFECTOR",
    geometry="SUPPLIED",
)
variant = handler.duplicate_liquid_setup_variant(
    delivery_domain["object"],
    delivery_domain["modifier"],
    "Delivery Domain Preview",
    "Delivery Variant",
    "Preview",
    variant_cache,
)
performance = handler.analyze_liquid_performance(delivery_domain["object"], delivery_domain["modifier"])

assert proxy["proxy"] == "Delivery Proxy"
assert max(record["maximum_matrix_error"] for record in proxy["transform_validation"]) < 1e-5
assert capsule["driver"] == "PARENT"
assert convex["geometry"] == "CONVEX_HULL"
assert decimated["role"] == "FLOW"
assert supplied["created_proxy"] is False
assert variant["cache_directory_resolved"] != delivery_domain["cache_directory_resolved"]
assert variant["disabled_domain"] == "Delivery Domain Preview"
assert performance["claims"]["exact_peak_memory"] is None
assert performance["measured_evaluation"]["performed"] is False

print("BLENDER_LIQUID_WORKFLOWS_SMOKE_OK")
