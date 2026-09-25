"""Run with Blender 5.1+ to smoke-test declarative scene composition handlers."""

import math
import sys
import tempfile

from pathlib import Path

import bpy

sys.path.append(str(Path(__file__).resolve().parent))
from smoke_addon import load_addon

load_addon("blender_mcp_scene_smoke")

from blender_mcp_scene_smoke.handlers.scene import SceneHandlersMixin  # ruff: ignore[module-import-not-at-top-of-file]
from blender_mcp_scene_smoke.server_core import BlenderMCPServer  # ruff: ignore[module-import-not-at-top-of-file]


def rounded(values):
    """Serialize a float sequence at the precision float32 transforms actually carry."""
    return [round(float(value), 5) for value in values]


def main() -> None:
    """Exercise native geometry, transforms, collections, hierarchy, constraints, and modifiers."""
    handler = SceneHandlersMixin()
    triangle = handler.create_geometry_object(
        "Scene Smoke Triangle",
        {
            "kind": "MESH",
            "vertices": [(0, 0, 0), (1, 0, 0), (0, 1, 0)],
            "edges": [],
            "faces": [(0, 1, 2)],
        },
    )
    assert triangle["type"] == "MESH"

    curve = handler.create_geometry_object(
        "Scene Smoke Curve",
        {
            "kind": "CURVE",
            "splines": [
                {
                    "type": "BEZIER",
                    "points": [
                        {
                            "co": (0, 0, 0),
                            "radius": 0.5,
                            "tilt": 0.25,
                            "weight": 0.75,
                            "handle_left_type": "VECTOR",
                            "handle_right_type": "ALIGNED",
                        },
                        {"co": (1, 1, 0), "radius": 1.5, "tilt": -0.25},
                    ],
                }
            ],
            "bevel_depth": 0.05,
        },
    )
    assert curve["type"] == "CURVE"
    curve_data = bpy.data.objects[curve["name"]].data
    assert math.isclose(curve_data.splines[0].bezier_points[0].radius, 0.5)
    assert curve_data.splines[0].bezier_points[0].handle_left_type == "VECTOR"

    try:
        handler.create_geometry_object(
            "Unsupported Surface Grid",
            {
                "kind": "SURFACE",
                "splines": [
                    {
                        "type": "NURBS",
                        "points": [(0, 0, 0), (1, 0, 0), (0, 1, 0), (1, 1, 0)],
                        "point_count_u": 2,
                        "point_count_v": 2,
                    }
                ],
            },
        )
    except ValueError as exc:
        assert "cannot author arbitrary Surface U/V topology" in str(exc)
    else:
        raise AssertionError("Read-only Surface U/V topology was silently flattened")
    assert bpy.data.curves.get("Unsupported Surface Grid") is None

    hair = handler.create_geometry_object(
        "Scene Smoke Hair",
        {
            "kind": "CURVES",
            "points": [(0, 0, 0), (0, 0, 1), (1, 0, 0), (1, 0, 1), (1, 0, 2)],
            "curve_sizes": [2, 3],
            "cyclic": [False, True],
            "attributes": [{"name": "guide_weight", "data_type": "FLOAT", "domain": "CURVE", "values": [0.25, 0.75]}],
        },
    )
    assert hair["type"] == "CURVES"
    assert hair["geometry"]["counts"] == {"curves": 2, "points": 5}
    assert bpy.data.objects[hair["name"]].data.attributes["guide_weight"].domain == "CURVE"

    grease_pencil = handler.create_geometry_object(
        "Scene Smoke Grease Pencil",
        {
            "kind": "GREASEPENCIL",
            "layers": [
                {
                    "name": "Annotations",
                    "frames": [
                        {
                            "frame_number": 1,
                            "strokes": [
                                {
                                    "points": [(0, 0, 0), (1, 0, 0), (1, 1, 0)],
                                    "cyclic": True,
                                    "radii": [0.5, 0.75, 1.0],
                                    "opacities": [1.0, 0.75, 0.5],
                                }
                            ],
                            "attributes": [
                                {
                                    "name": "stroke_id",
                                    "data_type": "INT",
                                    "domain": "STROKE",
                                    "values": [7],
                                }
                            ],
                        }
                    ],
                }
            ],
        },
    )
    assert grease_pencil["type"] == "GREASEPENCIL"
    assert grease_pencil["geometry"]["counts"] == {"layers": 1, "frames": 1, "strokes": 1, "points": 3}

    inspector = object.__new__(BlenderMCPServer)
    hair_info = inspector.get_object_info(hair["name"], sections=["GEOMETRY", "ATTRIBUTES"], limit=1)
    assert hair_info["type_data"]["curves"] == {"point_count": 5, "curve_count": 2, "surface": None}
    assert hair_info["type_data"]["attributes"]["limit"] == 1
    grease_info = inspector.get_object_info(grease_pencil["name"], sections=["GREASE_PENCIL"], limit=1)
    assert grease_info["type_data"]["grease_pencil"]["layers"]["returned_count"] == 1

    points = handler.create_geometry_object(
        "Scene Smoke Points",
        {"kind": "POINTCLOUD", "points": [(0, 0, 0), (1, 2, 3)], "radii": [0.1, 0.2]},
    )
    assert points["type"] == "POINTCLOUD"

    volume_file = Path(tempfile.gettempdir()) / "blender_mcp_missing_smoke.vdb"
    try:
        handler.create_geometry_object("Missing Volume", {"kind": "VOLUME", "filepath": str(volume_file)})
    except ValueError as exc:
        assert "does not exist" in str(exc)
    else:
        raise AssertionError("Missing volume path was accepted")

    handler.set_object_transform(
        "Scene Smoke Triangle",
        {"location": (2, 3, 4), "rotation_quaternion": (1, 0, 0, 0), "scale": (2, 2, 2)},
        "WORLD",
    )
    obj = bpy.data.objects["Scene Smoke Triangle"]
    assert tuple(round(value, 5) for value in obj.matrix_world.translation) == (2.0, 3.0, 4.0)

    copies = handler.duplicate_or_instance_objects(
        obj.name,
        ["Scene Smoke Linked", "Scene Smoke Copy"],
        [
            {"location": (0, 0, 0), "rotation": (0, 0, 0), "scale": (1, 1, 1)},
            {"location": (4, 0, 0), "rotation": (0, 0, 0), "scale": (1, 1, 1)},
        ],
        "LINKED_DATA",
    )
    assert len(copies["objects"]) == 2
    assert bpy.data.objects["Scene Smoke Linked"].data is obj.data

    collection = handler.manage_scene_collections("CREATE", "Scene Smoke Collection")
    assert collection["name"] == "Scene Smoke Collection"
    handler.manage_scene_collections("LINK_OBJECTS", "Scene Smoke Collection", [obj.name])
    assert bpy.data.collections["Scene Smoke Collection"].objects.get(obj.name) is obj

    handler.manage_object_hierarchy(
        [{"child_object_name": "Scene Smoke Linked", "parent_object_name": obj.name}],
        preserve_world_transform=True,
    )
    assert bpy.data.objects["Scene Smoke Linked"].parent is obj

    # --- World-space reply fields report the evaluated scene, not the pre-edit one -------------
    # `matrix_world` is owned by the dependency graph: after a write to `location` or
    # `matrix_basis` it keeps its pre-edit value - identity for an object this session has never
    # evaluated - until the graph re-evaluates. A reply that decomposed it without flushing first
    # reported a world transform the scene did not hold, while `location` beside it was correct.
    pivot = bpy.data.objects.new("Scene Smoke Pivot", None)
    bpy.context.scene.collection.objects.link(pivot)
    pivot.location = (10.0, 0.0, 0.0)
    born = handler.create_geometry_object(
        "Scene Smoke Child",
        {"kind": "MESH", "vertices": [(0, 0, 0), (1, 0, 0), (0, 1, 0)], "edges": [], "faces": [(0, 1, 2)]},
        location=(4.0, 0.0, 0.0),
    )
    assert rounded(born["transform"]["world_location"]) == [4.0, 0.0, 0.0], born["transform"]
    handler.manage_object_hierarchy(
        [{"child_object_name": "Scene Smoke Child", "parent_object_name": "Scene Smoke Pivot"}],
        preserve_world_transform=False,
    )
    child = bpy.data.objects["Scene Smoke Child"]

    local_move = handler.set_object_transform("Scene Smoke Child", {"location": (1.0, 2.0, 3.0)}, "LOCAL")
    independent = inspector.get_object_info("Scene Smoke Child")
    bpy.context.view_layer.update()
    truth = rounded(child.matrix_world.translation)
    assert truth == [11.0, 2.0, 3.0], truth
    assert rounded(local_move["location"]) == [1.0, 2.0, 3.0], local_move
    assert rounded(local_move["world_location"]) == truth, local_move
    assert rounded(row[3] for row in local_move["matrix_world"][:3]) == truth, local_move
    assert rounded(row[3] for row in independent["matrix_world"][:3]) == truth, independent["matrix_world"]

    # A WORLD-space patch solves its basis against the parent's evaluated matrix, so a parent
    # moved earlier in this same session used to drag the result by the parent's un-evaluated delta.
    pivot.location = (20.0, 0.0, 0.0)
    world_move = handler.set_object_transform("Scene Smoke Child", {"location": (5.0, 0.0, 0.0)}, "WORLD")
    bpy.context.view_layer.update()
    assert rounded(child.matrix_world.translation) == [5.0, 0.0, 0.0], rounded(child.matrix_world.translation)
    assert rounded(world_move["world_location"]) == [5.0, 0.0, 0.0], world_move

    carrier = bpy.data.objects.new("Scene Smoke Carrier", None)
    bpy.context.scene.collection.objects.link(carrier)
    bpy.context.view_layer.update()
    preserved = rounded(child.matrix_world.translation)
    carrier.location = (100.0, 0.0, 0.0)
    handler.manage_object_hierarchy(
        [{"child_object_name": "Scene Smoke Child", "parent_object_name": "Scene Smoke Carrier"}],
        preserve_world_transform=True,
    )
    bpy.context.view_layer.update()
    assert rounded(child.matrix_world.translation) == preserved, rounded(child.matrix_world.translation)

    # A duplicate with no transform of its own inherits the source's world matrix.
    child.location = (7.0, 7.0, 7.0)
    handler.duplicate_or_instance_objects("Scene Smoke Child", ["Scene Smoke Inherit"], None, "LINKED_DATA")
    bpy.context.view_layer.update()
    inherited = rounded(bpy.data.objects["Scene Smoke Inherit"].matrix_world.translation)
    assert inherited == rounded(child.matrix_world.translation), inherited

    handler.remove_scene_objects(
        ["Scene Smoke Inherit", "Scene Smoke Child", "Scene Smoke Carrier", "Scene Smoke Pivot"],
        confirm_remove=True,
    )

    handler.manage_object_constraints(
        "Scene Smoke Copy",
        "ADD",
        {
            "name": "Smoke Copy Location",
            "type": "COPY_LOCATION",
            "target_object_name": obj.name,
            "influence": 0.5,
            "settings": {"use_x": True, "use_y": False, "use_z": False},
        },
    )
    assert bpy.data.objects["Scene Smoke Copy"].constraints.get("Smoke Copy Location") is not None

    modifier = handler.manage_modifiers(
        obj.name,
        "ADD",
        {"name": "Smoke Bevel", "type": "BEVEL", "settings": {"width": 0.05, "segments": 2}},
    )
    assert modifier["modifier"] == "Smoke Bevel"

    removed = handler.remove_scene_objects(
        ["Scene Smoke Curve", "Scene Smoke Points", "Scene Smoke Hair", "Scene Smoke Grease Pencil"],
        confirm_remove=True,
    )
    assert set(removed["removed"]) == {
        "Scene Smoke Curve",
        "Scene Smoke Points",
        "Scene Smoke Hair",
        "Scene Smoke Grease Pencil",
    }

    triangle_name = obj.name
    remaining_objects = sorted(o.name for o in bpy.context.scene.objects)
    assert remaining_objects, "expected leftover objects before the reset_scene smoke test"

    reset = handler.reset_scene(confirm_reset=True)
    assert reset["scene"] == bpy.context.scene.name
    assert set(reset["unlinked_objects"]) >= {triangle_name, "Scene Smoke Copy"}
    assert list(bpy.context.scene.objects) == []
    assert list(bpy.context.scene.collection.children) == []
    assert reset["purged_datablock_count"] > 0
    assert bpy.data.objects.get(remaining_objects[0]) is None

    print("SCENE_SMOKE_OK")


if __name__ == "__main__":
    main()
