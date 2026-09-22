"""Typed tools for liquid render mesh, secondary particles, diffusion, and materials."""

from typing import Annotated, Literal

from mcp.server.fastmcp import Context
from pydantic import Field, model_validator

from ...app import mcp
from .._dispatch import call_blender
from .._inputs import StrictModel, dump_input
from .inspection_and_setup import ExistingPolicy

MeshGenerator = Literal["IMPROVED", "UNION"]
CacheMeshFormat = Literal["UNI", "OPENVDB", "RAW"]
CombinedParticleExport = Literal["OFF", "SPRAY_FOAM", "SPRAY_BUBBLES", "FOAM_BUBBLES", "SPRAY_FOAM_BUBBLES"]
ParticleBoundary = Literal["DELETE", "PUSHOUT"]
ViscosityPreset = Literal["WATER", "OIL", "HONEY", "MOLTEN", "STYLIZED"]
MaterialPreset = Literal["WATER", "GLASS", "OIL", "TINTED"]
LiquidMaterialAssignment = Literal["APPEND", "REPLACE_SLOT"]
ParticleRepresentation = Literal["OBJECT"]


class LiquidMeshPatch(StrictModel):
    """Allowlisted Blender 5.1 liquid mesh-generation properties."""

    use_mesh: bool | None = None
    mesh_scale: int | None = Field(default=None, ge=1, le=8)
    mesh_particle_radius: float | None = Field(default=None, gt=0.0, le=10.0)
    mesh_smoothen_pos: int | None = Field(default=None, ge=0, le=100)
    mesh_smoothen_neg: int | None = Field(default=None, ge=0, le=100)
    mesh_concave_upper: float | None = Field(default=None, ge=0.0, le=10.0)
    mesh_concave_lower: float | None = Field(default=None, ge=0.0, le=10.0)
    mesh_generator: MeshGenerator | None = None
    use_speed_vectors: bool | None = None
    cache_mesh_format: CacheMeshFormat | None = None

    @model_validator(mode="after")
    def validate_concave_range(self) -> "LiquidMeshPatch":
        if (
            self.mesh_concave_lower is not None
            and self.mesh_concave_upper is not None
            and self.mesh_concave_lower > self.mesh_concave_upper
        ):
            raise ValueError("mesh_concave_lower must be <= mesh_concave_upper")
        return self


class LiquidSecondaryParticlePatch(StrictModel):
    """Allowlisted spray, foam, bubble, and tracer particle properties."""

    use_spray_particles: bool | None = None
    use_foam_particles: bool | None = None
    use_bubble_particles: bool | None = None
    use_tracer_particles: bool | None = None
    sndparticle_combined_export: CombinedParticleExport | None = None
    sndparticle_boundary: ParticleBoundary | None = None
    sndparticle_life_min: float | None = Field(default=None, ge=0.0, le=10_000.0)
    sndparticle_life_max: float | None = Field(default=None, ge=0.0, le=10_000.0)
    sndparticle_potential_min_wavecrest: float | None = Field(default=None, ge=0.0, le=1_000.0)
    sndparticle_potential_max_wavecrest: float | None = Field(default=None, ge=0.0, le=1_000.0)
    sndparticle_potential_min_trappedair: float | None = Field(default=None, ge=0.0, le=1_000.0)
    sndparticle_potential_max_trappedair: float | None = Field(default=None, ge=0.0, le=1_000.0)
    sndparticle_potential_min_energy: float | None = Field(default=None, ge=0.0, le=1_000.0)
    sndparticle_potential_max_energy: float | None = Field(default=None, ge=0.0, le=1_000.0)
    sndparticle_sampling_wavecrest: int | None = Field(default=None, ge=0, le=10_000)
    sndparticle_sampling_trappedair: int | None = Field(default=None, ge=0, le=10_000)
    # Both radii are integer cell counts in Blender 5.1+ (RNA hard range 1-4), not float distances.
    sndparticle_potential_radius: int | None = Field(default=None, ge=1, le=4)
    sndparticle_update_radius: int | None = Field(default=None, ge=1, le=4)
    sndparticle_bubble_buoyancy: float | None = Field(default=None, ge=0.0, le=100.0)
    sndparticle_bubble_drag: float | None = Field(default=None, ge=0.0, le=100.0)
    particle_scale: int | None = Field(default=None, ge=1, le=100)

    @model_validator(mode="after")
    def validate_ranges(self) -> "LiquidSecondaryParticlePatch":
        pairs = (
            (self.sndparticle_life_min, self.sndparticle_life_max, "life"),
            (
                self.sndparticle_potential_min_wavecrest,
                self.sndparticle_potential_max_wavecrest,
                "wavecrest potential",
            ),
            (
                self.sndparticle_potential_min_trappedair,
                self.sndparticle_potential_max_trappedair,
                "trapped-air potential",
            ),
            (self.sndparticle_potential_min_energy, self.sndparticle_potential_max_energy, "energy potential"),
        )
        for minimum, maximum, label in pairs:
            if minimum is not None and maximum is not None and minimum > maximum:
                raise ValueError(f"{label} minimum must be <= maximum")
        return self


