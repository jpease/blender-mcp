from blender_mcp.server.tools.envelope import ok
from blender_mcp.server.tools.nd import _CANCELLED_WARNING, _nd_outcome


def test_ok_success_false_reports_not_ok() -> None:
    result = ok({"a": 1}, success=False)

    assert result["ok"] is False
    assert result["data"] == {"a": 1}


def test_ok_default_success_is_unchanged() -> None:
    result = ok({"a": 1})

    assert result["ok"] is True


def test_ok_defaults_changed_resources_to_empty_list() -> None:
    result = ok({"a": 1})

    assert result["changed_resources"] == []


def test_ok_reports_changed_resources_alongside_changed_objects() -> None:
    result = ok({"a": 1}, changed_objects=["Cube"], changed_resources=["Material.001"])

    assert result["changed_objects"] == ["Cube"]
    assert result["changed_resources"] == ["Material.001"]


def test_nd_outcome_cancelled_reports_not_ok_and_no_changed_objects() -> None:
    result = _nd_outcome({"cancelled": True}, changed_objects=["a", "b"])

    assert result["ok"] is False
    assert result["changed_objects"] == []
    assert result["warnings"] == [_CANCELLED_WARNING]


def test_nd_outcome_not_cancelled_reports_ok_and_changed_objects() -> None:
    result = _nd_outcome({"cancelled": False}, changed_objects=["a"])

    assert result["ok"] is True
    assert result["changed_objects"] == ["a"]
    assert result["warnings"] == []


def test_nd_outcome_passes_through_changed_resources() -> None:
    result = _nd_outcome({"cancelled": False}, changed_objects=["a"], changed_resources=["Mat.1"])

    assert result["changed_resources"] == ["Mat.1"]


def test_nd_outcome_cancelled_reports_no_changed_resources() -> None:
    result = _nd_outcome({"cancelled": True}, changed_resources=["Mat.1"])

    assert result["changed_resources"] == []


def test_nd_outcome_publishes_the_change_list_a_handler_names_itself_in_place_of_the_targets() -> None:
    """nd_clean_utils names the objects that lost a modifier; they reach the envelope, not `data`."""
    result = _nd_outcome({"cancelled": False, "changed_objects": ["Panel 0", "Panel 1"]}, changed_objects=["a"])

    assert result["changed_objects"] == ["Panel 0", "Panel 1"]
    assert "changed_objects" not in result["data"]


def test_nd_outcome_cancelled_drops_a_change_list_the_handler_named() -> None:
    result = _nd_outcome({"cancelled": True, "changed_objects": ["Panel 0"]})

    assert result["changed_objects"] == []
    assert "changed_objects" not in result["data"]
