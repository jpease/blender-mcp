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
rest of the session, so `ok()` shortens the pages of records in `data`, longest first and on
into the next until the encoded reply fits, marks each shortened page `truncated` with the
`next_offset` to resume from, and says so in `warnings`. Identifiers are never dropped to make
room: a page keeps at least one record, a payload with no record list to shorten is sent whole
with a warning, and a reply still over the budget with every page down to its last record says
so instead of offering an offset. Tools return what changed plus the identifiers to find the
rest; full state is a `detail=True` request, not the default.

No tool module shapes this dict itself: `envelope_for` lifts `changed_objects` and
`changed_resources` out of an addon reply, bounds the object list at `CHANGED_OBJECTS_LIMIT`, and
calls `ok()`, which leaves each module's own `_call` holding nothing but its transport and error
handling.
"""

from collections.abc import Sequence
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

# Linking a whole set changes hundreds of objects, and every name would sit in the agent's
# context for the rest of the session; `envelope_for` keeps this many and says how many there
# were in total.
CHANGED_OBJECTS_LIMIT = 50

# The two keys `envelope_for` lifts out of an addon reply; everything else stays in `data`.
_CHANGE_KEYS = frozenset({"changed_objects", "changed_resources"})


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


def _pagination_names(owner: dict, key: str) -> dict[str, str] | None:
    """
    Find the pagination keys that describe one page of records.

    A payload pages either with bare names beside an `items` list or with names prefixed by the
    list's own key, which is how `inspect_lighting_setup` reports `lights_truncated`. Without
    both spellings the shortening would leave a reply saying `truncated: false` about a page it
    had just cut.

    Args:
        owner: The dict holding the page.
        key: The key whose value is the list of records.

    Returns:
        The `truncated`/`offset`/`next_offset`/`returned_count` names in use, or None when the
        page carries no pagination to update.

    """
    for prefix in ("", f"{key}_"):
        if f"{prefix}truncated" in owner:
            return {
                "truncated": f"{prefix}truncated",
                "offset": f"{prefix}offset",
                "next_offset": f"{prefix}next_offset",
                "returned_count": f"{prefix}returned_count",
            }
    return None


# What a shortened page's warning says instead of a resume point once every page is down to its
# last record and the reply is still too big: the next page of one record would be over the
# budget too, and so would the one after it, so an offset is not a way out of this reply.
_NO_RESUME = "the reply is over the budget even with every page cut to one record - request a narrower scope"


def _page_bytes(page: tuple[dict, str]) -> int:
    """
    Measure one page's records on their own, the ordering key for which page to shorten first.

    Args:
        page: The (owning dict, key) pair naming the list of records.

    Returns:
        The encoded byte length of that list.

    """
    owner, key = page
    return len(to_json(owner[key], fallback=str))


def _shorten_page(reply: dict, owner: dict, key: str, current_bytes: int) -> tuple[int, str, int, int] | None:
    """
    Cut one page to the most records that leave `reply` inside the budget, never below one.

    The warning and the pagination keys are part of the reply, so both are in place, at their
    widest, while the page is measured. Writing them afterwards would push a reply that just
    fitted back over the budget - `next_offset` is a key some payloads do not carry at all.

    The page's own `offset` is what `next_offset` counts from, so a resumed page continues
    where this one stopped rather than restarting.

    Args:
        reply: The envelope, modified in place.
        owner: The dict holding the page.
        key: The key whose value is the list of records.
        current_bytes: What `reply` encodes to right now, already measured by the caller.

    Returns:
        The (warning index, key, kept, total) of the shortening, or None when the page costs the
        reply nothing at all.

    """
    records = owner[key]
    # `_record_pages` walks the payload before anything is cut, so a page nested inside a record
    # that a larger page has since dropped is no longer on the wire: cutting it buys no bytes and
    # its warning would name records this reply never carried.
    owner[key] = records[:1]
    reachable = _wire_bytes(reply) < current_bytes
    owner[key] = records
    if not reachable:
        return None
    total = len(records)
    names = _pagination_names(owner, key)
    start = int(owner.get(names["offset"]) or 0) if names else 0
    resume = f"continue with offset={start + total}" if names else "rerun with a narrower scope to see the rest"
    if names:
        owner[names["truncated"]] = True
        owner[names["next_offset"]] = start + total
        if names["returned_count"] in owner:
            owner[names["returned_count"]] = total
    index = len(reply["warnings"])
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
    if names:
        owner[names["next_offset"]] = start + kept
        if names["returned_count"] in owner:
            owner[names["returned_count"]] = kept
        resume = f"continue with offset={owner[names['next_offset']]}"
    reply["warnings"][index] = _shortening_warning(key, kept, total, resume)
    return index, key, kept, total


def _fit_budget(reply: dict) -> None:
    """
    Shorten pages of records in `reply`, longest first, until the encoded reply fits the budget.

    One page is not always enough. An ANIMATION `render_scene` reply carries `files` and
    `progress` side by side; cutting only the longer of the two left a 250-frame reply at 29,164
    bytes - 3.6x the budget - holding a single usable file path, because the untouched sibling
    was most of the weight. Shortening therefore walks on into the next-largest page.

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
    # Sorting once is enough: draining a page down to its last record only makes it smaller, so no
    # page can overtake one this ordering already placed ahead of it.
    shortened: list[tuple[int, str, int, int]] = []
    for owner, key in sorted(pages, key=_page_bytes, reverse=True):
        measured = _wire_bytes(reply)
        if measured <= REPLY_BYTE_BUDGET:
            return
        cut = _shorten_page(reply, owner, key, measured)
        if cut is not None:
            shortened.append(cut)
    # The loop returns as soon as a shortening is enough, so reaching here means every page has
    # been visited and only the last one's result is still unmeasured.
    if _wire_bytes(reply) <= REPLY_BYTE_BUDGET:
        return
    # Every page is down to its last record and the reply is still too big. Each warning already
    # names what it kept; what it must not also do is hand back an offset, which would send the
    # agent round a loop of replies every one of which is over the budget.
    for index, key, kept, total in shortened:
        reply["warnings"][index] = _shortening_warning(key, kept, total, _NO_RESUME)


