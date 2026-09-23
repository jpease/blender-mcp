"""Read-only Geometry Nodes discovery, graph inspection, evaluation, and validation."""

# ruff: file-ignore[line-too-long]

import contextlib

from typing import Any

import bpy

from ._shared import (
    BUILDER_KEY,
    OWNERSHIP_KEY,
    PURPOSE_KEY,
    SCHEMA_KEY,
    applicable_to_object,
    evaluated_summary,
    find_group,
    finite_bounds,
    group_dependencies,
    group_users,
    modifier_input_state,
    pagination,
    require_object,
    serialize_socket,
    serialize_value,
)


def _interface_record(item) -> dict[str, Any]:
    """Serialize one interface socket or panel with its stable identity."""
    record = {
        "item_type": item.item_type,
        "name": item.name,
        "identifier": getattr(item, "identifier", None),
        "position": item.position,
        "parent_identifier": getattr(getattr(item, "parent", None), "identifier", None),
        "description": getattr(item, "description", ""),
    }
    if item.item_type == "SOCKET":
        record.update(
            {
                "in_out": item.in_out,
                "socket_type": item.socket_type,
                "bl_socket_idname": item.bl_socket_idname,
                "attribute_domain": getattr(item, "attribute_domain", None),
                "hide_value": getattr(item, "hide_value", None),
            }
        )
        for key in ("default_value", "min_value", "max_value", "subtype", "default_attribute_name"):
            if hasattr(item, key):
                record[key] = serialize_value(getattr(item, key))
    else:
        record["default_closed"] = getattr(item, "default_closed", False)
    return record


def _group_identity(group) -> dict[str, Any]:
    """Serialize one GeometryNodeTree's sharing, role, and ownership state."""
    users = group_users(group)
    return {
        "name": group.name,
        "session_uid": group.session_uid,
        "users": group.users,
        "modifier_users": users,
        "shared": len(users) > 1,
        "orphaned": group.users == 0,
        "library": group.library.filepath if group.library else None,
        "is_editable": group.is_editable,
        "is_asset": group.asset_data is not None,
        "use_fake_user": group.use_fake_user,
        "description": group.description,
        "flags": {
            "is_modifier": group.is_modifier,
            "is_tool": group.is_tool,
            "is_type_mesh": group.is_type_mesh,
            "is_type_curve": group.is_type_curve,
            "is_type_pointcloud": group.is_type_pointcloud,
            "is_type_grease_pencil": group.is_type_grease_pencil,
            "is_mode_object": group.is_mode_object,
            "is_mode_edit": group.is_mode_edit,
            "is_mode_sculpt": group.is_mode_sculpt,
            "is_mode_paint": group.is_mode_paint,
        },
        "mcp": {
            "owned": OWNERSHIP_KEY in group,
            "uuid": group.get(OWNERSHIP_KEY),
            "schema_version": group.get(SCHEMA_KEY),
            "purpose": group.get(PURPOSE_KEY),
            "builder": group.get(BUILDER_KEY),
        },
    }


def _node_record(node) -> dict[str, Any]:
    """Serialize one graph node and relevant writable RNA settings."""
    properties = {}
    for prop in node.bl_rna.properties:
        if prop.is_readonly or prop.identifier in {
            "rna_type",
            "name",
            "label",
            "location",
            "dimensions",
            "width",
            "height",
            "parent",
            "mute",
            "hide",
            "select",
        }:
            continue
        if prop.type not in {"BOOLEAN", "INT", "FLOAT", "STRING", "ENUM", "POINTER"}:
            continue
        with contextlib.suppress(AttributeError, TypeError, ValueError):
            value = getattr(node, prop.identifier)
            if prop.type != "POINTER" or value is not None:
                properties[prop.identifier] = serialize_value(value)
    record = {
        "name": node.name,
        "label": node.label,
        "bl_idname": node.bl_idname,
        "parent": node.parent.name if node.parent else None,
        "location": list(node.location),
        "dimensions": list(node.dimensions),
        "width": node.width,
        "height": node.height,
        "mute": node.mute,
        "hide": node.hide,
        "properties": properties,
        "inputs": [serialize_socket(socket) for socket in node.inputs],
        "outputs": [serialize_socket(socket, include_default=False) for socket in node.outputs],
        "mcp_role": node.get("blender_mcp_role"),
    }
    if node.bl_idname in {"GeometryNodeRepeatInput", "GeometryNodeSimulationInput"}:
        paired = node.paired_output
        record["zone"] = {
            "role": "INPUT",
            "paired_node": paired.name if paired else None,
            "paired": paired is not None,
        }
    elif node.bl_idname in {"GeometryNodeRepeatOutput", "GeometryNodeSimulationOutput"}:
        items = node.repeat_items if node.bl_idname == "GeometryNodeRepeatOutput" else node.state_items
        sockets = [socket for socket in node.outputs if socket.identifier != "__extend__"]
        paired_input = next(
            (
                candidate
                for candidate in node.id_data.nodes
                if candidate.bl_idname in {"GeometryNodeRepeatInput", "GeometryNodeSimulationInput"}
                and candidate.paired_output == node
            ),
            None,
        )
        state_records = []
        for index, item in enumerate(items):
            socket = sockets[index] if index < len(sockets) else None
            state_records.append(
                {
                    "name": item.name,
                    "socket_type": item.socket_type,
                    "socket_identifier": socket.identifier if socket else None,
                    **({"attribute_domain": item.attribute_domain} if hasattr(item, "attribute_domain") else {}),
                }
            )
        record["zone"] = {
            "role": "OUTPUT",
            "paired_node": paired_input.name if paired_input else None,
            "paired": paired_input is not None,
            "state_items": state_records,
            "intended_frame_range": {
                "start": node.get("blender_mcp_frame_start"),
                "end": node.get("blender_mcp_frame_end"),
            }
            if node.bl_idname == "GeometryNodeSimulationOutput"
            else None,
        }
    return record


