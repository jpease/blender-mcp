"""
`pick_rays`: the frame-point-to-ray maths behind `pick_from_camera`, without Blender.

The frame corners are what `Camera.view_frame(scene=...)` returned on Blender 5.2.2 for a
1920x1080 render (a 50 mm lens on a 36 mm sensor, then with `shift_x = 0.2`, then an ortho
camera at `ortho_scale = 4`), so shift, sensor fit and aspect reach these tests the way they
reach the handler: already folded into the corners.
"""

from __future__ import annotations

import math
import types

import pytest

from conftest import load_addon_source_module

# The camera every test below looks through: at (0, -5, 1), turned 90 degrees about X, so it
# looks along +Y with +Z up - the default "front view" of a scene.
_EYE = (0.0, -5.0, 1.0)
_LOOK_ALONG_Y = (
    (1.0, 0.0, 0.0, _EYE[0]),
    (0.0, 0.0, -1.0, _EYE[1]),
    (0.0, 1.0, 0.0, _EYE[2]),
    (0.0, 0.0, 0.0, 1.0),
)
_PERSPECTIVE_CORNERS = (
    (0.5, 0.28125, -1.3888889),
    (0.5, -0.28125, -1.3888889),
    (-0.5, -0.28125, -1.3888889),
    (-0.5, 0.28125, -1.3888889),
)
_SHIFTED_CORNERS = (
    (0.7, 0.28125, -1.3888889),
    (0.7, -0.28125, -1.3888889),
    (-0.3, -0.28125, -1.3888889),
    (-0.3, 0.28125, -1.3888889),
)
_ORTHO_CORNERS = ((2.0, 1.125, -1.0), (2.0, -1.125, -1.0), (-2.0, -1.125, -1.0), (-2.0, 1.125, -1.0))


def _rays() -> types.ModuleType:
    """
    Load the `bpy`-free pick_rays module straight from the addon source.

    Returns:
        types.ModuleType: `pick_rays`.

    """
    return load_addon_source_module("pick_rays.py", "addon_pick_rays_under_test")


def _unit(vector: tuple[float, float, float]) -> tuple[float, float, float]:
    length = math.sqrt(sum(component * component for component in vector))
    return (vector[0] / length, vector[1] / length, vector[2] / length)


def _close(actual, expected, tolerance: float = 1e-9) -> bool:
    return all(math.isclose(a, e, abs_tol=tolerance) for a, e in zip(actual, expected, strict=True))


def _ray(corners, u, v, *, perspective=True, matrix=_LOOK_ALONG_Y, clip_start=0.1, clip_end=100.0):
    rays = _rays()
    bounds = rays.frame_bounds(corners, perspective=perspective)
    return rays.camera_ray(matrix, bounds, u, v, clip_start=clip_start, clip_end=clip_end)


def test_the_frame_centre_ray_leaves_the_eye_along_the_view_axis() -> None:
    ray = _ray(_PERSPECTIVE_CORNERS, 0.5, 0.5)

    assert _close(ray.origin, _EYE)
    assert _close(ray.direction, (0.0, 1.0, 0.0))


@pytest.mark.parametrize("order", [(0, 1, 2, 3), (2, 0, 3, 1)])
def test_the_frame_corners_map_to_the_view_frame_corners_whatever_their_order(order) -> None:
    """(0, 0) is the frame's top-left, (1, 1) its bottom-right, as in an image: up is +Z here."""
    corners = tuple(_PERSPECTIVE_CORNERS[index] for index in order)
    half_width = 0.5 / 1.3888889
    half_height = 0.28125 / 1.3888889

    top_left = _ray(corners, 0.0, 0.0)
    bottom_right = _ray(corners, 1.0, 1.0)

    assert _close(top_left.direction, _unit((-half_width, 1.0, half_height)))
    assert _close(bottom_right.direction, _unit((half_width, 1.0, -half_height)))


def test_a_shifted_frame_moves_the_centre_ray_off_the_view_axis() -> None:
    """The frame centre of a shift_x = 0.2 camera is 0.2 of the frame width right of the axis."""
    ray = _ray(_SHIFTED_CORNERS, 0.5, 0.5)

    assert _close(ray.direction, _unit((0.2 / 1.3888889, 1.0, 0.0)))


def test_an_ortho_camera_casts_parallel_rays_from_across_the_frame() -> None:
    top_left = _ray(_ORTHO_CORNERS, 0.0, 0.0, perspective=False)
    bottom_right = _ray(_ORTHO_CORNERS, 1.0, 1.0, perspective=False)

    assert _close(top_left.origin, (-2.0, -5.0, 1.0 + 1.125))
    assert _close(bottom_right.origin, (2.0, -5.0, 1.0 - 1.125))
    assert _close(top_left.direction, (0.0, 1.0, 0.0))
    assert _close(bottom_right.direction, (0.0, 1.0, 0.0))


