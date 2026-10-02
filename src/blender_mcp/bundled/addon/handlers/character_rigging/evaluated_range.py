"""
`sample_evaluated_range`: read a performance back from the depsgraph across a bounded set of frames.

One call steps the playhead through the requested frames and, at each, reads where the named
bones are and how the named meshes sit - their evaluated world bounds and, against other meshes,
how many of their vertices are inside and how deep. Nothing renders and nothing is written: the
evaluated meshes are released before the next frame, the trees built over them are plain Python
objects, and the playhead goes back to the frame and subframe it was on.

The inside test is ray parity, not closest-point normal sign. A point is inside a closed surface
exactly when a ray from it crosses that surface an odd number of times, whatever way the
surface's normals face; the closest-point test reads the sign off one face's normal and answers
wrongly near a concave edge, where the closest face can face away from the point it encloses.
Each point casts three rays in fixed, mutually skewed directions and takes the majority, so one
ray grazing an edge cannot flip the answer, and a vertex within `_CONTACT_TOLERANCE_M` of the
surface counts as touching rather than inside. Parity means something only for a closed
surface, so every `against` mesh is checked for open edges and reported.
"""

from collections import Counter

import bpy
import mathutils
import mathutils.bvhtree

from ...helpers import evaluated_bone_world_points, paginate
from .primitives import _armature_object, _mesh_object, _unique_names, _validate_limit_offset

# The most frames one call evaluates, whichever form names them. Every frame is a full
# depsgraph evaluation on Blender's main thread, which nothing else can use while it runs.
MAX_SAMPLED_FRAMES = 250
# Frames per page: the frames a call actually evaluates and returns. Every number in the reply
# costs a line of indented JSON: one bone runs ~230 bytes a frame and one mesh against a floor
# ~650, so ten frames of a bone and a mesh fill the budget, and frames past what fits are
# evaluated only for the envelope to cut them.
_DEFAULT_FRAME_LIMIT = 10
# A tenth of a millimetre: finer than any performance check reads, and three bytes a number
# cheaper than the six decimals the transform readers carry - bytes this reply spends by the
# thousand.
_DECIMALS = 4
# A vertex this close to the other surface is touching it, not inside it: a face resting on a
# floor lies on the floor to float precision, where a parity count is a coin toss.
_CONTACT_TOLERANCE_M = 1e-5
# How far past a hit the next ray starts. Larger than float32 resolves at scene scale, so the
# face just crossed is not hit again; small enough to step over no real wall.
_RAY_STEP_M = 1e-5
# Three skewed directions, none along an axis or a diagonal, where modelled geometry lines up
# with a ray and grazes edges. Normalized at use.
_PARITY_DIRECTIONS = ((0.5773, 0.3184, 0.7519), (-0.6627, 0.7246, 0.1890), (0.2031, -0.4412, -0.8743))
# A ray that keeps hitting past this many faces is caught in degenerate geometry, not crossing it.
_MAX_RAY_HITS = 10_000
# A closed surface borders every edge with exactly two faces; one face is a hole, three a fin.
_FACES_PER_CLOSED_EDGE = 2
_BONE_POINT_FIELDS = {"armature_object_name", "bone_name"}
_MESH_METRIC_FIELDS = {"object_names", "against_object_names"}


def _integer(value, label):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    return value