def _modifier_record(obj, modifier) -> dict[str, Any]:
    """Serialize one modifier instance and its exposed input overrides."""
    overrides = {}
    group = modifier.node_group
    if group is not None:
        for item in group.interface.items_tree:
            if item.item_type != "SOCKET" or item.in_out != "INPUT":
                continue
            identifier = item.identifier
            state = modifier_input_state(modifier, identifier)
            if state is not None:
                overrides[identifier] = {
                    "name": item.name,
                    **state,
                }
    warnings = []
    for warning in getattr(modifier, "node_warnings", ()):
        warnings.append({"type": warning.type, "message": warning.message, "node": warning.node_name})
    return {
        "object": obj.name,
        "modifier": modifier.name,
        "show_viewport": modifier.show_viewport,
        "show_render": modifier.show_render,
        "execution_time": getattr(modifier, "execution_time", None),
        "inputs": overrides,
        "bakes": [
            {
                "bake_id": bake.bake_id,
                "node": bake.node.name if bake.node else None,
                "node_type": bake.node.bl_idname if bake.node else None,
                "directory": bake.directory,
                "use_custom_path": bake.use_custom_path,
                "bake_mode": bake.bake_mode,
                "bake_target": bake.bake_target,
                "frame_start": bake.frame_start,
                "frame_end": bake.frame_end,
                "use_custom_simulation_frame_range": bake.use_custom_simulation_frame_range,
            }
            for bake in getattr(modifier, "bakes", ())
        ],
        "warnings": warnings,
    }


