"""Run with Blender 5.1+: blender --background --factory-startup --python this_file.py."""

# This executable Blender harness intentionally uses module-level setup and
# Blender's dynamically typed object return values.

from __future__ import annotations

import importlib.util
import shutil
import sys
import tempfile

from pathlib import Path

import bpy

REPO_ROOT = Path(__file__).resolve().parent.parent
ADDON_ROOT = REPO_ROOT / "src" / "blender_mcp" / "bundled" / "addon"
PACKAGE_NAME = "blender_mcp_liquid_smoke"
spec = importlib.util.spec_from_file_location(
    PACKAGE_NAME,
    ADDON_ROOT / "__init__.py",
    submodule_search_locations=[str(ADDON_ROOT)],
)
assert spec is not None and spec.loader is not None
addon = importlib.util.module_from_spec(spec)
sys.modules[PACKAGE_NAME] = addon
spec.loader.exec_module(addon)
LiquidHandlersMixin = sys.modules[f"{PACKAGE_NAME}.handlers.liquid"].LiquidHandlersMixin


class Harness(LiquidHandlersMixin):
    """Expose the mixin without starting a socket server."""


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
cache_path = str(Path(tempfile.gettempdir()) / "blendermcp-liquid-phase0-smoke")
shutil.rmtree(cache_path, ignore_errors=True)  # prove create_liquid_domain creates it, not just tolerates it existing
domain = handler.create_liquid_domain(
    scene_name="Scene",
    cache_directory=cache_path,
    new_object_name="Smoke Domain",
    dimensions=(4.0, 4.0, 4.0),
    resolution_max=32,
)
assert Path(cache_path).is_dir()
source = cube("Smoke Flow", 1.0, (0.0, 0.0, 1.0))
flow = handler.add_liquid_flow(
    object_name=source.name,
    domain_object_name=domain["object"],
    behavior="INFLOW",
    settings={"use_inflow": True, "subframes": 1},
)
collider = cube("Smoke Collider", 1.0, (0.0, 0.0, -1.0))
effector = handler.add_liquid_effector(
    object_name=collider.name,
    domain_object_name=domain["object"],
    settings={"surface_distance": 0.001},
)
fit = handler.fit_liquid_domain(
    scene_name="Scene",
    source_object_names=[source.name],
    collider_object_names=[collider.name],
    domain_object_name=domain["object"],
    modifier_name=domain["modifier"],
    padding=(0.5, 0.5, 0.5),
)
handler.configure_liquid_flow(
    object_name=source.name,
    modifier_name=flow["modifier"],
    domain_object_name=domain["object"],
    patch={"surface_distance": 1.5},
)
handler.configure_liquid_effector(
    object_name=collider.name,
    modifier_name=effector["modifier"],
    domain_object_name=domain["object"],
    patch={"subframes": 1},
)
nonuniform_source = cube("Smoke Flow NonUniform", 1.0, (1.5, 0.0, 1.0))
nonuniform_source.scale = (2.0, 1.0, 1.0)  # local scale itself is nonuniform
handler.add_liquid_flow(
    object_name=nonuniform_source.name,
    domain_object_name=domain["object"],
    behavior="INFLOW",
    settings={"use_inflow": True},
)
unapplied_collider = cube("Smoke Collider Unapplied", 1.0, (-1.5, 0.0, -1.0))
unapplied_collider.delta_scale = (2.0, 2.0, 2.0)  # world scale diverges from the unchanged local .scale
handler.add_liquid_effector(
    object_name=unapplied_collider.name,
    domain_object_name=domain["object"],
)

scope = handler.configure_liquid_scope_and_boundaries(
    domain_object_name=domain["object"],
    modifier_name=domain["modifier"],
    boundaries={"top": False},
)
solver = handler.configure_liquid_solver(
    domain_object_name=domain["object"],
    modifier_name=domain["modifier"],
    patch={"resolution_max": 40, "timesteps_max": 5},
)
estimate = handler.estimate_liquid_resources(domain["object"], domain["modifier"])
inspection = handler.get_liquid_simulation_info(scene_name="Scene")
object_inspection = handler.get_fluid_object_info(source.name)
validation = handler.validate_liquid_setup("Scene")

assert flow["created"] is True
assert effector["created"] is True
assert fit["created"] is False
assert scope["boundary_changes"]["use_collision_border_top"]["new"] is False
assert solver["changes"]["resolution_max"]["new"] == 40
assert estimate["estimated_grid"]["resolution_max"] == 40
assert inspection["domain_page"]["total"] == 1
assert object_inspection["flows"][0]["settings"]["flow_type"] == "LIQUID"
assert domain["object"] in validation["domains_checked"]
codes_by_object: dict[str, set[str]] = {}
for finding in validation["findings"]:
    codes_by_object.setdefault(finding["object"], set()).add(finding["code"])
assert "NONUNIFORM_MEMBER_SCALE" in codes_by_object.get(nonuniform_source.name, set())
assert "UNAPPLIED_MEMBER_TRANSFORM" in codes_by_object.get(unapplied_collider.name, set())

gas_cache_path = str(Path(tempfile.gettempdir()) / "blendermcp-gas-phase0-smoke")
shutil.rmtree(gas_cache_path, ignore_errors=True)
gas_domain = handler.create_fluid_domain(
    "GAS",
    scene_name="Scene",
    cache_directory=gas_cache_path,
    new_object_name="Smoke Gas Domain",
    dimensions=(3.0, 3.0, 3.0),
    resolution_max=32,
)
gas_source = cube("Smoke Gas Flow", 0.5, (0.0, 0.0, 0.0))
gas_flow = handler.add_fluid_flow(
    "GAS",
    gas_source.name,
    gas_domain["object"],
    gas_flow_type="BOTH",
    behavior="INFLOW",
    settings={"fuel_amount": 0.75, "surface_distance": 1.5},
)
gas_solver = handler.configure_fluid_solver(
    "GAS",
    gas_domain["object"],
    gas_domain["modifier"],
    {"use_noise": True, "noise_scale": 2},
)
gas_inspection = handler.inspect_fluid_simulation(
    "GAS", scene_name="Scene", domain_object_name=gas_domain["object"], limit=1
)
gas_cache = handler.manage_fluid_cache(
    "GAS", domain_object_name=gas_domain["object"], modifier_name=gas_domain["modifier"], action="STATUS"
)
assert gas_domain["domain_type"] == "GAS"
assert gas_flow["flow_type"] == "BOTH"
assert gas_solver["changes"]["use_noise"]["new"] is True
assert gas_inspection["returned_count"] == 1
assert gas_cache["cache"]["configuration"]["cache_type"] == "REPLAY"
print("BLENDER_LIQUID_PHASE0_SMOKE_OK")
