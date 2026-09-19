# pyright: reportArgumentType=false, reportGeneralTypeIssues=false
"""Liquid mesh, secondary-particle, diffusion, material, and render-setup handlers."""

import contextlib
import math

import bpy

from ..texture.materials import _MATERIAL_PRESETS as _PBR_MATERIAL_PRESETS
from .inspection_and_setup import (
    _ensure_collection,
    _finite,
    _get_domain,
    _get_object,
    _patch_rna,
    _reject_baked,
    _world_bounds,
)
from .simulation import (
    _SECONDARY_TOGGLES,
    _cache_state,
    _reject_cache_flags,
    _scene_context_for_object,
    _update_or_restore,
)

# Recorded on ParticleSettings (not the ParticleSystem, which rejects ID properties) the first time a
# Mantaflow system is observed, so later reads survive a rename of either datablock.
_PARTICLE_ROLE_PROPERTY = "blendermcp_particle_role"
_MESH_FIELDS = {
    "use_mesh",
    "mesh_scale",
    "mesh_particle_radius",
    "mesh_smoothen_pos",
    "mesh_smoothen_neg",
    "mesh_concave_upper",
    "mesh_concave_lower",
    "mesh_generator",
    "use_speed_vectors",
    "cache_mesh_format",
}
_SECONDARY_FIELDS = {
    "use_spray_particles",
    "use_foam_particles",
    "use_bubble_particles",
    "use_tracer_particles",
    "sndparticle_combined_export",
    "sndparticle_boundary",
    "sndparticle_life_min",
    "sndparticle_life_max",
    "sndparticle_potential_min_wavecrest",
    "sndparticle_potential_max_wavecrest",
    "sndparticle_potential_min_trappedair",
    "sndparticle_potential_max_trappedair",
    "sndparticle_potential_min_energy",
    "sndparticle_potential_max_energy",
    "sndparticle_sampling_wavecrest",
    "sndparticle_sampling_trappedair",
    "sndparticle_potential_radius",
    "sndparticle_update_radius",
    "sndparticle_bubble_buoyancy",
    "sndparticle_bubble_drag",
    "particle_scale",
}
_DIFFUSION_FIELDS = {
    "use_diffusion",
    "viscosity_base",
    "viscosity_exponent",
    "use_viscosity",
    "viscosity_value",
    "surface_tension",
}
_LIQUID_PRESETS = {
    "WATER": {
        "use_diffusion": True,
        "viscosity_base": 1.0,
        "viscosity_exponent": 6,
        "use_viscosity": False,
        "viscosity_value": 1.0,
        "surface_tension": 0.0,
    },
    "OIL": {
        "use_diffusion": True,
        "viscosity_base": 5.0,
        "viscosity_exponent": 5,
        "use_viscosity": False,
        "viscosity_value": 1.0,
        "surface_tension": 0.2,
    },
    "HONEY": {
        "use_diffusion": True,
        "viscosity_base": 2.0,
        "viscosity_exponent": 3,
        "use_viscosity": True,
        "viscosity_value": 2.0,
        "surface_tension": 0.8,
    },
    "MOLTEN": {
        "use_diffusion": True,
        "viscosity_base": 5.0,
        "viscosity_exponent": 3,
        "use_viscosity": True,
        "viscosity_value": 1.5,
        "surface_tension": 1.0,
    },
    "STYLIZED": {
        "use_diffusion": True,
        "viscosity_base": 1.0,
        "viscosity_exponent": 2,
        "use_viscosity": True,
        "viscosity_value": 2.0,
        "surface_tension": 2.0,
    },
}


def _estimate_mesh_output(obj, settings):
    bounds = _world_bounds(obj, evaluated=False)
    longest = max(bounds["dimensions"])
    cell = longest / settings.resolution_max if longest > 0 else 0.0
    scale = int(settings.mesh_scale)
    dimensions = [max(1, math.ceil(value / cell) * scale) if cell > 0 else 0 for value in bounds["dimensions"]]
    return {
        "base_resolution_max": int(settings.resolution_max),
        "mesh_scale": scale,
        "estimated_longest_axis_resolution": int(settings.resolution_max) * scale,
        "estimated_cells_xyz": dimensions,
        "base_cell_size_world": cell,
        "note": "Grid dimensions are geometric estimates, not occupied cells or output polygon counts.",
    }


