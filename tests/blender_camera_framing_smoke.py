# ruff: file-ignore[module-import-not-at-top-of-file]
"""
Blender 5.1+ background smoke coverage for framing a camera on objects.

Run with::

    blender --background --factory-startup --python tests/blender_camera_framing_smoke.py
"""

import importlib.util
import sys

from pathlib import Path

import bpy

addon_path = Path(__file__).resolve().parents[1] / "src" / "blender_mcp" / "bundled" / "addon" / "__init__.py"
package_name = "blender_mcp_camera_framing_smoke"
spec = importlib.util.spec_from_file_location(
    package_name, addon_path, submodule_search_locations=[str(addon_path.parent)]
)
assert spec is not None
addon = importlib.util.module_from_spec(spec)
sys.modules[package_name] = addon
spec.loader.exec_module(addon)

from blender_mcp_camera_framing_smoke.handlers.camera import CameraHandlersMixin

PLAUSIBLE_LENS_MM = (15.0, 60.0)
LENS_TOLERANCE_MM = 1e-6


def _new_object(name: str, data: bpy.types.ID | None = None) -> bpy.types.Object:
    obj = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(obj)
    return obj


scene = bpy.context.scene
handler = CameraHandlersMixin()

# A 1.6 x 0.8 x 1.2 m group 2.1 m in front of the camera: two children, as in a shot.
group_mesh = bpy.data.meshes.new("Framing Group Mesh")
corners = [(x, y, z) for x in (-0.8, 0.8) for y in (-0.4, 0.4) for z in (0.0, 1.2)]
faces = [(0, 1, 3, 2), (4, 5, 7, 6), (0, 1, 5, 4), (2, 3, 7, 6), (0, 2, 6, 4), (1, 3, 7, 5)]
group_mesh.from_pydata(corners, [], faces)
group = _new_object("Framing Group", group_mesh)
camera = _new_object("Framing Camera", bpy.data.cameras.new("Framing Camera Data"))
camera.location = (0.0, -2.1, 0.6)
camera.rotation_euler = (1.5708, 0.0, 0.0)
camera.data.lens = 30.0
bpy.context.view_layer.update()

# CHANGE_LENS keeps the camera where it is and solves the focal length that fits the subject.
framed = handler.frame_camera_on_objects(scene.name, camera.name, [group.name], margin=0.12, policy="CHANGE_LENS")
low, high = PLAUSIBLE_LENS_MM
assert low < framed["lens"] < high, f"solved lens {framed['lens']} mm is not a plausible framing"
assert abs(camera.data.lens - framed["lens"]) < LENS_TOLERANCE_MM, "the reported lens is not the camera's lens"

print("CAMERA_FRAMING_SMOKE_OK")
