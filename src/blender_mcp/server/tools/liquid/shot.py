# The orchestrator's signature intentionally exposes every policy it forwards to its sub-tools.
# ruff: file-ignore[multi-line-summary-second-line]
"""One typed entry point that turns container/source intent into a complete liquid setup."""

from typing import Annotated, Literal

from mcp.server.fastmcp import Context
from pydantic import Field, model_validator

from ...app import mcp
from .._dispatch import call_blender
from .._inputs import StrictModel, dump_input
from .delivery import ProxyEffectorSettings
from .inspection_and_setup import (
    CacheType,
    EffectorType,
    FlowBehavior,
    LiquidFlowPatch,
)
from .quality import QualityProfile, profile_patches

CollisionProxy = Literal["NONE", "HOLLOW_CONTAINER"]
RimAxis = Literal["X", "Y", "Z", "NEGATIVE_X", "NEGATIVE_Y", "NEGATIVE_Z"]

_MAX_SHOT_OBJECTS = 16


class ShotContainer(StrictModel):
    """A vessel the liquid has to stay inside, plus how its collider should be built."""

    object_name: str
    collision_proxy: CollisionProxy = "NONE"
    effector_type: EffectorType = "COLLISION"
    effector_settings: ProxyEffectorSettings | None = None
    rim_axis: RimAxis = "Z"
    wall_thickness: Annotated[float, Field(gt=0.0, le=10.0)] = 0.05
    bottom_thickness: Annotated[float, Field(gt=0.0, le=10.0)] | None = None
    proxy_object_name: str | None = None

    @model_validator(mode="after")
    def validate_container(self) -> "ShotContainer":
        """Reject proxy-only fields on a container that will not get a proxy."""
        if self.collision_proxy == "NONE" and self.proxy_object_name is not None:
            raise ValueError("proxy_object_name requires collision_proxy=HOLLOW_CONTAINER")
        return self


class ShotSource(StrictModel):
    """An object that emits or removes liquid, optionally only during a window of shot time."""

    object_name: str
    behavior: FlowBehavior = "INFLOW"
    enabled_seconds: tuple[float, float] | None = None
    flow_settings: LiquidFlowPatch | None = None

    @model_validator(mode="after")
    def validate_source(self) -> "ShotSource":
        """Enforce the Blender semantics of Use Flow before the request reaches Blender."""
        if self.enabled_seconds is None:
            return self
        if self.behavior == "GEOMETRY":
            raise ValueError(
                "enabled_seconds requires behavior INFLOW or OUTFLOW; Blender's Use Flow toggle has "
                "no effect on a GEOMETRY flow"
            )
        start, end = self.enabled_seconds
        if not 0.0 <= start < end:
            raise ValueError("enabled_seconds must be an increasing, non-negative [on, off] pair")
        return self


@mcp.tool()
async def setup_liquid_shot(
    ctx: Context,
    scene_name: str,
    cache_directory: str,
    containers: Annotated[list[ShotContainer], Field(min_length=1, max_length=_MAX_SHOT_OBJECTS)],
    sources: Annotated[list[ShotSource], Field(min_length=1, max_length=_MAX_SHOT_OBJECTS)],
    domain_object_name: str | None = None,
    new_domain_name: str = "Liquid Domain",
    modifier_name: str = "Liquid Domain",
    collection_name: str | None = None,
    quality: QualityProfile = "BALANCED",
    cache_type: CacheType = "REPLAY",
    cache_frame_start: Annotated[int, Field(ge=0, le=1_048_574)] = 1,
    cache_frame_end: Annotated[int, Field(ge=1, le=1_048_574)] = 250,
    padding: tuple[float, float, float] = (0.25, 0.25, 0.25),
    expected_travel: tuple[float, float, float] = (0.0, 0.0, 0.0),
    splash_height: Annotated[float, Field(ge=0.0, le=1000.0)] = 0.0,
    create_validation_volumes: bool = True,
    spill_catch_depth: Annotated[float, Field(gt=0.0, le=1000.0)] | None = None,
    spill_catch_margin: Annotated[float, Field(gt=0.0, le=1000.0)] | None = None,
    dry_run: bool = False,
) -> dict:
    """Build a whole liquid shot - domain, colliders, timed sources, quality, measurement volumes - in one call.

    This composes the existing single-purpose tools inside one transaction; it introduces no new
    mutation path, so their rules still hold (the domain must be unbaked, the cache directory must be
    explicit and unshared, flows and effectors land in the domain's scoped collections). Steps run in
    order: create_liquid_domain, then per container either create_liquid_proxy_rig (HOLLOW_CONTAINER,
    which installs the collider on the proxy - the visible container keeps no fluid modifier) or
    add_liquid_effector, then per source add_liquid_flow plus animate_liquid_flow keying ``use_inflow``
    on and off at the frames ``enabled_seconds`` resolves to at the scene's fps, then
    fit_liquid_domain, then apply_liquid_quality_profile, then the validation volumes, then
    validate_liquid_setup over the finished shot.

    ``dry_run=True`` validates the whole request and returns the resolved plan without creating or
    modifying anything; structural findings from validate_liquid_setup are only included when
    ``domain_object_name`` names a domain that already exists.

    Validation volumes are axis-aligned boxes approximating each container's interior (and, with
    ``spill_catch_depth``, a catch region below the rim). They carry no fluid modifier, are hidden
    from renders, and exist so validate_liquid_result can measure fill and spill later - a fill
    fraction is relative to those boxes, not to exact hollow geometry.

    Nothing is baked: run manage_liquid_cache START_BAKE afterwards, then validate_liquid_result with
    the returned ``simulation_id``.
    """
    solver_patch, mesh_patch = profile_patches(quality)
    return await call_blender(
        "setup_liquid_shot",
        {
            "scene_name": scene_name,
            "cache_directory": cache_directory,
            "containers": [dump_input(container) for container in containers],
            "sources": [_shot_source_payload(source) for source in sources],
            "domain_object_name": domain_object_name,
            "new_domain_name": new_domain_name,
            "modifier_name": modifier_name,
            "collection_name": collection_name,
            "quality": quality,
            "solver_patch": solver_patch,
            "mesh_patch": mesh_patch,
            "cache_type": cache_type,
            "cache_frame_start": cache_frame_start,
            "cache_frame_end": cache_frame_end,
            "padding": list(padding),
            "expected_travel": list(expected_travel),
            "splash_height": splash_height,
            "create_validation_volumes": create_validation_volumes,
            "spill_catch_depth": spill_catch_depth,
            "spill_catch_margin": spill_catch_margin,
            "dry_run": dry_run,
        },
        changed_objects=None if dry_run else _changed_objects(containers, sources, domain_object_name),
    )


def _changed_objects(
    containers: list[ShotContainer], sources: list[ShotSource], domain_object_name: str | None
) -> list[str]:
    """Name every object the shot will touch, so the dispatcher's change tracking covers all of them."""
    names = [container.object_name for container in containers]
    names.extend(source.object_name for source in sources)
    if domain_object_name is not None:
        names.append(domain_object_name)
    return sorted(set(names))


def _shot_source_payload(source: ShotSource) -> dict:
    """Flatten one source record, keeping enabled_seconds JSON-serializable as a list."""
    payload = dump_input(source) or {}
    if source.enabled_seconds is not None:
        payload["enabled_seconds"] = list(source.enabled_seconds)
    return payload
