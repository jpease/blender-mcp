"""Blender-main-thread handlers for cloth proxy rigs."""

from __future__ import annotations

import contextlib
import json
import math
import uuid

import bpy

from ...helpers import preserve_mode_and_selection, set_active, sync_from_editmode
from ._cache_helpers import _configure_independent_cache
from ._deform_binding import (
    _bind_deform_modifier,
    _restore_attachment_modifier,
    _snapshot_attachment_modifier,
    _unbind_deform_modifier,
)
from ._geometry_sampling import _evaluated_geometry_evidence, _proxy_proximity_evidence
from ._ownership import _remove_custom_property, _tag_owned_component, _tag_owned_object
from .inspection_and_setup import (
    _MCP_SCHEMA_VERSION,
    _OWNERSHIP_PREFIX,
    _finite,
    _get_object,
    _modifier_info,
    _reject_baked,
    _scene_context_for_object,
    _tag_update,
    _topology_summary,
    _validate_rna_value,
)


# Blender stores RNA floats as C floats. A request carrying a double such as 4.1
# reads back as 4.099999904632568, so comparing a stored setting to the value
# that produced it must tolerate one float32 round trip. float32 keeps about
# seven significant digits.
_STORED_FLOAT_TOLERANCE = 1e-6


def _matches_stored_float(stored, requested):
    """
    Report whether a modifier's stored float is the one that was requested.

    Args:
        stored: The value read back from the RNA property.
        requested: The value the caller asked for.

    Returns:
        bool: True when the two agree to within one float32 round trip.

    """
    return math.isclose(float(stored), float(requested), rel_tol=_STORED_FLOAT_TOLERANCE)


def _remove_created_object(obj, copied_data=None, copied_materials=(), copied_actions=()):
    for collection in list(obj.users_collection):
        with contextlib.suppress(Exception):
            collection.objects.unlink(obj)
    with contextlib.suppress(Exception):
        bpy.data.objects.remove(obj)
    if copied_data is not None and getattr(copied_data, "users", 1) == 0:
        with contextlib.suppress(Exception):
            bpy.data.batch_remove(ids=[copied_data])
    for material in copied_materials:
        if getattr(material, "users", 1) == 0:
            with contextlib.suppress(Exception):
                bpy.data.materials.remove(material)
    for action in copied_actions:
        if getattr(action, "users", 1) == 0:
            with contextlib.suppress(Exception):
                bpy.data.actions.remove(action)


def _validate_id_name(name, label):
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"{label} must be a nonempty name")
    if len(name.encode("utf-8")) > 63:
        raise ValueError(f"{label} exceeds Blender's 63-byte ID name limit")
    return name


def _modifier_dependency_target(modifier):
    if modifier.type in {"SURFACE_DEFORM", "NORMAL_EDIT"}:
        return getattr(modifier, "target", None)
    return getattr(modifier, "object", None)


def _set_modifier_dependency_target(modifier, target):
    if modifier.type in {"SURFACE_DEFORM", "NORMAL_EDIT"}:
        modifier.target = target
    elif hasattr(modifier, "object"):
        modifier.object = target


def _apply_named_modifier(obj, modifier):
    scene, view_layer = _scene_context_for_object(obj)
    with preserve_mode_and_selection():
        set_active(obj)
        if obj.mode != "OBJECT":
            result = bpy.ops.object.mode_set(mode="OBJECT")
            if "FINISHED" not in result:
                raise RuntimeError(f"Could not enter Object Mode for modifier application: {sorted(result)}")
        with bpy.context.temp_override(
            scene=scene,
            view_layer=view_layer,
            object=obj,
            active_object=obj,
            selected_objects=[obj],
            selected_editable_objects=[obj],
        ):
            result = bpy.ops.object.modifier_apply(modifier=modifier.name)
    if "FINISHED" not in result:
        raise RuntimeError(f"Blender did not apply modifier '{modifier.name}': {sorted(result)}")


