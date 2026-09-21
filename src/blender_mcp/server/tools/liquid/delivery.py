# MCP tool signatures intentionally expose explicit delivery and proxy policies.
# ruff: file-ignore[multi-line-summary-second-line]
"""Typed tools for production Mantaflow liquid delivery workflows."""

from typing import Annotated, Literal

from mcp.server.fastmcp import Context
from pydantic import Field, model_validator

from ...app import mcp
from .._dispatch import call_blender
from ._shared import _dump, _StrictModel
from .inspection_and_setup import ExistingPolicy, FlowBehavior

ProxyGeometry = Literal["BOX", "CAPSULE", "CONVEX_HULL", "DECIMATED", "HOLLOW_CONTAINER", "SUPPLIED"]
ProxyRole = Literal["FLOW", "EFFECTOR"]
ProxyDriver = Literal["COPY_TRANSFORMS", "PARENT"]
VariantDataPolicy = Literal["COPY", "LINK"]
VariantAnimationPolicy = Literal["COPY", "LINK", "NONE"]
VariantActivationPolicy = Literal["DISABLE_SOURCE", "DISABLE_VARIANT"]
RenderOutputPolicy = Literal["REQUIRE_BAKED", "ALLOW_REPLAY"]
RenderMaterialAssignment = Literal["KEEP", "APPEND", "REPLACE_SLOT"]
ExportFormat = Literal["ALEMBIC", "USD"]
ExportSpace = Literal["WORLD", "LOCAL"]
ExportUnits = Literal["SCENE", "METERS", "CENTIMETERS", "MILLIMETERS"]
ExportAxis = Literal["X", "Y", "Z", "NEGATIVE_X", "NEGATIVE_Y", "NEGATIVE_Z"]


class ProxyFlowSettings(_StrictModel):
    """Constrained settings for a proxy liquid flow."""

    behavior: FlowBehavior = "GEOMETRY"
    use_inflow: bool | None = None
    subframes: int = Field(default=0, ge=0, le=200)
    surface_distance: float = Field(default=1.5, ge=0.0, le=10.0)
    use_initial_velocity: bool = False
    velocity_factor: float = Field(default=1.0, ge=-100.0, le=100.0)


class ProxyEffectorSettings(_StrictModel):
    """Constrained settings for a proxy collision effector."""

    subframes: int = Field(default=0, ge=0, le=200)
    surface_distance: float = Field(default=0.001, ge=0.0, le=10.0)
    use_plane_init: bool = False


class LiquidRenderFinish(_StrictModel):
    """Optional reversible modifiers applied after the fluid modifier."""

    smooth_shading: bool = True
    subdivision_levels: int | None = Field(default=None, ge=0, le=3)
    subdivision_render_levels: int | None = Field(default=None, ge=0, le=4)
    smooth_factor: float | None = Field(default=None, ge=0.0, le=2.0)
    smooth_iterations: int = Field(default=2, ge=1, le=20)
    laplacian_lambda: float | None = Field(default=None, ge=0.0, le=1.0)
    laplacian_iterations: int = Field(default=2, ge=1, le=20)

    @model_validator(mode="after")
    def validate_finish(self) -> "LiquidRenderFinish":
        """Require a viewport subdivision level when a render level is supplied."""
        if self.subdivision_levels is None and self.subdivision_render_levels is not None:
            raise ValueError("subdivision_render_levels requires subdivision_levels")
        return self


