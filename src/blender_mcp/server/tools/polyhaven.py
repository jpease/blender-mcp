"""PolyHaven asset-library integration tools."""

import logging

from typing import Annotated, Literal

from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from ..app import mcp
from ._dispatch import send_blender_command
from .envelope import envelope_for, ok

logger = logging.getLogger("BlenderMCPServer")

AssetType = Literal["hdris", "textures", "models", "all"]
ImportAssetType = Literal["hdris", "textures", "models"]
AssetResolution = Annotated[str, Field(pattern=r"^[1-9][0-9]*k$")]


def _polyhaven_changed(asset_type: str, result: dict) -> tuple[list[str], list[str]]:
    """
    Split a Polyhaven import result into (changed_objects, changed_resources) by asset type.

    A model import names its own roots in the reply's `changed_objects`, which `envelope_for`
    lifts in place of this pair.

    Args:
        asset_type: The asset_type the import was requested with (`hdris`, `textures`, or `models`).
        result: The raw dict returned by the Blender-side Polyhaven import handler.

    Returns:
        tuple[list[str], list[str]]: (changed_objects, changed_resources) - never puts the Polyhaven
        asset ID itself into either list.

    """
    if asset_type == "textures":
        resources = ([result["material"]] if result.get("material") else []) + list(result.get("maps", []))
        return [], resources
    if asset_type == "hdris":
        return [], [name for name in (result.get("image_name"), result.get("world")) if name]
    return [], []


@mcp.tool()
async def get_polyhaven_categories(ctx: Context, asset_type: AssetType = "hdris") -> dict:
    """
    List Polyhaven categories and asset counts for a selected asset type.

    Args:
        ctx: MCP request context.
        asset_type: One of hdris, textures, models, all.

    Returns:
        the categories and their asset counts.

    Raises:
        ToolError: If the operation cannot be completed.

    """
    try:
        status = await send_blender_command("get_polyhaven_status")
        if not status.get("enabled", False):
            raise ToolError(
                "PolyHaven integration is disabled. Select it in the sidebar in BlenderMCP, then run it again."
            )
        result = await send_blender_command("get_polyhaven_categories", {"asset_type": asset_type})
        if "error" in result:
            raise ToolError(result["error"])
        return ok({"asset_type": asset_type, "categories": result["categories"]})
    except ToolError:
        raise
    except Exception as e:
        logger.error(f"Error getting Polyhaven categories: {e}")
        raise ToolError(f"Error getting Polyhaven categories: {e}") from e


@mcp.tool()
async def list_polyhaven_assets(
    ctx: Context,
    asset_type: AssetType = "all",
    categories: str | None = None,
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
    offset: Annotated[int, Field(ge=0)] = 0,
) -> dict:
    """
    List Polyhaven assets, optionally filtered by one or more categories.

    This catalog query has no free-text parameter; use `categories` to narrow the
    result set and `get_polyhaven_categories` to discover valid categories. Despite
    Results are deterministically ordered by asset ID and paginated with limit/offset.

    Args:
        ctx: MCP request context.
        asset_type: One of hdris, textures, models, all.
        categories: Optional comma-separated category names to filter by.
        limit: Maximum assets to return, capped at 100.
        offset: Zero-based catalog offset.

    Returns:
        matching assets with basic metadata and total/returned counts.

    Raises:
        ToolError: If the operation cannot be completed.

    """
    try:
        result = await send_blender_command(
            "list_polyhaven_assets",
            {"asset_type": asset_type, "categories": categories, "limit": limit, "offset": offset},
        )
        if "error" in result:
            raise ToolError(result["error"])
        return ok(
            {
                "total_count": result["total_count"],
                "returned_count": result["returned_count"],
                "assets": result["assets"],
                "offset": result["offset"],
                "limit": result["limit"],
                "truncated": result["truncated"],
                "next_offset": result["next_offset"],
            }
        )
    except ToolError:
        raise
    except Exception as e:
        logger.error(f"Error searching Polyhaven assets: {e}")
        raise ToolError(f"Error searching Polyhaven assets: {e}") from e


