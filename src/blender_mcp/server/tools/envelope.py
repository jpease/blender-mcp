"""
Shared structured-result envelope for MCP tool return values.

Every tool that returns a dict (all tools except `get_viewport_screenshot`,
`get_sketchfab_model_preview`, `render_lighting_preview`, `render_pbr_material_preview`,
`inspect_render_output`, and `create_studio_lighting`, which return one or more images
plus this same envelope as additional content items - see their docstrings) uses `ok()`
to build it:

Of those five, only `get_viewport_screenshot` is a live viewport capture (OpenGL/GPU
offscreen draw, not a render). `render_lighting_preview` and `render_pbr_material_preview`
render a disposable staging scene (a lighting comparison, a studio material preview) -
not the user's actual scene. `render_scene` renders the user's actual scene but only
writes files to disk and returns metadata (path, size, per-frame status), not pixels;
call `inspect_render_output` afterward (pointed at one of those written paths, or with
no path at all to read the in-memory Render Result) to actually see that render's pixels.

    {"ok": bool, "data": ..., "error": None, "warnings": [...], "changed_objects": [...],
     "changed_resources": [...]}

- `ok`: True unless the request reached Blender but produced no effect - for example an
  ND operator the user cancelled interactively (Esc). Check this before trusting
  `changed_objects`/`changed_resources`, which are empty whenever `ok` is False. A
  transport/validation failure never reaches this envelope; it raises `ToolError`.
- `data`: the tool-specific payload; see each tool's own Returns section for its shape.
- `error`: always None here; kept for shape symmetry with `ToolError`'s payload.
- `warnings`: non-fatal notices, e.g. that a topology-changing operation invalidated
  indices from an earlier `get_mesh_data` call, or that the operator was cancelled.
- `changed_objects`: names of Blender *objects* the call created, modified, or deleted.
  Never includes provider asset IDs, material/image/world names, or requested targets
  that turned out unchanged (e.g. a cancelled ND operator).
- `changed_resources`: names of non-object datablocks touched (materials, images,
  worlds, node groups, textures) - the counterpart to `changed_objects` for data that
  isn't a scene object.

Pagination fields (`list_scene_objects`, `get_mesh_data`, `list_polyhaven_assets`) live
inside `data`, not in this envelope: a `limit`/`offset` request, a total-count field
specific to that tool, `returned_count`, `truncated`, and `next_offset`. When `truncated`
is true, call again with `offset=next_offset` to continue.

Every reply is bounded by `REPLY_BYTE_BUDGET`. A reply stays in the agent's context for the
rest of the session, so `ok()` shortens the longest page of records in `data` until the
encoded reply fits, marks that page `truncated` with the `next_offset` to resume from, and
says so in `warnings`. Identifiers are never dropped to make room: a page keeps at least one
record, and a payload with no record list to shorten is sent whole with a warning. Tools
return what changed plus the identifiers to find the rest; full state is a `detail=True`
request, not the default.
"""

from typing import Any

from pydantic_core import to_json

STALE_INDEX_WARNING = (
    "This operation changed the mesh's topology. Vertex/edge/face indices from any get_mesh_data call made "
    "before this one are no longer reliable - call get_mesh_data again before reusing indices in further "
    "index-based edits."
)

# Wire bytes, the unit FastMCP sends: it encodes every reply with
# `pydantic_core.to_json(result, fallback=str, indent=2)`
# (`mcp/server/fastmcp/utilities/func_metadata.py`), so the indentation is part of the cost.
# 8 KiB is about 2,300 tokens, and every reply measured above it was a page of records, which
# pagination shortens without losing anything.
REPLY_BYTE_BUDGET = 8 * 1024


def _wire_bytes(reply: dict) -> int:
    return len(to_json(reply, fallback=str, indent=2))


