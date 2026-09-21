"""ND (HugeMenace) non-destructive hard-surface workflow tools."""

import logging

from typing import Annotated, Literal

from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from ..app import mcp
from ._dispatch import send_blender_command
from .envelope import ok

logger = logging.getLogger("BlenderMCPServer")

BooleanMode = Literal["UNION", "DIFFERENCE", "INTERSECT"]
LodMode = Literal["HIGH", "LOW"]
PulseToggle = Literal["CLEAR_VIEW", "CUSTOM_VIEW", "UTILS"]

ObjectNameList = Annotated[list[str], Field(min_length=1, max_length=500)]

_CANCELLED_WARNING = "ND operator was cancelled - the scene is unchanged"


def _nd_outcome(
    result,
    changed_objects: list[str] | None = None,
    changed_resources: list[str] | None = None,
) -> dict:
    """
    Build the tool envelope for an ND operator result, gating success and
    changed_objects/changed_resources on the handler's `cancelled` flag instead of
    optimistically reporting the objects/resources the tool targeted.

    Args:
        result: The raw dict returned by the Blender-side ND handler.
        changed_objects: Object names to report as changed when the operator was not cancelled.
        changed_resources: Non-object datablock names (e.g. materials) to report as changed when
            the operator was not cancelled.

    Returns:
        dict: The envelope produced by `ok()`.

    """
    if isinstance(result, dict) and result.get("cancelled"):
        return ok(result, success=False, changed_objects=[], warnings=[_CANCELLED_WARNING])
    return ok(result, changed_objects=changed_objects, changed_resources=changed_resources)


@mcp.tool()
async def nd_boolean(
    ctx: Context,
    object_name: str,
    cutter_object_name: str,
    mode: BooleanMode = "DIFFERENCE",
) -> dict:
    """
    Create a live ND Boolean modifier and retain the cutter as an ND utility object.

    The cutter becomes a wireframe utility object parented to the target instead of
    being deleted, unlike `mesh_boolean`. object_name and cutter_object_name must refer
    to different objects.

    Args:
        ctx: MCP request context.
        object_name: Name of the mesh object the boolean is applied to (the result/target).
        cutter_object_name: Name of the other mesh object used as the cutter/operand. Must differ from object_name.
        mode: One of UNION, DIFFERENCE, INTERSECT.

    Cancellation: if the user cancels this ND operator (Esc), this returns ok:false with an empty
    changed_objects and a warning explaining the scene is unchanged.

    Returns:
        the target and cutter object names and updated vertex/edge/polygon counts.

    Raises:
        ToolError: If the operation cannot be completed.

    """
    try:
        result = await send_blender_command(
            "nd_boolean",
            {
                "object_name": object_name,
                "cutter_object_name": cutter_object_name,
                "mode": mode,
            },
        )
        return _nd_outcome(result, changed_objects=[object_name, cutter_object_name])
    except Exception as e:
        logger.error(f"Error applying ND boolean: {e}")
        raise ToolError(f"Error applying ND boolean: {e}") from e


@mcp.tool()
async def nd_mark_as_util(
    ctx: Context,
    object_names: ObjectNameList,
    unmark: bool = False,
    parent_to: Annotated[str | None, Field(min_length=1)] = None,
) -> dict:
    """
    Mark objects as ND utilities, or restore previously marked objects.

    Marked objects display as wireframes and are hidden from renders and most viewport
    visibility categories. When parent_to is given (mark path only, unmark=False), each
    marked object is also reparented to it while preserving its world transform,
    replicating the parenting half of ND's real mark_as_util operator. The
    keyboard-modifier-driven behaviors of the real operator (Ctrl-revert,
    Alt-skip-parenting, Shift-recursive-children) have no scriptable equivalent and are
    not replicated here.

    Args:
        ctx: MCP request context.
        object_names: Names of the objects to mark/unmark.
        unmark: If True, restore normal (SOLID/visible) display instead of marking as a utility.
        parent_to: Name of an object to reparent each marked object to, preserving world transform. Only valid when
            unmark is False.

    Cancellation: unlike the other nd_* tools, this is a direct data mutation with no interactive/cancellable
    step - it always returns ok:true with changed_objects set to object_names.

    Returns:
        the affected object names.

    Raises:
        ToolError: If the operation cannot be completed.

    """
    try:
        result = await send_blender_command(
            "nd_mark_as_util", {"object_names": object_names, "unmark": unmark, "parent_to": parent_to}
        )
        return ok(result, changed_objects=object_names)
    except Exception as e:
        logger.error(f"Error marking ND utility objects: {e}")
        raise ToolError(f"Error marking ND utility objects: {e}") from e


