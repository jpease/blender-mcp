"""Material inventory, authoring, assignment, mapping, and texture-set tools."""

from typing import Annotated, Literal

from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field, model_validator

from ...app import mcp
from .._dispatch import call_blender
from .._inputs import StrictModel, dump_input, dump_inputs
from .._node_graph import NodeGraphEdit
from ._shared import TargetEngine, absolute_path

MaterialPreset = Literal["WATER", "GLASS", "OIL", "TINTED"]


class ShaderGraphTarget(StrictModel):
    """Exact Blender datablock that owns the shader graph."""

    type: Literal["MATERIAL", "WORLD", "LIGHT"]
    name: str = Field(min_length=1, max_length=255)


class ShaderGraphEdit(NodeGraphEdit):
    """One ordered shader-graph edit using stable node and socket identities."""


class PBRMaterialSettings(StrictModel):
    """Allowlisted Principled BSDF and material surface settings."""

    base_color: tuple[float, float, float, float] | None = None
    metallic: float | None = Field(default=None, ge=0, le=1)
    roughness: float | None = Field(default=None, ge=0, le=1)
    ior: float | None = Field(default=None, ge=1, le=4)
    transmission_weight: float | None = Field(default=None, ge=0, le=1)
    coat_weight: float | None = Field(default=None, ge=0, le=1)
    coat_roughness: float | None = Field(default=None, ge=0, le=1)
    sheen_weight: float | None = Field(default=None, ge=0, le=1)
    emission_color: tuple[float, float, float, float] | None = None
    emission_strength: float | None = Field(default=None, ge=0)
    alpha: float | None = Field(default=None, ge=0, le=1)
    normal_strength: float | None = Field(default=None, ge=0)
    displacement_mode: Literal["BUMP", "DISPLACEMENT", "BOTH"] | None = None
    surface_render_method: Literal["DITHERED", "BLENDED"] | None = None
    use_transparency_overlap: bool | None = None
    use_raytrace_refraction: bool | None = None
    thickness_mode: Literal["SPHERE", "SLAB"] | None = None
    use_backface_culling: bool | None = None
    use_transparent_shadow: bool | None = None
    volume_absorption_color: tuple[float, float, float, float] | None = None
    volume_density: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_colors(self) -> "PBRMaterialSettings":
        for name in ("base_color", "emission_color", "volume_absorption_color"):
            value = getattr(self, name)
            if value is not None and any(channel < 0 or channel > 1 for channel in value):
                raise ValueError(f"{name} channels must be in [0, 1]")
        if (self.volume_absorption_color is None) != (self.volume_density is None):
            raise ValueError("volume_absorption_color and volume_density must be supplied together")
        return self


class TextureMappingSettings(StrictModel):
    """Vector source and Image Texture sampling settings for a managed branch."""

    coordinate_source: Literal["UV", "OBJECT", "GENERATED", "CAMERA"] = "UV"
    uv_map_name: str | None = None
    object_name: str | None = None
    location: tuple[float, float, float] = (0.0, 0.0, 0.0)
    rotation: tuple[float, float, float] = (0.0, 0.0, 0.0)
    scale: tuple[float, float, float] = (1.0, 1.0, 1.0)
    projection: Literal["FLAT", "BOX", "SPHERE", "TUBE"] = "FLAT"
    projection_blend: float = Field(default=0.0, ge=0, le=1)
    interpolation: Literal["Linear", "Closest", "Cubic", "Smart"] = "Linear"
    extension: Literal["REPEAT", "EXTEND", "CLIP"] = "REPEAT"

    @model_validator(mode="after")
    def validate_source(self) -> "TextureMappingSettings":
        if self.coordinate_source == "UV" and not self.uv_map_name:
            raise ValueError("uv_map_name is required for UV coordinates")
        if self.coordinate_source == "OBJECT" and not self.object_name:
            raise ValueError("object_name is required for OBJECT coordinates")
        if any(value == 0 for value in self.scale):
            raise ValueError("scale components must be non-zero")
        return self


