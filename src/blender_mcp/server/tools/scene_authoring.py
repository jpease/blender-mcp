# Every tool takes `ctx` by convention so any of them can reach the FastMCP context without a
# signature change; none here uses it. Tool signatures are deliberately flat rather than
# wrapped in one nested object: a named top-level parameter is far easier for a model to fill
# correctly, and that legibility is worth the extra advertised schema it costs.
# ruff: file-ignore[too-many-arguments, too-many-positional-arguments, unused-function-argument]
# A tool docstring is wire payload sent to every client on every connection. `_documentation.py`
# strips a `Raises:` section and discards it (so it costs no advertised bytes) and folds a
# `Returns:` section into the shared envelope suffix it appends to every description (so it
# refines rather than duplicates). Neither is written for these three tools because they raise
# only the validation errors their summaries already state and return the unremarkable standard
# envelope documented in envelope.py; add a `Returns:` only when `data` carries a shape worth
# naming. The same file-ignore covers the `@model_validator` methods below, which pydantic
# calls rather than any caller who could act on a Raises: section; each states its rule in its
# one-line summary, while the module-level helpers they delegate to carry full Raises:.
# ruff: file-ignore[docstring-missing-exception, docstring-missing-returns]
# pydantic's discriminated-union and `Annotated` forms defeat pyright's call/type-form checks.
# pyright: reportCallIssue=false, reportInvalidTypeForm=false
"""
Scene authoring and destructive scene operations.

Split out of `scene.py` so a shot-assembly process does not advertise them. Geometry creation
carries ten geometry kinds across sixteen nested models - by far the heaviest schema in the
former core surface - that shot work never uses, and `reset_scene`/`remove_scene_objects` are
destructive operations that a shot surface should not offer at all. Everything here stays
reachable through the `scene-authoring` bundle.
"""

import asyncio

from typing import Annotated, Any, Literal

from mcp.server.fastmcp import Context
from pydantic import Field, model_validator

from ..app import mcp
from ._scene_shared import _call, _StrictModel


def _require_one_value_per(name: str, values: list | None, expected: int, domain: str) -> None:
    """
    Validate that an optional parallel array carries one entry per domain element.

    Args:
        name: Field name, used verbatim in the error message.
        values: The optional array to check. `None` means "not supplied" and always passes.
        expected: Number of elements in the domain the array runs parallel to.
        domain: Singular noun for one domain element, e.g. `"point"` or `"stroke point"`.

    Raises:
        ValueError: If `values` is supplied and its length is not `expected`.

    """
    if values is not None and len(values) != expected:
        raise ValueError(f"{name} must contain one value per {domain}")


class MeshGeometry(_StrictModel):
    """Declarative mesh topology in object-local coordinates."""

    kind: Literal["MESH"] = "MESH"
    vertices: Annotated[list[tuple[float, float, float]], Field(max_length=1_000_000)]
    edges: Annotated[list[tuple[int, int]], Field(max_length=2_000_000)] = Field(default_factory=list)
    faces: Annotated[list[list[int]], Field(max_length=1_000_000)] = Field(default_factory=list)


class CurvePoint(_StrictModel):
    """One editable legacy Curve/Surface control point."""

    co: tuple[float, float, float]
    radius: Annotated[float, Field(ge=0)] = 1.0
    tilt: float = 0.0
    weight: Annotated[float, Field(ge=0)] = 1.0
    handle_left: tuple[float, float, float] | None = None
    handle_right: tuple[float, float, float] | None = None
    handle_left_type: Literal["FREE", "VECTOR", "ALIGNED", "AUTO"] = "AUTO"
    handle_right_type: Literal["FREE", "VECTOR", "ALIGNED", "AUTO"] = "AUTO"