@mcp.tool()
async def nd_clean_utils(ctx: Context, confirm: bool = False) -> dict:
    """
    Remove orphaned boolean/array/mirror/lattice modifiers and their ND utility objects, scene-wide.

    A true dry-run isn't feasible without reimplementing ND's own cleanup logic, so this
    always performs the cleanup - but reports exactly what was removed by diffing the
    scene before and after.

    The mutation transaction wraps this in a single named Blender undo step on success,
    so Edit > Undo History can revert the whole cleanup as one action - but there is no
    MCP-level rollback once this response has been returned.

    Cancellation: this has no interactive/cancellable step of its own, but still uses the shared ND result
    shape - it returns ok:false only if the underlying handler reports "cancelled" (not expected in normal use).

    Args:
        ctx: MCP request context.
        confirm: Must be True to run - this is scene-wide and destructive with no way to scope or preview it.

    Returns:
        "removed_objects" (names of deleted ND utility objects, also reported in changed_objects) and
        "removed_modifiers" (each as {"object", "modifier", "type"}).

    Raises:
        ToolError: If the operation cannot be completed.

    """
    try:
        result = await send_blender_command("nd_clean_utils", {"confirm": confirm})
        removed = result.get("removed_objects", []) if isinstance(result, dict) else []
        return _nd_outcome(result, changed_objects=removed)
    except Exception as e:
        logger.error(f"Error cleaning ND utility objects: {e}")
        raise ToolError(f"Error cleaning ND utility objects: {e}") from e


@mcp.tool()
async def nd_create_id_material(
    ctx: Context,
    object_names: ObjectNameList,
    material_name: Annotated[str, Field(min_length=1)],
) -> dict:
    """
    Create/assign a single ND ID material to the given mesh/curve objects.

    Args:
        ctx: MCP request context.
        object_names: Names of the objects to assign the material to.
        material_name: Name of the ID material to create/reuse.

    Cancellation: if the user cancels this ND operator (Esc), this returns ok:false with an empty
    changed_objects and a warning explaining the scene is unchanged.

    Returns:
        "names" (the affected object names) and "material_name".

    Raises:
        ToolError: If the operation cannot be completed.

    """
    try:
        result = await send_blender_command(
            "nd_create_id_material", {"object_names": object_names, "material_name": material_name}
        )
        return _nd_outcome(result, changed_objects=object_names, changed_resources=[material_name])
    except Exception as e:
        logger.error(f"Error creating ND ID material: {e}")
        raise ToolError(f"Error creating ND ID material: {e}") from e


@mcp.tool()
async def nd_bulk_create_id_materials(ctx: Context, object_names: ObjectNameList) -> dict:
    """
    Assign a random distinct ND ID material to each given mesh/curve object.

    Args:
        ctx: MCP request context.
        object_names: Names of the objects to assign distinct ID materials to.

    Cancellation: if the user cancels this ND operator (Esc), this returns ok:false with an empty
    changed_objects and a warning explaining the scene is unchanged.

    Returns:
        "names" (the affected object names) and "material_names" (the distinct materials created).

    Raises:
        ToolError: If the operation cannot be completed.

    """
    try:
        result = await send_blender_command("nd_bulk_create_id_materials", {"object_names": object_names})
        materials = result.get("material_names", []) if isinstance(result, dict) else []
        return _nd_outcome(result, changed_objects=object_names, changed_resources=materials)
    except Exception as e:
        logger.error(f"Error bulk-creating ND ID materials: {e}")
        raise ToolError(f"Error bulk-creating ND ID materials: {e}") from e


@mcp.tool()
async def nd_set_lod_suffix(
    ctx: Context,
    object_names: ObjectNameList,
    mode: LodMode = "HIGH",
) -> dict:
    """
    Suffix object (and data-block) names with _high or _low, replacing any existing LOD suffix.

    Args:
        ctx: MCP request context.
        object_names: Names of the objects to rename.
        mode: One of HIGH, LOW.

    Cancellation: if the user cancels this ND operator (Esc), this returns ok:false with an empty
    changed_objects and a warning explaining the scene is unchanged.

    Returns:
        "names" (the objects' new, renamed names).

    Raises:
        ToolError: If the operation cannot be completed.

    """
    try:
        result = await send_blender_command("nd_set_lod_suffix", {"object_names": object_names, "mode": mode})
        changed = result.get("names", object_names) if isinstance(result, dict) else object_names
        return _nd_outcome(result, changed_objects=changed)
    except Exception as e:
        logger.error(f"Error setting ND LOD suffix: {e}")
        raise ToolError(f"Error setting ND LOD suffix: {e}") from e


