# This script runs inside Blender against live datablocks. bpy's collection
# stubs widen their elements to `ID`, which loses the concrete Object/Curve the
# API actually hands back, so argument types here are stub noise rather than
# reachable states. Same boundary as `src/blender_mcp/bundled` in pyproject.toml.
# pyright: reportArgumentType=false
"""
Run with Blender 5.1+ to check that geometry-nodes builders honour world placement and radius.

Every assertion reads the evaluated depsgraph in world space, never a builder's reply. Each host
object sits away from the world origin, so a builder that read a referenced object's local data
instead of where it sits would put its result somewhere else, and the assertion names where.
"""

from __future__ import annotations

import importlib.util
import sys

from pathlib import Path

import bmesh
import bpy

addon_path = Path(__file__).resolve().parents[1] / "src" / "blender_mcp" / "bundled" / "addon" / "__init__.py"
package_name = "blender_mcp_geometry_nodes_transform_smoke"
spec = importlib.util.spec_from_file_location(
    package_name, addon_path, submodule_search_locations=[str(addon_path.parent)]
)
assert spec is not None
addon = importlib.util.module_from_spec(spec)
sys.modules[package_name] = addon
spec.loader.exec_module(addon)

from blender_mcp_geometry_nodes_transform_smoke.handlers.geometry_nodes import (  # ruff: ignore[module-import-not-at-top-of-file]
    GeometryNodesHandlersMixin,
)

# How far an evaluated position may sit from where the builder promises it, in scene units.
TOLERANCE = 1e-3
# A cube's eight corners: what an uncut, undeformed cube evaluates to.
CUBE_VERTEX_COUNT = 8


class GeometryNodesSmokeHarness(GeometryNodesHandlersMixin):
    """Expose the Geometry Nodes handler mixins for direct Blender smoke testing."""


def _cube(name: str, location: tuple[float, float, float], size: float = 2.0):
    bpy.ops.mesh.primitive_cube_add(size=size, location=location)
    obj = bpy.context.active_object
    obj.name = name
    return obj


def _empty_mesh(name: str, location: tuple[float, float, float]):
    obj = bpy.data.objects.new(name, bpy.data.meshes.new(name))
    obj.location = location
    bpy.context.scene.collection.objects.link(obj)
    return obj


def _line_curve(name: str, location: tuple[float, float, float], start=(-1.0, 0.0, 0.0), end=(1.0, 0.0, 0.0)):
    """Create a straight two-point POLY curve in the XY plane of its own space."""
    data = bpy.data.curves.new(name, "CURVE")
    data.dimensions = "3D"
    spline = data.splines.new("POLY")
    spline.points.add(1)
    spline.points[0].co = (*start, 1.0)
    spline.points[1].co = (*end, 1.0)
    obj = bpy.data.objects.new(name, data)
    obj.location = location
    bpy.context.scene.collection.objects.link(obj)
    return obj


def _world_points(obj) -> list[tuple[float, float, float]]:
    """Evaluate `obj` and return every vertex of the result in world space."""
    bpy.context.view_layer.update()
    evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    mesh = evaluated.to_mesh()
    try:
        return [tuple(evaluated.matrix_world @ vertex.co) for vertex in mesh.vertices]
    finally:
        evaluated.to_mesh_clear()


def _centroid(points) -> tuple[float, ...]:
    assert points, "the builder evaluated to no geometry"
    return tuple(sum(point[axis] for point in points) / len(points) for axis in range(3))


def _extent(points, axis: int) -> float:
    return max(point[axis] for point in points) - min(point[axis] for point in points)


def _near(actual, expected) -> bool:
    return all(abs(a - e) <= TOLERANCE for a, e in zip(actual, expected, strict=True))


