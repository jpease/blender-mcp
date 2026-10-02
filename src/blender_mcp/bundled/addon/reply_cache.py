"""
The replies to recent mutating commands, so a resent request id is answered rather than run again.

The MCP server resends a command under its first request id when the first attempt's outcome is
unknown: the frame was written and the connection then failed, so Blender may have run it. An
id found here was run and answered once already, and the drain loop sends its reply back instead
of dispatching it a second time. The handshake advertises this as `idempotent_resend`.

Module state, not `BlenderMCPServer` state: Stop/Start Server builds a new server, and that
restart is exactly what drops the connection and prompts the resend. The entries belong to one
database, the session marker they were written under; any other marker clears them all, since a
reply written against another file says nothing about this one.

Bounded twice: `CAPACITY` entries, and in total no more bytes than the reply cap one frame may
take. No in-flight tracking is needed: commands run one at a time on the main thread, so a
resend is dequeued only after the original finished and was stored.

Deliberately free of `bpy`, so it is testable on its own.
"""

from collections import OrderedDict
from dataclasses import dataclass, field

CAPACITY = 64


@dataclass
class _Cache:
    """
    The entries, the database they belong to, and their total size.

    Attributes:
        marker: The `(session_id, session_epoch)` pair every entry was written under.
        total_bytes: The sum of the entries' lengths.
        entries: Request id -> the encoded frame answered for it, least recently used first.

    """

    marker: object = None
    total_bytes: int = 0
    entries: OrderedDict[str, bytes] = field(default_factory=OrderedDict)

    def follow(self, marker: object) -> None:
        """
        Drop every entry when the session marker has moved since they were written.

        Args:
            marker: The current `(session_id, session_epoch)` pair.

        """
        if self.marker != marker:
            self.entries.clear()
            self.total_bytes = 0
            self.marker = marker

    def forget(self, request_id: str) -> None:
        """
        Remove one entry, if present, and its bytes from the total.

        Args:
            request_id: The entry's id.

        """
        payload = self.entries.pop(request_id, None)
        if payload is not None:
            self.total_bytes -= len(payload)


_CACHE = _Cache()


def lookup(request_id: object, marker: object) -> bytes | None:
    """
    Return the frame already answered for this request id under this database, if any.

    Args:
        request_id: The frame's `id`; anything but a string misses.
        marker: The current `(session_id, session_epoch)` pair.

    Returns:
        bytes | None: The encoded frame, or None when the id must run.

    """
    _CACHE.follow(marker)
    if not isinstance(request_id, str):
        return None
    payload = _CACHE.entries.get(request_id)
    if payload is not None:
        _CACHE.entries.move_to_end(request_id)
    return payload


def store(request_id: object, marker: object, payload: bytes, max_bytes: int) -> bool:
    """
    Keep one answered frame, evicting the least recently used until it fits.

    Args:
        request_id: The frame's `id`; anything but a string is not kept.
        marker: The session marker it was answered under.
        payload: The encoded frame exactly as it was sent.
        max_bytes: The total the cache may hold: the reply cap.

    Returns:
        bool: True when it was kept; a payload over `max_bytes` on its own never is.

    """
    _CACHE.follow(marker)
    if not isinstance(request_id, str) or len(payload) > max_bytes:
        return False
    _CACHE.forget(request_id)
    while _CACHE.entries and (len(_CACHE.entries) >= CAPACITY or _CACHE.total_bytes + len(payload) > max_bytes):
        _CACHE.forget(next(iter(_CACHE.entries)))
    _CACHE.entries[request_id] = payload
    _CACHE.total_bytes += len(payload)
    return True


def caches_result(result: object) -> bool:
    """
    Say whether a reply's result may be kept.

    An inline image reply carries the image's bytes, base64-encoded; one would crowd dozens of
    ordinary replies out, and a lost image is cheap to capture again.

    Args:
        result: The response's `result`.

    Returns:
        bool: False for an inline image reply.

    """
    return not (isinstance(result, dict) and "image_base64" in result)