class TextureSetFiles(StrictModel):
    """Explicit local files for semantic PBR channels."""

    base_color: str | None = None
    metallic: str | None = None
    roughness: str | None = None
    glossiness: str | None = None
    normal_opengl: str | None = None
    normal_directx: str | None = None
    height: str | None = None
    displacement: str | None = None
    ambient_occlusion: str | None = None
    opacity: str | None = None
    emission: str | None = None
    orm: str | None = None
    rma: str | None = None

    @model_validator(mode="after")
    def validate_channels(self) -> "TextureSetFiles":
        values = dump_input(self)
        if not values:
            raise ValueError("Provide at least one texture channel")
        conflicts = [
            ("roughness", "glossiness"),
            ("normal_opengl", "normal_directx"),
            ("orm", "rma"),
        ]
        for first, second in conflicts:
            if first in values and second in values:
                raise ValueError(f"Choose {first} or {second}, not both")
        return self


@mcp.tool()
async def list_materials(
    ctx: Context,
    object_name: str | None = None,
    include_unassigned: bool = True,
    limit: int = Field(default=50, ge=1, le=200),
    offset: int = Field(default=0, ge=0),
) -> dict:
    """
    List materials with assignments, shader classification, image count, and basic Principled values.

    Use `object_name` to scope the inventory to one mesh. Follow `next_offset` while `truncated`
    is true. This intentionally summarizes graphs; use `inspect_material` for nodes and links.
    """
    params = {k: v for k, v in locals().items() if k != "ctx"}
    return await call_blender("list_materials", params)


@mcp.tool()
async def inspect_material(
    ctx: Context,
    material_name: str,
    node_limit: int = Field(default=100, ge=1, le=500),
    node_offset: int = Field(default=0, ge=0),
    link_limit: int = Field(default=200, ge=1, le=1000),
    link_offset: int = Field(default=0, ge=0),
) -> dict:
    """
    Inspect one material's effective output path, bounded graph, images, UV maps, and render settings.

    Node identity uses stable node names plus `bl_idname`; sockets include identifiers and display
    names. Pagination for nodes and links is independent. This tool never evaluates or edits pixels.
    """
    params = {k: v for k, v in locals().items() if k != "ctx"}
    return await call_blender("inspect_material", params)


@mcp.tool()
async def get_shader_node_type_info(
    ctx: Context,
    target_type: Literal["MATERIAL", "WORLD", "LIGHT"] = "MATERIAL",
    bl_idname: str | None = None,
    search: str | None = None,
    limit: int = Field(default=50, ge=1, le=200),
    offset: int = Field(default=0, ge=0),
) -> dict:
    """
    Discover shader node types and concrete sockets supported by the connected Blender runtime.

    Supply ``bl_idname`` for one exact schema or ``search`` to browse. Each candidate is instantiated
    in a disposable Material, World, or Light graph so dynamic sockets and target compatibility are
    measured rather than inferred from another Blender version.
    """
    return await call_blender(
        "get_shader_node_type_info",
        {
            "target_type": target_type,
            "bl_idname": bl_idname,
            "search": search or "",
            "limit": limit,
            "offset": offset,
        },
    )


@mcp.tool()
async def patch_shader_graph(
    ctx: Context,
    target: ShaderGraphTarget,
    operations: Annotated[list[ShaderGraphEdit], Field(min_length=1, max_length=500)],
    enable_nodes: bool = False,
) -> dict:
    """
    Atomically patch a Material, World, or Light shader graph without exposing raw node CRUD tools.

    The full ordered patch is applied to a private datablock copy and validated before all users are
    remapped in one commit. Node types and writable properties are checked against the connected
    Blender runtime; sockets use identifiers with an optional index fallback. Added nodes receive
    MCP ownership and role tags. Existing unrelated branches remain intact.

    Set ``enable_nodes=True`` only when the target does not already use nodes. The resulting graph
    must retain the target's appropriate Material, World, or Light output node.
    """
    return await call_blender(
        "patch_shader_graph",
        {
            "target": target.model_dump(),
            "operations": dump_inputs(operations),
            "enable_nodes": enable_nodes,
        },
    )


