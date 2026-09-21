"""Image datablock inventory, loading, interpretation, and explicit saving."""

from typing import Literal

from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from ...app import mcp
from .._dispatch import call_blender
from ._shared import absolute_path


@mcp.tool()
async def list_texture_images(
    ctx: Context,
    material_name: str | None = None,
    include_unused: bool = True,
    limit: int = Field(default=50, ge=1, le=200),
    offset: int = Field(default=0, ge=0),
) -> dict:
    """
    List bounded image metadata, material usage, storage state, UDIM tiles, and missing files.

    Estimated memory is an uncompressed pixel estimate, not file size. Follow `next_offset` while
    `truncated` is true. No image pixels are read or changed.
    """
    params = {k: v for k, v in locals().items() if k != "ctx"}
    return await call_blender("list_texture_images", params)


@mcp.tool()
async def load_texture_image(
    ctx: Context,
    path: str,
    name: str | None = None,
    check_existing: bool = True,
    max_bytes: int = Field(default=536_870_912, ge=1, le=2_147_483_648),
) -> dict:
    """
    Load one validated local texture file as a reusable Blender image datablock.

    The absolute path must exist, use a supported texture extension, and fit `max_bytes`. Blender
    performs decoding on its main thread. The result distinguishes a reused image from a new one.
    """
    return await call_blender(
        "load_texture_image",
        {"path": absolute_path(path, "path"), "name": name, "check_existing": check_existing, "max_bytes": max_bytes},
    )


@mcp.tool()
async def configure_texture_image(
    ctx: Context,
    image_name: str,
    semantic: Literal["COLOR", "NORMAL", "ROUGHNESS", "METALLIC", "AO", "HEIGHT", "DATA"] | None = None,
    colorspace: str | None = None,
    alpha_mode: Literal["STRAIGHT", "PREMUL", "CHANNEL_PACKED", "NONE"] | None = None,
) -> dict:
    """
    Configure how Blender interprets an image without altering its pixels.

    Semantic COLOR defaults to sRGB; data semantics default to Non-Color. An explicit colorspace
    wins but must exist in the active OCIO configuration. At least one setting is required.
    """
    if semantic is None and colorspace is None and alpha_mode is None:
        raise ToolError("Provide semantic, colorspace, or alpha_mode")
    return await call_blender(
        "configure_texture_image",
        {"image_name": image_name, "semantic": semantic, "colorspace": colorspace, "alpha_mode": alpha_mode},
    )


@mcp.tool()
async def save_texture_image(
    ctx: Context,
    image_name: str,
    output_path: str,
    file_format: Literal["PNG", "TIFF", "OPEN_EXR", "JPEG"] | None = None,
    color_mode: Literal["BW", "RGB", "RGBA"] | None = None,
    color_depth: Literal["8", "16", "32"] | None = None,
    overwrite: bool = False,
) -> dict:
    """
    Save one Blender image to an explicit absolute path without silently overwriting.

    The destination directory must already exist. The image filepath and output settings are
    restored if saving fails; successful results report the actual path and byte size.
    """
    return await call_blender(
        "save_texture_image",
        {
            "image_name": image_name,
            "output_path": absolute_path(output_path, "output_path"),
            "file_format": file_format,
            "color_mode": color_mode,
            "color_depth": color_depth,
            "overwrite": overwrite,
        },
    )
