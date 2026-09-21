# MCP tool signatures intentionally expose more than five keyword arguments so
# agents receive precise JSON schemas instead of opaque catch-all dictionaries.
"""Typed tool for attaching a cloth's pin group to a target deformer."""

from typing import Annotated, Literal

from mcp.server.fastmcp import Context
from pydantic import Field

from ...app import mcp
from .._dispatch import call_blender
from .inspection_and_setup import ExistingPolicy

AttachmentType = Literal["HOOK", "ARMATURE", "MESH_DEFORM", "SURFACE_DEFORM"]


@mcp.tool()
async def create_cloth_attachment(
    ctx: Context,
    cloth_object_name: str,
    cloth_modifier_name: str,
    pin_group_name: str,
    target_object_name: str,
    attachment_type: AttachmentType = "HOOK",
    attachment_modifier_name: str = "Cloth Attachment",
    bone_name: str | None = None,
    rest_frame: Annotated[int, Field(ge=0)] = 1,
    existing_policy: ExistingPolicy = "ERROR",
    bind: bool = True,
) -> dict:
    """
    Create or reuse a typed attachment modifier immediately before Cloth.

    HOOK supports an optional armature bone and preserves the rest transform. ARMATURE,
    MESH_DEFORM, and SURFACE_DEFORM retain live targets; the deform variants bind only when
    ``bind`` is true. The pin group must already exist and is never modified.
    """
    return await call_blender(
        "create_cloth_attachment",
        {
            "cloth_object_name": cloth_object_name,
            "cloth_modifier_name": cloth_modifier_name,
            "pin_group_name": pin_group_name,
            "target_object_name": target_object_name,
            "attachment_type": attachment_type,
            "attachment_modifier_name": attachment_modifier_name,
            "bone_name": bone_name,
            "rest_frame": rest_frame,
            "existing_policy": existing_policy,
            "bind": bind,
        },
        changed_objects=[cloth_object_name, target_object_name],
    )
