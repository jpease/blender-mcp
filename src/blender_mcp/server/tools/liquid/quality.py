"""Named liquid quality profiles applied through the existing solver and mesh tools."""

from typing import Literal

from mcp.server.fastmcp import Context

from ...app import mcp
from .._dispatch import call_blender
from .._inputs import dump_input
from .inspection_and_setup import LiquidSolverPatch
from .mesh_and_materials import LiquidMeshPatch

QualityProfile = Literal["PREVIEW", "BALANCED", "FINAL"]

# Each profile is only a documented bundle of fields for the existing configure_liquid_solver and
# configure_liquid_mesh patch models - applying one introduces no new mutation path. Building the
# models here rather than storing plain dicts means a profile that violates one of those models'
# bounds or cross-field rules fails at import instead of at call time. Values follow Blender's own
# Fluid defaults as the BALANCED baseline: PREVIEW trades resolution and surface refinement for
# iteration speed, FINAL raises both plus adaptive-timestep headroom.
QUALITY_PROFILES: dict[str, tuple[LiquidSolverPatch, LiquidMeshPatch]] = {
    "PREVIEW": (
        LiquidSolverPatch(
            resolution_max=48,
            use_adaptive_timesteps=True,
            timesteps_min=1,
            timesteps_max=4,
            cfl_condition=4.0,
            particle_radius=1.0,
            use_fractions=False,
        ),
        LiquidMeshPatch(
            use_mesh=True,
            mesh_scale=1,
            mesh_particle_radius=2.0,
            mesh_smoothen_pos=1,
            mesh_smoothen_neg=1,
            mesh_generator="IMPROVED",
            use_speed_vectors=False,
        ),
    ),
    "BALANCED": (
        LiquidSolverPatch(
            resolution_max=96,
            use_adaptive_timesteps=True,
            timesteps_min=1,
            timesteps_max=4,
            cfl_condition=4.0,
            particle_radius=1.0,
            use_fractions=True,
            fractions_threshold=0.05,
        ),
        LiquidMeshPatch(
            use_mesh=True,
            mesh_scale=2,
            mesh_particle_radius=2.0,
            mesh_smoothen_pos=1,
            mesh_smoothen_neg=1,
            mesh_generator="IMPROVED",
            use_speed_vectors=False,
        ),
    ),
    "FINAL": (
        LiquidSolverPatch(
            resolution_max=192,
            use_adaptive_timesteps=True,
            timesteps_min=1,
            timesteps_max=8,
            cfl_condition=2.0,
            particle_radius=1.5,
            use_fractions=True,
            fractions_threshold=0.05,
        ),
        LiquidMeshPatch(
            use_mesh=True,
            mesh_scale=2,
            mesh_particle_radius=1.5,
            mesh_smoothen_pos=2,
            mesh_smoothen_neg=2,
            mesh_generator="IMPROVED",
            use_speed_vectors=True,
        ),
    ),
}


def profile_patches(profile: str) -> tuple[dict, dict]:
    """
    Return the (solver, mesh) patch payload pair for a named profile.

    The bundles are the same LiquidSolverPatch/LiquidMeshPatch models the standalone tools accept, so
    a profile cannot smuggle in a field or cross-field combination those tools would reject.

    Raises:
        ValueError: if the profile name is not one of QUALITY_PROFILES.

    """
    try:
        solver, mesh = QUALITY_PROFILES[profile]
    except KeyError:
        raise ValueError(f"Unknown quality profile: {profile}; choose one of {sorted(QUALITY_PROFILES)}") from None
    solver_payload = dump_input(solver)
    mesh_payload = dump_input(mesh)
    assert solver_payload is not None and mesh_payload is not None
    return solver_payload, mesh_payload


@mcp.tool()
async def apply_liquid_quality_profile(
    ctx: Context,
    domain_object_name: str,
    modifier_name: str,
    profile: QualityProfile = "BALANCED",
    apply_solver: bool = True,
    apply_mesh: bool = True,
) -> dict:
    """
    Apply a named PREVIEW/BALANCED/FINAL preset through configure_liquid_solver and configure_liquid_mesh.

    This is a convenience wrapper over those two tools, not a separate mutation path: the same
    unbaked-domain and cache-stage rules apply, and the reported "changes" come from them unmodified.
    Raising quality invalidates cached DATA/MESH stages, so re-bake afterwards. At least one of
    apply_solver/apply_mesh must be true; use_speed_vectors in the FINAL profile requires a DATA
    re-bake, which configure_liquid_mesh reports through data_rebake_required.
    """
    if not apply_solver and not apply_mesh:
        raise ValueError("At least one of apply_solver or apply_mesh must be true")
    solver_patch, mesh_patch = profile_patches(profile)
    return await call_blender(
        "apply_liquid_quality_profile",
        {
            "domain_object_name": domain_object_name,
            "modifier_name": modifier_name,
            "profile": profile,
            "solver_patch": solver_patch if apply_solver else None,
            "mesh_patch": mesh_patch if apply_mesh else None,
        },
        changed_objects=[domain_object_name],
    )