def _resolved_frames(frames, frame_start, frame_end, frame_step):
    """
    Turn exactly one frame form into the ordered frames to sample, refusing more than the cap.

    Args:
        frames: Explicit frames, or None.
        frame_start: First frame of a range, or None.
        frame_end: Last frame of a range, inclusive, or None.
        frame_step: Range step.

    Returns:
        list[int]: The frames, in sampling order.

    Raises:
        ValueError: If neither or both forms are given, a range is half given or reversed, an
            explicit frame repeats, or the selection names more than `MAX_SAMPLED_FRAMES` frames.

    """
    has_range = frame_start is not None or frame_end is not None
    if (frames is not None) == has_range:
        raise ValueError("Supply exactly one of frames or frame_start/frame_end")
    if frames is not None:
        if not isinstance(frames, list) or not frames:
            raise ValueError("frames must be a non-empty list of integers")
        resolved = [_integer(frame, f"frames[{index}]") for index, frame in enumerate(frames)]
        _unique_names(resolved, "frames")
        if len(resolved) > MAX_SAMPLED_FRAMES:
            raise ValueError(
                f"frames names {len(resolved)} frames; at most {MAX_SAMPLED_FRAMES} are sampled per call. "
                "Split the list across calls."
            )
        return resolved
    if frame_start is None or frame_end is None:
        raise ValueError("frame_start and frame_end must be supplied together")
    start = _integer(frame_start, "frame_start")
    end = _integer(frame_end, "frame_end")
    step = _integer(frame_step, "frame_step")
    if step < 1:
        raise ValueError("frame_step must be at least 1")
    if end < start:
        raise ValueError("frame_end must not be before frame_start")
    count = (end - start) // step + 1
    if count > MAX_SAMPLED_FRAMES:
        raise ValueError(
            f"frame_start={start}..frame_end={end} at frame_step={step} names {count} frames; at most "
            f"{MAX_SAMPLED_FRAMES} are sampled per call. Raise frame_step to at least "
            f"{(end - start) // MAX_SAMPLED_FRAMES + 1} or split the range across calls."
        )
    return list(range(start, end + 1, step))


def _resolved_bone_points(bone_points):
    """
    Resolve each requested bone to its armature object, refusing anything that is not there.

    Args:
        bone_points: `{armature_object_name, bone_name}` records, or None.

    Returns:
        list[tuple]: `(armature object, bone name)` in request order.

    Raises:
        ValueError: If a record is malformed, its object is missing or not an armature, the
            bone is not on it, or a bone is named twice.

    """
    resolved = []
    for index, point in enumerate(bone_points or []):
        label = f"bone_points[{index}]"
        if not isinstance(point, dict):
            raise ValueError(f"{label} must be an object with armature_object_name and bone_name")
        unsupported = set(point) - _BONE_POINT_FIELDS
        if unsupported:
            raise ValueError(f"{label} has unsupported fields: {sorted(unsupported)}")
        armature = _armature_object(point.get("armature_object_name"))
        bone_name = point.get("bone_name")
        if not isinstance(bone_name, str) or not bone_name:
            raise ValueError(f"{label}.bone_name must be a non-empty string")
        if armature.pose is None or armature.pose.bones.get(bone_name) is None:
            raise ValueError(f"{label} bone '{bone_name}' does not exist on armature '{armature.name}'")
        resolved.append((armature, bone_name))
    _unique_names([f"{armature.name}:{bone_name}" for armature, bone_name in resolved], "bone_points")
    return resolved


def _resolved_mesh_metrics(mesh_metrics):
    """
    Resolve the measured meshes and the meshes they are tested against.

    Args:
        mesh_metrics: `{object_names, against_object_names?}`, or None.

    Returns:
        tuple: The measured mesh objects and the against mesh objects, each in request order.

    Raises:
        ValueError: If the record is malformed, a list repeats a name, or a name is missing or
            not a mesh.

    """
    if mesh_metrics is None:
        return [], []
    if not isinstance(mesh_metrics, dict):
        raise ValueError("mesh_metrics must be an object with object_names")
    unsupported = set(mesh_metrics) - _MESH_METRIC_FIELDS
    if unsupported:
        raise ValueError(f"mesh_metrics has unsupported fields: {sorted(unsupported)}")
    object_names = mesh_metrics.get("object_names")
    if not isinstance(object_names, list) or not object_names:
        raise ValueError("mesh_metrics.object_names must be a non-empty list of mesh object names")
    against_names = mesh_metrics.get("against_object_names") or []
    if not isinstance(against_names, list):
        raise ValueError("mesh_metrics.against_object_names must be a list of mesh object names")
    _unique_names(object_names, "mesh_metrics.object_names")
    _unique_names(against_names, "mesh_metrics.against_object_names")
    return [_mesh_object(name) for name in object_names], [_mesh_object(name) for name in against_names]


def _rounded(vector):
    return [round(float(value), _DECIMALS) for value in vector]


