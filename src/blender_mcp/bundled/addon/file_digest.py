"""
Bounded SHA-256 of a file the caller has already resolved and root-checked.

Blender's main thread runs the command queue, so a blocking read here freezes
the UI and every client queued behind it. Every caller therefore passes both a
per-file ceiling and what is left of the call's aggregate budget, and this
module never resolves, expands or confines a path itself: it hashes exactly the
bytes it is handed. Confinement belongs to the caller, which knows the roots.

bpy-free on purpose, so it can be read without Blender; `library_digest` is the
one caller, and it is where a `Library.filepath` becomes a path on this host.
"""

import hashlib
import os

# One call's whole disk budget. A shot links tens of canon libraries; a per-file
# bound alone still lets a request read tens of gigabytes on Blender's main thread.
MAX_DIGEST_FILES = 16
MAX_DIGEST_TOTAL_BYTES = 2 * 1024**3
# What one file may cost a caller that offers no bound of its own, matching
# `inspect_delivery`'s `max_hash_bytes` default. The whole-call budget is not a
# per-file bound: used as one it lets a single library stall the main thread for
# as long as the entire request was allowed to.
MAX_DIGEST_FILE_BYTES = 256 * 1024**2
_CHUNK_BYTES = 1024 * 1024


def file_digest(resolved_path: str, max_bytes: int, remaining_budget: int) -> tuple[str | None, str | None, int]:
    """
    SHA-256 one already-resolved, already-root-checked file.

    Args:
        resolved_path: Absolute filesystem path; the caller has confined it to
            the file roots.
        max_bytes: Largest single file to read.
        remaining_budget: Bytes left in this call's aggregate budget.

    Returns:
        tuple[str | None, str | None, int]: `(hex_digest, None, bytes read)` on
        success, else `(None, reason, bytes read)`, where reason is one of "file
        not found", "larger than max_hash_bytes", "call hash budget exhausted",
        "unreadable". The byte count is what this call spent of the budget, so
        the caller never has to stat the file a second time to find out. Never
        raises.

    """
    try:
        size = os.path.getsize(resolved_path)
    except OSError:
        return None, "file not found", 0
    if size > max_bytes:
        return None, "larger than max_hash_bytes", 0
    if size > remaining_budget:
        return None, "call hash budget exhausted", 0
    digest = hashlib.sha256()
    read = 0
    try:
        with open(resolved_path, "rb") as handle:
            while chunk := handle.read(_CHUNK_BYTES):
                read += len(chunk)
                # The file grew between the stat and the read: stop at the
                # budget rather than let one file spend the whole call's.
                if read > max_bytes or read > remaining_budget:
                    return None, "larger than max_hash_bytes", read
                digest.update(chunk)
    except OSError:
        return None, "unreadable", read
    return digest.hexdigest(), None, read
