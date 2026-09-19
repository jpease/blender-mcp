"""
Shared plumbing and cross-file types for the scene tool modules.

`scene.py` and `scene_authoring.py` belong to different bundles. If either imported the
other, a process selecting one would also register the other's tools.

Unlike the packages' `_shared.py` modules, `_call` here neither logs failures nor
prefixes them; FastMCP still turns them into a `ToolError`.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict

from ..connection import get_blender_connection
from .envelope import envelope_for


class _StrictModel(BaseModel):
    """
    Base for scene tool inputs: rejects unknown fields and non-finite floats.

    `allow_inf_nan=False` does not appear in the JSON schema, so clients meet it only as a
    validation error.
    """

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


def _call(
    command: str,
    params: dict[str, Any],
    changed_objects: list[str] | None = None,
    *,
    warnings: list[str] | None = None,
) -> dict:
    """
    Send one command to Blender and wrap the reply in the standard response envelope.

    Args:
        command: Add-on command name to dispatch.
        params: JSON-serializable parameters for that command.
        changed_objects: Object names to report as changed when the add-on does not say.
            An add-on `changed_objects` key replaces this rather than extending it.
        warnings: Notices the tool knows before the call - a destructive action's
            stale-index warning - which must be in the reply while it is measured against
            the byte budget.

    Returns:
        The `ok()` envelope, with `changed_objects` and `changed_resources` moved out of
        `data` into envelope fields.

    """
    result = get_blender_connection().send_command(command, params)
    return envelope_for(result, changed_objects=changed_objects or (), warnings=warnings or ())
