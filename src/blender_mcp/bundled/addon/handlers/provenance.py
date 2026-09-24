"""
The `blender_mcp` provenance block: written by `save_shot`, read back by `inspect_delivery`.

One module owns the format in both directions, because a reader that drifts from
its writer silently reports a file as unstamped. The block is shaped for C2PA
(`claim_generator`, `ingredients`, `actions`), so attaching a signed manifest to
a render becomes a serialization step rather than a second provenance model.
Unsigned and editable: it records authorship, it does not prove it.

Written as a JSON string, not a nested ID-property group: an `IDPropertyArray`
cannot hold groups, which is why every complex value this repo puts on a
datablock is `json.dumps`'d.

Read back as untrusted input. A `.blend` anyone could have authored carries this
property, so every value that comes out of one is bounded and stripped before it
reaches an agent's context.
"""

import json

from collections.abc import Sequence
from datetime import UTC, datetime

import bpy

from .. import ADDON_PROTOCOL_VERSION, authored, bl_info
from ..file_digest import MAX_DIGEST_FILE_BYTES
from ..library_digest import library_digests
from ..text_hygiene import client_safe_text
from .blend_files import PATH_REDACTION_REASONS, library_summary, path_frame

# The custom property every local scene carries after a save. One name, so a reader does not
# have to know which scene the add-on happened to be looking at.
PROVENANCE_PROPERTY = "blender_mcp"

# Without a bound an author-supplied block injects arbitrary text into the agent's context and
# spends the whole reply budget.
MAX_PROVENANCE_CHARS = 16_384
MAX_PROVENANCE_FIELD_CHARS = 120
MAX_PROVENANCE_ENTRIES = 50
# The only keys read back. Anything else a file carries under this name is dropped.
_PROVENANCE_SCALARS = ("claim_generator", "protocol_version", "blender_version", "saved_utc")
# What Blender raises for an ID custom property: a freed datablock, a value its
# property system cannot store, or a key that is not there.
_ID_PROPERTY_ERRORS = (AttributeError, KeyError, ReferenceError, TypeError)


def provenance_block(digest_roots: Sequence[str], blend_filepath: str) -> dict[str, object]:
    """
    Describe who authored this file, from what, in C2PA's vocabulary.

    Each ingredient's `filepath` is published by `blend_files.published_path_fields`
    as seen from the file being written, so it identifies the library from where the
    record will live: a library inside the configured file roots (with none, inside
    the saved file's directory) is recorded as a `//` link relative to that file, and
    any other as its leaf name with the `filepath_redaction_reason` that withheld the
    rest.

    Args:
        digest_roots: File roots to confine library checksums to; empty records
            no checksum, because hashing every library on every save would read
            gigabytes on Blender's main thread.
        blend_filepath: The `.blend` the save is about to write, which recorded
            links are relative to.

    Returns:
        dict[str, object]: The block, JSON-serializable and free of host paths
        outside the configured file roots.

    """
    addon_version = ".".join(str(part) for part in bl_info["version"])
    libraries = list(bpy.data.libraries)
    digests = (
        library_digests(libraries, digest_roots, max_file_bytes=MAX_DIGEST_FILE_BYTES)
        if digest_roots
        else [("", "")] * len(libraries)
    )
    frame = path_frame(blend_filepath)
    ingredients = []
    for library, (sha256, skipped) in zip(libraries, digests, strict=True):
        summary = library_summary(library, frame=frame)
        ingredients.append(
            {
                "name": summary["name"],
                "filepath": summary["filepath"],
                "filepath_redaction_reason": summary["filepath_redaction_reason"],
                "sha256": sha256,
                "skipped": skipped,
            }
        )
    return {
        "claim_generator": f"blender-mcp/{addon_version}",
        "protocol_version": ADDON_PROTOCOL_VERSION,
        "blender_version": bpy.app.version_string,
        "saved_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "ingredients": ingredients,
        "actions": [
            {
                "action": "c2pa.edited",
                "software_agent": f"blender-mcp/{addon_version}",
                "datablocks": [f"{entry['collection']}:{entry['name']}" for entry in authored.snapshot()],
                "datablocks_truncated": authored.was_truncated(),
            }
        ],
    }


def stamp_provenance(digest_roots: Sequence[str], blend_filepath: str) -> tuple[list[tuple[object, bool, object]], int]:
    """
    Write the provenance block into every local scene, remembering what was there.

    A linked scene is another file's data and assigning to it raises, so it is skipped.

    Args:
        digest_roots: Passed through to `provenance_block`.
        blend_filepath: Passed through to `provenance_block`.

    Returns:
        tuple[list[tuple[object, bool, object]], int]: The `(scene, key existed, previous
        value)` backup `restore_provenance` undoes, and how many ingredients were recorded.

    """
    block = provenance_block(digest_roots, blend_filepath)
    encoded = json.dumps(block, sort_keys=True)
    backup: list[tuple[object, bool, object]] = []
    for scene in bpy.data.scenes:
        if scene.library is not None:
            continue
        backup.append((scene, PROVENANCE_PROPERTY in scene, scene.get(PROVENANCE_PROPERTY)))
        scene[PROVENANCE_PROPERTY] = encoded
    return backup, len(block["ingredients"])  # pyright: ignore[reportArgumentType]


