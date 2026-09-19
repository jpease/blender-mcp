# pyright: reportCallIssue=false, reportInvalidTypeForm=false
"""
Typed scene composition, hierarchy, constraint, modifier and validation tools.

Registers the seven core-surface scene tools, including the cross-domain
`validate_scene` preflight. Geometry authoring and destructive scene operations live in
`scene_authoring.py` (bundle: `scene-authoring`); the shared input base and Blender
dispatch helper live in `_scene_shared.py`.
"""

import asyncio
import functools
import operator

from typing import Annotated, Any, Literal

from mcp.server.fastmcp import Context
from pydantic import Field, TypeAdapter, create_model, model_validator

from ..app import mcp
from ._scene_shared import _call, _StrictModel
from .envelope import STALE_INDEX_WARNING


class TransformPatch(_StrictModel):
    """Partial transform channels or a complete 4x4 matrix."""

    location: tuple[float, float, float] | None = None
    rotation_euler: tuple[float, float, float] | None = None
    rotation_quaternion: tuple[float, float, float, float] | None = None
    rotation_axis_angle: tuple[float, float, float, float] | None = None
    scale: tuple[float, float, float] | None = None
    matrix: tuple[tuple[float, float, float, float], ...] | None = None

    @model_validator(mode="after")
    def validate_representation(self) -> "TransformPatch":
        """Reject ambiguous rotation and matrix representations."""
        rotations = (self.rotation_euler, self.rotation_quaternion, self.rotation_axis_angle)
        if not any(value is not None for value in (self.location, *rotations, self.scale, self.matrix)):
            raise ValueError("patch must set at least one transform field")
        if sum(value is not None for value in rotations) > 1:
            raise ValueError("Supply at most one rotation representation")
        if self.matrix is not None:
            if len(self.matrix) != 4 or any(len(row) != 4 for row in self.matrix):
                raise ValueError("matrix must be 4x4")
            if any(value is not None for value in (self.location, *rotations, self.scale)):
                raise ValueError("matrix is mutually exclusive with component transforms")
        if self.scale is not None and any(value == 0 for value in self.scale):
            raise ValueError("scale components must be non-zero")
        return self


class InstanceTransform(_StrictModel):
    """Local transform assigned to one generated duplicate or instance."""

    location: tuple[float, float, float] = (0.0, 0.0, 0.0)
    rotation: tuple[float, float, float] = (0.0, 0.0, 0.0)
    scale: tuple[float, float, float] = (1.0, 1.0, 1.0)

    @model_validator(mode="after")
    def validate_scale(self) -> "InstanceTransform":
        """Reject degenerate instance transforms."""
        if any(value == 0 for value in self.scale):
            raise ValueError("scale components must be non-zero")
        return self


class HierarchyAssignment(_StrictModel):
    """One explicit object-parent or bone-parent assignment."""

    child_object_name: str = Field(min_length=1)
    parent_object_name: str | None = None
    parent_bone_name: str | None = None


class ConstraintSpec(_StrictModel):
    """Allowlisted object constraint and settings patch."""

    name: str = Field(min_length=1)
    type: Literal[
        "COPY_LOCATION",
        "COPY_ROTATION",
        "COPY_SCALE",
        "COPY_TRANSFORMS",
        "CHILD_OF",
        "DAMPED_TRACK",
        "TRACK_TO",
        "LOCKED_TRACK",
        "FOLLOW_PATH",
        "CLAMP_TO",
        "LIMIT_LOCATION",
        "LIMIT_ROTATION",
        "LIMIT_SCALE",
        "LIMIT_DISTANCE",
        "STRETCH_TO",
        "SHRINKWRAP",
    ]
    target_object_name: str | None = None
    subtarget: str | None = None
    influence: float = Field(default=1.0, ge=0, le=1)
    settings: dict[str, Any] = Field(default_factory=dict)


class ModifierIdReference(_StrictModel):
    """Explicit Blender ID pointer used by modifier settings."""

    id_type: Literal["OBJECT", "COLLECTION", "TEXTURE"]
    name: Annotated[str, Field(min_length=1)]