def _record_pages(data: object) -> list[tuple[dict, str]]:
    """
    Find the lists of records in a payload that shortening could bound.

    Descends through dicts and through list entries, because the heaviest lists are nested:
    `list_libraries` returns one library record whose own `datablocks` list is the payload.

    Args:
        data: A tool's `data` payload.

    Returns:
        Each (owning dict, key) whose value is a list of two or more records, outermost first.

    """
    pages: list[tuple[dict, str]] = []
    pending: list[object] = [data]
    while pending:
        current = pending.pop(0)
        if isinstance(current, list):
            pending.extend(current)
            continue
        if not isinstance(current, dict):
            continue
        for key, value in current.items():
            if isinstance(value, list):
                # A one-entry list is no page to shorten, but its entry may own the heavy one:
                # `list_libraries` returns a single library record holding every linked datablock.
                if len(value) > 1:
                    pages.append((current, key))
                pending.extend(value)
            elif isinstance(value, dict):
                pending.append(value)
    return pages


def _shortening_warning(key: str, kept: int, total: int, resume: str) -> str:
    return (
        f"{key} was shortened to {kept} of {total} records to stay within the {REPLY_BYTE_BUDGET}-byte reply "
        f"budget; {resume}."
    )


def _fit_budget(reply: dict) -> None:
    """
    Shorten the longest page of records in `reply` until the encoded reply fits the budget.

    The warning is part of the reply, so it is in place while the page is measured: appending it
    afterwards would push a just-fitting reply back over the budget.

    The page's own `offset` is what `next_offset` counts from, so a resumed page continues
    where this one stopped rather than restarting.

    Args:
        reply: The envelope, modified in place.

    """
    if _wire_bytes(reply) <= REPLY_BYTE_BUDGET:
        return
    pages = _record_pages(reply["data"])
    if not pages:
        reply["warnings"].append(
            f"This reply is {_wire_bytes(reply)} bytes, over the {REPLY_BYTE_BUDGET}-byte reply budget, and holds "
            "no page of records to shorten; request a narrower scope."
        )
        return
    owner, key = max(pages, key=lambda page: len(to_json(page[0][page[1]], fallback=str)))
    records = owner[key]
    total = len(records)
    paged = "truncated" in owner
    start = int(owner.get("offset") or 0) if paged else 0
    resume = f"continue with offset={start + total}" if paged else "rerun with a narrower scope to see the rest"
    # The warning and the pagination keys are part of the reply, so both are in place, at their
    # widest, while the page is measured. Writing them afterwards would push a reply that just
    # fitted back over the budget - `next_offset` is a key some payloads do not carry at all.
    if paged:
        owner["truncated"] = True
        owner["next_offset"] = start + total
        if "returned_count" in owner:
            owner["returned_count"] = total
    reply["warnings"].append(_shortening_warning(key, total, total, resume))
    # Bytes grow with the record count, so the largest page that fits is a bisection, not a walk:
    # a 500-bone pose would otherwise re-encode the whole reply 500 times.
    low, high = 1, total
    while low < high:
        middle = (low + high + 1) // 2
        owner[key] = records[:middle]
        if _wire_bytes(reply) <= REPLY_BYTE_BUDGET:
            low = middle
        else:
            high = middle - 1
    kept = low
    owner[key] = records[:kept]
    if paged:
        owner["next_offset"] = start + kept
        if "returned_count" in owner:
            owner["returned_count"] = kept
        resume = f"continue with offset={owner['next_offset']}"
    reply["warnings"][-1] = _shortening_warning(key, kept, total, resume)


def ok(
    data: Any = None,
    *,
    success: bool = True,
    warnings: list[str] | None = None,
    changed_objects: list[str] | None = None,
    changed_resources: list[str] | None = None,
) -> dict:
    merged_warnings = list(warnings or [])
    # The Blender addon surfaces non-fatal notices (e.g. that an undo checkpoint
    # could not be recorded) as a `warnings` list on its result. Lift them into
    # the envelope's own warnings so the client sees them, and drop the key from
    # `data` to avoid reporting the same notice twice. Every mutating tool passes
    # the addon result straight through as `data`, so this single point covers
    # them all.
    if isinstance(data, dict) and isinstance(data.get("warnings"), list):
        merged_warnings.extend(str(warning) for warning in data["warnings"])
        data = {key: value for key, value in data.items() if key != "warnings"}
    reply = {
        "ok": success,
        "data": data,
        "error": None,
        "warnings": merged_warnings,
        "changed_objects": changed_objects or [],
        "changed_resources": changed_resources or [],
    }
    _fit_budget(reply)
    return reply
