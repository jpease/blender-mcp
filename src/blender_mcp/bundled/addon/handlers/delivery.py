"""
`inspect_delivery`: does this .blend resolve on another machine.

Every other tool reports intent. This one reads back what the file actually
points at - linked libraries, images, fonts, sounds, simulation caches and the
render output template - and judges each reference by shape and by whether it
exists here. `portable` is a proof, not a guess: it is true only when the file
is saved, the scan was complete, and no reference is absolute or missing.

Published paths follow `text_hygiene`: a `//`-relative path may be published
whole, anything else is reduced to a leaf, so a reply never carries this host's
directory layout. `blend_filepath` is the one exception, published absolute
like `get_session_info.current_filepath`, because a client compares it against
the configured file roots.

`hash_libraries` reads linked `.blend` files on Blender's main thread, so it is
opt-in, confined to the configured file roots (a `Library.filepath` comes out of
the opened file and is author-controlled), bounded per file and per call, and
applied only to the libraries on the returned page.
"""

import json
import os

import bpy

from ..file_digest import MAX_DIGEST_FILES, MAX_DIGEST_TOTAL_BYTES, file_digest
from ..file_paths import canonical_path, enforce_roots
from ..output_roots import configured_file_roots
from ..text_hygiene import (
    client_safe_leaf,
    client_safe_name_leaf,
    client_safe_text,
    relative_link_body,
    safe_relative_link,
    strip_unsafe,
)
from .file_lifecycle import (
    _MAX_REPORTED_LINK_CHARS,
    PROVENANCE_PROPERTY,
    _is_indirect_library,
    _library_summary,
)
from .simulation_cache import point_cache_info
from .texture._shared import image_path_missing

# A reference class is one question a recipient asks ("are the libraries there?"),
# so the counts are published per class even when a class is empty.
DELIVERY_KINDS = ("LIBRARY", "IMAGE", "FONT", "SOUND", "CACHE", "RENDER_OUTPUT", "PATH")
# Verdicts that cost portability. PACKED travels inside the file; UNSET points at
# nothing, so nothing can fail to arrive.
UNPORTABLE_VERDICTS = frozenset({"ABSOLUTE", "MISSING"})
# A safety valve, not a page size: a scan past this cannot prove portability, so
# it says so rather than answering from a partial read.
MAX_DELIVERY_ENTRIES = 2000
# Images Blender makes itself: a render result or a generated grid has no file.
_NON_FILE_IMAGE_SOURCES = frozenset({"GENERATED", "VIEWER"})
# Blender's built-in font is not a file on this machine and cannot fail to ship.
_BUILTIN_FONT_PATH = "<builtin>"


def _published_path(raw: object, *, is_directory: bool = False) -> str:
    """
    Publish a path whole when it is a safe `//` link, else as a bare leaf.

    Args:
        raw: The path as Blender reported it.
        is_directory: True when the caller knows the path names a directory;
            its last component is then a directory name, routinely a username.

    Returns:
        str: The link, a leaf, or "" when there is no path at all.

    """
    text = str(raw or "")
    if not text.strip():
        return ""
    whole = safe_relative_link(text, _MAX_REPORTED_LINK_CHARS)
    return whole if whole is not None else client_safe_leaf(text, is_directory=is_directory)


def _is_relative(raw: object) -> bool:
    """
    Report whether a path resolves against the open `.blend`.

    Not `startswith("//")`: `///Users/...` has the prefix and names an absolute
    location.

    Args:
        raw: The path as Blender reported it.

    Returns:
        bool: True for a genuine Blender-relative path.

    """
    return relative_link_body(strip_unsafe(raw)) is not None


def _absolute(raw: object) -> bool:
    """
    Report whether a path names a location on this machine specifically.

    Args:
        raw: The path as Blender reported it.

    Returns:
        bool: True when there is a path and it is not `//`-relative.

    """
    return bool(str(raw or "").strip()) and not _is_relative(raw)


def _entry_name(value: object) -> str:
    """
    Publish a datablock or owner name, which a `.blend` author chose.

    Args:
        value: The name to reduce.

    Returns:
        str: One admissible leaf.

    """
    return client_safe_name_leaf(value)


def _shape_verdict(raw: object) -> str:
    """
    Judge a path that exists by its shape alone.

    Args:
        raw: The path as Blender reported it.

    Returns:
        str: `UNSET`, `RELATIVE_OK` or `ABSOLUTE`.

    """
    if not str(raw or "").strip():
        return "UNSET"
    return "RELATIVE_OK" if _is_relative(raw) else "ABSOLUTE"


