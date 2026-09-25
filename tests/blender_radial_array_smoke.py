"""
Run with Blender 5.1+ to smoke-test add_radial_array_modifier's world-space pivot.

The unit tests for this handler (`tests/server/tools/test_mesh_model.py`) can only assert
that the helper empty's `matrix_world` was assigned the matrix the handler intended. They
cannot say whether Blender's Array modifier then *rotates the copies around that world
pivot*, because the fake `bpy` has no modifier evaluation - and the production audit's
standing finding is precisely that an object-offset matrix might not be a world-space
"rotate around this point" operation at all.

This script settles it against the real evaluated depsgraph: it reads the evaluated mesh's
vertices, transforms them to world space, and compares that point set against the one an
explicit translate-to-pivot / rotate / translate-back composition predicts, for the four
cases the audit named - an object away from the origin, a rotated and non-uniformly scaled
object, a parented object whose parent carries its own transform, and the `radius`-derived
pivot.
"""

import math
import sys

from pathlib import Path

import bpy
import mathutils

sys.path.append(str(Path(__file__).resolve().parent))
from smoke_addon import load_addon

load_addon("blender_mcp_radial_array_smoke")

from blender_mcp_radial_array_smoke import server_core  # ruff: ignore[module-import-not-at-top-of-file]

# The evaluated copies are float32 rasterizations of a float64 matrix product, so an exact
# comparison is not available; 1e-5 m is four orders of magnitude below the 0.4 m geometry
# being placed and still catches a wrong pivot, which misplaces a copy by whole units.
TOLERANCE = 1e-5


def _cube(name, size=0.4):
    """
    Add a small cube at the origin and return it.

    Args:
        name: Object name to give the cube.
        size: Edge length, small enough that a misplaced copy cannot overlap a correct one.

    Returns:
        bpy.types.Object: The new cube, linked to the scene collection.

    """
    bpy.ops.mesh.primitive_cube_add(size=size)
    created = bpy.context.active_object
    assert created is not None, "primitive_cube_add left no active object"
    created.name = name
    return created


def _world_points(obj):
    """
    Read one object's evaluated vertices in world space.

    Args:
        obj: The object whose modifier stack should be evaluated.

    Returns:
        list: Every evaluated vertex, in world space.

    """
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = obj.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh()
    try:
        matrix = evaluated.matrix_world
        return [matrix @ vertex.co.copy() for vertex in mesh.vertices]
    finally:
        evaluated.to_mesh_clear()


def _expected_points(base_points, pivot, axis, count):
    """
    Predict the array's world-space point set from an explicit pivot rotation.

    This is the composition the audit asked the handler to be checked against - built here
    from `mathutils` primitives rather than from the handler's own helper, so a bug in that
    helper cannot cancel itself out.

    Args:
        base_points: The unmodified object's world-space vertices.
        pivot: World-space point every copy rotates around.
        axis: 'X', 'Y' or 'Z'.
        count: Total number of copies, the original included.

    Returns:
        list: Every expected world-space vertex.

    """
    angle = (2 * math.pi) / count
    expected = []
    for index in range(count):
        rotation = (
            mathutils.Matrix.Translation(pivot)
            @ mathutils.Matrix.Rotation(angle * index, 4, axis)
            @ mathutils.Matrix.Translation(-pivot)
        )
        expected.extend(rotation @ point for point in base_points)
    return expected


def _assert_same_point_set(actual, expected, label):
    """
    Assert two world-space point sets match within TOLERANCE, order-independently.

    Args:
        actual: Points read off the evaluated mesh.
        expected: Points the pivot composition predicts.
        label: Case name, so a failure says which case failed.

    Returns:
        float: The worst per-vertex deviation observed, in metres.

    Raises:
        AssertionError: If the counts differ or any expected point has no match.

    """
    assert len(actual) == len(expected), f"{label}: {len(actual)} evaluated vertices, expected {len(expected)}"
    unmatched = list(actual)
    worst = 0.0
    for point in expected:
        index, distance = min(
            ((index, (candidate - point).length) for index, candidate in enumerate(unmatched)),
            key=lambda pair: pair[1],
        )
        assert distance <= TOLERANCE, (
            f"{label}: no evaluated vertex within {TOLERANCE} m of {tuple(round(v, 6) for v in point)}; "
            f"nearest was {distance} m away"
        )
        worst = max(worst, distance)
        del unmatched[index]
    return worst


