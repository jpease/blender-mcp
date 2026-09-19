"""Shared Blender-runtime operations for validated node-graph patches."""

from typing import Any

import bpy


def resolve_rna_value(prop, value: Any) -> Any:
    """Resolve a scalar/vector or explicit Blender-ID reference for RNA assignment."""
    if prop.type != "POINTER" or not isinstance(value, dict):
        return value
    id_type = str(value.get("id_type", "")).upper()
    name = value.get("name")
    collections = {
        "OBJECT": bpy.data.objects,
        "COLLECTION": bpy.data.collections,
        "MATERIAL": bpy.data.materials,
        "IMAGE": bpy.data.images,
        "TEXTURE": bpy.data.textures,
        "NODE_GROUP": bpy.data.node_groups,
        "TEXT": bpy.data.texts,
        "WORLD": bpy.data.worlds,
        "LIGHT": bpy.data.lights,
        "CAMERA": bpy.data.cameras,
    }
    collection = collections.get(id_type)
    if collection is None or not isinstance(name, str):
        raise ValueError(
            "Pointer values require {'id_type': OBJECT|COLLECTION|MATERIAL|IMAGE|TEXTURE|NODE_GROUP|"
            "TEXT|WORLD|LIGHT|CAMERA, 'name': ...}"
        )
    resolved = collection.get(name)
    if resolved is None:
        raise ValueError(f"{id_type} datablock not found: {name}")
    return resolved


def set_writable_property(target, name: str, value: Any) -> None:
    """Assign one direct runtime-verified writable RNA property."""
    if name.startswith("_") or "." in name:
        raise ValueError(f"Nested or private RNA property paths are not allowed: {name}")
    prop = target.bl_rna.properties.get(name)
    if prop is None or prop.is_readonly or name in {"rna_type", "type", "bl_idname"}:
        raise ValueError(f"Property '{name}' is not writable on {target.bl_rna.identifier}")
    setattr(target, name, resolve_rna_value(prop, value))


def resolve_socket(sockets, identifier: str | None, index: int | None, endpoint: str):
    """Resolve a node socket by stable identifier with explicit index fallback."""
    if identifier is not None:
        matches = [socket for socket in sockets if socket.identifier == identifier]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1 and index is None:
            raise ValueError(f"{endpoint} socket identifier '{identifier}' is ambiguous; provide its index")
    if index is not None and index < len(sockets):
        socket = sockets[index]
        if identifier is not None and socket.identifier != identifier:
            raise ValueError(f"{endpoint} socket index {index} does not match identifier '{identifier}'")
        return socket
    raise ValueError(f"Could not resolve {endpoint} socket identifier={identifier!r} index={index!r}")


def node_by_name(tree, name: str | None):
    """Resolve one exact node name in a node tree."""
    if not name:
        raise ValueError("A node name is required")
    node = tree.nodes.get(name)
    if node is None:
        raise ValueError(f"Node '{name}' not found in '{tree.name}'")
    return node


def validate_node_type(bl_idname: str, allowed_prefixes: tuple[str, ...], graph_label: str) -> None:
    """Require a runtime-registered node type from an explicit graph-family allowlist."""
    if not isinstance(bl_idname, str) or not bl_idname.startswith(allowed_prefixes):
        raise ValueError(f"Node type '{bl_idname}' is outside the {graph_label} authoring allowlist")
    if getattr(bpy.types, bl_idname, None) is None:
        raise ValueError(f"Node type unavailable in this Blender runtime: {bl_idname}")


def set_node_properties(node, properties: dict[str, Any]) -> None:
    """Apply runtime-verified direct properties to one node."""
    for key, value in properties.items():
        if key == "location":
            node.location = value
        elif key == "parent":
            raise ValueError("Use MOVE_TO_FRAME to set a node parent")
        else:
            set_writable_property(node, key, value)