def _entry(kind: str, name: str, raw: object, verdict: str, detail: dict, *, is_directory: bool = False) -> dict:
    """
    Build one reference entry in the single shape every source shares.

    Args:
        kind: One of `DELIVERY_KINDS`.
        name: The already-reduced owner name.
        raw: The path as Blender reported it, published by the shared rule.
        verdict: The portability verdict.
        detail: Kind-specific fields.
        is_directory: Passed through to `_published_path`.

    Returns:
        dict: `kind`, `name`, `path`, `absolute`, `verdict`, `detail`.

    """
    return {
        "kind": kind,
        "name": name,
        "path": _published_path(raw, is_directory=is_directory),
        "absolute": verdict not in {"PACKED", "UNSET"} and _absolute(raw),
        "verdict": verdict,
        "detail": detail,
    }


def _resolved(raw, library=None) -> str:
    """
    Resolve a Blender path for existence checks only; never published.

    Args:
        raw: The path as Blender reported it.
        library: The library to resolve a linked path against, if any.

    Returns:
        str: An absolute path, or "" when there was nothing to resolve.

    """
    text = str(raw or "")
    if not text.strip():
        return ""
    return str(bpy.path.abspath(text, library=library) or "")


def _library_entries(room: int) -> tuple[list[dict], dict[int, object], bool]:
    """
    Describe every linked library, and remember which entry came from which library.

    Args:
        room: Entries still allowed before the scan is capped.

    Returns:
        tuple[list[dict], dict[int, object], bool]: The entries sorted by name,
        a map from `id(entry)` to its `bpy.types.Library` so the page can be
        hashed, and whether the cap stopped the scan.

    """
    entries: list[dict] = []
    owners: dict[int, object] = {}
    capped = False
    for library in bpy.data.libraries:
        if len(entries) >= room:
            capped = True
            break
        summary = _library_summary(library)
        raw = str(getattr(library, "filepath", "") or "")
        if summary["is_missing"]:
            verdict = "MISSING"
        elif summary["is_relative"]:
            verdict = "RELATIVE_OK"
        else:
            verdict = "ABSOLUTE"
        entry = {
            "kind": "LIBRARY",
            "name": summary["name"],
            "path": summary["filepath"],
            "absolute": _absolute(raw),
            "verdict": verdict,
            "detail": {"indirect": _is_indirect_library(library), "sha256": "", "hash_skipped": ""},
        }
        owners[id(entry)] = library
        entries.append(entry)
    entries.sort(key=lambda item: item["name"])
    return entries, owners, capped


def _image_entries(room: int) -> tuple[list[dict], bool]:
    """
    Describe every local file-backed image.

    Args:
        room: Entries still allowed before the scan is capped.

    Returns:
        tuple[list[dict], bool]: The entries sorted by name, and whether the cap
        stopped the scan.

    """
    entries: list[dict] = []
    capped = False
    for image in bpy.data.images:
        if getattr(image, "library", None) is not None or image.source in _NON_FILE_IMAGE_SOURCES:
            continue
        if len(entries) >= room:
            capped = True
            break
        raw = str(getattr(image, "filepath", "") or "")
        if getattr(image, "packed_file", None) is not None:
            verdict = "PACKED"
        elif image_path_missing(image):
            verdict = "MISSING"
        else:
            verdict = _shape_verdict(raw)
        entries.append(
            _entry(
                "IMAGE",
                _entry_name(image.name),
                raw,
                verdict,
                {
                    "users": int(getattr(image, "users", 0)),
                    "dirty": bool(getattr(image, "is_dirty", False)),
                    "source": str(image.source),
                },
            )
        )
    entries.sort(key=lambda item: item["name"])
    return entries, capped


def _packed_or_existing_verdict(datablock, raw: str) -> str:
    """
    Judge a font or sound, whose only states are packed, missing, or a path shape.

    Args:
        datablock: The `VectorFont` or `Sound`.
        raw: Its `filepath`.

    Returns:
        str: `PACKED`, `MISSING`, `UNSET`, `RELATIVE_OK` or `ABSOLUTE`.

    """
    if getattr(datablock, "packed_file", None) is not None:
        return "PACKED"
    resolved = _resolved(raw)
    if resolved and not os.path.exists(resolved):
        return "MISSING"
    return _shape_verdict(raw)


