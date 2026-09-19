"""Shared validation, serialization, and transport helpers for cloth tools."""

import logging
import sys

from mcp.server.fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict

from ...connection import get_blender_connection
from ..envelope import envelope_for

logger = logging.getLogger("BlenderMCPServer")


class _StrictModel(BaseModel):
    """Reject unknown values in public cloth tool models."""

    model_config = ConfigDict(extra="forbid")


def _dump(model: BaseModel | None) -> dict | None:
    """Serialize only values explicitly supplied to an optional patch model."""
    return model.model_dump(exclude_none=True, exclude_unset=True) if model is not None else None


def _connection_call(command: str, params: dict, changed_objects: list[str] | None = None) -> dict:
    """Send one Blender command and normalize its response envelope."""
    try:
        result = get_blender_connection().send_command(command, params)
    except Exception as exc:
        logger.error("Error running %s: %s", command, exc)
        raise ToolError(f"Error running {command}: {exc}") from exc
    return envelope_for(result, changed_objects=changed_objects or ())


def _call(command: str, params: dict, changed_objects: list[str] | None = None) -> dict:
    """Dispatch through the package hook so tests and embedders can replace the transport."""
    package = sys.modules.get(__package__) if __package__ is not None else None
    override = getattr(package, "_call", None) if package is not None else None
    if override is not None and override is not _call:
        return override(command, params, changed_objects)
    return _connection_call(command, params, changed_objects)