def ok(
    data: Any = None,
    *,
    success: bool = True,
    warnings: list[str] | None = None,
    changed_objects: list[str] | None = None,
    changed_resources: list[str] | None = None,
) -> dict:
    """
    Wrap one tool payload in the envelope this module documents.

    `ok()` takes ownership of `data`: shortening an over-budget page rewrites that list inside the
    payload, so a caller that still needs its own dict passes a copy (which is what `envelope_for`
    does with every addon reply).

    Args:
        data: The tool-specific payload. A `warnings` list on it is lifted into the envelope's own
            warnings and dropped from the payload, so one notice is never reported twice.
        success: False when the request reached Blender but produced no effect.
        warnings: The tool's own non-fatal notices, kept ahead of the payload's.
        changed_objects: Names of Blender objects the call created, modified, or deleted.
        changed_resources: Names of non-object datablocks the call touched.

    Returns:
        The envelope, already shortened to fit `REPLY_BYTE_BUDGET`.

    """
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


def envelope_for(
    reply: object,
    *,
    changed_objects: Sequence[str] = (),
    changed_resources: Sequence[str] = (),
    warnings: Sequence[str] = (),
    limit: int = CHANGED_OBJECTS_LIMIT,
) -> dict:
    """
    Shape one addon reply into the envelope: the deterministic half of every tool module's `_call`.

    Deterministic and free of transport, which is all that was ever different between the twelve
    copies this replaced. The reply's own top level is copied before `ok()` takes ownership of it,
    so a caller still holding its reply keeps the dict it sent; a dict nested deeper is shared, and
    a shortening reaching that far does write its pagination keys there.

    Args:
        reply: Whatever the addon returned. A dict is copied and its `changed_objects` and
            `changed_resources` keys move into the envelope; any other value becomes `data` as is.
        changed_objects: Object names to report when the addon names none itself. An addon-supplied
            list replaces this rather than extending it, so a call that changed nothing - a
            cancelled operator, a no-op patch - reports nothing.
        changed_resources: Non-object datablock names, under that same replacement rule.
        warnings: Notices to carry into the envelope. They belong here rather than appended to the
            result, which would land after `ok()` had already measured the reply against its budget.
        limit: Most `changed_objects` names to keep; a longer list is cut to it and a warning names
            the total.

    Returns:
        The `ok()` envelope.

    """
    data: Any = reply
    objects: Sequence[str] = changed_objects
    resources: Sequence[str] = changed_resources
    if isinstance(reply, dict):
        data = {key: value for key, value in reply.items() if key not in _CHANGE_KEYS}
        objects = reply.get("changed_objects", objects)
        resources = reply.get("changed_resources", resources)
    notices = list(warnings)
    if len(objects) > limit:
        notices.append(f"changed_objects lists the first {limit} of {len(objects)} objects")
        objects = objects[:limit]
    return ok(data, warnings=notices, changed_objects=list(objects), changed_resources=list(resources))