class GeometryNodesInspectionHandlersMixin:
    """Provide bounded, non-mutating inspection and validation commands."""

    def list_procedural_systems(self, limit=50, offset=0, include_orphans=True):
        records = []
        for group in bpy.data.node_groups:
            if group.bl_idname != "GeometryNodeTree":
                continue
            record = _group_identity(group)
            if not include_orphans and record["orphaned"]:
                continue
            record["interface"] = [_interface_record(item) for item in group.interface.items_tree]
            records.append(record)
        records.sort(key=lambda item: item["name"].casefold())
        result = pagination(records, offset, limit, 200)
        result["systems"] = result.pop("items")
        return result

    def get_geometry_node_graph(self, node_group_name, sections=None, limit=100, offset=0):
        group = find_group(node_group_name)
        selected = set(sections or ["IDENTITY", "INTERFACE", "NODES", "LINKS", "MODIFIERS", "WARNINGS"])
        data: dict[str, Any] = {"node_group": group.name, "sections": sorted(selected)}
        if "IDENTITY" in selected:
            data["identity"] = _group_identity(group)
            data["dependencies"] = group_dependencies(group)
        if "INTERFACE" in selected:
            data["interface"] = [_interface_record(item) for item in group.interface.items_tree]
        graph_records = []
        if "NODES" in selected:
            graph_records.extend({"kind": "NODE", **_node_record(node)} for node in group.nodes)
        if "LINKS" in selected:
            graph_records.extend(
                {
                    "kind": "LINK",
                    "from_node": link.from_node.name,
                    "from_socket_identifier": link.from_socket.identifier,
                    "from_socket_index": list(link.from_node.outputs).index(link.from_socket),
                    "to_node": link.to_node.name,
                    "to_socket_identifier": link.to_socket.identifier,
                    "to_socket_index": list(link.to_node.inputs).index(link.to_socket),
                    "is_muted": link.is_muted,
                    "is_valid": link.is_valid,
                }
                for link in group.links
            )
        page = pagination(graph_records, offset, limit, 500)
        data["graph_items"] = page.pop("items")
        data["pagination"] = page
        if "MODIFIERS" in selected or "WARNINGS" in selected:
            modifiers = []
            for obj in bpy.data.objects:
                modifiers.extend(
                    _modifier_record(obj, modifier)
                    for modifier in obj.modifiers
                    if modifier.type == "NODES" and modifier.node_group == group
                )
            data["modifiers"] = modifiers
        return data

    def get_geometry_node_type_info(self, bl_idname=None, search=None, category=None, limit=50, offset=0):
        candidates = []
        for name in dir(bpy.types):
            cls = getattr(bpy.types, name)
            node_id = getattr(cls, "bl_rna", None)
            identifier = getattr(node_id, "identifier", "")
            if not identifier.startswith(("GeometryNode", "ShaderNode", "FunctionNode", "NodeGroup", "NodeFrame")):
                continue
            if (
                search is not None
                and search.casefold() not in f"{identifier} {getattr(cls, 'bl_label', '')}".casefold()
            ):
                continue
            inferred_category = identifier.removeprefix("GeometryNode").split("_")[0]
            if category is not None and category.casefold() not in inferred_category.casefold():
                continue
            candidates.append(identifier)
        candidates = sorted(set(candidates))
        if bl_idname is not None:
            candidates = [bl_idname]
        records = []
        temporary = bpy.data.node_groups.new("__BlenderMCP_NodeTypeProbe__", "GeometryNodeTree")
        try:
            for identifier in candidates:
                cls = getattr(bpy.types, identifier, None)
                available = cls is not None
                record = {
                    "bl_idname": identifier,
                    "available": available,
                    "label": getattr(cls, "bl_label", "") if cls else "",
                    "creatable": False,
                    "properties": [],
                    "inputs": [],
                    "outputs": [],
                }
                if available:
                    record["properties"] = [
                        {
                            "identifier": prop.identifier,
                            "type": prop.type,
                            "readonly": prop.is_readonly,
                            "description": prop.description,
                        }
                        for prop in cls.bl_rna.properties
                        if prop.identifier != "rna_type"
                    ]
                    try:
                        node = temporary.nodes.new(identifier)
                        record["creatable"] = True
                        record["inputs"] = [serialize_socket(socket) for socket in node.inputs]
                        record["outputs"] = [serialize_socket(socket, include_default=False) for socket in node.outputs]
                        temporary.nodes.remove(node)
                    except RuntimeError as exc:
                        record["creation_error"] = str(exc)
                records.append(record)
        finally:
            bpy.data.node_groups.remove(temporary)
        if bl_idname is not None:
            if not records:
                return {"bl_idname": bl_idname, "available": False, "creatable": False}
            return records[0]
        result = pagination(records, offset, limit, 200)
        result["node_types"] = result.pop("items")
        return result

    def inspect_evaluated_geometry(self, object_name, frame=None, instance_limit=500):
        """Read any object's evaluated result at a frame, restoring the playhead afterwards."""
        obj = require_object(object_name)
        scene = bpy.context.scene
        previous_frame = scene.frame_current
        try:
            if frame is not None:
                scene.frame_set(frame)
            data = evaluated_summary(obj)
            evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
            data.update(
                {
                    "object": obj.name,
                    "frame": scene.frame_current,
                    "source_type": obj.type,
                    "evaluated_type": evaluated.type,
                    "materials": [slot.material.name if slot.material else None for slot in evaluated.material_slots],
                }
            )
            mesh = None
            try:
                mesh = evaluated.to_mesh()
                data["attributes"] = [
                    {"name": attribute.name, "data_type": attribute.data_type, "domain": attribute.domain}
                    for attribute in mesh.attributes
                ]
            except RuntimeError:
                data["attributes"] = []
            finally:
                if mesh is not None:
                    evaluated.to_mesh_clear()
            instances = []
            total_instances = 0
            for instance in bpy.context.evaluated_depsgraph_get().object_instances:
                if instance.parent is None or instance.parent.original != obj:
                    continue
                total_instances += 1
                if len(instances) < instance_limit:
                    instances.append(
                        {
                            "object": instance.object.original.name,
                            "persistent_id": list(instance.persistent_id),
                            "world_matrix": [list(row) for row in instance.matrix_world],
                        }
                    )
            data["instances"] = {
                "total_count": total_instances,
                "returned_count": len(instances),
                "truncated": total_instances > len(instances),
                "items": instances,
            }
            data["limitations"] = [
                "Object.to_mesh exposes mesh-representable evaluated output; separate non-mesh components may not be enumerable."
            ]
            return data
        finally:
            if scene.frame_current != previous_frame:
                scene.frame_set(previous_frame)

    def validate_geometry_node_graph(self, node_group_name, object_names=None, topology_warning_threshold=1_000_000):
        group = find_group(node_group_name)
        findings = []

        def finding(severity, code, message, **location):
            findings.append({"severity": severity, "code": code, "message": message, **location})

        if group.library is not None or not group.is_editable:
            finding("ERROR", "READ_ONLY_GROUP", "The group is library-linked or read-only.", group=group.name)
        names = [item.name for item in group.interface.items_tree if item.item_type == "SOCKET"]
        for duplicate in sorted({name for name in names if names.count(name) > 1}):
            finding(
                "WARNING",
                "DUPLICATE_INTERFACE_NAME",
                f"Exposed name '{duplicate}' is duplicated; use stable identifiers when setting inputs.",
                group=group.name,
            )
        outputs = [node for node in group.nodes if node.bl_idname == "NodeGroupOutput" and node.is_active_output]
        if not outputs:
            finding("ERROR", "MISSING_ACTIVE_OUTPUT", "The graph has no active Group Output node.", group=group.name)
        else:
            geometry_inputs = [socket for socket in outputs[0].inputs if socket.bl_idname == "NodeSocketGeometry"]
            if geometry_inputs and not any(socket.is_linked for socket in geometry_inputs):
                finding(
                    "WARNING",
                    "UNLINKED_GEOMETRY_OUTPUT",
                    "The active geometry output is unlinked and will produce empty geometry.",
                    node=outputs[0].name,
                )
        for link in group.links:
            if not link.is_valid:
                finding(
                    "ERROR",
                    "INVALID_LINK",
                    "A graph link is invalid for its current socket types.",
                    node=link.to_node.name,
                    socket=link.to_socket.identifier,
                )
        targets = []
        for user in group_users(group):
            if object_names is not None and user["object"] not in object_names:
                continue
            targets.append((require_object(user["object"]), user["modifier"]))
        if object_names is not None:
            missing_users = set(object_names) - {obj.name for obj, _modifier in targets}
            for name in sorted(missing_users):
                finding("ERROR", "MISSING_MODIFIER_USER", f"'{name}' does not use this group.", affected_user=name)
        for obj, modifier_name in targets:
            if not applicable_to_object(group, obj):
                finding(
                    "ERROR",
                    "UNSUPPORTED_OBJECT_TYPE",
                    f"Group flags do not allow object type {obj.type}.",
                    affected_user=obj.name,
                    modifier=modifier_name,
                )
                continue
            modifier = obj.modifiers.get(modifier_name)
            for warning in getattr(modifier, "node_warnings", ()):
                finding(
                    warning.type,
                    "BLENDER_NODE_WARNING",
                    warning.message,
                    node=warning.node_name,
                    affected_user=obj.name,
                )
            try:
                summary = evaluated_summary(obj)
                counts = summary.get("mesh_counts", {})
                if counts.get("vertices", 0) == 0:
                    finding(
                        "WARNING", "EMPTY_OUTPUT", "Evaluated output contains no mesh vertices.", affected_user=obj.name
                    )
                if counts.get("vertices", 0) > topology_warning_threshold:
                    finding(
                        "WARNING",
                        "EXTREME_TOPOLOGY",
                        f"Evaluated vertex count exceeds {topology_warning_threshold}.",
                        affected_user=obj.name,
                    )
                if not finite_bounds(summary.get("world_bounds", {})):
                    finding(
                        "ERROR",
                        "NONFINITE_BOUNDS",
                        "Evaluated world bounds are empty or non-finite.",
                        affected_user=obj.name,
                    )
            except Exception as exc:
                finding("ERROR", "EVALUATION_FAILED", str(exc), affected_user=obj.name, modifier=modifier_name)
            if any(abs(component - 1.0) > 1e-6 for component in obj.scale):
                finding(
                    "INFO",
                    "UNAPPLIED_SCALE",
                    "Object scale is not [1,1,1]; confirm the graph's local-space behavior is intentional.",
                    affected_user=obj.name,
                )
        severities = {"ERROR": 0, "WARNING": 0, "INFO": 0}
        for item in findings:
            severities[item["severity"]] = severities.get(item["severity"], 0) + 1
        return {
            "node_group": group.name,
            "valid": severities["ERROR"] == 0,
            "summary": severities,
            "findings": findings,
            "note": "Structural and evaluated checks do not establish artistic or geometric correctness.",
        }
