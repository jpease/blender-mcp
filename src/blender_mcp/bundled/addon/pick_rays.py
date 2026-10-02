"""
Frame-point-to-ray maths for `pick_from_camera`, with no `bpy` so it is unit-tested without Blender.

A frame point is (u, v) over the camera's render frame: (0, 0) its bottom-left corner, (1, 1) its
top-right. The frame itself comes from `Camera.view_frame(scene=...)`, which already folds in lens
shift, sensor fit and the render's aspect ratio, so nothing here re-derives any of them: it only
interpolates between the corners Blender returned and carries the result into world space.
"""

import math

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

Vec3 = tuple[float, float, float]

# A region is sampled on a REGION_GRID x REGION_GRID grid of cell centres: 256 rays, enough to rank
# what fills a region by area without a call costing more than a few milliseconds.
REGION_GRID = 16
# How many of a region's objects a reply ranks, and the share of the region below which an object
# is a sliver rather than something the region shows.
REGION_TOP_OBJECTS = 6
REGION_MIN_COVERAGE = 0.04
# A unit direction whose vertical part is at most this runs level with the ground plane: the
# plane is met, if at all, a trillion ray lengths away, which is no point to report.
LEVEL_EPSILON = 1e-12


@dataclass(frozen=True, slots=True)
class FrameBounds:
    """
    The render frame in camera space: x and y extents, and how a frame point becomes a ray.

    Attributes:
        x_min: Left edge.
        x_max: Right edge.
        y_min: Bottom edge.
        y_max: Top edge.
        perspective: True when the extents are slopes on the plane one unit in front of the eye;
            False for an orthographic frame, whose extents are camera-space lengths.

    """

    x_min: float
    x_max: float
    y_min: float
    y_max: float
    perspective: bool


@dataclass(frozen=True, slots=True)
class CameraRay:
    """
    One ray through a frame point, in world space.

    Attributes:
        origin: Where distances are measured from: the eye, or the frame point on an
            orthographic camera's own plane.
        direction: Unit direction into the scene.
        near: Distance along the ray at which the camera's clip_start depth is reached.
        far: Distance along the ray at which its clip_end depth is reached.

    """

    origin: Vec3
    direction: Vec3
    near: float
    far: float


@dataclass(frozen=True, slots=True)
class RankedObject:
    """
    One object a region shows.

    Attributes:
        object_name: The object.
        coverage: Its share of the region's samples.
        sample_index: The index of its own sample nearest the mean frame position of all of
            them, so the hit reported for it lies on it even when that mean does not.

    """

    object_name: str
    coverage: float
    sample_index: int


@dataclass(frozen=True, slots=True)
class RegionRanking:
    """
    What fills a region, ranked by area.

    Attributes:
        objects: At most REGION_TOP_OBJECTS objects covering at least REGION_MIN_COVERAGE each,
            largest first, ties by name.
        dropped_object_count: Objects hit but not listed.
        background_fraction: Share of samples that hit no object.

    """

    objects: list[RankedObject]
    dropped_object_count: int
    background_fraction: float


def frame_bounds(corners: Sequence[Sequence[float]], *, perspective: bool) -> FrameBounds:
    """
    Read the frame's extents off `view_frame`'s four camera-space corners, in any order.

    Args:
        corners: The corners `Camera.view_frame(scene=...)` returned.
        perspective: Whether the camera is perspective; a perspective corner is divided by its
            depth so the extents describe the plane one unit in front of the eye.

    Returns:
        FrameBounds: The extents.

    """
    if perspective:
        xs = [corner[0] / -corner[2] for corner in corners]
        ys = [corner[1] / -corner[2] for corner in corners]
    else:
        xs = [corner[0] for corner in corners]
        ys = [corner[1] for corner in corners]
    return FrameBounds(min(xs), max(xs), min(ys), max(ys), perspective)


def _axes(matrix_world: Sequence[Sequence[float]]) -> tuple[Vec3, Vec3, Vec3, Vec3]:
    """
    Split a camera's world matrix into its unit X, Y and Z axes and its location.

    The axes are normalised because a render ignores the camera object's scale.
    """
    axes = []
    for column in range(3):
        axis = (matrix_world[0][column], matrix_world[1][column], matrix_world[2][column])
        length = math.sqrt(sum(component * component for component in axis))
        axes.append((axis[0] / length, axis[1] / length, axis[2] / length))
    location = (matrix_world[0][3], matrix_world[1][3], matrix_world[2][3])
    return axes[0], axes[1], axes[2], location