@mcp.tool()
async def create_pbr_material(
    ctx: Context,
    material_name: str,
    target_engine: TargetEngine = "BOTH",
    preset: MaterialPreset | None = None,
    settings: PBRMaterialSettings | None = None,
    reuse_existing: bool = False,
) -> dict:
    """
    Create one unassigned Principled material with an explicit engine compatibility target.

    A collision is rejected unless `reuse_existing` is true; reuse never clears or rebuilds an
    existing graph. `preset` seeds WATER/GLASS/OIL/TINTED starting values (WATER/OIL/TINTED also add
    a Volume Absorption node); `settings` overrides individual preset values. The result identifies
    the active surface shader, any Volume Absorption node, and any engine compromise.
    """
    params = {
        "material_name": material_name,
        "target_engine": target_engine,
        "preset": preset,
        # The handler reads a mapping here, so an absent patch stays `{}` rather than null.
        "settings": dump_input(settings) or {},
        "reuse_existing": reuse_existing,
    }
    return await call_blender("create_pbr_material", params)


@mcp.tool()
async def configure_pbr_material(
    ctx: Context, material_name: str, patch: PBRMaterialSettings, target_engine: TargetEngine = "BOTH"
) -> dict:
    """
    Patch supplied Principled and material settings on the shader feeding the active output.

    Omitted values remain unchanged. `BOTH` retains a shared normal/bump workflow and reports
    features that cannot render equivalently; true displacement is accepted only for Cycles.
    """
    values = dump_input(patch)
    if not values:
        raise ToolError("Provide at least one material setting")
    return await call_blender(
        "configure_pbr_material", {"material_name": material_name, "patch": values, "target_engine": target_engine}
    )


@mcp.tool()
async def assign_material(
    ctx: Context,
    material_name: str,
    object_names: list[str],
    mode: Literal["APPEND", "REPLACE_SLOT", "ASSIGN_FACES"] = "APPEND",
    slot_index: int | None = Field(default=None, ge=0),
    face_indices: dict[str, list[int]] | None = None,
) -> dict:
    """
    Assign a material to explicit mesh objects without clearing unrelated slots.

    `REPLACE_SLOT` requires `slot_index`. `ASSIGN_FACES` requires per-object face indices and uses
    the existing matching slot or appends one. Every object and index is validated before mutation.
    """
    if not object_names or len(set(object_names)) != len(object_names):
        raise ToolError("object_names must be a non-empty list of unique names")
    if mode == "REPLACE_SLOT" and slot_index is None:
        raise ToolError("slot_index is required for REPLACE_SLOT")
    if mode == "ASSIGN_FACES" and face_indices is None:
        raise ToolError("face_indices is required for ASSIGN_FACES")
    params = {k: v for k, v in locals().items() if k != "ctx"}
    return await call_blender("assign_material", params, changed_objects=object_names)


@mcp.tool()
async def configure_texture_mapping(
    ctx: Context, material_name: str, texture_node_names: list[str], settings: TextureMappingSettings
) -> dict:
    """
    Attach or update one managed coordinate/mapping branch for explicit Image Texture nodes.

    Existing unrelated vector branches are not rewritten. All target nodes are validated before
    mutation, and managed nodes are reused on repeated calls.
    """
    if not texture_node_names:
        raise ToolError("texture_node_names must not be empty")
    return await call_blender(
        "configure_texture_mapping",
        {
            "material_name": material_name,
            "texture_node_names": texture_node_names,
            "settings": dump_input(settings),
        },
    )


@mcp.tool()
async def apply_pbr_texture_set(
    ctx: Context,
    material_name: str,
    textures: TextureSetFiles,
    target_engine: TargetEngine = "BOTH",
    uv_map_name: str = "UVMap",
    normal_strength: float = Field(default=1.0, ge=0),
    height_strength: float = Field(default=0.1, ge=0),
    ao_display_strength: float = Field(default=0.0, ge=0, le=1),
    reuse_existing_images: bool = True,
) -> dict:
    """
    Build a managed, repeatable PBR image branch from an explicit local texture set.

    Color maps use sRGB; scalar/vector data use Non-Color. DirectX normals are green-channel
    corrected. Packed ORM/RMA channels are separated explicitly. AO is multiplied into base color
    only when `ao_display_strength` is non-zero. Existing user-authored branches are preserved.
    """
    supplied = dump_input(textures) or {}
    files = {channel: absolute_path(path, channel) for channel, path in supplied.items()}
    params = {
        "material_name": material_name,
        "textures": files,
        "target_engine": target_engine,
        "uv_map_name": uv_map_name,
        "normal_strength": normal_strength,
        "height_strength": height_strength,
        "ao_display_strength": ao_display_strength,
        "reuse_existing_images": reuse_existing_images,
    }
    return await call_blender("apply_pbr_texture_set", params)
