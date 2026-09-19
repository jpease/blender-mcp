"""Compose the existing liquid handlers into one transactional "fill this container" setup."""

import math
import uuid

import bpy
import mathutils

from ..scene_physics import _scene_fps
from ._geometry import _RIM_AXES, _cube_geometry
from .inspection_and_setup import (
    _ensure_collection,
    _get_domain,
    _get_object,
    _get_scene,
    _link_object,
    _register_owned_objects,
    _tag_liquid_object,
    _world_bounds,
)

# The shot's own identity key. ``create_liquid_proxy_rig`` already owns
# "blendermcp_liquid_simulation_id" for a single proxy rig, so a shot that builds several rigs must
# not overwrite it; the shot id is stamped alongside it on every object the shot touches.
SHOT_ID_PROPERTY = "blendermcp_liquid_shot_id"
# Which container a validation volume belongs to, so validate_liquid_result can pair them up without
# re-deriving geometry.
VOLUME_CONTAINER_PROPERTY = "blendermcp_liquid_volume_container"
VALIDATION_COLLECTION_SUFFIX = "Validation Volumes"

_FLOW_BEHAVIORS = {"GEOMETRY", "INFLOW", "OUTFLOW"}
_COLLISION_PROXIES = {"NONE", "HOLLOW_CONTAINER"}
_MAX_CONTAINERS = 16
_MAX_SOURCES = 16


def _require_mapping(value, label):
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _require_positive(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a positive finite number")
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{label} must be a positive finite number")
    return float(value)


def _resolve_containers(scene, containers):
    """Validate the container records and resolve each to a mesh object linked to the scene."""
    if not containers or len(containers) > _MAX_CONTAINERS:
        raise ValueError(f"containers must contain 1-{_MAX_CONTAINERS} records")
    resolved = []
    for index, record in enumerate(containers):
        _require_mapping(record, f"containers[{index}]")
        obj = _get_object(record.get("object_name"), {"MESH"})
        if obj.name not in scene.objects:
            raise ValueError(f"Container '{obj.name}' is not linked to scene '{scene.name}'")
        proxy = record.get("collision_proxy", "NONE")
        if proxy not in _COLLISION_PROXIES:
            raise ValueError(f"containers[{index}].collision_proxy must be one of {sorted(_COLLISION_PROXIES)}")
        rim_axis = record.get("rim_axis", "Z")
        if rim_axis not in _RIM_AXES:
            raise ValueError(f"containers[{index}].rim_axis must be one of {sorted(_RIM_AXES)}")
        wall_thickness = _require_positive(record.get("wall_thickness", 0.05), f"containers[{index}].wall_thickness")
        bottom_thickness = record.get("bottom_thickness")
        if bottom_thickness is not None:
            bottom_thickness = _require_positive(bottom_thickness, f"containers[{index}].bottom_thickness")
        resolved.append(
            {
                "object": obj,
                "collision_proxy": proxy,
                "rim_axis": rim_axis,
                "wall_thickness": wall_thickness,
                "bottom_thickness": bottom_thickness,
                "effector_type": record.get("effector_type", "COLLISION"),
                "effector_settings": record.get("effector_settings"),
                "proxy_object_name": record.get("proxy_object_name") or f"{obj.name} Collision Proxy",
            }
        )
    return resolved


def _resolve_sources(scene, sources, fps, frame_start):
    """Validate the source records, resolving enabled_seconds into the frames animate_liquid_flow keys."""
    if not sources or len(sources) > _MAX_SOURCES:
        raise ValueError(f"sources must contain 1-{_MAX_SOURCES} records")
    resolved = []
    for index, record in enumerate(sources):
        _require_mapping(record, f"sources[{index}]")
        obj = _get_object(record.get("object_name"), {"MESH"})
        if obj.name not in scene.objects:
            raise ValueError(f"Source '{obj.name}' is not linked to scene '{scene.name}'")
        behavior = record.get("behavior", "INFLOW")
        if behavior not in _FLOW_BEHAVIORS:
            raise ValueError(f"sources[{index}].behavior must be one of {sorted(_FLOW_BEHAVIORS)}")
        window = _resolve_enabled_window(record.get("enabled_seconds"), behavior, fps, frame_start, index)
        resolved.append(
            {
                "object": obj,
                "behavior": behavior,
                "enabled_seconds": record.get("enabled_seconds"),
                "enabled_frames": window,
                "flow_settings": record.get("flow_settings"),
            }
        )
    return resolved


def _resolve_enabled_window(enabled_seconds, behavior, fps, frame_start, index):
    """
    Convert an [on, off] seconds window into the (on_frame, off_frame) pair keyed on use_inflow.

    Seconds are measured from the scene's frame_start at the scene's current fps, which is the same
    conversion get_scene_physics_info reports, so a caller can state intent in shot time.
    """
    if enabled_seconds is None:
        return None
    if behavior == "GEOMETRY":
        raise ValueError(
            f"sources[{index}].enabled_seconds requires behavior INFLOW or OUTFLOW; Blender's "
            "Use Flow toggle has no effect on a GEOMETRY flow"
        )
    if isinstance(enabled_seconds, (str, bytes)) or not isinstance(enabled_seconds, (list, tuple)):
        raise ValueError(f"sources[{index}].enabled_seconds must be an [on_seconds, off_seconds] pair")
    if len(enabled_seconds) != 2:
        raise ValueError(f"sources[{index}].enabled_seconds must be an [on_seconds, off_seconds] pair")
    values = []
    for seconds in enabled_seconds:
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds):
            raise ValueError(f"sources[{index}].enabled_seconds entries must be finite numbers")
        values.append(float(seconds))
    if values[1] <= values[0]:
        raise ValueError(f"sources[{index}].enabled_seconds must be increasing")
    on_frame = round(frame_start + values[0] * fps)
    off_frame = round(frame_start + values[1] * fps)
    if off_frame <= on_frame:
        raise ValueError(
            f"sources[{index}].enabled_seconds resolves to a single frame at {fps:g} fps; "
            "widen the window or raise the scene frame rate"
        )
    return (on_frame, off_frame)


