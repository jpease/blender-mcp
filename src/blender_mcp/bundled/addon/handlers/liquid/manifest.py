"""
Read and write the per-domain liquid sidecar manifest kept beside a domain's cache.

The manifest lets later calls tell MCP-owned cache files and MCP-owned scene objects apart from
unrelated content in the same directory. It is plain metadata: writes are idempotent and are *not*
rolled back by ``mutation_transaction``, so nothing here may touch simulation data itself.
"""

from __future__ import annotations

import contextlib
import json
import os

from datetime import UTC, datetime

MANIFEST_FILENAME = ".blender_mcp_liquid_manifest.json"


def manifest_path(resolved_directory):
    return os.path.join(resolved_directory, MANIFEST_FILENAME)


def read_manifest(resolved_directory):
    """Read this domain's manifest, or None if absent, unreadable, or not in the expected shape."""
    try:
        with open(manifest_path(resolved_directory), encoding="utf-8") as handle:
            manifest = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(manifest, dict) or not isinstance(manifest.get("stages"), dict):
        return None
    return manifest


def _blank_manifest(domain_uuid):
    return {"domain_uuid": domain_uuid, "stages": {}, "objects": {}}


def _write(resolved_directory, manifest):
    with open(manifest_path(resolved_directory), "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2)
    return manifest


def write_stage_entry(resolved_directory, domain_uuid, stage, cache_type, frame_range):
    """Record a successful bake stage so later STATUS/overwrite checks recognize MCP-owned files."""
    manifest = read_manifest(resolved_directory) or _blank_manifest(domain_uuid)
    manifest["domain_uuid"] = domain_uuid
    manifest.setdefault("objects", {})
    manifest["stages"][stage] = {
        "cache_type": cache_type,
        "frame_range": list(frame_range),
        "updated_at": datetime.now(UTC).isoformat(),
    }
    return _write(resolved_directory, manifest)


def register_objects(resolved_directory, domain_uuid, entries):
    """
    Record UUID -> {name, role} for objects this domain owns, keyed by UUID rather than name.

    ``entries`` is an iterable of ``(object_uuid, object_name, role)``. Names are stored only as a
    convenience for humans reading the file; lookups key off the UUID so a rename cannot orphan an
    entry. Returns the written manifest, or None when the directory is not writable - registration is
    best-effort bookkeeping and must never fail an otherwise successful scene mutation.
    """
    entries = [entry for entry in entries if entry[0]]
    if not entries:
        return None
    manifest = read_manifest(resolved_directory) or _blank_manifest(domain_uuid)
    manifest["domain_uuid"] = domain_uuid
    manifest.setdefault("stages", {})
    objects = manifest.setdefault("objects", {})
    timestamp = datetime.now(UTC).isoformat()
    for object_uuid, object_name, role in entries:
        objects[object_uuid] = {"name": object_name, "role": role, "updated_at": timestamp}
    with contextlib.suppress(OSError):
        return _write(resolved_directory, manifest)
    return None
