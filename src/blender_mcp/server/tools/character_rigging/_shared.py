"""Shared plumbing for the character rigging tool package; registers no tools."""

import logging
import sys

from mcp.server.fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict

from ...connection import get_blender_connection
from ..envelope import ok

logger = logging.getLogger("BlenderMCPServer")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


def _call(command: str, params: dict, changed_objects: list[str] | None = None) -> dict:
    # Defer to a package-level `_call` when one has been swapped in, so tests can patch the package.
    package = sys.modules.get(__package__) if __package__ is not None else None
    package_call = getattr(package, "_call", None) if package is not None else None
    if package_call is not None and package_call is not _call:
        return package_call(command, params, changed_objects)
    try:
        result = get_blender_connection().send_command(command, params)
        changed = result.get("changed_objects", changed_objects or []) if isinstance(result, dict) else changed_objects
        resources = result.get("changed_resources", []) if isinstance(result, dict) else []
        if isinstance(result, dict):
            result = {
                key: value for key, value in result.items() if key not in {"changed_objects", "changed_resources"}
            }
        return ok(result, changed_objects=changed or [], changed_resources=resources)
    except Exception as exc:
        logger.error("Error running %s: %s", command, exc)
        raise ToolError(f"Error running {command}: {exc}") from exc