def _interior_box(bounds, rim_axis, wall_thickness, bottom_thickness):
    """
    Return the (center, dimensions) of the usable interior of an axis-aligned container bound box.

    This is deliberately a box approximation: it is a measurement reference for
    validate_liquid_result's fill fraction, never simulation geometry, and a box keeps the reported
    fraction interpretable for the tapered containers this is usually pointed at.
    """
    axis_index, sign = _RIM_AXES[rim_axis]
    minimum = list(bounds["minimum"])
    maximum = list(bounds["maximum"])
    for axis in range(3):
        if axis == axis_index:
            continue
        minimum[axis] += wall_thickness
        maximum[axis] -= wall_thickness
    if sign > 0:
        minimum[axis_index] += bottom_thickness
    else:
        maximum[axis_index] -= bottom_thickness
    dimensions = [maximum[axis] - minimum[axis] for axis in range(3)]
    if any(value <= 0 for value in dimensions):
        raise ValueError(
            "Container interior collapses at the requested wall/bottom thickness; "
            "reduce wall_thickness or use a larger container"
        )
    center = [(minimum[axis] + maximum[axis]) * 0.5 for axis in range(3)]
    return center, dimensions


def _spill_box(bounds, rim_axis, catch_depth, margin):
    """Return the (center, dimensions) of the catch region around and below a container's rim."""
    axis_index, sign = _RIM_AXES[rim_axis]
    minimum = list(bounds["minimum"])
    maximum = list(bounds["maximum"])
    for axis in range(3):
        if axis == axis_index:
            continue
        minimum[axis] -= margin
        maximum[axis] += margin
    if sign > 0:
        minimum[axis_index] -= catch_depth
    else:
        maximum[axis_index] += catch_depth
    dimensions = [maximum[axis] - minimum[axis] for axis in range(3)]
    center = [(minimum[axis] + maximum[axis]) * 0.5 for axis in range(3)]
    return center, dimensions


def _spill_margin(bounds, rim_axis):
    """Default the catch region's lateral margin to the container's widest non-rim extent."""
    axis_index, _sign = _RIM_AXES[rim_axis]
    lateral = [value for axis, value in enumerate(bounds["dimensions"]) if axis != axis_index]
    return max(max(lateral), 1e-4)


def _box_object(name, center, dimensions):
    """Build an axis-aligned world-space box as a standalone, still-unlinked mesh object."""
    vertices, faces = _cube_geometry(dimensions)
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(vertices, [], faces)
    mesh.validate()
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    obj.matrix_world = mathutils.Matrix.Translation(tuple(center))
    # Validation volumes are measurement references only: they carry no fluid modifier, so Mantaflow
    # ignores them whatever collection they sit in, and they must never reach a render.
    obj.hide_render = True
    obj.display_type = "WIRE"
    return obj


