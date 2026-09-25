"""Task-level builders for common reusable procedural systems."""

from typing import Any, Literal

from mcp.server.fastmcp import Context

from ...app import mcp
from .._dispatch import call_blender


async def _build(command: str, params: dict[str, Any], object_name: str, group_name: str) -> dict:
    return await call_blender(command, params, changed_objects=[object_name], changed_resources=[group_name])


def _without_context(values: dict[str, Any]) -> dict[str, Any]:
    """Remove FastMCP's injected context from a Blender command payload."""
    return {key: value for key, value in values.items() if key != "ctx"}


@mcp.tool()
async def create_procedural_scatter(
    ctx: Context,
    object_name: str,
    group_name: str,
    source_type: Literal["OBJECT", "COLLECTION"],
    source_name: str | None = None,
    distribution: Literal["SURFACE_RANDOM", "SURFACE_POISSON", "VOLUME"] = "SURFACE_RANDOM",
    density: float = 10.0,
    distance_min: float = 0.1,
    seed: int = 0,
    scale_min: float = 1.0,
    scale_max: float = 1.0,
    mask_attribute: str | None = None,
    include_original: bool = True,
    realize_instances: bool = False,
    output_type: Literal["INSTANCES", "POINTS", "HAIR_CURVES"] = "INSTANCES",
    density_attribute: str | None = "mcp_scatter_density",
    selection_attribute: str | None = "mcp_scatter_selection",
    orientation: Literal["NORMAL", "RANDOM", "NONE"] = "NORMAL",
    orientation_offset: tuple[float, float, float] = (0.0, 0.0, 0.0),
    guide_length: float = 1.0,
    source_collection_policy: Literal["WHOLE_COLLECTION", "PICK_INSTANCE", "SEPARATE_CHILDREN"] = "PICK_INSTANCE",
) -> dict:
    """
    Build and attach a deterministic surface or volume instance scatter system.

    Instances remain unrealized by default. Source object/collection, density or minimum distance,
    mask, seed, scale range, original-geometry passthrough, and realization are exposed as controls.
    Each instance is the source's own shape; where the source sits in the scene does not move it.
    """
    if density < 0 or distance_min <= 0 or scale_min < 0 or scale_max < scale_min or guide_length <= 0:
        raise ValueError("Require density >= 0, distance_min > 0, and 0 <= scale_min <= scale_max")
    if output_type == "INSTANCES" and not source_name:
        raise ValueError("source_name is required for INSTANCES output")
    return await _build("create_procedural_scatter", _without_context(locals()), object_name, group_name)


@mcp.tool()
async def create_curve_generator(
    ctx: Context,
    object_name: str,
    group_name: str,
    curve_object_name: str | None = None,
    profile_object_name: str | None = None,
    radius: float = 0.05,
    resolution: int = 32,
    trim_start: float = 0.0,
    trim_end: float = 1.0,
    fill_caps: bool = True,
    material_name: str | None = None,
) -> dict:
    """
    Build and attach an editable curve-to-mesh generator for cables, pipes, rails, or trims.

    The source curve stays editable. Radius, resampling, trim range, profile, cap policy, and material
    are explicit. The mesh follows the path curve where it sits in the world, whatever the modifier
    object's transform; a profile curve is read in its own local space, so only its shape matters,
    and is scaled by radius.
    """
    if radius <= 0 or resolution < 2 or not (0 <= trim_start <= trim_end <= 1):
        raise ValueError("Require radius > 0, resolution >= 2, and 0 <= trim_start <= trim_end <= 1")
    return await _build("create_curve_generator", _without_context(locals()), object_name, group_name)


@mcp.tool()
async def create_procedural_array(
    ctx: Context,
    object_name: str,
    group_name: str,
    source_name: str,
    layout: Literal["LINEAR", "GRID", "RADIAL", "CURVE"],
    count: int = 5,
    count_y: int = 1,
    spacing: tuple[float, float, float] = (1.0, 0.0, 0.0),
    angular_span: float = 6.283185307179586,
    endpoint_policy: Literal["EXCLUDE_END", "INCLUDE_BOTH"] = "EXCLUDE_END",
    pivot_object_name: str | None = None,
    curve_object_name: str | None = None,
    realize_instances: bool = False,
) -> dict:
    """
    Build and attach an instanced linear, grid, radial, or curve-following array.

    Use the existing Array modifier tool for ordinary one-axis mesh repetition. This builder is for
    multi-axis layouts, explicit pivots, curve orientation, and preserved instances. The pivot's world
    location is the radial centre, and a CURVE layout follows its curve where it sits in the world;
    each copy is the source's own shape, wherever the source sits.
    """
    if count < 1 or count_y < 1:
        raise ValueError("count and count_y must be at least 1")
    return await _build("create_procedural_array", _without_context(locals()), object_name, group_name)