def _simple_file_entries(collection, kind: str, room: int) -> tuple[list[dict], bool]:
    """
    Describe every local font or sound, which share one rule.

    Args:
        collection: `bpy.data.fonts` or `bpy.data.sounds`.
        kind: `FONT` or `SOUND`.
        room: Entries still allowed before the scan is capped.

    Returns:
        tuple[list[dict], bool]: The entries sorted by name, and whether the cap
        stopped the scan.

    """
    entries: list[dict] = []
    capped = False
    for datablock in collection:
        raw = str(getattr(datablock, "filepath", "") or "")
        if getattr(datablock, "library", None) is not None or raw == _BUILTIN_FONT_PATH:
            continue
        if len(entries) >= room:
            capped = True
            break
        entries.append(
            _entry(
                kind,
                _entry_name(datablock.name),
                raw,
                _packed_or_existing_verdict(datablock, raw),
                {"users": int(getattr(datablock, "users", 0))},
            )
        )
    entries.sort(key=lambda item: item["name"])
    return entries, capped


def _point_cache_verdict(info: dict, raw: str) -> str:
    """
    Judge a PointCache, whose disk form depends on how it was configured.

    An external cache is a directory the user named. A disk cache is written
    beside the `.blend` as `blendcache_<name>`, so it travels with the file -
    but only once the file has been saved somewhere. Neither flag means the
    cache lives in memory and there is nothing on disk to carry.

    Args:
        info: `point_cache_info` output.
        raw: The cache's `filepath`.

    Returns:
        str: The verdict.

    """
    if info.get("use_external"):
        resolved = _resolved(raw)
        if not resolved or not os.path.isdir(resolved):
            return "MISSING"
        return _shape_verdict(raw)
    if info.get("use_disk_cache"):
        return "RELATIVE_OK" if bpy.data.filepath else "UNSET"
    return "UNSET"


def _point_cache_entry(owner_name: str, cache) -> dict:
    """
    Describe one PointCache.

    Args:
        owner_name: The object or scene that owns it.
        cache: The `bpy.types.PointCache`.

    Returns:
        dict: One `CACHE` entry.

    """
    info = point_cache_info(cache)
    raw = str(info.get("filepath", "") or "")
    return _entry(
        "CACHE",
        # Each half is reduced on its own and the colon is ours: `client_safe_name_leaf`
        # refuses a colon, so reducing the joined string would publish every cache as
        # "the requested file".
        f"{_entry_name(owner_name)}:{_entry_name(info.get('name') or 'cache')}",
        raw,
        _point_cache_verdict(info, raw),
        {
            "use_disk_cache": bool(info.get("use_disk_cache", False)),
            "use_external": bool(info.get("use_external", False)),
            "is_baked": bool(info.get("is_baked", False)),
            "is_outdated": bool(info.get("is_outdated", False)),
        },
        is_directory=True,
    )


def _fluid_domain_entry(owner_name: str, domain) -> dict:
    """
    Describe one Mantaflow domain's cache directory.

    Mantaflow has no in-memory cache and no `use_disk_cache`: an empty
    `cache_directory` is a defect, not a portable default.

    Args:
        owner_name: The object that owns the domain modifier.
        domain: The modifier's `domain_settings`.

    Returns:
        dict: One `CACHE` entry.

    """
    raw = str(getattr(domain, "cache_directory", "") or "")
    resolved = _resolved(raw)
    verdict = "MISSING" if not resolved or not os.path.isdir(resolved) else _shape_verdict(raw)
    baked = bool(
        getattr(domain, "has_cache_baked_any", False)
        or getattr(domain, "has_cache_baked_data", False)
        or getattr(domain, "has_cache_baked_mesh", False)
    )
    return _entry("CACHE", _entry_name(owner_name), raw, verdict, {"is_baked": baked}, is_directory=True)


