"""
`pick_from_camera`'s server half: the region it accepts, and that it is advertised read-only.

What a pick returns is the add-on's work and is proven against real Blender by
`tests/blender_pick_smoke.py`; the frame-point maths by `tests/test_pick_rays.py`.
"""

import asyncio

import pytest

from pydantic import ValidationError

from blender_mcp.server.app import mcp
from blender_mcp.server.tools.viewport import PickRegion


@pytest.mark.parametrize(
    "bounds",
    [
        {"u_min": 0.5, "v_min": 0.1, "u_max": 0.5, "v_max": 0.9},
        {"u_min": 0.1, "v_min": 0.9, "u_max": 0.9, "v_max": 0.2},
    ],
    ids=["zero-width", "inverted-height"],
)
def test_a_region_must_span_a_positive_area_of_the_frame(bounds) -> None:
    """An empty or inverted region has no samples to rank; it is refused before any round trip."""
    with pytest.raises(ValidationError, match=r"u_min < u_max and v_min < v_max"):
        PickRegion(**bounds)


def test_a_region_stays_inside_the_frame() -> None:
    with pytest.raises(ValidationError):
        PickRegion(u_min=-0.1, v_min=0.0, u_max=0.5, v_max=0.5)


def test_pick_from_camera_is_advertised_read_only() -> None:
    """It moves the playhead only inside the call and puts it back, so a client may run it unasked."""
    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
    annotations = tools["pick_from_camera"].annotations

    assert annotations is not None
    assert annotations.readOnlyHint is True
    assert "[read-only]" in (tools["pick_from_camera"].description or "")