@mcp.tool()
async def create_liquid_proxy_rig(
    ctx: Context,
    scene_name: str,
    source_object_name: str,
    proxy_object_name: str,
    domain_object_name: str,
    domain_modifier_name: str,
    role: ProxyRole,
    geometry: ProxyGeometry = "BOX",
    driver: ProxyDriver = "COPY_TRANSFORMS",
    collection_name: str = "Liquid Proxies",
    modifier_name: str = "Liquid Proxy",
    existing_policy: ExistingPolicy = "ERROR",
    decimate_ratio: Annotated[float, Field(ge=0.01, le=1.0)] = 0.2,
    wall_thickness: Annotated[float, Field(gt=0.0, le=10.0)] = 0.05,
    bottom_thickness: Annotated[float, Field(gt=0.0, le=10.0)] | None = None,
    rim_axis: ExportAxis = "Z",
    allow_deforming_proxy: bool = False,
    flow_settings: ProxyFlowSettings | None = None,
    effector_settings: ProxyEffectorSettings | None = None,
    validation_frames: list[int] | None = None,
) -> dict:
    """Create or register a low-cost liquid source/collision proxy that follows a visible asset.

    ``SUPPLIED`` treats ``proxy_object_name`` as an existing proxy; other geometry modes create it.
    Generated proxies follow rigid transforms only. Deforming behavior is accepted only for an explicit
    supplied proxy with ``allow_deforming_proxy=True``. Mantaflow coupling remains one-way.

    ``HOLLOW_CONTAINER`` shells the source's evaluated geometry inward (a live Solidify modifier, so
    it stays reversible) and removes the cap facing ``rim_axis`` to leave an open pour opening.
    ``wall_thickness`` sets the side/rim thickness; ``bottom_thickness`` optionally weights a thicker
    base via a vertex group over the detected opposite cap. Both are validated against the domain's
    cell size, matching ``validate_liquid_setup``'s thin-wall leak check.
    """
    return await call_blender(
        "create_liquid_proxy_rig",
        {
            "scene_name": scene_name,
            "source_object_name": source_object_name,
            "proxy_object_name": proxy_object_name,
            "domain_object_name": domain_object_name,
            "domain_modifier_name": domain_modifier_name,
            "role": role,
            "geometry": geometry,
            "driver": driver,
            "collection_name": collection_name,
            "modifier_name": modifier_name,
            "existing_policy": existing_policy,
            "decimate_ratio": decimate_ratio,
            "wall_thickness": wall_thickness,
            "bottom_thickness": bottom_thickness,
            "rim_axis": rim_axis,
            "allow_deforming_proxy": allow_deforming_proxy,
            "flow_settings": _dump(flow_settings),
            "effector_settings": _dump(effector_settings),
            "validation_frames": validation_frames or [],
        },
        changed_objects=[source_object_name, domain_object_name],
    )


@mcp.tool()
async def duplicate_liquid_setup_variant(
    ctx: Context,
    source_domain_object_name: str,
    source_domain_modifier_name: str,
    variant_domain_object_name: str,
    variant_collection_name: str,
    name_suffix: Annotated[str, Field(min_length=1)],
    cache_directory: Annotated[str, Field(min_length=1)],
    mesh_data_policy: VariantDataPolicy = "COPY",
    material_policy: VariantDataPolicy = "LINK",
    animation_policy: VariantAnimationPolicy = "COPY",
    activation_policy: VariantActivationPolicy = "DISABLE_VARIANT",
) -> dict:
    """Duplicate a complete scoped liquid setup with remapped members and an independent empty cache.

    Flow, effector, force, and guide dependencies discovered from the domain are duplicated. One domain
    is explicitly disabled so overlapping variants cannot evaluate together accidentally.
    """
    return await call_blender(
        "duplicate_liquid_setup_variant",
        {
            "source_domain_object_name": source_domain_object_name,
            "source_domain_modifier_name": source_domain_modifier_name,
            "variant_domain_object_name": variant_domain_object_name,
            "variant_collection_name": variant_collection_name,
            "name_suffix": name_suffix,
            "cache_directory": cache_directory,
            "mesh_data_policy": mesh_data_policy,
            "material_policy": material_policy,
            "animation_policy": animation_policy,
            "activation_policy": activation_policy,
        },
        changed_objects=[source_domain_object_name],
    )


