# pyright: reportGeneralTypeIssues=false, reportOptionalSubscript=false
"""Read-only lighting inventory, scene inspection, and validation handlers."""

import math

import bpy
import mathutils

from ._shared import (
    LIGHT_TYPES,
    bounded_page,
    collection_in_scene,
    collection_is_in_tree,
    engine_identifiers,
    external_file_findings,
    light_object,
    light_snapshot,
    light_summary,
    node_tree_snapshot,
    plain,
    resolve_engine,
    scene_by_name,
)

MAX_SCENE_RESOURCES = 500


def _scene_lights(scene, collection=None, light_type=None):
    """Return stable-name-sorted light objects in the requested scope."""
    source = collection.all_objects if collection is not None else scene.objects
    lights = [obj for obj in source if obj.type == "LIGHT" and obj.name in scene.objects]
    if light_type is not None:
        if light_type not in LIGHT_TYPES:
            raise ValueError(f"light_type must be one of {sorted(LIGHT_TYPES)}")
        lights = [obj for obj in lights if obj.data.type == light_type]
    return sorted(lights, key=lambda item: item.name)


def _world_snapshot(world):
    """Serialize the scene World and its bounded shader graph."""
    if world is None:
        return None
    return {
        "name": world.name,
        "use_nodes": bool(world.use_nodes),
        "color": list(world.color),
        "node_tree": node_tree_snapshot(world.node_tree),
    }


def _color_management_snapshot(scene):
    """Serialize display settings that affect lighting evaluation."""
    settings = scene.view_settings
    return {
        "view_transform": settings.view_transform,
        "look": settings.look,
        "exposure": float(settings.exposure),
        "exposure_multiplier": float(2.0**settings.exposure),
        "gamma": float(settings.gamma),
    }


def _quality_snapshot(scene):
    """Serialize the allowlisted Cycles and EEVEE lighting-quality settings present at runtime."""
    cycles_fields = (
        "samples",
        "use_adaptive_sampling",
        "adaptive_threshold",
        "use_denoising",
        "light_sampling_threshold",
        "sample_clamp_direct",
        "sample_clamp_indirect",
        "max_bounces",
        "diffuse_bounces",
        "glossy_bounces",
        "transmission_bounces",
        "transparent_max_bounces",
        "volume_bounces",
        "device",
    )
    eevee_fields = (
        "taa_render_samples",
        "light_threshold",
        "shadow_pool_size",
        "shadow_resolution_scale",
        "shadow_ray_count",
        "shadow_step_count",
        "use_raytracing",
        "ray_tracing_method",
        "use_fast_gi",
        "volumetric_tile_size",
        "volumetric_samples",
        "volumetric_ray_depth",
    )
    cycles = getattr(scene, "cycles", None)
    eevee = getattr(scene, "eevee", None)
    return {
        "cycles": {
            field: plain(getattr(cycles, field))
            for field in cycles_fields
            if cycles is not None and hasattr(cycles, field)
        },
        "eevee": {
            field: plain(getattr(eevee, field)) for field in eevee_fields if eevee is not None and hasattr(eevee, field)
        },
    }


def _material_resources(scene):
    """Find bounded emissive and volume materials actually assigned in the scene."""
    emissive = []
    volumes = []
    seen = set()
    scan_truncated = False
    for obj in scene.objects:
        for slot in obj.material_slots:
            material = slot.material
            if material is None or material.name in seen or not material.use_nodes or material.node_tree is None:
                continue
            if len(seen) >= MAX_SCENE_RESOURCES:
                scan_truncated = True
                break
            seen.add(material.name)
            types = {node.bl_idname for node in material.node_tree.nodes}
            material_objects = sorted(
                candidate.name
                for candidate in scene.objects
                if any(candidate_slot.material == material for candidate_slot in candidate.material_slots)
            )
            record = {"material": material.name, "objects": material_objects}
            if "ShaderNodeEmission" in types or "ShaderNodeBsdfPrincipled" in types:
                principled_emission = any(
                    node.bl_idname == "ShaderNodeBsdfPrincipled"
                    and any(
                        socket.name in {"Emission", "Emission Color"}
                        and any(float(channel) > 0 for channel in socket.default_value[:3])
                        for socket in node.inputs
                        if hasattr(socket, "default_value") and hasattr(socket.default_value, "__len__")
                    )
                    for node in material.node_tree.nodes
                )
                if "ShaderNodeEmission" in types or principled_emission:
                    emissive.append(record)
            if "ShaderNodeVolumePrincipled" in types:
                volumes.append(record)
        if scan_truncated:
            break
    return emissive, volumes, scan_truncated


