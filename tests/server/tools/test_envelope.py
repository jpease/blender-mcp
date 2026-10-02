"""
Coverage for the envelope: shaping an addon reply, lifting its `warnings` list, and the byte budget.

`envelope_for` is the half of `_dispatch.call_blender` that has no transport in it, so the rules
the twelve per-package copies used to each restate - an addon list replaces the tool's guess, the
object list is bounded, the reply dict is left alone - are asserted here once instead of per package.
"""

from pydantic_core import to_json

from blender_mcp.server.tools.envelope import CHANGE_LIST_LIMIT, REPLY_BYTE_BUDGET, envelope_for, ok

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
    data = {
        "names": {"items": [f"bone{index}" for index in range(2_000)], "total": 2_000, "offset": 0, "truncated": False}
    }

    result = ok(data)

    assert _wire_bytes(result) <= REPLY_BYTE_BUDGET
    assert result["data"]["names"]["next_offset"] == len(result["data"]["names"]["items"])


def test_a_page_paged_under_a_prefixed_name_is_still_marked_truncated() -> None:
    """
    `get_camera_rig_info` pages its action records as `animation_truncated`/`animation_next_offset`.

    Recognizing only the bare spelling left the reply claiming `animation_truncated: false` about
    a page the budget had just cut, with no offset to resume from.
    """
    data = {
        "animation": _records(_OVER_BUDGET),
        "animation_total": _OVER_BUDGET,
        "animation_offset": 0,
        "animation_truncated": False,
        "animation_next_offset": None,
    }

    result = ok(data)

    assert _wire_bytes(result) <= REPLY_BYTE_BUDGET
    assert result["data"]["animation_truncated"] is True
    assert result["data"]["animation_next_offset"] == len(result["data"]["animation"])


def test_a_page_whose_owner_takes_no_offset_is_marked_truncated_but_offers_none() -> None:
    """
    A bounded sub-list carries `truncated` but no offset, because its tool pages something else.

    `list_libraries(detail=true)` pages each library's `datablocks` beside `limit`,
    `returned_count` and `truncated`; its own `offset` pages libraries. Writing a `next_offset`
    into the datablocks page told the agent to resume at an offset that skips libraries.
    """
    datablocks = {
        "total": 150,
        "by_type": {"MESH": 150},
        "limit": 100,
        "returned_count": 100,
        "truncated": True,
        "records": _records(100),
    }
    data = {
        "libraries": [{"name": "canon.blend", "datablocks": datablocks}],
        "total": 1,
        "offset": 0,
        "limit": 25,
        "returned_count": 1,
        "truncated": False,
        "next_offset": None,
    }

    result = ok(data)
    page = result["data"]["libraries"][0]["datablocks"]

    assert _wire_bytes(result) <= REPLY_BYTE_BUDGET
    assert 0 < len(page["records"]) < 100
    assert page["truncated"] is True
    assert page["returned_count"] == len(page["records"])
    assert "next_offset" not in page
    assert (result["data"]["truncated"], result["data"]["next_offset"]) == (False, None)
    shortened = [warning for warning in result["warnings"] if "was shortened to" in warning]
    assert len(shortened) == 1 and "narrower scope" in shortened[0] and "offset=" not in shortened[0]


def test_a_prefixed_page_without_its_own_offset_offers_no_offset_to_resume_from() -> None:
    """`render_scene(detail=true)` flags `progress_truncated` but takes no progress offset."""
    data = {"files": ["a.png"], "progress": _records(_OVER_BUDGET), "progress_truncated": False}

    result = ok(data)

    assert result["data"]["progress_truncated"] is True
    assert "progress_next_offset" not in result["data"]
    assert not any("offset=" in warning for warning in result["warnings"])


