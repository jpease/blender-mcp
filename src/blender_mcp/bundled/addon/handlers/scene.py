# ruff: file-ignore[magic-value-comparison, too-many-branches, too-many-locals, undocumented-public-method]
"""Blender-side handlers for typed scene composition and native geometry."""

import os

from collections import Counter

import bpy
import mathutils

from ..helpers import (
    MAX_FRAME,
    MAX_LISTED_NAMES,
    MIN_FRAME,
    apply_modifier,
    bounded_int,
    counted_page,
    modifier_result,
    paginate,
    rotation_as_native_list,
)
from ..object_lookup import find_object
from ..text_hygiene import client_safe_text

_VALIDATE_SCENE_DOMAINS = ("scene", "camera", "lighting", "pbr", "cloth", "liquid", "persistence")
# How many items of a list `evidence` one finding may carry. A single PBR OVERLAPPING_UVS
# finding's evidence is every overlapping face pair `validate_pbr_asset` found, up to its own
# `overlap_pair_limit` (default 100) `[face_a, face_b]` entries at ~25-30 wire bytes each - about
# 3 KB of an 8 KiB reply. The envelope shortens the longest page first and `findings` strictly
# contains `evidence`, so that one finding used to starve every other domain's findings out of
# the same reply. Twelve entries still show the pattern; `evidence_omitted` says how many the
# caller is not seeing, and the per-domain validator still reports the full list on its own.
_MAX_FINDING_EVIDENCE_ITEMS = 12

# Every collection `transaction._TRACKED_COLLECTIONS` rolls back, minus `libraries`, spelled
# out rather than imported so this domain does not depend on another module's private name.
_PERSISTENCE_COLLECTIONS = (
    "objects",
    "meshes",
    "curves",
    "materials",
    "textures",
    "images",
    "node_groups",
    "worlds",
    "actions",
    "armatures",
    "cameras",
    "lights",
    "collections",
    "pointclouds",
    "volumes",
    "metaballs",
    "lattices",
    "grease_pencils",
)


def _normalized_domain_finding(domain, item):
    message = item.get("message")
    evidence = item.get("evidence")
    if message is None and isinstance(evidence, str):
        message, evidence = evidence, None
    omitted = 0
    if isinstance(evidence, list) and len(evidence) > _MAX_FINDING_EVIDENCE_ITEMS:
        omitted = len(evidence) - _MAX_FINDING_EVIDENCE_ITEMS
        evidence = evidence[:_MAX_FINDING_EVIDENCE_ITEMS]
    finding = {
        "domain": domain,
        "severity": item["severity"],
        "code": item.get("code"),
        "subject": item.get("subject") or item.get("object") or item.get("resource"),
        "message": message,
        "evidence": evidence,
        "remediation": item.get("remediation"),
    }
    if omitted:
        # Deliberately not `evidence_truncated`: the envelope reads that spelling as a page it
        # can resume with an `evidence` offset, and no side of this call accepts one.
        finding["evidence_omitted"] = omitted
    return finding


def _scene_level_findings(scene, active_domains):
    findings = []

    def add(severity, code, subject, message, evidence=None, remediation=None):
        findings.append(
            {
                "domain": "scene",
                "severity": severity,
                "code": code,
                "subject": subject,
                "message": message,
                "evidence": evidence,
                "remediation": remediation,
            }
        )

    if "camera" not in active_domains and "lighting" not in active_domains:
        if scene.camera is None:
            add(
                "ERROR",
                "MISSING_CAMERA",
                scene.name,
                "The scene has no active render camera.",
                remediation="Assign an explicit scene camera before rendering.",
            )
        elif scene.camera.type != "CAMERA":
            add(
                "ERROR",
                "INVALID_SCENE_CAMERA",
                scene.camera.name,
                "The active scene object is not a camera.",
                remediation="Assign a camera object as the scene camera.",
            )

    if not any(obj.type == "LIGHT" for obj in scene.objects):
        add(
            "WARNING",
            "MISSING_LIGHTS",
            scene.name,
            "The scene has no light objects.",
            remediation="Add at least one light, or confirm the world background provides the intended illumination.",
        )

    if scene.frame_end < scene.frame_start:
        add(
            "ERROR",
            "INVALID_FRAME_RANGE",
            scene.name,
            "frame_end is before frame_start.",
            evidence={"frame_start": scene.frame_start, "frame_end": scene.frame_end},
            remediation="Set frame_end to a value at or after frame_start.",
        )

    for obj in scene.objects:
        action = obj.animation_data.action if obj.animation_data else None
        if action is None:
            continue
        start, end = action.frame_range
        if start < scene.frame_start or end > scene.frame_end:
            add(
                "WARNING",
                "ANIMATION_OUTSIDE_FRAME_RANGE",
                obj.name,
                "The object's action extends outside the scene's playback frame range.",
                evidence={
                    "action_frame_range": [start, end],
                    "scene_frame_range": [scene.frame_start, scene.frame_end],
                },
                remediation="Extend the scene frame range, or trim/retime the action.",
            )

    for obj in scene.objects:
        if obj.type != "MESH":
            continue
        if any(abs(component - 1.0) > 1e-4 for component in obj.scale):
            add(
                "WARNING",
                "UNAPPLIED_SCALE",
                obj.name,
                "Object scale is not applied (not 1.0 on every axis).",
                evidence={"scale": [round(float(value), 6) for value in obj.scale]},
                remediation="Apply scale (Object > Apply > Scale) if this asset is finalized for delivery.",
            )
        degenerate = sum(1 for polygon in obj.data.polygons if polygon.area <= 1e-12)
        if degenerate:
            add(
                "WARNING",
                "DEGENERATE_GEOMETRY",
                obj.name,
                f"{degenerate} zero-area face(s) in the base mesh.",
                evidence={"zero_area_face_count": degenerate},
                remediation="Repair or remove degenerate faces before rendering.",
            )
        for modifier in obj.modifiers:
            if modifier.type == "CLOTH" and modifier.point_cache.is_outdated:
                add(
                    "WARNING",
                    "DIRTY_SIMULATION_CACHE",
                    obj.name,
                    f"Cloth modifier '{modifier.name}' cache is outdated relative to current settings.",
                    evidence={"modifier": modifier.name, "kind": "CLOTH"},
                    remediation="Rebake this cloth cache before relying on cached playback.",
                )

    if scene.rigidbody_world is not None and scene.rigidbody_world.point_cache.is_outdated:
        add(
            "WARNING",
            "DIRTY_SIMULATION_CACHE",
            scene.name,
            "Rigid body world cache is outdated relative to current settings.",
            evidence={"kind": "RIGID_BODY_WORLD"},
            remediation="Rebake the rigid body world cache before relying on cached playback.",
        )

    return findings