def _source_summaries(sources_result):
    """Replace the live object reference in each source record with its name for the response."""
    return [
        {key: (value.name if key == "object" else value) for key, value in entry.items()} for entry in sources_result
    ]


def _shot_plan(scene, fps, resolved_containers, resolved_sources, options):
    """Describe every step the shot will take, in order, without touching the scene."""
    steps = []
    for entry in resolved_containers:
        if entry["collision_proxy"] == "HOLLOW_CONTAINER":
            steps.append(
                {
                    "step": "create_liquid_proxy_rig",
                    "object": entry["object"].name,
                    "proxy": entry["proxy_object_name"],
                    "geometry": "HOLLOW_CONTAINER",
                    "wall_thickness": entry["wall_thickness"],
                    "bottom_thickness": entry["bottom_thickness"],
                    "rim_axis": entry["rim_axis"],
                }
            )
        else:
            steps.append(
                {
                    "step": "add_liquid_effector",
                    "object": entry["object"].name,
                    "effector_type": entry["effector_type"],
                }
            )
    for entry in resolved_sources:
        steps.append({"step": "add_liquid_flow", "object": entry["object"].name, "behavior": entry["behavior"]})
        if entry["enabled_frames"] is not None:
            on_frame, off_frame = entry["enabled_frames"]
            steps.append(
                {
                    "step": "animate_liquid_flow",
                    "object": entry["object"].name,
                    "property": "use_inflow",
                    "keyframes": [
                        {"frame": on_frame, "use_inflow": True},
                        {"frame": off_frame, "use_inflow": False},
                    ],
                }
            )
    steps.append({"step": "fit_liquid_domain", "sources": [entry["object"].name for entry in resolved_sources]})
    steps.append({"step": "apply_liquid_quality_profile", "profile": options["quality"]})
    if options["create_validation_volumes"]:
        for entry in resolved_containers:
            steps.append(
                {"step": "create_validation_volume", "role": "CONTAINER_VOLUME", "container": entry["object"].name}
            )
            if options["spill_catch_depth"] is not None:
                steps.append(
                    {"step": "create_validation_volume", "role": "SPILL_VOLUME", "container": entry["object"].name}
                )
    steps.append({"step": "validate_liquid_setup", "domain": options["domain_object_name"] or "<new domain>"})
    return {
        "scene": scene.name,
        "fps": fps,
        "frame_start": scene.frame_start,
        "quality": options["quality"],
        "cache": {
            "directory": options["cache_directory"],
            "type": options["cache_type"],
            "frame_start": options["cache_frame_start"],
            "frame_end": options["cache_frame_end"],
        },
        "containers": [
            {
                "object": entry["object"].name,
                "collision_proxy": entry["collision_proxy"],
                "rim_axis": entry["rim_axis"],
                "wall_thickness": entry["wall_thickness"],
                "bottom_thickness": entry["bottom_thickness"],
                "effector_type": entry["effector_type"],
            }
            for entry in resolved_containers
        ],
        "sources": [
            {
                "object": entry["object"].name,
                "behavior": entry["behavior"],
                "enabled_seconds": list(entry["enabled_seconds"]) if entry["enabled_seconds"] else None,
                "enabled_frames": list(entry["enabled_frames"]) if entry["enabled_frames"] else None,
            }
            for entry in resolved_sources
        ],
        "steps": steps,
    }


def _shot_warnings(domain, quality, volumes, validation):
    """Collect the sub-handlers' warnings plus the disclosures this composition owes the caller."""
    warnings = list(domain.get("warnings") or [])
    warnings.extend(quality.get("warnings") or [])
    if volumes:
        warnings.append(
            "Validation volumes are axis-aligned box approximations of each container's interior and "
            "catch region, carry no fluid modifier, and are hidden from renders; fill fractions from "
            "validate_liquid_result are relative to those boxes, not to exact hollow geometry."
        )
    errors = [finding for finding in validation.get("findings") or [] if finding.get("severity") == "ERROR"]
    if errors:
        warnings.append(
            f"validate_liquid_setup reported {len(errors)} ERROR finding(s) on the built shot; "
            "read setup_validation and fix them before baking."
        )
    warnings.append("No cache was baked; setup_liquid_shot never starts a bake.")
    return warnings