def _cache_entries(scene, room: int) -> tuple[list[dict], bool]:
    """
    Describe every simulation cache the file writes or reads.

    Args:
        scene: The scene whose rigid-body world is included.
        room: Entries still allowed before the scan is capped.

    Returns:
        tuple[list[dict], bool]: The entries sorted by name, and whether the cap
        stopped the scan.

    """
    entries: list[dict] = []
    capped = False
    for obj in bpy.data.objects:
        if getattr(obj, "library", None) is not None:
            continue
        for modifier in getattr(obj, "modifiers", ()):
            if len(entries) >= room:
                capped = True
                break
            if modifier.type == "CLOTH" and getattr(modifier, "point_cache", None) is not None:
                entries.append(_point_cache_entry(obj.name, modifier.point_cache))
            elif modifier.type == "FLUID" and getattr(modifier, "fluid_type", "") == "DOMAIN":
                domain = getattr(modifier, "domain_settings", None)
                if domain is not None:
                    entries.append(_fluid_domain_entry(obj.name, domain))
        if capped:
            break
    world = getattr(scene, "rigidbody_world", None)
    cache = getattr(world, "point_cache", None) if world is not None else None
    if cache is not None:
        if len(entries) >= room:
            capped = True
        else:
            entries.append(_point_cache_entry(str(getattr(scene, "name", "")), cache))
    entries.sort(key=lambda item: item["name"])
    return entries, capped


def _render_output_entry(scene) -> dict:
    """
    Describe where this scene's frames are written.

    Args:
        scene: The scene under inspection.

    Returns:
        dict: One `RENDER_OUTPUT` entry.

    """
    render = scene.render
    raw = str(getattr(render, "filepath", "") or "")
    return _entry(
        "RENDER_OUTPUT",
        _entry_name(getattr(scene, "name", "")),
        raw,
        _shape_verdict(raw),
        {"file_format": str(getattr(render.image_settings, "file_format", ""))},
        is_directory=raw.endswith(("/", "\\")),
    )


def _residual_path_entries(seen: set[str], room: int) -> tuple[list[dict], bool]:
    """
    Report every external path the named sources did not already account for.

    `blend_paths(local=True)` lists this file's own references, which is why the
    image, font and sound sources filter out linked datablocks: the two sets must
    describe the same scope. An indirect library reappears here with a re-derived
    path that does not string-match its `filepath`; the normalised comparison
    catches it, and anything it does not catch is reported once, which is the
    safe direction.

    Args:
        seen: Normalised absolute paths already reported.
        room: Entries still allowed before the scan is capped.

    Returns:
        tuple[list[dict], bool]: The entries sorted by name, and whether the cap
        stopped the scan.

    """
    entries: list[dict] = []
    capped = False
    for path in bpy.utils.blend_paths(absolute=False, packed=False, local=True):
        raw = str(path)
        resolved = _resolved(raw)
        if os.path.normpath(resolved) in seen:
            continue
        if len(entries) >= room:
            capped = True
            break
        if _is_relative(raw):
            verdict = "RELATIVE_OK"
        elif resolved and not os.path.exists(resolved):
            verdict = "MISSING"
        else:
            verdict = _shape_verdict(raw)
        entries.append(_entry("PATH", _entry_name(raw), raw, verdict, {}))
    entries.sort(key=lambda item: item["name"])
    return entries, capped


def _hash_page_libraries(page: list[dict], owners: dict[int, object], roots: list[str], max_hash_bytes: int) -> None:
    """
    Fill in `sha256`/`hash_skipped` for the libraries on this page, in place.

    Each path is resolved and confined to the configured roots before it is
    opened: a `Library.filepath` comes out of the opened `.blend` and would
    otherwise turn a checksum into an arbitrary-file read oracle.

    Args:
        page: The entries being returned.
        owners: `id(entry)` to `bpy.types.Library`, from `_library_entries`.
        roots: The configured file roots; never empty here.
        max_hash_bytes: Largest single file to read.

    """
    budget = MAX_DIGEST_TOTAL_BYTES
    hashed = 0
    for entry in page:
        library = owners.get(id(entry))
        if entry["kind"] != "LIBRARY" or library is None:
            continue
        if hashed >= MAX_DIGEST_FILES:
            entry["detail"]["hash_skipped"] = "call hash file limit reached"
            continue
        resolved = canonical_path(_resolved(getattr(library, "filepath", ""), getattr(library, "parent", None)))
        try:
            enforce_roots(resolved, roots)
        except ValueError:
            entry["detail"]["hash_skipped"] = "outside the configured file roots"
            continue
        digest, reason = file_digest(resolved, max_hash_bytes, budget)
        hashed += 1
        if digest is None:
            entry["detail"]["hash_skipped"] = reason or "unreadable"
            continue
        entry["detail"]["sha256"] = digest
        budget -= os.path.getsize(resolved)


