"""Shared validation primitives for Geometry Nodes tools."""

import math

from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator


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
