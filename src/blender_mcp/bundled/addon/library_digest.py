"""
The one bounded, root-confined SHA-256 pass over the libraries an open `.blend` links.

`save_shot(provenance_checksums=True)` and `inspect_delivery(hash_libraries=True)`
ask the same question of the same files, so they share this loop: it owns the
file-count cutoff, the confinement, the per-file bound and the call's aggregate
budget. Two copies of it had already drifted - one passed the whole call budget
as its per-file bound, leaving a single huge library to hash unbounded on
Blender's main thread - and a second copy is what made that possible.

A `Library.filepath` comes out of the opened `.blend` and is author-controlled,
so an unconfined checksum would read any file on this host: the resolved path is
used for reading only and never returned.

Imports `bpy`, unlike `file_digest`, because only Blender resolves a library's
path against the parent library it was reached through.
"""

from collections.abc import Sequence

import bpy

from .file_digest import MAX_DIGEST_FILES, MAX_DIGEST_TOTAL_BYTES, file_digest
from .file_paths import PathOutsideRootsError, canonical_path, enforce_roots
from .output_roots import configured_file_roots


def require_digest_roots(parameter: str) -> list[str]:
    """
    Read the configured file roots, refusing a hashing request that has none to be confined to.

    Args:
        parameter: The parameter that asked for checksums, named in the refusal.

    Returns:
        list[str]: The configured roots, never empty.

    Raises:
        ValueError: When no file roots are configured.

    """
    roots = configured_file_roots()
    if not roots:
        raise ValueError(
            f"{parameter} requires BLENDERMCP_FILE_ROOTS (or BLENDERMCP_OUTPUT_ROOTS) to be configured; "
            "without roots a checksum would read any file on this host"
        )
    return roots


def library_digests(libraries: Sequence[object], roots: Sequence[str], *, max_file_bytes: int) -> list[tuple[str, str]]:
    """
    SHA-256 each linked library, confined to the file roots and bounded per file and per call.

    Args:
        libraries: The libraries to hash, in the caller's own order.
        roots: The configured file roots; a library outside them is reported, never read.
        max_file_bytes: Largest single library to read.

    Returns:
        list[tuple[str, str]]: One `(sha256, skipped reason)` pair per library, in
        the order given; exactly one of the two is ever non-empty.

    """
    digests: list[tuple[str, str]] = []
    budget = MAX_DIGEST_TOTAL_BYTES
    hashed = 0
    for library in libraries:
        if hashed >= MAX_DIGEST_FILES:
            digests.append(("", "call hash file limit reached"))
            continue
        raw = str(getattr(library, "filepath", "") or "")
        resolved = canonical_path(bpy.path.abspath(raw, library=getattr(library, "parent", None)))
        try:
            enforce_roots(resolved, roots)
        except PathOutsideRootsError:
            digests.append(("", "outside the configured file roots"))
            continue
        digest, reason, read = file_digest(resolved, max_file_bytes, budget)
        hashed += 1
        budget -= read
        digests.append((digest, "") if digest is not None else ("", reason or "unreadable"))
    return digests