@mcp.tool()
async def nd_single_vertex(
    ctx: Context,
    location: tuple[float, float, float] = (0, 0, 0),
) -> dict:
    """
    Create an ND single-vertex sketch object at location, left in Object mode.

    Args:
        ctx: MCP request context.
        location: [x, y, z] world location for the new vertex object.

    Cancellation: if the user cancels this ND operator (Esc), this returns ok:false with an empty
    changed_objects and a warning explaining the scene is unchanged (no object is created).

    Returns:
        the new object's name and location.

    Raises:
        ToolError: If the operation cannot be completed.

    """
    try:
        result = await send_blender_command("nd_single_vertex", {"location": list(location)})
        name = result.get("name") if isinstance(result, dict) else None
        return _nd_outcome(result, changed_objects=[name] if name else [])
    except Exception as e:
        logger.error(f"Error creating ND single vertex: {e}")
        raise ToolError(f"Error creating ND single vertex: {e}") from e


@mcp.tool()
async def nd_apply_modifiers(ctx: Context, object_names: ObjectNameList) -> dict:
    """
    Apply eligible modifiers on objects using ND's default REGULAR apply mode.

    This mode is selective and preserves ND's built-in exclusions for bevel,
    weighted normals, and related modifiers. The
    SOFT/HARD/duplicate variants are driven by modifier keys in ND's UI and are not reachable
    from a script.

    Args:
        ctx: MCP request context.
        object_names: Names of the objects to apply modifiers on.

    Cancellation: if the user cancels this ND operator (Esc), this returns ok:false with an empty
    changed_objects and a warning explaining the scene is unchanged. This also changes topology on any object
    whose modifiers were applied - indices from an earlier get_mesh_data call are no longer valid for those
    objects afterward.

    Returns:
        "names" (the affected object names).

    Raises:
        ToolError: If the operation cannot be completed.

    """
    try:
        result = await send_blender_command("nd_apply_modifiers", {"object_names": object_names})
        return _nd_outcome(result, changed_objects=object_names)
    except Exception as e:
        logger.error(f"Error applying ND modifiers: {e}")
        raise ToolError(f"Error applying ND modifiers: {e}") from e


@mcp.tool()
async def nd_pulse_viewport_toggle(ctx: Context, toggle: PulseToggle) -> dict:
    """
    Pulse an ND viewport toggle that has no readable on/off state of its own.

    ND exposes no readable state for CLEAR_VIEW, CUSTOM_VIEW, or UTILS, so this just
    flips ND's internal toggle operator - it is NOT guaranteed idempotent. ND's
    SILHOUETTE toggle is a genuine modal operator and is intentionally not exposed here.
    For the native Blender viewport overlays (cavity, wireframes, face orientation), use
    set_viewport_overlay instead - those are true idempotent setters.

    Args:
        ctx: MCP request context.
        toggle: One of CLEAR_VIEW, CUSTOM_VIEW, UTILS.

    Cancellation: if the user cancels this ND operator (Esc), this returns ok:false with an empty
    changed_objects and a warning explaining the scene is unchanged. No objects/resources are ever reported as
    changed - this toggles viewport display state only.

    Returns:
        "toggle" (the toggle that was pulsed).

    Raises:
        ToolError: If the operation cannot be completed.

    """
    try:
        result = await send_blender_command("nd_pulse_viewport_toggle", {"toggle": toggle})
        return _nd_outcome(result)
    except Exception as e:
        logger.error(f"Error pulsing ND viewport toggle: {e}")
        raise ToolError(f"Error pulsing ND viewport toggle: {e}") from e


@mcp.tool()
async def nd_capture_utils(ctx: Context) -> dict:
    """
    Display and select all ND utility objects in the scene.

    Args:
        ctx: MCP request context.

    Cancellation: if the user cancels this ND operator (Esc), this returns ok:false with an empty
    changed_objects and a warning explaining the scene is unchanged. No objects/resources are ever reported as
    changed - this only changes selection/display state.

    Returns:
        "status" ("captured" on success).

    Raises:
        ToolError: If the operation cannot be completed.

    """
    try:
        result = await send_blender_command("nd_capture_utils", {})
        return _nd_outcome(result)
    except Exception as e:
        logger.error(f"Error capturing ND utility objects: {e}")
        raise ToolError(f"Error capturing ND utility objects: {e}") from e