def _probe_records(scene):
    """Return bounded EEVEE light-probe summaries."""
    probes = []
    for obj in scene.objects:
        if obj.type != "LIGHT_PROBE":
            continue
        data = obj.data
        record = {
            "object": obj.name,
            "probe_data": data.name,
            "probe_type": data.type,
            "hidden_viewport": bool(obj.hide_viewport),
            "hidden_render": bool(obj.hide_render),
        }
        for field in ("resolution_x", "resolution_y", "resolution_z", "bake_samples"):
            if hasattr(data, field):
                record[field] = plain(getattr(data, field))
        probes.append(record)
        if len(probes) >= MAX_SCENE_RESOURCES:
            break
    return probes


def _excluded_collections(view_layer):
    """List collections excluded in one view layer."""
    excluded = []

    def visit(layer_collection, path):
        current = [*path, layer_collection.name]
        if layer_collection.exclude:
            excluded.append("/".join(current))
        for child in layer_collection.children:
            visit(child, current)

    visit(view_layer.layer_collection, [])
    return excluded


def _finding(severity, code, resource, message, evidence, remediation):
    """Build one stable, agent-actionable validation finding."""
    return {
        "severity": severity,
        "code": code,
        "resource": resource,
        "message": message,
        "evidence": evidence,
        "remediation": remediation,
    }


