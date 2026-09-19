# ruff: file-ignore[too-many-statements-in-try-clause]
"""Shared validation, serialization, and transport helpers for liquid tools."""

import logging
import sys

from mcp.server.fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict

from ...connection import get_blender_connection
from ..envelope import ok

logger = logging.getLogger("BlenderMCPServer")


class _StrictModel(BaseModel):
    """Reject unknown values in public liquid tool models."""

    model_config = ConfigDict(extra="forbid")


class _FiniteStrictModel(_StrictModel):
    """Also reject non-finite numeric values for cross-domain fluid models."""

    model_config = ConfigDict(allow_inf_nan=False)


def _dump(model: BaseModel | None) -> dict | None:
    """Serialize only values explicitly supplied to an optional patch model."""
    return model.model_dump(exclude_none=True, exclude_unset=True) if model is not None else None


def _connection_call(command: str, params: dict, changed_objects: list[str] | None = None) -> dict:
    """Send one Blender command and normalize its response envelope."""
    try:
        result = get_blender_connection().send_command(command, params)
        changed = result.get("changed_objects", changed_objects or []) if isinstance(result, dict) else changed_objects
        resources = result.get("changed_resources", []) if isinstance(result, dict) else []
        warnings = result.get("warnings", []) if isinstance(result, dict) else []
        if isinstance(result, dict):
            result = {
                key: value
                for key, value in result.items()
                if key not in {"changed_objects", "changed_resources", "warnings"}
            }
        envelope = ok(result, changed_objects=changed or [], changed_resources=resources)
        envelope["warnings"] = warnings
        return envelope
    except Exception as exc:
        logger.error("Error running %s: %s", command, exc)
        raise ToolError(f"Error running {command}: {exc}") from exc


def _call(command: str, params: dict, changed_objects: list[str] | None = None) -> dict:
    """Dispatch through the package hook so tests and embedders can replace the transport."""
    package = sys.modules.get(__package__) if __package__ is not None else None
    override = getattr(package, "_call", None) if package is not None else None
    if override is not None and override is not _call:
        return override(command, params, changed_objects)
    return _connection_call(command, params, changed_objects)