_MODIFIER_SETTING_NAMES = {
    "ARRAY": "count fit_type relative_offset_displace constant_offset_displace use_relative_offset use_constant_offset use_object_offset offset_object use_merge_vertices merge_threshold",
    "BEVEL": "width segments limit_method angle_limit affect profile vertex_group harden_normals",
    "BOOLEAN": "operation solver object collection operand_type use_self use_hole_tolerant",
    "BUILD": "frame_start frame_duration use_reverse use_random_order seed",
    "CAST": "cast_type factor radius size use_x use_y use_z use_radius_as_size object vertex_group",
    "CURVE": "object deform_axis vertex_group",
    "DECIMATE": "decimate_type ratio iterations angle_limit use_collapse_triangulate vertex_group vertex_group_factor",
    "DISPLACE": "strength mid_level direction space texture texture_coords texture_coords_object uv_layer vertex_group",
    "LATTICE": "object strength vertex_group",
    "MASK": "mode armature vertex_group invert_vertex_group threshold",
    "MESH_DEFORM": "object vertex_group precision use_dynamic_bind",
    "MIRROR": "use_axis use_bisect_axis use_bisect_flip_axis use_clip use_mirror_merge merge_threshold mirror_object",
    "REMESH": "mode octree_depth scale sharpness voxel_size use_smooth_shade use_remove_disconnected threshold",
    "SCREW": "axis angle steps render_steps iterations screw_offset object use_merge_vertices merge_threshold",
    "SHRINKWRAP": "target wrap_method wrap_mode offset project_limit use_project_x use_project_y use_project_z use_negative_direction use_positive_direction cull_face vertex_group",
    "SIMPLE_DEFORM": "deform_method deform_axis deform_angle deform_factor limits origin vertex_group invert_vertex_group",
    "SKIN": "use_smooth_shade branch_smoothing",
    "SMOOTH": "factor iterations use_x use_y use_z vertex_group",
    "SOLIDIFY": "thickness offset use_even_offset use_quality_normals material_offset material_offset_rim vertex_group",
    "SUBSURF": "subdivision_type levels render_levels quality uv_smooth boundary_smooth show_only_control_edges",
    "TRIANGULATE": "quad_method ngon_method min_vertices keep_custom_normals",
    "WAVE": "height width narrowness speed damping_time falloff_radius start_position_x start_position_y use_x use_y use_cyclic use_normal texture texture_coords_object uv_layer vertex_group",
    "WELD": "mode merge_threshold loose_edges vertex_group",
    "WIREFRAME": "thickness offset use_even_offset use_relative_offset use_boundary use_replace material_offset vertex_group",
    "WEIGHTED_NORMAL": "weight keep_sharp thresh mode vertex_group invert_vertex_group",
    "UV_PROJECT": "uv_layer aspect_x aspect_y scale_x scale_y",
    "UV_WARP": "object_from object_to bone_from bone_to uv_layer center axis_u axis_v vertex_group invert_vertex_group",
    "VOLUME_TO_MESH": "object grid_name threshold adaptivity",
    "MESH_TO_VOLUME": "object density voxel_amount voxel_size interior_band_width resolution_mode",
    "OCEAN": "geometry_mode resolution spatial_size wave_scale wave_scale_min wind_velocity wave_alignment wave_direction damping smallest_wave choppiness time spectrum fetch_jonswap sharpen_peak random_seed",
}
_MODIFIER_BOOL_SETTINGS = {
    "harden_normals",
    "invert_vertex_group",
    "keep_custom_normals",
    "keep_sharp",
    "loose_edges",
    "show_only_control_edges",
    "use_boundary",
    "use_clip",
    "use_collapse_triangulate",
    "use_constant_offset",
    "use_cyclic",
    "use_dynamic_bind",
    "use_even_offset",
    "use_hole_tolerant",
    "use_merge_vertices",
    "use_mirror_merge",
    "use_negative_direction",
    "use_normal",
    "use_object_offset",
    "use_positive_direction",
    "use_quality_normals",
    "use_radius_as_size",
    "use_random_order",
    "use_relative_offset",
    "use_remove_disconnected",
    "use_replace",
    "use_reverse",
    "use_self",
    "use_smooth_shade",
    "use_x",
    "use_y",
    "use_z",
    "use_project_x",
    "use_project_y",
    "use_project_z",
}
_MODIFIER_INT_SETTINGS = {
    "count",
    "frame_duration",
    "frame_start",
    "iterations",
    "levels",
    "material_offset",
    "material_offset_rim",
    "min_vertices",
    "octree_depth",
    "precision",
    "quality",
    "random_seed",
    "render_levels",
    "render_steps",
    "resolution",
    "seed",
    "segments",
    "steps",
    "voxel_amount",
}
_MODIFIER_VECTOR_SETTINGS = {
    "center",
    "constant_offset_displace",
    "limits",
    "relative_offset_displace",
    "use_axis",
    "use_bisect_axis",
    "use_bisect_flip_axis",
}
_MODIFIER_POINTER_SETTINGS = {
    "armature",
    "collection",
    "mirror_object",
    "object",
    "object_from",
    "object_to",
    "offset_object",
    "origin",
    "target",
    "texture",
    "texture_coords_object",
}
_MODIFIER_STRING_SETTINGS = {
    "affect",
    "axis",
    "axis_u",
    "axis_v",
    "bone_from",
    "bone_to",
    "boundary_smooth",
    "cast_type",
    "cull_face",
    "decimate_type",
    "deform_axis",
    "deform_method",
    "direction",
    "fit_type",
    "geometry_mode",
    "grid_name",
    "limit_method",
    "mode",
    "ngon_method",
    "operand_type",
    "operation",
    "quad_method",
    "resolution_mode",
    "solver",
    "space",
    "spectrum",
    "subdivision_type",
    "texture_coords",
    "uv_layer",
    "uv_smooth",
    "vertex_group",
    "wrap_method",
    "wrap_mode",
}