class LiquidDiffusionConfig(StrictModel):
    """Viscosity and surface tension from exactly one source: preset, physical units, or base/exponent."""

    preset: ViscosityPreset | None = None
    use_diffusion: bool | None = None
    viscosity_base: float | None = Field(default=None, ge=0.0, le=10.0)
    viscosity_exponent: int | None = Field(default=None, ge=0, le=10)
    use_viscosity: bool | None = None
    viscosity_value: float | None = Field(default=None, ge=0.0, le=10.0)
    surface_tension: float | None = Field(default=None, ge=0.0, le=100.0)
    dynamic_viscosity_pa_s: float | None = Field(default=None, gt=0.0)
    density_kg_m3: float | None = Field(default=None, gt=0.0)

    @model_validator(mode="after")
    def validate_source(self) -> "LiquidDiffusionConfig":
        if (self.dynamic_viscosity_pa_s is None) != (self.density_kg_m3 is None):
            raise ValueError("dynamic_viscosity_pa_s and density_kg_m3 must be supplied together")
        direct = self.viscosity_base is not None or self.viscosity_exponent is not None
        sources = int(self.preset is not None) + int(self.dynamic_viscosity_pa_s is not None) + int(direct)
        if sources > 1:
            raise ValueError("Choose one viscosity source: preset, dynamic/density conversion, or base/exponent")
        return self


class LiquidMaterialConfig(StrictModel):
    """Shader settings for the liquid surface material, starting from a preset."""

    preset: MaterialPreset = "WATER"
    base_color: tuple[float, float, float, float] | None = None
    transmission_weight: float | None = Field(default=None, ge=0.0, le=1.0)
    ior: float | None = Field(default=None, ge=1.0, le=3.0)
    roughness: float | None = Field(default=None, ge=0.0, le=1.0)
    volume_absorption_color: tuple[float, float, float, float] | None = None
    volume_density: float | None = Field(default=None, ge=0.0)


@mcp.tool()
async def configure_liquid_mesh(
    ctx: Context, domain_object_name: str, modifier_name: str, patch: LiquidMeshPatch
) -> dict:
    """
    Patch render-surface generation on one unbaked liquid domain.

    If both are given, mesh_concave_lower must be <= mesh_concave_upper; the same check is re-run
    against the domain's current values when only one of the pair is supplied.
    """
    return await call_blender(
        "configure_liquid_mesh",
        {"domain_object_name": domain_object_name, "modifier_name": modifier_name, "patch": dump_input(patch)},
        changed_objects=[domain_object_name],
    )


@mcp.tool()
async def configure_liquid_secondary_particles(
    ctx: Context, domain_object_name: str, modifier_name: str, patch: LiquidSecondaryParticlePatch
) -> dict:
    """Patch spray, foam, bubble, and tracer generation on an unbaked liquid domain."""
    return await call_blender(
        "configure_liquid_secondary_particles",
        {"domain_object_name": domain_object_name, "modifier_name": modifier_name, "patch": dump_input(patch)},
        changed_objects=[domain_object_name],
    )


@mcp.tool()
async def configure_liquid_diffusion(
    ctx: Context, domain_object_name: str, modifier_name: str, config: LiquidDiffusionConfig
) -> dict:
    """Configure viscosity and surface tension from direct values, a versioned preset, or SI inputs."""
    return await call_blender(
        "configure_liquid_diffusion",
        {"domain_object_name": domain_object_name, "modifier_name": modifier_name, "config": dump_input(config)},
        changed_objects=[domain_object_name],
    )


@mcp.tool()
async def create_liquid_material(
    ctx: Context,
    domain_object_name: str,
    modifier_name: str,
    material_name: str,
    config: LiquidMaterialConfig | None = None,
    existing_policy: ExistingPolicy = "ERROR",
    assignment: LiquidMaterialAssignment = "APPEND",
    slot_index: Annotated[int, Field(ge=0)] | None = None,
) -> dict:
    """Create and assign a Principled transparent liquid material without clearing unrelated slots."""
    return await call_blender(
        "create_liquid_material",
        {
            "domain_object_name": domain_object_name,
            "modifier_name": modifier_name,
            "material_name": material_name,
            "config": (config or LiquidMaterialConfig()).model_dump(exclude_none=True),
            "existing_policy": existing_policy,
            "assignment": assignment,
            "slot_index": slot_index,
        },
        changed_objects=[domain_object_name],
    )


@mcp.tool()
async def create_secondary_particle_render_setup(
    ctx: Context,
    domain_object_name: str,
    modifier_name: str,
    representation: ParticleRepresentation = "OBJECT",
    instance_object_name: str | None = None,
    create_instance_sphere: bool = False,
    helper_collection_name: str = "Liquid Particle Helpers",
    helper_object_name: str = "Liquid Particle Instance",
    material_name: str | None = None,
    display_percentage: Annotated[int, Field(ge=1, le=100)] = 25,
    particle_size: Annotated[float, Field(gt=0.0)] | None = None,
    max_systems: Annotated[int, Field(ge=1, le=64)] = 16,
) -> dict:
    """Configure discovered baked Mantaflow particle systems for bounded object instancing."""
    return await call_blender(
        "create_secondary_particle_render_setup",
        {
            "domain_object_name": domain_object_name,
            "modifier_name": modifier_name,
            "representation": representation,
            "instance_object_name": instance_object_name,
            "create_instance_sphere": create_instance_sphere,
            "helper_collection_name": helper_collection_name,
            "helper_object_name": helper_object_name,
            "material_name": material_name,
            "display_percentage": display_percentage,
            "particle_size": particle_size,
            "max_systems": max_systems,
        },
        changed_objects=[name for name in [domain_object_name, instance_object_name] if name],
    )
