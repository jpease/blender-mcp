"""Cloth material and solver patch models, shared by configure_cloth and setup tools."""

from typing import Annotated, Literal

from pydantic import Field

from .._inputs import StrictModel

MaterialPreset = Literal["COTTON", "SILK", "DENIM", "LEATHER", "RUBBER"]


class ClothMaterialPatch(StrictModel):
    """Allowlisted Blender 5.1 cloth material properties."""

    mass: Annotated[float, Field(gt=0)] | None = None
    air_damping: Annotated[float, Field(ge=0)] | None = None
    bending_model: Literal["ANGULAR", "LINEAR"] | None = None
    tension_stiffness: Annotated[float, Field(ge=0)] | None = None
    tension_stiffness_max: Annotated[float, Field(ge=0)] | None = None
    compression_stiffness: Annotated[float, Field(ge=0)] | None = None
    compression_stiffness_max: Annotated[float, Field(ge=0)] | None = None
    shear_stiffness: Annotated[float, Field(ge=0)] | None = None
    shear_stiffness_max: Annotated[float, Field(ge=0)] | None = None
    bending_stiffness: Annotated[float, Field(ge=0)] | None = None
    bending_stiffness_max: Annotated[float, Field(ge=0)] | None = None
    tension_damping: Annotated[float, Field(ge=0)] | None = None
    compression_damping: Annotated[float, Field(ge=0)] | None = None
    shear_damping: Annotated[float, Field(ge=0)] | None = None
    bending_damping: Annotated[float, Field(ge=0)] | None = None


class ClothSolverPatch(StrictModel):
    """Allowlisted solver controls, deliberately excluding material and collision settings."""

    quality: Annotated[int, Field(ge=1)] | None = None
    time_scale: Annotated[float, Field(ge=0)] | None = None
    gravity: tuple[float, float, float] | None = None
    voxel_cell_size: Annotated[float, Field(gt=0)] | None = None
