"""Shared Blender-runtime helpers for structured Geometry Nodes commands."""

import math
import uuid

from typing import Any

import bpy
import mathutils

from ...helpers import paginate
from ..node_graph import resolve_rna_value
from ..node_graph import set_writable_property as set_writable_property

OWNERSHIP_KEY = "blender_mcp_uuid"
SCHEMA_KEY = "blender_mcp_schema_version"
PURPOSE_KEY = "blender_mcp_purpose"
SOURCE_KEY = "blender_mcp_source_uuid"
BUILDER_KEY = "blender_mcp_builder"
ROLE_KEY = "blender_mcp_role"
SCHEMA_VERSION = 1
SUPPORTED_OBJECT_TYPES = {"MESH", "CURVE", "POINTCLOUD", "GREASEPENCIL"}
SUPPORTED_NODE_FAMILIES = ("GeometryNode", "ShaderNode", "FunctionNode", "NodeFrame", "NodeGroup")


def find_group(name: str):
    """Return one GeometryNodeTree, including a linked read-only group."""
    group = bpy.data.node_groups.get(name)
    if group is None:
        raise ValueError(f"Geometry node group not found: {name}")
    if group.bl_idname != "GeometryNodeTree":
        raise ValueError(f"Node group '{name}' is {group.bl_idname}, not GeometryNodeTree")
    return group


def require_group(name: str):
    """Return one editable GeometryNodeTree or raise a precise error."""
    group = find_group(name)
    if group.library is not None or not group.is_editable:
        raise ValueError(f"Geometry node group '{name}' is linked or read-only")
    return group


def require_object(name: str):
    """Return an object that Geometry Nodes can operate on."""
    obj = bpy.data.objects.get(name)
    if obj is None:
        raise ValueError(f"Object not found: {name}")
    if obj.type not in SUPPORTED_OBJECT_TYPES:
        raise ValueError(f"Object '{name}' has unsupported type {obj.type}")
    return obj


def require_nodes_modifier(obj, name: str):
    """Return one exact Geometry Nodes modifier."""
    modifier = obj.modifiers.get(name)
    if modifier is None:
        raise ValueError(f"Modifier '{name}' not found on '{obj.name}'")
    if modifier.type != "NODES":
        raise ValueError(f"Modifier '{name}' on '{obj.name}' is not a Geometry Nodes modifier")
    return modifier


def tag_group(group, purpose: str, *, source_uuid: str | None = None, builder: str | None = None) -> None:
    """Attach stable ownership, schema, provenance, and builder metadata."""
    group[OWNERSHIP_KEY] = str(uuid.uuid4())
    group[SCHEMA_KEY] = SCHEMA_VERSION
    group[PURPOSE_KEY] = purpose
    if source_uuid:
        group[SOURCE_KEY] = source_uuid
    if builder:
        group[BUILDER_KEY] = builder


def collision_safe_name(requested: str) -> str:
    """Return Blender's next unused node-group name without creating data."""
    if bpy.data.node_groups.get(requested) is None:
        return requested
    index = 1
    while bpy.data.node_groups.get(f"{requested}.{index:03d}") is not None:
        index += 1
    return f"{requested}.{index:03d}"


def create_group_datablock(name: str, collision_policy: str, purpose: str):
    """Create or resolve a local GeometryNodeTree under an explicit collision policy."""
    existing = bpy.data.node_groups.get(name)
    if existing is not None:
        if collision_policy == "REUSE":
            return require_group(name), False
        if collision_policy == "ERROR":
            raise ValueError(f"Node group already exists: {name}")
        name = collision_safe_name(name)
    group = bpy.data.node_groups.new(name, "GeometryNodeTree")
    tag_group(group, purpose)
    return group, True


def interface_items(group) -> list[Any]:
    """Return the flattened, stable interface item tree."""
    return list(group.interface.items_tree)


