"""The one temporary-file round trip every tool that returns Blender's pixels makes."""

import contextlib
import logging
import os
import tempfile

from collections.abc import Callable
from pathlib import Path

from mcp.server.fastmcp import Image

from .envelope import ok

logger = logging.getLogger("BlenderMCPServer")


def _verify_capture(result: dict, temp_path: str, missing_file: str) -> None:
    """
    Check that the command reported success and left a file behind.

    Args:
        result: The command's reply.
        temp_path: Where the file was asked for.
        missing_file: Error text for a reply that claimed success but wrote nothing.

    Raises:
        Exception: If the reply carries an error, or no file was written.

    """
    if "error" in result:
        raise Exception(result["error"])
    if not os.path.exists(temp_path):
        raise Exception(missing_file)


def capture_png(
    send: Callable[[str, dict], dict],
    command: str,
    params: dict,
    *,
    prefix: str,
    metadata: Callable[[dict], dict],
    failure: str,
    missing_file: str,
) -> list[Image | dict]:
    """
    Have Blender write one PNG to a temporary file, read it back, and delete it.

    Every step blocks - a socket round trip, then a file Blender writes, read here and removed
    again - so the whole of it belongs in one function a tool hands to a single
    `asyncio.to_thread`, rather than stalling the event loop a step at a time.

    Args:
        send: `_dispatch.send_command`, taken as an argument rather than imported so this
            module stays free of the transport it is handed.
        command: The addon command that writes the file.
        params: That command's parameters. "filepath" and "format" are added here, since the
            temporary file is this function's to name and to remove.
        prefix: Temporary-file name prefix, so a file that outlives a crash names its source.
        metadata: Builds the envelope's data from the command's reply.
        failure: Prefix for the raised error, naming the operation that failed.
        missing_file: Error text for a command that reported success but wrote nothing.

    Returns:
        list[Image | dict]: The image, then the envelope carrying its metadata.

    Raises:
        Exception: If the command failed, reported an error, or wrote no file.

    """
    temp_path = None
    try:
        descriptor, temp_path = tempfile.mkstemp(prefix=prefix, suffix=".png")
        os.close(descriptor)
        result = send(command, {**params, "filepath": temp_path, "format": "png"})
        _verify_capture(result, temp_path, missing_file)
        return [Image(data=Path(temp_path).read_bytes(), format="png"), ok(metadata(result))]
    except Exception as e:
        logger.error(f"{failure}: {e!s}")
        raise Exception(f"{failure}: {e!s}") from e
    finally:
        if temp_path:
            # Blender may never have written the file, and the client is owed that failure
            # rather than a cleanup error on top of it.
            with contextlib.suppress(FileNotFoundError):
                os.remove(temp_path)
