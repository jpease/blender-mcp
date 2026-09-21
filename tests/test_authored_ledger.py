"""
The bounded provenance ledger `save_shot` writes into the `.blend`.

Every assertion here is about what a recipient reads out of that file: which
datablocks the session claims to have made, in what order, and whether the claim is
complete. The ledger used to keep that answer in a list plus a parallel membership
set, co-evicted by hand; the invariant "the keys mirror the entries" was something
the code had to remember rather than something the data structure guaranteed, and
forgetting it would have made an evicted name unrecordable forever.

`authored.py` is free of `bpy`, so none of this needs a Blender stub.
"""

import pytest

from conftest import load_addon_source_module


@pytest.fixture(name="authored")
def _authored():
    """
    Load a private copy of the ledger module.

    A fresh execution per test, because the ledger is process-wide state.

    Returns:
        ModuleType: The freshly executed `authored` module.

    """
    return load_addon_source_module("authored.py", "blender_mcp_addon_authored_test")


def test_records_arrive_oldest_first_and_a_repeat_is_one_datablock(authored) -> None:
    """One datablock touched twice is one provenance entry, not two."""
    authored.record([{"collection": "objects", "name": "Hero"}, {"collection": "meshes", "name": "Hero"}])
    authored.record([{"collection": "objects", "name": "Hero"}, {"collection": "objects", "name": "Prop"}])

    assert authored.snapshot() == [
        {"collection": "objects", "name": "Hero"},
        {"collection": "meshes", "name": "Hero"},
        {"collection": "objects", "name": "Prop"},
    ]
    assert authored.was_truncated() is False


def test_overflow_drops_the_oldest_and_stops_claiming_a_complete_history(authored) -> None:
    """
    A file that claimed a complete provenance block it does not have would mislead its recipient.

    The cap is what keeps a long session's ledger fit to save, so the honesty flag
    is the other half of it.
    """
    authored.record({"collection": "objects", "name": f"obj{index}"} for index in range(authored.MAX_TRACKED_AUTHORED))
    assert authored.was_truncated() is False

    authored.record([{"collection": "objects", "name": "newest"}])

    entries = authored.snapshot()
    assert len(entries) == authored.MAX_TRACKED_AUTHORED
    assert entries[0] == {"collection": "objects", "name": "obj1"}, "eviction must drop the oldest record"
    assert entries[-1] == {"collection": "objects", "name": "newest"}
    assert authored.was_truncated() is True


def test_a_name_that_was_evicted_can_be_recorded_again(authored) -> None:
    """
    Blender frees a name for reuse, and an evicted record must not suppress its successor.

    This is the co-eviction the two-structure ledger had to perform by hand: keeping
    the key of a dropped entry would have hidden every later datablock of that name,
    for the rest of the session, from the provenance block.
    """
    authored.record({"collection": "objects", "name": f"obj{index}"} for index in range(authored.MAX_TRACKED_AUTHORED))
    authored.record([{"collection": "objects", "name": "filler"}])
    assert {"collection": "objects", "name": "obj0"} not in authored.snapshot(), "obj0 was meant to be evicted"

    authored.record([{"collection": "objects", "name": "obj0"}])

    assert authored.snapshot()[-1] == {"collection": "objects", "name": "obj0"}


def test_the_snapshot_cannot_be_used_to_edit_the_ledger(authored) -> None:
    """A caller holding the reply must not be able to rewrite what the file will claim."""
    authored.record([{"collection": "objects", "name": "Hero"}])

    snapshot = authored.snapshot()
    snapshot.append({"collection": "objects", "name": "Forged"})
    snapshot[0]["name"] = "Renamed"

    assert authored.snapshot() == [{"collection": "objects", "name": "Hero"}]


def test_clearing_forgets_the_entries_and_the_truncation_claim(authored) -> None:
    """The database this ledger described is gone, so a stale `truncated` would describe nothing."""
    authored.record(
        {"collection": "objects", "name": f"obj{index}"} for index in range(authored.MAX_TRACKED_AUTHORED + 1)
    )
    assert authored.was_truncated() is True

    authored.clear()

    assert authored.snapshot() == []
    assert authored.was_truncated() is False
