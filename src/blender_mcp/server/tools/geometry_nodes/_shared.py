"""Shared transport and validation primitives for Geometry Nodes tools."""

import logging
import math

from collections.abc import Sequence
from typing import Any

from mcp.server.fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, model_validator

from ...connection import get_blender_connection
from ..envelope import envelope_for

logger = logging.getLogger("BlenderMCPServer")


class GeometryNodesRequest(BaseModel):
    """Reject unknown and non-finite values in Geometry Nodes request records."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    @model_validator(mode="after")
    def reject_nested_nonfinite_values(self) -> "GeometryNodesRequest":
        """Reject NaN and infinities nested inside open JSON-shaped fields."""

        def validate(value: Any) -> None:
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("numeric values must be finite")
            if isinstance(value, dict):
                for nested in value.values():
                    validate(nested)
            elif isinstance(value, (list, tuple)):
                for nested in value:
                    validate(nested)

        validate(self.model_dump())
        return self


def model_records(items: Sequence[BaseModel]) -> list[dict[str, Any]]:
    """Convert validated request records into JSON-serializable dictionaries."""
    return [item.model_dump(exclude_none=True) for item in items]


def call_geometry_nodes(
    command: str,
    params: dict[str, Any],
    *,
    changed_objects: list[str] | None = None,
    changed_resources: list[str] | None = None,
) -> dict[str, Any]:
    """Send one validated Geometry Nodes command to Blender."""
    try:
        result = get_blender_connection().send_command(command, params)
    except Exception as exc:
        logger.error("Error running %s: %s", command, exc)
        raise ToolError(f"Error running {command}: {exc}") from exc
    return envelope_for(
        result,
        changed_objects=changed_objects or (),
        changed_resources=changed_resources or (),
    )
