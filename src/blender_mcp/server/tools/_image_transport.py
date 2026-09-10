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

Choosing between the two is all this module does: it takes the round trip as an
argument the way `image_capture.capture_png` does, so `_dispatch` stays the one
place that owns the socket and the one seam a test reroutes.
"""

import base64
import contextlib
import os
import tempfile

from collections.abc import Callable
from typing import Any

from ..connection import get_last_handshake

# First addon protocol version whose image handlers accept `inline=True` and
# answer with `image_base64`. 37 on this history, not the 31 the feature first
# carried: every addon from 31 through 36 dispatches these two commands by
# keyword, so `inline` reaches one of them as an unexpected argument and the
# command fails. The number is the version that actually re-signed them.
INLINE_IMAGE_PROTOCOL_VERSION = 37


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


def request_image(
    send: Callable[[str, dict], dict],
    command: str,
    params: dict[str, Any],
    *,
    prefix: str,
    missing_file: str,
) -> tuple[bytes, dict[str, Any]]:
    """
    Run an image-producing Blender command and return its bytes and metadata.

    Args:
        send: `_dispatch.send_command`, taken as an argument rather than imported so this
            module stays free of the transport it is handed.
        command: Addon command to invoke.
        params: Command parameters, minus whichever transport key is added here.
        prefix: Temp-file prefix, used only by the shared-path transport.
        missing_file: Error text for a command that reported success but wrote nothing,
            used only by the shared-path transport.

    Returns:
        tuple[bytes, dict[str, Any]]: The image bytes, then the command's reply.

    """
    if addon_supports_inline_images():
        return _request_inline(send, command, params)
    return _request_via_shared_path(send, command, params, prefix, missing_file)


def _request_inline(
    send: Callable[[str, dict], dict], command: str, params: dict[str, Any]
) -> tuple[bytes, dict[str, Any]]:
    """
    Ask Blender for the image in the reply itself, touching no local filesystem.

    Args:
        send: The round trip to Blender.
        command: Addon command to invoke.
        params: Command parameters, plus `inline`.

    Returns:
        tuple[bytes, dict[str, Any]]: The decoded image bytes, then the reply.

    Raises:
        Exception: If Blender reported an error, or answered without image bytes.

    """
    result = send(command, {**params, "inline": True})
    _raise_for_error(result)
    encoded = result.get("image_base64")
    if not encoded:
        raise Exception(f"{command} returned no inline image data")
    return base64.b64decode(encoded), result


def _request_via_shared_path(
    send: Callable[[str, dict], dict], command: str, params: dict[str, Any], prefix: str, missing_file: str
) -> tuple[bytes, dict[str, Any]]:
    """
    Name a temporary file for Blender to write, read it back, and delete it.

    Only correct while both processes see the same mount, which is exactly what the
    inline transport exists to stop assuming - so a reply that wrote nothing says so.

    Args:
        send: The round trip to Blender.
        command: Addon command to invoke.
        params: Command parameters, plus the `filepath` named here.
        prefix: Temporary-file name prefix, so a file that outlives a crash names its source.
        missing_file: Error text for a command that reported success but wrote nothing.

    Returns:
        tuple[bytes, dict[str, Any]]: The image bytes read back, then the reply.

    Raises:
        Exception: If Blender reported an error, or wrote no image.

    """
    descriptor, temp_path = tempfile.mkstemp(prefix=prefix, suffix=".png")
    os.close(descriptor)
    try:
        result = send(command, {**params, "filepath": temp_path})
        _raise_for_error(result)
        # mkstemp already created the file, so its mere existence proves nothing; an empty
        # one is what a Blender that cannot see this path leaves behind.
        if not os.path.exists(temp_path) or os.path.getsize(temp_path) == 0:
            raise Exception(
                f"{missing_file}: {command} did not write an image to {temp_path}. If Blender is "
                "running on another host or in a container it cannot see this path - update the "
                "Blender addon so the server can request image bytes inline instead."
            )
        with open(temp_path, "rb") as handle:
            return handle.read(), result
    finally:
        # Blender may never have written the file, and the client is owed that failure
        # rather than a cleanup error on top of it.
        with contextlib.suppress(FileNotFoundError):
            os.remove(temp_path)


def _raise_for_error(result: dict[str, Any]) -> None:
    """
    Turn a reply that carries an error into one.

    Args:
        result: The command's reply.

    Raises:
        Exception: If the reply carries an `error` key.

    """
    if isinstance(result, dict) and "error" in result:
        raise Exception(result["error"])
