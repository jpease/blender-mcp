"""Agent-facing retopology target creation, inspection, and checkpoints."""

import asyncio

from typing import Annotated, Literal

from mcp.server.fastmcp import Context
from pydantic import Field

from ...app import mcp
from ..envelope import ok
from ._shared import _call

InitialGeometry = Literal["EMPTY", "SINGLE_VERTEX", "PLANE", "GRID", "DUPLICATED_EVALUATED_SURFACE"]
CheckpointAction = Literal["CREATE", "LIST", "COMPARE", "RESTORE", "DELETE"]


@mcp.tool()
async def create_retopology_target(
    ctx: Context,
    source_object_names: list[str],
    name: str | None = None,
    initial_geometry: InitialGeometry = "EMPTY",
    collection_name: str = "Retopology",
    size: Annotated[float, Field(gt=0)] = 1.0,
    grid_segments: tuple[int, int] = (4, 4),
    add_mirror: bool = False,
    add_shrinkwrap: bool = True,
    subdivision_levels: Annotated[int, Field(ge=0, le=6)] = 0,
) -> dict:
    """
    Create an editable low-poly target linked to one or more source meshes.

    Use EMPTY when topology will be built by later tools, SINGLE_VERTEX for
    vertex-by-vertex work, PLANE/GRID for a starter patch, or
    DUPLICATED_EVALUATED_SURFACE to copy the sources' currently evaluated
    geometry. The target is put in `collection_name`, receives the first
    source's world transform, and records all source names in custom
    properties. Mirror and Shrinkwrap remain live and are ordered before an
    optional Subdivision Surface modifier.

    Args:
        ctx: MCP request context.
        source_object_names: Existing mesh objects, ordered with the primary projection source first.
        name: Desired target object name; Blender makes it collision-safe when already used.
        initial_geometry: Starter geometry strategy.
        collection_name: Dedicated collection to create or reuse.
        size: Local width/height for PLANE and GRID; must be positive.
        grid_segments: Vertex counts [u, v] for GRID, each at least 2.
        add_mirror: Add a live X-axis Mirror modifier.
        add_shrinkwrap: Add a live named Shrinkwrap targeting the first source.
        subdivision_levels: Live Subdivision Surface viewport/render levels, 0 to 6; 0 omits it.

    Returns:
        Target name, source links, collection, base counts, modifier order, and topology revision.

    """
    result = await asyncio.to_thread(
        _call,
        "create_retopology_target",
        {
            "source_object_names": source_object_names,
            "name": name,
            "initial_geometry": initial_geometry,
            "collection_name": collection_name,
            "size": size,
            "grid_segments": list(grid_segments),
            "add_mirror": add_mirror,
            "add_shrinkwrap": add_shrinkwrap,
            "subdivision_levels": subdivision_levels,
        },
    )
    return ok(result, changed_objects=[result["name"]])


@mcp.tool()
async def inspect_retopology(
    ctx: Context,
    object_name: str,
    selected_vertex_indices: list[int] | None = None,
    adjacency_depth: Annotated[int, Field(ge=0, le=20)] = 1,
    limit: Annotated[int, Field(ge=1, le=1000)] = 100,
    offset: Annotated[int, Field(ge=0)] = 0,
) -> dict:
    """
    Inspect topology quality and a bounded set of planning details.

    This is the preferred inspection before index-based retopology edits. It
    reports aggregate components, boundary-loop summaries, face-type counts,
    non-manifold edges, poles grouped by valence, isolated/degenerate
    elements, edge-length and face-aspect statistics, UV layers, vertex
    groups, modifiers, and symmetry evidence. When vertices are supplied, it
    also returns their adjacency neighborhoods up to `adjacency_depth`.
    Potentially long diagnostic element lists share one deterministic
    `offset`/`limit` page; `boundary_vertex` records include loop/order fields
    for reconstructing ordered loops. Reuse the returned `topology_revision` as
    `expected_revision` in mutating tools so stale indices are rejected.
    Coordinates and lengths are base-mesh local space unless explicitly named world space.
    """
    return ok(
        await asyncio.to_thread(
            _call,
            "inspect_retopology",
            {
                "object_name": object_name,
                "selected_vertex_indices": selected_vertex_indices,
                "adjacency_depth": adjacency_depth,
                "limit": limit,
                "offset": offset,
            },
        )
    )


@mcp.tool()
async def manage_retopology_checkpoint(
    ctx: Context,
    action: CheckpointAction,
    object_name: str,
    checkpoint_name: str | None = None,
    confirm: bool = False,
) -> dict:
    """
    Create, list, compare, restore, or delete recoverable mesh checkpoints.

    CREATE copies the mesh, local transform, modifier settings, vertex groups,
    and custom attributes into a hidden backup collection. LIST needs only the
    target name. COMPARE reports count/hash differences without mutation.
    RESTORE and DELETE require `confirm=True`; RESTORE replaces the target's
    mesh and relevant object state while keeping the checkpoint available.
    Provide `checkpoint_name` for every action except LIST.
    """
    result = await asyncio.to_thread(
        _call,
        "manage_retopology_checkpoint",
        {"action": action, "object_name": object_name, "checkpoint_name": checkpoint_name, "confirm": confirm},
    )
    normalized_action = action.upper()
    if normalized_action == "RESTORE":
        changed = [object_name]
    elif normalized_action in {"CREATE", "DELETE"} and result.get("backup_object"):
        changed = [result["backup_object"]]
    else:
        changed = []
    return ok(result, changed_objects=changed)
