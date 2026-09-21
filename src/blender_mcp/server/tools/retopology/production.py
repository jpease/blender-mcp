"""Agent-facing tools for retopology-to-production handoff: data transfer, UVs, baking."""

from typing import Annotated, Literal

from mcp.server.fastmcp import Context
from pydantic import Field

from ...app import mcp
from .._dispatch import call_blender, send_blender_command
from ..envelope import envelope_for

VertexMapping = Literal[
    "TOPOLOGY",
    "NEAREST",
    "EDGE_NEAREST",
    "EDGEINTERP_NEAREST",
    "POLY_NEAREST",
    "POLYINTERP_NEAREST",
    "POLYINTERP_VNORPROJ",
]
DataTransferMixMode = Literal["REPLACE", "ABOVE_THRESHOLD", "BELOW_THRESHOLD", "MIX", "ADD", "SUB", "MUL"]


@mcp.tool()
async def transfer_mesh_attributes(
    ctx: Context,
    source_object_name: str,
    object_name: str,
    data_types: list[
        Literal[
            "VERTEX_GROUPS",
            "UVS",
            "COLOR_ATTRIBUTES",
            "CUSTOM_NORMALS",
            "SEAMS",
            "CREASES",
            "BEVEL_WEIGHTS",
            "SHARP_EDGES",
            "SMOOTH_SHADING",
            "MATERIAL_INDICES",
        ]
    ],
    modifier_name: str = "RetopologyDataTransfer",
    vertex_mapping: VertexMapping = "POLYINTERP_NEAREST",
    edge_mapping: str = "NEAREST",
    loop_mapping: str = "POLYINTERP_NEAREST",
    polygon_mapping: str = "NEAREST",
    use_object_transform: bool = True,
    max_distance: Annotated[float, Field(gt=0)] | None = None,
    source_layers: Literal["ACTIVE", "ALL"] = "ALL",
    destination_layers: Literal["NAME", "INDEX"] = "NAME",
    mix_mode: DataTransferMixMode = "REPLACE",
    mix_factor: Annotated[float, Field(ge=0, le=1)] = 1.0,
    apply: bool = False,
) -> dict:
    """
    Transfer named production data from a source mesh to new topology.

    Data Transfer supports vertex groups, UVs, colors, custom normals, seams,
    creases, bevel weights, sharp edges, and smooth shading. Mapping enum
    strings are passed to the corresponding Blender 5.1 vertex/edge/loop/face
    mapping property and invalid values are rejected by RNA before commit.
    `edge_mapping`, `loop_mapping`, and `polygon_mapping` accept Blender 5.1's
    `DataTransferModifier.edge_mapping`/`loop_mapping`/`poly_mapping` RNA enum
    identifiers (documented examples: edge_mapping `TOPOLOGY`/`VERT_NEAREST`/
    `NEAREST`/`POLY_NEAREST`/`EDGEINTERP_VNORPROJ`; loop_mapping `TOPOLOGY`/
    `NEAREST_NORMAL`/`NEAREST_POLYNOR`/`NEAREST_POLY`/`POLYINTERP_NEAREST`/
    `POLYINTERP_LNORPROJ`; polygon_mapping `TOPOLOGY`/`NEAREST`/`NORMAL_PROJECT`)
    — these three are left as plain strings pending human verification against
    the running Blender 5.1 RNA rather than a possibly-stale `Literal`.
    `use_object_transform=True` evaluates mapping in world space. `max_distance=None`
    disables the distance gate. The named modifier stays live by default;
    `apply=True` bakes all supported data types; connectivity and indices are retained.
    MATERIAL_INDICES is always mapped immediately from nearest evaluated source
    faces because Data Transfer does not support it; source material slots are
    added to the destination without deleting existing slots.
    """
    params = {key: value for key, value in locals().items() if key != "ctx"}
    return await call_blender("transfer_mesh_attributes", params, changed_objects=[object_name])