def _persistence_findings(max_findings):
    """
    Report local datablocks the save will discard, and actions that drive nothing.

    File-wide, not scene-scoped: `bpy.data` is what the save writes. Scans one match past
    `max_findings` so a full page can be told apart from a truncated one.

    Args:
        max_findings: Most findings to return.

    Returns:
        tuple[list[dict], bool]: The findings, and whether at least one more exists.

    """
    findings = []
    for coll_name in _PERSISTENCE_COLLECTIONS:
        for datablock in getattr(bpy.data, coll_name, ()):
            if getattr(datablock, "library", None) is not None:
                continue
            users = int(getattr(datablock, "users", 1) or 0)
            if users == 0:
                findings.append(
                    {
                        "severity": "WARNING",
                        "code": "UNREFERENCED_DATABLOCK",
                        "subject": client_safe_text(getattr(datablock, "name", ""), 64),
                        "message": "Nothing references this datablock, so the save will discard it.",
                        "evidence": {"collection": coll_name},
                        "remediation": (
                            "Assign it to an object, scene or animation, or set use_fake_user to keep it deliberately."
                        ),
                    }
                )
            elif coll_name == "actions" and getattr(datablock, "use_fake_user", False) and users <= 1:
                findings.append(
                    {
                        "severity": "INFO",
                        "code": "ACTION_KEPT_BY_FAKE_USER_ONLY",
                        "subject": client_safe_text(getattr(datablock, "name", ""), 64),
                        "message": "The action survives the save but drives nothing.",
                        "evidence": {"collection": "actions"},
                        "remediation": "Assign it with keyframe_character_pose or manage_action, or delete it.",
                    }
                )
            if len(findings) > max_findings:
                return findings[:max_findings], True
    return findings[:max_findings], len(findings) > max_findings


def _collected_domain_findings(handler, scene, domains, domain_limit):
    """
    Run every in-scope domain validator and normalize what each one found.

    Each domain is asked for `domain_limit` findings, not the caller's page length: a domain
    that stopped at the page length could not fill a page starting past it, so a resumed page
    came back empty while `total_findings` still promised more.

    Args:
        handler: The server whose per-domain validator methods are called.
        scene: The scene to validate.
        domains: Domain names to run, already filtered to the caller's scope.
        domain_limit: Findings to ask each domain for, within the [1, 1000] they accept.

    Returns:
        tuple[list[dict], dict]: The normalized findings, and each domain's count and whether
        it stopped at its own internal cap.

    """
    findings = []
    domain_summaries = {}

    def record(domain, items, *, truncated=False):
        normalized = [_normalized_domain_finding(domain, item) for item in items]
        findings.extend(normalized)
        domain_summaries[domain] = {"findings": len(normalized), "truncated": bool(truncated)}

    if "camera" in domains:
        record("camera", handler.validate_camera_rig(scene.name).get("findings", []))

    if "lighting" in domains:
        result = handler.validate_lighting_setup(scene.name, limit=200, offset=0)
        record("lighting", result.get("findings", []), truncated=result.get("truncated", False))

    if "pbr" in domains:
        mesh_names = sorted(obj.name for obj in scene.objects if obj.type == "MESH")
        if mesh_names:
            record("pbr", handler.validate_pbr_asset(object_names=mesh_names).get("findings", []))
        else:
            domain_summaries["pbr"] = {"findings": 0, "truncated": False}

    if "cloth" in domains:
        result = handler.validate_cloth_setup(scene.name, max_findings=domain_limit)
        record("cloth", result.get("findings", []), truncated=result.get("truncated", False))

    if "liquid" in domains:
        result = handler.validate_liquid_setup(scene.name, max_findings=domain_limit)
        record("liquid", result.get("findings", []), truncated=result.get("truncated", False))

    if "scene" in domains:
        record("scene", _scene_level_findings(scene, domains))

    if "persistence" in domains:
        items, persistence_truncated = _persistence_findings(domain_limit)
        record("persistence", items, truncated=persistence_truncated)

    return findings, domain_summaries


