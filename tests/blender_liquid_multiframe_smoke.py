"""
Run with Blender 5.1+: blender --background --factory-startup --python this_file.py.

Disclosure: this file runs under `--background`, which has no window manager, so
`START_BAKE`/`RESUME` always fall back to Blender's synchronous bake path
(`job_mode == "SYNCHRONOUS"`) here. The non-blocking `INVOKE_DEFAULT` job dispatch and a
true mid-bake `PAUSE` (which requires another thread issuing the pause while the bake is
still running) can only be exercised interactively against a real Blender window; this
smoke test instead verifies the RESUME/PAUSE *precondition* rejections (no paused bake to
resume, nothing currently baking to pause) against a domain that has already finished a
synchronous MODULAR bake.
"""

# This executable Blender harness intentionally uses module-level setup and
# Blender's dynamically typed object return values.

from __future__ import annotations

import importlib.util
import shutil
import sys
import tempfile

from pathlib import Path

import bmesh
import bpy

REPO_ROOT = Path(__file__).resolve().parent.parent
ADDON_ROOT = REPO_ROOT / "src" / "blender_mcp" / "bundled" / "addon"
PACKAGE_NAME = "blender_mcp_liquid_multiframe_smoke"
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
TextureHandlers = sys.modules[f"{PACKAGE_NAME}.handlers.texture"].TextureHandlers


class Harness(LiquidHandlersMixin, TextureHandlers):
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
    # This hand-built vertex/face winding produces inward-facing normals; recalc them
    # outward so wall-thickness ray casts (which start just outside the surface and fire
    # inward) actually hit the opposite interior wall instead of missing outward.
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    bm.to_mesh(mesh)
    bm.free()
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    obj.location = location
    return obj


handler = Harness()
cache_path = str(Path(tempfile.gettempdir()) / "blendermcp-liquid-multiframe-smoke")
shutil.rmtree(cache_path, ignore_errors=True)
domain = handler.create_liquid_domain(
    scene_name="Scene",
    cache_directory=cache_path,
    new_object_name="Multiframe Domain",
    dimensions=(4.0, 4.0, 4.0),
    resolution_max=16,
    cache_type="MODULAR",
    cache_frame_start=1,
    cache_frame_end=4,
    timesteps_min=1,
    timesteps_max=2,
)

pour = cube("Multiframe Pour", 0.6, (0.0, 0.0, 1.3))
pour_flow = handler.add_liquid_flow(
    object_name=pour.name,
    domain_object_name=domain["object"],
    behavior="INFLOW",
    settings={"use_inflow": True, "subframes": 1},
)

# Created with use_inflow=True on an OUTFLOW behavior: exercises the item-1 fix that
# "Use Flow" applies to Inflow AND Outflow, not Inflow only.
drain = cube("Multiframe Drain", 0.6, (1.3, 0.0, -1.3))
drain_flow = handler.add_liquid_flow(
    object_name=drain.name,
    domain_object_name=domain["object"],
    behavior="OUTFLOW",
    settings={"use_inflow": True, "subframes": 1},
)

# Drain starts inactive, then switches on mid-range (frame 3 of a 1-4 bake) so it only
# drains liquid for the back half of the bake.
handler.animate_liquid_flow(
    object_name=drain.name,
    modifier_name=drain_flow["modifier"],
    domain_object_name=domain["object"],
    keyframes=[
        {"frame": 1, "use_inflow": False},
        {"frame": 3, "use_inflow": True},
    ],
)

moving_collider = cube("Multiframe Moving Collider", 0.5, (-1.3, 0.0, -1.3))
moving_collider.location = (-1.3, -0.8, -1.3)
moving_collider.keyframe_insert(data_path="location", frame=1)
moving_collider.location = (-1.3, 0.8, -1.3)
moving_collider.keyframe_insert(data_path="location", frame=4)
moving_effector = handler.add_liquid_effector(
    object_name=moving_collider.name,
    domain_object_name=domain["object"],
)

# Deliberately thinner than 1.5 estimated cells (domain spans 4.0 over 16 cells, so
# cell_size ~= 0.25 and 1.5x that is ~0.375) to confirm THIN_EFFECTOR_WALL fires.
thin_collider = cube("Multiframe Thin Wall", 0.1, (1.3, 0.0, 1.3))
thin_effector = handler.add_liquid_effector(
    object_name=thin_collider.name,
    domain_object_name=domain["object"],
)

handler.configure_liquid_mesh(
    domain_object_name=domain["object"],
    modifier_name=domain["modifier"],
    patch={"use_mesh": True},
)
handler.manage_liquid_cache(
    domain_object_name=domain["object"],
    modifier_name=domain["modifier"],
    action="CONFIGURE",
    patch={"cache_resumable": True},
)