def _bounded_int(name: str, value: object, low: int, high: int) -> int:
    """
    Accept an integer inside its documented range, or refuse it.

    Args:
        name: Parameter name, for the message.
        value: What the client sent.
        low: Smallest accepted value.
        high: Largest accepted value.

    Returns:
        int: The value.

    Raises:
        ValueError: When it is not an integer in range.

    """
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{name} must be an integer in [{low}, {high}]")
    return value


class DeliveryHandlersMixin:
    """Answer whether the open file's external references travel with it."""

    @staticmethod
    def inspect_delivery(
        scene_name: object,
        limit: object = 50,
        offset: object = 0,
        hash_libraries: object = False,
        max_hash_bytes: object = 268_435_456,
    ) -> dict:
        """
        Report every external reference this file carries, and whether it would resolve elsewhere.

        Args:
            scene_name: The scene whose render output and rigid-body cache are
                included; every other source is file-wide.
            limit: Entries per page, 1 to 200.
            offset: Entries to skip.
            hash_libraries: Also SHA-256 each linked library on the returned
                page. Reads those files from disk on Blender's main thread, so
                it requires configured file roots and is bounded per file and
                per call.
            max_hash_bytes: Largest single library to hash.

        Returns:
            dict: `scene`, `blend_filepath` (absolute), `saved`, `portable`,
            `classes` (per kind: `total`, `unportable`), `entries` (the page),
            `limit`, `offset`, `total`, `truncated`, `next_offset`,
            `provenance`, `changed_objects`, `warnings`, `limitations`.

        Raises:
            ValueError: When an argument is out of range, the scene is unknown,
                or hashing was requested with no file roots configured.

        """
        limit = _bounded_int("limit", limit, 1, 200)
        offset = _bounded_int("offset", offset, 0, 2**31 - 1)
        max_hash_bytes = _bounded_int("max_hash_bytes", max_hash_bytes, 1, 8 * 1024**3)
        hash_libraries = bool(hash_libraries)
        if not isinstance(scene_name, str) or not scene_name.strip():
            raise ValueError("scene_name must be a non-empty string")
        scene = bpy.data.scenes.get(scene_name.strip())
        if scene is None:
            raise ValueError(f"Scene not found: {scene_name}")
        roots = configured_file_roots() if hash_libraries else []
        if hash_libraries and not roots:
            raise ValueError(
                "hash_libraries requires BLENDERMCP_FILE_ROOTS (or BLENDERMCP_OUTPUT_ROOTS) to be configured; "
                "without roots a checksum would read any file on this host"
            )

        entries, owners, capped = _collect_entries(scene)
        page = entries[offset : offset + limit]
        if hash_libraries:
            _hash_page_libraries(page, owners, roots, max_hash_bytes)
        return _delivery_report(scene, entries, page, limit, offset, capped)


def _collect_entries(scene) -> tuple[list[dict], dict[int, object], bool]:
    """
    Walk every reference source in one fixed order, bounded by `MAX_DELIVERY_ENTRIES`.

    Args:
        scene: The scene under inspection.

    Returns:
        tuple[list[dict], dict[int, object], bool]: Every entry, the library
        owner map for hashing, and whether the cap stopped the scan.

    """
    entries, owners, capped = _library_entries(MAX_DELIVERY_ENTRIES)
    # Everything a named source reports is also something `blend_paths` reports,
    # so the residual source must not report it twice. Read from `bpy.data`, not
    # from the entries, so a capped scan still suppresses the duplicates it saw.
    seen = {
        os.path.normpath(_resolved(raw))
        for raw in (
            *(str(getattr(library, "filepath", "") or "") for library in bpy.data.libraries),
            *(str(getattr(image, "filepath", "") or "") for image in bpy.data.images),
            *(str(getattr(font, "filepath", "") or "") for font in bpy.data.fonts),
            *(str(getattr(sound, "filepath", "") or "") for sound in bpy.data.sounds),
        )
    }
    # Caches and the render output are not traversed by `blend_paths`, so they
    # are not in `seen`; the residual source runs last, against everything else.
    for source in (
        _image_entries,
        lambda room: _simple_file_entries(bpy.data.fonts, "FONT", room),
        lambda room: _simple_file_entries(bpy.data.sounds, "SOUND", room),
        lambda room: _cache_entries(scene, room),
        lambda room: ([_render_output_entry(scene)], False) if room > 0 else ([], True),
        lambda room: _residual_path_entries(seen, room),
    ):
        produced, stopped = source(MAX_DELIVERY_ENTRIES - len(entries))
        entries.extend(produced)
        capped = capped or stopped
    return entries, owners, capped