def _run_case(server, label, obj, pivot, axis, count, **kwargs):
    """
    Add a radial array to one object and compare the evaluated result against the prediction.

    Args:
        server: The handler mixin instance under test.
        label: Case name for failure messages.
        obj: The object to array.
        pivot: The world-space pivot the case expects.
        axis: Rotation axis.
        count: Copy count.
        **kwargs: The pivot argument under test (pivot_location, pivot_object_name, radius).

    Returns:
        float: The worst per-vertex deviation observed, in metres.

    """
    base_points = _world_points(obj)
    result = server.add_radial_array_modifier(object_name=obj.name, count=count, axis=axis, **kwargs)
    assert result["applied"] is False, result
    bpy.context.view_layer.update()
    worst = _assert_same_point_set(_world_points(obj), _expected_points(base_points, pivot, axis, count), label)
    print(f"{label}: {count} copies about {tuple(round(v, 3) for v in pivot)}, worst deviation {worst:.3e} m")
    return worst


def _offset_case(server):
    """
    Array an object that sits away from the world origin around the origin itself.

    Args:
        server: The handler mixin instance under test.

    """
    offset = _cube("RadialOffset")
    offset.location = (2.0, 0.0, 0.0)
    bpy.context.view_layer.update()
    _run_case(
        server,
        "away from the origin",
        offset,
        mathutils.Vector((0.0, 0.0, 0.0)),
        "Z",
        4,
        pivot_location=(0.0, 0.0, 0.0),
    )


def _skewed_case(server):
    """
    Array a rotated, non-uniformly scaled object around an arbitrary world point.

    Args:
        server: The handler mixin instance under test.

    """
    skewed = _cube("RadialSkewed")
    skewed.location = (1.5, -0.5, 0.25)
    skewed.rotation_euler = (0.3, -0.7, 1.1)
    skewed.scale = (1.7, 0.6, 2.3)
    bpy.context.view_layer.update()
    _run_case(
        server,
        "rotated and non-uniformly scaled",
        skewed,
        mathutils.Vector((1.0, -2.0, 0.5)),
        "Z",
        5,
        pivot_location=(1.0, -2.0, 0.5),
    )


def _parented_case(server):
    """
    Array a child of a moved, rotated and scaled parent around a pivot object.

    Args:
        server: The handler mixin instance under test.

    """
    parent = bpy.data.objects.new("RadialParent", None)
    bpy.context.collection.objects.link(parent)
    parent.location = (-3.0, 4.0, 1.0)
    parent.rotation_euler = (0.0, 0.0, math.pi / 3)
    parent.scale = (2.0, 2.0, 2.0)
    child = _cube("RadialChild")
    child.parent = parent
    bpy.context.view_layer.update()
    child.matrix_parent_inverse = parent.matrix_world.inverted()
    child.location = (1.0, 0.0, 0.0)
    pivot_empty = bpy.data.objects.new("RadialPivotTarget", None)
    bpy.context.collection.objects.link(pivot_empty)
    pivot_empty.location = (-1.0, 2.0, 0.0)
    bpy.context.view_layer.update()
    _run_case(
        server,
        "parented to a transformed parent",
        child,
        pivot_empty.matrix_world.translation.copy(),
        "Z",
        6,
        pivot_object_name=pivot_empty.name,
    )


def _radius_case(server):
    """
    Array around the pivot the `radius` argument derives, rather than a given point.

    Args:
        server: The handler mixin instance under test.

    """
    # Axis Z offsets along its perpendicular axis, X (_RADIAL_AXIS_PERP).
    spun = _cube("RadialRadius")
    spun.location = (0.0, 1.0, 0.0)
    bpy.context.view_layer.update()
    _run_case(
        server,
        "radius-derived pivot",
        spun,
        mathutils.Vector((-3.0, 1.0, 0.0)),
        "Z",
        3,
        radius=3.0,
    )


def main() -> None:
    """Prove the arrayed copies land where a world-space pivot rotation puts them."""
    server = server_core.BlenderMCPServer()
    _offset_case(server)
    _skewed_case(server)
    _parented_case(server)
    _radius_case(server)
    print("RADIAL_ARRAY_SMOKE_OK")


main()
