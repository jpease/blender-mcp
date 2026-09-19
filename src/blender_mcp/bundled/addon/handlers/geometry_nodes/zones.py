# ruff: file-ignore[magic-value-comparison, undocumented-public-method]
"""Atomic Repeat and Simulation Zone construction handlers."""

import math

from typing import Any

from ._shared import ROLE_KEY, group_dependencies, require_group
from .authoring import _apply_graph_operation, atomic_group_edit

SUPPORTED_ZONE_SOCKET_TYPES = {"GEOMETRY", "FLOAT", "INT", "BOOLEAN", "VECTOR", "ROTATION", "RGBA"}
SUPPORTED_ATTRIBUTE_DOMAINS = {"POINT", "EDGE", "FACE", "CORNER", "CURVE", "INSTANCE"}
MAX_REPEAT_ITERATIONS = 256
MAX_ZONE_ITEMS = 32
MAX_ZONE_GRAPH_OPERATIONS = 200


def _validate_zone_request(
    group,
    input_name: str,
    output_name: str,
    state_items: list[dict[str, Any]],
    operations: list[dict[str, Any]],
    locations,
    *,
    allow_attribute_domain: bool,
) -> None:
    """Validate names and state types before copying an editable graph."""
    if input_name == output_name:
        raise ValueError("Zone input and output node names must differ")
    for name in (input_name, output_name):
        if not name:
            raise ValueError("Zone node names must not be empty")
        if group.nodes.get(name) is not None:
            raise ValueError(f"Node name already exists in '{group.name}': {name}")
    if not 1 <= len(state_items) <= MAX_ZONE_ITEMS:
        raise ValueError(f"state_items must contain 1-{MAX_ZONE_ITEMS} entries")
    if len(operations) > MAX_ZONE_GRAPH_OPERATIONS:
        raise ValueError(f"graph_operations is limited to {MAX_ZONE_GRAPH_OPERATIONS} edits")
    if any(len(location) != 2 or not all(math.isfinite(float(value)) for value in location) for location in locations):
        raise ValueError("Zone node locations must contain two finite numbers")
    if any(not isinstance(item, dict) or not item.get("name") or not item.get("socket_type") for item in state_items):
        raise ValueError("Each state item requires a non-empty name and socket_type")
    names = [item["name"] for item in state_items]
    if len(names) != len(set(names)):
        raise ValueError("state item names must be unique")
    invalid_types = sorted({item["socket_type"] for item in state_items} - SUPPORTED_ZONE_SOCKET_TYPES)
    if invalid_types:
        raise ValueError(f"Unsupported zone socket types: {invalid_types}")
    invalid_domains = sorted(
        {
            item["attribute_domain"]
            for item in state_items
            if item.get("attribute_domain") is not None and item["attribute_domain"] not in SUPPORTED_ATTRIBUTE_DOMAINS
        }
    )
    if invalid_domains:
        raise ValueError(f"Unsupported state attribute domains: {invalid_domains}")
    if not allow_attribute_domain and any(item.get("attribute_domain") is not None for item in state_items):
        raise ValueError("attribute_domain is supported only by Simulation Zone state items")


def _configure_state_items(collection, specifications: list[dict[str, Any]], *, simulation: bool) -> None:
    """Replace a zone's implicit Geometry state with the requested exact schema."""
    collection.clear()
    for specification in specifications:
        try:
            item = collection.new(specification["socket_type"], specification["name"])
        except (TypeError, RuntimeError, ValueError) as exc:
            raise ValueError(
                f"Blender rejected {specification['socket_type']} state item '{specification['name']}': {exc}"
            ) from exc
        domain = specification.get("attribute_domain")
        if simulation and domain is not None:
            item.attribute_domain = domain
        elif not simulation and domain is not None:
            raise ValueError("attribute_domain is supported only by Simulation Zone state items")


