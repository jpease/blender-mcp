# ruff: file-ignore[multi-line-summary-second-line]
"""Geometry Nodes bake and simulation-cache lifecycle tool."""

import asyncio

from typing import Annotated, Literal

from mcp.server.fastmcp import Context
from pydantic import Field

from ...app import mcp
from ._shared import call_geometry_nodes

BakeAction = Literal["INSPECT", "BAKE", "PACK", "UNPACK", "DELETE"]


@mcp.tool()
async def manage_geometry_nodes_bake(
    ctx: Context,
    object_name: str,
    modifier_name: str,
    action: BakeAction = "INSPECT",
    bake_id: Annotated[int | None, Field(ge=0)] = None,
    frame_start: int | None = None,
    frame_end: int | None = None,
    bake_target: Literal["PACKED", "DISK"] | None = None,
    directory: str | None = None,
    max_frames: Annotated[int | None, Field(ge=1, le=10_000)] = None,
    max_bytes: Annotated[int | None, Field(ge=1)] = None,
    time_limit_seconds: Annotated[float | None, Field(gt=0, le=86_400)] = None,
    unpack_method: Literal["USE_LOCAL", "WRITE_LOCAL", "USE_ORIGINAL", "WRITE_ORIGINAL"] = "USE_ORIGINAL",
    confirm_bake: bool = False,
    confirm_overwrite: bool = False,
    confirm_delete: bool = False,
) -> dict:
    """Inspect or explicitly operate on one Geometry Nodes bake/cache entry.

    Start with ``INSPECT`` to obtain stable ``bake_id`` values. ``BAKE`` requires an explicit frame
    range, storage target, frame/byte/time budgets, and confirmation. ``DISK`` baking and unpacking
    also require a user-chosen existing directory. ``DELETE`` permanently removes that entry's
    cache and requires separate confirmation. Packing changes the open file only; this tool never
    saves the .blend file.
    """
    if action != "INSPECT" and bake_id is None:
        raise ValueError(f"{action} requires bake_id from an INSPECT result")
    if action == "BAKE":
        if not confirm_bake:
            raise ValueError("confirm_bake=True is required for BAKE")
        required = {
            "frame_start": frame_start,
            "frame_end": frame_end,
            "bake_target": bake_target,
            "max_frames": max_frames,
            "max_bytes": max_bytes,
            "time_limit_seconds": time_limit_seconds,
        }
        missing = [name for name, value in required.items() if value is None]
        if missing:
            raise ValueError(f"BAKE requires explicit values for: {', '.join(missing)}")
        if frame_start is not None and frame_end is not None and frame_start > frame_end:
            raise ValueError("frame_start must not exceed frame_end")
        if bake_target == "DISK" and not directory:
            raise ValueError("DISK baking requires an explicit existing directory")
    if action == "UNPACK" and not directory:
        raise ValueError("UNPACK requires an explicit existing directory")
    if action == "DELETE" and not confirm_delete:
        raise ValueError("confirm_delete=True is required for DELETE")
    return await asyncio.to_thread(
        call_geometry_nodes,
        "manage_geometry_nodes_bake",
        {
            "object_name": object_name,
            "modifier_name": modifier_name,
            "action": action,
            "bake_id": bake_id,
            "frame_start": frame_start,
            "frame_end": frame_end,
            "bake_target": bake_target,
            "directory": directory,
            "max_frames": max_frames,
            "max_bytes": max_bytes,
            "time_limit_seconds": time_limit_seconds,
            "unpack_method": unpack_method,
            "confirm_bake": confirm_bake,
            "confirm_overwrite": confirm_overwrite,
            "confirm_delete": confirm_delete,
        },
        changed_objects=[] if action == "INSPECT" else [object_name],
    )
