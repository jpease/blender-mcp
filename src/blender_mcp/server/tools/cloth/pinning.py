"""Typed tools for cloth vertex weights and pin-goal behavior."""

import asyncio

from typing import Annotated, Literal

from mcp.server.fastmcp import Context
from pydantic import Field

from ...app import mcp
from ._shared import _call, _StrictModel

WeightOperation = Literal["REPLACE", "ADD", "SUBTRACT"]
WeightRole = Literal[
    "PIN_MASS",
    "STRUCTURAL_STIFFNESS",
    "SHEAR_STIFFNESS",
    "BENDING_STIFFNESS",
    "SHRINK",
    "PRESSURE",
    "INTERNAL_SPRINGS",
    "OBJECT_COLLISION_EXCLUSION",
    "SELF_COLLISION_EXCLUSION",
]


class VertexWeightAssignment(_StrictModel):
    """Assign one exact base-mesh vertex index a normalized weight."""

    vertex_index: int = Field(ge=0)
    weight: float = Field(ge=0.0, le=1.0)


class ClothPinningPatch(_StrictModel):
    """Pin goal controls applied to an existing vertex group."""

    pin_stiffness: Annotated[float, Field(ge=0)] | None = None
    goal_min: Annotated[float, Field(ge=0, le=1)] | None = None
    goal_max: Annotated[float, Field(ge=0, le=1)] | None = None
    goal_default: Annotated[float, Field(ge=0, le=1)] | None = None
    goal_spring: Annotated[float, Field(ge=0, le=1)] | None = None
    goal_friction: Annotated[float, Field(ge=0)] | None = None


@mcp.tool()
async def set_cloth_vertex_weights(
    ctx: Context,
    object_name: str,
    modifier_name: str,
    role: WeightRole,
    group_name: str,
    assignments: Annotated[list[VertexWeightAssignment], Field(min_length=1)],
    operation: WeightOperation = "REPLACE",
) -> dict:
    """
    Create or update one role-specific cloth vertex group using exact base-mesh indices.

    The complete batch is validated before editing. ADD and SUBTRACT clamp to [0, 1]; unrelated and
    locked groups are preserved. Query mesh indices again after any topology-changing operation.
    """
    return await asyncio.to_thread(
        _call,
        "set_cloth_vertex_weights",
        {
            "object_name": object_name,
            "modifier_name": modifier_name,
            "role": role,
            "group_name": group_name,
            "assignments": [item.model_dump() for item in assignments],
            "operation": operation,
        },
        [object_name],
    )
