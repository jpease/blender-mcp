"""
Shared plumbing and cross-file types for the scene tool modules.

`scene.py` and `scene_authoring.py` are registered by different bundles but speak the same
dialect: every input model forbids unknown fields, and every tool dispatches through one
envelope-wrapping call. Both live here rather than in either tool module so that neither
has to import the other. A tool module that imports a peer tool module would drag that
peer's `@mcp.tool()` registrations into every process that selects only one of them,
silently coupling bundle membership to import membership.

This adapts, to a flat module, the `_shared.py` convention used by the `camera`, `cloth`,
`geometry_nodes`, `lighting`, `liquid`, `retopology`, and `texture` tool packages. Unlike
those, `_call` here does not itself catch and re-raise as `ToolError` -- but FastMCP's own
`Tool.run` converts anything that escapes, so a client sees a `ToolError` either way. The
observable difference is a missing server-side `logger.error` line and a message reading
"Error executing tool <name>" instead of the siblings' "Error running <name>" (or, in
retopology, "<name> failed"). That gap is inherited from `scene.py` and preserved
deliberately so this move stays behaviour-neutral; unifying the eight copies belongs in
its own change.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict

from ..connection import get_blender_connection
from .envelope import ok


class _StrictModel(BaseModel):
    """
    Base for scene tool inputs: rejects unknown fields and non-finite floats.

    `extra="forbid"` reaches the client as `additionalProperties: false`, so an unknown key
    is an advertised validation error rather than a silent drop. `allow_inf_nan=False` is
    enforced server-side only -- pydantic emits nothing for it in the JSON schema -- so
    `NaN`/`inf` coordinates are refused before they reach Blender, but a client cannot see
    that rule in `tools/list` and will only meet it as an error.
    """

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


def _call(command: str, params: dict[str, Any], changed_objects: list[str] | None = None) -> dict:
    """
    Send one command to Blender and wrap the reply in the standard response envelope.

    Unlike the `_shared.py` siblings, failures are neither logged here nor given an
    "Error running <command>" prefix; FastMCP still converts them to `ToolError` before they
    reach the client. See this module's docstring for why that gap is preserved.

    Args:
        command: Add-on command name to dispatch.
        params: JSON-serializable parameters for that command.
        changed_objects: Object names to report as changed when the add-on does not say.
            The add-on's own `changed_objects` key, when present, replaces this value
            rather than extending it.

    Returns:
        The `ok()` envelope. `changed_objects` and `changed_resources` are removed from
        the add-on's payload and surfaced as envelope fields, so they do not also appear
        inside `data`.

    """
    result = get_blender_connection().send_command(command, params)
    resources: list[str] = []
    objects = changed_objects or []
    if isinstance(result, dict):
        result = dict(result)
        objects = result.pop("changed_objects", objects)
        resources = result.pop("changed_resources", resources)
    return ok(result, changed_objects=objects, changed_resources=resources)