@mcp.tool()
async def unwrap_retopology_uvs(
    ctx: Context,
    object_name: str,
    uv_map_name: str = "RetopologyUV",
    method: Literal["ANGLE_BASED", "CONFORMAL", "MINIMUM_STRETCH"] = "ANGLE_BASED",
    replace_existing: bool = False,
    average_island_scale: bool = True,
    minimize_stretch_iterations: Annotated[int, Field(ge=0, le=1000)] = 10,
    pack_islands: bool = True,
    margin: Annotated[float, Field(ge=0, le=1)] = 0.001,
) -> dict:
    """
    Create and validate a seam-driven bake-ready UV map.

    All base-mesh faces are unwrapped using existing seam attributes; the tool
    does not invent seams. An existing map with `uv_map_name` is preserved and
    causes an error unless `replace_existing=True`. Optional island scaling,
    bounded stretch minimization, and packing are checked for FINISHED status.
    Returns island count, zero-area faces, overlap pairs, UVs outside [0,1],
    geometric/UV stretch statistics, and texel-density variation. Existing
    maps other than an explicitly replaced same-name map are untouched.
    """
    params = {key: value for key, value in locals().items() if key != "ctx"}
    return await call_blender("unwrap_retopology_uvs", params, changed_objects=[object_name])


@mcp.tool()
async def create_bake_cage(
    ctx: Context,
    object_name: str,
    high_poly_object_names: list[str],
    name: str | None = None,
    collection_name: str = "Retopology Bake Cages",
    offset: float = 0.02,
    vertex_group: str | None = None,
    validate_enclosure: bool = True,
) -> dict:
    """
    Create an editable, non-rendering cage with low-poly-identical topology.

    The base mesh is copied without modifiers, keeping vertex/edge/face indices
    identical. Cage vertices move in target-local averaged-normal directions by
    `offset`, multiplied by `vertex_group` weights when supplied. The cage uses
    the low-poly world transform, is placed in a dedicated collection, remains
    viewport-visible, and is hidden from renders. Validation reports topology
    identity, self-intersections, high-poly samples likely outside the cage,
    and bidirectional normal-ray misses; it does not silently alter the cage.
    """
    reply = await send_blender_command(
        "create_bake_cage",
        {
            "object_name": object_name,
            "high_poly_object_names": high_poly_object_names,
            "name": name,
            "collection_name": collection_name,
            "offset": offset,
            "vertex_group": vertex_group,
            "validate_enclosure": validate_enclosure,
        },
    )
    return envelope_for(reply, changed_objects=[reply["cage_object"]])


@mcp.tool()
async def bake_retopology_maps(
    ctx: Context,
    object_name: str,
    high_poly_object_names: list[str],
    map_type: Literal["NORMAL", "DISPLACEMENT", "AO", "POSITION", "DIFFUSE", "ROUGHNESS", "EMISSION"],
    output_path: str,
    width: Annotated[int, Field(ge=1, le=16384)] = 2048,
    height: Annotated[int, Field(ge=1, le=16384)] = 2048,
    uv_map_name: str | None = None,
    cage_object_name: str | None = None,
    cage_extrusion: Annotated[float, Field(ge=0)] = 0.0,
    max_ray_distance: Annotated[float, Field(ge=0)] = 0.0,
    margin: Annotated[int, Field(ge=0)] = 16,
    normal_space: Literal["TANGENT", "OBJECT"] = "TANGENT",
    normal_swizzle: tuple[str, str, str] = ("POS_X", "POS_Y", "POS_Z"),
    overwrite: bool = False,
    confirm: bool = False,
) -> dict:
    """
    Bake one validated high-to-low map to an explicit file path.

    This is synchronous and potentially expensive, so `confirm=True` is
    required. The absolute `output_path` parent must exist; an existing file is
    rejected unless `overwrite=True`. The low mesh needs non-empty UVs and is
    selected-active while every named high mesh is selected as a source.
    Cycles, selection, active object, active UV, and active material image nodes
    are restored in `finally`. A supplied cage must have topology identical to
    the low mesh. `cage_extrusion` and `max_ray_distance` are world-space scene
    units; normal channel values use Blender's POS_X/NEG_X etc. enums. Returns
    image name, dimensions, map type, and the written path only after bake and save succeed.
    """
    params = {key: value for key, value in locals().items() if key != "ctx"}
    reply = await send_blender_command("bake_retopology_maps", params)
    return envelope_for(reply, changed_objects=[object_name], changed_resources=[reply["image"]])
