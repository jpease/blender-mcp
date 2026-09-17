"""
`candidates`: the candidate list is safe to publish.

Each name is reduced, the list is bounded with a count of the rest, every entry
carries a uid, and a non-library name survives.
"""

from __future__ import annotations

import types

from conftest import load_addon_source_module


def _candidates() -> types.ModuleType:
    """
    Load the `bpy`-free candidates module straight from the addon source.

    Returns:
        types.ModuleType: `candidates`.

    """
    return load_addon_source_module("candidates.py", "addon_candidates_under_test")


def _datablock(name: str, uid: int, id_type: str | None = None) -> types.SimpleNamespace:
    return types.SimpleNamespace(name=name, session_uid=uid, id_type=id_type)


def test_a_non_library_name_is_published_not_blanked() -> None:
    """A name string passed where a datablock belongs would blank every name."""
    text = _candidates().describe_candidates([_datablock("HeroCam", 7, "OBJECT")])

    assert text == "'HeroCam' (session_uid 7)"


def test_describe_candidates_reduces_a_library_name_to_a_leaf_by_its_id_type() -> None:
    """`display_name` applies the leaf rule when the datablock reports `id_type == 'LIBRARY'`."""
    text = _candidates().describe_candidates([_datablock("/studio/vault/canon.blend", 3, "LIBRARY")])

    assert text == "'canon.blend' (session_uid 3)"


def test_describe_library_candidates_reduces_to_a_leaf_without_consulting_id_type() -> None:
    """A library that does not report its discriminator is still held to the leaf rule."""
    text = _candidates().describe_library_candidates([_datablock("/studio/vault/canon.blend", 3)])

    assert "/studio" not in text
    assert text == "'canon.blend' (session_uid 3)"


def test_the_list_is_bounded_and_reports_how_many_were_left_out() -> None:
    """Past the cap, the list shows the first `MAX_CANDIDATES` and counts the rest."""
    module = _candidates()
    libraries = [_datablock(f"lib{index:02d}.blend", index) for index in range(12)]

    text = module.describe_library_candidates(libraries)

    assert text.count("session_uid") == module.MAX_CANDIDATES
    assert text.endswith(", and 2 more")
    assert "lib11.blend" not in text


def test_candidates_that_reduce_to_one_name_stay_distinguishable_by_uid() -> None:
    """Hostile names collapse to the sentinel; the uid still tells them apart."""
    libraries = [_datablock("../../secret/a:b", uid) for uid in (41, 42)]

    text = _candidates().describe_library_candidates(libraries)

    assert "session_uid 41" in text and "session_uid 42" in text
    assert "secret" not in text