@pytest.mark.parametrize("perspective", [True, False])
def test_the_camera_objects_scale_does_not_change_the_ray(perspective) -> None:
    """A render ignores the camera object's scale, so the ray must too."""
    corners = _PERSPECTIVE_CORNERS if perspective else _ORTHO_CORNERS
    scaled = tuple(
        tuple(value * (3.0 if column < 3 and row < 3 else 1.0) for column, value in enumerate(values))
        for row, values in enumerate(_LOOK_ALONG_Y)
    )

    plain = _ray(corners, 0.2, 0.9, perspective=perspective)
    stretched = _ray(corners, 0.2, 0.9, perspective=perspective, matrix=scaled)

    assert _close(stretched.origin, plain.origin)
    assert _close(stretched.direction, plain.direction)


def test_clip_start_and_clip_end_bound_the_ray_by_depth_not_by_length() -> None:
    """Clipping is a depth along the view axis, so an off-axis ray's clip distances are longer."""
    centre = _ray(_PERSPECTIVE_CORNERS, 0.5, 0.5, clip_start=0.5, clip_end=40.0)
    corner = _ray(_PERSPECTIVE_CORNERS, 1.0, 1.0, clip_start=0.5, clip_end=40.0)
    stretch = math.sqrt((0.5 / 1.3888889) ** 2 + (0.28125 / 1.3888889) ** 2 + 1.0)

    assert (centre.near, centre.far) == pytest.approx((0.5, 40.0))
    assert (corner.near, corner.far) == pytest.approx((0.5 * stretch, 40.0 * stretch))


def test_an_ortho_ray_is_clipped_along_its_own_length() -> None:
    ray = _ray(_ORTHO_CORNERS, 0.3, 0.7, perspective=False, clip_start=0.5, clip_end=40.0)

    assert (ray.near, ray.far) == pytest.approx((0.5, 40.0))


def test_a_ray_falling_toward_the_ground_meets_it_at_z_zero() -> None:
    point, distance = _rays().ground_plane_point((0.0, -5.0, 1.0), _unit((0.0, 1.0, -1.0)))

    assert _close(point, (0.0, -4.0, 0.0))
    assert distance == pytest.approx(math.sqrt(2.0))


@pytest.mark.parametrize("direction", [(0.0, 1.0, 0.0), _unit((0.0, 1.0, 1.0))])
def test_a_ray_level_with_or_rising_from_the_ground_never_meets_it(direction) -> None:
    assert _rays().ground_plane_point((0.0, -5.0, 1.0), direction) is None


def test_region_samples_are_the_cell_centres_of_a_bounded_grid() -> None:
    rays = _rays()
    samples = rays.region_samples(0.2, 0.4, 0.6, 0.8)

    assert len(samples) == rays.REGION_GRID**2 == 256
    assert samples[0] == pytest.approx((0.2 + 0.4 / 32, 0.4 + 0.4 / 32))
    assert samples[-1] == pytest.approx((0.6 - 0.4 / 32, 0.8 - 0.4 / 32))
    assert all(0.2 < u < 0.6 and 0.4 < v < 0.8 for u, v in samples)


def _grid_owners(fractions: dict[str, float], total: int = 100) -> list[str | None]:
    owners: list[str | None] = []
    for name, fraction in fractions.items():
        owners.extend([name] * round(fraction * total))
    return owners + [None] * (total - len(owners))


def test_region_ranking_orders_objects_by_coverage_and_drops_the_slivers() -> None:
    owners = _grid_owners({"Small": 0.25, "Big": 0.5, "Sliver": 0.03})
    samples = [(index / 100, 0.5) for index in range(100)]

    ranking = _rays().rank_region(owners, samples)

    assert [(entry.object_name, entry.coverage) for entry in ranking.objects] == [("Big", 0.5), ("Small", 0.25)]
    assert ranking.dropped_object_count == 1
    assert ranking.background_fraction == pytest.approx(0.22)


def test_region_ranking_keeps_at_most_six_objects() -> None:
    owners = _grid_owners({f"Obj{index}": 0.1 for index in range(8)})
    samples = [(index / 100, 0.5) for index in range(100)]

    ranking = _rays().rank_region(owners, samples)

    assert len(ranking.objects) == 6
    assert ranking.dropped_object_count == 2


def test_the_centroid_hit_is_the_objects_own_sample_nearest_its_mean_position() -> None:
    """
    A ring's mean lies in its hole; the reported sample must still be one the object covers.

    Four edge midpoints tie for nearest, and the earliest sample wins, so the answer is stable.
    """
    samples = [(u / 4, v / 4) for v in range(5) for u in range(5)]
    owners = ["Ring" if u in {0, 4} or v in {0, 4} else None for v in range(5) for u in range(5)]

    ranking = _rays().rank_region(owners, samples)
    (ring,) = ranking.objects

    assert owners[ring.sample_index] == "Ring"
    assert samples[ring.sample_index] == (0.5, 0.0)
