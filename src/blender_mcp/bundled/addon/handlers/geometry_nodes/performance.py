# ruff: file-ignore[magic-value-comparison, too-many-branches, too-many-locals, too-many-statements, undocumented-public-method]
"""Bounded whole-system Geometry Nodes performance analysis handlers."""

import itertools
import math
import os
import statistics
import time

from collections import deque
from typing import Any

import bpy

from ._shared import evaluated_summary, require_nodes_modifier, require_object

HEAVY_NODE_TYPES = {
    "GeometryNodeRealizeInstances",
    "GeometryNodeMeshToVolume",
    "GeometryNodePointsToVolume",
    "GeometryNodeVolumeToMesh",
    "GeometryNodeSubdivisionSurface",
    "GeometryNodeSubdivideMesh",
    "GeometryNodeDistributePointsInVolumeGrid",
    "GeometryNodeDistributePointsOnFaces",
    "GeometryNodeMeshBoolean",
    "GeometryNodeRepeatInput",
    "GeometryNodeSimulationInput",
}


def _active_output_reachable_nodes(group) -> set[Any]:
    """Return nodes contributing to active group outputs by reverse graph traversal."""
    incoming: dict[Any, list[Any]] = {}
    for link in group.links:
        if link.is_valid and not link.is_muted:
            incoming.setdefault(link.to_node, []).append(link.from_node)
    pending = [node for node in group.nodes if node.bl_idname == "NodeGroupOutput" and node.is_active_output]
    reachable = set(pending)
    while pending:
        node = pending.pop()
        for predecessor in incoming.get(node, []):
            if predecessor not in reachable:
                reachable.add(predecessor)
                pending.append(predecessor)
    return reachable


def _distance_to_active_output(group, start) -> int | None:
    """Find the shortest downstream node count to an active output."""
    targets = {node for node in group.nodes if node.bl_idname == "NodeGroupOutput" and node.is_active_output}
    outgoing: dict[Any, list[Any]] = {}
    for link in group.links:
        if link.is_valid and not link.is_muted:
            outgoing.setdefault(link.from_node, []).append(link.to_node)
    queue = deque([(start, 0)])
    visited = {start}
    while queue:
        node, distance = queue.popleft()
        if node in targets:
            return distance
        for successor in outgoing.get(node, []):
            if successor not in visited:
                visited.add(successor)
                queue.append((successor, distance + 1))
    return None


def _unlinked_default(node, socket_name: str):
    """Read a static input default only when it is not driven by a field/link."""
    socket = node.inputs.get(socket_name)
    if socket is None or socket.is_linked or not hasattr(socket, "default_value"):
        return None
    value = socket.default_value
    if isinstance(value, (int, float, bool, str)):
        return value
    try:
        return list(value)
    except TypeError:
        return None


def _nested_group_metrics(root, max_groups: int = 100) -> dict[str, Any]:
    """Measure bounded nested-group depth and detect recursive references."""
    deepest = 0
    visited_groups = set()
    recursive = False
    truncated = False

    def visit(group, depth: int, ancestry: frozenset[int]) -> None:
        nonlocal deepest, recursive, truncated
        deepest = max(deepest, depth)
        identity = group.session_uid
        if identity in ancestry:
            recursive = True
            return
        if len(visited_groups) >= max_groups:
            truncated = True
            return
        visited_groups.add(identity)
        next_ancestry = ancestry | {identity}
        for node in group.nodes:
            nested = getattr(node, "node_tree", None)
            if nested is not None and getattr(nested, "bl_idname", None) == "GeometryNodeTree":
                visit(nested, depth + 1, next_ancestry)

    visit(root, 0, frozenset())
    return {
        "maximum_depth": deepest,
        "groups_visited": len(visited_groups),
        "recursive_reference": recursive,
        "truncated": truncated,
    }


def _directory_has_files(path: str) -> bool:
    """Return bounded evidence that a configured disk cache contains files."""
    if not path:
        return False
    resolved = os.path.realpath(bpy.path.abspath(path))
    if not os.path.isdir(resolved):
        return False
    return any(files for _root, _directories, files in itertools.islice(os.walk(resolved), 1_000))


