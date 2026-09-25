"""Atomic interface and node-graph authoring handlers."""

from collections.abc import Callable
from typing import Any

import bpy

from ...helpers import counted_page
from ..node_graph import apply_graph_operation
from ._shared import (
    add_interface_socket,
    find_interface_item,
    find_panel,
    group_users,
    initialize_group,
    require_group,
    set_writable_property,
)


def _replace_group_references(original, replacement) -> None:
    """Redirect object modifiers and nested group nodes to an atomic replacement."""
    for obj in bpy.data.objects:
        for modifier in obj.modifiers:
            if modifier.type == "NODES" and modifier.node_group == original:
                modifier.node_group = replacement
    for tree in bpy.data.node_groups:
        if tree == original or not hasattr(tree, "nodes"):
            continue
        for node in tree.nodes:
            if getattr(node, "node_tree", None) == original:
                node.node_tree = replacement


def atomic_group_edit(group, edit: Callable[[Any], Any]):
    """Edit a private copy and swap it in only after every operation succeeds."""
    original_name = group.name
    working = group.copy()
    working.name = f"{original_name}.__MCP_WORKING__"
    try:
        result = edit(working)
    except Exception:
        bpy.data.node_groups.remove(working, do_unlink=True)
        raise
    _replace_group_references(group, working)
    bpy.data.node_groups.remove(group, do_unlink=True)
    working.name = original_name
    return working, result


def _user_objects_page(users: list[dict[str, Any]]) -> dict[str, object]:
    """
    Count the objects whose modifiers run a node group, by type, beside a sample of their names.

    Every one of them evaluates the edited group, but a shared group can drive any number of
    objects and the caller's next call names the group, which `changed_resources` carries.

    Args:
        users: `group_users` records: one per modifier, so an object can appear more than once.

    Returns:
        dict[str, object]: A `counted_page` over the distinct user objects.

    """
    objects = [bpy.data.objects[name] for name in sorted({user["object"] for user in users})]
    return counted_page(objects, type_of=lambda obj: obj.type, name_of=lambda obj: obj.name)


def _apply_graph_operation(group, operation: dict[str, Any], name_map: dict[str, str]) -> None:
    """Apply one Geometry Nodes edit through the shared node-graph engine."""
    apply_graph_operation(
        group,
        operation,
        name_map,
        allowed_prefixes=(
            "GeometryNode",
            "ShaderNode",
            "FunctionNode",
            "NodeFrame",
            "NodeGroupInput",
            "NodeGroupOutput",
        ),
        graph_label="Geometry Nodes",
    )


def _apply_interface_edit(group, edit: dict[str, Any], migration_policy: str) -> dict[str, Any]:
    """Apply one interface operation to a private working copy."""
    action = edit["operation"]
    if action == "ADD_SOCKET":
        spec = edit.get("socket")
        if spec is None:
            raise ValueError("ADD_SOCKET requires socket")
        panels = {item.name: item for item in group.interface.items_tree if item.item_type == "PANEL"}
        item = add_interface_socket(group, spec, panels)
        return {"operation": action, "identifier": item.identifier, "name": item.name}
    if action == "ADD_PANEL":
        spec = edit.get("panel")
        if spec is None:
            raise ValueError("ADD_PANEL requires panel")
        parent = find_panel(group, spec["parent_panel"]) if spec.get("parent_panel") else None
        item = group.interface.new_panel(
            name=spec["name"],
            description=spec.get("description", ""),
            default_closed=spec.get("default_closed", False),
            parent=parent,
        )
        return {"operation": action, "identifier": item.identifier, "name": item.name}
    identifier = edit.get("identifier")
    if not identifier:
        raise ValueError(f"{action} requires identifier")
    item = find_interface_item(group, identifier)
    if action == "UPDATE":
        changes = edit.get("changes", {})
        if (
            "socket_type" in changes
            and changes["socket_type"] != getattr(item, "socket_type", None)
            and migration_policy != "ALLOW_BREAKING"
        ):
            raise ValueError("Changing socket_type requires migration_policy='ALLOW_BREAKING'")
        for key, value in changes.items():
            set_writable_property(item, key, value)
    elif action == "MOVE":
        parent_identifier = edit.get("parent_identifier")
        position = edit.get("position", 0)
        if parent_identifier is not None:
            parent = find_interface_item(group, parent_identifier)
            if parent.item_type != "PANEL":
                raise ValueError("parent_identifier must identify a panel")
            group.interface.move_to_parent(item, parent, position)
        else:
            group.interface.move(item, position)
    elif action == "REMOVE":
        if migration_policy != "ALLOW_BREAKING":
            raise ValueError("Removing interface items requires migration_policy='ALLOW_BREAKING'")
        group.interface.remove(item, move_content_to_parent=False)
    else:
        raise ValueError(f"Unsupported interface operation: {action}")
    return {"operation": action, "identifier": identifier, "name": item.name if action != "REMOVE" else None}


class GeometryNodesAuthoringHandlersMixin:
    """Create reusable groups and perform copy-on-write graph/interface edits."""

    def create_geometry_node_group(
        self,
        name,
        execution_role="MODIFIER",
        geometry_types=None,
        tool_modes=None,
        sockets=None,
        panels=None,
        description="",
        color_tag="NONE",
        collision_policy="ERROR",
        purpose="custom procedural system",
    ):
        group, created = initialize_group(
            name,
            purpose=purpose,
            execution_role=execution_role,
            geometry_types=geometry_types,
            tool_modes=tool_modes,
            sockets=sockets,
            panels=panels,
            description=description,
            color_tag=color_tag,
            collision_policy=collision_policy,
        )
        return {
            "node_group": group.name,
            "created": created,
            "execution_role": execution_role,
            "interface": [
                {
                    "identifier": getattr(item, "identifier", None),
                    "name": item.name,
                    "item_type": item.item_type,
                    "direction": getattr(item, "in_out", None),
                    "socket_type": getattr(item, "socket_type", None),
                }
                for item in group.interface.items_tree
            ],
            "mcp_uuid": group.get("blender_mcp_uuid"),
            "changed_resources": [group.name] if created else [],
        }

    def edit_node_group_interface(self, node_group_name, edits, migration_policy="ERROR_ON_BREAKING"):
        group = require_group(node_group_name)
        affected_users = group_users(group)

        def edit_copy(working):
            return [_apply_interface_edit(working, edit, migration_policy) for edit in edits]

        replacement, changes = atomic_group_edit(group, edit_copy)
        return {
            "node_group": replacement.name,
            "changes": changes,
            "affected_users": _user_objects_page(affected_users),
            "migration_policy": migration_policy,
            "changed_resources": [replacement.name],
        }

    def patch_geometry_node_graph(self, node_group_name, operations):
        group = require_group(node_group_name)
        users = group_users(group)

        def edit_copy(working):
            name_map = {}
            for operation in operations:
                _apply_graph_operation(working, operation, name_map)
            return name_map

        replacement, name_map = atomic_group_edit(group, edit_copy)
        return {
            "node_group": replacement.name,
            "operation_count": len(operations),
            "name_map": name_map,
            "node_count": len(replacement.nodes),
            "link_count": len(replacement.links),
            "affected_users": _user_objects_page(users),
            "changed_resources": [replacement.name],
        }
