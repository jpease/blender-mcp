"""Shared transport, validation models, and public lighting enums."""

import logging

from typing import Literal

from mcp.server.fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict

from ...connection import get_blender_connection
from ..envelope import envelope_for

logger = logging.getLogger("BlenderMCPServer")

TargetEngine = Literal["BOTH", "CYCLES", "EEVEE"]
LightType = Literal["POINT", "SPOT", "AREA", "SUN"]
StudioLightingMood = Literal["SOFT", "HIGH_CONTRAST", "BEAUTY"]


class StrictLightingInput(BaseModel):
    """Reject unknown fields and non-finite numbers at the MCP boundary."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


def dump_input(model: BaseModel | None) -> dict | None:
    """Serialize only fields an agent explicitly supplied."""
    return model.model_dump(exclude_none=True, exclude_unset=True) if model is not None else None


def call_blender(command: str, params: dict, changed_objects: list[str] | None = None) -> dict:
    """Send one lighting command and normalize its production result envelope."""
    try:
        result = get_blender_connection().send_command(command, params)
    except Exception as exc:
        logger.error("Error running %s: %s", command, exc)
        raise ToolError(f"Error running {command}: {exc}") from exc
    return envelope_for(result, changed_objects=changed_objects or ())
