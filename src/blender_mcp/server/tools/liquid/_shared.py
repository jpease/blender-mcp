"""Shared validation and serialization helpers for liquid tools."""

from pydantic import BaseModel, ConfigDict


class _StrictModel(BaseModel):
    """Reject unknown values in public liquid tool models."""

    model_config = ConfigDict(extra="forbid")


class _FiniteStrictModel(_StrictModel):
    """Also reject non-finite numeric values for cross-domain fluid models."""

    model_config = ConfigDict(allow_inf_nan=False)


def _dump(model: BaseModel | None) -> dict | None:
    """Serialize only values explicitly supplied to an optional patch model."""
    return model.model_dump(exclude_none=True, exclude_unset=True) if model is not None else None
