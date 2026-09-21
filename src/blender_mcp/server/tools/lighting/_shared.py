"""Shared validation models and public lighting enums; dispatch lives in `tools/_dispatch.py`."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

TargetEngine = Literal["BOTH", "CYCLES", "EEVEE"]
LightType = Literal["POINT", "SPOT", "AREA", "SUN"]
StudioLightingMood = Literal["SOFT", "HIGH_CONTRAST", "BEAUTY"]


class StrictLightingInput(BaseModel):
    """Reject unknown fields and non-finite numbers at the MCP boundary."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


def dump_input(model: BaseModel | None) -> dict | None:
    """Serialize only fields an agent explicitly supplied."""
    return model.model_dump(exclude_none=True, exclude_unset=True) if model is not None else None
