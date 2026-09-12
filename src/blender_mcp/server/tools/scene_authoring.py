# pyright: reportCallIssue=false, reportInvalidTypeForm=false
# ruff: file-ignore[docstring-missing-exception, docstring-missing-returns, too-many-arguments, too-many-positional-arguments, unused-function-argument]
"""
Scene authoring and destructive scene operations.

Split out of `scene.py` so a shot-assembly process does not advertise them. Geometry
creation carries sixteen type variants (~25 KB of schema) that shot work never uses, and
`reset_scene`/`remove_scene_objects` are destructive operations that a shot surface should
not offer at all. Everything here stays reachable through the `scene-authoring` bundle.
"""

import asyncio

from typing import Annotated, Any, Literal

from mcp.server.fastmcp import Context
from pydantic import Field, model_validator

from ..app import mcp
from .scene import _call, _StrictModel


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
        """Require one finite, non-negative radius per point when supplied."""
        if self.radii is not None:
            if len(self.radii) != len(self.points):
                raise ValueError("radii must contain one value per point")
            if any(radius < 0 for radius in self.radii):
                raise ValueError("radii must be non-negative")
        return self


class GeometryAttribute(_StrictModel):
    """One native geometry attribute with values in domain order."""

    name: Annotated[str, Field(min_length=1, max_length=128)]
    data_type: Literal["FLOAT", "INT", "BOOLEAN", "FLOAT_VECTOR", "FLOAT_COLOR", "BYTE_COLOR"]
    domain: Literal["POINT", "CURVE", "STROKE", "LAYER"]
    values: Annotated[list[Any], Field(max_length=2_000_000)]


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
        if self.cyclic is not None and len(self.cyclic) != len(self.curve_sizes):
            raise ValueError("cyclic must contain one value per curve")
        invalid_domains = sorted({attribute.domain for attribute in self.attributes} - {"POINT", "CURVE"})
        if invalid_domains:
            raise ValueError(f"Curves attributes only support POINT or CURVE domains: {invalid_domains}")
        _validate_attribute_lengths(self.attributes, len(self.points), len(self.curve_sizes))
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
            if values is not None and len(values) != len(self.points):
                raise ValueError(f"{name} must contain one value per stroke point")
        return self


class GreasePencilFrame(_StrictModel):
    """One Grease Pencil drawing at an integer frame."""

    frame_number: int
    strokes: Annotated[list[GreasePencilStroke], Field(max_length=100_000)] = Field(default_factory=list)
    attributes: Annotated[list[GeometryAttribute], Field(max_length=256)] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_attributes(self) -> "GreasePencilFrame":
        """Validate drawing-local point and stroke attribute lengths."""
        invalid_domains = sorted({attribute.domain for attribute in self.attributes} - {"POINT", "STROKE"})
        if invalid_domains:
            raise ValueError(f"Grease Pencil drawing attributes only support POINT or STROKE: {invalid_domains}")
        _validate_attribute_lengths(
            self.attributes,
            sum(len(stroke.points) for stroke in self.strokes),
            len(self.strokes),
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


def _validate_attribute_lengths(attributes: list[GeometryAttribute], point_count: int, curve_count: int) -> None:
    for attribute in attributes:
        expected = point_count if attribute.domain == "POINT" else curve_count
        if len(attribute.values) != expected:
            raise ValueError(
                f"Attribute '{attribute.name}' on {attribute.domain} requires {expected} values, "
                f"received {len(attribute.values)}"
            )


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
    """Create one native Blender geometry object from a validated declarative specification."""
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
    """Remove only the named scene objects after dependency inspection and explicit confirmation."""
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
    """Clear one scene to an empty, deterministic starting state after explicit confirmation."""
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
