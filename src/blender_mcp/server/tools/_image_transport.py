"""
Carry an image produced inside Blender back to the MCP server.

Two transports exist because the MCP server and Blender are not guaranteed to
share a filesystem. The original transport hands Blender a path from the MCP
server's own `tempfile` and reads the bytes back off disk afterwards, which
silently assumes both processes see the same mount. That holds when Blender
runs on the same host and breaks when it runs anywhere else - a container, a
VM, another machine.

The inline transport instead lets Blender write to a destination it chooses
itself and return the bytes base64-encoded in the command reply, so nothing
about the two filesystems has to line up. It is used whenever the connected
addon advertises it (see `INLINE_IMAGE_PROTOCOL_VERSION`); an older addon
still gets the shared-path transport, so upgrading the server alone does not
break an installation that has not updated its addon yet.
"""

import base64
import contextlib
import os
import tempfile

from typing import Any

from ..connection import BlenderConnection, get_blender_connection, get_last_handshake

# First addon protocol version whose image handlers accept `inline=True` and
# answer with `image_base64`. Keep in sync with ADDON_PROTOCOL_VERSION.
INLINE_IMAGE_PROTOCOL_VERSION = 31


def addon_supports_inline_images() -> bool:
    """
    Report whether the connected addon can return image bytes in its reply.

    Returns:
        bool: True when the handshake reported a protocol new enough to accept
        `inline=True`, False when it is older or no handshake has happened.

    """
    handshake = get_last_handshake()
    if handshake is None or handshake.protocol_version is None:
        return False
    return handshake.protocol_version >= INLINE_IMAGE_PROTOCOL_VERSION


def request_image(command: str, params: dict[str, Any], *, prefix: str) -> tuple[bytes, dict[str, Any]]:
    """
    Run an image-producing Blender command and return its bytes and metadata.

    Args:
        command: Addon command to invoke.
        params: Command parameters, minus whichever transport key is added here.
        prefix: Temp-file prefix used only by the shared-path fallback.

    Returns:
        tuple[bytes, dict[str, Any]]: The image bytes, then the command's reply.

    """
    blender = get_blender_connection()
    if addon_supports_inline_images():
        return _request_inline(blender, command, params)
    return _request_via_shared_path(blender, command, params, prefix)


def _request_inline(blender: BlenderConnection, command: str, params: dict[str, Any]) -> tuple[bytes, dict[str, Any]]:
    result = blender.send_command(command, {**params, "inline": True})
    _raise_for_error(result)
    encoded = result.get("image_base64")
    if not encoded:
        raise Exception(f"{command} returned no inline image data")
    return base64.b64decode(encoded), result


def _request_via_shared_path(
    blender: BlenderConnection, command: str, params: dict[str, Any], prefix: str
) -> tuple[bytes, dict[str, Any]]:
    descriptor, temp_path = tempfile.mkstemp(prefix=prefix, suffix=".png")
    os.close(descriptor)
    try:
        result = blender.send_command(command, {**params, "filepath": temp_path})
        _raise_for_error(result)
        if not os.path.exists(temp_path) or os.path.getsize(temp_path) == 0:
            raise Exception(
                f"{command} did not write an image to {temp_path}. If Blender is running on another "
                "host or in a container it cannot see this path - update the Blender addon so the "
                "server can request image bytes inline instead."
            )
        with open(temp_path, "rb") as handle:
            return handle.read(), result
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.remove(temp_path)


def _raise_for_error(result: dict[str, Any]) -> None:
    if isinstance(result, dict) and "error" in result:
        raise Exception(result["error"])