def test_cutting_an_unpaged_sibling_leaves_the_real_pages_resume_point_alone() -> None:
    """
    Bare pagination keys describe the page `returned_count` counts, not every list beside it.

    `list_scene_objects` returned a two-record `objects` page beside every selected object's name;
    cutting that list rewrote the page's `next_offset` to 234, and paging on skipped 232 objects.
    `inspect_lighting_setup` has the same shape: a `lights` page beside every emissive material.
    """
    names = [f"Set_part_{index:04d}_mat" for index in range(_OVER_BUDGET * 2)]
    data = {
        "lights": [{"name": "Key"}, {"name": "Fill"}],
        "emissive_materials": names,
        "offset": _RESUMED_OFFSET,
        "limit": 2,
        "returned_count": 2,
        "truncated": True,
        "next_offset": _RESUMED_OFFSET + 2,
    }

    result = ok(data)

    assert _wire_bytes(result) <= REPLY_BYTE_BUDGET
    assert len(result["data"]["emissive_materials"]) < len(names)
    assert result["data"]["returned_count"] == 2
    assert result["data"]["next_offset"] == _RESUMED_OFFSET + 2


def test_the_largest_list_is_the_one_cut() -> None:
    """Cutting a short sibling page would not bring the reply under budget, so it is left whole."""
    findings = [{"code": "NO_CAMERA"}, {"code": "NO_WORLD"}, {"code": "NO_LIGHTS"}]
    data = {"lights": _records(_OVER_BUDGET), "findings": list(findings)}

    result = ok(data)

    assert _wire_bytes(result) <= REPLY_BYTE_BUDGET
    assert 0 < len(result["data"]["lights"]) < _OVER_BUDGET
    assert result["data"]["findings"] == findings