def _bounds(points):
    if not points:
        return None
    return {
        "minimum": _rounded(min(point[axis] for point in points) for axis in range(3)),
        "maximum": _rounded(max(point[axis] for point in points) for axis in range(3)),
    }


def _world_mesh(obj, depsgraph):
    """
    Read one evaluated mesh's world vertices and polygons, releasing the evaluated mesh.

    Args:
        obj: The original mesh object.
        depsgraph: The depsgraph at the current frame.

    Returns:
        tuple: World vertex positions, polygon vertex lists, and whether every edge borders
        exactly two faces.

    Raises:
        ValueError: If the object does not evaluate to a mesh.

    """
    evaluated = obj.evaluated_get(depsgraph)
    try:
        mesh = evaluated.to_mesh()
    except RuntimeError as exc:
        raise ValueError(f"Object '{obj.name}' does not evaluate to a mesh: {exc}") from exc
    try:
        matrix = evaluated.matrix_world
        points = [matrix @ vertex.co for vertex in mesh.vertices]
        polygons = [list(polygon.vertices) for polygon in mesh.polygons]
        edge_faces = Counter(key for polygon in mesh.polygons for key in polygon.edge_keys)
        closed = bool(polygons) and all(count == _FACES_PER_CLOSED_EDGE for count in edge_faces.values())
        return points, polygons, closed
    finally:
        evaluated.to_mesh_clear()


def _crossings(tree, origin, direction):
    count = 0
    for _hit in range(_MAX_RAY_HITS):
        location, _normal, _index, _distance = tree.ray_cast(origin, direction)
        if location is None:
            return count
        count += 1
        origin = location + direction * _RAY_STEP_M
    return count


def _penetration(points, against, directions):
    """
    Count the points inside a closed world-space surface and the deepest of them.

    Args:
        points: World positions of the measured mesh's evaluated vertices.
        against: `(tree, bounds)`: the surface's world-space BVH and its world bounds.
        directions: The parity ray directions, normalized.

    Returns:
        tuple[int, float]: Inside vertices, and the greatest distance from one of them to the
        surface - how far it would have to move to get out.

    """
    tree, (minimum, maximum) = against
    inside = 0
    deepest = 0.0
    for point in points:
        if any(point[axis] < minimum[axis] or point[axis] > maximum[axis] for axis in range(3)):
            continue
        _location, _normal, _index, distance = tree.find_nearest(point)
        if distance is None or distance <= _CONTACT_TOLERANCE_M:
            continue
        votes = sum(_crossings(tree, point, direction) % 2 for direction in directions)
        if votes * 2 > len(directions):
            inside += 1
            deepest = max(deepest, distance)
    return inside, deepest


def _against_surfaces(against_objects, depsgraph, open_surfaces):
    """
    Build each against mesh's world-space tree at the current frame.

    Args:
        against_objects: The against mesh objects.
        depsgraph: The depsgraph at the current frame.
        open_surfaces: A set collecting the names of against meshes found with open edges.

    Returns:
        dict: Object name to `(tree, (minimum, maximum))`, or to None for a mesh with no faces.

    """
    surfaces = {}
    for obj in against_objects:
        points, polygons, closed = _world_mesh(obj, depsgraph)
        if not closed:
            open_surfaces.add(obj.name)
        if not polygons:
            surfaces[obj.name] = None
            continue
        tree = mathutils.bvhtree.BVHTree.FromPolygons(points, polygons, all_triangles=False, epsilon=0.0)
        minimum = [min(point[axis] for point in points) - _CONTACT_TOLERANCE_M for axis in range(3)]
        maximum = [max(point[axis] for point in points) + _CONTACT_TOLERANCE_M for axis in range(3)]
        surfaces[obj.name] = (tree, (minimum, maximum))
    return surfaces


