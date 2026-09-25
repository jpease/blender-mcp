"""The one round trip every tool that returns Blender's pixels makes."""

from collections.abc import Callable

from mcp.server.fastmcp import Image

from ._image_transport import request_image
from .envelope import ok


def capture_png(
    send: Callable[[str, dict], dict],
    command: str,
    params: dict,
    *,
    prefix: str,
    metadata: Callable[[dict], dict],
    missing_file: str,
) -> list[Image | dict]:
    """
    Have Blender produce one PNG and return it beside the envelope describing it.

    Every step blocks - a socket round trip, and for an addon too old to answer inline, a
    file Blender writes, read here and removed again - so the whole of it belongs in one
    function a tool hands to a single `asyncio.to_thread`, rather than stalling the event
    loop a step at a time. Which transport carries the bytes is `_image_transport`'s
    decision, not a tool's.

    Args:
        send: `_dispatch.send_command`, taken as an argument rather than imported so this
            module stays free of the transport it is handed.
        command: The addon command that produces the image.
        params: That command's parameters. "format" is added here, and the transport adds
            whichever of "inline"/"filepath" it needs.
        prefix: Temporary-file name prefix for the shared-path transport, so a file that
            outlives a crash names its source.
        metadata: Builds the envelope's data from the command's reply.
        missing_file: Error text for a command that reported success but wrote nothing.

    Returns:
        list[Image | dict]: The image, then the envelope carrying its metadata.

    Raises:
        ToolError: If Blender refused the command or the round trip failed (`send`'s own
            error, unchanged), or the command answered without producing an image.

    """
    image_bytes, result = request_image(
        send, command, {**params, "format": "png"}, prefix=prefix, missing_file=missing_file
    )
    return [Image(data=image_bytes, format="png"), ok(metadata(result))]
