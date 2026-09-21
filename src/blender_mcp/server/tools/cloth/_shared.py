"""Shared validation and serialization helpers for cloth tools."""

from pydantic import BaseModel, ConfigDict


class _StrictModel(BaseModel):
    """Reject unknown values in public cloth tool models."""

    model_config = ConfigDict(extra="forbid")


def _dump(model: BaseModel | None) -> dict | None:
    """Serialize only values explicitly supplied to an optional patch model."""
    return model.model_dump(exclude_none=True, exclude_unset=True) if model is not None else None