def _frame_sample(frame, bones, meshes, against_objects, open_surfaces):
    """
    Read every requested number at the frame the playhead is already on.

    Args:
        frame: The frame, for the record to state.
        bones: `(armature object, bone name)` pairs.
        meshes: The measured mesh objects.
        against_objects: The meshes each measured mesh is tested against.
        open_surfaces: A set collecting against meshes found with open edges.

    Returns:
        dict: The frame's record.

    """
    depsgraph = bpy.context.evaluated_depsgraph_get()
    record = {"frame": frame}
    if bones:
        record["bones"] = []
        for armature, bone_name in bones:
            head, tail = evaluated_bone_world_points(armature.evaluated_get(depsgraph), bone_name)
            record["bones"].append({"head_world": _rounded(head), "tail_world": _rounded(tail)})
    if meshes:
        surfaces = _against_surfaces(against_objects, depsgraph, open_surfaces)
        directions = [mathutils.Vector(direction).normalized() for direction in _PARITY_DIRECTIONS]
        record["meshes"] = []
        for obj in meshes:
            points, _polygons, _closed = _world_mesh(obj, depsgraph)
            entry = {"object_name": obj.name, "world_bounds": _bounds(points)}
            if against_objects:
                entry["penetration"] = []
                for against in against_objects:
                    if against is obj:
                        continue
                    surface = surfaces[against.name]
                    inside, deepest = (0, 0.0) if surface is None else _penetration(points, surface, directions)
                    entry["penetration"].append(
                        {
                            "against_object_name": against.name,
                            "inside_vertices": inside,
                            "max_depth_m": round(deepest, _DECIMALS),
                        }
                    )
            record["meshes"].append(entry)
    return record


class EvaluatedRangeHandlersMixin:
    """Read evaluated bone and mesh numbers across a bounded set of frames."""

    def sample_evaluated_range(
        self,
        frames=None,
        frame_start=None,
        frame_end=None,
        frame_step=1,
        bone_points=None,
        mesh_metrics=None,
        limit=_DEFAULT_FRAME_LIMIT,
        offset=0,
    ):
        """
        Evaluate the depsgraph at one page of the requested frames and report what moved where.

        Every name and every argument is checked before the playhead moves, so a refusal leaves
        the scene exactly as it was.

        Args:
            frames: Explicit frames to sample, in order.
            frame_start: First frame of a range, instead of frames.
            frame_end: Last frame of that range, inclusive.
            frame_step: Step through that range.
            bone_points: `{armature_object_name, bone_name}` records.
            mesh_metrics: `{object_names, against_object_names?}`.
            limit: Frames evaluated and returned by this call.
            offset: Index into the resolved frames of the first one this call evaluates.

        Returns:
            dict: The requested names, one page of per-frame samples, and the restored timeline.

        Raises:
            ValueError: On any refused argument, before the playhead moves.

        """
        sampled = _resolved_frames(frames, frame_start, frame_end, frame_step)
        bones = _resolved_bone_points(bone_points)
        meshes, against_objects = _resolved_mesh_metrics(mesh_metrics)
        if not bones and not meshes:
            raise ValueError("Name something to sample: bone_points, mesh_metrics, or both")
        _validate_limit_offset(limit, offset, MAX_SAMPLED_FRAMES)
        start, end, truncated, next_offset = paginate(len(sampled), offset, limit, MAX_SAMPLED_FRAMES)
        scene = bpy.context.scene
        original_frame = scene.frame_current
        original_subframe = scene.frame_subframe
        open_surfaces = set()
        items = []
        try:
            for frame in sampled[start:end]:
                scene.frame_set(frame)
                items.append(_frame_sample(frame, bones, meshes, against_objects, open_surfaces))
        finally:
            scene.frame_set(original_frame, subframe=original_subframe)
        reply = {
            "coordinate_space": "WORLD",
            "bone_points": [
                {"armature_object_name": armature.name, "bone_name": bone_name} for armature, bone_name in bones
            ],
            "samples": {
                "items": items,
                "total": len(sampled),
                "offset": start,
                "limit": limit,
                "truncated": truncated,
                "next_offset": next_offset,
            },
            "timeline_restored": {"frame": scene.frame_current, "subframe": scene.frame_subframe},
        }
        if against_objects:
            reply["inside_test"] = "RAY_PARITY"
        if open_surfaces:
            reply["warnings"] = [
                f"{', '.join(sorted(open_surfaces))} has open edges; inside_vertices and max_depth_m assume a "
                "closed surface and are not reliable against it."
            ]
        return reply
