# pyright: reportGeneralTypeIssues=false
"""Keyframe animation of liquid flow-source properties."""

import contextlib
import math

import bpy

from .inspection_and_setup import (
    _get_domain,
    _get_role,
    _object_in_collection,
    _reject_baked,
    _serialize,
    _validate_rna_value,
)

_FLOW_ANIMATION_FIELDS = {
    "use_inflow",
    "use_initial_velocity",
    "velocity_factor",
    "velocity_normal",
    "velocity_random",
}


def _action_fcurves(owner):
    owner_id = getattr(owner, "id_data", owner)
    animation = getattr(owner_id, "animation_data", None)
    action = getattr(animation, "action", None)
    if action is None:
        return []
    slot = getattr(animation, "action_slot", None)
    layers = getattr(action, "layers", None)
    if slot is not None and layers is not None:
        curves = []
        for layer in layers:
            for strip in layer.strips:
                if getattr(strip, "type", None) != "KEYFRAME":
                    continue
                channelbag = strip.channelbag(slot)
                if channelbag is not None:
                    curves.extend(channelbag.fcurves)
        return curves
    return list(getattr(action, "fcurves", ()))


def _key_points(owner, data_path, frame):
    return [
        (curve, point)
        for curve in _action_fcurves(owner)
        if curve.data_path == data_path
        for point in curve.keyframe_points
        if abs(float(point.co[0]) - frame) <= 1e-6
    ]


def _snapshot_point(point):
    return {
        "co": list(point.co),
        "interpolation": point.interpolation,
        "easing": point.easing,
        "handle_left": list(point.handle_left),
        "handle_right": list(point.handle_right),
        "handle_left_type": point.handle_left_type,
        "handle_right_type": point.handle_right_type,
    }


def _restore_point(point, snapshot):
    for name, value in snapshot.items():
        setattr(point, name, value)


class LiquidAnimationHandlers:
    """Insert and roll back keyframes on liquid flow-source properties."""

    def animate_liquid_flow(
        self,
        object_name,
        modifier_name,
        domain_object_name,
        keyframes,
        policy="INSERT_ONLY",
        subframes=None,
    ):
        obj, modifier, flow = _get_role(object_name, modifier_name, "FLOW")
        domain_obj, _domain_modifier, domain = _get_domain(domain_object_name)
        _reject_baked(domain)
        if policy not in {"INSERT_ONLY", "REPLACE_EXISTING"}:
            raise ValueError("policy must be INSERT_ONLY or REPLACE_EXISTING")
        if not keyframes or len(keyframes) > 500:
            raise ValueError("keyframes must contain 1-500 records")
        if domain.fluid_group is not None and not _object_in_collection(obj, domain.fluid_group):
            raise ValueError(f"Flow '{obj.name}' is outside domain collection '{domain.fluid_group.name}'")
        if subframes is not None:
            _validate_rna_value(flow, "subframes", subframes)
        resolved = []
        identities = set()
        for index, record in enumerate(keyframes):
            frame = float(record["frame"])
            if not math.isfinite(frame) or not -1_000_000 <= frame <= 1_000_000:
                raise ValueError(f"Keyframe {index} has an invalid frame")
            properties = set(record) & _FLOW_ANIMATION_FIELDS
            if len(properties) != 1:
                raise ValueError(f"Keyframe {index} must set exactly one flow property")
            property_name = properties.pop()
            value = record[property_name]
            prop = flow.bl_rna.properties.get(property_name)
            if prop is None or prop.is_readonly or not prop.is_animatable:
                raise ValueError(
                    f"FluidFlowSettings.{property_name} is not keyable in Blender {bpy.app.version_string}"
                )
            _validate_rna_value(flow, property_name, value)
            identity = (property_name, frame)
            if identity in identities:
                raise ValueError(f"Duplicate keyframe for {property_name} at {frame:g}")
            identities.add(identity)
            path = flow.path_from_id(property_name)
            existing = _key_points(flow, path, frame)
            if policy == "INSERT_ONLY" and existing:
                raise ValueError(f"A key already exists for {property_name} at frame {frame:g}")
            resolved.append(
                {
                    "property": property_name,
                    "value": value,
                    "frame": frame,
                    "path": path,
                    "interpolation": record.get("interpolation", "CONSTANT"),
                    "old_value": _serialize(getattr(flow, property_name)),
                    "existing": [(curve, point, _snapshot_point(point)) for curve, point in existing],
                }
            )
        old_subframes = flow.subframes
        applied = []
        try:
            if subframes is not None:
                flow.subframes = subframes
            for entry in resolved:
                applied.append(entry)
                setattr(flow, entry["property"], entry["value"])
                inserted = flow.keyframe_insert(data_path=entry["property"], frame=entry["frame"], group="Liquid MCP")
                if not inserted:
                    raise RuntimeError(f"Blender did not insert {entry['property']} at frame {entry['frame']:g}")
                points = _key_points(flow, entry["path"], entry["frame"])
                if not points:
                    raise RuntimeError("Inserted flow keyframe could not be found in the object action")
                for curve, point in points:
                    point.interpolation = entry["interpolation"]
                    curve.update()
            obj.update_tag(refresh={"DATA"})
            bpy.context.view_layer.update()
        except Exception:
            flow.subframes = old_subframes
            for entry in reversed(applied):
                with contextlib.suppress(Exception):
                    setattr(flow, entry["property"], entry["old_value"])
                if entry["existing"]:
                    for curve, point, snapshot in entry["existing"]:
                        with contextlib.suppress(Exception):
                            _restore_point(point, snapshot)
                            curve.update()
                else:
                    with contextlib.suppress(Exception):
                        flow.keyframe_delete(data_path=entry["property"], frame=entry["frame"])
            raise
        animation = getattr(obj, "animation_data", None)
        action = getattr(animation, "action", None)
        keyed = [
            {
                "property": entry["property"],
                "data_path": entry["path"],
                "frame": entry["frame"],
                "value": entry["value"],
                "interpolation": entry["interpolation"],
            }
            for entry in resolved
        ]
        warnings = ["Flow animation invalidates data, mesh, and particle cache stages."]
        if flow.flow_behavior == "GEOMETRY":
            warnings.append("GEOMETRY is a one-shot source; use INFLOW for continuous emission scheduling.")
        if any(item["property"] == "use_inflow" for item in keyed) and flow.flow_behavior != "INFLOW":
            warnings.append("use_inflow keys have no continuous-emission meaning unless flow_behavior is INFLOW.")
        return {
            "changed_objects": [obj.name, domain_obj.name],
            "object": obj.name,
            "modifier": modifier.name,
            "domain": domain_obj.name,
            "action": action.name if action else None,
            "action_slot": getattr(getattr(animation, "action_slot", None), "identifier", None),
            "policy": policy,
            "subframes": int(flow.subframes),
            "keyframes": keyed,
            "invalidated_cache_stages": ["DATA", "MESH", "PARTICLES"],
            "warnings": warnings,
        }
