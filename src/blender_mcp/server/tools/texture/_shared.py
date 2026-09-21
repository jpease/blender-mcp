"""Shared types and validation for texturing tools; dispatch lives in `tools/_dispatch.py`."""

from pathlib import Path
from typing import Literal

from mcp.server.fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict

TargetEngine = Literal["BOTH", "CYCLES", "EEVEE", "BLENDER_EEVEE_NEXT"]


class StrictTextureInput(BaseModel):
    """Reject unknown fields and non-finite numeric input at the MCP boundary."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


def explicit_fields(model: BaseModel | None) -> dict:
    """Serialize only fields the caller supplied."""
    return model.model_dump(exclude_none=True, exclude_unset=True) if model else {}


def absolute_path(value: str, label: str) -> str:
    """Require an explicit absolute filesystem path."""
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ToolError(f"{label} must be an absolute path")
    return str(path)