def test_a_second_page_is_shortened_when_cutting_the_first_one_is_not_enough() -> None:
    """
    An ANIMATION `render_scene` reply carries `files` and `progress` side by side.

    Cutting only the longer of the two left the 250-frame default at 29,164 bytes - 3.6x the
    budget - holding a single usable file path, because the untouched sibling was most of the
    weight. Both pages have to come down, and both have to stay resumable from their own offset.
    """
    data = {
        "files": _records(_OVER_BUDGET),
        "files_total": _OVER_BUDGET,
        "files_offset": 0,
        "files_truncated": False,
        "progress": _records(_OVER_BUDGET // 2),
        "progress_total": 900,
        "progress_offset": _RESUMED_OFFSET,
        "progress_truncated": False,
    }

    result = ok(data)
    payload = result["data"]

    assert _wire_bytes(result) <= REPLY_BYTE_BUDGET
    assert 0 < len(payload["files"]) < _OVER_BUDGET
    assert 0 < len(payload["progress"]) < _OVER_BUDGET // 2
    assert payload["files_truncated"] is True
    assert payload["files_next_offset"] == len(payload["files"])
    assert payload["progress_truncated"] is True
    assert payload["progress_next_offset"] == _RESUMED_OFFSET + len(payload["progress"])
    assert len([warning for warning in result["warnings"] if "was shortened to" in warning]) == 2


def test_each_shortened_pages_warning_names_that_page_and_no_other() -> None:
    """
    Two pages cut in one reply must each be described by their own numbers.

    The counts and the resume offset differ per page - `progress` resumes from its own offset, not
    from zero - so a warning carrying the other page's numbers would send the agent to a page that
    does not exist and tell it the wrong total. Only the pairing is asserted here; the pagination
    keys themselves are covered above.
    """
    data = {
        "files": _records(_OVER_BUDGET),
        "files_offset": 0,
        "files_truncated": False,
        "progress": _records(_OVER_BUDGET // 2),
        "progress_offset": _RESUMED_OFFSET,
        "progress_truncated": False,
    }

    result = ok(data)
    payload = result["data"]

    shortened = [warning for warning in result["warnings"] if "was shortened to" in warning]
    assert shortened == [
        f"files was shortened to {len(payload['files'])} of {_OVER_BUDGET} records to stay within the "
        f"{REPLY_BYTE_BUDGET}-byte reply budget; continue with files_offset={len(payload['files'])}.",
        f"progress was shortened to {len(payload['progress'])} of {_OVER_BUDGET // 2} records to stay "
        f"within the {REPLY_BYTE_BUDGET}-byte reply budget; continue with "
        f"progress_offset={_RESUMED_OFFSET + len(payload['progress'])}.",
    ]


def test_a_camera_rig_page_resumes_from_the_offset_it_was_requested_at() -> None:
    """
    A secondary page resumes from its own offset, through the parameter named after it.

    `get_camera_rig_info` pages `children` through `children_offset`. A page requested at
    children_offset=50 and cut by the budget was resumed from 0 - re-reading the fifty children
    already seen - and the warning said `offset=`, a parameter the tool does not take.
    """
    data = {
        "children": _records(_OVER_BUDGET),
        "children_total": 2 * _OVER_BUDGET,
        "children_offset": 50,
        "children_returned_count": _OVER_BUDGET,
        "children_truncated": True,
        "children_next_offset": 50 + _OVER_BUDGET,
    }

    result = ok(data)
    kept = len(result["data"]["children"])

    assert 0 < kept < _OVER_BUDGET
    assert result["data"]["children_next_offset"] == 50 + kept
    assert any(warning.endswith(f"continue with children_offset={50 + kept}.") for warning in result["warnings"])
    assert not any("offset=" in warning and "children_offset=" not in warning for warning in result["warnings"])


def test_a_reply_too_big_at_one_record_per_page_says_so_rather_than_offering_an_offset() -> None:
    """
    A reply that cannot fit says so, instead of naming an offset to resume from.

    Some replies cannot be brought inside the budget without dropping identifiers, which the
    shortening never does. `continue with offset=1` would be a false way out: the next page of
    one record would be over the budget too, and so would the one after that.
    """
    heavy = [{"note": "x" * REPLY_BYTE_BUDGET}, {"note": "y" * REPLY_BYTE_BUDGET}]
    data = {
        "findings": list(heavy),
        "findings_offset": 0,
        "findings_truncated": False,
        "samples": list(heavy),
        "samples_offset": 0,
        "samples_truncated": False,
    }

    result = ok(data)

    assert len(result["data"]["findings"]) == 1
    assert len(result["data"]["samples"]) == 1
    assert _wire_bytes(result) > REPLY_BYTE_BUDGET
    assert not any("offset=" in warning for warning in result["warnings"])
    floor = "over the budget even with every page cut to one record - request a narrower scope."
    assert [warning.endswith(floor) for warning in result["warnings"]] == [True, True]


def test_a_page_inside_a_dropped_record_is_not_reported_as_shortened() -> None:
    """
    Walking on to the next page has to skip one that is no longer in the reply.

    `_record_pages` lists every page before anything is cut, so the pages inside the records an
    outer shortening dropped cost the reply nothing; announcing a shortening of one of them
    would name a total the client never had a single record of.
    """
    data = {
        "libraries": [
            {"name": "kept", "records": _records(_OVER_BUDGET // 2)},
            {"name": "dropped", "records": _records(_OVER_BUDGET)},
        ],
        "offset": 0,
        "truncated": False,
    }

    result = ok(data)

    assert _wire_bytes(result) <= REPLY_BYTE_BUDGET
    assert [library["name"] for library in result["data"]["libraries"]] == ["kept"]
    assert not any(f"of {_OVER_BUDGET} records" in warning for warning in result["warnings"])


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


def test_a_vector_or_matrix_is_never_cut_as_if_it_were_a_page() -> None:
    """
    `get_character_rig_info` for one bone: once its record lists are spent, the matrix is all that is left.

    Cut to its first row, `matrix_world` no longer holds the bone head's world position, `head` cut
    to one float is not a point, and `lock_rotation` cut to one flag says nothing about Y and Z; the
    reply has to arrive whole and over budget, with the warning.
    """
    matrix = [[1.0, 0.0, 0.0, 0.25], [0.0, 1.0, 0.0, -0.5], [0.0, 0.0, 1.0, 1.75], [0.0, 0.0, 0.0, 1.0]]
    bone = {
        "name": "head",
        "head": [0.25, -0.5, 1.75],
        "matrix_world": matrix,
        "lock_rotation": [True, False, True],
        "note": "z" * REPLY_BYTE_BUDGET,
    }

    result = ok({"pose_bones": [bone]})

    sent = result["data"]["pose_bones"][0]
    assert sent["matrix_world"] == matrix
    assert sent["head"] == [0.25, -0.5, 1.75]
    assert sent["lock_rotation"] == [True, False, True]
    assert any("no page of records" in warning for warning in result["warnings"])


def test_a_numeric_value_is_never_cut_however_long_it_is() -> None:
    """
    Length does not tell a value from a page: an n-gon's vertex loop is one value at any size.

    `get_mesh_data` returns a face's `vertices` whole; cut to a prefix it names a different face,
    one the mesh does not have.
    """
    loop = list(range(REPLY_BYTE_BUDGET // 2))

    result = ok({"faces": [{"index": 0, "vertices": loop, "normal": [0.0, 0.0, 1.0]}]})

    assert result["data"]["faces"][0]["vertices"] == loop
    assert any("no page of records" in warning for warning in result["warnings"])


def test_a_frame_list_is_a_page() -> None:
    """A rigid-body bake keys up to 10,000 frames, and its `frames_truncated` declares the list a page."""
    result = ok({"frames": list(range(REPLY_BYTE_BUDGET)), "frames_truncated": False})

    assert _wire_bytes(result) <= REPLY_BYTE_BUDGET
    assert len(result["data"]["frames"]) < REPLY_BYTE_BUDGET
    assert result["data"]["frames_truncated"] is True
    assert any("frames was shortened" in warning for warning in result["warnings"])


def test_an_undeclared_frames_list_is_one_value() -> None:
    """
    `frames` is a page in a bake and an NLA layer's `[start, end]` interval elsewhere.

    The key alone cannot say which, so only a list its reply declares is cut; the interval cut to
    its start would claim a layer that never ends.
    """
    result = ok({"strip": "Walk", "frames": [1.0, 250.0], "note": "z" * REPLY_BYTE_BUDGET})

    assert result["data"]["frames"] == [1.0, 250.0]
    assert any("no page of records" in warning for warning in result["warnings"])


def test_an_undeclared_list_of_names_is_one_value() -> None:
    """Names are no page by being names: an undeclared list goes out whole, over the budget, with the warning."""
    names = [f"Piece_{index:04d}" for index in range(_OVER_BUDGET * 2)]

    result = ok({"source_object_names": names})

    assert result["data"]["source_object_names"] == names
    assert any("no page of records" in warning for warning in result["warnings"])


def test_a_complete_value_list_is_never_cut_to_make_room() -> None:
    """
    `bake_rigid_bodies_to_keyframes` reports the `channels` it keyed; a list of names is no page.

    With nothing the budget could shorten, the walk used to go on into every list of names, and
    `["location", "rotation_quaternion"]` cut to `["location"]` said rotation was never keyed.
    """
    channels = ["location", "rotation_quaternion"]

    result = ok({"channels": list(channels), "note": "z" * REPLY_BYTE_BUDGET})

    assert result["data"]["channels"] == channels
    assert any("no page of records" in warning for warning in result["warnings"])


def test_a_map_of_frame_lists_is_shortened_by_whole_entries() -> None:
    """
    `bake_ragdoll_to_armature` keys every bone at every sampled frame and reports them per bone.

    The map used to be no page at all, so the walk cut the small `channels` beside it instead and
    the reply stayed over the budget. It is cut by entry count: every bone that stays keeps its
    whole frame list, because a list cut short would claim keys that were never written, and the
    warning counts entries and says to narrow the scope.
    """
    frames = list(range(1, 60))
    keyed = {f"b{index:03d}": list(frames) for index in range(60)}
    channels = ["location", "rotation_quaternion"]

    result = ok({"channels": list(channels), "keyed_frames_by_bone": keyed})
    sent = result["data"]["keyed_frames_by_bone"]

    assert _wire_bytes(result) <= REPLY_BYTE_BUDGET
    assert result["data"]["channels"] == channels
    assert 0 < len(sent) < len(keyed)
    assert list(sent) == list(keyed)[: len(sent)]
    assert all(value == frames for value in sent.values())
    assert [warning for warning in result["warnings"] if "was shortened" in warning] == [
        f"keyed_frames_by_bone was shortened to {len(sent)} of 60 entries to stay within the "
        f"{REPLY_BYTE_BUDGET}-byte reply budget; rerun with a narrower scope to see the rest."
    ]


def test_bare_pagination_declares_the_list_returned_count_counts() -> None:
    """
    A tool's primary page is declared by the bare keys beside it, whatever it is called.

    `returned_count` says which list they page when the owner holds another list as well; the
    other list is a value, and stays whole.
    """
    data = {
        "libraries": _records(_OVER_BUDGET),
        "domains_checked": ["linking", "overrides"],
        "offset": 0,
        "returned_count": _OVER_BUDGET,
        "truncated": False,
        "next_offset": None,
    }

    result = ok(data)
    payload = result["data"]

    assert _wire_bytes(result) <= REPLY_BYTE_BUDGET
    assert 0 < len(payload["libraries"]) < _OVER_BUDGET
    assert (payload["truncated"], payload["next_offset"]) == (True, len(payload["libraries"]))
    assert payload["domains_checked"] == ["linking", "overrides"]


def test_pages_inside_an_undeclared_list_are_found_and_its_records_kept() -> None:
    """An undeclared list of records is never cut, but the pages its records own still are."""
    data = {
        "domains": [
            {"name": "Liquid", "findings": _records(_OVER_BUDGET // 2)},
            {"name": "Smoke", "findings": _records(_OVER_BUDGET // 2)},
        ]
    }

    result = ok(data)
    domains = result["data"]["domains"]

    assert _wire_bytes(result) <= REPLY_BYTE_BUDGET
    assert [domain["name"] for domain in domains] == ["Liquid", "Smoke"]
    assert len(domains[0]["findings"]) + len(domains[1]["findings"]) < _OVER_BUDGET


def test_an_index_list_is_a_page() -> None:
    """A retopology bridge's `created_vertex_indices` grows with the mesh; it lists elements, it is no value."""
    result = ok({"created_vertex_indices": list(range(REPLY_BYTE_BUDGET))})

    assert len(result["data"]["created_vertex_indices"]) < REPLY_BYTE_BUDGET
    assert any("created_vertex_indices was shortened" in warning for warning in result["warnings"])


def test_a_list_of_index_pairs_is_a_page() -> None:
    """`overlap_face_pairs` is one record per overlapping pair of faces, however many the mesh has."""
    pairs = [[index, index + 1] for index in range(REPLY_BYTE_BUDGET)]

    result = ok({"overlap_face_pairs": pairs})

    assert len(result["data"]["overlap_face_pairs"]) < len(pairs)
    assert any("overlap_face_pairs was shortened" in warning for warning in result["warnings"])


def test_a_list_of_name_pairs_is_a_page() -> None:
    """A rigid-body assembly's `initial_aabb_overlap_candidates` names each overlapping pair of pieces."""
    pairs = [[f"Piece_{index:04d}", f"Piece_{index + 1:04d}"] for index in range(_OVER_BUDGET)]

    result = ok({"initial_aabb_overlap_candidates": pairs})
    sent = result["data"]["initial_aabb_overlap_candidates"]

    assert _wire_bytes(result) <= REPLY_BYTE_BUDGET
    assert 0 < len(sent) < len(pairs)
    assert all(len(pair) == 2 for pair in sent)


def test_an_addon_change_list_replaces_the_tools_own_guess() -> None:
    """The addon knows what actually changed; a tool's pre-call guess is only a fallback."""
    result = envelope_for(
        {"object": "Cube", "changed_objects": ["Cube.001"], "changed_resources": ["Rust"]},
        changed_objects=["Cube"],
        changed_resources=["Metal"],
    )

    assert result["changed_objects"] == ["Cube.001"]
    assert result["changed_resources"] == ["Rust"]
    assert result["data"] == {"object": "Cube"}


def test_an_addon_reporting_nothing_changed_overrides_the_guess_with_nothing() -> None:
    """A cancelled operator returns empty lists, and reporting the requested target would be a lie."""
    result = envelope_for({"cancelled": True, "changed_objects": []}, changed_objects=["Cube"])

    assert result["changed_objects"] == []


def test_a_tools_own_change_list_is_used_when_the_addon_names_none() -> None:
    result = envelope_for({"object": "Cube"}, changed_objects=["Cube"], changed_resources=["Metal"])

    assert result["changed_objects"] == ["Cube"]
    assert result["changed_resources"] == ["Metal"]


def test_a_long_change_list_is_bounded_and_the_warning_names_the_total() -> None:
    """Linking a set changes hundreds of objects; the names would sit in the agent's context all session."""
    names = [f"Part{index:03d}_geo" for index in range(480)]

    result = envelope_for({"changed_objects": names})

    assert result["changed_objects"] == names[:CHANGE_LIST_LIMIT]
    assert result["warnings"] == [f"changed_objects lists the first {CHANGE_LIST_LIMIT} of 480 objects"]


def test_a_long_resource_list_is_bounded_the_same_way() -> None:
    """`changed_resources` sits outside `data`, where the budget never reaches, so it needs its own cap."""
    names = [f"Mat{index:04d}" for index in range(2_000)]

    result = envelope_for({"changed_objects": ["Cube"], "changed_resources": names})

    assert result["changed_resources"] == names[:CHANGE_LIST_LIMIT]
    assert result["changed_objects"] == ["Cube"]
    assert result["warnings"] == [f"changed_resources lists the first {CHANGE_LIST_LIMIT} of 2000 resources"]
    assert _wire_bytes(result) <= REPLY_BYTE_BUDGET


def test_a_change_list_exactly_at_the_limit_is_sent_whole_and_unremarked() -> None:
    names = [f"Part{index:03d}_geo" for index in range(CHANGE_LIST_LIMIT)]

    result = envelope_for({"changed_objects": names, "changed_resources": names})

    assert result["changed_objects"] == names
    assert result["changed_resources"] == names
    assert result["warnings"] == []


def test_a_non_dict_reply_becomes_the_payload_as_it_stands() -> None:
    """Not every addon command answers with a dict; a list or a scalar is still the payload."""
    assert envelope_for(["Cube", "Sphere"])["data"] == ["Cube", "Sphere"]
    assert envelope_for(None)["data"] is None
    assert envelope_for(7, changed_objects=["Cube"])["changed_objects"] == ["Cube"]


def test_the_addon_reply_is_left_untouched() -> None:
    """The reply is the caller's; shaping it in place would corrupt a retry or a shared canned reply."""
    reply = {"object": "Cube", "changed_objects": ["Cube"], "records": _records(_OVER_BUDGET)}

    envelope_for(reply)

    assert set(reply) == {"object", "changed_objects", "records"}
    assert len(reply["records"]) == _OVER_BUDGET


def test_warnings_reach_the_envelope_before_it_is_measured() -> None:
    """A warning appended after `ok()` returned would push a reply that just fitted back over budget."""
    result = envelope_for({"records": _records(_OVER_BUDGET)}, warnings=["topology indices are stale"])

    assert result["warnings"][0] == "topology indices are stale"
    assert _wire_bytes(result) <= REPLY_BYTE_BUDGET