def _modifier_field_type(name: str):
    if name in _MODIFIER_BOOL_SETTINGS:
        return bool | None
    if name in _MODIFIER_INT_SETTINGS:
        return int | None
    if name in _MODIFIER_VECTOR_SETTINGS:
        return tuple[Any, ...] | None
    if name in _MODIFIER_POINTER_SETTINGS:
        return ModifierIdReference | None
    if name in _MODIFIER_STRING_SETTINGS:
        return str | None
    return float | None


class ModifierSpec(_StrictModel):
    """Compatibility base for the public ``{name, type, settings}`` shape."""

    name: Annotated[str, Field(min_length=1)]
    type: str
    settings: dict[str, Any] = Field(default_factory=dict)


_modifier_variants = []
for _modifier_type, _setting_names in _MODIFIER_SETTING_NAMES.items():
    _settings_model = create_model(
        f"{_modifier_type.title().replace('_', '')}ModifierSettings",
        __base__=_StrictModel,
        # pydantic matches `**fields` against create_model's reserved dunder
        # keywords before it sees them as field definitions.
        **{name: (_modifier_field_type(name), None) for name in _setting_names.split()},  # pyright: ignore[reportArgumentType]
    )
    _variant = create_model(
        f"{_modifier_type.title().replace('_', '')}ModifierSpec",
        __base__=ModifierSpec,
        type=(Literal[_modifier_type], _modifier_type),
        settings=(_settings_model, Field(default_factory=_settings_model)),
    )
    _modifier_variants.append(_variant)

ModifierSpecInput = Annotated[functools.reduce(operator.or_, _modifier_variants), Field(discriminator="type")]

# Validated inside manage_modifiers, not declared as its parameter type, so every modifier's
# settings schema stays out of the tool schema clients carry in context.
modifier_spec_adapter = TypeAdapter(ModifierSpecInput)


@mcp.tool()
async def set_object_transform(
    ctx: Context,
    object_name: str,
    patch: TransformPatch,
    space: Literal["LOCAL", "WORLD"] = "WORLD",
) -> dict:
    """Set selected transform channels or one complete matrix in explicit local or world space."""
    return await asyncio.to_thread(
        _call,
        "set_object_transform",
        {"object_name": object_name, "patch": patch.model_dump(exclude_none=True), "space": space},
        [object_name],
    )


@mcp.tool()
async def duplicate_or_instance_objects(
    ctx: Context,
    source_object_name: str,
    names: Annotated[list[str], Field(min_length=1, max_length=10_000)],
    transforms: list[InstanceTransform] | None = None,
    mode: Literal["COPY", "LINKED_DATA", "COLLECTION_INSTANCE"] = "LINKED_DATA",
    collection_name: str | None = None,
) -> dict:
    """Create bounded object copies, linked-data copies, or collection instances from one explicit source."""
    if transforms is not None and len(transforms) != len(names):
        raise ValueError("transforms must contain one record per requested name")
    return await asyncio.to_thread(
        _call,
        "duplicate_or_instance_objects",
        {
            "source_object_name": source_object_name,
            "names": names,
            "transforms": [item.model_dump() for item in transforms] if transforms else None,
            "mode": mode,
            "collection_name": collection_name,
        },
    )


@mcp.tool()
async def manage_scene_collections(
    ctx: Context,
    action: Literal["CREATE", "LINK_OBJECTS", "UNLINK_OBJECTS", "SET_VISIBILITY", "REMOVE"],
    collection_name: str,
    object_names: list[str] | None = None,
    parent_collection_name: str | None = None,
    hide_viewport: bool | None = None,
    hide_render: bool | None = None,
    confirm_remove: bool = False,
) -> dict:
    """Manage explicit scene collections without relying on selection or active context."""
    return await asyncio.to_thread(
        _call,
        "manage_scene_collections",
        {key: value for key, value in locals().items() if key != "ctx"},
        object_names,
    )