@mcp.tool()
async def import_polyhaven_asset(
    ctx: Context,
    asset_id: Annotated[str, Field(min_length=1, pattern=r"^[A-Za-z0-9_-]+$")],
    asset_type: ImportAssetType,
    resolution: AssetResolution = "1k",
    file_format: Annotated[str | None, Field(pattern=r"^[A-Za-z0-9]+$")] = None,
) -> dict:
    """
    Download a Polyhaven HDRI, texture, or model and make it available in Blender.

    The exact result depends on `asset_type`; use `apply_polyhaven_texture` after
    downloading a texture to assign it to a specific object.

    Args:
        ctx: MCP request context.
        asset_id: Polyhaven asset ID from `list_polyhaven_assets`.
        asset_type: Asset type: `hdris`, `textures`, or `models`.
        resolution: Requested resolution, such as `1k`, `2k`, or `4k`.
        file_format: Optional provider-supported format, such as HDR/EXR for HDRIs, JPG/PNG for textures, or
            GLTF/FBX for models.

    Returns:
        a "message" string, plus asset_type-specific fields: for `models`, "imported_objects" (total,
        by_type, up to 10 names; changed_objects names the model's roots); for `textures`, "material"
        and "maps" (also reported in changed_resources); for `hdris`, "image_name", "image_path", and
        "world". HDRIs are retained in a stable Blender data cache and configured through the
        non-destructive managed World graph. The image is also reported in changed_resources.
        changed_objects/changed_resources never contain the Polyhaven asset_id itself.

    Raises:
        ToolError: If the operation cannot be completed.

    """
    try:
        result = await send_blender_command(
            "import_polyhaven_asset",
            {
                "asset_id": asset_id,
                "asset_type": asset_type,
                "resolution": resolution,
                "file_format": file_format,
            },
        )
        if "error" in result:
            raise ToolError(result["error"])
        if not result.get("success"):
            raise ToolError(f"Failed to download asset: {result.get('message', 'Unknown error')}")
        changed_objects, changed_resources = _polyhaven_changed(asset_type, result)
        return envelope_for(result, changed_objects=changed_objects, changed_resources=changed_resources)
    except ToolError:
        raise
    except Exception as e:
        logger.error(f"Error downloading Polyhaven asset: {e}")
        raise ToolError(f"Error downloading Polyhaven asset: {e}") from e


@mcp.tool()
async def apply_polyhaven_texture(
    ctx: Context,
    object_name: Annotated[str, Field(min_length=1)],
    texture_id: Annotated[str, Field(min_length=1, pattern=r"^[A-Za-z0-9_-]+$")],
    replacement_policy: Literal["APPEND", "REPLACE_SLOT", "REPLACE_ALL"] = "APPEND",
    material_slot_index: Annotated[int | None, Field(ge=0)] = None,
    confirm_replace_all: bool = False,
) -> dict:
    """
    Assign a previously downloaded Polyhaven texture to an object.

    Args:
        ctx: MCP request context.
        object_name: Name of the object to apply the texture to
        texture_id: Polyhaven texture ID; it must have been downloaded first.
        replacement_policy: APPEND reuses/adds the imported material without disturbing other slots;
            REPLACE_SLOT replaces one explicit slot; REPLACE_ALL clears all slots and requires confirmation.
        material_slot_index: Existing slot index required by REPLACE_SLOT.
        confirm_replace_all: Explicit confirmation required by REPLACE_ALL.

    This reuses the material graph created by import_polyhaven_asset rather than building a duplicate.

    Returns:
        a "message" string, "material" (the new material's name), "maps" (texture map names used), and
        "material_info" (node setup details). "material" and "maps" are also reported in changed_resources.

    Raises:
        ToolError: If the operation cannot be completed.

    """
    try:
        if replacement_policy == "REPLACE_ALL" and not confirm_replace_all:
            raise ToolError("confirm_replace_all=True is required for REPLACE_ALL")
        if replacement_policy == "REPLACE_SLOT" and material_slot_index is None:
            raise ToolError("material_slot_index is required for REPLACE_SLOT")

        result = await send_blender_command(
            "apply_polyhaven_texture",
            {
                "object_name": object_name,
                "texture_id": texture_id,
                "replacement_policy": replacement_policy,
                "material_slot_index": material_slot_index,
                "confirm_replace_all": confirm_replace_all,
            },
        )
        if "error" in result:
            raise ToolError(result["error"])
        if not result.get("success"):
            raise ToolError(f"Failed to apply texture: {result.get('message', 'Unknown error')}")
        resources = ([result["material"]] if result.get("material") else []) + list(result.get("maps", []))
        return ok(result, changed_objects=[object_name], changed_resources=resources)
    except ToolError:
        raise
    except Exception as e:
        logger.error(f"Error applying texture: {e}")
        raise ToolError(f"Error applying texture: {e}") from e
