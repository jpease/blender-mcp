# ruff: file-ignore[module-import-not-at-top-of-file]
"""
Blender 5.1+ background smoke coverage for `pick_from_camera`, through the add-on's own dispatch.

Every expected point is a known world point, and the frame point aimed at it comes from Blender's
own `world_to_camera_view`, so a pick that lands on it proves the frame-point mapping end to end:
lens shift, a vertical sensor fit, an ortho camera, and an animated object at another frame.

Run with::

    blender --background --factory-startup --python tests/blender_pick_smoke.py
"""

import math
import sys

from pathlib import Path

import bpy
import mathutils

from bpy_extras.object_utils import world_to_camera_view

sys.path.append(str(Path(__file__).resolve().parent))
from smoke_addon import load_addon

addon = load_addon("blender_mcp_pick_smoke")

from blender_mcp_pick_smoke.server_core import BlenderMCPServer

pick_module = sys.modules["blender_mcp_pick_smoke.handlers.pick"]
rays = addon.pick_rays
TOLERANCE_M = 1e-4
scene = bpy.context.scene
server = BlenderMCPServer()


def _fresh_scene() -> None:
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj)
    for collection in list(bpy.data.collections):
        bpy.data.collections.remove(collection)
    scene.render.resolution_x = 1920
    scene.render.resolution_y = 1080
    scene.render.pixel_aspect_x = scene.render.pixel_aspect_y = 1.0
    scene.frame_set(1)


def _box(name: str, center, half, collection=None) -> bpy.types.Object:
    hx, hy, hz = half
    mesh = bpy.data.meshes.new(name)
    corners = [(x, y, z) for x in (-hx, hx) for y in (-hy, hy) for z in (-hz, hz)]
    mesh.from_pydata(corners, [], [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)])
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    obj.location = center
    (collection or scene.collection).objects.link(obj)
    return obj


def _camera(name: str, location, look_at, **optics) -> bpy.types.Object:
    data = bpy.data.cameras.new(name)
    for key, value in optics.items():
        setattr(data, key, value)
    camera = bpy.data.objects.new(name, data)
    camera.location = location
    direction = mathutils.Vector(look_at) - mathutils.Vector(location)
    camera.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
    scene.collection.objects.link(camera)
    bpy.context.view_layer.update()
    return camera


def _frame_point(camera: bpy.types.Object, point) -> list[float]:
    bpy.context.view_layer.update()
    projected = world_to_camera_view(scene, camera, mathutils.Vector(point))
    assert 0.0 < projected.x < 1.0 and 0.0 < projected.y < 1.0 and projected.z > 0.0, (point, projected)
    return [projected.x, projected.y]


def _pick(**params) -> dict:
    response = server.execute_command_internal({"type": "pick_from_camera", "params": params})
    assert response["status"] == "success", response
    return response["result"]


def _refusal(**params) -> str:
    response = server.execute_command_internal({"type": "pick_from_camera", "params": params})
    assert response["status"] == "error", response
    return response["message"]


def _assert_lands(record: dict, object_name: str, point, normal=None) -> None:
    assert record["hit"] == "OBJECT" and record["object_name"] == object_name, record
    error = (mathutils.Vector(record["target_point"]) - mathutils.Vector(point)).length
    assert error <= TOLERANCE_M, (record, point, error)
    if normal is not None:
        assert (mathutils.Vector(record["normal"]) - mathutils.Vector(normal)).length < 1e-4, record


def _cube_face_case(**optics) -> None:
    _fresh_scene()
    _box("Cube", (0.0, 0.0, 1.0), (1.0, 1.0, 1.0))
    camera = _camera("Cam", (1.5, -6.0, 2.5), (0.0, 0.0, 1.0), lens=50.0, **optics)
    face_centre = (0.0, -1.0, 1.0)
    off_centre = (0.6, -1.0, 1.5)
    result = _pick(camera_name="Cam", points=[_frame_point(camera, face_centre), _frame_point(camera, off_centre)])
    _assert_lands(result["points"][0], "Cube", face_centre, (0.0, -1.0, 0.0))
    _assert_lands(result["points"][1], "Cube", off_centre, (0.0, -1.0, 0.0))
    assert result["projection"] == "PERSP" and result["skipped_hit_count"] == 0, result


def case_cube_face_with_lens_shift() -> None:
    _cube_face_case(shift_x=0.15, shift_y=-0.05)


def case_cube_face_with_vertical_sensor_fit() -> None:
    _cube_face_case(sensor_fit="VERTICAL", sensor_height=20.0)


