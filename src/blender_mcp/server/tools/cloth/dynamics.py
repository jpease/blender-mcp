"""
Cloth sewing, pressure, internal-spring, and field-weight patch models.

Shared by configure_cloth; SewingPair is also used directly by other cloth tools.
"""

from typing import Annotated

from pydantic import Field

from ._shared import _StrictModel


class SewingPair(_StrictModel):
    """One explicit cross-panel sewing spring between two base-mesh vertices."""

    source_vertex: int = Field(ge=0)
    target_vertex: int = Field(ge=0)


class ClothPressurePatch(_StrictModel):
    """Allowlisted Blender 5.1 pressure properties."""

    use_pressure: bool | None = None
    uniform_pressure_force: float | None = None
    use_pressure_volume: bool | None = None
    target_volume: Annotated[float, Field(ge=0)] | None = None
    pressure_factor: Annotated[float, Field(ge=0)] | None = None
    fluid_density: Annotated[float, Field(gt=0)] | None = None
    vertex_group_pressure: Annotated[str, Field(min_length=1)] | None = None


class ClothInternalSpringsPatch(_StrictModel):
    """Allowlisted Blender 5.1 internal-spring properties."""

    use_internal_springs: bool | None = None
    internal_spring_max_length: Annotated[float, Field(ge=0)] | None = None
    internal_spring_max_diversion: Annotated[float, Field(ge=0)] | None = None
    internal_spring_normal_check: bool | None = None
    internal_tension_stiffness: Annotated[float, Field(ge=0)] | None = None
    internal_compression_stiffness: Annotated[float, Field(ge=0)] | None = None
    internal_tension_stiffness_max: Annotated[float, Field(ge=0)] | None = None
    internal_compression_stiffness_max: Annotated[float, Field(ge=0)] | None = None
    internal_friction: Annotated[float, Field(ge=0)] | None = None
    vertex_group_intern: Annotated[str, Field(min_length=1)] | None = None


class ClothFieldWeightsPatch(_StrictModel):
    """Allowlisted Blender 5.1 EffectorWeights values and collection scope."""

    all: float | None = None
    gravity: float | None = None
    force: float | None = None
    vortex: float | None = None
    magnetic: float | None = None
    wind: float | None = None
    curve_guide: float | None = None
    texture: float | None = None
    harmonic: float | None = None
    charge: float | None = None
    lennardjones: float | None = None
    turbulence: float | None = None
    drag: float | None = None
    boid: float | None = None
    smokeflow: float | None = None
    apply_to_hair_growing: bool | None = None
    collection_name: Annotated[str, Field(min_length=1)] | None = None
    clear_collection: bool = False
