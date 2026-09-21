"""One merged tool patching any combination of cloth-solver concerns."""

from collections.abc import Callable
from typing import Annotated, Any

from mcp.server.fastmcp import Context
from pydantic import Field, model_validator

from ...app import mcp
from .._dispatch import call_blender
from ..envelope import ok
from ._shared import _dump, _StrictModel
from .collisions import ClothColliderPatch, ClothCollisionPatch
from .dynamics import (
    ClothFieldWeightsPatch,
    ClothInternalSpringsPatch,
    ClothPressurePatch,
    SewingPair,
)
from .material_and_solver import ClothMaterialPatch, ClothSolverPatch, MaterialPreset
from .pinning import ClothPinningPatch


class ClothMaterialSection(_StrictModel):
    """Everything ``configure_cloth_material`` accepted: an optional preset plus patch."""

    patch: ClothMaterialPatch | None = None
    preset: MaterialPreset | None = None


class ClothPinningSection(_StrictModel):
    """Everything ``configure_cloth_pinning`` accepted: the pin group and its goal patch."""

    group_name: Annotated[str, Field(min_length=1)]
    patch: ClothPinningPatch


class ClothSewingSection(_StrictModel):
    """Everything ``configure_cloth_sewing`` accepted, dry-run-first by default."""

    seam_pairs: Annotated[list[SewingPair], Field(min_length=1)]
    sewing_force_max: Annotated[float, Field(gt=0)]
    create_missing_edges: bool = False
    dry_run: bool = True
    max_pair_distance: Annotated[float, Field(gt=0)] | None = None


class ClothInternalSpringsSection(_StrictModel):
    """Everything ``configure_cloth_internal_springs`` accepted, including its cost bound."""

    patch: ClothInternalSpringsPatch
    max_estimated_springs: Annotated[int, Field(ge=1)] = 2_000_000


class ClothRestShapeSection(_StrictModel):
    """Everything ``configure_cloth_rest_shape`` accepted; every field was required before."""

    shape_key_name: str
    use_dynamic_mesh: bool
    cache_frame_start: Annotated[int, Field(ge=0)]
    cache_frame_end: Annotated[int, Field(ge=0)]


def _solver_call(object_name: str, modifier_name: str, patch: ClothSolverPatch) -> tuple[str, dict, list[str]]:
    return (
        "configure_cloth_solver",
        {"object_name": object_name, "modifier_name": modifier_name, "patch": _dump(patch)},
        [object_name],
    )


def _material_call(object_name: str, modifier_name: str, section: ClothMaterialSection) -> tuple[str, dict, list[str]]:
    return (
        "configure_cloth_material",
        {
            "object_name": object_name,
            "modifier_name": modifier_name,
            "patch": _dump(section.patch),
            "preset": section.preset,
        },
        [object_name],
    )


def _pinning_call(object_name: str, modifier_name: str, section: ClothPinningSection) -> tuple[str, dict, list[str]]:
    return (
        "configure_cloth_pinning",
        {
            "object_name": object_name,
            "modifier_name": modifier_name,
            "group_name": section.group_name,
            "patch": _dump(section.patch),
        },
        [object_name],
    )


def _collisions_call(object_name: str, modifier_name: str, patch: ClothCollisionPatch) -> tuple[str, dict, list[str]]:
    return (
        "configure_cloth_collisions",
        {"object_name": object_name, "modifier_name": modifier_name, "patch": _dump(patch)},
        [object_name],
    )


def _collider_call(object_name: str, modifier_name: str, patch: ClothColliderPatch) -> tuple[str, dict, list[str]]:
    return (
        "configure_cloth_collider",
        {"object_name": object_name, "modifier_name": modifier_name, "patch": _dump(patch)},
        [object_name],
    )


def _sewing_call(object_name: str, modifier_name: str, section: ClothSewingSection) -> tuple[str, dict, list[str]]:
    return (
        "configure_cloth_sewing",
        {
            "object_name": object_name,
            "modifier_name": modifier_name,
            "seam_pairs": [item.model_dump() for item in section.seam_pairs],
            "sewing_force_max": section.sewing_force_max,
            "create_missing_edges": section.create_missing_edges,
            "dry_run": section.dry_run,
            "max_pair_distance": section.max_pair_distance,
        },
        [] if section.dry_run else [object_name],
    )


def _pressure_call(object_name: str, modifier_name: str, patch: ClothPressurePatch) -> tuple[str, dict, list[str]]:
    return (
        "configure_cloth_pressure",
        {"object_name": object_name, "modifier_name": modifier_name, "patch": _dump(patch)},
        [object_name],
    )