def _expand_viscosity_config(config):
    config = dict(config or {})
    preset = config.pop("preset", None)
    dynamic = config.pop("dynamic_viscosity_pa_s", None)
    density = config.pop("density_kg_m3", None)
    source = "DIRECT"
    conversion = None
    if preset is not None:
        if preset not in _LIQUID_PRESETS:
            raise ValueError(f"Unknown viscosity preset: {preset}")
        expanded = dict(_LIQUID_PRESETS[preset])
        expanded.update(config)
        source = f"PRESET:{preset}"
    elif dynamic is not None or density is not None:
        if dynamic is None or density is None or dynamic <= 0 or density <= 0:
            raise ValueError("Positive dynamic_viscosity_pa_s and density_kg_m3 are both required")
        kinematic = float(dynamic) / float(density)
        exponent = max(0, min(10, math.ceil(-math.log10(kinematic))))
        base = kinematic * (10**exponent)
        if not 0.0 < base <= 10.0:
            raise ValueError("Converted kinematic viscosity is outside Blender's base/exponent range [1e-10, 10]")
        expanded = {
            "use_diffusion": True,
            "viscosity_base": base,
            "viscosity_exponent": exponent,
            **config,
        }
        source = "SI_DYNAMIC_DENSITY"
        conversion = {
            "dynamic_viscosity_pa_s": dynamic,
            "density_kg_m3": density,
            "kinematic_viscosity_m2_s": kinematic,
            "formula": "dynamic_viscosity_pa_s / density_kg_m3",
        }
    else:
        expanded = config
    return expanded, source, conversion


def _octahedron_mesh(name):
    mesh = bpy.data.meshes.new(f"{name} Mesh")
    vertices = [(1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)]
    faces = [
        (0, 2, 4),
        (2, 1, 4),
        (1, 3, 4),
        (3, 0, 4),
        (2, 0, 5),
        (1, 2, 5),
        (3, 1, 5),
        (0, 3, 5),
    ]
    mesh.from_pydata(vertices, [], faces)
    mesh.update()
    return mesh


def _classify_particle_role_by_name(system):
    """
    Derive a role from Blender's freshly generated system/settings labels.

    Only used the first time a Mantaflow system is seen, before its role is recorded; every later
    read comes from the stored property so a rename cannot silently reclassify a system.
    """
    label = f"{system.name} {getattr(system.settings, 'name', '')}".lower()
    roles = [role for role in ("spray", "foam", "bubble", "tracer") if role in label]
    return "+".join(role.upper() for role in roles) if roles else "UNKNOWN"


def _particle_role(system):
    """
    Return the recorded role for a Mantaflow particle system, falling back to its labels.

    The role lives on ``system.settings`` because ParticleSystem itself rejects ID properties
    ("id properties not supported for this type" in Blender 5.2.1) while ParticleSettings is a real
    datablock that accepts them.
    """
    recorded = getattr(system.settings, "get", lambda _key: None)(_PARTICLE_ROLE_PROPERTY)
    if isinstance(recorded, str) and recorded:
        return recorded
    return _classify_particle_role_by_name(system)


def _tag_particle_roles(obj):
    """
    Record ``blendermcp_particle_role`` on any of the domain's systems that lack it, then report them.

    Mantaflow, not this add-on, creates the systems, so the earliest moment a role can be captured is
    the call that enables the corresponding secondary-particle toggle - at which point Blender's own
    labels are still authoritative.
    """
    tagged = []
    for system in obj.particle_systems:
        settings = system.settings
        recorded = settings.get(_PARTICLE_ROLE_PROPERTY) if hasattr(settings, "get") else None
        if not isinstance(recorded, str) or not recorded:
            role = _classify_particle_role_by_name(system)
            settings[_PARTICLE_ROLE_PROPERTY] = role
            tagged.append({"system": system.name, "settings": settings.name, "role": role})
    return tagged