def find_interface_item(group, identifier: str):
    """Resolve an interface item by its stable identifier."""
    item = next((item for item in interface_items(group) if getattr(item, "identifier", None) == identifier), None)
    if item is None:
        raise ValueError(f"Interface item '{identifier}' not found in '{group.name}'")
    return item


def find_panel(group, name_or_identifier: str):
    """Resolve one interface panel by stable identifier or exact name."""
    matches = [
        item
        for item in interface_items(group)
        if item.item_type == "PANEL"
        and (getattr(item, "identifier", None) == name_or_identifier or item.name == name_or_identifier)
    ]
    if len(matches) != 1:
        raise ValueError(f"Expected one interface panel '{name_or_identifier}', found {len(matches)}")
    return matches[0]


def serialize_value(value: Any) -> Any:
    """Convert an RNA value into a bounded JSON-compatible representation."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, mathutils.Matrix):
        return [list(row) for row in value]
    if isinstance(value, (mathutils.Vector, mathutils.Euler, mathutils.Quaternion, mathutils.Color)):
        return list(value)
    if hasattr(value, "bl_rna") and hasattr(value, "name"):
        return {"id_type": value.bl_rna.identifier, "name": value.name}
    try:
        return list(value)
    except (TypeError, ValueError):
        return str(value)


def serialize_socket(socket, include_default: bool = True) -> dict[str, Any]:
    """Describe a concrete node socket without treating its display name as identity."""
    data = {
        "identifier": socket.identifier,
        "name": socket.name,
        "bl_idname": socket.bl_idname,
        "enabled": socket.enabled,
        "hide": socket.hide,
        "is_linked": socket.is_linked,
        "is_multi_input": socket.is_multi_input,
    }
    if include_default and hasattr(socket, "default_value"):
        data["default_value"] = serialize_value(socket.default_value)
    return data


def socket_by_name(sockets, name: str, occurrence: int = 0):
    """Resolve a builder-owned socket by runtime display name."""
    matches = [socket for socket in sockets if socket.name == name]
    if occurrence >= len(matches):
        available = [socket.name for socket in sockets]
        raise ValueError(f"Socket '{name}' occurrence {occurrence} not found; available: {available}")
    return matches[occurrence]


def link(group, from_node, from_name: str, to_node, to_name: str, *, from_occurrence: int = 0, to_occurrence: int = 0):
    """Link builder-owned nodes using runtime-resolved socket names."""
    return group.links.new(
        socket_by_name(from_node.outputs, from_name, from_occurrence),
        socket_by_name(to_node.inputs, to_name, to_occurrence),
    )


def set_input(node, name: str, value: Any, occurrence: int = 0) -> None:
    """Set a builder-owned input default through the concrete socket."""
    socket = socket_by_name(node.inputs, name, occurrence)
    if not hasattr(socket, "default_value"):
        raise ValueError(f"Socket '{name}' on '{node.name}' has no default value")
    socket.default_value = value


def add_interface_socket(group, spec: dict[str, Any], panels: dict[str, Any] | None = None):
    """Create and configure one runtime-validated interface socket."""
    socket_type = spec["socket_type"]
    parent_name = spec.get("parent_panel")
    parent = (panels or {}).get(parent_name) if parent_name else None
    try:
        socket = group.interface.new_socket(
            name=spec["name"],
            description=spec.get("description", ""),
            in_out=spec["direction"],
            socket_type=socket_type,
            parent=parent,
        )
    except (RuntimeError, TypeError) as exc:
        raise ValueError(f"Unsupported interface socket type in this Blender runtime: {socket_type}") from exc
    for key in (
        "default_value",
        "min_value",
        "max_value",
        "subtype",
        "attribute_domain",
        "hide_value",
        "default_attribute_name",
    ):
        if key in spec and spec[key] is not None and hasattr(socket, key):
            prop = socket.bl_rna.properties.get(key)
            value = resolve_rna_value(prop, spec[key]) if prop is not None else spec[key]
            setattr(socket, key, value)
    return socket


def configure_role_flags(group, execution_role: str, geometry_types: list[str], tool_modes: list[str]) -> None:
    """Set a valid modifier/tool role and explicit applicability flags."""
    group.is_modifier = execution_role == "MODIFIER"
    group.is_tool = execution_role == "TOOL"
    for key in ("MESH", "CURVE", "POINTCLOUD", "GREASE_PENCIL"):
        setattr(group, f"is_type_{key.lower()}", key in geometry_types)
    for key in ("OBJECT", "EDIT", "SCULPT", "PAINT"):
        setattr(group, f"is_mode_{key.lower()}", execution_role == "TOOL" and key in tool_modes)
    if execution_role == "MODIFIER" and not geometry_types:
        group.is_type_mesh = True
        group.is_type_curve = True
        group.is_type_pointcloud = True
    if execution_role == "TOOL" and (not geometry_types or not tool_modes):
        raise ValueError("Tool groups require at least one geometry type and one tool mode")


def add_group_io(group) -> tuple[Any, Any]:
    """Add named Group Input and Group Output nodes."""
    input_node = group.nodes.new("NodeGroupInput")
    input_node.name = "Group Input"
    input_node.location = (-500, 0)
    output_node = group.nodes.new("NodeGroupOutput")
    output_node.name = "Group Output"
    output_node.is_active_output = True
    output_node.location = (500, 0)
    return input_node, output_node


def add_default_geometry_interface(group) -> tuple[Any, Any]:
    """Create the standard modifier geometry input/output pass-through contract."""
    input_socket = group.interface.new_socket(name="Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    output_socket = group.interface.new_socket(name="Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    return input_socket, output_socket


def initialize_group(
    name: str,
    *,
    purpose: str,
    execution_role: str = "MODIFIER",
    geometry_types: list[str] | None = None,
    tool_modes: list[str] | None = None,
    sockets: list[dict[str, Any]] | None = None,
    panels: list[dict[str, Any]] | None = None,
    description: str = "",
    color_tag: str = "NONE",
    collision_policy: str = "ERROR",
):
    """Create a fully tagged, role-configured group and its initial interface."""
    group, created = create_group_datablock(name, collision_policy, purpose)
    if not created:
        return group, False
    try:
        group.description = description
        if color_tag in group.bl_rna.properties["color_tag"].enum_items:
            group.color_tag = color_tag
        configure_role_flags(group, execution_role, geometry_types or [], tool_modes or [])
        panel_map = {}
        for panel_spec in panels or []:
            parent = panel_map.get(panel_spec.get("parent_panel"))
            panel = group.interface.new_panel(
                name=panel_spec["name"],
                description=panel_spec.get("description", ""),
                default_closed=panel_spec.get("default_closed", False),
                parent=parent,  # pyright: ignore[reportCallIssue]
            )
            panel_map[panel.name] = panel
        effective_sockets = sockets or []
        if execution_role == "MODIFIER" and not effective_sockets:
            effective_sockets = [
                {"name": "Geometry", "direction": "INPUT", "socket_type": "NodeSocketGeometry"},
                {"name": "Geometry", "direction": "OUTPUT", "socket_type": "NodeSocketGeometry"},
            ]
        for spec in effective_sockets:
            add_interface_socket(group, spec, panel_map)
        input_node, output_node = add_group_io(group)
        geometry_input = next(
            (socket for socket in input_node.outputs if socket.bl_idname == "NodeSocketGeometry"), None
        )
        geometry_output = next(
            (socket for socket in output_node.inputs if socket.bl_idname == "NodeSocketGeometry"), None
        )
        if geometry_input is not None and geometry_output is not None:
            group.links.new(geometry_input, geometry_output)
        return group, True
    except Exception:
        bpy.data.node_groups.remove(group, do_unlink=True)  # pyright: ignore[reportArgumentType]
        raise


def group_users(group) -> list[dict[str, Any]]:
    """List exact object modifier instances that use a node group."""
    users = []
    for obj in bpy.data.objects:
        for index, modifier in enumerate(obj.modifiers):
            if modifier.type == "NODES" and modifier.node_group == group:
                users.append({"object": obj.name, "modifier": modifier.name, "stack_index": index})
    return users


def group_dependencies(group) -> list[dict[str, str]]:
    """Return external datablocks referenced directly by graph RNA properties or socket defaults."""
    found = {}
    for node in group.nodes:
        for prop in node.bl_rna.properties:
            if prop.type != "POINTER" or prop.is_readonly or prop.identifier == "rna_type":
                continue
            try:
                value = getattr(node, prop.identifier)
            except (AttributeError, TypeError):
                continue
            if value is not None and hasattr(value, "name"):
                found[value.bl_rna.identifier, value.name] = {
                    "id_type": value.bl_rna.identifier,
                    "name": value.name,
                }
        for socket in node.inputs:
            value = getattr(socket, "default_value", None)
            if value is not None and hasattr(value, "name"):
                found[value.bl_rna.identifier, value.name] = {
                    "id_type": value.bl_rna.identifier,
                    "name": value.name,
                }
    return list(found.values())


def _layer_includes(layer_collection, obj) -> bool:
    """
    Report whether a view layer's collection tree includes obj, without waiting for a resync.

    `ViewLayer.objects` lags a link made earlier in the same command until the layer updates,
    and updating every layer of every scene to ask is a full evaluation each.

    Args:
        layer_collection: A view layer's layer collection, the root to start from.
        obj: The object to find.

    Returns:
        bool: Whether an included collection under this one holds obj.

    """
    if layer_collection.exclude:
        return False
    if layer_collection.collection.objects.get(obj.name) == obj:
        return True
    return any(_layer_includes(child, obj) for child in layer_collection.children)


def evaluation_layer(obj):
    """
    Name the scene and view layer whose dependency graph evaluates obj, preferring the context's.

    The context's graph evaluates the context view layer only: an object in another scene, or in
    collections that layer excludes, comes back from it as its base data with no modifier applied.

    Args:
        obj: The object to evaluate.

    Returns:
        tuple: The scene, whose playhead is the one that moves obj, and the view layer.

    Raises:
        ValueError: If no view layer of any scene includes obj.

    """
    context_layer = bpy.context.view_layer
    candidates = [(bpy.context.scene, context_layer)]
    candidates += [(scene, layer) for scene in obj.users_scene for layer in scene.view_layers if layer != context_layer]
    for scene, layer in candidates:
        if _layer_includes(layer.layer_collection, obj):
            return scene, layer
    raise ValueError(
        f"Object '{obj.name}' is in no view layer, so Blender evaluates none of its modifiers; link it into a "
        "collection that a view layer includes"
    )


def evaluated_depsgraph(view_layer):
    """
    Return a view layer's dependency graph, evaluated at its scene's current frame.

    Args:
        view_layer: A layer from `evaluation_layer`.

    Returns:
        bpy.types.Depsgraph: The evaluated graph.

    """
    if view_layer == bpy.context.view_layer:
        return bpy.context.evaluated_depsgraph_get()
    view_layer.update()
    return view_layer.depsgraph


def evaluated_summary(obj, depsgraph=None) -> dict[str, Any]:
    """
    Return bounded evaluated counts and world-space bounds without applying modifiers.

    Args:
        obj: The object to read.
        depsgraph: The graph to read it from, already at the caller's frame; resolved from the
            view layer that evaluates obj when omitted.

    Returns:
        dict: world_bounds and mesh_counts (or mesh_unavailable), or only evaluation_unavailable
        when no view layer evaluates obj - an object outside every view layer is still a valid
        modifier host, so a builder that just attached one must not fail for it.

    """
    if depsgraph is None:
        try:
            _scene, view_layer = evaluation_layer(obj)
        except ValueError as exc:
            return {"evaluation_unavailable": str(exc)}
        depsgraph = evaluated_depsgraph(view_layer)
    evaluated = obj.evaluated_get(depsgraph)
    bounds = [evaluated.matrix_world @ mathutils.Vector(corner) for corner in evaluated.bound_box]
    summary: dict[str, Any] = {
        "world_bounds": {
            "min": [min(point[axis] for point in bounds) for axis in range(3)] if bounds else None,
            "max": [max(point[axis] for point in bounds) for axis in range(3)] if bounds else None,
        }
    }
    mesh = None
    try:
        mesh = evaluated.to_mesh()
        summary["mesh_counts"] = {
            "vertices": len(mesh.vertices),
            "edges": len(mesh.edges),
            "faces": len(mesh.polygons),
        }
    except RuntimeError as exc:
        summary["mesh_unavailable"] = str(exc)
    finally:
        if mesh is not None:
            evaluated.to_mesh_clear()
    return summary


def applicable_to_object(group, obj) -> bool:
    """Check the group's explicit Blender applicability flags against an object."""
    mapping = {
        "MESH": "is_type_mesh",
        "CURVE": "is_type_curve",
        "POINTCLOUD": "is_type_pointcloud",
        "GREASEPENCIL": "is_type_grease_pencil",
    }
    flag = mapping.get(obj.type)
    return bool(flag and getattr(group, flag, False))


