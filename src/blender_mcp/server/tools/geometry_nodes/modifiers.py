"""Live modifier attachment, parameter, lifecycle, and copy tools."""

from typing import Annotated, Any, Literal

from mcp.server.fastmcp import Context
from pydantic import Field

from ...app import mcp
from .._dispatch import call_blender
from .._inputs import OpenPayloadModel, dump_inputs


class ModifierInputValue(OpenPayloadModel):
    """Set one exposed input by stable interface identifier."""

    identifier: str = Field(min_length=1)
    value: Any = None
    mode: Literal["VALUE", "ATTRIBUTE"] = "VALUE"
    attribute_name: str | None = None


class ModifierInputTarget(OpenPayloadModel):
    """Apply validated exposed-input values to one exact modifier instance."""

    object_name: str = Field(min_length=1)
    modifier_name: str = Field(min_length=1)
    inputs: Annotated[list[ModifierInputValue], Field(min_length=1, max_length=200)]


class ModifierReassignment(OpenPayloadModel):
    """Identify one exact modifier instance that should use a copied group."""

    object_name: str = Field(min_length=1)
    modifier_name: str = Field(min_length=1)


@mcp.tool()
async def attach_geometry_nodes_modifier(
    ctx: Context,
    object_name: str,
    node_group_name: str | None = None,
    new_group_name: str | None = None,
    modifier_name: str = "GeometryNodes",
    stack_index: int | None = None,
    show_viewport: bool = True,
    show_render: bool = True,
    single_user: bool = False,
    input_values: list[ModifierInputValue] | None = None,
) -> dict:
    """
    Attach an existing group as a live modifier, or atomically create a pass-through group.

    Name the target object explicitly. Exactly one of ``node_group_name`` and ``new_group_name``
    is required. Applicability is checked before mutation, and all created data is removed on failure.
    """
    if (node_group_name is None) == (new_group_name is None):
        raise ValueError("Provide exactly one of node_group_name and new_group_name")
    params = {
        "object_name": object_name,
        "node_group_name": node_group_name,
        "new_group_name": new_group_name,
        "modifier_name": modifier_name,
        "stack_index": stack_index,
        "show_viewport": show_viewport,
        "show_render": show_render,
        "single_user": single_user,
        "input_values": dump_inputs(input_values or []),
    }
    resources = [name for name in [node_group_name, new_group_name] if name]
    return await call_blender(
        "attach_geometry_nodes_modifier", params, changed_objects=[object_name], changed_resources=resources
    )


@mcp.tool()
async def set_geometry_nodes_inputs(
    ctx: Context,
    targets: Annotated[list[ModifierInputTarget], Field(min_length=1, max_length=200)],
) -> dict:
    """
    Set exposed inputs on one or more modifier instances without editing shared groups.

    Every object, modifier, socket identifier, value type, and referenced datablock is validated
    before the first assignment, preventing a partially updated batch.
    """
    object_names = list(dict.fromkeys(target.object_name for target in targets))
    return await call_blender(
        "set_geometry_nodes_inputs", {"targets": dump_inputs(targets)}, changed_objects=object_names
    )


@mcp.tool()
async def manage_geometry_nodes_modifier(
    ctx: Context,
    object_name: str,
    modifier_name: str,
    action: Literal["RENAME", "MOVE", "SET_VISIBILITY", "MUTE", "REPLACE_GROUP", "REMOVE", "APPLY"],
    new_name: str | None = None,
    stack_index: int | None = None,
    show_viewport: bool | None = None,
    show_render: bool | None = None,
    mute: bool | None = None,
    replacement_group_name: str | None = None,
    confirm_destructive: bool = False,
) -> dict:
    """
    Change one exact Geometry Nodes modifier while preserving its live graph by default.

    ``REMOVE`` and ``APPLY`` discard procedural state and require ``confirm_destructive=True``.
    Applying changes base geometry and invalidates previously inspected topology indices.
    """
    if action in {"REMOVE", "APPLY"} and not confirm_destructive:
        raise ValueError(f"confirm_destructive=True is required for {action}")
    return await call_blender(
        "manage_geometry_nodes_modifier",
        {
            "object_name": object_name,
            "modifier_name": modifier_name,
            "action": action,
            "new_name": new_name,
            "stack_index": stack_index,
            "show_viewport": show_viewport,
            "show_render": show_render,
            "mute": mute,
            "replacement_group_name": replacement_group_name,
            "confirm_destructive": confirm_destructive,
        },
        changed_objects=[object_name],
    )


@mcp.tool()
async def copy_geometry_node_group(
    ctx: Context,
    node_group_name: str,
    new_name: str,
    reassign_modifiers: list[ModifierReassignment] | None = None,
    duplicate_object_name: str | None = None,
    duplicated_object_name: str | None = None,
    copy_object_data: bool = False,
    copy_action: bool = False,
    reassign_duplicate_modifiers: bool = True,
    collision_policy: Literal["ERROR", "UNIQUE"] = "ERROR",
) -> dict:
    """
    Copy a reusable group and optionally reassign exact modifier instances to the copy.

    External object, collection, material, image, and texture references remain shared and are
    returned for review. The copy receives a new MCP UUID and source-provenance tag.
    """
    if duplicate_object_name is None and duplicated_object_name is not None:
        raise ValueError("duplicated_object_name requires duplicate_object_name")
    objects = list(dict.fromkeys(item.object_name for item in reassign_modifiers or []))
    if duplicated_object_name:
        objects.append(duplicated_object_name)
    return await call_blender(
        "copy_geometry_node_group",
        {
            "node_group_name": node_group_name,
            "new_name": new_name,
            "reassign_modifiers": dump_inputs(reassign_modifiers or []),
            "duplicate_object_name": duplicate_object_name,
            "duplicated_object_name": duplicated_object_name,
            "copy_object_data": copy_object_data,
            "copy_action": copy_action,
            "reassign_duplicate_modifiers": reassign_duplicate_modifiers,
            "collision_policy": collision_policy,
        },
        changed_objects=objects,
        changed_resources=[new_name],
    )