class LiquidMeshAndMaterialHandlers:
    """Configure liquid mesh, secondary particles, diffusion, materials, and particle render setups."""

    def configure_liquid_mesh(self, domain_object_name, modifier_name, patch):
        obj, modifier, settings = _get_domain(domain_object_name, modifier_name)
        if not patch:
            raise ValueError("Mesh patch cannot be empty")
        _reject_cache_flags(
            settings,
            ("has_cache_baked_mesh", "is_cache_baking_mesh", "is_cache_baking_any"),
            "Cannot change liquid mesh settings while the mesh stage is baked or baking",
        )
        if "use_speed_vectors" in patch:
            _reject_cache_flags(
                settings,
                ("has_cache_baked_data", "has_cache_baked_mesh", "is_cache_baking_any"),
                "Speed vectors must be configured before data or mesh baking",
            )
        concave_lower = patch.get("mesh_concave_lower", settings.mesh_concave_lower)
        concave_upper = patch.get("mesh_concave_upper", settings.mesh_concave_upper)
        if concave_lower > concave_upper:
            raise ValueError("mesh_concave_lower must be <= mesh_concave_upper")
        changes = _patch_rna(settings, patch, _MESH_FIELDS)
        _update_or_restore(obj, settings, changes)
        return {
            "changed_objects": [obj.name],
            "object": obj.name,
            "modifier": modifier.name,
            "changes": changes,
            "estimated_output": _estimate_mesh_output(obj, settings),
            "cache": _cache_state(settings),
            "invalidated_cache_stages": ["MESH"],
            "data_rebake_required": "use_speed_vectors" in changes,
            "next_required_bake_stage": "DATA" if "use_speed_vectors" in changes else "MESH",
            "retained_live_modifier": True,
        }

    def configure_liquid_secondary_particles(self, domain_object_name, modifier_name, patch):
        obj, modifier, settings = _get_domain(domain_object_name, modifier_name)
        if not patch:
            raise ValueError("Secondary-particle patch cannot be empty")
        _reject_baked(settings)
        prospective = {name: patch.get(name, getattr(settings, name)) for name in _SECONDARY_FIELDS}
        pairs = (
            ("sndparticle_life_min", "sndparticle_life_max"),
            ("sndparticle_potential_min_wavecrest", "sndparticle_potential_max_wavecrest"),
            ("sndparticle_potential_min_trappedair", "sndparticle_potential_max_trappedair"),
            ("sndparticle_potential_min_energy", "sndparticle_potential_max_energy"),
        )
        for minimum, maximum in pairs:
            if prospective[minimum] > prospective[maximum]:
                raise ValueError(f"{minimum} must be <= {maximum}")
        changes = _patch_rna(settings, patch, _SECONDARY_FIELDS)
        _update_or_restore(obj, settings, changes)
        # Enabling a toggle is when Mantaflow materializes the matching system, so this is the first
        # and most reliable moment to record its role for later name-independent lookups.
        tagged_roles = _tag_particle_roles(obj)
        enabled = [
            name.removeprefix("use_").removesuffix("_particles").upper()
            for name in _SECONDARY_TOGGLES
            if getattr(settings, name)
        ]
        return {
            "changed_objects": [obj.name],
            "object": obj.name,
            "modifier": modifier.name,
            "changes": changes,
            "enabled_particle_types": enabled,
            "particle_roles_recorded": tagged_roles,
            "combined_export": settings.sndparticle_combined_export,
            "combined_export_semantics": (
                "OFF creates separate eligible systems; another value combines the named roles into one output."
            ),
            "invalidated_cache_stages": ["DATA", "PARTICLES"],
            "next_required_bake_stage": "DATA",
            "warnings": ["Secondary particles increase simulation, cache, and render cost."] if enabled else [],
        }

    def configure_liquid_diffusion(self, domain_object_name, modifier_name, config):
        obj, modifier, settings = _get_domain(domain_object_name, modifier_name)
        if not config:
            raise ValueError("Diffusion config cannot be empty")
        _reject_baked(settings)
        patch, source, conversion = _expand_viscosity_config(config)
        if not patch:
            raise ValueError("Diffusion config does not contain a setting")
        changes = _patch_rna(settings, patch, _DIFFUSION_FIELDS)
        _update_or_restore(obj, settings, changes)
        kinematic = float(settings.viscosity_base) * (10.0 ** (-int(settings.viscosity_exponent)))
        warnings = []
        if settings.use_viscosity and settings.viscosity_value >= 5:
            warnings.append("Very high viscosity can require smaller time steps and may destabilize the solve.")
        warnings.append("Viscosity is scene-scale sensitive and does not turn liquid into rigid-body material.")
        return {
            "changed_objects": [obj.name],
            "object": obj.name,
            "modifier": modifier.name,
            "preset_schema_version": 1,
            "source": source,
            "expanded_values": patch,
            "si_conversion": conversion,
            "represented_kinematic_viscosity_m2_s": kinematic,
            "changes": changes,
            "invalidated_cache_stages": ["DATA", "MESH", "PARTICLES"],
            "warnings": warnings,
        }

    def create_liquid_material(
        self,
        domain_object_name,
        modifier_name,
        material_name,
        config,
        existing_policy="ERROR",
        assignment="APPEND",
        slot_index=None,
    ):
        """Resolve the domain and preset, then delegate node construction and assignment to the shared PBR handlers."""
        obj, modifier, settings = _get_domain(domain_object_name, modifier_name)
        if existing_policy not in {"ERROR", "REUSE"}:
            raise ValueError("existing_policy must be ERROR or REUSE")
        if assignment not in {"APPEND", "REPLACE_SLOT"}:
            raise ValueError("assignment must be APPEND or REPLACE_SLOT")
        if assignment == "REPLACE_SLOT":
            if slot_index is None or not 0 <= slot_index < len(obj.data.materials):
                raise ValueError("REPLACE_SLOT requires an existing valid slot_index")
        elif slot_index is not None:
            raise ValueError("slot_index is valid only with REPLACE_SLOT")
        existing_material = bpy.data.materials.get(material_name)
        created = existing_material is None
        if existing_material is not None and existing_policy == "ERROR":
            raise ValueError(f"Material already exists: {material_name}")
        preset = config.get("preset", "WATER")
        if preset not in _PBR_MATERIAL_PRESETS:
            raise ValueError(f"Unknown liquid material preset: {preset}")
        overrides = {key: value for key, value in config.items() if key != "preset"}
        values = {**_PBR_MATERIAL_PRESETS[preset], **overrides}
        for color_name in ("base_color", "volume_absorption_color"):
            color = values[color_name]
            _finite(color, color_name)
            if len(color) != 4 or any(not 0.0 <= component <= 1.0 for component in color):
                raise ValueError(f"{color_name} must contain four values in [0, 1]")

        creation = self.create_pbr_material(
            material_name,
            target_engine="BOTH",
            preset=preset,
            settings=overrides,
            reuse_existing=(existing_policy == "REUSE"),
        )
        material = bpy.data.materials[material_name]
        if created:
            material["blendermcp_liquid_material"] = obj.name
            material["blendermcp_liquid_material_schema"] = 1
        try:
            assignment_result = self.assign_material(material_name, [obj.name], mode=assignment, slot_index=slot_index)
        except Exception:
            if created:
                with contextlib.suppress(Exception):
                    bpy.data.materials.remove(material)  # pyright: ignore[reportArgumentType]
            raise
        changed_objects = list(assignment_result["changed_objects"])
        return {
            "changed_objects": changed_objects,
            "changed_resources": creation["changed_resources"],
            "domain": obj.name,
            "modifier": modifier.name,
            "material": material.name,
            "created": created,
            "configured_nodes": (
                {"surface_node": "PBR Principled BSDF", "output_node": "PBR Material Output"} if created else None
            ),
            "preset_schema_version": 1,
            "expanded_values": values if created else None,
            "existing_material_reused_unchanged": not created,
            "assignment": {
                "policy": assignment,
                "slot_index": assignment_result["assignments"][0]["slot_index"],
                "slot_changed": bool(changed_objects),
            },
            "solver_settings_changed": False,
            "cache_changed": False,
            "warnings": [] if settings.use_mesh else ["Liquid mesh generation is currently disabled on this domain."],
        }

    def create_secondary_particle_render_setup(
        self,
        domain_object_name,
        modifier_name,
        representation="OBJECT",
        instance_object_name=None,
        create_instance_sphere=False,
        helper_collection_name="Liquid Particle Helpers",
        helper_object_name="Liquid Particle Instance",
        material_name=None,
        display_percentage=25,
        particle_size=None,
        max_systems=16,
    ):
        obj, modifier, settings = _get_domain(domain_object_name, modifier_name)
        if representation != "OBJECT":
            raise ValueError("Phase 1 supports OBJECT particle representation only")
        if bool(instance_object_name) == bool(create_instance_sphere):
            raise ValueError("Provide exactly one of instance_object_name or create_instance_sphere=True")
        if not settings.has_cache_baked_particles:
            raise ValueError("Secondary particles must be baked before configuring their render systems")
        systems = list(obj.particle_systems)
        if len(systems) > max_systems:
            raise ValueError(f"Domain has {len(systems)} particle systems, exceeding max_systems={max_systems}")
        tagged_roles = _tag_particle_roles(obj)
        discovered = [(system, _particle_role(system)) for system in systems]
        eligible = [(system, role) for system, role in discovered if role != "UNKNOWN"]
        if not eligible:
            raise ValueError("No publicly identifiable Mantaflow secondary particle systems were found on the domain")
        scene, _view_layer = _scene_context_for_object(obj)
        material = None
        if material_name:
            material = bpy.data.materials.get(material_name)
            if material is None:
                raise ValueError(f"Material not found: {material_name}")
        created_helper = False
        helper_collection_linked = False
        if instance_object_name:
            instance = _get_object(instance_object_name, {"MESH"})
        else:
            if bpy.data.objects.get(helper_object_name) is not None:
                raise ValueError(f"Helper object already exists: {helper_object_name}")
            collection, _created, helper_collection_linked = _ensure_collection(scene, helper_collection_name)
            mesh = _octahedron_mesh(helper_object_name)
            instance = bpy.data.objects.new(helper_object_name, mesh)
            collection.objects.link(instance)
            instance.scale = (0.025, 0.025, 0.025)
            instance.display_type = "WIRE"
            instance.hide_render = True
            instance["blendermcp_liquid_helper"] = obj.name
            created_helper = True
            if material is not None:
                instance.data.materials.append(material)
        snapshots = []
        try:
            for system, _role in eligible:
                particle_settings = system.settings
                snapshots.append(
                    (
                        particle_settings,
                        particle_settings.render_type,
                        particle_settings.instance_object,
                        particle_settings.display_percentage,
                        particle_settings.particle_size,
                    )
                )
                particle_settings.render_type = "OBJECT"
                particle_settings.instance_object = instance
                particle_settings.display_percentage = display_percentage
                if particle_size is not None:
                    particle_settings.particle_size = particle_size
            bpy.context.view_layer.update()
        except Exception:
            for particle_settings, render_type, old_instance, percentage, old_size in reversed(snapshots):
                with contextlib.suppress(Exception):
                    particle_settings.render_type = render_type
                    particle_settings.instance_object = old_instance
                    particle_settings.display_percentage = percentage
                    particle_settings.particle_size = old_size
            if created_helper:
                with contextlib.suppress(Exception):
                    bpy.data.objects.remove(instance, do_unlink=True)  # pyright: ignore[reportArgumentType]
            if helper_collection_linked:
                with contextlib.suppress(Exception):
                    scene.collection.children.unlink(collection)  # pyright: ignore[reportArgumentType]
            raise
        mappings = []
        for system, role in discovered:
            count = len(system.particles)
            mappings.append(
                {
                    "system": system.name,
                    "settings": system.settings.name,
                    "role": role,
                    "configured": role != "UNKNOWN",
                    "particle_count_current_frame": count,
                    "estimated_viewport_instances": math.ceil(count * display_percentage / 100),
                    "estimated_render_instances": count,
                }
            )
        return {
            "changed_objects": [obj.name, instance.name] if created_helper else [obj.name],
            "domain": obj.name,
            "modifier": modifier.name,
            "instance_object": instance.name,
            "instance_helper_created": created_helper,
            "systems": mappings,
            "particle_roles_recorded": tagged_roles,
            "unknown_systems_left_unchanged": [item["system"] for item in mappings if not item["configured"]],
            "classification_basis": (
                f"Recorded '{_PARTICLE_ROLE_PROPERTY}' custom property on each system's ParticleSettings, "
                "first derived from Blender's own labels; unrecognized systems are not mutated."
            ),
            "warnings": ["Particle counts are observed at the current frame and may vary over the bake."],
        }