@mcp.tool()
async def create_surface_paneling(
    ctx: Context,
    object_name: str,
    group_name: str,
    source_collection_name: str | None = None,
    panel_scale: float = 0.9,
    depth: float = 0.05,
    normal_offset: float = 0.0,
    seed: int = 0,
    mask_attribute: str | None = None,
    realize_instances: bool = False,
) -> dict:
    """
    Build and attach editable panels, tiles, shingles, facade units, scales, or greebles.

    The source mesh is not destructively subdivided or voxelized. Panel IDs are stored as a named
    attribute, and estimated output growth is returned for production review.
    """
    if panel_scale <= 0 or depth < 0:
        raise ValueError("panel_scale must be positive and depth must be non-negative")
    return await _build("create_surface_paneling", _without_context(locals()), object_name, group_name)


@mcp.tool()
async def create_procedural_boolean(
    ctx: Context,
    object_name: str,
    group_name: str,
    cutter_source: Literal["OBJECT", "COLLECTION"],
    cutter_name: str,
    operation: Literal["DIFFERENCE", "UNION", "INTERSECT"] = "DIFFERENCE",
    solver: Literal["FLOAT", "EXACT", "MANIFOLD"] = "EXACT",
    include_cutters: bool = False,
) -> dict:
    """
    Build and attach a live multi-cutter Boolean system without deleting cutter objects.

    Cutter dependencies stay explicit and editable. Each cutter cuts where it sits in the world, so
    moving it moves the cut. Collection instances are realized only on the cutter branch required by
    the Boolean node; the target object's base mesh remains unchanged.
    """
    return await _build("create_procedural_boolean", _without_context(locals()), object_name, group_name)


@mcp.tool()
async def create_procedural_deformer(
    ctx: Context,
    object_name: str,
    group_name: str,
    template: Literal["NOISE_DISPLACEMENT", "TAPER", "TWIST", "PROXIMITY_PUSH", "MASK_OFFSET"],
    strength: float = 0.1,
    scale: float = 1.0,
    axis: Literal["X", "Y", "Z"] = "Z",
    coordinate_space: Literal["OBJECT", "WORLD"] = "OBJECT",
    seed: int = 0,
    target_object_name: str | None = None,
    mask_attribute: str | None = None,
) -> dict:
    """
    Build and attach a reusable field-based deformation template.

    The result documents object-versus-world-space behavior and keeps strength, scale, axis, seed,
    target, and mask contracts exposed instead of relying on a legacy Texture datablock.
    PROXIMITY_PUSH measures distance to the target where it sits in the world.
    """
    if scale <= 0:
        raise ValueError("scale must be positive")
    return await _build("create_procedural_deformer", _without_context(locals()), object_name, group_name)


@mcp.tool()
async def create_volume_generator(
    ctx: Context,
    object_name: str,
    group_name: str,
    source: Literal["MESH", "POINTS", "CUBE"] = "MESH",
    output_type: Literal["VOLUME", "MESH"] = "VOLUME",
    density: float = 1.0,
    voxel_size: float = 0.1,
    radius: float = 0.5,
    threshold: float = 0.1,
    material_name: str | None = None,
    density_grid_name: str = "density",
    delivery: Literal["LIVE_GRAPH", "OPENVDB"] = "LIVE_GRAPH",
    output_path: str | None = None,
    confirm_write: bool = False,
    confirm_overwrite: bool = False,
) -> dict:
    """
    Build and attach a bounded static volume or fog-source graph after runtime capability checks.

    This creates procedural volume geometry, not a fluid simulation. Voxel size and estimated memory
    risk are reported so an agent can avoid accidentally requesting an impractical resolution. OPENVDB
    delivery writes through Blender's bundled OpenVDB module and creates a native file-backed Volume object;
    it does not claim that Blender's Volume RNA can author arbitrary grids.
    """
    if density < 0 or voxel_size <= 0 or radius <= 0:
        raise ValueError("Require density >= 0, voxel_size > 0, and radius > 0")
    if not density_grid_name.strip():
        raise ValueError("density_grid_name must be non-empty")
    if delivery == "OPENVDB" and (not output_path or not confirm_write):
        raise ValueError("OPENVDB delivery requires output_path and confirm_write=True")
    if delivery == "OPENVDB" and output_type != "VOLUME":
        raise ValueError("OPENVDB delivery requires output_type=VOLUME")
    if delivery == "LIVE_GRAPH" and output_path is not None:
        raise ValueError("output_path is only valid for OPENVDB delivery")
    return await _build("create_volume_generator", _without_context(locals()), object_name, group_name)
