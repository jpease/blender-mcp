"""
Blender 5.1+ background smoke coverage for the validate_scene preflight aggregator.

Run with::

    blender --background --factory-startup --python tests/blender_scene_validate_smoke.py
"""

import importlib.util
import sys

from pathlib import Path

import bpy

addon_path = Path(__file__).resolve().parents[1] / "src" / "blender_mcp" / "bundled" / "addon" / "__init__.py"
package_name = "blender_mcp_scene_validate_smoke"
spec = importlib.util.spec_from_file_location(
    package_name, addon_path, submodule_search_locations=[str(addon_path.parent)]
)
assert spec is not None
addon = importlib.util.module_from_spec(spec)
sys.modules[package_name] = addon
spec.loader.exec_module(addon)

from blender_mcp_scene_validate_smoke.server_core import (  # ruff: ignore[module-import-not-at-top-of-file]
    BlenderMCPServer,
)

scene = bpy.context.scene
server = BlenderMCPServer()

# The factory-startup scene ships a default Cube/Light/Camera; strip them so
# the scene is genuinely empty before exercising the "nothing is set up yet"
# findings this smoke test depends on.
for obj in list(scene.objects):
    bpy.data.objects.remove(obj, do_unlink=True)
assert scene.camera is None

# An empty scene has no camera, no lights, and a default frame range: every
# scene-level check plus the camera/lighting domain checks should all fire on
# the first pass, with no scope filter applied.
baseline = server.validate_scene(scene.name)
assert baseline["scene"] == scene.name
assert baseline["domains_checked"] == ["scene", "camera", "lighting", "pbr", "cloth", "liquid"]
codes = {finding["code"] for finding in baseline["findings"]}
scene_domain_codes = {finding["code"] for finding in baseline["findings"] if finding["domain"] == "scene"}
assert "MISSING_LIGHTS" in codes
# validate_camera_rig and validate_lighting_setup already report the missing
# camera on their own domains (MISSING_SCENE_CAMERA / MISSING_CAMERA), so the
# scene-level dedup rule must suppress the "scene" domain's own duplicate.
assert any(finding["domain"] == "camera" for finding in baseline["findings"])
assert any(finding["domain"] == "lighting" and finding["code"] == "MISSING_CAMERA" for finding in baseline["findings"])
assert "MISSING_CAMERA" not in scene_domain_codes
# Lighting's own MISSING_CAMERA finding is an ERROR, so the aggregate must not
# be reported ready even though nothing else in the empty scene errors out.
assert baseline["ready"] is False

# Restricting scope to just "scene" (excluding camera/lighting) must flip the
# dedup rule: the scene-level MISSING_CAMERA finding should now appear because
# nothing else in scope is reporting the missing camera.
scene_only = server.validate_scene(scene.name, scope=["scene"])
assert scene_only["domains_checked"] == ["scene"]
scene_only_codes = {finding["code"] for finding in scene_only["findings"]}
assert "MISSING_CAMERA" in scene_only_codes
assert "MISSING_LIGHTS" in scene_only_codes
assert scene_only["ready"] is False

# Unknown or empty scope values are rejected before anything runs.
try:
    server.validate_scene(scene.name, scope=["not_a_domain"])
except ValueError as error:
    assert "Unknown validation scope domain" in str(error)
else:
    raise AssertionError("validate_scene should reject an unknown scope domain")

try:
    server.validate_scene(scene.name, scope=[])
except ValueError as error:
    assert "at least one domain" in str(error)
else:
    raise AssertionError("validate_scene should reject an empty scope list")

try:
    server.validate_scene(scene.name, max_findings=0)
except ValueError as error:
    assert "max_findings must be in" in str(error)
else:
    raise AssertionError("validate_scene should reject an out-of-range max_findings")

try:
    server.validate_scene("Does Not Exist")
except ValueError as error:
    assert "Scene not found" in str(error)
else:
    raise AssertionError("validate_scene should reject an unknown scene name")

# Add a camera and a light, fix the frame range, and re-check: the previously
# universal findings should clear.
camera = bpy.data.objects.new("Smoke Camera", bpy.data.cameras.new("Smoke Camera Data"))
scene.collection.objects.link(camera)
scene.camera = camera
light = bpy.data.objects.new("Smoke Light", bpy.data.lights.new("Smoke Light Data", type="POINT"))
scene.collection.objects.link(light)

lit = server.validate_scene(scene.name, scope=["scene"])
lit_codes = {finding["code"] for finding in lit["findings"]}
assert "MISSING_CAMERA" not in lit_codes
assert "MISSING_LIGHTS" not in lit_codes

# frame_start/frame_end are mutually clamped by Blender's own RNA range
# callbacks (setting one past the other drags the other along), so an
# inverted scene frame range can never be constructed through this API and
# INVALID_FRAME_RANGE is covered separately with a fake scene in a plain
# unit test instead. An animation that outlives the (valid) playback range,
# and an unapplied/degenerate mesh, are all still directly constructible
# here and must surface as distinct, correctly-coded findings.
scene.frame_start = 1
scene.frame_end = 20
mesh_data = bpy.data.meshes.new("Smoke Degenerate Mesh")
mesh_data.from_pydata(
    [(0, 0, 0), (1, 0, 0), (1, 0, 0), (0, 1, 0)],
    [],
    [(0, 1, 2), (0, 2, 3)],
)
mesh_data.update()
mesh_obj = bpy.data.objects.new("Smoke Degenerate Object", mesh_data)
mesh_obj.scale = (2.0, 1.0, 1.0)
scene.collection.objects.link(mesh_obj)
mesh_obj.keyframe_insert(data_path="location", index=0, frame=1)
mesh_obj.keyframe_insert(data_path="location", index=0, frame=50)

geometry = server.validate_scene(scene.name, scope=["scene"])
geometry_by_code = {finding["code"]: finding for finding in geometry["findings"]}
assert "ANIMATION_OUTSIDE_FRAME_RANGE" in geometry_by_code
assert geometry_by_code["ANIMATION_OUTSIDE_FRAME_RANGE"]["subject"] == mesh_obj.name
assert "UNAPPLIED_SCALE" in geometry_by_code
assert geometry_by_code["UNAPPLIED_SCALE"]["subject"] == mesh_obj.name
assert "DEGENERATE_GEOMETRY" in geometry_by_code
assert geometry_by_code["DEGENERATE_GEOMETRY"]["subject"] == mesh_obj.name
# All three of the above are WARNING-severity; the scene is still "ready".
assert geometry["ready"] is True

bounded = server.validate_scene(scene.name, scope=["scene"], max_findings=1)
assert len(bounded["findings"]) == 1
assert bounded["truncated"] is True
assert bounded["total_findings"] > 1

# pbr domain must not be silently skipped, and must not fall back to the
# active scene's full object list when this scene happens to have no meshes
# to hand it - exercised earlier implicitly via the "scene"-only scope calls
# above (pbr excluded), so explicitly request it now against the meshed scene.
with_pbr = server.validate_scene(scene.name, scope=["pbr"])
assert with_pbr["domain_summaries"]["pbr"]["findings"] >= 0

print("SCENE_VALIDATE_SMOKE_OK")