class SplineRecord(_StrictModel):
    """One curve or surface spline and its interpolation settings."""

    type: Literal["POLY", "BEZIER", "NURBS"] = "POLY"
    points: Annotated[list[tuple[float, float, float] | CurvePoint], Field(min_length=1, max_length=100_000)]
    cyclic: bool = False
    order_u: Annotated[int, Field(ge=2, le=64)] = 4
    endpoint: bool = True
    point_count_u: Annotated[int | None, Field(ge=1, le=100_000)] = None
    point_count_v: Annotated[int, Field(ge=1, le=100_000)] = 1
    order_v: Annotated[int, Field(ge=2, le=64)] = 4
    endpoint_v: bool = True
    cyclic_v: bool = False

    @model_validator(mode="after")
    def validate_surface_shape(self) -> "SplineRecord":
        """Require an explicit rectangular U/V shape when a V dimension is used."""
        count_u = self.point_count_u or len(self.points)
        if count_u * self.point_count_v != len(self.points):
            raise ValueError("point_count_u * point_count_v must equal the number of points")
        return self


class SplineGeometry(_StrictModel):
    """Curve or surface data composed from one or more splines."""

    kind: Literal["CURVE", "SURFACE"]
    dimensions: Literal["2D", "3D"] = "3D"
    splines: Annotated[list[SplineRecord], Field(min_length=1, max_length=10_000)]
    resolution_u: Annotated[int, Field(ge=1, le=1024)] = 12
    bevel_depth: Annotated[float, Field(ge=0)] = 0.0
    bevel_resolution: Annotated[int, Field(ge=0, le=32)] = 4
    extrude: Annotated[float, Field(ge=0)] = 0.0


class TextGeometry(_StrictModel):
    """Editable Blender font geometry."""

    kind: Literal["TEXT"] = "TEXT"
    body: Annotated[str, Field(max_length=100_000)]
    size: Annotated[float, Field(gt=0)] = 1.0
    extrude: Annotated[float, Field(ge=0)] = 0.0
    bevel_depth: Annotated[float, Field(ge=0)] = 0.0
    align_x: Literal["LEFT", "CENTER", "RIGHT", "JUSTIFY", "FLUSH"] = "LEFT"
    align_y: Literal["TOP_BASELINE", "TOP", "CENTER", "BOTTOM", "BOTTOM_BASELINE"] = "TOP_BASELINE"


class MetaElement(_StrictModel):
    """One metaball family element."""

    co: tuple[float, float, float]
    radius: Annotated[float, Field(gt=0)] = 1.0
    stiffness: Annotated[float, Field(ge=0, le=10)] = 2.0
    type: Literal["BALL", "CAPSULE", "PLANE", "ELLIPSOID", "CUBE"] = "BALL"


class MetaGeometry(_StrictModel):
    """Editable metaball data containing explicit elements."""

    kind: Literal["META"] = "META"
    elements: Annotated[list[MetaElement], Field(min_length=1, max_length=10_000)]
    resolution: Annotated[float, Field(gt=0)] = 0.4
    render_resolution: Annotated[float, Field(gt=0)] = 0.2
    threshold: Annotated[float, Field(gt=0)] = 0.6


class LatticeGeometry(_StrictModel):
    """Lattice resolution specification."""

    kind: Literal["LATTICE"] = "LATTICE"
    points_u: Annotated[int, Field(ge=2, le=64)] = 2
    points_v: Annotated[int, Field(ge=2, le=64)] = 2
    points_w: Annotated[int, Field(ge=2, le=64)] = 2


class PointCloudGeometry(_StrictModel):
    """Native point-cloud positions and optional point radii."""

    kind: Literal["POINTCLOUD"] = "POINTCLOUD"
    points: Annotated[list[tuple[float, float, float]], Field(max_length=2_000_000)]
    radii: list[float] | None = None

    @model_validator(mode="after")
    def validate_radii(self) -> "PointCloudGeometry":
        """Require one non-negative radius per point when supplied; finiteness comes from _StrictModel."""
        _require_one_value_per("radii", self.radii, len(self.points), "point")
        if self.radii is not None and any(radius < 0 for radius in self.radii):
            raise ValueError("radii must be non-negative")
        return self


