# ruff: file-ignore[module-import-not-at-top-of-file]
"""Run with Blender 5.1+ to smoke-test scene unit/gravity/sync-mode handlers."""

import math
import sys

from pathlib import Path

import bpy

sys.path.append(str(Path(__file__).resolve().parent))
from smoke_addon import load_addon

load_addon("blender_mcp_scene_physics_smoke")

from blender_mcp_scene_physics_smoke.handlers.scene_physics import ScenePhysicsHandlersMixin


def main() -> None:
    """Exercise unit/gravity/sync-mode patching, rollback, the non-default-scale warning, and seconds-to-frame."""
    handler = ScenePhysicsHandlersMixin()
    scene = bpy.context.scene

    scene.render.fps = 24
    scene.render.fps_base = 1.0
    scene.frame_start = 1

    info = handler.get_scene_physics_info(scene.name, convert_seconds=[0.0, 5.0])
    assert info["unit_settings"]["system"] == "METRIC"
    assert info["seconds_to_frame"] == [
        {"seconds": 0.0, "frame": 1.0},
        {"seconds": 5.0, "frame": 121.0},
    ]

    scaled = bpy.data.objects.new("ScaledCube", bpy.data.meshes.new("ScaledCubeMesh"))
    scaled.scale = (2.0, 2.0, 2.0)
    scene.collection.objects.link(scaled)
    try:
        configured = handler.configure_scene_physics(
            scene.name,
            {"system": "IMPERIAL", "scale_length": 0.5, "gravity": (0.0, 0.0, -1.62), "use_gravity": True},
        )
        assert configured["settings"]["unit_settings"]["system"] == "IMPERIAL"
        assert math.isclose(configured["settings"]["unit_settings"]["scale_length"], 0.5)
        assert all(
            math.isclose(actual, expected, abs_tol=1e-6)
            for actual, expected in zip(configured["settings"]["gravity"], (0.0, 0.0, -1.62), strict=True)
        )
        assert configured["warnings"], "expected a non-default-scale warning"
        assert "ScaledCube" in configured["warnings"][0]
    finally:
        bpy.data.objects.remove(scaled, do_unlink=True)

    try:
        handler.configure_scene_physics(scene.name, {"scale_length": 1000.0})
    except ValueError as exc:
        assert "between" in str(exc)
    else:
        raise AssertionError("Out-of-range scale_length was accepted")
    assert math.isclose(scene.unit_settings.scale_length, 0.5)

    try:
        handler.configure_scene_physics(scene.name, {"sync_mode": "NOT_A_MODE"})
    except ValueError as exc:
        assert "sync_mode" in str(exc)
    else:
        raise AssertionError("Invalid sync_mode was accepted")
    assert scene.sync_mode == "NONE"

    reverted = handler.configure_scene_physics(
        scene.name, {"system": "METRIC", "scale_length": 1.0, "sync_mode": "NONE"}
    )
    assert reverted["settings"]["sync_mode"] == "NONE"
    assert reverted["warnings"] == []
    print("SCENE_PHYSICS_SMOKE_OK")


if __name__ == "__main__":
    main()
