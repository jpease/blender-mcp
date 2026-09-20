"""
Bounded SHA-256 of a file the caller has already resolved and root-checked.

Blender's main thread runs the command queue, so a blocking read here freezes
the UI and every client queued behind it. Every caller therefore passes both a
per-file ceiling and what is left of the call's aggregate budget, and this
module never resolves, expands or confines a path itself: it hashes exactly the
bytes it is handed. Confinement belongs to the caller, which knows the roots.

bpy-free on purpose: `handlers/delivery.py` and `handlers/file_lifecycle.py`
both use it, and the lifecycle module must never import the delivery one.
"""

import hashlib
import os

# One call's whole disk budget. A shot links tens of canon libraries; a per-file
# bound alone still lets a request read tens of gigabytes on Blender's main thread.
MAX_DIGEST_FILES = 16
MAX_DIGEST_TOTAL_BYTES = 2 * 1024**3
_CHUNK_BYTES = 1024 * 1024


def file_digest(resolved_path: str, max_bytes: int, remaining_budget: int) -> tuple[str | None, str | None]:
    """
    SHA-256 one already-resolved, already-root-checked file.

    Args:
        resolved_path: Absolute filesystem path; the caller has confined it to
            the file roots.
        max_bytes: Largest single file to read.
        remaining_budget: Bytes left in this call's aggregate budget.

    Returns:
        tuple[str | None, str | None]: `(hex_digest, None)` on success, else
        `(None, reason)`, where reason is one of "file not found", "larger than
        max_hash_bytes", "call hash budget exhausted", "unreadable". Never raises.

    """
    try:
        size = os.path.getsize(resolved_path)
    except OSError:
        return None, "file not found"
    if size > max_bytes:
        return None, "larger than max_hash_bytes"
    if size > remaining_budget:
        return None, "call hash budget exhausted"
    digest = hashlib.sha256()
    read = 0
    try:
        with open(resolved_path, "rb") as handle:
            while chunk := handle.read(_CHUNK_BYTES):
                read += len(chunk)
                # The file grew between the stat and the read: stop at the
                # budget rather than let one file spend the whole call's.
                if read > max_bytes or read > remaining_budget:
                    return None, "larger than max_hash_bytes"
                digest.update(chunk)
    except OSError:
        return None, "unreadable"
    return digest.hexdigest(), None