def case_off_object_lands_on_the_ground_plane() -> None:
    _fresh_scene()
    _box("Cube", (0.0, 0.0, 1.0), (1.0, 1.0, 1.0))
    camera = _camera("Cam", (1.5, -6.0, 2.5), (0.0, 0.0, 1.0), lens=35.0)
    ground = (-1.8, -1.5, 0.0)
    (record,) = _pick(camera_name="Cam", points=[_frame_point(camera, ground)])["points"]
    assert record["hit"] == "GROUND_PLANE" and record["object_name"] is None, record
    assert (mathutils.Vector(record["target_point"]) - mathutils.Vector(ground)).length <= TOLERANCE_M, record
    assert math.isclose(record["distance"], (mathutils.Vector(ground) - camera.location).length, abs_tol=TOLERANCE_M)
    # A level camera's upper half rises away from the ground plane; this one faces away from the cube.
    _camera("Level", (0.0, -6.0, 1.0), (0.0, -20.0, 1.0))
    (rising,) = _pick(camera_name="Level", points=[[0.5, 0.9]])["points"]
    assert rising["hit"] == "NONE" and rising["target_point"] is None, rising


def case_region_ranks_the_larger_object_first() -> None:
    _fresh_scene()
    big = _box("Big", (-0.8, 0.0, 2.0), (0.9, 0.5, 0.9))
    small = _box("Small", (1.0, 0.0, 2.0), (0.4, 0.4, 0.4))
    # Aimed below them, so both sit in the upper half of the frame.
    camera = _camera("Cam", (0.0, -10.0, 1.0), (0.0, 0.0, 0.4), lens=20.0)
    # bpy's stub types a bound_box entry as a float rather than the 3-component array it is.
    corners = [
        _frame_point(camera, obj.matrix_world @ mathutils.Vector(corner))  # pyright: ignore[reportArgumentType]
        for obj in (big, small)
        for corner in obj.bound_box
    ]
    region = {
        "u_min": min(u for u, _v in corners) - 0.02,
        "v_min": min(v for _u, v in corners) - 0.02,
        "u_max": max(u for u, _v in corners) + 0.02,
        "v_max": max(v for _u, v in corners) + 0.02,
    }
    assert region["v_min"] > 0.5, region
    result = _pick(camera_name="Cam", region=region)
    names = [entry["object_name"] for entry in result["objects"]]
    assert names == ["Big", "Small"], result
    assert result["objects"][0]["coverage"] > result["objects"][1]["coverage"], result
    for entry in result["objects"]:
        assert entry["centroid"]["object_name"] == entry["object_name"], entry
    assert result["sample_count"] == 256 and 0.0 < result["background_fraction"] < 1.0, result


def case_ortho_camera() -> None:
    _fresh_scene()
    _box("Cube", (0.0, 0.0, 1.0), (1.0, 1.0, 1.0))
    camera = _camera("Ortho", (2.0, -6.0, 3.0), (0.0, 0.0, 1.0), type="ORTHO", ortho_scale=6.0)
    targets = [(0.0, -1.0, 1.0), (-0.7, -1.0, 0.4), (0.5, -0.5, 2.0)]
    result = _pick(camera_name="Ortho", points=[_frame_point(camera, point) for point in targets])
    assert result["projection"] == "ORTHO", result
    _assert_lands(result["points"][0], "Cube", targets[0], (0.0, -1.0, 0.0))
    _assert_lands(result["points"][1], "Cube", targets[1], (0.0, -1.0, 0.0))
    _assert_lands(result["points"][2], "Cube", targets[2], (0.0, 0.0, 1.0))


def _animated_mover() -> bpy.types.Object:
    mover = _box("Mover", (-3.0, 0.0, 1.0), (0.5, 0.5, 0.5))
    mover.keyframe_insert("location", frame=1)
    mover.location = (3.0, 0.0, 1.0)
    mover.keyframe_insert("location", frame=21)
    return mover


def case_another_frame_follows_the_animated_object() -> None:
    _fresh_scene()
    _animated_mover()
    camera = _camera("Cam", (0.0, -10.0, 1.0), (0.0, 0.0, 1.0), lens=24.0)
    scene.frame_set(21)
    face_at_21 = (3.0, -0.5, 1.2)
    target = _frame_point(camera, face_at_21)
    scene.frame_set(1)
    (record,) = _pick(camera_name="Cam", points=[target], frame=21)["points"]
    _assert_lands(record, "Mover", face_at_21, (0.0, -1.0, 0.0))