class LightingInspectionHandlers:
    """Provide read-only light inventory, detailed inspection, scene snapshots, and audits."""

    def list_lights(self, scene_name, collection_name=None, light_type=None, limit=50, offset=0, detail=False):
        """Return one stable page of light records, trimmed unless detail is requested."""
        scene = scene_by_name(scene_name)
        collection = collection_in_scene(scene, collection_name) if collection_name else None
        lights = _scene_lights(scene, collection, light_type)
        start, end, truncated, next_offset = bounded_page(len(lights), offset, limit)
        record = light_snapshot if detail else light_summary
        records = [record(obj) for obj in lights[start:end]]
        return {
            "scene": scene.name,
            "collection": collection.name if collection else None,
            "light_type_filter": light_type,
            "detail": bool(detail),
            "lights": records,
            "total": len(lights),
            "offset": start,
            "limit": min(int(limit), 200),
            "returned_count": len(records),
            "truncated": truncated,
            "next_offset": next_offset,
            "scene_unit_scale": float(scene.unit_settings.scale_length),
        }

    def inspect_light(self, scene_name, light_name):
        """Return complete bounded configuration for one scene light."""
        scene = scene_by_name(scene_name)
        obj = light_object(light_name, scene=scene)
        return {"scene": scene.name, **light_snapshot(obj, include_nodes=True)}

    def inspect_lighting_setup(self, scene_name, limit=50, offset=0, detail=False):
        """Return a bounded, reproducible scene-level lighting snapshot."""
        scene = scene_by_name(scene_name)
        lights = _scene_lights(scene)
        start, end, truncated, next_offset = bounded_page(len(lights), offset, limit)
        record = light_snapshot if detail else light_summary
        emissive, volumes, materials_truncated = _material_resources(scene)
        view_layer = bpy.context.view_layer if bpy.context.scene == scene else scene.view_layers[0]
        return {
            "scene": scene.name,
            "render_engine": scene.render.engine,
            "available_engines": engine_identifiers(),
            "units": {
                "system": scene.unit_settings.system,
                "scale_length": float(scene.unit_settings.scale_length),
                "length_unit": scene.unit_settings.length_unit,
            },
            "camera": scene.camera.name if scene.camera else None,
            "color_management": _color_management_snapshot(scene),
            "world": _world_snapshot(scene.world),
            "lights_detail": bool(detail),
            "lights": [record(obj) for obj in lights[start:end]],
            "lights_total": len(lights),
            "lights_offset": start,
            "lights_limit": min(int(limit), 200),
            "lights_truncated": truncated,
            "lights_next_offset": next_offset,
            "emissive_materials": emissive,
            "volume_materials": volumes,
            "material_scan_truncated": materials_truncated,
            "eevee_probes": _probe_records(scene),
            "excluded_collections": _excluded_collections(view_layer),
            "quality": _quality_snapshot(scene),
        }

    def validate_lighting_setup(
        self,
        scene_name,
        target_engine="BOTH",
        subject_object_names=None,
        limit=100,
        offset=0,
    ):
        """Audit lighting readiness and return paginated evidence-rich findings."""
        scene = scene_by_name(scene_name)
        if target_engine not in {"BOTH", "CYCLES", "EEVEE"}:
            raise ValueError("target_engine must be BOTH, CYCLES, or EEVEE")
        subjects = []
        if subject_object_names is not None:
            if len(subject_object_names) > 100 or len(set(subject_object_names)) != len(subject_object_names):
                raise ValueError("subject_object_names must be unique and contain at most 100 names")
            from ._shared import object_in_scene

            subjects = [object_in_scene(scene, name) for name in subject_object_names]
        findings = []
        if scene.camera is None:
            findings.append(
                _finding(
                    "ERROR",
                    "MISSING_CAMERA",
                    scene.name,
                    "The scene has no active render camera.",
                    {"scene_camera": None},
                    "Assign an explicit scene camera before preview or delivery renders.",
                )
            )
        for engine in ["CYCLES", "EEVEE"] if target_engine == "BOTH" else [target_engine]:
            try:
                resolve_engine(engine)
            except ValueError as exc:
                findings.append(
                    _finding(
                        "ERROR",
                        "ENGINE_UNAVAILABLE",
                        scene.name,
                        str(exc),
                        {"available_engines": engine_identifiers()},
                        f"Enable a Blender build/runtime that registers {engine}.",
                    )
                )
        scale = float(scene.unit_settings.scale_length)
        if not math.isfinite(scale) or scale <= 0 or scale < 1e-4 or scale > 1e4:
            findings.append(
                _finding(
                    "WARNING",
                    "EXTREME_SCENE_SCALE",
                    scene.name,
                    "Scene unit scale is outside a predictable lighting range.",
                    {"scale_length": scale},
                    "Confirm real-world scale before tuning light size, falloff, and power.",
                )
            )
        lights = _scene_lights(scene)
        for index, obj in enumerate(lights):
            data = obj.data
            effective_power = float(data.energy) * (2.0 ** float(getattr(data, "exposure", 0.0)))
            if not math.isfinite(float(data.energy)) or data.energy <= 0:
                findings.append(
                    _finding(
                        "ERROR",
                        "NONPOSITIVE_LIGHT_ENERGY",
                        obj.name,
                        "Light energy must be positive and finite for predictable illumination.",
                        {"energy": float(data.energy)},
                        "Set a positive finite energy or disable the light object intentionally.",
                    )
                )
            elif effective_power > 1e9:
                findings.append(
                    _finding(
                        "WARNING",
                        "EXTREME_EFFECTIVE_POWER",
                        obj.name,
                        "Energy combined with exposure is unusually high and may clip the render.",
                        {
                            "energy": float(data.energy),
                            "exposure": float(data.exposure),
                            "effective_power": effective_power,
                        },
                        "Check scene scale and reduce energy or exposure deliberately.",
                    )
                )
            if not data.use_shadow:
                findings.append(
                    _finding(
                        "WARNING",
                        "SHADOWS_DISABLED",
                        obj.name,
                        "This light does not cast shadows.",
                        {"use_shadow": False},
                        "Enable shadows unless shadowless fill is intentional.",
                    )
                )
            if obj.hide_render or obj.hide_viewport:
                findings.append(
                    _finding(
                        "INFO",
                        "LIGHT_DISABLED",
                        obj.name,
                        "The light is hidden in a viewport or final render.",
                        {"hide_viewport": bool(obj.hide_viewport), "hide_render": bool(obj.hide_render)},
                        "Confirm the visibility state is intentional for the target render.",
                    )
                )
            for dependency in external_file_findings(data.node_tree):
                findings.append(
                    _finding(
                        "ERROR",
                        "MISSING_LIGHT_DEPENDENCY",
                        obj.name,
                        "A light shader depends on an unavailable file.",
                        dependency,
                        "Restore the persistent image/IES file or update the managed dependency.",
                    )
                )
            linking = getattr(obj, "light_linking", None)
            for role in ("receiver_collection", "blocker_collection"):
                collection = getattr(linking, role, None) if linking is not None else None
                if collection is not None and not collection_is_in_tree(scene.collection, collection):
                    findings.append(
                        _finding(
                            "ERROR",
                            "LINK_COLLECTION_OUTSIDE_SCENE",
                            obj.name,
                            "A light-link collection is not linked into this scene.",
                            {"role": role, "collection": collection.name},
                            "Use a collection linked to the light's scene.",
                        )
                    )
            location = obj.matrix_world.translation
            for other in lights[index + 1 :]:
                if data.type != other.data.type:
                    continue
                same_position = (location - other.matrix_world.translation).length <= 1e-5
                same_direction = (obj.matrix_world.to_quaternion() @ mathutils.Vector((0.0, 0.0, -1.0))).dot(
                    other.matrix_world.to_quaternion() @ mathutils.Vector((0.0, 0.0, -1.0))
                ) >= 1.0 - 1e-6
                duplicate_transform = same_direction if data.type == "SUN" else same_position
                if data.type in {"SPOT", "AREA"}:
                    duplicate_transform = same_position and same_direction
                if duplicate_transform:
                    findings.append(
                        _finding(
                            "WARNING",
                            "COINCIDENT_LIGHTS",
                            obj.name,
                            "Two lights of the same type occupy the same world position.",
                            {"other_light": other.name, "world_location": list(location)},
                            "Confirm both lights are intentional or separate their transforms.",
                        )
                    )
            if subjects and data.type in {"SPOT", "AREA", "SUN"}:
                direction = obj.matrix_world.to_quaternion() @ mathutils.Vector((0.0, 0.0, -1.0))
                for subject in subjects:
                    toward = subject.matrix_world.translation - location
                    if toward.length_squared > 1e-12 and direction.dot(toward.normalized()) <= 0:
                        findings.append(
                            _finding(
                                "WARNING",
                                "LIGHT_AIMED_AWAY",
                                obj.name,
                                "The light's local -Z axis points away from a declared subject.",
                                {"subject": subject.name, "direction": list(direction)},
                                "Use aim_light or confirm intentional back-lighting.",
                            )
                        )
        if scene.world and scene.world.use_nodes:
            for dependency in external_file_findings(scene.world.node_tree):
                findings.append(
                    _finding(
                        "ERROR",
                        "MISSING_WORLD_DEPENDENCY",
                        scene.world.name,
                        "The World shader depends on an unavailable image/IES file.",
                        dependency,
                        "Restore the persistent source file or configure a new environment.",
                    )
                )
        probes = _probe_records(scene)
        volume_probes = [record for record in probes if record["probe_type"] == "VOLUME"]
        for probe in volume_probes:
            samples = (
                int(probe.get("resolution_x", 1))
                * int(probe.get("resolution_y", 1))
                * int(probe.get("resolution_z", 1))
            )
            if samples > 262_144:
                findings.append(
                    _finding(
                        "WARNING",
                        "EXPENSIVE_VOLUME_PROBE",
                        probe["object"],
                        "The EEVEE Volume probe grid is unusually large.",
                        {"grid_samples": samples},
                        "Reduce probe resolution or confirm the memory and bake budget.",
                    )
                )
        if target_engine == "BOTH":
            findings.append(
                _finding(
                    "INFO",
                    "CROSS_ENGINE_DIFFERENCES",
                    scene.name,
                    "Cycles and EEVEE do not guarantee equivalent indirect light, mesh emission, "
                    "volumes, IES, reflections, ray visibility, or sky sun discs.",
                    {"target_engine": "BOTH"},
                    "Render matched previews in both engines and compare the actual outputs.",
                )
            )
        exposure = float(scene.view_settings.exposure)
        if abs(exposure) > 10:
            findings.append(
                _finding(
                    "WARNING",
                    "EXTREME_DISPLAY_EXPOSURE",
                    scene.name,
                    "Display exposure may hide weak lighting or cause clipping.",
                    {"exposure_stops": exposure, "multiplier": 2.0**exposure},
                    "Evaluate lighting with an intentional, documented exposure.",
                )
            )
        severity_order = {"ERROR": 0, "WARNING": 1, "INFO": 2}
        findings.sort(key=lambda item: (severity_order[item["severity"]], item["code"], item["resource"]))
        start, end, truncated, next_offset = bounded_page(len(findings), offset, limit)
        page = findings[start:end]
        return {
            "scene": scene.name,
            "target_engine": target_engine,
            "valid": not any(item["severity"] == "ERROR" for item in findings),
            "summary": {
                severity: sum(item["severity"] == severity for item in findings)
                for severity in ("ERROR", "WARNING", "INFO")
            },
            "findings": page,
            "total": len(findings),
            "offset": start,
            "limit": min(int(limit), 200),
            "returned_count": len(page),
            "truncated": truncated,
            "next_offset": next_offset,
        }
