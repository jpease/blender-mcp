"""
Addon-side half of the inline image transport.

An image handler is normally handed a destination path chosen by the MCP
server and writes to it, which only works while both processes see the same
filesystem. When the server asks for `inline=True` it wants the bytes in the
reply instead, so the handler writes to a path *this* process picks (always
writable, whatever the server's filesystem looks like) and the image is
base64-encoded into the response.

Deliberately free of `bpy`: nothing here touches Blender state, which keeps it
testable outside Blender and safe to import from any handler.
"""

import base64
import contextlib
import os
import tempfile

from collections.abc import Iterator
from typing import Any


@contextlib.contextmanager
def image_destination(inline: bool, filepath: str | None, suffix: str = ".png") -> Iterator[str]:
    """
    Yield the path an image handler should write to.

    Args:
        inline: True when the caller wants bytes back in the reply rather than
            a file written to a path it named.
        filepath: Destination chosen by the caller; required unless `inline`.
        suffix: Extension for the temporary file created when `inline`.

    Yields:
        str: Path to write the image to.

    Raises:
        ValueError: If a shared-path write was requested without a filepath.

    """
    if not inline:
        if not filepath:
            raise ValueError("No filepath provided")
        yield filepath
        return

    descriptor, path = tempfile.mkstemp(prefix="blender_mcp_inline_", suffix=suffix)
    os.close(descriptor)
    try:
        yield path
    finally:
        # The bytes are already in the reply by now, and this path is local to
        # this process - leaving it behind would leak a file per capture.
        if os.path.exists(path):
            os.remove(path)


def finalize_image_reply(result: dict[str, Any], *, inline: bool, path: str) -> dict[str, Any]:
    """
    Fold the written image into the reply when the caller asked for it inline.

    Args:
        result: Reply the handler built.
        inline: True when the image bytes belong in the reply.
        path: Path the handler just wrote.

    Returns:
        dict: The reply, with `image_base64` added and the process-local
        `filepath` removed when inline; unchanged otherwise.

    """
    if not inline:
        return result

    with open(path, "rb") as handle:
        result["image_base64"] = base64.b64encode(handle.read()).decode("ascii")
    # This path only exists inside this process; reporting it would invite the
    # caller to try to read it.
    result.pop("filepath", None)
    return result
