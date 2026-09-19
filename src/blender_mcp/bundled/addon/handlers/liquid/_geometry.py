"""Shared geometry constants and primitives for liquid handlers."""

from collections.abc import Sequence

_RIM_AXES = {
    "X": (0, 1.0),
    "Y": (1, 1.0),
    "Z": (2, 1.0),
    "NEGATIVE_X": (0, -1.0),
    "NEGATIVE_Y": (1, -1.0),
    "NEGATIVE_Z": (2, -1.0),
}


def _cube_geometry(
    dimensions: Sequence[float],
) -> tuple[list[tuple[float, float, float]], list[tuple[int, int, int, int]]]:
    """Return centered cube vertices and faces for the requested dimensions."""
    half = [value * 0.5 for value in dimensions]
    vertices = [
        (-half[0], -half[1], -half[2]),
        (half[0], -half[1], -half[2]),
        (half[0], half[1], -half[2]),
        (-half[0], half[1], -half[2]),
        (-half[0], -half[1], half[2]),
        (half[0], -half[1], half[2]),
        (half[0], half[1], half[2]),
        (-half[0], half[1], half[2]),
    ]
    faces = [(0, 1, 2, 3), (4, 7, 6, 5), (0, 4, 5, 1), (1, 5, 6, 2), (2, 6, 7, 3), (4, 0, 3, 7)]
    return vertices, faces