# A `.blend` anyone could have authored carries this block, so it is untrusted input: without
# a bound it injects arbitrary text into the agent's context and spends the whole reply budget.
MAX_PROVENANCE_CHARS = 16_384
MAX_PROVENANCE_FIELD_CHARS = 120
MAX_PROVENANCE_ENTRIES = 50
# The only keys read back. Anything else a file carries under this name is dropped.
_PROVENANCE_SCALARS = ("claim_generator", "protocol_version", "blender_version", "saved_utc")


def _clean_scalar(value: object) -> str:
    """
    Reduce one provenance scalar to bounded, control-free text.

    Args:
        value: Whatever the file carried.

    Returns:
        str: Safe text.

    """
    return client_safe_text(value, MAX_PROVENANCE_FIELD_CHARS)


def _clean_provenance(block: dict) -> dict:
    """
    Keep only the known keys of a provenance block, each bounded and control-free.

    Args:
        block: The parsed block.

    Returns:
        dict: `present`, `valid`, the known scalars, `ingredients` and `actions`.

    """
    cleaned: dict[str, object] = {"present": True, "valid": True}
    for key in _PROVENANCE_SCALARS:
        cleaned[key] = _clean_scalar(block.get(key, ""))
    ingredients = block.get("ingredients")
    cleaned["ingredients"] = [
        {
            "name": _clean_scalar(entry.get("name", "")),
            "filepath": _clean_scalar(entry.get("filepath", "")),
            "sha256": _clean_scalar(entry.get("sha256", "")),
            "skipped": _clean_scalar(entry.get("skipped", "")),
        }
        for entry in (ingredients if isinstance(ingredients, list) else [])[:MAX_PROVENANCE_ENTRIES]
        if isinstance(entry, dict)
    ]
    actions = block.get("actions")
    cleaned["actions"] = [_clean_action(entry) for entry in _bounded_dicts(actions)]
    return cleaned


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


def _read_provenance(scene) -> dict | None:
    """
    Read this scene's provenance block back, treating it as text a stranger wrote.

    Args:
        scene: The scene under inspection.

    Returns:
        dict | None: None when the file carries no block; otherwise `present` with either
        `valid: False` and a reason, or the cleaned block.

    """
    try:
        raw = scene.get(PROVENANCE_PROPERTY)
    except Exception:
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


def _delivery_report(scene, entries: list[dict], page: list[dict], limit: int, offset: int, capped: bool) -> dict:
    """
    Shape the reply, counting portability over every entry rather than the page.

    Args:
        scene: The scene under inspection.
        entries: Every collected entry.
        page: The entries being returned.
        limit: The page size that produced `page`.
        offset: The offset that produced `page`.
        capped: Whether `MAX_DELIVERY_ENTRIES` stopped the scan.

    Returns:
        dict: The `inspect_delivery` result.

    """
    classes = {kind: {"total": 0, "unportable": 0} for kind in DELIVERY_KINDS}
    unportable = 0
    for entry in entries:
        bucket = classes[entry["kind"]]
        bucket["total"] += 1
        if entry["verdict"] in UNPORTABLE_VERDICTS:
            bucket["unportable"] += 1
            unportable += 1
    saved = bool(bpy.data.filepath)
    warnings = []
    if not saved:
        warnings.append(
            "This session has never been saved, so // paths have nothing to resolve against; save first, "
            "then re-inspect."
        )
    if capped:
        warnings.append(
            f"More than {MAX_DELIVERY_ENTRIES} external references; portability could not be proven from a "
            "partial scan."
        )
    returned_end = offset + len(page)
    return {
        "scene": _entry_name(getattr(scene, "name", "")),
        "blend_filepath": bpy.data.filepath,
        "saved": saved,
        "portable": saved and not capped and unportable == 0,
        "classes": classes,
        "entries": page,
        "limit": limit,
        "offset": offset,
        "total": len(entries),
        "truncated": returned_end < len(entries),
        "next_offset": returned_end if returned_end < len(entries) else None,
        "provenance": _read_provenance(scene),
        "changed_objects": [],
        "warnings": warnings,
        "limitations": [
            "Paths that are not // -relative are reported by leaf name only, so the reply never carries this "
            "host's directory layout.",
            "A packed image is portable; a packed image with unsaved edits (dirty) is not yet written.",
            "Verdicts describe path shape and existence on this machine, not whether the destination can read them.",
        ],
    }
