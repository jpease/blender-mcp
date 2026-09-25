"""Live Geometry Nodes modifier lifecycle and exposed-input handlers."""

from typing import Any

import bpy

from ...helpers import apply_modifier
from ._shared import (
    OWNERSHIP_KEY,
    applicable_to_object,
    evaluated_summary,
    group_dependencies,
    group_users,
    initialize_group,
    require_group,
    require_nodes_modifier,
    require_object,
    serialize_value,
    set_modifier_input,
    tag_group,
)


def _input_items(group) -> dict[str, Any]:
    """Map stable identifiers to exposed input interface sockets."""
    return {
        item.identifier: item
        for item in group.interface.items_tree
        if item.item_type == "SOCKET" and item.in_out == "INPUT"
    }


def _resolve_datablock_value(item, value):
    """Resolve ID-backed socket values from an explicit type/name record."""
    if not isinstance(value, dict):
        return value
    socket_type = item.socket_type
    collections = {
        "NodeSocketObject": bpy.data.objects,
        "NodeSocketCollection": bpy.data.collections,
        "NodeSocketMaterial": bpy.data.materials,
        "NodeSocketImage": bpy.data.images,
        "NodeSocketTexture": bpy.data.textures,
    }
    collection = collections.get(socket_type)
    name = value.get("name")
    if collection is None or not isinstance(name, str):
        raise ValueError(f"Socket '{item.name}' expects {socket_type}, not a datablock reference record")
    resolved = collection.get(name)
    if resolved is None:
        raise ValueError(f"Referenced {socket_type.removeprefix('NodeSocket')} not found: {name}")
    return resolved


def _normalize_input(item, spec: dict[str, Any]) -> dict[str, Any]:
    """Prevalidate one exposed input update and resolve its final value."""
    mode = spec.get("mode", "VALUE")
    if mode == "ATTRIBUTE":
        attribute_name = spec.get("attribute_name")
        if not attribute_name:
            raise ValueError(f"Attribute mode for '{item.name}' requires attribute_name")
        if item.socket_type in {"NodeSocketGeometry", "NodeSocketObject", "NodeSocketCollection", "NodeSocketMaterial"}:
            raise ValueError(f"Socket '{item.name}' ({item.socket_type}) does not support named-attribute mode")
        return {"identifier": item.identifier, "mode": mode, "attribute_name": attribute_name}
    return {
        "identifier": item.identifier,
        "mode": mode,
        "value": _resolve_datablock_value(item, spec.get("value")),
    }


