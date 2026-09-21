"""Sketchfab asset-library integration tools."""

import base64
import logging

from typing import Annotated

from mcp.server.fastmcp import Context, Image
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from ..app import mcp
from ._dispatch import send_blender_command
from .envelope import ok

logger = logging.getLogger("BlenderMCPServer")


def _preview_metadata(result: dict, uid: str) -> dict:
    """
    Build the metadata dict for a Sketchfab preview result, alongside its Image content item.

    Args:
        result: The raw dict returned by the Blender-side Sketchfab preview handler.
        uid: The model UID the preview was requested for.

    Returns:
        dict: "uid", "model_name", "author", "thumbnail_width", "thumbnail_height" (the latter two None if
        the handler didn't report them).

    """
    return {
        "uid": uid,
        "model_name": result.get("model_name", "Unknown"),
        "author": result.get("author", "Unknown"),
        "thumbnail_width": result.get("thumbnail_width"),
        "thumbnail_height": result.get("thumbnail_height"),
    }


@mcp.tool()
async def search_sketchfab_models(
    ctx: Context,
    query: Annotated[str, Field(min_length=1)],
    categories: str | None = None,
    count: Annotated[int, Field(ge=1, le=100)] = 20,
    downloadable: bool = True,
    cursor: Annotated[str | None, Field(max_length=2_048)] = None,
) -> dict:
    """
    Search one bounded Sketchfab result page for models, optionally filtering by category and downloadability.

    Pass the returned continuation cursor to retrieve the next provider page.

    Args:
        ctx: MCP request context.
        query: Search terms describing the desired model.
        categories: Optional comma-separated Sketchfab categories.
        count: Maximum number of results to return; defaults to 20.
        downloadable: When true (default), return only models that can be downloaded.
        cursor: Opaque continuation URL returned by an earlier search; omit for the first page.

    Returns:
        matching model metadata, including UIDs for previewing or importing.

    Raises:
        ToolError: If the operation cannot be completed.

    """
    try:
        logger.info(
            f"Searching Sketchfab models with query: {query}, categories: {categories}, count: {count}, downloadable: {downloadable}"
        )
        result = await send_blender_command(
            "search_sketchfab_models",
            {
                "query": query,
                "categories": categories,
                "count": count,
                "downloadable": downloadable,
                "cursor": cursor,
            },
        )
        if result is None:
            raise ToolError("Received no response from Sketchfab search")
        if "error" in result:
            raise ToolError(result["error"])
        models = result.get("results", []) or []
        return ok(
            {
                "query": query,
                "count": len(models),
                "results": models,
                "next_cursor": result.get("next"),
                "previous_cursor": result.get("previous"),
            }
        )
    except ToolError:
        raise
    except Exception as e:
        logger.error(f"Error searching Sketchfab models: {e}")
        raise ToolError(f"Error searching Sketchfab models: {e}") from e


@mcp.tool(structured_output=False)
async def get_sketchfab_model_preview(ctx: Context, uid: Annotated[str, Field(min_length=1)]) -> list[Image | dict]:
    """
    Return a Sketchfab model's thumbnail for visual review before import.

    Use this to visually confirm a model before downloading.

    Unlike other tools, this returns two content items instead of one dict: the
    thumbnail image itself, followed by an ok() envelope carrying its metadata - read
    both.

    Args:
        ctx: MCP request context.
        uid: Model UID returned by `search_sketchfab_models`.

    Returns:
        [Image, dict]: the thumbnail image, then an envelope whose data has "uid", "model_name", "author",
        "thumbnail_width", "thumbnail_height".

    Raises:
        Exception: If the operation cannot be completed.

    """
    try:
        logger.info(f"Getting Sketchfab model preview for UID: {uid}")

        result = await send_blender_command("get_sketchfab_model_preview", {"uid": uid})

        if result is None:
            raise Exception("Received no response from Blender")

        if "error" in result:
            raise Exception(result["error"])

        # Decode base64 image data
        image_data = base64.b64decode(result["image_data"])
        img_format = result.get("format", "jpeg")

        metadata = _preview_metadata(result, uid)
        logger.info(f"Preview retrieved for '{metadata['model_name']}' by {metadata['author']}")

        return [Image(data=image_data, format=img_format), ok(metadata)]

    except Exception as e:
        logger.error(f"Error getting Sketchfab preview: {e!s}")
        raise Exception(f"Failed to get preview: {e!s}") from e


@mcp.tool()
async def import_sketchfab_model(
    ctx: Context,
    uid: Annotated[str, Field(min_length=1)],
    target_size: Annotated[float, Field(gt=0)],
) -> dict:
    """
    Download and import a Sketchfab model, scaling its largest dimension to a chosen size.

    The model will be scaled so its largest dimension equals target_size.

    Args:
        ctx: MCP request context.
        uid: Downloadable model UID returned by `search_sketchfab_models`.
        target_size: Required target size in Blender units for the imported model's largest dimension; for example,
            1.0 for a chair or 4.5 for a car.

    Returns:
        import details including object names, dimensions, and bounding box. The model must be downloadable and you
        must have proper access rights.

    Raises:
        ToolError: If the operation cannot be completed.

    """
    try:
        logger.info(f"Downloading Sketchfab model: {uid}, target_size={target_size}")

        result = await send_blender_command(
            "import_sketchfab_model",
            {
                "uid": uid,
                "normalize_size": True,  # Always normalize
                "target_size": target_size,
            },
        )

        if result is None:
            raise ToolError("Received no response from Sketchfab download request")
        if "error" in result:
            raise ToolError(result["error"])
        if not result.get("success"):
            raise ToolError(f"Failed to download model: {result.get('message', 'Unknown error')}")
        return ok(result, changed_objects=result.get("imported_objects", []))
    except ToolError:
        raise
    except Exception as e:
        logger.error(f"Error downloading Sketchfab model: {e}")
        raise ToolError(f"Error downloading Sketchfab model: {e}") from e
