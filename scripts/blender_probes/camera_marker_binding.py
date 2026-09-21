r"""
Print who applies a timeline camera marker's binding, and the two ways it silently does not.

A camera marker binds a camera from its frame onward, and nothing in a reply says which camera a
frame actually rendered through, so every claim here is the kind that gets assumed wrongly. The
probe answers four of them, in the order a render loop meets them:

- whether `scene.frame_set` applies the binding, and whether a bare `scene.frame_current`
  assignment does (it is the same playhead, and only one of the two evaluates the scene);
- whether `bpy.ops.render.render(write_still=True)` applies it again over a `scene.camera` set
  between the two, which decides whether a per-frame still loop can choose its own camera at all;
- which camera a frame *before* the earliest marker resolves to - Blender falls back to that
  marker's camera, not to `scene.camera`, so one marker at frame 167 also claims frames 1-166;
- whether a marker whose camera has `hide_render` set is honoured, since a skipped marker leaves
  `scene.camera` rendering every frame as though the marker had never been created.

From the repository root::

    /opt/homebrew/bin/blender --background --factory-startup \
        --python scripts/blender_probes/camera_marker_binding.py
"""

import os
import tempfile

import bpy


def _cameras(scene: "bpy.types.Scene") -> tuple["bpy.types.Object", ...]:
    """
    Link two cameras and make the first one the scene's own.

    Args:
        scene: Scene to link them into.

    Returns:
        tuple: The two camera objects, in creation order.

    """
    made = []
    for name in ("CamA", "CamB"):
        camera = bpy.data.objects.new(name, bpy.data.cameras.new(name))
        scene.collection.objects.link(camera)
        made.append(camera)
    scene.camera = made[0]
    return tuple(made)


def _mark(scene: "bpy.types.Scene", name: str, frame: int, camera: "bpy.types.Object") -> None:
    """
    Bind a camera at a frame, the way create_camera_markers does.

    Args:
        scene: Scene holding the timeline.
        name: Marker name.
        frame: Frame the marker sits on.
        camera: Camera object to bind.

    """
    marker = scene.timeline_markers.new(name, frame=frame)
    marker.camera = camera


def _report(label: str, scene: "bpy.types.Scene") -> None:
    """
    Print one observation as `label -> camera name`.

    Args:
        label: What was just done.
        scene: Scene whose active camera is the answer.

    """
    print(f"  {label} -> scene.camera = {scene.camera.name if scene.camera else None}")


scene = bpy.context.scene
scene.render.engine = "BLENDER_WORKBENCH"
scene.render.resolution_x = 16
scene.render.resolution_y = 16
cam_a, cam_b = _cameras(scene)
_mark(scene, "cut_a", 1, cam_a)
_mark(scene, "cut_b", 5, cam_b)

print(f"=== BLENDER === {bpy.app.version_string}")
print("\n=== markers: cut_a@1 -> CamA, cut_b@5 -> CamB ===")

print("\n=== does scene.frame_set apply the binding? ===")
scene.frame_set(6)
_report("frame_set(6)", scene)
scene.frame_set(2)
_report("frame_set(2)", scene)

print("\n=== does a bare scene.frame_current assignment apply it? ===")
scene.camera = cam_a
scene.frame_current = 6
_report("scene.camera = CamA, then scene.frame_current = 6", scene)

print("\n=== does the still render operator re-apply it over a forced scene.camera? ===")
scene.frame_set(6)
scene.camera = cam_a
with tempfile.TemporaryDirectory() as directory:
    scene.render.filepath = os.path.join(directory, "probe.png")
    result = bpy.ops.render.render(animation=False, write_still=True, scene=scene.name)
_report(f"scene.camera = CamA at frame 6, render {sorted(result)}", scene)

print("\n=== what does a frame before the earliest marker resolve to? ===")
for marker in list(scene.timeline_markers):
    if marker.name == "cut_a":
        scene.timeline_markers.remove(marker)
scene.camera = cam_a
scene.frame_set(1)
_report("only marker is cut_b@5 -> CamB, scene.camera = CamA, frame_set(1)", scene)

print("\n=== is a marker camera hidden from render honoured? ===")
cam_b.hide_render = True
scene.camera = cam_a
scene.frame_set(6)
_report("CamB.hide_render = True, scene.camera = CamA, frame_set(6)", scene)
