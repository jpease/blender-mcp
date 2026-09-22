"""Shared types and path validation for texturing tools; dispatch lives in `tools/_dispatch.py`."""

from pathlib import Path
from typing import Literal

from mcp.server.fastmcp.exceptions import ToolError

TargetEngine = Literal["BOTH", "CYCLES", "EEVEE", "BLENDER_EEVEE_NEXT"]


def absolute_path(value: str, label: str) -> str:
    """Require an explicit absolute filesystem path."""
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ToolError(f"{label} must be an absolute path")
    return str(path)