@mcp.tool()
async def manage_object_hierarchy(
    ctx: Context,
    assignments: Annotated[list[HierarchyAssignment], Field(min_length=1, max_length=1_000)],
    preserve_world_transform: bool = True,
) -> dict:
    """Parent or unparent an explicit batch while optionally preserving each child's world transform."""
    names = [assignment.child_object_name for assignment in assignments]
    return await asyncio.to_thread(
        _call,
        "manage_object_hierarchy",
        {
            "assignments": [item.model_dump() for item in assignments],
            "preserve_world_transform": preserve_world_transform,
        },
        names,
    )


@mcp.tool()
async def manage_object_constraints(
    ctx: Context,
    object_name: str,
    action: Literal["ADD", "PATCH", "REMOVE", "MOVE"],
    constraint: ConstraintSpec,
    position: int | None = Field(default=None, ge=0),
) -> dict:
    """Add, patch, move, or remove one typed object constraint using a bounded property allowlist."""
    return await asyncio.to_thread(
        _call,
        "manage_object_constraints",
        {"object_name": object_name, "action": action, "constraint": constraint.model_dump(), "position": position},
        [object_name],
    )


@mcp.tool()
async def manage_modifiers(
    ctx: Context,
    object_name: str,
    action: Literal["ADD", "PATCH", "MOVE", "REMOVE", "APPLY"],
    modifier: dict[str, Any],
    position: int | None = Field(default=None, ge=0),
    confirm_destructive: bool = False,
) -> dict:
    """
    Manage one allowlisted non-Geometry-Nodes modifier and report evaluated geometry evidence.

    modifier is {"name": ..., "type": ..., "settings": {...}}, where type selects one of Blender's
    modifier types (ARRAY, BEVEL, BOOLEAN, BUILD, CAST, CURVE, DECIMATE, DISPLACE, LATTICE, MASK,
    MESH_DEFORM, MIRROR, REMESH, SCREW, SHRINKWRAP, SIMPLE_DEFORM, SKIN, SMOOTH, SOLIDIFY, SUBSURF,
    TRIANGULATE, WAVE, WELD, WIREFRAME, WEIGHTED_NORMAL, UV_PROJECT, UV_WARP, VOLUME_TO_MESH,
    MESH_TO_VOLUME, or OCEAN) and settings accepts only that type's allowlisted fields, validated
    against Blender's real modifier schema before this call reaches Blender.
    """
    if action in {"REMOVE", "APPLY"} and not confirm_destructive:
        raise ValueError("confirm_destructive=True is required for REMOVE or APPLY")
    validated_modifier = modifier_spec_adapter.validate_python(modifier)
    warnings = [STALE_INDEX_WARNING] if action == "APPLY" else None
    result = await asyncio.to_thread(
        _call,
        "manage_modifiers",
        {
            "object_name": object_name,
            "action": action,
            "modifier": validated_modifier.model_dump(exclude_none=True),
            "position": position,
            "confirm_destructive": confirm_destructive,
        },
        [object_name],
    )
    if warnings:
        result["warnings"].extend(warnings)
    return result


@mcp.tool()
async def validate_scene(
    ctx: Context,
    scene_name: str,
    scope: Annotated[
        list[Literal["scene", "camera", "lighting", "pbr", "cloth", "liquid"]],
        Field(min_length=1, max_length=6),
    ]
    | None = None,
    max_findings: Annotated[int, Field(ge=1, le=1000)] = 300,
) -> dict:
    """
    Run one bounded, non-mutating pre-render preflight aggregating every domain validator.

    Orchestrates ``validate_pbr_asset``, ``validate_lighting_setup``, ``validate_cloth_setup``,
    ``validate_liquid_setup``, and ``validate_camera_rig`` for this scene, plus scene-level checks
    no domain owns: camera/light presence (only when the camera and lighting domains are both
    excluded from scope, since each already reports missing-camera findings on its own), frame
    range consistency, unapplied mesh scale, degenerate base-mesh geometry, and dirty cloth/rigid-
    body simulation caches.

    Findings are normalized to ``{domain, severity, code, subject, message, evidence,
    remediation}``, sorted by severity, and bounded by ``max_findings``. Check ``truncated`` and
    ``domain_summaries`` before trusting an empty result as "clean" - a domain can be truncated
    internally even while the top-level list still has room. Passing this check does not replace
    representative evaluated-frame review in Blender.
    """
    return await asyncio.to_thread(
        _call,
        "validate_scene",
        {"scene_name": scene_name, "scope": scope, "max_findings": max_findings},
    )
