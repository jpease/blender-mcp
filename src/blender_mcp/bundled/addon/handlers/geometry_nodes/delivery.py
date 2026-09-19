# ruff: file-ignore[too-many-branches, too-many-locals, too-many-statements, too-many-statements-in-try-clause, undocumented-public-method]
"""Non-destructive Geometry Nodes delivery handlers."""

import uuid

from typing import Any

import bpy

from ...helpers import apply_modifier, preserve_mode_and_selection, set_active
from ._shared import OWNERSHIP_KEY, ROLE_KEY, SOURCE_KEY, evaluated_summary, require_nodes_modifier, require_object


def _unique_object_name(requested: str) -> str:
    """Return a Blender-style collision-safe object name."""
    if bpy.data.objects.get(requested) is None:
        return requested
    index = 1
    while bpy.data.objects.get(f"{requested}.{index:03d}") is not None:
        index += 1
    return f"{requested}.{index:03d}"


def _require_output_collection(name: str):
    """Resolve one local editable collection for the new delivery object."""
    collection = bpy.data.collections.get(name)
    if collection is None:
        raise ValueError(f"Output collection not found: {name}")
    if collection.library is not None or not collection.is_editable:
        raise ValueError(f"Output collection '{name}' is linked or read-only")
    return collection


def _instance_summary(source, limit: int = 10_000) -> dict[str, Any]:
    """Count evaluated instances attributable to the source object."""
    depsgraph = bpy.context.evaluated_depsgraph_get()
    items = []
    total = 0
    for instance in depsgraph.object_instances:
        parent = instance.parent
        if parent is None or parent.original != source:
            continue
        total += 1
        if len(items) < limit:
            items.append(instance.object.original.name)
    return {
        "total_count": total,
        "returned_count": len(items),
        "truncated": total > len(items),
        "source_objects": sorted(set(items)),
    }


def _mesh_schema(mesh) -> dict[str, Any]:
    """Describe delivery-relevant layers retained by a mesh datablock."""
    return {
        "counts": {
            "vertices": len(mesh.vertices),
            "edges": len(mesh.edges),
            "faces": len(mesh.polygons),
        },
        "materials": [material.name if material else None for material in mesh.materials],
        "attributes": [
            {"name": attribute.name, "data_type": attribute.data_type, "domain": attribute.domain}
            for attribute in mesh.attributes
        ],
        "uv_layers": [layer.name for layer in mesh.uv_layers],
    }


def _tag_delivery(output, source, mode: str, frame: int) -> None:
    """Store stable provenance without depending on mutable display names alone."""
    output[OWNERSHIP_KEY] = str(uuid.uuid4())
    output[ROLE_KEY] = "procedural_delivery"
    output[SOURCE_KEY] = source.get(OWNERSHIP_KEY, str(source.session_uid))
    output["blender_mcp_source_object"] = source.name
    output["blender_mcp_delivery_mode"] = mode
    output["blender_mcp_frame"] = frame


def _new_live_copy(source, name: str, collection):
    """Duplicate an object and its base data while retaining live modifiers."""
    output = source.copy()
    output.name = name
    if source.data is not None:
        output.data = source.data.copy()
        output.data.name = f"{name} Data"
    collection.objects.link(output)
    return output