def _check_boolean_cuts_where_the_cutter_sits(handler) -> None:
    target = _cube("Boolean Target", (3.0, 0.0, 0.0))
    cutter = _cube("Boolean Cutter", (3.0, 0.0, 1.0), size=1.0)
    handler.create_procedural_boolean(target.name, "Boolean Transform", cutter_source="OBJECT", cutter_name=cutter.name)
    overlapping = _world_points(target)
    assert len(overlapping) > CUBE_VERTEX_COUNT, f"an overlapping cutter left the cube uncut: {len(overlapping)} verts"
    assert max(point[2] for point in overlapping) <= 1.0 + TOLERANCE
    cutter.location = (3.0, 0.0, 10.0)
    clear = _world_points(target)
    assert len(clear) == CUBE_VERTEX_COUNT, f"a cutter 9 units away still cut the target: {len(clear)} verts"


def _check_radial_pivot_is_the_world_centroid(handler) -> None:
    host = _empty_mesh("Radial Host", (5.0, 0.0, 0.0))
    pivot = bpy.data.objects.new("Radial Pivot", None)
    pivot.location = (0.0, 4.0, 0.0)
    bpy.context.scene.collection.objects.link(pivot)
    # Far from both, so a source read where it sits would drag every copy with it.
    source = _cube("Radial Source", (-10.0, -10.0, 0.0), size=0.2)
    handler.create_procedural_array(
        host.name,
        "Radial Transform",
        source_name=source.name,
        layout="RADIAL",
        count=4,
        pivot_object_name=pivot.name,
        realize_instances=True,
    )
    centroid = _centroid(_world_points(host))
    assert _near(centroid, (0.0, 4.0, 0.0)), f"radial centroid {centroid} is not the pivot's world location"


def _check_curve_array_follows_a_moved_curve(handler) -> None:
    host = _empty_mesh("Curve Array Host", (2.0, 0.0, 0.0))
    path = _line_curve("Curve Array Path", (0.0, 0.0, -5.0))
    source = _cube("Curve Array Source", (-10.0, 10.0, 0.0), size=0.2)
    handler.create_procedural_array(
        host.name,
        "Curve Array Transform",
        source_name=source.name,
        layout="CURVE",
        count=3,
        curve_object_name=path.name,
        realize_instances=True,
    )
    centroid = _centroid(_world_points(host))
    assert _near(centroid, (0.0, 0.0, -5.0)), (
        f"curve array centroid {centroid} does not follow the curve's world placement"
    )


def _check_curve_generator_follows_a_moved_curve(handler) -> None:
    host = _empty_mesh("Path Host", (2.0, 0.0, 0.0))
    path = _line_curve("Moved Path", (0.0, 0.0, 7.0))
    # Far off the path: a profile read where it sits would carry the tube 50 units along Y.
    profile = _line_curve("Offset Profile", (0.0, 50.0, 0.0), start=(0.0, -1.0, 0.0), end=(0.0, 1.0, 0.0))
    handler.create_curve_generator(
        host.name,
        "Path Transform",
        curve_object_name=path.name,
        profile_object_name=profile.name,
        radius=0.1,
    )
    points = _world_points(host)
    centroid = _centroid(points)
    assert _near(centroid, (0.0, 0.0, 7.0)), (
        f"generated mesh centroid {centroid} does not follow the path's world placement"
    )
    assert _extent(points, 0) > 2.0 - TOLERANCE, "the generated mesh does not span the path"


def _check_curve_radius_scales_the_profile(handler) -> None:
    extents = {}
    for radius in (0.05, 0.5):
        host = _empty_mesh(f"Radius Host {radius}", (0.0, 0.0, 0.0))
        path = _line_curve(f"Radius Path {radius}", (0.0, 0.0, 0.0))
        handler.create_curve_generator(host.name, f"Radius {radius}", curve_object_name=path.name, radius=radius)
        extents[radius] = _extent(_world_points(host), 2)
    assert abs(extents[0.05] - 0.1) <= TOLERANCE, f"radius 0.05 gave a {extents[0.05]} thick tube"
    assert abs(extents[0.5] - 1.0) <= TOLERANCE, f"radius 0.5 gave a {extents[0.5]} thick tube"