class LiquidShotHandlers:
    """Build a whole liquid shot by composing the single-purpose liquid handlers in one transaction."""

    def setup_liquid_shot(
        self,
        scene_name,
        cache_directory,
        containers,
        sources,
        domain_object_name=None,
        new_domain_name="Liquid Domain",
        modifier_name="Liquid Domain",
        collection_name=None,
        quality="BALANCED",
        solver_patch=None,
        mesh_patch=None,
        cache_type="REPLAY",
        cache_frame_start=1,
        cache_frame_end=250,
        padding=(0.25, 0.25, 0.25),
        expected_travel=(0.0, 0.0, 0.0),
        splash_height=0.0,
        create_validation_volumes=True,
        spill_catch_depth=None,
        spill_catch_margin=None,
        dry_run=False,
    ):
        """
        Turn container/source intent into a complete, unbaked liquid setup.

        Every mutating step delegates to the standalone handler that owns it, so the rules those
        handlers enforce (unbaked domain, explicit cache path, collection scoping, proxy transform
        validation, flow-field gating) apply here unchanged. Baking is deliberately not part of this
        call: the caller runs manage_liquid_cache afterwards.
        """
        scene = _get_scene(scene_name)
        fps = _scene_fps(scene)
        resolved_containers = _resolve_containers(scene, containers)
        resolved_sources = _resolve_sources(scene, sources, fps, scene.frame_start)
        self._reject_role_conflicts(resolved_containers, resolved_sources, domain_object_name)
        if spill_catch_depth is not None:
            spill_catch_depth = _require_positive(spill_catch_depth, "spill_catch_depth")
        if spill_catch_margin is not None:
            spill_catch_margin = _require_positive(spill_catch_margin, "spill_catch_margin")
        if not dry_run and not solver_patch and not mesh_patch:
            raise ValueError(
                "setup_liquid_shot needs the resolved solver/mesh patches for the requested quality "
                "profile; the server tool supplies them from its profile table"
            )
        options = {
            "domain_object_name": domain_object_name,
            "new_domain_name": new_domain_name,
            "modifier_name": modifier_name,
            "collection_name": collection_name,
            "quality": quality,
            "solver_patch": solver_patch,
            "mesh_patch": mesh_patch,
            "cache_directory": cache_directory,
            "cache_type": cache_type,
            "cache_frame_start": cache_frame_start,
            "cache_frame_end": cache_frame_end,
            "padding": padding,
            "expected_travel": expected_travel,
            "splash_height": splash_height,
            "create_validation_volumes": create_validation_volumes,
            "spill_catch_depth": spill_catch_depth,
            "spill_catch_margin": spill_catch_margin,
        }
        plan = _shot_plan(scene, fps, resolved_containers, resolved_sources, options)
        if dry_run:
            return self._dry_run_report(scene, plan, options)
        return self._execute_shot(scene, plan, resolved_containers, resolved_sources, options)

    @staticmethod
    def _reject_role_conflicts(resolved_containers, resolved_sources, domain_object_name):
        """Reject a request where one object would be asked to play two incompatible roles."""
        container_names = [entry["object"].name for entry in resolved_containers]
        source_names = [entry["object"].name for entry in resolved_sources]
        for label, names in (("containers", container_names), ("sources", source_names)):
            if len(set(names)) != len(names):
                raise ValueError(f"{label} lists the same object more than once")
        overlap = sorted(set(container_names) & set(source_names))
        if overlap:
            raise ValueError(f"Objects cannot be both container and source: {', '.join(overlap)}")
        if domain_object_name is not None and domain_object_name in {*container_names, *source_names}:
            raise ValueError("The domain object cannot also be a container or a source")

    def _dry_run_report(self, scene, plan, options):
        """Report the resolved plan plus whatever preflight can run before the shot exists."""
        domain_object_name = options["domain_object_name"]
        preflight = None
        if domain_object_name is not None and bpy.data.objects.get(domain_object_name) is not None:
            preflight = self.validate_liquid_setup(scene.name, [domain_object_name])
        warnings = [
            "dry_run resolved the plan only; no object, modifier, cache directory, or validation "
            "volume was created and no existing object was modified.",
        ]
        if preflight is None:
            warnings.append(
                "Structural findings from validate_liquid_setup need a domain that already exists; "
                "run this call without dry_run and read setup_validation to validate the built shot."
            )
        else:
            warnings.append(
                "existing_setup_validation describes the supplied domain as it stands now, not the planned shot."
            )
        return {
            "dry_run": True,
            "scene": scene.name,
            "simulation_id": None,
            "planned_domain": domain_object_name,
            "planned_modifier": options["modifier_name"],
            "plan": plan,
            "existing_setup_validation": preflight,
            "changed_objects": [],
            "retained_live_modifier": True,
            "warnings": warnings,
        }

    def _execute_shot(self, scene, plan, resolved_containers, resolved_sources, options):
        """Run every step for real; the dispatcher's mutation_transaction makes the whole set atomic."""
        simulation_id = uuid.uuid4().hex
        domain = self.create_liquid_domain(
            scene.name,
            options["cache_directory"],
            object_name=options["domain_object_name"],
            new_object_name=options["new_domain_name"],
            collection_name=options["collection_name"],
            modifier_name=options["modifier_name"],
            cache_type=options["cache_type"],
            cache_frame_start=options["cache_frame_start"],
            cache_frame_end=options["cache_frame_end"],
        )
        domain_name = domain["object"]
        domain_object, domain_modifier, domain_settings = _get_domain(domain_name, options["modifier_name"])
        domain_object[SHOT_ID_PROPERTY] = simulation_id
        containers_result = self._build_containers(scene, resolved_containers, domain_name, options, simulation_id)
        sources_result = self._build_sources(resolved_sources, domain_name, simulation_id)
        fitted = self.fit_liquid_domain(
            scene.name,
            [entry["object"].name for entry in resolved_sources],
            collider_object_names=[entry["object"].name for entry in resolved_containers],
            domain_object_name=domain_name,
            modifier_name=options["modifier_name"],
            padding=options["padding"],
            expected_travel=options["expected_travel"],
            splash_height=options["splash_height"],
        )
        quality = self.apply_liquid_quality_profile(
            domain_name,
            options["modifier_name"],
            plan["quality"],
            solver_patch=options["solver_patch"],
            mesh_patch=options["mesh_patch"],
        )
        volumes = self._build_validation_volumes(
            scene, resolved_containers, domain_settings, domain["domain_uuid"], options, simulation_id
        )
        validation = self.validate_liquid_setup(scene.name, [domain_name])
        changed = {domain_name}
        changed.update(entry["object"] for entry in containers_result)
        changed.update(entry["proxy"] for entry in containers_result if entry["proxy"])
        changed.update(entry["object"].name for entry in sources_result)
        changed.update(entry["volume"] for entry in volumes)
        return {
            "dry_run": False,
            "scene": scene.name,
            "simulation_id": simulation_id,
            "simulation_id_property": SHOT_ID_PROPERTY,
            "domain": domain_name,
            "domain_modifier": domain_modifier.name,
            "domain_uuid": domain["domain_uuid"],
            "created_domain_object": domain["created_object"],
            "plan": plan,
            "containers": containers_result,
            "sources": _source_summaries(sources_result),
            "domain_fit": fitted,
            "quality": quality,
            "validation_volumes": volumes,
            "setup_validation": validation,
            "cache_directory_resolved": domain["cache_directory_resolved"],
            "changed_objects": sorted(changed),
            "retained_live_modifier": True,
            "next_actions": [
                "Read setup_validation and fix every ERROR finding before baking.",
                f"Bake with manage_liquid_cache START_BAKE on '{domain_name}'; this tool never bakes.",
                f"After baking, call validate_liquid_result with simulation_id '{simulation_id}'.",
            ],
            "warnings": _shot_warnings(domain, quality, volumes, validation),
        }

    def _build_containers(self, scene, resolved_containers, domain_name, options, simulation_id):
        """Give every container a collider, through the hollow proxy rig when one was requested."""
        results = []
        for entry in resolved_containers:
            obj = entry["object"]
            if entry["collision_proxy"] == "HOLLOW_CONTAINER":
                # create_liquid_proxy_rig installs the effector modifier on the proxy itself, so the
                # source container must not also get one - that would collide twice.
                rig = self.create_liquid_proxy_rig(
                    scene.name,
                    obj.name,
                    entry["proxy_object_name"],
                    domain_name,
                    options["modifier_name"],
                    "EFFECTOR",
                    geometry="HOLLOW_CONTAINER",
                    wall_thickness=entry["wall_thickness"],
                    bottom_thickness=entry["bottom_thickness"],
                    rim_axis=entry["rim_axis"],
                    effector_settings=entry["effector_settings"],
                )
                proxy_object = _get_object(rig["proxy"], {"MESH"})
                proxy_object[SHOT_ID_PROPERTY] = simulation_id
                results.append(
                    {
                        "object": obj.name,
                        "collision_proxy": "HOLLOW_CONTAINER",
                        "proxy": rig["proxy"],
                        "proxy_uuid": rig["proxy_uuid"],
                        "fluid_modifier": rig["fluid_modifier"],
                        "collection": rig["collection"],
                        "transform_validation": rig["transform_validation"],
                        "warnings": rig["warnings"],
                    }
                )
                continue
            effector = self.add_liquid_effector(
                obj.name,
                domain_name,
                effector_type=entry["effector_type"],
                settings=entry["effector_settings"],
            )
            obj[SHOT_ID_PROPERTY] = simulation_id
            results.append(
                {
                    "object": obj.name,
                    "collision_proxy": "NONE",
                    "proxy": None,
                    "proxy_uuid": None,
                    "fluid_modifier": effector["modifier"],
                    "collection": effector["effector_collection"],
                    "transform_validation": None,
                    "warnings": effector.get("warnings") or [],
                }
            )
        return results

    def _build_sources(self, resolved_sources, domain_name, simulation_id):
        """Add each flow and, when a seconds window was given, key Use Flow on and off around it."""
        results = []
        for entry in resolved_sources:
            obj = entry["object"]
            flow = self.add_liquid_flow(
                obj.name,
                domain_name,
                behavior=entry["behavior"],
                settings=entry["flow_settings"],
            )
            obj[SHOT_ID_PROPERTY] = simulation_id
            animation = None
            if entry["enabled_frames"] is not None:
                on_frame, off_frame = entry["enabled_frames"]
                animation = self.animate_liquid_flow(
                    obj.name,
                    flow["modifier"],
                    domain_name,
                    [
                        {"frame": on_frame, "use_inflow": True},
                        {"frame": off_frame, "use_inflow": False},
                    ],
                )
            results.append(
                {
                    "object": obj,
                    "behavior": entry["behavior"],
                    "fluid_modifier": flow["modifier"],
                    "flow_collection": flow["flow_collection"],
                    "enabled_seconds": entry["enabled_seconds"],
                    "enabled_frames": list(entry["enabled_frames"]) if entry["enabled_frames"] else None,
                    "animation": animation,
                }
            )
        return results

    def _build_validation_volumes(
        self, scene, resolved_containers, domain_settings, domain_uuid, options, simulation_id
    ):
        """Create the measurement boxes validate_liquid_result reads, tagged so it can find them."""
        if not options["create_validation_volumes"]:
            return []
        collection, _created, _linked = _ensure_collection(
            scene, f"{options['new_domain_name']} {VALIDATION_COLLECTION_SUFFIX}"
        )
        entries = []
        registrations = []
        for entry in resolved_containers:
            obj = entry["object"]
            bounds = _world_bounds(obj, evaluated=True)
            bottom_thickness = entry["bottom_thickness"] or entry["wall_thickness"]
            specs = [
                (
                    "CONTAINER_VOLUME",
                    f"{obj.name} Interior Volume",
                    _interior_box(bounds, entry["rim_axis"], entry["wall_thickness"], bottom_thickness),
                )
            ]
            if options["spill_catch_depth"] is not None:
                margin = options["spill_catch_margin"] or _spill_margin(bounds, entry["rim_axis"])
                specs.append(
                    (
                        "SPILL_VOLUME",
                        f"{obj.name} Spill Volume",
                        _spill_box(bounds, entry["rim_axis"], options["spill_catch_depth"], margin),
                    )
                )
            for role, name, (center, dimensions) in specs:
                volume = _box_object(name, center, dimensions)
                _link_object(collection, volume)
                volume[SHOT_ID_PROPERTY] = simulation_id
                volume[VOLUME_CONTAINER_PROPERTY] = obj.name
                volume_uuid = _tag_liquid_object(volume, role)["uuid"]
                registrations.append((volume_uuid, volume.name, role))
                entries.append(
                    {
                        "volume": volume.name,
                        "volume_uuid": volume_uuid,
                        "role": role,
                        "container": obj.name,
                        "collection": collection.name,
                        "center": [float(value) for value in center],
                        "dimensions": [float(value) for value in dimensions],
                        "enclosed_volume": float(dimensions[0] * dimensions[1] * dimensions[2]),
                    }
                )
        _register_owned_objects(domain_settings, domain_uuid, registrations)
        bpy.context.view_layer.update()
        return entries
