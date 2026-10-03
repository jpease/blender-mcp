"""
Bounded HTTP helpers used by optional asset providers.

Nothing here touches `bpy`: these run on the provider fetch worker threads
(`provider_fetches.py`), never on Blender's main thread. A `Transfer` passed in
hears each response's declared size and every chunk as it lands, and may raise
from either to abandon the request; the response is closed whichever way the
transfer ends.
"""

import json

from typing import Any, Protocol

import requests

CONNECT_TIMEOUT_SECONDS = 5
READ_TIMEOUT_SECONDS = 60
DEFAULT_TIMEOUT = (CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS)
MAX_JSON_BYTES = 16 * 1024 * 1024
CHUNK_BYTES = 256 * 1024


class Transfer(Protocol):
    """Hears one fetch's progress; raising from either method abandons the request."""

    def begin(self, declared_bytes: int | None) -> None:
        """
        Learn a response's declared size, before any of its body is read.

        Args:
            declared_bytes: Its Content-Length, or None when it declared none.

        """

    def advance(self, received_bytes: int) -> None:
        """
        Learn that another stretch of the body arrived.

        Args:
            received_bytes: How many bytes this chunk held.

        """


def _declared_size(response) -> int | None:
    value = response.headers.get("Content-Length")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _check_declared_size(response, max_bytes: int, transfer: Transfer | None) -> None:
    size = _declared_size(response)
    if size is not None and size > max_bytes:
        raise ValueError(f"Download declares {size} bytes, exceeding the {max_bytes}-byte limit")
    if transfer is not None:
        transfer.begin(size)


def _bounded_chunks(response, max_bytes: int, transfer: Transfer | None):
    """
    Yield a streamed body's chunks, stopping it at the byte limit.

    Args:
        response: The streamed response.
        max_bytes: How many bytes the body may hold in all.
        transfer: Told about each chunk before it is yielded, when given.

    Yields:
        bytes: Each non-empty chunk.

    Raises:
        ValueError: Once the body passes `max_bytes`.

    """
    received = 0
    for chunk in response.iter_content(chunk_size=CHUNK_BYTES):
        if not chunk:
            continue
        received += len(chunk)
        if received > max_bytes:
            raise ValueError(f"Download exceeded the {max_bytes}-byte limit")
        if transfer is not None:
            transfer.advance(len(chunk))
        yield chunk


def get_json(
    url: str, *, headers: dict | None = None, params: dict | None = None, transfer: Transfer | None = None
) -> Any:
    """Fetch and decode one bounded JSON document."""
    response = requests.get(url, headers=headers, params=params, timeout=DEFAULT_TIMEOUT, stream=True)
    try:
        response.raise_for_status()
        _check_declared_size(response, MAX_JSON_BYTES, transfer)
        return json.loads(b"".join(_bounded_chunks(response, MAX_JSON_BYTES, transfer)))
    finally:
        response.close()


def download_file(
    url: str,
    filepath: str,
    *,
    headers: dict | None = None,
    max_bytes: int,
    transfer: Transfer | None = None,
) -> int:
    """Stream one response to an explicit path while enforcing a byte limit."""
    response = requests.get(url, headers=headers, timeout=DEFAULT_TIMEOUT, stream=True)
    try:
        response.raise_for_status()
        _check_declared_size(response, max_bytes, transfer)
        written = 0
        with open(filepath, "wb") as file_handle:
            for chunk in _bounded_chunks(response, max_bytes, transfer):
                written += len(chunk)
                file_handle.write(chunk)
        return written
    finally:
        response.close()


def get_bytes(
    url: str, *, headers: dict | None = None, max_bytes: int, transfer: Transfer | None = None
) -> tuple[bytes, str]:
    """Fetch one bounded binary response and return bytes plus Content-Type."""
    response = requests.get(url, headers=headers, timeout=DEFAULT_TIMEOUT, stream=True)
    try:
        response.raise_for_status()
        _check_declared_size(response, max_bytes, transfer)
        body = b"".join(_bounded_chunks(response, max_bytes, transfer))
        return body, response.headers.get("Content-Type", "")
    finally:
        response.close()