def _set_modifier_inputs(modifier, specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate all exposed inputs before assigning any of them."""
    group = modifier.node_group
    if group is None:
        raise ValueError(f"Modifier '{modifier.name}' has no node group")
    items = _input_items(group)
    normalized = []
    for spec in specs:
        identifier = spec["identifier"]
        item = items.get(identifier)
        if item is None:
            raise ValueError(f"Input identifier '{identifier}' not found on '{group.name}'")
        normalized.append((item, _normalize_input(item, spec)))
    changed = []
    for item, spec in normalized:
        identifier = item.identifier
        if spec["mode"] == "ATTRIBUTE":
            set_modifier_input(modifier, identifier, attribute_name=spec["attribute_name"])
            value = {"mode": "ATTRIBUTE", "attribute_name": spec["attribute_name"]}
        else:
            set_modifier_input(modifier, identifier, value=spec["value"])
            value = {"mode": "VALUE", "value": serialize_value(spec["value"])}
        changed.append({"identifier": identifier, "name": item.name, **value})
    return changed


def _stack_order(obj) -> list[str]:
    """Return the object's exact modifier order."""
    return [modifier.name for modifier in obj.modifiers]


def _raw_input_state(modifier, identifier: str) -> dict[str, Any]:
    """Capture one modifier input in a form that can be restored after batch failure."""
    inputs = getattr(getattr(modifier, "properties", None), "inputs", None)
    runtime_input = getattr(inputs, identifier, None) if inputs is not None else None
    if runtime_input is not None and hasattr(runtime_input, "value"):
        value = runtime_input.value.copy() if hasattr(runtime_input.value, "copy") else runtime_input.value
        return {
            "api": "RNA",
            "type": runtime_input.type,
            "attribute_name": runtime_input.attribute_name,
            "value": value,
        }
    keys = set(modifier.keys())
    return {
        "api": "ID_PROPERTY",
        "present": identifier in keys,
        "value": modifier.get(identifier),
        "use_attribute": modifier.get(f"{identifier}_use_attribute"),
        "attribute_name": modifier.get(f"{identifier}_attribute_name"),
    }


def _restore_raw_input(modifier, identifier: str, state: dict[str, Any]) -> None:
    """Restore a modifier input captured by ``_raw_input_state``."""
    if state["api"] == "RNA":
        runtime_input = getattr(modifier.properties.inputs, identifier)
        runtime_input.value = state["value"]
        runtime_input.type = state["type"]
        runtime_input.attribute_name = state["attribute_name"]
        return
    if state["present"]:
        modifier[identifier] = state["value"]
    elif identifier in modifier:
        del modifier[identifier]
    for suffix, key in (("_use_attribute", "use_attribute"), ("_attribute_name", "attribute_name")):
        property_name = f"{identifier}{suffix}"
        if state[key] is not None:
            modifier[property_name] = state[key]
        elif property_name in modifier:
            del modifier[property_name]


def _move_modifier(obj, modifier, stack_index) -> None:
    """Move a modifier to an exact, validated stack index."""
    if stack_index is None:
        raise ValueError("MOVE requires stack_index")
    if not 0 <= int(stack_index) < len(obj.modifiers):
        raise ValueError(f"stack_index must be between 0 and {len(obj.modifiers) - 1}")
    current = list(obj.modifiers).index(modifier)
    obj.modifiers.move(current, int(stack_index))


def _set_modifier_visibility(modifier, show_viewport, show_render) -> None:
    """Set whichever of the viewport and render visibility flags were supplied."""
    if show_viewport is None and show_render is None:
        raise ValueError("SET_VISIBILITY requires show_viewport and/or show_render")
    if show_viewport is not None:
        modifier.show_viewport = show_viewport
    if show_render is not None:
        modifier.show_render = show_render


def _replace_modifier_group(obj, modifier, replacement_group_name) -> None:
    """Point a modifier at another node group that supports the object's type."""
    if not replacement_group_name:
        raise ValueError("REPLACE_GROUP requires replacement_group_name")
    replacement = require_group(replacement_group_name)
    if not applicable_to_object(replacement, obj):
        raise ValueError(f"Replacement group does not support {obj.type}")
    modifier.node_group = replacement


def _commit_modifier(obj, modifier, action: str, confirm_destructive) -> None:
    """Remove or apply a modifier, only when the caller confirmed the destructive action."""
    if not confirm_destructive:
        raise ValueError(f"confirm_destructive=True is required for {action}")
    if action == "REMOVE":
        obj.modifiers.remove(modifier)
    else:
        apply_modifier(obj, modifier)


class GeometryNodesModifierHandlersMixin:
    """Attach, configure, copy, and explicitly commit live procedural modifiers."""

    def attach_geometry_nodes_modifier(
        self,
        object_name,
        node_group_name=None,
        new_group_name=None,
        modifier_name="GeometryNodes",
        stack_index=None,
        show_viewport=True,
        show_render=True,
        single_user=False,
        input_values=None,
    ):
        obj = require_object(object_name)
        if (node_group_name is None) == (new_group_name is None):
            raise ValueError("Provide exactly one of node_group_name and new_group_name")
        created_group = False
        if new_group_name is not None:
            group, created_group = initialize_group(
                new_group_name,
                purpose="attached pass-through procedural system",
                execution_role="MODIFIER",
                geometry_types=[obj.type if obj.type != "GREASEPENCIL" else "GREASE_PENCIL"],
            )
        else:
            if node_group_name is None:
                raise ValueError("node_group_name is required")
            group = require_group(node_group_name)
        if not group.is_modifier:
            raise ValueError(f"Node group '{group.name}' is not marked for modifier execution")
        if not applicable_to_object(group, obj):
            raise ValueError(f"Node group '{group.name}' does not support object type {obj.type}")
        if stack_index is not None and not 0 <= int(stack_index) <= len(obj.modifiers):
            raise ValueError(f"stack_index must be between 0 and {len(obj.modifiers)}")
        if single_user and group.users > 0 and not created_group:
            source_uuid_value = group.get(OWNERSHIP_KEY)
            source_uuid = str(source_uuid_value) if source_uuid_value is not None else None
            group = group.copy()
            group.name = f"{group.name} ({obj.name})"
            tag_group(group, "single-user modifier group", source_uuid=source_uuid)
            created_group = True
        modifier = None
        try:
            if obj.modifiers.get(modifier_name) is not None:
                raise ValueError(f"Modifier already exists on '{obj.name}': {modifier_name}")
            modifier = obj.modifiers.new(name=modifier_name, type="NODES")
            modifier.node_group = group
            modifier.show_viewport = show_viewport
            modifier.show_render = show_render
            if stack_index is not None:
                obj.modifiers.move(len(obj.modifiers) - 1, int(stack_index))
            changed_inputs = _set_modifier_inputs(modifier, input_values or [])
            bpy.context.view_layer.update()
            return {
                "object": obj.name,
                "modifier": modifier.name,
                "node_group": group.name,
                "modifier_stack": _stack_order(obj),
                "group_users": group_users(group),
                "inputs": changed_inputs,
                "base_counts": {
                    "vertices": len(obj.data.vertices) if obj.type == "MESH" else None,
                    "edges": len(obj.data.edges) if obj.type == "MESH" else None,
                    "faces": len(obj.data.polygons) if obj.type == "MESH" else None,
                },
                "evaluated": evaluated_summary(obj),
                "live_dependencies": group_dependencies(group),
                "changed_objects": [obj.name],
                "changed_resources": [group.name] if created_group else [],
            }
        except Exception:
            if modifier is not None and modifier.name in obj.modifiers:
                obj.modifiers.remove(modifier)
            if created_group and group.users == 0:
                bpy.data.node_groups.remove(group, do_unlink=True)  # pyright: ignore[reportArgumentType]
            raise

    def set_geometry_nodes_inputs(self, targets):
        prepared = []
        for target in targets:
            obj = require_object(target["object_name"])
            modifier = require_nodes_modifier(obj, target["modifier_name"])
            items = _input_items(modifier.node_group)
            normalized = []
            for spec in target["inputs"]:
                item = items.get(spec["identifier"])
                if item is None:
                    raise ValueError(f"Input '{spec['identifier']}' not found on {obj.name}/{modifier.name}")
                normalized.append((item, _normalize_input(item, spec)))
            prepared.append((obj, modifier, normalized))
        updates = []
        snapshots = [
            (modifier, spec["identifier"], _raw_input_state(modifier, spec["identifier"]))
            for _obj, modifier, normalized in prepared
            for _item, spec in normalized
        ]
        try:
            for obj, modifier, normalized in prepared:
                specs = [spec for _item, spec in normalized]
                updates.append(
                    {
                        "object": obj.name,
                        "modifier": modifier.name,
                        "inputs": _set_modifier_inputs(modifier, specs),
                    }
                )
        except Exception:
            for modifier, identifier, state in reversed(snapshots):
                _restore_raw_input(modifier, identifier, state)
            raise
        bpy.context.view_layer.update()
        objects = sorted({obj.name for obj, _modifier, _normalized in prepared})
        return {"updates": updates, "changed_objects": objects}

    def manage_geometry_nodes_modifier(
        self,
        object_name,
        modifier_name,
        action,
        new_name=None,
        stack_index=None,
        show_viewport=None,
        show_render=None,
        mute=None,
        replacement_group_name=None,
        confirm_destructive=False,
    ):
        obj = require_object(object_name)
        modifier = require_nodes_modifier(obj, modifier_name)
        old_order = _stack_order(obj)
        old_group = modifier.node_group
        if action == "RENAME":
            if not new_name:
                raise ValueError("RENAME requires new_name")
            modifier.name = new_name
        elif action == "MOVE":
            _move_modifier(obj, modifier, stack_index)
        elif action == "SET_VISIBILITY":
            _set_modifier_visibility(modifier, show_viewport, show_render)
        elif action == "MUTE":
            if mute is None:
                raise ValueError("MUTE requires mute")
            modifier.show_viewport = not mute
            modifier.show_render = not mute
        elif action == "REPLACE_GROUP":
            _replace_modifier_group(obj, modifier, replacement_group_name)
        elif action in {"REMOVE", "APPLY"}:
            _commit_modifier(obj, modifier, action, confirm_destructive)
        else:
            raise ValueError(f"Unsupported modifier action: {action}")
        bpy.context.view_layer.update()
        return {
            "object": obj.name,
            "action": action,
            "old_stack_order": old_order,
            "new_stack_order": _stack_order(obj),
            "old_group": old_group.name if old_group else None,
            "group_users": group_users(old_group) if old_group and old_group.name in bpy.data.node_groups else [],
            "evaluated": evaluated_summary(obj),
            "topology_indices_stale": action == "APPLY",
            "changed_objects": [obj.name],
        }

    def copy_geometry_node_group(
        self,
        node_group_name,
        new_name,
        reassign_modifiers=None,
        duplicate_object_name=None,
        duplicated_object_name=None,
        copy_object_data=False,
        copy_action=False,
        reassign_duplicate_modifiers=True,
        collision_policy="ERROR",
    ):
        source = require_group(node_group_name)
        if bpy.data.node_groups.get(new_name) is not None:
            if collision_policy == "ERROR":
                raise ValueError(f"Node group already exists: {new_name}")
            from ._shared import collision_safe_name

            new_name = collision_safe_name(new_name)
        targets = []
        for record in reassign_modifiers or []:
            obj = require_object(record["object_name"])
            modifier = require_nodes_modifier(obj, record["modifier_name"])
            if modifier.node_group != source:
                raise ValueError(f"{obj.name}/{modifier.name} does not use '{source.name}'")
            targets.append((obj, modifier))
        copied = source.copy()
        copied.name = new_name
        purpose_value = source.get("blender_mcp_purpose", "copied procedural system")
        source_uuid_value = source.get(OWNERSHIP_KEY)
        tag_group(
            copied,
            str(purpose_value),
            source_uuid=str(source_uuid_value) if source_uuid_value is not None else None,
        )
        duplicated = None
        if duplicate_object_name is not None:
            source_object = require_object(duplicate_object_name)
            requested_name = duplicated_object_name or f"{source_object.name} Copy"
            if bpy.data.objects.get(requested_name) is not None:
                raise ValueError(f"Duplicated object name already exists: {requested_name}")
            duplicated = source_object.copy()
            duplicated.name = requested_name
            if copy_object_data and source_object.data is not None:
                duplicated.data = source_object.data.copy()
            if copy_action and source_object.animation_data and source_object.animation_data.action:
                duplicated.animation_data_create()
                duplicated.animation_data.action = source_object.animation_data.action.copy()
            for collection in source_object.users_collection:
                collection.objects.link(duplicated)
            if reassign_duplicate_modifiers:
                for modifier in duplicated.modifiers:
                    if modifier.type == "NODES" and modifier.node_group == source:
                        modifier.node_group = copied
        for _obj, modifier in targets:
            modifier.node_group = copied
        return {
            "source_group": source.name,
            "copied_group": copied.name,
            "source_uuid": source.get(OWNERSHIP_KEY),
            "copy_uuid": copied.get(OWNERSHIP_KEY),
            "reassigned_modifiers": [{"object": obj.name, "modifier": modifier.name} for obj, modifier in targets],
            "external_dependencies": group_dependencies(copied),
            "source_users": group_users(source),
            "copy_users": group_users(copied),
            "duplicated_object": duplicated.name if duplicated else None,
            "object_data_copied": bool(duplicated and copy_object_data),
            "action_copied": bool(duplicated and copy_action),
            "changed_objects": sorted(
                {obj.name for obj, _modifier in targets} | ({duplicated.name} if duplicated else set())
            ),
            "changed_resources": [copied.name],
        }
