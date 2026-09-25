# pyright: reportGeneralTypeIssues=false
"""Precise removal of rigid-body settings and MCP-owned helpers."""

import bpy

from ...helpers import counted_page, preserve_mode_and_selection, set_active
from .inspection_and_setup import (
    _add_rigid_body,
    _body_snapshot,
    _cache_info,
    _require_finished,
    _restore_fields,
    _run_object_operator,
    _scene,
    _view_layer_for,
)


def _removed_page(items, type_of):
    """Count what a removal took away by type, beside a page of its names."""
    return counted_page(items, type_of=type_of, name_of=lambda item: item.name)


def _object_type(obj):
    return obj.type


def _remove_constraint(scene, obj):
    view_layer = _view_layer_for(scene, obj)
    with bpy.context.temp_override(scene=scene, view_layer=view_layer), preserve_mode_and_selection():
        set_active(obj)
        result = bpy.ops.rigidbody.constraint_remove()
    _require_finished(result, "bpy.ops.rigidbody.constraint_remove")
    if obj.rigid_body_constraint is not None:
        raise RuntimeError(f"Rigid-body constraint settings remain on '{obj.name}'")


class RigidBodyLifecycleHandlers:
    """Remove explicitly selected rigid-body components with production safeguards."""

    def remove_rigid_body_components(
        self,
        scene_name,
        component_type,
        object_names=None,
        rig_id=None,
        confirm_destructive=False,
    ):
        scene = _scene(scene_name)
        names = list(object_names or [])
        if len(names) > 500 or len(set(names)) != len(names):
            raise ValueError("object_names must contain at most 500 unique names")
        world = scene.rigidbody_world
        cache_before = _cache_info(world.point_cache) if world else None

        if component_type == "WORLD":
            if names or rig_id:
                raise ValueError("WORLD does not accept object_names or rig_id")
            if world is None:
                raise ValueError(f"Scene '{scene.name}' has no rigid-body world")
            if not confirm_destructive:
                raise ValueError("WORLD removal requires confirm_destructive=True")
            view_layer = _view_layer_for(scene)
            with bpy.context.temp_override(scene=scene, view_layer=view_layer):
                result = bpy.ops.rigidbody.world_remove()
            _require_finished(result, "bpy.ops.rigidbody.world_remove")
            if scene.rigidbody_world is not None:
                raise RuntimeError("Rigid-body world remains after world_remove reported FINISHED")
            # Nothing removed is a next target: `removed` names the scene whose world went.
            return {
                "changed_objects": [],
                "changed_resources": [scene.name],
                "component_type": component_type,
                "removed": _removed_page([scene], lambda _scene: "RIGIDBODY_WORLD"),
                "cache_before": cache_before,
                "undo_recoverable": True,
            }

        if component_type == "TAGGED_HELPERS":
            if not confirm_destructive:
                raise ValueError("TAGGED_HELPERS removal requires confirm_destructive=True")
            candidates = []
            for obj in scene.objects:
                owned = bool(obj.get("blendermcp_rigid_body_role"))
                matches_id = rig_id is None or obj.get("blendermcp_rigid_body_rig_id") == rig_id
                matches_name = not names or obj.name in names
                if owned and matches_id and matches_name:
                    candidates.append(obj)
            missing = sorted(set(names) - {obj.name for obj in candidates})
            if missing:
                raise ValueError(f"Objects are missing or not tagged rigid-body helpers: {missing}")
            if not candidates:
                raise ValueError("No tagged rigid-body helpers matched the requested scope")
            # A rig's helpers can be many and are gone: counted in `removed`, none to act on next.
            removed = _removed_page(candidates, _object_type)
            for obj in candidates:
                bpy.data.objects.remove(obj, do_unlink=True)
            return {
                "changed_objects": [],
                "component_type": component_type,
                "removed": removed,
                "rig_id": rig_id,
                "undo_recoverable": True,
            }

        if component_type not in {"BODY_SETTINGS", "CONSTRAINT_SETTINGS"}:
            raise ValueError(f"Unsupported component_type: {component_type}")
        if not names:
            raise ValueError(f"{component_type} requires object_names")
        objects = []
        snapshots = {}
        for name in names:
            obj = bpy.data.objects.get(name)
            if obj is None or obj.name not in scene.objects:
                raise ValueError(f"Object '{name}' is not linked to scene '{scene.name}'")
            component = obj.rigid_body if component_type == "BODY_SETTINGS" else obj.rigid_body_constraint
            if component is None:
                raise ValueError(f"Object '{name}' has no {component_type.lower().replace('_', ' ')}")
            objects.append(obj)
            if component_type == "BODY_SETTINGS":
                snapshots[obj.name] = _body_snapshot(component)
            else:
                snapshots[obj.name] = {
                    prop.identifier: getattr(component, prop.identifier)
                    for prop in component.bl_rna.properties
                    if prop.identifier != "rna_type" and not prop.is_readonly
                }
        if world and world.point_cache.is_baked:
            if not confirm_destructive:
                raise ValueError("Removing components from a baked world requires confirm_destructive=True")
            view_layer = _view_layer_for(scene)
            with bpy.context.temp_override(scene=scene, view_layer=view_layer, point_cache=world.point_cache):
                result = bpy.ops.ptcache.free_bake()
            _require_finished(result, "bpy.ops.ptcache.free_bake")
        removed = []
        try:
            for obj in objects:
                if component_type == "BODY_SETTINGS":
                    view_layer = _view_layer_for(scene, obj)
                    with bpy.context.temp_override(scene=scene, view_layer=view_layer), preserve_mode_and_selection():
                        set_active(obj)
                        result = bpy.ops.rigidbody.object_remove()
                    _require_finished(result, "bpy.ops.rigidbody.object_remove")
                    if obj.rigid_body is not None:
                        raise RuntimeError(f"Rigid-body settings remain on '{obj.name}'")
                else:
                    _remove_constraint(scene, obj)
                removed.append(obj.name)
        except Exception:
            for obj in objects:
                if obj.name not in removed:
                    continue
                snapshot = snapshots[obj.name]
                if component_type == "BODY_SETTINGS":
                    _add_rigid_body(scene, obj, snapshot["type"])
                    layers = snapshot["collision_collections"]
                    _restore_fields(
                        obj.rigid_body,
                        {name: value for name, value in snapshot.items() if name != "collision_collections"},
                    )
                    obj.rigid_body.collision_collections = layers
                else:
                    constraint_type = snapshot["type"]
                    _run_object_operator(scene, obj, bpy.ops.rigidbody.constraint_add, type=constraint_type)
                    _restore_fields(obj.rigid_body_constraint, snapshot)
            raise
        return {
            "changed_objects": [],
            "component_type": component_type,
            "removed": _removed_page(objects, _object_type),
            "mesh_objects_retained": component_type == "BODY_SETTINGS",
            "cache_before": cache_before,
            "cache_freed": bool(cache_before and cache_before["is_baked"]),
            "undo_recoverable": True,
        }