@mcp.tool()
async def prepare_liquid_render_mesh(
    ctx: Context,
    domain_object_name: str,
    modifier_name: str,
    finish: LiquidRenderFinish | None = None,
    output_policy: RenderOutputPolicy = "REQUIRE_BAKED",
    material_name: str | None = None,
    material_assignment: RenderMaterialAssignment = "KEEP",
    material_slot_index: Annotated[int, Field(ge=0)] | None = None,
    subdivision_modifier_name: str = "Liquid Render Subdivision",
    smooth_modifier_name: str = "Liquid Render Smooth",
    laplacian_modifier_name: str = "Liquid Render Laplacian",
    existing_policy: ExistingPolicy = "ERROR",
    delivery_object_name: str | None = None,
) -> dict:
    """Add reversible post-fluid render finishing or create an explicit current-frame delivery mesh."""
    return await call_blender(
        "prepare_liquid_render_mesh",
        {
            "domain_object_name": domain_object_name,
            "modifier_name": modifier_name,
            "finish": (finish or LiquidRenderFinish()).model_dump(exclude_none=True),
            "output_policy": output_policy,
            "material_name": material_name,
            "material_assignment": material_assignment,
            "material_slot_index": material_slot_index,
            "subdivision_modifier_name": subdivision_modifier_name,
            "smooth_modifier_name": smooth_modifier_name,
            "laplacian_modifier_name": laplacian_modifier_name,
            "existing_policy": existing_policy,
            "delivery_object_name": delivery_object_name,
        },
        changed_objects=[domain_object_name],
    )


@mcp.tool()
async def export_liquid_simulation(
    ctx: Context,
    scene_name: str,
    domain_object_name: str,
    modifier_name: str,
    filepath: Annotated[str, Field(min_length=1)],
    file_format: ExportFormat,
    frame_start: int,
    frame_end: int,
    frame_step: Annotated[int, Field(ge=1)] = 1,
    coordinate_space: ExportSpace = "WORLD",
    units: ExportUnits = "SCENE",
    forward_axis: ExportAxis = "NEGATIVE_Z",
    up_axis: ExportAxis = "Y",
    include_surface: bool = True,
    include_secondary_particles: bool = False,
    include_materials: bool = True,
    include_velocity_attributes: bool = True,
    overwrite: bool = False,
    max_frames: Annotated[int, Field(ge=1, le=2_000)] = 500,
) -> dict:
    """Atomically export a baked liquid surface and optional secondary particles to Alembic or USD."""
    return await call_blender(
        "export_liquid_simulation",
        {
            "scene_name": scene_name,
            "domain_object_name": domain_object_name,
            "modifier_name": modifier_name,
            "filepath": filepath,
            "file_format": file_format,
            "frame_start": frame_start,
            "frame_end": frame_end,
            "frame_step": frame_step,
            "coordinate_space": coordinate_space,
            "units": units,
            "forward_axis": forward_axis,
            "up_axis": up_axis,
            "include_surface": include_surface,
            "include_secondary_particles": include_secondary_particles,
            "include_materials": include_materials,
            "include_velocity_attributes": include_velocity_attributes,
            "overwrite": overwrite,
            "max_frames": max_frames,
        },
        changed_objects=[domain_object_name],
    )


@mcp.tool()
async def analyze_liquid_performance(
    ctx: Context,
    domain_object_name: str,
    modifier_name: str,
    frames: list[int] | None = None,
    measure_replay_evaluation: bool = False,
    timeout_seconds: Annotated[float, Field(gt=0.0, le=300.0)] = 30.0,
    max_dependency_objects: Annotated[int, Field(ge=1, le=1_000)] = 200,
    max_cache_entries: Annotated[int, Field(ge=1, le=100_000)] = 10_000,
) -> dict:
    """Report bounded structural cost evidence and optional measured replay frame-evaluation timings."""
    return await call_blender(
        "analyze_liquid_performance",
        {
            "domain_object_name": domain_object_name,
            "modifier_name": modifier_name,
            "frames": frames or [],
            "measure_replay_evaluation": measure_replay_evaluation,
            "timeout_seconds": timeout_seconds,
            "max_dependency_objects": max_dependency_objects,
            "max_cache_entries": max_cache_entries,
        },
        changed_objects=[domain_object_name] if measure_replay_evaluation else [],
    )
