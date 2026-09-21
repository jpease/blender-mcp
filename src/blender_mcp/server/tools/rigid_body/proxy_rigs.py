"""Low-resolution rigid-body proxy rig tools."""

from typing import Annotated, Literal

from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .._dispatch import call_blender
from .inspection_and_setup import RigidBodySettingsPatch, mcp


class RigidBodyProxyMapping(BaseModel):
    """Map one preserved render object to an existing or generated proxy."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    render_object_name: str = Field(min_length=1)
    proxy_object_name: str | None = None
    approximation: Literal["BOX", "SPHERE", "CAPSULE", "CYLINDER", "CONVEX_HULL", "LOW_RES_SOURCE"] = "CONVEX_HULL"
    low_resolution_source_name: str | None = None
    driver: Literal["PARENT", "COPY_TRANSFORMS"] = "COPY_TRANSFORMS"

    @model_validator(mode="after")
    def validate_low_resolution_source(self) -> "RigidBodyProxyMapping":
        if (self.approximation == "LOW_RES_SOURCE") != (self.low_resolution_source_name is not None):
            raise ValueError("low_resolution_source_name is required only for LOW_RES_SOURCE")
        if self.proxy_object_name == self.render_object_name:
            raise ValueError("Render and proxy objects must be distinct")
        return self


@mcp.tool()
async def create_rigid_body_proxy_rig(
    ctx: Context,
    scene_name: str,
    rig_name: str,
    mappings: Annotated[list[RigidBodyProxyMapping], Field(min_length=1, max_length=64)],
    proxy_collection_name: str = "Rigid Body Proxies",
    control_collection_name: str = "Rigid Body Controls",
    render_collection_name: str = "Rigid Body Render Assets",
    settings: RigidBodySettingsPatch | None = None,
    verification_frames: Annotated[list[int] | None, Field(max_length=5)] = None,
    transform_tolerance: Annotated[float, Field(gt=0.0, le=1.0)] = 0.001,
    confirm_delete_baked_cache: bool = False,
) -> dict:
    """
    Build a tagged proxy rig while preserving render meshes, modifiers, and materials.

    Each mapping creates or reuses a physics proxy for one render object without altering the
    render object's own mesh, modifiers, or materials. `driver` controls how the render object is
    kept in sync with its proxy's simulated motion: PARENT parents the render object to the proxy
    (preserving its existing world transform), COPY_TRANSFORMS instead adds a Copy Transforms
    constraint, leaving parenting untouched. `approximation` picks the proxy's collision shape; use
    LOW_RES_SOURCE with low_resolution_source_name to reuse an existing low-poly mesh as the proxy's
    collision geometry instead of a primitive shape. Requires scene_name to already have a rigid
    body world. Rejects with confirm_delete_baked_cache=False if that world already has a baked
    simulation cache.
    """
    frames = verification_frames or []
    render_names = [mapping.render_object_name for mapping in mappings]
    explicit_proxy_names = [mapping.proxy_object_name for mapping in mappings if mapping.proxy_object_name]
    if len(render_names) != len(set(render_names)):
        raise ToolError("render_object_name values must be unique")
    if len(explicit_proxy_names) != len(set(explicit_proxy_names)):
        raise ToolError("proxy_object_name values must be unique")
    if set(render_names) & set(explicit_proxy_names):
        raise ToolError("Render and proxy object sets must be disjoint")
    if frames != sorted(set(frames)):
        raise ToolError("verification_frames must be unique and ordered")
    if settings is not None and settings.type not in {None, "ACTIVE"}:
        raise ToolError("Proxy rig settings.type must be ACTIVE when supplied")
    payload = settings.model_dump(exclude_none=True, exclude_unset=True) if settings else {}
    changed = [
        *render_names,
        *explicit_proxy_names,
        *[mapping.low_resolution_source_name for mapping in mappings if mapping.low_resolution_source_name],
    ]
    return await call_blender(
        "create_rigid_body_proxy_rig",
        {
            "scene_name": scene_name,
            "rig_name": rig_name,
            "mappings": [mapping.model_dump(exclude_none=True) for mapping in mappings],
            "proxy_collection_name": proxy_collection_name,
            "control_collection_name": control_collection_name,
            "render_collection_name": render_collection_name,
            "settings": payload,
            "verification_frames": frames,
            "transform_tolerance": transform_tolerance,
            "confirm_delete_baked_cache": confirm_delete_baked_cache,
        },
        changed_objects=changed,
    )