class GeometryAttribute(_StrictModel):
    """One native geometry attribute with values in domain order."""

    name: Annotated[str, Field(min_length=1, max_length=128)]
    data_type: Literal["FLOAT", "INT", "BOOLEAN", "FLOAT_VECTOR", "FLOAT_COLOR", "BYTE_COLOR"]
    domain: Literal["POINT", "CURVE", "STROKE", "LAYER"]
    values: Annotated[list[Any], Field(max_length=2_000_000)]


def _validate_attributes(attributes: list[GeometryAttribute], counts: dict[str, int], label: str) -> None:
    """
    Validate attribute domains and value counts against the domains a geometry supports.

    `GeometryAttribute` permits more domains than any single geometry type accepts, so each
    geometry declares the domains it supports and their sizes here. Keying the size lookup on
    the same mapping that defines the allowlist keeps the two rules from drifting apart.

    Args:
        attributes: Attributes to validate.
        counts: Supported domain name to the number of elements in that domain, in the
            order the domains should be listed if the allowlist check fails.
        label: Geometry name used to open the allowlist error, e.g. `"Curves"`.

    Raises:
        ValueError: If an attribute uses an unsupported domain, or carries a number of
            values that does not match the size of its domain.

    """
    unsupported = sorted({attribute.domain for attribute in attributes} - counts.keys())
    if unsupported:
        raise ValueError(f"{label} attributes only support {' or '.join(counts)} domains: {unsupported}")
    for attribute in attributes:
        expected = counts[attribute.domain]
        if len(attribute.values) != expected:
            raise ValueError(
                f"Attribute '{attribute.name}' on {attribute.domain} requires {expected} values, "
                f"received {len(attribute.values)}"
            )


class CurvesGeometry(_StrictModel):
    """Modern Curves/hair geometry with per-curve point counts and attributes."""

    kind: Literal["CURVES"] = "CURVES"
    points: Annotated[list[tuple[float, float, float]], Field(max_length=2_000_000)]
    curve_sizes: Annotated[list[int], Field(min_length=1, max_length=1_000_000)]
    cyclic: list[bool] | None = None
    surface_object_name: str | None = None
    attributes: Annotated[list[GeometryAttribute], Field(max_length=256)] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_topology(self) -> "CurvesGeometry":
        """Ensure curve offsets and optional values match their domains."""
        if any(size < 1 for size in self.curve_sizes) or sum(self.curve_sizes) != len(self.points):
            raise ValueError("curve_sizes must be positive and sum to the number of points")
        _require_one_value_per("cyclic", self.cyclic, len(self.curve_sizes), "curve")
        _validate_attributes(
            self.attributes,
            {"POINT": len(self.points), "CURVE": len(self.curve_sizes)},
            "Curves",
        )
        return self


class GreasePencilStroke(_StrictModel):
    """One editable Grease Pencil stroke."""

    points: Annotated[list[tuple[float, float, float]], Field(min_length=1, max_length=100_000)]
    cyclic: bool = False
    radii: list[float] | None = None
    opacities: list[Annotated[float, Field(ge=0, le=1)]] | None = None

    @model_validator(mode="after")
    def validate_point_data(self) -> "GreasePencilStroke":
        """Require point-domain arrays to match the stroke's point count."""
        for name, values in (("radii", self.radii), ("opacities", self.opacities)):
            _require_one_value_per(name, values, len(self.points), "stroke point")
        return self


class GreasePencilFrame(_StrictModel):
    """One Grease Pencil drawing at an integer frame."""

    frame_number: int
    strokes: Annotated[list[GreasePencilStroke], Field(max_length=100_000)] = Field(default_factory=list)
    attributes: Annotated[list[GeometryAttribute], Field(max_length=256)] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_attributes(self) -> "GreasePencilFrame":
        """Validate drawing-local point and stroke attribute lengths."""
        _validate_attributes(
            self.attributes,
            {
                "POINT": sum(len(stroke.points) for stroke in self.strokes),
                "STROKE": len(self.strokes),
            },
            "Grease Pencil drawing",
        )
        return self