def _combine(base: Vec3, terms: Sequence[tuple[float, Vec3]]) -> Vec3:
    x, y, z = base
    for scale, axis in terms:
        x += scale * axis[0]
        y += scale * axis[1]
        z += scale * axis[2]
    return (x, y, z)


def camera_ray(
    matrix_world: Sequence[Sequence[float]],
    bounds: FrameBounds,
    u: float,
    v: float,
    *,
    clip_start: float,
    clip_end: float,
) -> CameraRay:
    """
    Cast the ray through one frame point.

    Args:
        matrix_world: The camera's evaluated 4x4 world matrix, as rows.
        bounds: The frame, from `frame_bounds`.
        u: Horizontal frame position, 0 at the left edge.
        v: Vertical frame position, 0 at the bottom edge.
        clip_start: The camera's near clip depth.
        clip_end: The camera's far clip depth.

    Returns:
        CameraRay: The ray: from the eye through the frame point for a perspective camera,
        parallel to the view axis from the frame point for an orthographic one.

    """
    x_axis, y_axis, z_axis, location = _axes(matrix_world)
    x = bounds.x_min + u * (bounds.x_max - bounds.x_min)
    y = bounds.y_min + v * (bounds.y_max - bounds.y_min)
    if not bounds.perspective:
        origin = _combine(location, ((x, x_axis), (y, y_axis)))
        direction = (-z_axis[0], -z_axis[1], -z_axis[2])
        return CameraRay(origin, direction, clip_start, clip_end)
    # One unit of depth along this ray is `stretch` units of length, so a clip depth is that
    # much further away along an off-axis ray than along the view axis.
    stretch = math.sqrt(x * x + y * y + 1.0)
    direction = _combine((0.0, 0.0, 0.0), ((x / stretch, x_axis), (y / stretch, y_axis), (-1.0 / stretch, z_axis)))
    return CameraRay(location, direction, clip_start * stretch, clip_end * stretch)


def ground_plane_point(origin: Vec3, direction: Vec3) -> tuple[Vec3, float] | None:
    """
    Meet the world's z = 0 plane, the fallback for a ray that hits nothing.

    Args:
        origin: The ray's origin.
        direction: Its unit direction.

    Returns:
        tuple[Vec3, float] | None: The point and its distance along the ray, or None when the
        ray runs level with the plane or away from it.

    """
    if abs(direction[2]) <= LEVEL_EPSILON:
        return None
    distance = -origin[2] / direction[2]
    if distance <= 0.0:
        return None
    return _combine(origin, ((distance, direction),)), distance


def region_samples(
    u_min: float, v_min: float, u_max: float, v_max: float, grid: int = REGION_GRID
) -> list[tuple[float, float]]:
    """
    List the cell centres of a grid laid over a frame region, row by row from the bottom-left.

    Args:
        u_min: Left edge of the region.
        v_min: Bottom edge.
        u_max: Right edge.
        v_max: Top edge.
        grid: Cells along each side.

    Returns:
        list[tuple[float, float]]: grid * grid (u, v) points.

    """
    step_u = (u_max - u_min) / grid
    step_v = (v_max - v_min) / grid
    return [
        (u_min + (column + 0.5) * step_u, v_min + (row + 0.5) * step_v) for row in range(grid) for column in range(grid)
    ]


def rank_region(owners: Sequence[str | None], samples: Sequence[tuple[float, float]]) -> RegionRanking:
    """
    Rank the objects a region's samples hit by the share of samples each one owns.

    Args:
        owners: The object each sample hit, or None for a sample that hit no object.
        samples: The samples' (u, v) positions, index for index with owners.

    Returns:
        RegionRanking: The ranking.

    """
    total = len(owners)
    counts = Counter(owner for owner in owners if owner is not None)
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    kept = [(name, count) for name, count in ordered if count / total >= REGION_MIN_COVERAGE][:REGION_TOP_OBJECTS]
    ranked = []
    for name, count in kept:
        indices = [index for index, owner in enumerate(owners) if owner == name]
        mean_u = sum(samples[index][0] for index in indices) / count
        mean_v = sum(samples[index][1] for index in indices) / count
        nearest = min(indices, key=lambda index: (samples[index][0] - mean_u) ** 2 + (samples[index][1] - mean_v) ** 2)
        ranked.append(RankedObject(name, count / total, nearest))
    background = sum(1 for owner in owners if owner is None) / total
    return RegionRanking(ranked, len(counts) - len(ranked), background)