class GeometryNodesDeliveryHandlersMixin:
    """Create explicit delivery copies without silently replacing their live sources."""

    def realize_procedural_output(
        self,
        object_name,
        output_name,
        collection_name,
        delivery_mode="REALIZED_MESH",
        modifier_name=None,
        frame=None,
        collision_policy="ERROR",
        confirm_destructive=False,
    ):
        source = require_object(object_name)
        collection = _require_output_collection(collection_name)
        if not output_name:
            raise ValueError("output_name must not be empty")
        if bpy.data.objects.get(output_name) is not None:
            if collision_policy == "ERROR":
                raise ValueError(f"Output object already exists: {output_name}")
            if collision_policy != "UNIQUE":
                raise ValueError(f"Unsupported collision_policy: {collision_policy}")
            output_name = _unique_object_name(output_name)
        if delivery_mode not in {"REALIZED_MESH", "LIVE_INSTANCE_COPY", "APPLIED_MODIFIER_COPY"}:
            raise ValueError(f"Unsupported delivery_mode: {delivery_mode}")
        if delivery_mode == "APPLIED_MODIFIER_COPY":
            if not modifier_name:
                raise ValueError("APPLIED_MODIFIER_COPY requires modifier_name")
            require_nodes_modifier(source, modifier_name)
            if not confirm_destructive:
                raise ValueError("confirm_destructive=True is required for APPLIED_MODIFIER_COPY")

        scene = bpy.context.scene
        previous_frame = scene.frame_current
        previous_subframe = scene.frame_subframe
        output = None
        created_mesh = None
        try:
            if frame is not None:
                scene.frame_set(int(frame))
                bpy.context.view_layer.update()
            delivery_frame = scene.frame_current
            base = {
                "type": source.type,
                "mesh": _mesh_schema(source.data) if source.type == "MESH" else None,
                "vertex_groups": [group.name for group in source.vertex_groups],
            }
            instances = _instance_summary(source)
            warnings = []

            if delivery_mode == "REALIZED_MESH":
                depsgraph = bpy.context.evaluated_depsgraph_get()
                evaluated = source.evaluated_get(depsgraph)
                try:
                    created_mesh = bpy.data.meshes.new_from_object(
                        evaluated,
                        preserve_all_data_layers=True,
                        depsgraph=depsgraph,
                    )
                except RuntimeError as exc:
                    raise ValueError(
                        f"Evaluated output of '{source.name}' cannot be represented as a standalone mesh: {exc}"
                    ) from exc
                created_mesh.name = f"{output_name} Mesh"
                output = bpy.data.objects.new(output_name, created_mesh)
                output.matrix_world = source.matrix_world.copy()
                collection.objects.link(output)
                if source.vertex_groups:
                    warnings.append(
                        "Object vertex-group definitions are not created by Mesh.new_from_object; equivalent named "
                        "attributes, when present, remain in the realized mesh."
                    )
                live_state = False
                topology_indices_stale = True
            else:
                output = _new_live_copy(source, output_name, collection)
                if delivery_mode == "APPLIED_MODIFIER_COPY":
                    if modifier_name is None:
                        raise RuntimeError("Validated modifier_name was unexpectedly missing")
                    modifier = require_nodes_modifier(output, modifier_name)
                    with preserve_mode_and_selection():
                        set_active(output)
                        apply_modifier(output, modifier)
                    live_state = any(item.type == "NODES" for item in output.modifiers)
                    topology_indices_stale = True
                else:
                    live_state = True
                    topology_indices_stale = False
                    warnings.append(
                        "LIVE_INSTANCE_COPY retains its procedural modifiers and instances; it is a delivery copy, "
                        "not a standalone realized mesh."
                    )

            _tag_delivery(output, source, delivery_mode, delivery_frame)
            bpy.context.view_layer.update()
            realized_mesh = _mesh_schema(output.data) if output.type == "MESH" else None
            retained_attributes = [item["name"] for item in realized_mesh["attributes"]] if realized_mesh else []
            source_attributes = [item["name"] for item in base["mesh"]["attributes"]] if base["mesh"] else []
            return {
                "source_object": source.name,
                "output_object": output.name,
                "output_collection": collection.name,
                "delivery_mode": delivery_mode,
                "frame": delivery_frame,
                "source_retained": True,
                "live_procedural_state": live_state,
                "base": base,
                "output": realized_mesh,
                "evaluated": evaluated_summary(output),
                "source_instances": instances,
                "retained_named_attributes": retained_attributes,
                "lost_base_named_attributes": sorted(set(source_attributes) - set(retained_attributes)),
                "lost_components": ["Non-mesh components that Object.to_mesh cannot represent"]
                if delivery_mode == "REALIZED_MESH"
                else [],
                "topology_indices_stale": topology_indices_stale,
                "warnings": warnings,
                "provenance": {
                    "source_object": source.name,
                    "source_session_uid": source.session_uid,
                    "delivery_uuid": output[OWNERSHIP_KEY],
                },
                "changed_objects": [output.name],
                "changed_resources": [output.data.name] if output.data is not None else [],
            }
        except Exception:
            if output is not None and output.name in bpy.data.objects:
                bpy.data.objects.remove(output, do_unlink=True)
            if created_mesh is not None and created_mesh.name in bpy.data.meshes:
                bpy.data.meshes.remove(created_mesh, do_unlink=True)
            raise
        finally:
            if scene.frame_current != previous_frame or scene.frame_subframe != previous_subframe:
                scene.frame_set(previous_frame, subframe=previous_subframe)
                bpy.context.view_layer.update()