def _state_socket_records(output_node, state_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the stable socket identifiers Blender assigned to state items."""
    sockets = [socket for socket in output_node.outputs if socket.identifier != "__extend__"]
    if len(sockets) != len(state_items):
        raise RuntimeError("Blender produced an unexpected zone state socket layout")
    return [
        {
            "name": item.name,
            "socket_type": specification["socket_type"],
            "socket_identifier": socket.identifier,
            **({"attribute_domain": item.attribute_domain} if hasattr(item, "attribute_domain") else {}),
        }
        for item, specification, socket in zip(
            output_node.state_items if hasattr(output_node, "state_items") else output_node.repeat_items,
            state_items,
            sockets,
            strict=True,
        )
    ]


def _wire_state_passthrough(group, input_node, output_node) -> None:
    """Give a newly-created zone valid, deterministic feedback links."""
    output_inputs = {socket.identifier: socket for socket in output_node.inputs if socket.identifier != "__extend__"}
    for socket in input_node.outputs:
        target = output_inputs.get(socket.identifier)
        if target is not None:
            group.links.new(socket, target)


def _apply_internal_patch(group, operations: list[dict[str, Any]], protected_names: set[str]) -> dict[str, str]:
    """Apply requested internal edits while protecting the paired zone endpoints."""
    name_map: dict[str, str] = {}
    for operation in operations:
        if operation.get("node_name") in protected_names and operation["operation"] in {"REMOVE_NODE", "UPDATE_NODE"}:
            raise ValueError("graph_operations may connect zone nodes but may not rename or remove them")
        _apply_graph_operation(group, operation, name_map)
    return name_map


def _zone_nodes(group, input_type: str, output_type: str, input_name: str, output_name: str, locations):
    """Create, name, place, and pair the two nodes forming one zone."""
    output_node = group.nodes.new(output_type)
    input_node = group.nodes.new(input_type)
    input_node.name = input_name
    output_node.name = output_name
    input_node.label = input_name
    output_node.label = output_name
    input_node.location = locations[0]
    output_node.location = locations[1]
    if not input_node.pair_with_output(output_node):
        raise RuntimeError(f"Blender could not pair {input_type} with {output_type}")
    return input_node, output_node


class GeometryNodesZoneHandlersMixin:
    """Create bounded, paired iteration and simulation zones in editable graphs."""

    def create_repeat_zone(
        self,
        node_group_name,
        input_node_name="Repeat Input",
        output_node_name="Repeat Output",
        state_items=None,
        iterations=1,
        input_location=(-240.0, 0.0),
        output_location=(240.0, 0.0),
        graph_operations=None,
    ):
        group = require_group(node_group_name)
        items = state_items or [{"name": "Geometry", "socket_type": "GEOMETRY"}]
        operations = graph_operations or []
        _validate_zone_request(
            group,
            input_node_name,
            output_node_name,
            items,
            operations,
            (input_location, output_location),
            allow_attribute_domain=False,
        )
        if not 1 <= int(iterations) <= MAX_REPEAT_ITERATIONS:
            raise ValueError(f"iterations must be in [1, {MAX_REPEAT_ITERATIONS}]")

        def edit(working):
            input_node, output_node = _zone_nodes(
                working,
                "GeometryNodeRepeatInput",
                "GeometryNodeRepeatOutput",
                input_node_name,
                output_node_name,
                (input_location, output_location),
            )
            _configure_state_items(output_node.repeat_items, items, simulation=False)
            input_node.inputs["Iterations"].default_value = int(iterations)
            input_node[ROLE_KEY] = "repeat_zone_input"
            output_node[ROLE_KEY] = "repeat_zone_output"
            _wire_state_passthrough(working, input_node, output_node)
            name_map = _apply_internal_patch(working, operations, {input_node_name, output_node_name})
            if input_node.paired_output != output_node:
                raise RuntimeError("The Repeat Zone became unpaired during the graph patch")
            return {
                "input_node": input_node.name,
                "output_node": output_node.name,
                "state_items": _state_socket_records(output_node, items),
                "node_name_map": name_map,
            }

        updated, result = atomic_group_edit(group, edit)
        return {
            "node_group": updated.name,
            "zone_type": "REPEAT",
            "iterations": int(iterations),
            "maximum_iterations": MAX_REPEAT_ITERATIONS,
            "complexity_estimate": {
                "state_values_per_iteration": len(items),
                "iteration_state_steps": int(iterations) * len(items),
            },
            **result,
            "changed_resources": [updated.name],
        }

    def create_simulation_zone(
        self,
        node_group_name,
        input_node_name="Simulation Input",
        output_node_name="Simulation Output",
        state_items=None,
        frame_start=1,
        frame_end=250,
        time_step_mode="SCENE_DELTA_TIME",
        skip_simulation=False,
        input_location=(-240.0, 0.0),
        output_location=(240.0, 0.0),
        graph_operations=None,
    ):
        group = require_group(node_group_name)
        items = state_items or [{"name": "Geometry", "socket_type": "GEOMETRY"}]
        operations = graph_operations or []
        _validate_zone_request(
            group,
            input_node_name,
            output_node_name,
            items,
            operations,
            (input_location, output_location),
            allow_attribute_domain=True,
        )
        if frame_start > frame_end:
            raise ValueError("frame_start must not exceed frame_end")
        if time_step_mode != "SCENE_DELTA_TIME":
            raise ValueError("Blender 5.1 Simulation Zones expose only scene-derived Delta Time")

        def edit(working):
            input_node, output_node = _zone_nodes(
                working,
                "GeometryNodeSimulationInput",
                "GeometryNodeSimulationOutput",
                input_node_name,
                output_node_name,
                (input_location, output_location),
            )
            _configure_state_items(output_node.state_items, items, simulation=True)
            output_node.inputs["Skip"].default_value = bool(skip_simulation)
            input_node[ROLE_KEY] = "simulation_zone_input"
            output_node[ROLE_KEY] = "simulation_zone_output"
            output_node["blender_mcp_frame_start"] = int(frame_start)
            output_node["blender_mcp_frame_end"] = int(frame_end)
            output_node["blender_mcp_time_step"] = time_step_mode
            _wire_state_passthrough(working, input_node, output_node)
            name_map = _apply_internal_patch(working, operations, {input_node_name, output_node_name})
            if input_node.paired_output != output_node:
                raise RuntimeError("The Simulation Zone became unpaired during the graph patch")
            return {
                "input_node": input_node.name,
                "output_node": output_node.name,
                "state_items": _state_socket_records(output_node, items),
                "node_name_map": name_map,
            }

        updated, result = atomic_group_edit(group, edit)
        return {
            "node_group": updated.name,
            "zone_type": "SIMULATION",
            "state_schema": result["state_items"],
            "intended_frame_range": {"start": int(frame_start), "end": int(frame_end)},
            "time_step": {"mode": time_step_mode, "socket": "Delta Time"},
            "cache_status": "NOT_BAKED_BY_THIS_OPERATION",
            "dependencies": group_dependencies(updated),
            **result,
            "changed_resources": [updated.name],
        }