def apply_graph_operation(
    tree,
    operation: dict[str, Any],
    name_map: dict[str, str],
    *,
    allowed_prefixes: tuple[str, ...],
    graph_label: str,
    managed_owner: str | None = None,
) -> None:
    """Apply one schema-validated graph edit to a private working tree."""
    action = operation["operation"]
    if action == "ADD_NODE":
        bl_idname = operation.get("bl_idname")
        if not bl_idname:
            raise ValueError("ADD_NODE requires bl_idname")
        validate_node_type(bl_idname, allowed_prefixes, graph_label)
        node = tree.nodes.new(bl_idname)
        requested = operation.get("new_name") or operation.get("node_name")
        if requested:
            if tree.nodes.get(requested) is not None and tree.nodes.get(requested) != node:
                tree.nodes.remove(node)
                raise ValueError(f"Node name already exists: {requested}")
            node.name = requested
        set_node_properties(node, operation.get("properties", {}))
        if managed_owner is not None:
            node["mcp_owner"] = managed_owner
            node["mcp_graph_role"] = operation.get("managed_role") or requested or node.name
        name_map[requested or node.name] = node.name
        return

    node_name = operation.get("node_name")
    if action not in {"ADD_LINK", "REMOVE_LINK"} and not node_name:
        raise ValueError(f"{action} requires node_name")
    if action == "UPDATE_NODE":
        node = node_by_name(tree, node_name)
        new_name = operation.get("new_name")
        if new_name and new_name != node.name:
            if tree.nodes.get(new_name) is not None:
                raise ValueError(f"Node name already exists: {new_name}")
            node.name = new_name
        set_node_properties(node, operation.get("properties", {}))
    elif action == "SET_INPUT":
        node = node_by_name(tree, node_name)
        socket = resolve_socket(
            node.inputs,
            operation.get("socket_identifier"),
            operation.get("socket_index"),
            f"input on {node.name}",
        )
        if not hasattr(socket, "default_value"):
            raise ValueError(f"Input '{socket.identifier}' on '{node.name}' has no settable default")
        prop = socket.bl_rna.properties.get("default_value")
        value = resolve_rna_value(prop, operation.get("value")) if prop is not None else operation.get("value")
        socket.default_value = value
    elif action == "ADD_LINK":
        from_node = node_by_name(tree, operation.get("from_node"))
        to_node = node_by_name(tree, operation.get("to_node"))
        from_socket = resolve_socket(
            from_node.outputs,
            operation.get("from_socket_identifier"),
            operation.get("from_socket_index"),
            f"output on {from_node.name}",
        )
        to_socket = resolve_socket(
            to_node.inputs,
            operation.get("to_socket_identifier"),
            operation.get("to_socket_index"),
            f"input on {to_node.name}",
        )
        tree.links.new(from_socket, to_socket)
    elif action == "REMOVE_LINK":
        from_node = node_by_name(tree, operation.get("from_node"))
        to_node = node_by_name(tree, operation.get("to_node"))
        from_socket = resolve_socket(
            from_node.outputs,
            operation.get("from_socket_identifier"),
            operation.get("from_socket_index"),
            f"output on {from_node.name}",
        )
        to_socket = resolve_socket(
            to_node.inputs,
            operation.get("to_socket_identifier"),
            operation.get("to_socket_index"),
            f"input on {to_node.name}",
        )
        matches = [link for link in tree.links if link.from_socket == from_socket and link.to_socket == to_socket]
        if len(matches) != 1:
            raise ValueError(f"Expected one matching link, found {len(matches)}")
        tree.links.remove(matches[0])
    elif action == "MOVE_TO_FRAME":
        node = node_by_name(tree, node_name)
        frame_name = operation.get("frame_name")
        node.parent = node_by_name(tree, frame_name) if frame_name else None
        if node.parent is not None and node.parent.bl_idname != "NodeFrame":
            raise ValueError(f"Node '{frame_name}' is not a frame")
    elif action == "REMOVE_NODE":
        tree.nodes.remove(node_by_name(tree, node_name))
    elif action == "SET_ACTIVE_OUTPUT":
        node = node_by_name(tree, node_name)
        if not hasattr(node, "is_active_output"):
            raise ValueError(f"Node '{node.name}' cannot be an active output")
        node.is_active_output = True
    else:
        raise ValueError(f"Unsupported graph operation: {action}")
