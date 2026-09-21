"""Compound bodies, constraint assemblies, fracture preparation, and animated colliders."""

from typing import Annotated, Any, Literal

from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .._dispatch import call_blender
from .inspection_and_setup import Vector3, mcp, rigid_body_constraint_adapter


class ConstraintEdge(BaseModel):
    """One deterministic connection between two rigid bodies."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    object1_name: str = Field(min_length=1)
    object2_name: str = Field(min_length=1)
    name: str | None = None
    location: Vector3 | None = None

    @model_validator(mode="after")
    def validate_endpoints(self) -> "ConstraintEdge":
        if self.object1_name == self.object2_name:
            raise ValueError("Constraint endpoints must be distinct")
        return self


@mcp.tool()
async def create_compound_rigid_body(
    ctx: Context,
    scene_name: str,
    root_object_name: str,
    child_object_names: Annotated[list[str], Field(min_length=1, max_length=128)],
    render_object_name: str | None = None,
    total_mass: Annotated[float | None, Field(gt=0.0)] = None,
    child_collision_shape: Literal["BOX", "SPHERE", "CAPSULE", "CYLINDER", "CONE", "CONVEX_HULL"] = "CONVEX_HULL",
    confirm_delete_baked_cache: bool = False,
) -> dict:
    """
    Assemble convex child colliders beneath an active COMPOUND root while preserving world transforms.

    root_object_name becomes the single active COMPOUND rigid body; child_object_names become its
    child shapes (each shaped per child_collision_shape) without their own independent rigid body
    settings. render_object_name, if given, is an existing visual mesh that is parented to the root
    so it follows the compound's simulated motion, but is not itself treated as a collider - use this
    to keep a single non-convex render mesh in sync with a compound made of simpler convex proxies.
    """
    if root_object_name in child_object_names or len(set(child_object_names)) != len(child_object_names):
        raise ToolError("root and child_object_names must be unique")
    return await call_blender(
        "create_compound_rigid_body",
        {
            "scene_name": scene_name,
            "root_object_name": root_object_name,
            "child_object_names": child_object_names,
            "render_object_name": render_object_name,
            "total_mass": total_mass,
            "child_collision_shape": child_collision_shape,
            "confirm_delete_baked_cache": confirm_delete_baked_cache,
        },
        changed_objects=[root_object_name, *child_object_names, *([render_object_name] if render_object_name else [])],
    )


@mcp.tool()
async def create_rigid_body_constraint_network(
    ctx: Context,
    scene_name: str,
    network_name: str,
    body_names: Annotated[list[str], Field(min_length=2, max_length=256)],
    configuration: dict[str, Any],
    edges: Annotated[list[ConstraintEdge] | None, Field(max_length=512)] = None,
    pairing: Literal["EXPLICIT", "CHAIN", "NEAREST", "RADIUS", "PARENT"] = "EXPLICIT",
    radius: Annotated[float | None, Field(gt=0.0)] = None,
    max_neighbors: Annotated[int, Field(ge=1, le=32)] = 4,
    collection_name: str | None = None,
    confirm_delete_baked_cache: bool = False,
) -> dict:
    """
    Build a bounded, deterministic constraint graph connecting body_names with `configuration`.

    pairing selects how edges are derived: EXPLICIT uses exactly the given `edges` (required, and
    the only pairing that accepts edges); CHAIN links each consecutive pair in body_names in order;
    NEAREST links each body to its single closest other body; RADIUS links every pair of bodies
    within `radius` (required) of each other, up to max_neighbors per body; PARENT links each body
    to its existing Blender object-parent, if that parent is also in body_names. All pairings other
    than EXPLICIT ignore edges.

    configuration is a discriminated-union object keyed by "type": FIXED, POINT, HINGE, SLIDER,
    PISTON, GENERIC, GENERIC_SPRING, or MOTOR - each with its own additional fields, validated
    against Blender's real constraint schema before this call reaches Blender.
    """
    if len(set(body_names)) != len(body_names):
        raise ToolError("body_names must be unique")
    if pairing == "EXPLICIT" and not edges:
        raise ToolError("EXPLICIT pairing requires edges")
    if pairing != "EXPLICIT" and edges:
        raise ToolError("edges are only valid with EXPLICIT pairing")
    if pairing == "RADIUS" and radius is None:
        raise ToolError("RADIUS pairing requires radius")
    validated_configuration = rigid_body_constraint_adapter.validate_python(configuration)
    return await call_blender(
        "create_rigid_body_constraint_network",
        {
            "scene_name": scene_name,
            "network_name": network_name,
            "body_names": body_names,
            "configuration": validated_configuration.model_dump(exclude_none=True, exclude_unset=True),
            "edges": [edge.model_dump(exclude_none=True) for edge in edges] if edges else [],
            "pairing": pairing,
            "radius": radius,
            "max_neighbors": max_neighbors,
            "collection_name": collection_name,
            "confirm_delete_baked_cache": confirm_delete_baked_cache,
        },
        changed_objects=body_names,
    )


@mcp.tool()
async def prepare_fracture_rigid_bodies(
    ctx: Context,
    scene_name: str,
    piece_object_names: Annotated[list[str], Field(min_length=2, max_length=500)],
    density: Annotated[float, Field(gt=0.0)],
    collision_shape: Literal["BOX", "CONVEX_HULL"] = "CONVEX_HULL",
    use_deactivation: bool = True,
    collision_margin: Annotated[float, Field(ge=0.0, le=1.0)] = 0.001,
    bond_distance: Annotated[float | None, Field(gt=0.0)] = None,
    breaking_threshold: Annotated[float | None, Field(gt=0.0)] = None,
    constraint_collection_name: str = "Rigid Body Fracture Bonds",
    confirm_delete_baked_cache: bool = False,
) -> dict:
    """Prepare existing closed fracture pieces as mass-consistent active bodies and optional breakable bonds."""
    if len(set(piece_object_names)) != len(piece_object_names):
        raise ToolError("piece_object_names must be unique")
    if (bond_distance is None) != (breaking_threshold is None):
        raise ToolError("bond_distance and breaking_threshold must be supplied together")
    return await call_blender(
        "prepare_fracture_rigid_bodies",
        {
            "scene_name": scene_name,
            "piece_object_names": piece_object_names,
            "density": density,
            "collision_shape": collision_shape,
            "use_deactivation": use_deactivation,
            "collision_margin": collision_margin,
            "bond_distance": bond_distance,
            "breaking_threshold": breaking_threshold,
            "constraint_collection_name": constraint_collection_name,
            "confirm_delete_baked_cache": confirm_delete_baked_cache,
        },
        changed_objects=piece_object_names,
    )


@mcp.tool()
async def create_rigid_body_chain(
    ctx: Context,
    scene_name: str,
    chain_name: str,
    body_names: Annotated[list[str], Field(min_length=2, max_length=256)],
    configuration: dict[str, Any],
    axis: Vector3 = (0.0, 0.0, 1.0),
    start_anchor_name: str | None = None,
    end_anchor_name: str | None = None,
    collection_name: str | None = None,
    confirm_delete_baked_cache: bool = False,
) -> dict:
    """
    Connect ordered bodies and optional passive anchors as a stable mechanical chain.

    configuration is a discriminated-union object keyed by "type": FIXED, POINT, HINGE, SLIDER,
    PISTON, GENERIC, GENERIC_SPRING, or MOTOR - each with its own additional fields, validated
    against Blender's real constraint schema before this call reaches Blender.
    """
    names = [
        *body_names,
        *([start_anchor_name] if start_anchor_name else []),
        *([end_anchor_name] if end_anchor_name else []),
    ]
    if len(set(names)) != len(names):
        raise ToolError("chain bodies and anchors must be unique")
    if sum(value * value for value in axis) <= 1e-16:
        raise ToolError("axis must be non-zero")
    validated_configuration = rigid_body_constraint_adapter.validate_python(configuration)
    return await call_blender(
        "create_rigid_body_chain",
        {
            "scene_name": scene_name,
            "chain_name": chain_name,
            "body_names": body_names,
            "configuration": validated_configuration.model_dump(exclude_none=True, exclude_unset=True),
            "axis": axis,
            "start_anchor_name": start_anchor_name,
            "end_anchor_name": end_anchor_name,
            "collection_name": collection_name,
            "confirm_delete_baked_cache": confirm_delete_baked_cache,
        },
        changed_objects=names,
    )


@mcp.tool()
async def setup_animated_passive_collider(
    ctx: Context,
    scene_name: str,
    object_name: str,
    collision_shape: Literal["BOX", "SPHERE", "CAPSULE", "CYLINDER", "CONE", "CONVEX_HULL", "MESH"],
    mesh_source: Literal["BASE", "DEFORM", "FINAL"] = "FINAL",
    use_deform: bool = False,
    sample_frames: Annotated[list[int] | None, Field(max_length=32)] = None,
    maximum_evaluated_faces: Annotated[int, Field(ge=1_000, le=1_000_000)] = 100_000,
    confirm_delete_baked_cache: bool = False,
) -> dict:
    """Configure and inspect an animated passive collider without altering its animation or modifiers."""
    if use_deform and collision_shape != "MESH":
        raise ToolError("use_deform=True requires collision_shape='MESH'")
    return await call_blender(
        "setup_animated_passive_collider",
        {
            "scene_name": scene_name,
            "object_name": object_name,
            "collision_shape": collision_shape,
            "mesh_source": mesh_source,
            "use_deform": use_deform,
            "sample_frames": sample_frames or [],
            "maximum_evaluated_faces": maximum_evaluated_faces,
            "confirm_delete_baked_cache": confirm_delete_baked_cache,
        },
        changed_objects=[object_name],
    )