class GreasePencilLayer(_StrictModel):
    """One named Grease Pencil layer and its drawings."""

    name: Annotated[str, Field(min_length=1, max_length=128)]
    frames: Annotated[list[GreasePencilFrame], Field(max_length=100_000)] = Field(default_factory=list)


class GreasePencilGeometry(_StrictModel):
    """Blender 5.x Grease Pencil layers, frames, strokes, and attributes."""

    kind: Literal["GREASEPENCIL"] = "GREASEPENCIL"
    layers: Annotated[list[GreasePencilLayer], Field(min_length=1, max_length=10_000)]


class VolumeGeometry(_StrictModel):
    """OpenVDB-backed volume data."""

    kind: Literal["VOLUME"] = "VOLUME"
    filepath: Annotated[str, Field(min_length=1)]
    is_sequence: bool = False
    frame_start: int = 1
    frame_duration: Annotated[int, Field(ge=1)] = 1


GeometrySpec = Annotated[
    MeshGeometry
    | SplineGeometry
    | TextGeometry
    | MetaGeometry
    | LatticeGeometry
    | PointCloudGeometry
    | CurvesGeometry
    | GreasePencilGeometry
    | VolumeGeometry,
    Field(discriminator="kind"),
]


class ManagedRigSelector(_StrictModel):
    """Select MCP-owned objects by a known rig ownership tag."""

    system: Literal["CAMERA", "RIGID_BODY"]
    rig_id: Annotated[str, Field(min_length=1, max_length=256)]


@mcp.tool()
async def create_geometry_object(
    ctx: Context,
    name: str,
    geometry: GeometrySpec,
    collection_name: str | None = None,
    location: tuple[float, float, float] = (0.0, 0.0, 0.0),
    rotation: tuple[float, float, float] = (0.0, 0.0, 0.0),
    scale: tuple[float, float, float] = (1.0, 1.0, 1.0),
) -> dict:
    """
    Create one native Blender geometry object from a validated declarative spec.

    Transforms are parent-local; rotation is XYZ Euler radians; scale components must be
    non-zero. name must not already be in use. A named collection_name is created if it does
    not exist; omit it to use the active collection.
    """
    return await asyncio.to_thread(
        _call,
        "create_geometry_object",
        {
            "name": name,
            "geometry": geometry.model_dump(),
            "collection_name": collection_name,
            "location": location,
            "rotation": rotation,
            "scale": scale,
        },
    )


@mcp.tool()
async def remove_scene_objects(
    ctx: Context,
    object_names: Annotated[list[str], Field(min_length=1, max_length=1_000)] | None = None,
    managed_rig: ManagedRigSelector | None = None,
    confirm_remove: bool = False,
) -> dict:
    """Remove scene objects given by exactly one of object_names or managed_rig; requires confirm_remove=True."""
    if not confirm_remove:
        raise ValueError("confirm_remove=True is required")
    if (object_names is None) == (managed_rig is None):
        raise ValueError("Provide exactly one of object_names or managed_rig")
    return await asyncio.to_thread(
        _call,
        "remove_scene_objects",
        {
            "object_names": object_names,
            "managed_rig": managed_rig.model_dump() if managed_rig else None,
            "confirm_remove": confirm_remove,
        },
        object_names or [],
    )


@mcp.tool()
async def reset_scene(
    ctx: Context,
    confirm_reset: bool = False,
    scene_name: str | None = None,
    purge_orphaned_data: bool = True,
) -> dict:
    """
    Clear a scene to an empty state; requires confirm_reset=True.

    Clears the scene named by scene_name, or the active scene when omitted. Unless
    purge_orphaned_data=False, also purges every orphaned local datablock in the whole
    file, not only the ones this scene released.
    """
    if not confirm_reset:
        raise ValueError("confirm_reset=True is required")
    return await asyncio.to_thread(
        _call,
        "reset_scene",
        {
            "confirm_reset": confirm_reset,
            "scene_name": scene_name,
            "purge_orphaned_data": purge_orphaned_data,
        },
    )