def _check_proximity_push_measures_to_the_target_where_it_sits(handler) -> None:
    host = _cube("Proximity Host", (-6.0, 0.0, 0.0))
    target = _cube("Proximity Target", (-6.0, 0.0, 0.0), size=0.5)
    handler.create_procedural_deformer(
        host.name,
        "Proximity Transform",
        template="PROXIMITY_PUSH",
        strength=0.5,
        scale=3.0,
        target_object_name=target.name,
    )
    near = _world_points(host)
    assert _extent(near, 0) > 2.0 + TOLERANCE, "a target inside the host did not push it"
    target.location = (100.0, 0.0, 0.0)
    far = _world_points(host)
    assert abs(_extent(far, 0) - 2.0) <= TOLERANCE, "a target 106 units away still pushed the host"


def _data_cube(name: str, size: float, collection):
    """Build a mesh cube linked only into `collection`, without operators that act on the active scene."""
    mesh = bpy.data.meshes.new(name)
    shape = bmesh.new()
    bmesh.ops.create_cube(shape, size=size)
    shape.to_mesh(mesh)
    shape.free()
    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)
    return obj


def _check_an_object_in_another_scene_is_evaluated_in_its_own(handler) -> None:
    """
    Build and inspect an array whose host lives only in a scene that is not the active one.

    The context's graph evaluates the active view layer alone: read from it, the host comes back
    as its base cube with no array, and a frame the inspection asks for moves the wrong scene's
    playhead. A host in no view layer at all can still take a modifier, but nothing evaluates it.
    """
    active = bpy.context.scene
    elsewhere = bpy.data.scenes.new("Elsewhere")
    host = _data_cube("Elsewhere Host", 1.0, elsewhere.collection)
    source = _data_cube("Elsewhere Source", 0.2, elsewhere.collection)
    built = handler.create_procedural_array(
        host.name,
        "Elsewhere Array",
        source_name=source.name,
        layout="LINEAR",
        count=5,
        spacing=(1.0, 0.0, 0.0),
        realize_instances=True,
    )
    counts = built["evaluated"]["mesh_counts"]
    assert counts == {"vertices": 40, "edges": 60, "faces": 30}, f"the builder read the base cube: {counts}"

    for frame, x in ((1, 0.0), (10, 10.0)):
        host.location.x = x
        host.keyframe_insert("location", frame=frame)
    elsewhere.frame_set(1)
    active_frame = active.frame_current
    inspected = handler.inspect_evaluated_geometry(host.name, frame=10)
    assert inspected["frame"] == 10, inspected["frame"]
    assert inspected["mesh_counts"]["vertices"] == 40, inspected["mesh_counts"]
    low_x = inspected["world_bounds"]["min"][0]
    assert abs(low_x - 9.9) <= TOLERANCE, f"inspected at frame 10, the host's array starts at x={low_x}"
    assert (active.frame_current, elsewhere.frame_current) == (active_frame, 1), "a playhead was left moved"

    excluded = bpy.data.collections.new("Elsewhere Excluded")
    elsewhere.collection.children.link(excluded)
    elsewhere.view_layers[0].layer_collection.children[excluded.name].exclude = True
    unseen = _data_cube("Elsewhere Unseen", 1.0, excluded)
    attached = handler.create_procedural_array(
        unseen.name, "Unseen Array", source_name=source.name, layout="LINEAR", count=2, spacing=(1.0, 0.0, 0.0)
    )
    assert "in no view layer" in attached["evaluated"]["evaluation_unavailable"], attached["evaluated"]
    try:
        handler.inspect_evaluated_geometry(unseen.name)
    except ValueError as refusal:
        assert "in no view layer" in str(refusal), refusal
    else:
        raise AssertionError("an object no view layer evaluates was inspected as if it were evaluated")


def main() -> None:
    """Check each builder against world placement, then the curve radius."""
    handler = GeometryNodesSmokeHarness()
    _check_boolean_cuts_where_the_cutter_sits(handler)
    _check_radial_pivot_is_the_world_centroid(handler)
    _check_curve_array_follows_a_moved_curve(handler)
    _check_curve_generator_follows_a_moved_curve(handler)
    _check_curve_radius_scales_the_profile(handler)
    _check_proximity_push_measures_to_the_target_where_it_sits(handler)
    _check_an_object_in_another_scene_is_evaluated_in_its_own(handler)
    print("GEOMETRY_NODES_TRANSFORM_SMOKE_OK")


if __name__ == "__main__":
    main()