class ClothProxyRigHandlers:
    """Blender-main-thread handlers for cloth proxy rigs."""

    def create_cloth_proxy_rig(
        self,
        render_object_name,
        proxy_object_name,
        proxy_source_policy="EXISTING",
        bind_type="SURFACE_DEFORM",
        cloth_modifier_name="Cloth",
        bind_modifier_name="Cloth Proxy Bind",
        existing_policy="ERROR",
        allow_topology_change=False,
        decimate_ratio=0.25,
        vertex_group_name=None,
        surface_deform_falloff=4.0,
        mesh_deform_precision=5,
        rest_frame=1,
        validation_frames=None,
    ):
        render_obj = _get_object(render_object_name, {"MESH"})
        sync_from_editmode(render_obj)
        _validate_id_name(proxy_object_name, "proxy_object_name")
        _validate_id_name(cloth_modifier_name, "cloth_modifier_name")
        _validate_id_name(bind_modifier_name, "bind_modifier_name")
        if proxy_source_policy not in {"EXISTING", "DUPLICATE_RENDER", "DECIMATE_RENDER"}:
            raise ValueError("proxy_source_policy must be EXISTING, DUPLICATE_RENDER, or DECIMATE_RENDER")
        if bind_type not in {"SURFACE_DEFORM", "MESH_DEFORM"}:
            raise ValueError("bind_type must be SURFACE_DEFORM or MESH_DEFORM")
        if existing_policy not in {"ERROR", "REUSE"}:
            raise ValueError("existing_policy must be ERROR or REUSE")
        if render_object_name == proxy_object_name:
            raise ValueError("Render and proxy objects must be distinct")
        _finite(decimate_ratio, "decimate_ratio")
        if not 0.01 <= decimate_ratio <= 1.0:
            raise ValueError("decimate_ratio must be in [0.01, 1.0]")
        if proxy_source_policy == "DECIMATE_RENDER" and not allow_topology_change:
            raise ValueError("DECIMATE_RENDER requires allow_topology_change=True")
        frame_set: set[int] = {int(rest_frame)}
        frame_set.update(int(frame) for frame in validation_frames or [])
        frames = sorted(frame_set)
        if len(frames) > 12:
            raise ValueError("At most 12 unique rest/validation frames may be evaluated")
        if vertex_group_name and render_obj.vertex_groups.get(vertex_group_name) is None:
            raise ValueError(f"Render vertex group not found: {vertex_group_name}")
        scene, view_layer = _scene_context_for_object(render_obj)
        original_frame = scene.frame_current
        original_subframe = scene.frame_subframe
        created_proxy = False
        proxy_data = None
        created_cloth = False
        created_bind = False
        bound_during_request = False
        ownership_records = []
        original_bind_index = None
        bind_snapshot = None
        proxy_obj = None
        simulation_id = uuid.uuid4().hex
        rig_property = f"{_OWNERSHIP_PREFIX}_proxy_rig_{simulation_id}"
        try:
            existing_proxy = bpy.data.objects.get(proxy_object_name)
            if proxy_source_policy == "EXISTING":
                proxy_obj = _get_object(proxy_object_name, {"MESH"})
                sync_from_editmode(proxy_obj)
                if proxy_obj.name not in scene.objects:
                    raise ValueError(f"Proxy '{proxy_obj.name}' is not linked to render scene '{scene.name}'")
            else:
                if existing_proxy is not None:
                    raise ValueError(f"Object already exists: {proxy_object_name}")
                proxy_obj = render_obj.copy()
                proxy_obj.name = proxy_object_name
                proxy_data = render_obj.data.copy()
                proxy_obj.data = proxy_data
                for modifier in list(proxy_obj.modifiers):
                    if modifier.type not in {"ARMATURE", "HOOK", "LATTICE"}:
                        proxy_obj.modifiers.remove(modifier)
                collection = render_obj.users_collection[0] if render_obj.users_collection else scene.collection
                collection.objects.link(proxy_obj)
                created_proxy = True
                ownership_records.append(
                    (proxy_obj, _tag_owned_object(proxy_obj, "simulation_proxy", simulation_id, render_obj.name))
                )
                if proxy_source_policy == "DECIMATE_RENDER" and decimate_ratio < 1.0:
                    if getattr(proxy_obj.data, "shape_keys", None) is not None:
                        proxy_obj.shape_key_clear()
                    decimate = proxy_obj.modifiers.new(name="Cloth Proxy Decimate", type="DECIMATE")
                    decimate.decimate_type = "COLLAPSE"
                    decimate.ratio = decimate_ratio
                    proxy_obj.modifiers.move(list(proxy_obj.modifiers).index(decimate), 0)
                    _apply_named_modifier(proxy_obj, decimate)
            if not proxy_obj.data.vertices or not proxy_obj.data.polygons:
                raise ValueError(f"Proxy '{proxy_obj.name}' must have nonempty vertices and faces")

            cloth_modifier = proxy_obj.modifiers.get(cloth_modifier_name)
            if cloth_modifier is not None:
                if cloth_modifier.type != "CLOTH":
                    raise ValueError(f"Modifier '{cloth_modifier_name}' on proxy is not Cloth")
                if existing_policy == "ERROR":
                    raise ValueError(f"Cloth modifier '{cloth_modifier_name}' already exists on proxy")
                _reject_baked([(proxy_obj, cloth_modifier)])
            else:
                cloth_modifier = proxy_obj.modifiers.new(name=cloth_modifier_name, type="CLOTH")
                created_cloth = True
                view_layer.update()
                _configure_independent_cache(
                    cloth_modifier.point_cache,
                    proxy_obj.name,
                    cloth_modifier.name,
                    identity_token=simulation_id,
                )
                ownership_records.append(
                    (
                        proxy_obj,
                        _tag_owned_component(
                            proxy_obj,
                            cloth_modifier,
                            "cloth_proxy",
                            simulation_id,
                            render_obj.name,
                        ),
                    )
                )
            if cloth_modifier.settings is None or cloth_modifier.point_cache is None:
                raise RuntimeError("Blender did not initialize the proxy Cloth modifier")

            bind_modifier = render_obj.modifiers.get(bind_modifier_name)
            if bind_modifier is not None:
                if bind_modifier.type != bind_type:
                    raise ValueError(
                        f"Modifier '{bind_modifier_name}' is {bind_modifier.type}, not requested {bind_type}"
                    )
                if existing_policy == "ERROR":
                    raise ValueError(f"Binding modifier '{bind_modifier_name}' already exists")
                original_bind_index = list(render_obj.modifiers).index(bind_modifier)
                bind_snapshot = _snapshot_attachment_modifier(bind_modifier)
                current_target = _modifier_dependency_target(bind_modifier)
                if bind_modifier.is_bound and current_target != proxy_obj:
                    raise ValueError("A bound reused deformation modifier cannot be retargeted safely")
            else:
                bind_modifier = render_obj.modifiers.new(name=bind_modifier_name, type=bind_type)
                created_bind = True
            if bind_modifier.is_bound:
                expected_setting = (
                    _matches_stored_float(bind_modifier.falloff, surface_deform_falloff)
                    if bind_type == "SURFACE_DEFORM"
                    else int(bind_modifier.precision) == int(mesh_deform_precision)
                )
                if bind_modifier.vertex_group != (vertex_group_name or "") or not expected_setting:
                    raise ValueError("A bound reused deformation modifier does not match the requested settings")
            else:
                _set_modifier_dependency_target(bind_modifier, proxy_obj)
                if hasattr(bind_modifier, "vertex_group"):
                    bind_modifier.vertex_group = vertex_group_name or ""
                if bind_type == "SURFACE_DEFORM":
                    _validate_rna_value(bind_modifier, "falloff", surface_deform_falloff)
                    bind_modifier.falloff = surface_deform_falloff
                else:
                    _validate_rna_value(bind_modifier, "precision", mesh_deform_precision)
                    bind_modifier.precision = mesh_deform_precision
                scene.frame_set(rest_frame)
                view_layer.update()
                proximity = _proxy_proximity_evidence(render_obj, proxy_obj, view_layer.depsgraph)
                _bind_deform_modifier(render_obj, bind_modifier)
                bound_during_request = True
            if bind_modifier.is_bound and "proximity" not in locals():
                proximity = _proxy_proximity_evidence(render_obj, proxy_obj, view_layer.depsgraph)
            if created_bind:
                ownership_records.append(
                    (
                        render_obj,
                        _tag_owned_component(
                            render_obj,
                            bind_modifier,
                            "proxy_binding",
                            simulation_id,
                            proxy_obj.name,
                        ),
                    )
                )
            validation = []
            for frame in frames:
                scene.frame_set(frame)
                view_layer.update()
                validation.append(
                    {
                        "frame": frame,
                        "proxy": _evaluated_geometry_evidence(proxy_obj, view_layer.depsgraph),
                        "render": _evaluated_geometry_evidence(render_obj, view_layer.depsgraph),
                    }
                )
            render_obj[rig_property] = json.dumps(
                {
                    "owned": True,
                    "simulation_id": simulation_id,
                    "role": "proxy_rig",
                    "proxy": proxy_obj.name,
                    "render": render_obj.name,
                    "binding": bind_modifier.name,
                    "schema_version": _MCP_SCHEMA_VERSION,
                },
                sort_keys=True,
            )
            _tag_update(render_obj)
        except Exception:
            with contextlib.suppress(Exception):
                _remove_custom_property(render_obj, rig_property)
            for owner, record in reversed(ownership_records):
                with contextlib.suppress(Exception):
                    del owner[record["object_property"]]
            if created_bind and "bind_modifier" in locals():
                with contextlib.suppress(Exception):
                    render_obj.modifiers.remove(bind_modifier)
            elif bind_snapshot is not None and "bind_modifier" in locals():
                if bound_during_request:
                    with contextlib.suppress(Exception):
                        _unbind_deform_modifier(render_obj, bind_modifier)
                _restore_attachment_modifier(bind_modifier, bind_snapshot)
                with contextlib.suppress(Exception):
                    render_obj.modifiers.move(list(render_obj.modifiers).index(bind_modifier), original_bind_index)
            if created_cloth and proxy_obj is not None and not created_proxy:
                with contextlib.suppress(Exception):
                    proxy_obj.modifiers.remove(cloth_modifier)
            if created_proxy and proxy_obj is not None:
                _remove_created_object(proxy_obj, proxy_data)
            raise
        finally:
            scene.frame_set(original_frame, subframe=original_subframe)
            view_layer.update()
        warnings = []
        topology = _topology_summary(proxy_obj)
        if topology["non_manifold_edges"]:
            warnings.append("Proxy mesh is non-manifold; Mesh Deform binding and cloth behavior may be unreliable.")
        if proximity["maximum_distance"] and proximity["maximum_distance"] > max(render_obj.dimensions) * 0.1:
            warnings.append("Some render vertices are far from the proxy relative to the render bounds.")
        return {
            "changed_objects": [render_obj.name, proxy_obj.name],
            "changed_resources": [proxy_data.name] if proxy_data is not None else [],
            "render_object": render_obj.name,
            "proxy_object": proxy_obj.name,
            "proxy_created": created_proxy,
            "simulation_id": simulation_id,
            "proxy_source_policy": proxy_source_policy,
            "topology_changed": proxy_source_policy == "DECIMATE_RENDER" and decimate_ratio < 1.0,
            "cloth_modifier": _modifier_info(proxy_obj, cloth_modifier),
            "binding_modifier": {**_modifier_info(render_obj, bind_modifier), "is_bound": bind_modifier.is_bound},
            "rest_frame": rest_frame,
            "rest_coverage": proximity,
            "proxy_topology": topology,
            "validation_frames": validation,
            "source_geometry_preserved": True,
            "retained_live_dependencies": True,
            "ownership": [record for _owner, record in ownership_records],
            "warnings": warnings,
        }