def _required_name(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def _object(name):
    """
    Resolve one object by name for every scene tool in this module.

    A name shared by an override and its linked original means the override, the
    only one of the pair these tools can edit. `server_core._resolve_targets`
    uses the same rule, so a rollback restores the object the handler changed.

    Args:
        name: The client-supplied object name.

    Returns:
        The one object that name means.

    Raises:
        ValueError: When the name is blank, unknown, or linked from several
            libraries with no local object.

    """
    obj = find_object(bpy.data.objects, _required_name(name, "object_name"))
    if obj is None:
        raise ValueError(f"Object not found: {name}")
    return obj


def _scene_collection(name=None):
    if name is None:
        return bpy.context.collection
    collection = bpy.data.collections.get(_required_name(name, "collection_name"))
    if collection is None:
        collection = bpy.data.collections.new(name)
        bpy.context.scene.collection.children.link(collection)
    return collection


def _update_view_layer():
    """
    Flush pending dependency-graph updates so world-space matrices are current.

    `matrix_world` is owned by the dependency graph: after a write to
    `location`, `rotation_*` or `matrix_basis` it keeps its pre-edit value -
    identity for an object this session has never evaluated - until the graph
    re-evaluates. Reading it for a reply, reading it to copy it, and assigning
    it (which solves a basis against the parent's evaluated matrix) all need
    the graph to have caught up first. `matrix_basis` is not affected; it is
    computed from the object's own channels on read.
    """
    view_layer = getattr(bpy.context, "view_layer", None)
    if view_layer is not None:
        view_layer.update()


def _transform_snapshot(obj):
    _update_view_layer()
    world_location, world_rotation, world_scale = obj.matrix_world.decompose()
    return {
        "location": list(obj.location),
        "rotation": rotation_as_native_list(obj),
        "rotation_mode": obj.rotation_mode,
        "scale": list(obj.scale),
        "matrix_world": [list(row) for row in obj.matrix_world],
        "world_location": list(world_location),
        "world_rotation_quaternion": list(world_rotation),
        "world_scale": list(world_scale),
    }


def _validate_mesh_topology(vertices, edges, faces):
    vertex_count = len(vertices)
    for label, records in (("edge", edges), ("face", faces)):
        for record_index, record in enumerate(records):
            if label == "edge" and len(record) != 2:
                raise ValueError(f"edge {record_index} must contain exactly two vertex indices")
            if label == "face" and len(record) < 3:
                raise ValueError(f"face {record_index} must contain at least three vertex indices")
            for index in record:
                if not isinstance(index, int) or index < 0 or index >= vertex_count:
                    raise ValueError(f"{label} {record_index} contains invalid vertex index {index}")


def _create_mesh(name, spec):
    vertices = spec.get("vertices", [])
    edges = spec.get("edges", [])
    faces = spec.get("faces", [])
    _validate_mesh_topology(vertices, edges, faces)
    data = bpy.data.meshes.new(name)
    data.from_pydata(vertices, edges, faces)
    data.validate(verbose=False)
    data.update()
    return data


def _create_spline_data(name, spec):
    geometry_type = spec["kind"]
    data = bpy.data.curves.new(name, type=geometry_type)
    data.dimensions = spec.get("dimensions", "3D")
    data.resolution_u = spec.get("resolution_u", 12)
    data.bevel_depth = spec.get("bevel_depth", 0.0)
    data.bevel_resolution = spec.get("bevel_resolution", 4)
    data.extrude = spec.get("extrude", 0.0)
    for record in spec["splines"]:
        spline_type = record.get("type", "POLY")
        if geometry_type == "SURFACE" and spline_type == "BEZIER":
            raise ValueError("SURFACE geometry does not support BEZIER splines")
        spline = data.splines.new(spline_type)
        points = record["points"]
        if spline_type == "BEZIER":
            spline.bezier_points.add(len(points) - 1)
            for point, raw in zip(spline.bezier_points, points, strict=True):
                record_point = raw if isinstance(raw, dict) else {"co": raw}
                point.co = record_point["co"]
                point.radius = record_point.get("radius", 1.0)
                point.tilt = record_point.get("tilt", 0.0)
                point.weight_softbody = record_point.get("weight", 1.0)
                point.handle_left_type = record_point.get("handle_left_type", "AUTO")
                point.handle_right_type = record_point.get("handle_right_type", "AUTO")
                if record_point.get("handle_left") is not None:
                    point.handle_left = record_point["handle_left"]
                if record_point.get("handle_right") is not None:
                    point.handle_right = record_point["handle_right"]
        else:
            spline.points.add(len(points) - 1)
            for point, raw in zip(spline.points, points, strict=True):
                record_point = raw if isinstance(raw, dict) else {"co": raw}
                point.co = (*record_point["co"], record_point.get("weight", 1.0))
                point.radius = record_point.get("radius", 1.0)
                point.tilt = record_point.get("tilt", 0.0)
                point.weight_softbody = record_point.get("weight", 1.0)
            if spline_type == "NURBS":
                count_u = record.get("point_count_u") or len(points)
                count_v = record.get("point_count_v", 1)
                if geometry_type == "SURFACE" and count_v > 1:
                    for property_name, value in (("point_count_u", count_u), ("point_count_v", count_v)):
                        prop = spline.bl_rna.properties.get(property_name)
                        if prop is None or prop.is_readonly:
                            raise ValueError(
                                "This Blender runtime cannot author arbitrary Surface U/V topology through "
                                f"Curve RNA ({property_name} is unavailable or read-only)"
                            )
                        setattr(spline, property_name, value)
                    spline.order_v = min(record.get("order_v", 4), count_v)
                    spline.use_endpoint_v = record.get("endpoint_v", True)
                    spline.use_cyclic_v = record.get("cyclic_v", False)
                spline.order_u = min(record.get("order_u", 4), count_u)
                spline.use_endpoint_u = record.get("endpoint", True)
        spline.use_cyclic_u = record.get("cyclic", False)
    return data


def _create_font(name, spec):
    data = bpy.data.curves.new(name, type="FONT")
    data.body = spec["body"]
    data.size = spec.get("size", 1.0)
    data.extrude = spec.get("extrude", 0.0)
    data.bevel_depth = spec.get("bevel_depth", 0.0)
    data.align_x = spec.get("align_x", "LEFT")
    data.align_y = spec.get("align_y", "TOP_BASELINE")
    return data


def _create_meta(name, spec):
    data = bpy.data.metaballs.new(name)
    data.resolution = spec.get("resolution", 0.4)
    data.render_resolution = spec.get("render_resolution", 0.2)
    data.threshold = spec.get("threshold", 0.6)
    for record in spec["elements"]:
        element = data.elements.new()
        element.co = record["co"]
        element.radius = record.get("radius", 1.0)
        element.stiffness = record.get("stiffness", 2.0)
        element.type = record.get("type", "BALL")
    return data


def _create_lattice(name, spec):
    data = bpy.data.lattices.new(name)
    data.points_u = spec.get("points_u", 2)
    data.points_v = spec.get("points_v", 2)
    data.points_w = spec.get("points_w", 2)
    return data


def _create_pointcloud(name, spec):
    data = bpy.data.pointclouds.new(name)
    points = spec.get("points", [])
    data.resize(len(points))
    if points:
        data.attributes["position"].data.foreach_set("vector", [value for point in points for value in point])
    radii = spec.get("radii")
    if radii is not None:
        attribute = data.attributes.get("radius") or data.attributes.new("radius", "FLOAT", "POINT")
        attribute.data.foreach_set("value", radii)
    return data


_ATTRIBUTE_VALUE_PROPERTIES = {
    "FLOAT": "value",
    "INT": "value",
    "BOOLEAN": "value",
    "FLOAT_VECTOR": "vector",
    "FLOAT_COLOR": "color",
    "BYTE_COLOR": "color",
}


def _write_geometry_attributes(container, records):
    schema = []
    for record in records:
        domain = "CURVE" if record["domain"] == "STROKE" else record["domain"]
        if domain == "LAYER":
            raise ValueError("LAYER attributes are not drawing-local; store them as layer custom properties")
        attribute = container.get(record["name"])
        if attribute is not None:
            if attribute.data_type != record["data_type"] or attribute.domain != domain:
                raise ValueError(f"Attribute already exists with incompatible schema: {record['name']}")
        else:
            attribute = container.new(record["name"], record["data_type"], domain)
        property_name = _ATTRIBUTE_VALUE_PROPERTIES[record["data_type"]]
        for element, value in zip(attribute.data, record["values"], strict=True):
            setattr(element, property_name, value)
        schema.append(
            {
                "name": attribute.name,
                "data_type": attribute.data_type,
                "domain": attribute.domain,
                "count": len(attribute.data),
            }
        )
    return schema


def _create_curves(name, spec):
    collection = getattr(bpy.data, "hair_curves", None)
    if collection is None:
        raise ValueError("Modern Curves geometry is unavailable in this Blender runtime")
    data = collection.new(name)
    data.add_curves(spec["curve_sizes"])
    positions = data.attributes.get("position")
    positions.data.foreach_set("vector", [value for point in spec["points"] for value in point])
    if spec.get("cyclic") is not None:
        cyclic = data.attributes.get("cyclic") or data.attributes.new("cyclic", "BOOLEAN", "CURVE")
        cyclic.data.foreach_set("value", spec["cyclic"])
    surface_name = spec.get("surface_object_name")
    if surface_name:
        surface = _object(surface_name)
        if surface.type != "MESH":
            raise ValueError("surface_object_name must identify a mesh object")
        data.surface = surface
    _write_geometry_attributes(data.attributes, spec.get("attributes", []))
    return data


def _create_grease_pencil(name, spec):
    collection = getattr(bpy.data, "grease_pencils", None)
    if collection is None:
        raise ValueError("Blender 5.x Grease Pencil geometry is unavailable in this runtime")
    data = collection.new(name)
    seen_layers = set()
    for layer_record in spec["layers"]:
        layer_name = layer_record["name"]
        if layer_name in seen_layers:
            raise ValueError(f"Duplicate Grease Pencil layer: {layer_name}")
        is_first_layer = not seen_layers
        seen_layers.add(layer_name)
        layer = data.layers.new(layer_name, set_active=is_first_layer)
        seen_frames = set()
        for frame_record in layer_record.get("frames", []):
            frame_number = frame_record["frame_number"]
            if frame_number in seen_frames:
                raise ValueError(f"Duplicate frame {frame_number} in layer '{layer_name}'")
            seen_frames.add(frame_number)
            drawing = layer.frames.new(frame_number).drawing
            strokes = frame_record.get("strokes", [])
            if not strokes:
                continue
            drawing.add_strokes([len(stroke["points"]) for stroke in strokes])
            points = [point for stroke in strokes for point in stroke["points"]]
            drawing.attributes["position"].data.foreach_set("vector", [value for point in points for value in point])
            cyclic = drawing.attributes.get("cyclic") or drawing.attributes.new("cyclic", "BOOLEAN", "CURVE")
            cyclic.data.foreach_set("value", [stroke.get("cyclic", False) for stroke in strokes])
            radii = [value for stroke in strokes for value in (stroke.get("radii") or [1.0] * len(stroke["points"]))]
            radius = drawing.attributes.get("radius") or drawing.attributes.new("radius", "FLOAT", "POINT")
            radius.data.foreach_set("value", radii)
            opacities = [
                value for stroke in strokes for value in (stroke.get("opacities") or [1.0] * len(stroke["points"]))
            ]
            opacity = drawing.attributes.get("opacity") or drawing.attributes.new("opacity", "FLOAT", "POINT")
            opacity.data.foreach_set("value", opacities)
            _write_geometry_attributes(drawing.attributes, frame_record.get("attributes", []))
    return data


def _create_volume(name, spec):
    path = os.path.abspath(spec["filepath"])
    if not os.path.isfile(path):
        raise ValueError(f"Volume file does not exist: {path}")
    if os.path.splitext(path)[1].lower() != ".vdb":
        raise ValueError("Volume filepath must use the .vdb extension")
    data = bpy.data.volumes.new(name)
    data.filepath = path
    data.is_sequence = spec.get("is_sequence", False)
    data.frame_start = spec.get("frame_start", 1)
    data.frame_duration = spec.get("frame_duration", 1)
    return data


_GEOMETRY_BUILDERS = {
    "MESH": _create_mesh,
    "CURVE": _create_spline_data,
    "SURFACE": _create_spline_data,
    "TEXT": _create_font,
    "META": _create_meta,
    "LATTICE": _create_lattice,
    "POINTCLOUD": _create_pointcloud,
    "CURVES": _create_curves,
    "GREASEPENCIL": _create_grease_pencil,
    "VOLUME": _create_volume,
}


def _attribute_schema(data):
    return [
        {"name": item.name, "data_type": item.data_type, "domain": item.domain, "count": len(item.data)}
        for item in getattr(data, "attributes", ())
    ]


def _geometry_counts(data, kind):
    if kind == "MESH":
        return {"points": len(data.vertices), "edges": len(data.edges), "faces": len(data.polygons)}
    if kind in {"CURVE", "SURFACE"}:
        return {
            "splines": len(data.splines),
            "points": sum(len(spline.points) + len(spline.bezier_points) for spline in data.splines),
        }
    if kind == "POINTCLOUD":
        return {"points": len(data.points)}
    if kind == "CURVES":
        return {"curves": len(data.curves), "points": len(data.points)}
    if kind == "GREASEPENCIL":
        frames = [frame for layer in data.layers for frame in layer.frames]
        return {
            "layers": len(data.layers),
            "frames": len(frames),
            "strokes": sum(len(frame.drawing.strokes) for frame in frames),
            "points": sum(len(frame.drawing.attributes["position"].data) for frame in frames),
        }
    return {}


_CONSTRAINT_SETTINGS = {
    "COPY_LOCATION": {"use_x", "use_y", "use_z", "invert_x", "invert_y", "invert_z", "use_offset", "head_tail"},
    "COPY_ROTATION": {"use_x", "use_y", "use_z", "invert_x", "invert_y", "invert_z", "mix_mode", "euler_order"},
    "COPY_SCALE": {"use_x", "use_y", "use_z", "power", "use_make_uniform", "use_offset", "use_add"},
    "COPY_TRANSFORMS": {"mix_mode", "remove_target_shear"},
    "CHILD_OF": {
        "use_location_x",
        "use_location_y",
        "use_location_z",
        "use_rotation_x",
        "use_rotation_y",
        "use_rotation_z",
        "use_scale_x",
        "use_scale_y",
        "use_scale_z",
    },
    "DAMPED_TRACK": {"track_axis", "head_tail"},
    "TRACK_TO": {"track_axis", "up_axis", "use_target_z", "head_tail"},
    "LOCKED_TRACK": {"track_axis", "lock_axis", "head_tail"},
    "FOLLOW_PATH": {
        "offset",
        "offset_factor",
        "forward_axis",
        "up_axis",
        "use_curve_follow",
        "use_curve_radius",
        "use_fixed_location",
    },
    "CLAMP_TO": {"main_axis", "use_cyclic"},
    "LIMIT_LOCATION": {
        "use_min_x",
        "use_min_y",
        "use_min_z",
        "use_max_x",
        "use_max_y",
        "use_max_z",
        "min_x",
        "min_y",
        "min_z",
        "max_x",
        "max_y",
        "max_z",
        "use_transform_limit",
    },
    "LIMIT_ROTATION": {
        "use_limit_x",
        "use_limit_y",
        "use_limit_z",
        "min_x",
        "min_y",
        "min_z",
        "max_x",
        "max_y",
        "max_z",
        "euler_order",
        "use_transform_limit",
    },
    "LIMIT_SCALE": {
        "use_min_x",
        "use_min_y",
        "use_min_z",
        "use_max_x",
        "use_max_y",
        "use_max_z",
        "min_x",
        "min_y",
        "min_z",
        "max_x",
        "max_y",
        "max_z",
        "use_transform_limit",
    },
    "LIMIT_DISTANCE": {"distance", "limit_mode", "use_transform_limit"},
    "STRETCH_TO": {"rest_length", "bulge", "volume", "keep_axis", "head_tail"},
    "SHRINKWRAP": {
        "shrinkwrap_type",
        "wrap_mode",
        "wrap_method",
        "distance",
        "project_limit",
        "use_project_x",
        "use_project_y",
        "use_project_z",
        "use_negative_direction",
        "use_positive_direction",
        "cull_face",
    },
}


_MODIFIER_SETTINGS = {
    "ARRAY": {
        "count",
        "fit_type",
        "relative_offset_displace",
        "constant_offset_displace",
        "use_relative_offset",
        "use_constant_offset",
        "use_object_offset",
        "offset_object",
        "use_merge_vertices",
        "merge_threshold",
    },
    "BEVEL": {
        "width",
        "segments",
        "limit_method",
        "angle_limit",
        "affect",
        "profile",
        "vertex_group",
        "harden_normals",
    },
    "BOOLEAN": {"operation", "solver", "object", "collection", "operand_type", "use_self", "use_hole_tolerant"},
    "BUILD": {"frame_start", "frame_duration", "use_reverse", "use_random_order", "seed"},
    "CAST": {
        "cast_type",
        "factor",
        "radius",
        "size",
        "use_x",
        "use_y",
        "use_z",
        "use_radius_as_size",
        "object",
        "vertex_group",
    },
    "CURVE": {"object", "deform_axis", "vertex_group"},
    "DECIMATE": {
        "decimate_type",
        "ratio",
        "iterations",
        "angle_limit",
        "use_collapse_triangulate",
        "vertex_group",
        "vertex_group_factor",
    },
    "DISPLACE": {
        "strength",
        "mid_level",
        "direction",
        "space",
        "texture",
        "texture_coords",
        "texture_coords_object",
        "uv_layer",
        "vertex_group",
    },
    "LATTICE": {"object", "strength", "vertex_group"},
    "MASK": {"mode", "armature", "vertex_group", "invert_vertex_group", "threshold"},
    "MESH_DEFORM": {"object", "vertex_group", "precision", "use_dynamic_bind"},
    "MIRROR": {
        "use_axis",
        "use_bisect_axis",
        "use_bisect_flip_axis",
        "use_clip",
        "use_mirror_merge",
        "merge_threshold",
        "mirror_object",
    },
    "REMESH": {
        "mode",
        "octree_depth",
        "scale",
        "sharpness",
        "voxel_size",
        "use_smooth_shade",
        "use_remove_disconnected",
        "threshold",
    },
    "SCREW": {
        "axis",
        "angle",
        "steps",
        "render_steps",
        "iterations",
        "screw_offset",
        "object",
        "use_merge_vertices",
        "merge_threshold",
    },
    "SHRINKWRAP": {
        "target",
        "wrap_method",
        "wrap_mode",
        "offset",
        "project_limit",
        "use_project_x",
        "use_project_y",
        "use_project_z",
        "use_negative_direction",
        "use_positive_direction",
        "cull_face",
        "vertex_group",
    },
    "SIMPLE_DEFORM": {
        "deform_method",
        "deform_axis",
        "deform_angle",
        "deform_factor",
        "limits",
        "origin",
        "vertex_group",
        "invert_vertex_group",
    },
    "SKIN": {"use_smooth_shade", "branch_smoothing"},
    "SMOOTH": {"factor", "iterations", "use_x", "use_y", "use_z", "vertex_group"},
    "SOLIDIFY": {
        "thickness",
        "offset",
        "use_even_offset",
        "use_quality_normals",
        "material_offset",
        "material_offset_rim",
        "vertex_group",
    },
    "SUBSURF": {
        "subdivision_type",
        "levels",
        "render_levels",
        "quality",
        "uv_smooth",
        "boundary_smooth",
        "show_only_control_edges",
    },
    "TRIANGULATE": {"quad_method", "ngon_method", "min_vertices", "keep_custom_normals"},
    "WAVE": {
        "height",
        "width",
        "narrowness",
        "speed",
        "damping_time",
        "falloff_radius",
        "start_position_x",
        "start_position_y",
        "use_x",
        "use_y",
        "use_cyclic",
        "use_normal",
        "texture",
        "texture_coords_object",
        "uv_layer",
        "vertex_group",
    },
    "WELD": {"mode", "merge_threshold", "loose_edges", "vertex_group"},
    "WIREFRAME": {
        "thickness",
        "offset",
        "use_even_offset",
        "use_relative_offset",
        "use_boundary",
        "use_replace",
        "material_offset",
        "vertex_group",
    },
    "WEIGHTED_NORMAL": {"weight", "keep_sharp", "thresh", "mode", "vertex_group", "invert_vertex_group"},
    "UV_PROJECT": {"uv_layer", "aspect_x", "aspect_y", "scale_x", "scale_y"},
    "UV_WARP": {
        "object_from",
        "object_to",
        "bone_from",
        "bone_to",
        "uv_layer",
        "center",
        "axis_u",
        "axis_v",
        "vertex_group",
        "invert_vertex_group",
    },
    "VOLUME_TO_MESH": {"object", "grid_name", "threshold", "adaptivity"},
    "MESH_TO_VOLUME": {"object", "density", "voxel_amount", "voxel_size", "interior_band_width", "resolution_mode"},
    "OCEAN": {
        "geometry_mode",
        "resolution",
        "spatial_size",
        "wave_scale",
        "wave_scale_min",
        "wind_velocity",
        "wave_alignment",
        "wave_direction",
        "damping",
        "smallest_wave",
        "choppiness",
        "time",
        "spectrum",
        "fetch_jonswap",
        "sharpen_peak",
        "random_seed",
    },
}


def _resolve_setting(value):
    if not isinstance(value, dict) or set(value) != {"id_type", "name"}:
        return value
    collections = {
        "OBJECT": bpy.data.objects,
        "COLLECTION": bpy.data.collections,
        "TEXTURE": bpy.data.textures,
    }
    collection = collections.get(str(value["id_type"]).upper())
    if collection is None:
        raise ValueError(f"Unsupported ID reference type: {value['id_type']}")
    resolved = collection.get(value["name"])
    if resolved is None:
        raise ValueError(f"{value['id_type']} datablock not found: {value['name']}")
    return resolved


def _apply_allowlisted_settings(owner, settings, allowed):
    unknown = sorted(set(settings) - allowed)
    if unknown:
        raise ValueError(f"Unsupported settings for {owner.bl_rna.identifier}: {unknown}")
    prepared = []
    for name, value in settings.items():
        prop = owner.bl_rna.properties.get(name)
        if prop is None or prop.is_readonly:
            raise ValueError(f"Property '{name}' is not writable on {owner.bl_rna.identifier}")
        prepared.append((name, _resolve_setting(value)))
    previous = [(name, getattr(owner, name)) for name, _value in prepared]
    try:
        for name, value in prepared:
            setattr(owner, name, value)
    except Exception:
        for name, value in previous:
            try:
                setattr(owner, name, value)
            except Exception:
                pass
        raise


def _restore_properties(owner, values):
    for name, value in values.items():
        try:
            setattr(owner, name, value)
        except Exception:
            pass


class SceneHandlersMixin:
    """Provide native scene composition handlers."""

    def create_geometry_object(
        self, name, geometry, collection_name=None, location=(0, 0, 0), rotation=(0, 0, 0), scale=(1, 1, 1)
    ):
        name = _required_name(name, "name")
        if bpy.data.objects.get(name) is not None:
            raise ValueError(f"Object already exists: {name}")
        kind = geometry.get("kind")
        builder = _GEOMETRY_BUILDERS.get(kind)
        if builder is None:
            raise ValueError(f"Unsupported geometry kind: {kind}")
        if any(value == 0 for value in scale):
            raise ValueError("scale components must be non-zero")
        collection_name_by_kind = {
            "MESH": "meshes",
            "CURVE": "curves",
            "SURFACE": "curves",
            "TEXT": "curves",
            "META": "metaballs",
            "LATTICE": "lattices",
            "POINTCLOUD": "pointclouds",
            "CURVES": "hair_curves",
            "GREASEPENCIL": "grease_pencils",
            "VOLUME": "volumes",
        }
        data_collection = getattr(bpy.data, collection_name_by_kind[kind])
        existing_data = {item.as_pointer() for item in data_collection}
        data = None
        obj = None
        try:
            data = builder(name, geometry)
            obj = bpy.data.objects.new(name, data)
            _scene_collection(collection_name).objects.link(obj)
            obj.location = location
            obj.rotation_euler = rotation
            obj.scale = scale
        except Exception:
            if (obj := bpy.data.objects.get(name)) is not None:
                bpy.data.objects.remove(obj, do_unlink=True)
            for created_data in [item for item in data_collection if item.as_pointer() not in existing_data]:
                data_collection.remove(created_data)
            raise
        counts = _geometry_counts(data, kind)
        return {
            "name": obj.name,
            "type": obj.type,
            "data_name": data.name,
            "collection": next(iter(obj.users_collection)).name,
            "transform": _transform_snapshot(obj),
            "geometry": {"kind": kind, "counts": counts, "attributes": _attribute_schema(data)},
            "changed_objects": [obj.name],
            "changed_resources": [data.name],
        }

    def set_object_transform(self, object_name, patch, space="WORLD"):
        obj = _object(object_name)
        space = str(space).upper()
        if space not in {"LOCAL", "WORLD"}:
            raise ValueError("space must be LOCAL or WORLD")
        if space == "WORLD":
            # Both the read below and `obj.matrix_world = ...` resolve against the parent's
            # evaluated matrix, so an unflushed parent would place this object relative to
            # wherever the parent used to be.
            _update_view_layer()
        if "matrix" in patch:
            matrix = mathutils.Matrix(patch["matrix"])
            if space == "WORLD":
                obj.matrix_world = matrix
            else:
                obj.matrix_basis = matrix
        else:
            matrix = obj.matrix_world.copy() if space == "WORLD" else obj.matrix_basis.copy()
            location, rotation, scale = matrix.decompose()
            location = mathutils.Vector(patch.get("location", location))
            scale = mathutils.Vector(patch.get("scale", scale))
            if "rotation_euler" in patch:
                rotation = mathutils.Euler(patch["rotation_euler"], "XYZ").to_quaternion()
            elif "rotation_quaternion" in patch:
                rotation = mathutils.Quaternion(patch["rotation_quaternion"])
                if rotation.magnitude == 0:
                    raise ValueError("rotation_quaternion must be non-zero")
                rotation.normalize()
            elif "rotation_axis_angle" in patch:
                angle, x, y, z = patch["rotation_axis_angle"]
                axis = mathutils.Vector((x, y, z))
                if axis.length_squared == 0:
                    raise ValueError("rotation_axis_angle axis must be non-zero")
                rotation = mathutils.Quaternion(axis.normalized(), angle)
            result = mathutils.Matrix.LocRotScale(location, rotation, scale)
            if space == "WORLD":
                obj.matrix_world = result
            else:
                obj.matrix_basis = result
        return {"name": obj.name, **_transform_snapshot(obj)}

    def set_scene_frame(self, frame, subframe=0.0, scene_name=None):
        """Move one scene's playhead so the inspection tools report a different frame."""
        scene = (
            bpy.context.scene if scene_name is None else bpy.data.scenes.get(_required_name(scene_name, "scene_name"))
        )
        if scene is None:
            raise ValueError(f"Scene not found: {scene_name}")
        frame = bounded_int("frame", frame, MIN_FRAME, MAX_FRAME)
        subframe = float(subframe)
        if not 0.0 <= subframe < 1.0:
            raise ValueError("subframe must be in [0.0, 1.0)")
        scene.frame_set(frame, subframe=subframe)
        fps_base = scene.render.fps_base
        return {
            "scene": scene.name,
            "frame": scene.frame_current,
            "subframe": float(scene.frame_subframe),
            "frame_start": scene.frame_start,
            "frame_end": scene.frame_end,
            "fps": scene.render.fps / fps_base if fps_base else float(scene.render.fps),
        }

    def duplicate_or_instance_objects(
        self, source_object_name, names, transforms=None, mode="LINKED_DATA", collection_name=None
    ):
        source = _object(source_object_name)
        if len(names) != len(set(names)):
            raise ValueError("names must be unique")
        collisions = [name for name in names if bpy.data.objects.get(name) is not None]
        if collisions:
            raise ValueError(f"Objects already exist: {collisions}")
        collection = _scene_collection(collection_name)
        records = []
        if mode == "COLLECTION_INSTANCE" and not source.users_collection:
            raise ValueError(f"Source object '{source.name}' is not linked to a collection")
        if not transforms or not all(transforms):
            # A duplicate with no transform of its own inherits `source.matrix_world`, which
            # still reads pre-edit until the graph catches up with whatever last moved it.
            _update_view_layer()
        created = []
        try:
            for index, name in enumerate(names):
                if mode == "COLLECTION_INSTANCE":
                    duplicate = bpy.data.objects.new(name, None)
                    duplicate.instance_type = "COLLECTION"
                    duplicate.instance_collection = source.users_collection[0]
                else:
                    duplicate = source.copy()
                    duplicate.name = name
                    if mode == "COPY" and source.data is not None:
                        duplicate.data = source.data.copy()
                created.append(duplicate)
                collection.objects.link(duplicate)
                transform = transforms[index] if transforms else None
                if transform:
                    if any(value == 0 for value in transform["scale"]):
                        raise ValueError(f"Scale components must be non-zero for '{name}'")
                    duplicate.location = transform["location"]
                    duplicate.rotation_euler = transform["rotation"]
                    duplicate.scale = transform["scale"]
                else:
                    duplicate.matrix_world = source.matrix_world.copy()
                records.append({"name": duplicate.name, "data": getattr(duplicate.data, "name", None)})
        except Exception:
            for duplicate in reversed(created):
                bpy.data.objects.remove(duplicate, do_unlink=True)
            raise
        return {"source": source.name, "mode": mode, "objects": records, "changed_objects": names}

    def manage_scene_collections(
        self,
        action,
        collection_name,
        object_names=None,
        parent_collection_name=None,
        hide_viewport=None,
        hide_render=None,
        confirm_remove=False,
    ):
        action = str(action).upper()
        collection = bpy.data.collections.get(collection_name)
        if action == "CREATE":
            if collection is not None:
                raise ValueError(f"Collection already exists: {collection_name}")
            parent = (
                bpy.data.collections.get(parent_collection_name)
                if parent_collection_name
                else bpy.context.scene.collection
            )
            if parent is None:
                raise ValueError(f"Parent collection not found: {parent_collection_name}")
            collection = bpy.data.collections.new(_required_name(collection_name, "collection_name"))
            parent.children.link(collection)
        elif collection is None:
            raise ValueError(f"Collection not found: {collection_name}")
        objects = [_object(name) for name in object_names or []]
        if action == "LINK_OBJECTS":
            for obj in objects:
                if collection.objects.get(obj.name) is None:
                    collection.objects.link(obj)
        elif action == "UNLINK_OBJECTS":
            invalid = [
                obj.name
                for obj in objects
                if collection.objects.get(obj.name) is not None and len(obj.users_collection) == 1
            ]
            if invalid:
                raise ValueError(f"Cannot unlink objects from their only collection: {invalid}")
            for obj in objects:
                if collection.objects.get(obj.name) is not None:
                    collection.objects.unlink(obj)
        elif action == "SET_VISIBILITY":
            if hide_viewport is None and hide_render is None:
                raise ValueError("SET_VISIBILITY requires hide_viewport or hide_render")
            if hide_viewport is not None:
                collection.hide_viewport = hide_viewport
            if hide_render is not None:
                collection.hide_render = hide_render
        elif action == "REMOVE":
            if not confirm_remove:
                raise ValueError("confirm_remove=True is required")
            if collection.objects or collection.children:
                raise ValueError("Collection must be empty before removal")
            bpy.data.collections.remove(collection)
            return {"removed": collection_name, "changed_resources": [collection_name]}
        elif action != "CREATE":
            raise ValueError(f"Unsupported collection action: {action}")
        # The collection is what the caller acts on next; its members, however many, are counted.
        return {
            "name": collection.name,
            "objects": _counted_objects(list(collection.objects)),
            "hide_viewport": collection.hide_viewport,
            "hide_render": collection.hide_render,
            "changed_objects": [],
            "changed_resources": [collection.name],
        }

    def manage_object_hierarchy(self, assignments, preserve_world_transform=True):
        children = [_object(record["child_object_name"]) for record in assignments]
        if len({child.name for child in children}) != len(children):
            raise ValueError("Each child object may appear only once")
        planned = {}
        for record, child in zip(assignments, children, strict=True):
            parent_name = record.get("parent_object_name")
            parent = _object(parent_name) if parent_name else None
            if parent is child:
                raise ValueError(f"Object '{child.name}' cannot parent itself")
            bone_name = record.get("parent_bone_name")
            if bone_name and parent is None:
                raise ValueError(f"Bone parenting requires a parent object for '{child.name}'")
            if bone_name and parent.type != "ARMATURE":
                raise ValueError(f"Bone parenting requires an armature parent for '{child.name}'")
            if bone_name and parent.data.bones.get(bone_name) is None:
                raise ValueError(f"Bone not found: {bone_name}")
            planned[child] = parent
        for child, parent in planned.items():
            ancestor = parent
            seen = set()
            while ancestor is not None:
                if ancestor is child:
                    raise ValueError(f"Parenting '{child.name}' to '{parent.name}' would create a cycle")
                if ancestor in seen:
                    raise ValueError("Existing or requested parent graph contains a cycle")
                seen.add(ancestor)
                ancestor = planned.get(ancestor, ancestor.parent)
        for record, child in zip(assignments, children, strict=True):
            if preserve_world_transform:
                # Per assignment, not once: the world matrix read here and the one written
                # below both resolve against evaluated parents, and an earlier assignment in
                # this same loop may have moved one of those parents.
                _update_view_layer()
            world = child.matrix_world.copy()
            parent_name = record.get("parent_object_name")
            child.parent = _object(parent_name) if parent_name else None
            child.parent_type = "BONE" if record.get("parent_bone_name") else "OBJECT"
            child.parent_bone = record.get("parent_bone_name") or ""
            if preserve_world_transform:
                child.matrix_world = world
        return {"assignments": assignments, "changed_objects": [obj.name for obj in children]}

    def manage_object_constraints(self, object_name, action, constraint, position=None):
        obj = _object(object_name)
        action = str(action).upper()
        name = constraint["name"]
        existing = obj.constraints.get(name)
        created = False
        if action == "ADD":
            if existing is not None:
                raise ValueError(f"Constraint already exists: {name}")
            item = obj.constraints.new(constraint["type"])
            item.name = name
            created = True
        else:
            if existing is None:
                raise ValueError(f"Constraint not found: {name}")
            item = existing
            if item.type != constraint["type"]:
                raise ValueError(f"Constraint '{name}' has type {item.type}, not {constraint['type']}")
        if action in {"ADD", "PATCH"}:
            settings = constraint.get("settings", {})
            previous = {
                "influence": item.influence,
                **{key: getattr(item, key) for key in settings if hasattr(item, key)},
            }
            if hasattr(item, "target"):
                previous["target"] = item.target
            if hasattr(item, "subtarget"):
                previous["subtarget"] = item.subtarget
            try:
                target_name = constraint.get("target_object_name")
                if target_name is not None:
                    if not hasattr(item, "target"):
                        raise ValueError(f"Constraint type {item.type} does not accept a target")
                    item.target = _object(target_name)
                if constraint.get("subtarget") is not None and hasattr(item, "subtarget"):
                    item.subtarget = constraint["subtarget"]
                item.influence = constraint.get("influence", 1.0)
                _apply_allowlisted_settings(item, settings, _CONSTRAINT_SETTINGS[item.type])
            except Exception:
                if created:
                    obj.constraints.remove(item)
                else:
                    _restore_properties(item, previous)
                raise
        elif action == "REMOVE":
            obj.constraints.remove(item)
            return {"object": obj.name, "removed": name}
        elif action == "MOVE":
            if position is None or position >= len(obj.constraints):
                raise ValueError("MOVE requires a valid position")
            obj.constraints.move(list(obj.constraints).index(item), position)
        else:
            raise ValueError(f"Unsupported constraint action: {action}")
        return {
            "object": obj.name,
            "constraint": item.name,
            "type": item.type,
            "stack_index": list(obj.constraints).index(item),
        }

    def manage_modifiers(self, object_name, action, modifier, position=None, confirm_destructive=False):
        obj = _object(object_name)
        action = str(action).upper()
        name = modifier["name"]
        existing = obj.modifiers.get(name)
        created = False
        if action == "ADD":
            if existing is not None:
                raise ValueError(f"Modifier already exists: {name}")
            item = obj.modifiers.new(name=name, type=modifier["type"])
            created = True
        else:
            if existing is None:
                raise ValueError(f"Modifier not found: {name}")
            item = existing
            if item.type != modifier["type"]:
                raise ValueError(f"Modifier '{name}' has type {item.type}, not {modifier['type']}")
        if action in {"ADD", "PATCH"}:
            settings = modifier.get("settings", {})
            previous = {key: getattr(item, key) for key in settings if hasattr(item, key)}
            try:
                _apply_allowlisted_settings(item, settings, _MODIFIER_SETTINGS[item.type])
            except Exception:
                if created:
                    obj.modifiers.remove(item)
                else:
                    _restore_properties(item, previous)
                raise
        elif action == "MOVE":
            if position is None or position >= len(obj.modifiers):
                raise ValueError("MOVE requires a valid position")
            obj.modifiers.move(list(obj.modifiers).index(item), position)
        elif action == "REMOVE":
            if not confirm_destructive:
                raise ValueError("confirm_destructive=True is required")
            obj.modifiers.remove(item)
            return {"name": obj.name, "removed_modifier": name}
        elif action == "APPLY":
            if not confirm_destructive:
                raise ValueError("confirm_destructive=True is required")
            apply_modifier(obj, item)
            return {"name": obj.name, "applied_modifier": name, "base_counts": _base_counts(obj)}
        else:
            raise ValueError(f"Unsupported modifier action: {action}")
        return {"name": obj.name, **modifier_result(obj, item, False)}

    def set_object_visibility(self, object_name, hide_render=None, hide_viewport=None, hide_select=None):
        """
        Set one or more visibility flags on an explicit object, in place.

        Unlike `remove_scene_objects`, this never removes an ID: a library-override object
        hidden this way stays present, so Blender's liboverride resync - which recreates any
        override object still present in the linked source but missing locally - has nothing to
        undo. Prefer this over deletion whenever the intent is "make this not render", not "this
        object should no longer exist in the file".

        Args:
            object_name: The object to change.
            hide_render: Exclude the object from rendered output, or leave unchanged if None.
            hide_viewport: Exclude the object from the 3D viewport, or leave unchanged if None.
            hide_select: Make the object unselectable in the viewport, or leave unchanged if None.

        Returns:
            The object's name and its three visibility flags after the change.

        Raises:
            ValueError: If none of the three flags were provided, or the object is not found.

        """
        if hide_render is None and hide_viewport is None and hide_select is None:
            raise ValueError("Provide at least one of hide_render, hide_viewport, hide_select")
        obj = _object(object_name)
        if hide_render is not None:
            obj.hide_render = bool(hide_render)
        if hide_viewport is not None:
            obj.hide_viewport = bool(hide_viewport)
        if hide_select is not None:
            obj.hide_select = bool(hide_select)
        return {
            "name": obj.name,
            "hide_render": obj.hide_render,
            "hide_viewport": obj.hide_viewport,
            "hide_select": obj.hide_select,
            "changed_objects": [obj.name],
        }

    def remove_scene_objects(
        self, object_names=None, managed_rig=None, confirm_remove=False, confirm_override_removal=False
    ):
        if not confirm_remove:
            raise ValueError("confirm_remove=True is required")
        if (object_names is None) == (managed_rig is None):
            raise ValueError("Provide exactly one of object_names or managed_rig")
        selector = None
        if managed_rig is not None:
            property_names = {
                "CAMERA": "mcp_camera_rig_id",
                "RIGID_BODY": "blendermcp_rigid_body_rig_id",
            }
            system = str(managed_rig.get("system", "")).upper()
            property_name = property_names.get(system)
            rig_id = managed_rig.get("rig_id")
            if property_name is None or not isinstance(rig_id, str) or not rig_id:
                raise ValueError("managed_rig requires a supported system and non-empty rig_id")
            object_names = sorted(obj.name for obj in bpy.data.objects if obj.get(property_name) == rig_id)
            if not object_names:
                raise ValueError(f"No {system} objects found for managed rig ID: {rig_id}")
            selector = {"system": system, "rig_id": rig_id, "ownership_property": property_name}
        object_names = list(object_names or [])
        if len(object_names) != len(set(object_names)):
            raise ValueError("object_names must be unique")
        objects = [_object(name) for name in object_names]
        # A library-override object deleted here is indistinguishable, to Blender's own
        # liboverride resync, from one nobody ever touched: resync recreates any override object
        # still present in the linked source but missing locally, the next time this file (or any
        # file linking the same library) opens - silently undoing the deletion. Refuse by default,
        # the same way every other destructive path in this handler does, rather than let that
        # surface as an unexplained resurrection on the next load.
        override_names = sorted(obj.name for obj in objects if obj.override_library is not None)
        if override_names and not confirm_override_removal:
            raise ValueError(
                "Deleting a library-override object does not survive Blender's own liboverride "
                "resync and will be recreated the next time this file is opened. Overrides: "
                f"{_name_sample(override_names)}. Pass confirm_override_removal=True to delete them anyway, or "
                "call set_object_visibility(hide_render=True, hide_viewport=True) instead, which "
                "changes a property rather than removing the ID and survives the resync."
            )
        dependencies = {
            obj.name: {
                "children": [child.name for child in obj.children],
                "collections": [collection.name for collection in obj.users_collection],
                "data": getattr(obj.data, "name", None),
                "data_users_before": getattr(obj.data, "users", None),
                "materials": [
                    {"name": slot.material.name, "users_before": slot.material.users}
                    for slot in obj.material_slots
                    if slot.material
                ],
            }
            for obj in objects
        }
        # Counted while the objects still exist: a removed object's type and name read nothing.
        removed = _counted_objects(objects)
        sampled = [obj.name for obj in objects[:MAX_LISTED_NAMES]]
        for obj in objects:
            bpy.data.objects.remove(obj, do_unlink=True)
        # Keyed by datablock: a rig's members sharing one material retain it once, not once each.
        retained = {}
        for dependency in dependencies.values():
            data_name = dependency["data"]
            if data_name and dependency["data_users_before"] and dependency["data_users_before"] > 1:
                retained["OBJECT_DATA", data_name] = {
                    "kind": "OBJECT_DATA",
                    "name": data_name,
                    "reason": "shared users remain",
                }
            for material in dependency["materials"]:
                if material["users_before"] > 1:
                    retained["MATERIAL", material["name"]] = {
                        "kind": "MATERIAL",
                        "name": material["name"],
                        "reason": "shared users remain",
                    }
        warnings = []
        if override_names:
            warnings.append(
                "Deleted library-override object(s) will be recreated by Blender's liboverride "
                f"resync the next time this file is opened: {_name_sample(override_names)}. Use "
                "set_object_visibility to hide them durably instead of deleting them."
            )
        # Nothing removed is left to act on, so no name is a next target: `removed` counts them.
        return {
            "removed": removed,
            "selector": selector,
            "dependencies": {name: dependencies[name] for name in sampled},
            "retained_shared_datablocks": list(retained.values()),
            "purged_datablocks": [],
            "changed_objects": [],
            "warnings": warnings,
        }

    def reset_scene(self, confirm_reset=False, scene_name=None, purge_orphaned_data=True):
        if not confirm_reset:
            raise ValueError("confirm_reset=True is required")
        scene = bpy.context.scene if scene_name is None else bpy.data.scenes.get(scene_name)
        if scene is None:
            raise ValueError(f"Scene not found: {scene_name}")

        master = scene.collection
        # Counted before anything is unlinked or purged: a purged object reads nothing.
        unlinked_objects = _counted_objects(sorted(scene.objects, key=lambda obj: obj.name))
        for obj in list(master.objects):
            master.objects.unlink(obj)

        unlinked_collections = counted_page(
            sorted(master.children, key=lambda child: child.name),
            type_of=lambda _child: "COLLECTION",
            name_of=lambda child: child.name,
        )
        for child in list(master.children):
            master.children.unlink(child)

        purged_datablock_count = 0
        if purge_orphaned_data:
            purged_datablock_count = bpy.data.orphans_purge(do_local_ids=True, do_linked_ids=False, do_recursive=True)

        # The emptied scene is the next target; what left it is counted, not named in full.
        return {
            "scene": scene.name,
            "unlinked_objects": unlinked_objects,
            "unlinked_collections": unlinked_collections,
            "purged_datablock_count": purged_datablock_count,
            "changed_objects": [],
            "changed_resources": [scene.name],
        }

    def validate_scene(self, scene_name, scope=None, max_findings=300, offset=0):
        if not 1 <= int(max_findings) <= 1000:
            raise ValueError("max_findings must be in [1, 1000]")
        if not 0 <= int(offset) <= 9999:
            raise ValueError("offset must be in [0, 9999]")
        scene = bpy.data.scenes.get(_required_name(scene_name, "scene_name"))
        if scene is None:
            raise ValueError(f"Scene not found: {scene_name}")
        if scope is not None:
            if not scope:
                raise ValueError("scope must contain at least one domain when provided")
            unknown = sorted(set(scope) - set(_VALIDATE_SCENE_DOMAINS))
            if unknown:
                raise ValueError(f"Unknown validation scope domain(s): {unknown}")
            domains = [domain for domain in _VALIDATE_SCENE_DOMAINS if domain in scope]
        else:
            domains = list(_VALIDATE_SCENE_DOMAINS)

        # A sub-validator asked for `max_findings` alone cannot fill a page that starts past it,
        # so every domain is asked for enough findings to reach the end of this page, within the
        # [1, 1000] its own parameter accepts.
        domain_limit = min(int(offset) + int(max_findings), 1000)
        findings, domain_summaries = _collected_domain_findings(self, scene, domains, domain_limit)

        severity_order = {"ERROR": 0, "WARNING": 1, "INFO": 2}
        findings.sort(
            key=lambda item: (
                severity_order.get(item["severity"], 3),
                item["domain"],
                str(item["code"]),
                str(item["subject"]),
            )
        )
        # Two different facts, so two keys: `truncated` is strictly "more findings follow this
        # page", which `next_offset` resumes, while `domains_truncated` is "some domain stopped
        # counting at its own cap", which no offset of this call can reach and which
        # `domain_summaries` already names per domain.
        domains_truncated = any(summary["truncated"] for summary in domain_summaries.values())
        start, end, truncated, next_offset = paginate(len(findings), int(offset), int(max_findings), 1000)
        page = findings[start:end]

        return {
            "scene": scene.name,
            "domains_checked": domains,
            "domain_summaries": domain_summaries,
            "findings": page,
            "summary": dict(Counter(item["severity"] for item in findings)),
            "total_findings": len(findings),
            "offset": start,
            "limit": int(max_findings),
            "returned_count": len(page),
            "truncated": truncated,
            "next_offset": next_offset,
            "domains_truncated": domains_truncated,
            "ready": not any(item["severity"] == "ERROR" for item in findings),
            "limitations": [
                "Aggregates validate_pbr_asset, validate_lighting_setup, validate_cloth_setup, "
                "validate_liquid_setup, and validate_camera_rig plus scene-level checks (camera/light presence "
                "when those domains are excluded from scope, frame range consistency, unapplied scale, "
                "degenerate geometry, dirty cloth/rigid-body caches); it is a structural preflight, not a "
                "rendered-frame review.",
                "The persistence domain reads bpy.data file-wide, not just this scene, and reports local "
                "datablocks only.",
            ],
        }


def _base_counts(obj):
    if obj.type != "MESH":
        return None
    return {"vertices": len(obj.data.vertices), "edges": len(obj.data.edges), "polygons": len(obj.data.polygons)}


def _counted_objects(objects):
    """
    Count objects by type beside a sample of their names.

    Args:
        objects: Every object the reply accounts for, in the order the sample should follow.

    Returns:
        dict: `helpers.counted_page` over the objects, typed by `Object.type`.

    """
    return counted_page(objects, type_of=lambda obj: obj.type, name_of=lambda obj: obj.name)


def _name_sample(names):
    """
    Name at most `MAX_LISTED_NAMES` of `names` in a message, saying how many more there are.

    Args:
        names: Every name the message is about.

    Returns:
        str: The first names, comma-separated, and `(+N more)` when some were left out.

    """
    shown = ", ".join(names[:MAX_LISTED_NAMES])
    extra = len(names) - MAX_LISTED_NAMES
    return f"{shown} (+{extra} more)" if extra > 0 else shown
