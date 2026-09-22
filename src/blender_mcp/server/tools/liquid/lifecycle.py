# ruff: file-ignore[multi-line-summary-second-line]
"""Typed tools for removing fluid modifier components."""

from typing import Annotated

from mcp.server.fastmcp import Context
from pydantic import Field

from ...app import mcp
from .._dispatch import call_blender
from .._inputs import StrictModel


class FluidComponentTarget(StrictModel):
    """One fluid modifier to remove, and whether its owned helper object goes with it."""

    object_name: str
    modifier_name: str
    remove_owned_helper_object: bool = False


@mcp.tool()
async def remove_fluid_components(
    ctx: Context,
    targets: Annotated[list[FluidComponentTarget], Field(min_length=1)],
    accept_orphaned_cache: bool = False,
) -> dict:
    """Remove exact fluid modifiers and optionally MCP-owned helper objects after a cache-orphan preflight.

    Preflight rejects removal if it would orphan an existing on-disk bake, unless accept_orphaned_cache=True.
    """
    return await call_blender(
        "remove_fluid_components",
        {
            "targets": [item.model_dump() for item in targets],
            "accept_orphaned_cache": accept_orphaned_cache,
        },
        changed_objects=[item.object_name for item in targets],
    )