preflight = handler.validate_liquid_setup("Scene")
codes_by_object: dict[str, set[str]] = {}
for finding in preflight["findings"]:
    codes_by_object.setdefault(finding["object"], set()).add(finding["code"])
assert "THIN_EFFECTOR_WALL" in codes_by_object.get(thin_collider.name, set())

bake_data = handler.manage_liquid_cache(
    domain_object_name=domain["object"],
    modifier_name=domain["modifier"],
    action="START_BAKE",
    stage="DATA",
    confirm_bake=True,
)
assert bake_data["job_mode"] == "SYNCHRONOUS"
assert bake_data["cache_after"]["stages"]["has_cache_baked_data"] is True
assert any("--background" in warning for warning in bake_data["warnings"])

bake_mesh = handler.manage_liquid_cache(
    domain_object_name=domain["object"],
    modifier_name=domain["modifier"],
    action="START_BAKE",
    stage="MESH",
    confirm_bake=True,
)
assert bake_mesh["job_mode"] == "SYNCHRONOUS"
assert bake_mesh["cache_after"]["stages"]["has_cache_baked_mesh"] is True

status = handler.manage_liquid_cache(
    domain_object_name=domain["object"],
    modifier_name=domain["modifier"],
    action="STATUS",
)
assert status["cache"]["stages"]["has_cache_baked_data"] is True
assert status["cache"]["stages"]["has_cache_baked_mesh"] is True
assert status["directory_is_manifest_owned"] is True

sample = handler.sample_liquid_simulation(
    domain_object_name=domain["object"],
    modifier_name=domain["modifier"],
    frames=[1, 2, 3, 4],
)
assert sample["evaluated_frames"] == [1, 2, 3, 4]
assert sample["timed_out"] is False

# Rendered-frame regression check for the item-18 shared-PBR WATER preset (transmission +
# Volume Absorption): renders through the same preview path exercised in
# blender_texture_smoke.py rather than the baked domain mesh itself, since a full GI render of
# the fluid mesh is unbounded scope for a smoke check. This still catches a broken node graph
# that would otherwise only fail silently (e.g. a disconnected BSDF/output link). Uses EEVEE
# rather than Cycles: this Blender build has no Cycles render engine registered even after
# `preferences.addon_enable(module="cycles")`, so BLENDER_EEVEE_NEXT (falling back to
# BLENDER_EEVEE) is the only engine available under --background here.
material = handler.create_liquid_material(
    domain_object_name=domain["object"],
    modifier_name=domain["modifier"],
    material_name="Multiframe Water",
    config={"preset": "WATER"},
)
assert material["created"] is True

preview_path = str(Path(tempfile.gettempdir()) / "blendermcp-liquid-multiframe-preview.png")
Path(preview_path).unlink(missing_ok=True)
preview = handler.render_pbr_material_preview(
    "Multiframe Water",
    "BLENDER_EEVEE_NEXT",
    "SPHERE",
    32,
    4,
    True,
    {"BLENDER_EEVEE_NEXT": preview_path},
)
assert preview["outputs"][0]["size_bytes"] > 0

rendered = bpy.data.images.load(preview_path)
try:
    pixels = list(rendered.pixels)
    alphas = pixels[3::4]
    reds = pixels[0::4]
    assert any(alpha < 0.05 for alpha in alphas), "expected transparent background pixels around the sphere"
    assert any(alpha > 0.5 for alpha in alphas), "expected the rendered sphere to be visible"
    assert max(reds) - min(reds) > 0.01, "rendered material should not be a single flat color"
finally:
    bpy.data.images.remove(rendered)

# Nothing was ever paused mid-bake (the --background bake ran synchronously to
# completion), so RESUME must reject the already-finished stage rather than silently
# re-running the bake operator. Blender leaves cache_frame_pause_data set to the final
# frame after a normal completed bake too (not just an interrupted pause), so the
# already-fully-baked check below is required in addition to the pause-frame check.
try:
    handler.manage_liquid_cache(
        domain_object_name=domain["object"],
        modifier_name=domain["modifier"],
        action="RESUME",
        stage="DATA",
        confirm_bake=True,
    )
except ValueError as error:
    assert "is already fully baked; nothing to resume" in str(error)
else:
    raise AssertionError("RESUME should have rejected an already-finished stage")

# The DATA stage already finished, so nothing is currently baking; PAUSE must reject
# rather than silently no-op.
try:
    handler.manage_liquid_cache(
        domain_object_name=domain["object"],
        modifier_name=domain["modifier"],
        action="PAUSE",
    )
except ValueError as error:
    assert "No liquid cache stage is currently baking" in str(error)
else:
    raise AssertionError("PAUSE should have rejected a domain with nothing baking")

print("BLENDER_LIQUID_MULTIFRAME_SMOKE_OK")
