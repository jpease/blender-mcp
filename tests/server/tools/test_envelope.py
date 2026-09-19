"""Coverage for the envelope: lifting an addon `warnings` list, and the per-reply byte budget."""

from pydantic_core import to_json

from blender_mcp.server.tools.envelope import REPLY_BYTE_BUDGET, ok

# A page this long passes the budget whatever the exact record size is.
_OVER_BUDGET = 400
# The resumed page's own starting offset, so next_offset cannot pass by counting from zero.
_RESUMED_OFFSET = 500


def _wire_bytes(reply: dict) -> int:
    """
    Measure a reply the way FastMCP sends it.

    Args:
        reply: The envelope `ok()` built.

    Returns:
        The byte length of `pydantic_core.to_json(reply, fallback=str, indent=2)`, which is what
        `mcp.server.fastmcp.utilities.func_metadata` puts on the wire.

    """
    return len(to_json(reply, fallback=str, indent=2))


def _records(count: int) -> list[dict]:
    """
    Build records big enough that a page of them passes the budget.

    Args:
        count: How many records to build.

    Returns:
        Records of roughly 300 wire bytes each.

    """
    return [
        {"name": f"DEF-spine.{index:03d}", "matrix": [[0.8414709848078965] * 4] * 2, "deform": True}
        for index in range(count)
    ]


def test_ok_lifts_data_warnings_into_envelope_and_drops_the_key() -> None:
    result = ok({"name": "Cube", "warnings": ["Undo checkpoint unavailable (global undo is disabled)"]})

    assert result["warnings"] == ["Undo checkpoint unavailable (global undo is disabled)"]
    assert result["data"] == {"name": "Cube"}  # the key is removed from data
    assert result["ok"] is True


def test_ok_merges_data_warnings_after_tool_supplied_warnings() -> None:
    result = ok(
        {"name": "Cube", "warnings": ["from addon"]},
        warnings=["from tool"],
    )

    assert result["warnings"] == ["from tool", "from addon"]
    assert "warnings" not in result["data"]


def test_ok_leaves_non_dict_data_and_missing_warnings_untouched() -> None:
    assert ok(["a", "b"])["data"] == ["a", "b"]
    assert ok(["a", "b"])["warnings"] == []
    assert ok({"name": "Cube"})["data"] == {"name": "Cube"}
    assert ok({"name": "Cube"})["warnings"] == []


def test_ok_ignores_non_list_warnings_on_data() -> None:
    # A tool payload that happens to carry a scalar `warnings` field is left as
    # data; only a list is treated as liftable notices.
    result = ok({"warnings": "not a list"})

    assert result["data"] == {"warnings": "not a list"}
    assert result["warnings"] == []


def test_a_reply_within_the_budget_is_sent_whole() -> None:
    """The budget must not touch the median reply; it exists for the few that are pages of records."""
    data = {"objects": {"items": _records(3), "total": 3, "offset": 0, "limit": 25, "truncated": False}}

    result = ok(data)

    assert result["data"]["objects"]["items"] == data["objects"]["items"]
    assert result["warnings"] == []


def test_an_oversized_record_page_is_cut_to_the_budget_and_stays_resumable() -> None:
    """
    A page too big for the budget is shortened, not dropped, and says where to resume.

    `truncated`/`next_offset` is the pagination contract the server instructions already
    describe, so a client that follows it keeps every record.
    """
    data = {
        "bones": {
            "items": _records(_OVER_BUDGET),
            "total": _OVER_BUDGET,
            "offset": 0,
            "limit": _OVER_BUDGET,
            "truncated": False,
        },
        "armature_object": "HeroRig",
    }

    result = ok(data)
    page = result["data"]["bones"]

    assert _wire_bytes(result) <= REPLY_BYTE_BUDGET
    assert 0 < len(page["items"]) < _OVER_BUDGET
    assert page["truncated"] is True
    assert page["next_offset"] == len(page["items"])
    assert page["total"] == _OVER_BUDGET
    assert result["data"]["armature_object"] == "HeroRig"
    assert any("budget" in warning and str(_OVER_BUDGET) in warning for warning in result["warnings"])