def pagination(records: list[Any], offset: int, limit: int, max_limit: int = 500) -> dict[str, Any]:
    """Page a list using the repository's common pagination contract."""
    start, end, truncated, next_offset = paginate(len(records), offset, limit, max_limit)
    return {
        "items": records[start:end],
        "total_count": len(records),
        "returned_count": end - start,
        "offset": start,
        "limit": min(max(1, limit), max_limit),
        "truncated": truncated,
        "next_offset": next_offset,
    }


def finite_bounds(bounds: dict[str, Any]) -> bool:
    """Return whether every reported bound component is finite."""
    values = [*(bounds.get("min") or []), *(bounds.get("max") or [])]
    return bool(values) and all(math.isfinite(value) for value in values)


def modifier_input_state(modifier, identifier: str) -> dict[str, Any] | None:
    """Read one modifier input across Blender's 5.1 and newer interface APIs."""
    inputs = getattr(getattr(modifier, "properties", None), "inputs", None)
    runtime_input = getattr(inputs, identifier, None) if inputs is not None else None
    if runtime_input is not None:
        if not hasattr(runtime_input, "value"):
            return None
        return {
            "value": serialize_value(runtime_input.value),
            "use_attribute": runtime_input.type == "ATTRIBUTE",
            "attribute_name": runtime_input.attribute_name or None,
        }
    try:
        keys = modifier.keys()
    except TypeError:
        return None
    if identifier not in keys:
        return None
    return {
        "value": serialize_value(modifier[identifier]),
        "use_attribute": bool(modifier.get(f"{identifier}_use_attribute", False)),
        "attribute_name": modifier.get(f"{identifier}_attribute_name"),
    }


def set_modifier_input(modifier, identifier: str, *, value: Any = None, attribute_name: str | None = None) -> None:
    """Write one modifier input across Blender's 5.1 and newer interface APIs."""
    inputs = getattr(getattr(modifier, "properties", None), "inputs", None)
    runtime_input = getattr(inputs, identifier, None) if inputs is not None else None
    if runtime_input is not None:
        if attribute_name is not None:
            runtime_input.type = "ATTRIBUTE"
            runtime_input.attribute_name = attribute_name
        else:
            runtime_input.type = "VALUE"
            runtime_input.value = value
        return
    if attribute_name is not None:
        modifier[f"{identifier}_use_attribute"] = True
        modifier[f"{identifier}_attribute_name"] = attribute_name
    else:
        modifier[identifier] = value
        if f"{identifier}_use_attribute" in modifier:
            modifier[f"{identifier}_use_attribute"] = False