def _graph_heuristics(group, modifier, topology_warning_threshold: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Report structural cost signals separately from measured timings."""
    findings: list[dict[str, Any]] = []

    def add(severity: str, code: str, message: str, node=None, evidence=None) -> None:
        findings.append(
            {
                "severity": severity,
                "code": code,
                "message": message,
                "node": node.name if node is not None else None,
                "evidence": evidence or {},
            }
        )

    reachable = _active_output_reachable_nodes(group)
    for node in group.nodes:
        if node.bl_idname in HEAVY_NODE_TYPES and node not in reachable:
            add(
                "INFO",
                "UNUSED_HEAVY_BRANCH",
                "This potentially expensive node is disconnected from every active group output.",
                node,
            )
        if node.bl_idname == "GeometryNodeRealizeInstances" and node in reachable:
            distance = _distance_to_active_output(group, node)
            if distance is not None and distance > 2:
                add(
                    "WARNING",
                    "EARLY_INSTANCE_REALIZATION",
                    "Instances are realized several graph stages before output; keep them instanced until required.",
                    node,
                    {"nodes_to_active_output": distance},
                )
        elif node.bl_idname in {"GeometryNodeMeshToVolume", "GeometryNodePointsToVolume", "GeometryNodeVolumeToMesh"}:
            voxel = _unlinked_default(node, "Voxel Size")
            severity = "WARNING" if isinstance(voxel, (int, float)) and voxel < 0.02 else "INFO"
            add(
                severity,
                "VOLUME_CONVERSION_COST",
                "Volume conversion cost grows sharply as voxel size decreases.",
                node,
                {"voxel_size_default": voxel},
            )
        elif node.bl_idname in {"GeometryNodeSubdivisionSurface", "GeometryNodeSubdivideMesh"}:
            level = _unlinked_default(node, "Level")
            if level is None:
                level = _unlinked_default(node, "Levels")
            if isinstance(level, int) and level >= 4:
                add(
                    "WARNING",
                    "HIGH_SUBDIVISION",
                    "A static subdivision level of four or more can multiply topology rapidly.",
                    node,
                    {"level": level},
                )
        elif node.bl_idname == "GeometryNodeMeshGrid":
            vertices_x = _unlinked_default(node, "Vertices X")
            vertices_y = _unlinked_default(node, "Vertices Y")
            if isinstance(vertices_x, int) and isinstance(vertices_y, int):
                total = vertices_x * vertices_y
                if total > topology_warning_threshold:
                    add(
                        "WARNING",
                        "LARGE_GRID",
                        "The Grid node's static dimensions exceed the topology warning threshold.",
                        node,
                        {"vertices": total},
                    )
        elif node.bl_idname == "GeometryNodeDistributePointsOnFaces":
            density = _unlinked_default(node, "Density")
            if isinstance(density, (int, float)) and density > 1_000:
                add(
                    "WARNING",
                    "HIGH_POINT_DENSITY",
                    "Static point density is high; inspect the source surface area before final evaluation.",
                    node,
                    {"density": density},
                )
        elif node.bl_idname == "GeometryNodeMeshBoolean":
            geometry_input = node.inputs.get("Mesh 2")
            fan_in = len(geometry_input.links) if geometry_input is not None else 0
            if fan_in > 8:
                add(
                    "WARNING",
                    "BOOLEAN_FAN_IN",
                    "Many Boolean inputs are evaluated together; stage or simplify cutters when possible.",
                    node,
                    {"linked_cutters": fan_in},
                )
        elif node.bl_idname == "GeometryNodeRepeatInput":
            iterations = _unlinked_default(node, "Iterations")
            if isinstance(iterations, int) and iterations > 64:
                add(
                    "WARNING",
                    "HIGH_REPEAT_COUNT",
                    "The Repeat Zone has a high static iteration count.",
                    node,
                    {"iterations": iterations},
                )

    for bake in modifier.bakes:
        node = bake.node
        if node is None or node.bl_idname != "GeometryNodeSimulationOutput":
            continue
        effective_target = bake.bake_target if bake.bake_target != "INHERIT" else modifier.bake_target
        directory = bake.directory if bake.use_custom_path else modifier.bake_directory
        if effective_target == "DISK" and not _directory_has_files(directory):
            add(
                "WARNING",
                "SIMULATION_DISK_CACHE_MISSING",
                "A Simulation Zone targets disk but no cache-file evidence was found.",
                node,
                {"bake_id": bake.bake_id, "directory": directory},
            )
        elif effective_target == "PACKED":
            add(
                "INFO",
                "PACKED_CACHE_STATUS_UNAVAILABLE",
                "Public Blender RNA cannot prove whether this packed Simulation Zone cache is baked.",
                node,
                {"bake_id": bake.bake_id},
            )
    nested = _nested_group_metrics(group)
    if nested["maximum_depth"] > 4:
        add(
            "WARNING",
            "DEEP_NESTED_GROUPS",
            "Deep nested node groups can hide repeated realization or generation costs.",
            evidence=nested,
        )
    return findings, nested


def _bounded_instance_count(obj, limit: int) -> dict[str, Any]:
    """Count only up to the caller's dependency-graph instance budget."""
    count = 0
    for instance in bpy.context.evaluated_depsgraph_get().object_instances:
        if instance.parent is None or instance.parent.original != obj:
            continue
        count += 1
        if count > limit:
            return {"count": limit, "truncated": True, "lower_bound": limit + 1}
    return {"count": count, "truncated": False, "lower_bound": count}


class GeometryNodesPerformanceHandlersMixin:
    """Measure bounded evaluation cost and report explainable graph heuristics."""

    def analyze_procedural_performance(
        self,
        object_name,
        modifier_name,
        frames,
        repetitions=1,
        time_limit_seconds=30.0,
        instance_limit=10_000,
        topology_warning_threshold=1_000_000,
    ):
        obj = require_object(object_name)
        modifier = require_nodes_modifier(obj, modifier_name)
        group = modifier.node_group
        if group is None:
            raise ValueError(f"Modifier '{modifier.name}' has no node group")
        if not frames or len(frames) > 8 or len(set(frames)) != len(frames):
            raise ValueError("frames must contain 1-8 unique frame numbers")
        if not 1 <= int(repetitions) <= 5:
            raise ValueError("repetitions must be in [1, 5]")
        if time_limit_seconds <= 0 or instance_limit < 1 or topology_warning_threshold < 1:
            raise ValueError("time and count limits must be positive")

        findings, nested = _graph_heuristics(group, modifier, int(topology_warning_threshold))
        scene = bpy.context.scene
        original_frame = scene.frame_current
        original_subframe = scene.frame_subframe
        deadline = time.monotonic() + float(time_limit_seconds)
        samples = []
        timed_out = False
        try:
            for frame in frames:
                for repetition in range(int(repetitions)):
                    if time.monotonic() >= deadline:
                        timed_out = True
                        break
                    started = time.perf_counter()
                    scene.frame_set(int(frame))
                    bpy.context.view_layer.update()
                    summary = evaluated_summary(obj)
                    instances = _bounded_instance_count(obj, int(instance_limit))
                    elapsed = time.perf_counter() - started
                    counts = summary.get("mesh_counts", {})
                    samples.append(
                        {
                            "frame": int(frame),
                            "repetition": repetition + 1,
                            "elapsed_seconds": elapsed,
                            "modifier_execution_seconds": float(getattr(modifier, "execution_time", 0.0)),
                            "evaluated": summary,
                            "instances": instances,
                        }
                    )
                    if counts.get("vertices", 0) > topology_warning_threshold:
                        findings.append(
                            {
                                "severity": "WARNING",
                                "code": "EVALUATED_TOPOLOGY_GROWTH",
                                "message": "Evaluated vertex count exceeds the requested warning threshold.",
                                "node": None,
                                "evidence": {
                                    "frame": int(frame),
                                    "vertices": counts["vertices"],
                                    "threshold": topology_warning_threshold,
                                },
                            }
                        )
                    if instances["truncated"]:
                        findings.append(
                            {
                                "severity": "WARNING",
                                "code": "INSTANCE_LIMIT_REACHED",
                                "message": "Instance traversal reached its explicit bound.",
                                "node": None,
                                "evidence": {"frame": int(frame), "limit": instance_limit},
                            }
                        )
                if timed_out:
                    break
        finally:
            scene.frame_set(original_frame, subframe=original_subframe)
            bpy.context.view_layer.update()

        elapsed_values = [sample["elapsed_seconds"] for sample in samples]
        modifier_times = [sample["modifier_execution_seconds"] for sample in samples]
        warnings = [
            {"type": warning.type, "message": warning.message, "node": warning.node_name}
            for warning in modifier.node_warnings
        ]
        return {
            "object": obj.name,
            "modifier": modifier.name,
            "node_group": group.name,
            "requested_frames": [int(frame) for frame in frames],
            "evaluated_frames": sorted({sample["frame"] for sample in samples}),
            "repetitions": int(repetitions),
            "timed_out": timed_out,
            "samples": samples,
            "whole_system_timing": {
                "sample_count": len(elapsed_values),
                "minimum_seconds": min(elapsed_values) if elapsed_values else None,
                "median_seconds": statistics.median(elapsed_values) if elapsed_values else None,
                "maximum_seconds": max(elapsed_values) if elapsed_values else None,
                "mean_seconds": math.fsum(elapsed_values) / len(elapsed_values) if elapsed_values else None,
            },
            "modifier_reported_timing": {
                "minimum_seconds": min(modifier_times) if modifier_times else None,
                "maximum_seconds": max(modifier_times) if modifier_times else None,
            },
            "nested_groups": nested,
            "heuristic_findings": findings,
            "modifier_warnings": warnings,
            "limitations": [
                "Blender public RNA does not provide trustworthy per-node execution timing; "
                "node findings are heuristics.",
                "A single dependency-graph evaluation cannot be safely preempted mid-call; "
                "the time limit stops later samples.",
            ],
        }