def restore_provenance(backup: list[tuple[object, bool, object]]) -> None:
    """
    Put every scene's `blender_mcp` property back as it was, in reverse order.

    A scene the failed save already freed cannot be restored and must not turn a
    reported save failure into an unrelated one, so the errors an ID custom
    property raises are the only ones passed over.

    Args:
        backup: `(scene, key existed, previous value)` triples recorded before writing.

    """
    for scene, existed, previous in reversed(backup):
        try:
            if existed:
                scene[PROVENANCE_PROPERTY] = previous  # pyright: ignore[reportIndexIssue]
            else:
                del scene[PROVENANCE_PROPERTY]  # pyright: ignore[reportIndexIssue]
        except _ID_PROPERTY_ERRORS:
            continue


def _clean_scalar(value: object) -> str:
    """
    Reduce one provenance scalar to bounded, control-free text.

    Args:
        value: Whatever the file carried.

    Returns:
        str: Safe text.

    """
    return client_safe_text(value, MAX_PROVENANCE_FIELD_CHARS)


def _bounded_dicts(value: object) -> list[dict]:
    """
    Take at most `MAX_PROVENANCE_ENTRIES` dicts out of whatever the file carried.

    Args:
        value: The parsed value, of any shape.

    Returns:
        list[dict]: The usable entries.

    """
    if not isinstance(value, list):
        return []
    return [entry for entry in value[:MAX_PROVENANCE_ENTRIES] if isinstance(entry, dict)]


def _clean_action(entry: dict) -> dict:
    """
    Reduce one recorded action, including the datablock names it claims.

    Args:
        entry: The parsed action.

    Returns:
        dict: `action`, `software_agent`, `datablocks`, `datablocks_truncated`.

    """
    names = entry.get("datablocks")
    return {
        "action": _clean_scalar(entry.get("action", "")),
        "software_agent": _clean_scalar(entry.get("software_agent", "")),
        "datablocks": [
            _clean_scalar(name) for name in (names[:MAX_PROVENANCE_ENTRIES] if isinstance(names, list) else [])
        ],
        "datablocks_truncated": bool(entry.get("datablocks_truncated")),
    }


def _clean_provenance(block: dict) -> dict:
    """
    Keep only the known keys of a provenance block, each bounded and control-free.

    Args:
        block: The parsed block.

    Returns:
        dict: `present`, `valid`, the known scalars, `ingredients` and `actions`. An
        ingredient's `filepath_redaction_reason` is one of `PATH_REDACTION_REASONS`, or
        None when the block records no known code: a whole link, or a block written
        before the field existed.

    """
    cleaned: dict[str, object] = {"present": True, "valid": True}
    for key in _PROVENANCE_SCALARS:
        cleaned[key] = _clean_scalar(block.get(key, ""))
    cleaned["ingredients"] = [
        {
            "name": _clean_scalar(entry.get("name", "")),
            "filepath": _clean_scalar(entry.get("filepath", "")),
            "filepath_redaction_reason": (
                entry.get("filepath_redaction_reason")
                if entry.get("filepath_redaction_reason") in PATH_REDACTION_REASONS
                else None
            ),
            "sha256": _clean_scalar(entry.get("sha256", "")),
            "skipped": _clean_scalar(entry.get("skipped", "")),
        }
        for entry in _bounded_dicts(block.get("ingredients"))
    ]
    cleaned["actions"] = [_clean_action(entry) for entry in _bounded_dicts(block.get("actions"))]
    return cleaned


def read_provenance(scene) -> dict | None:
    """
    Read one scene's provenance block back, treating it as text a stranger wrote.

    Args:
        scene: The scene under inspection.

    Returns:
        dict | None: None when the file carries no block; otherwise `present` with either
        `valid: False` and a reason, or the cleaned block.

    """
    try:
        raw = scene.get(PROVENANCE_PROPERTY)
    except _ID_PROPERTY_ERRORS:
        return None
    if raw is None:
        return None
    if not isinstance(raw, str) or len(raw) > MAX_PROVENANCE_CHARS:
        return {"present": True, "valid": False, "reason": "not a JSON string of the expected size"}
    try:
        block = json.loads(raw)
    except (TypeError, ValueError):
        return {"present": True, "valid": False, "reason": "unparseable"}
    if not isinstance(block, dict):
        return {"present": True, "valid": False, "reason": "unparseable"}
    return _clean_provenance(block)
