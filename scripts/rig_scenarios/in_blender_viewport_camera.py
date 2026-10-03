"""
Pose the live 3D viewport and project points through it, for `scenario_viewport_camera.py`.

Runs inside Blender as a `--blender-script`. The scenario runs outside Blender and cannot set
`RegionView3D.view_matrix` or call `bpy_extras.view3d_utils` itself, so it asks through files
in the rig's work dir and this timer answers:

- `view_request.json` -> `view_ready.json`: set the viewport's lens, projection, distance and
  view matrix (or switch it into camera view), make the scene's render aspect the region's, and
  report the region size and the view matrix the viewport actually ended up with.
- `project_request.json` -> `project_result.json`: report a camera's `matrix_world` and where
  each world point lands, normalized 0..1 from the bottom-left, through `world_to_camera_view`
  for the camera and through `location_3d_to_region_2d` over the region size for the viewport.

The viewport used is the largest VIEW_3D WINDOW region, the same one `create_camera` reads.
"""

import json
import os
import pathlib

from collections.abc import Callable

import bpy
import mathutils

from bpy_extras.object_utils import world_to_camera_view
from bpy_extras.view3d_utils import location_3d_to_region_2d

WORK = pathlib.Path(os.environ["BLENDERMCP_RIG_WORK_DIR"])
VIEW_REQUEST = WORK / "view_request.json"
VIEW_READY = WORK / "view_ready.json"
PROJECT_REQUEST = WORK / "project_request.json"
PROJECT_RESULT = WORK / "project_result.json"


def _largest_view3d() -> tuple:
    """
    Return (window, area, region, space) for the largest 3D viewport.

    Returns:
        tuple: The window, area, WINDOW region and SpaceView3D of the largest VIEW_3D.

    Raises:
        RuntimeError: If no window shows a 3D viewport.

    """
    best = None
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type != "VIEW_3D":
                continue
            for region in area.regions:
                if region.type == "WINDOW" and (best is None or region.width * region.height > best[0]):
                    best = (region.width * region.height, window, area, region, area.spaces.active)
    if best is None:
        raise RuntimeError("no 3D viewport in any window")
    return best[1:]


def _rows(matrix: mathutils.Matrix) -> list[list[float]]:
    """
    Flatten a matrix to nested lists for JSON.

    Args:
        matrix: Any mathutils matrix.

    Returns:
        list[list[float]]: Its rows.

    """
    return [list(row) for row in matrix]


def _set_view(request: dict) -> dict:
    """
    Apply one view request to the largest viewport.

    Args:
        request: mode (PERSP, ORTHO or CAMERA), plus lens, distance and view_matrix rows, or
            camera for CAMERA.

    Returns:
        dict: The region size and the viewport's resulting view matrix and settings.

    """
    window, area, region, space = _largest_view3d()
    r3d = space.region_3d
    scene = bpy.context.scene
    scene.render.resolution_x = region.width
    scene.render.resolution_y = region.height
    scene.render.resolution_percentage = 100
    scene.render.pixel_aspect_x = 1.0
    scene.render.pixel_aspect_y = 1.0
    if request["mode"] == "CAMERA":
        scene.camera = bpy.data.objects[request["camera"]]
        r3d.view_perspective = "CAMERA"
    else:
        space.lens = request["lens"]
        r3d.view_perspective = request["mode"]
        r3d.view_distance = request["distance"]
        r3d.view_matrix = mathutils.Matrix(request["view_matrix"])
    with bpy.context.temp_override(window=window, area=area, region=region):
        r3d.update()
    return {
        "region": [region.width, region.height],
        "view_matrix": _rows(r3d.view_matrix),
        "view_perspective": r3d.view_perspective,
        "view_distance": r3d.view_distance,
        "lens": space.lens,
    }


def _project(request: dict) -> dict:
    """
    Project world points through a camera and through the largest viewport.

    Args:
        request: camera (an object name) and points (world-space triples).

    Returns:
        dict: The camera's matrix_world, the viewport's inverted view matrix, and both
        normalized projections of every point.

    """
    _window, _area, region, space = _largest_view3d()
    r3d = space.region_3d
    scene = bpy.context.scene
    camera = bpy.data.objects[request["camera"]]
    camera_ndc = []
    region_ndc = []
    for point in request["points"]:
        world = mathutils.Vector(point)
        projected = world_to_camera_view(scene, camera, world)
        camera_ndc.append([projected.x, projected.y])
        pixel = location_3d_to_region_2d(region, r3d, world)
        region_ndc.append(None if pixel is None else [pixel.x / region.width, pixel.y / region.height])
    return {
        "matrix_world": _rows(camera.matrix_world),
        "view_matrix_inverted": _rows(r3d.view_matrix.inverted()),
        "camera_ndc": camera_ndc,
        "region_ndc": region_ndc,
        "camera_type": camera.data.type,
        "lens": camera.data.lens,
    }


def _answer(request_path: pathlib.Path, result_path: pathlib.Path, handler: Callable[[dict], dict]) -> None:
    """
    Answer one pending request file, writing either the result or the error.

    Args:
        request_path: The request the scenario wrote.
        result_path: Where the scenario polls for the answer.
        handler: The function turning the request into a result.

    """
    request = json.loads(request_path.read_text(encoding="utf-8"))
    request_path.unlink()
    try:
        result = {"ok": True, **handler(request)}
    except Exception as error:  # every failure must reach the scenario, not the console
        result = {"ok": False, "error": f"{type(error).__name__}: {error}"}
    result_path.write_text(json.dumps(result), encoding="utf-8")


def _watch() -> float:
    """
    Poll for the scenario's requests.

    Returns:
        float: Seconds until the next poll.

    """
    if VIEW_REQUEST.is_file():
        _answer(VIEW_REQUEST, VIEW_READY, _set_view)
    if PROJECT_REQUEST.is_file():
        _answer(PROJECT_REQUEST, PROJECT_RESULT, _project)
    return 0.1


bpy.app.timers.register(_watch, persistent=True)
print("RIG-BLENDER: viewport camera helper waiting for requests in", WORK, flush=True)