def _internal_springs_call(
    object_name: str, modifier_name: str, section: ClothInternalSpringsSection
) -> tuple[str, dict, list[str]]:
    return (
        "configure_cloth_internal_springs",
        {
            "object_name": object_name,
            "modifier_name": modifier_name,
            "patch": _dump(section.patch),
            "max_estimated_springs": section.max_estimated_springs,
        },
        [object_name],
    )


def _rest_shape_call(
    object_name: str, modifier_name: str, section: ClothRestShapeSection
) -> tuple[str, dict, list[str]]:
    return (
        "configure_cloth_rest_shape",
        {
            "object_name": object_name,
            "modifier_name": modifier_name,
            "shape_key_name": section.shape_key_name,
            "use_dynamic_mesh": section.use_dynamic_mesh,
            "cache_frame_start": section.cache_frame_start,
            "cache_frame_end": section.cache_frame_end,
        },
        [object_name],
    )


def _field_weights_call(
    object_name: str, modifier_name: str, patch: ClothFieldWeightsPatch
) -> tuple[str, dict, list[str]]:
    return (
        "configure_cloth_field_weights",
        {"object_name": object_name, "modifier_name": modifier_name, "patch": _dump(patch)},
        [object_name],
    )


_SECTION_BUILDERS: dict[str, Callable[[str, str, Any], tuple[str, dict, list[str]]]] = {
    "solver": _solver_call,
    "material": _material_call,
    "pinning": _pinning_call,
    "collisions": _collisions_call,
    "collider": _collider_call,
    "sewing": _sewing_call,
    "pressure": _pressure_call,
    "internal_springs": _internal_springs_call,
    "rest_shape": _rest_shape_call,
    "field_weights": _field_weights_call,
}


class ClothPatch(_StrictModel):
    """One optional section per cloth-solver concern; only populated sections are applied."""

    solver: ClothSolverPatch | None = None
    material: ClothMaterialSection | None = None
    pinning: ClothPinningSection | None = None
    collisions: ClothCollisionPatch | None = None
    collider: ClothColliderPatch | None = None
    sewing: ClothSewingSection | None = None
    pressure: ClothPressurePatch | None = None
    internal_springs: ClothInternalSpringsSection | None = None
    rest_shape: ClothRestShapeSection | None = None
    field_weights: ClothFieldWeightsPatch | None = None

    @model_validator(mode="after")
    def _require_one_section(self) -> "ClothPatch":
        if not any(getattr(self, name) is not None for name in _SECTION_BUILDERS):
            raise ValueError("patch must set at least one section")
        return self


@mcp.tool()
async def configure_cloth(
    ctx: Context,
    object_name: str,
    modifier_name: str,
    patch: ClothPatch,
) -> dict:
    """
    Patch any combination of cloth-solver concerns through one entry point.

    ``patch`` accepts one optional section per concern - solver, material, pinning,
    collisions, collider, sewing, pressure, internal_springs, rest_shape, and
    field_weights - and only populated sections are applied, in that fixed order, each
    through the exact RNA-patch command its dedicated tool used before this merge. A
    section's failure raises immediately and aborts every section after it; sections
    already applied are not rolled back. Each populated section's own diff is reported
    unchanged, nested in ``data`` under its section name; ``warnings``,
    ``changed_objects``, and ``changed_resources`` are the order-preserving union across
    every applied section.
    """
    data: dict[str, Any] = {}
    all_ok = True
    warnings: list[str] = []
    changed_objects: list[str] = []
    changed_resources: list[str] = []

    for name, build in _SECTION_BUILDERS.items():
        section = getattr(patch, name)
        if section is None:
            continue
        command, params, default_changed = build(object_name, modifier_name, section)
        section_result = await call_blender(command, params, changed_objects=default_changed)
        data[name] = section_result.get("data")
        all_ok = all_ok and bool(section_result.get("ok", True))
        warnings.extend(section_result.get("warnings") or [])
        for changed_name in section_result.get("changed_objects") or []:
            if changed_name not in changed_objects:
                changed_objects.append(changed_name)
        for resource_name in section_result.get("changed_resources") or []:
            if resource_name not in changed_resources:
                changed_resources.append(resource_name)

    return ok(
        data,
        success=all_ok,
        warnings=warnings,
        changed_objects=changed_objects,
        changed_resources=changed_resources,
    )