def case_the_playhead_is_unchanged_after_picking_another_frame() -> None:
    _fresh_scene()
    _animated_mover()
    _camera("Cam", (0.0, -10.0, 1.0), (0.0, 0.0, 1.0), lens=24.0)
    scene.frame_set(4, subframe=0.25)
    location_before = tuple(bpy.data.objects["Mover"].matrix_world.translation)
    result = _pick(camera_name="Cam", points=[[0.5, 0.5]], frame=17.5)
    assert math.isclose(result["frame"], 17.5), result
    assert (scene.frame_current, scene.frame_subframe) == (4, 0.25), (scene.frame_current, scene.frame_subframe)
    assert tuple(bpy.data.objects["Mover"].matrix_world.translation) == location_before


def case_a_hide_render_proxy_is_skipped_under_render_and_hit_under_viewport() -> None:
    _fresh_scene()
    _box("Cube", (0.0, 0.0, 1.0), (1.0, 1.0, 1.0))
    proxy = _box("Proxy", (0.0, -3.0, 1.0), (1.5, 0.05, 1.5))
    proxy.hide_render = True
    camera = _camera("Cam", (0.0, -8.0, 1.0), (0.0, 0.0, 1.0))
    target = _frame_point(camera, (0.0, -1.0, 1.0))
    render = _pick(camera_name="Cam", points=[target])
    _assert_lands(render["points"][0], "Cube", (0.0, -1.0, 1.0))
    assert render["skipped_hit_count"] == 2, render  # into the proxy and out of it
    assert render["skipped_objects"] == [{"object_name": "Proxy", "reason": "HIDE_RENDER"}], render
    viewport = _pick(camera_name="Cam", points=[target], visibility="VIEWPORT")
    _assert_lands(viewport["points"][0], "Proxy", (0.0, -3.05, 1.0))
    assert viewport["skipped_hit_count"] == 0 and "hidden_in_viewport_count" not in viewport, viewport


def case_a_viewport_hidden_render_object_is_counted() -> None:
    _fresh_scene()
    _box("Cube", (0.0, 0.0, 1.0), (1.0, 1.0, 1.0))
    hidden = _box("Unseen", (0.0, -3.0, 1.0), (0.5, 0.5, 0.5))
    hidden.hide_viewport = True
    camera = _camera("Cam", (0.0, -8.0, 1.0), (0.0, 0.0, 1.0))
    result = _pick(camera_name="Cam", points=[_frame_point(camera, (0.0, -1.0, 1.0))])
    _assert_lands(result["points"][0], "Cube", (0.0, -1.0, 1.0))
    assert result["hidden_in_viewport_count"] == 1, result
    assert any("'Unseen'" in warning for warning in result.get("warnings", [])), result


def case_a_collection_instance_is_hit_though_its_source_is_excluded() -> None:
    _fresh_scene()
    source = bpy.data.collections.new("Source")
    scene.collection.children.link(source)
    _box("Wall", (0.0, 0.0, 1.0), (1.0, 0.1, 1.0), collection=source)
    bpy.context.view_layer.layer_collection.children["Source"].exclude = True
    instance = bpy.data.objects.new("WallInstance", None)
    instance.instance_type = "COLLECTION"
    instance.instance_collection = source
    instance.location = (2.0, 0.0, 0.0)
    scene.collection.objects.link(instance)
    camera = _camera("Cam", (2.0, -8.0, 1.0), (2.0, 0.0, 1.0))
    (record,) = _pick(camera_name="Cam", points=[_frame_point(camera, (2.0, -0.1, 1.0))])["points"]
    _assert_lands(record, "Wall", (2.0, -0.1, 1.0))


def case_a_panoramic_camera_is_refused() -> None:
    _fresh_scene()
    _box("Cube", (0.0, 0.0, 1.0), (1.0, 1.0, 1.0))
    _camera("Pano", (0.0, -8.0, 1.0), (0.0, 0.0, 1.0), type="PANO")
    message = _refusal(camera_name="Pano", points=[[0.5, 0.5]])
    assert "panoramic" in message, message


CASES = [
    case_cube_face_with_lens_shift,
    case_cube_face_with_vertical_sensor_fit,
    case_off_object_lands_on_the_ground_plane,
    case_region_ranks_the_larger_object_first,
    case_ortho_camera,
    case_another_frame_follows_the_animated_object,
    case_the_playhead_is_unchanged_after_picking_another_frame,
    case_a_hide_render_proxy_is_skipped_under_render_and_hit_under_viewport,
    case_a_viewport_hidden_render_object_is_counted,
    case_a_collection_instance_is_hit_though_its_source_is_excluded,
    case_a_panoramic_camera_is_refused,
]


def main() -> None:
    for case in CASES:
        case()
        print(f"ok {case.__name__}")
    print("PICK_SMOKE_OK")


if __name__ == "__main__":
    main()
