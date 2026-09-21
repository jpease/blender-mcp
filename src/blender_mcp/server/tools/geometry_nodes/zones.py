# ruff: file-ignore[multi-line-summary-second-line]
"""Bounded Repeat and Simulation Zone authoring tools."""

from typing import Annotated, Literal

from mcp.server.fastmcp import Context
from pydantic import Field

from ...app import mcp
from .._dispatch import call_blender
from ._shared import GeometryNodesRequest, model_records
from .authoring import GraphEdit

ZoneSocketType = Literal["GEOMETRY", "FLOAT", "INT", "BOOLEAN", "VECTOR", "ROTATION", "RGBA"]
AttributeDomain = Literal["POINT", "EDGE", "FACE", "CORNER", "CURVE", "INSTANCE"]

MAX_ZONE_ITEMS = 32
MAX_REPEAT_ITERATIONS = 256
MAX_ZONE_GRAPH_OPERATIONS = 200


class ZoneStateSpec(GeometryNodesRequest):
    """Describe one value carried between Repeat or Simulation Zone steps."""

    name: str = Field(min_length=1, max_length=128)
    socket_type: ZoneSocketType
    attribute_domain: AttributeDomain | None = None


def _validate_state_items(items: list[ZoneStateSpec]) -> None:
    """Reject ambiguous schemas before Blender copies or edits a node group."""
    if not 1 <= len(items) <= MAX_ZONE_ITEMS:
        raise ValueError(f"state_items must contain 1-{MAX_ZONE_ITEMS} entries")
    names = [item.name for item in items]
    if len(names) != len(set(names)):
        raise ValueError("state item names must be unique")


def _validate_graph_operations(operations: list[GraphEdit]) -> None:
    """Keep the zone's accompanying graph patch bounded."""
    if len(operations) > MAX_ZONE_GRAPH_OPERATIONS:
        raise ValueError(f"graph_operations is limited to {MAX_ZONE_GRAPH_OPERATIONS} edits")


@mcp.tool()
async def create_repeat_zone(
    ctx: Context,
    node_group_name: str,
    input_node_name: str = "Repeat Input",
    output_node_name: str = "Repeat Output",
    state_items: Annotated[list[ZoneStateSpec] | None, Field(max_length=MAX_ZONE_ITEMS)] = None,
    iterations: Annotated[int, Field(ge=1, le=MAX_REPEAT_ITERATIONS)] = 1,
    input_location: tuple[float, float] = (-240.0, 0.0),
    output_location: tuple[float, float] = (240.0, 0.0),
    graph_operations: Annotated[list[GraphEdit] | None, Field(max_length=MAX_ZONE_GRAPH_OPERATIONS)] = None,
) -> dict:
    """Add one paired, bounded Repeat Zone to an existing editable node group.

    The state schema defaults to one Geometry item. Each state output is connected through the
    zone as a safe pass-through; use ``graph_operations`` to connect existing nodes or replace the
    pass-through with iterative work. ``iterations`` is capped at 256 to prevent accidental graph
    explosions. Inspect the returned socket identifiers before later graph edits.
    """
    items = state_items or [ZoneStateSpec(name="Geometry", socket_type="GEOMETRY")]
    operations = graph_operations or []
    _validate_state_items(items)
    _validate_graph_operations(operations)
    if not 1 <= iterations <= MAX_REPEAT_ITERATIONS:
        raise ValueError(f"iterations must be in [1, {MAX_REPEAT_ITERATIONS}]")
    return await call_blender(
        "create_repeat_zone",
        {
            "node_group_name": node_group_name,
            "input_node_name": input_node_name,
            "output_node_name": output_node_name,
            "state_items": model_records(items),
            "iterations": iterations,
            "input_location": input_location,
            "output_location": output_location,
            "graph_operations": model_records(operations),
        },
        changed_resources=[node_group_name],
    )


@mcp.tool()
async def create_simulation_zone(
    ctx: Context,
    node_group_name: str,
    input_node_name: str = "Simulation Input",
    output_node_name: str = "Simulation Output",
    state_items: Annotated[list[ZoneStateSpec] | None, Field(max_length=MAX_ZONE_ITEMS)] = None,
    frame_start: int = 1,
    frame_end: int = 250,
    time_step_mode: Literal["SCENE_DELTA_TIME"] = "SCENE_DELTA_TIME",
    skip_simulation: bool = False,
    input_location: tuple[float, float] = (-240.0, 0.0),
    output_location: tuple[float, float] = (240.0, 0.0),
    graph_operations: Annotated[list[GraphEdit] | None, Field(max_length=MAX_ZONE_GRAPH_OPERATIONS)] = None,
) -> dict:
    """Add one paired Simulation Zone without baking or changing the scene frame range.

    The state schema defaults to Geometry and is wired as a pass-through. Blender supplies the
    zone's Delta Time from scene evaluation; ``frame_start`` and ``frame_end`` record the intended
    cache/evaluation range in MCP metadata but do not alter timeline settings. Use
    ``graph_operations`` for explicit initial-state and internal feedback connections.
    """
    items = state_items or [ZoneStateSpec(name="Geometry", socket_type="GEOMETRY")]
    operations = graph_operations or []
    _validate_state_items(items)
    _validate_graph_operations(operations)
    if frame_start > frame_end:
        raise ValueError("frame_start must not exceed frame_end")
    return await call_blender(
        "create_simulation_zone",
        {
            "node_group_name": node_group_name,
            "input_node_name": input_node_name,
            "output_node_name": output_node_name,
            "state_items": model_records(items),
            "frame_start": frame_start,
            "frame_end": frame_end,
            "time_step_mode": time_step_mode,
            "skip_simulation": skip_simulation,
            "input_location": input_location,
            "output_location": output_location,
            "graph_operations": model_records(operations),
        },
        changed_resources=[node_group_name],
    )