def test_a_resumed_page_continues_from_the_offset_it_was_given() -> None:
    """`next_offset` must count from the page's own offset, not from zero."""
    data = {
        "elements": {
            "items": _records(_OVER_BUDGET),
            "total": 900,
            "offset": _RESUMED_OFFSET,
            "limit": _OVER_BUDGET,
            "truncated": True,
        }
    }

    page = ok(data)["data"]["elements"]

    assert page["next_offset"] == _RESUMED_OFFSET + len(page["items"])


def test_the_keys_the_shortening_adds_are_inside_the_budget_it_measured() -> None:
    """
    With small records there is no slack to absorb a key written after the measurement.

    `next_offset` is a key many payloads do not carry until a page is shortened, so writing it
    afterwards puts the reply back over the budget by its own length.
    """
    data = {"names": {"items": [f"bone{index}" for index in range(2_000)], "total": 2_000, "truncated": False}}

    result = ok(data)

    assert _wire_bytes(result) <= REPLY_BYTE_BUDGET
    assert result["data"]["names"]["next_offset"] == len(result["data"]["names"]["items"])


def test_a_page_paged_under_a_prefixed_name_is_still_marked_truncated() -> None:
    """
    `inspect_lighting_setup` pages its inventory as `lights_truncated`/`lights_next_offset`.

    Recognizing only the bare spelling left the reply claiming `lights_truncated: false` about a
    page the budget had just cut, with no offset to resume from.
    """
    data = {
        "lights": _records(_OVER_BUDGET),
        "lights_total": _OVER_BUDGET,
        "lights_offset": 0,
        "lights_truncated": False,
        "lights_next_offset": None,
    }

    result = ok(data)

    assert _wire_bytes(result) <= REPLY_BYTE_BUDGET
    assert result["data"]["lights_truncated"] is True
    assert result["data"]["lights_next_offset"] == len(result["data"]["lights"])


def test_the_largest_list_is_the_one_cut() -> None:
    """Cutting a short sibling list would not bring the reply under budget."""
    data = {
        "lights": {"items": _records(_OVER_BUDGET)},
        "domains_checked": ["lighting", "cameras", "geometry"],
    }

    result = ok(data)

    assert _wire_bytes(result) <= REPLY_BYTE_BUDGET
    assert result["data"]["domains_checked"] == ["lighting", "cameras", "geometry"]


def test_a_page_nested_inside_a_single_record_is_found() -> None:
    """
    `list_libraries` returns one library record whose own datablock list is the whole payload.

    A walk that only descends through dicts sees a one-entry list and stops, leaving the reply
    unbounded, which is exactly how that tool measured at 20,960 bytes.
    """
    data = {
        "libraries": [{"name": "canon.blend", "datablocks": _records(_OVER_BUDGET)}],
        "total": 1,
        "offset": 0,
    }

    result = ok(data)

    assert _wire_bytes(result) <= REPLY_BYTE_BUDGET
    assert result["data"]["libraries"][0]["name"] == "canon.blend"
    assert 0 < len(result["data"]["libraries"][0]["datablocks"]) < _OVER_BUDGET


def test_an_unpaginated_oversized_list_is_cut_and_says_to_narrow_the_scope() -> None:
    """
    Not every oversized list carries pagination keys; the reply must still be bounded.

    Inventing `next_offset` for a list the tool cannot resume would send the client back for
    records it will never get, so the warning names the scope instead.
    """
    result = ok({"findings": _records(_OVER_BUDGET)})

    assert _wire_bytes(result) <= REPLY_BYTE_BUDGET
    assert 0 < len(result["data"]["findings"]) < _OVER_BUDGET
    assert any("narrower" in warning for warning in result["warnings"])


def test_a_single_record_too_big_for_the_budget_is_still_reported() -> None:
    """One record over budget is a real reply; the client needs it, plus the warning."""
    result = ok({"findings": [{"note": "x" * (REPLY_BYTE_BUDGET * 2)}]})

    assert len(result["data"]["findings"]) == 1
    assert any("budget" in warning for warning in result["warnings"])


def test_the_budget_leaves_a_flat_oversized_reply_alone() -> None:
    """
    With no record list to cut, the reply is sent whole with a warning.

    Silently dropping fields from a flat payload would make the reply wrong rather than short.
    """
    result = ok({"note": "y" * (REPLY_BYTE_BUDGET * 2)})

    assert len(result["data"]["note"]) == REPLY_BYTE_BUDGET * 2
    assert any("budget" in warning for warning in result["warnings"])
