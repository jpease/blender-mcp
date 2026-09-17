"""
`object_lookup.find_object`: one deterministic object per name after a library override.

The stub keys objects by name and by `(name, library_filepath)`, `None` meaning
local, as Blender does, and lists the linked original first so order cannot decide.
"""

from __future__ import annotations

import types

import pytest

from conftest import load_addon_source_module


def _object_lookup() -> types.ModuleType:
    """
    Load the `bpy`-free lookup module straight from the addon source.

    Returns:
        types.ModuleType: `object_lookup`.

    """
    return load_addon_source_module("object_lookup.py", "addon_object_lookup_under_test")


class _Objects:
    """`bpy.data.objects` as far as lookup goes: `get` by name or `(name, library_filepath)`, and `values`."""

    def __init__(self, *objects: types.SimpleNamespace) -> None:
        self._objects = list(objects)

    def get(self, key: object) -> types.SimpleNamespace | None:
        name, filepath = key if isinstance(key, tuple) else (key, ...)
        for obj in self._objects:
            if obj.name != name:
                continue
            obj_filepath = obj.library.filepath if obj.library else None
            if filepath is ... or obj_filepath == filepath:
                return obj
        return None

    def values(self) -> list[types.SimpleNamespace]:
        return list(self._objects)


_LIBRARY_UIDS = iter(range(100, 10_000))


def _obj(name: str, library: str | None = None, *, override: bool = False) -> types.SimpleNamespace:
    lib = (
        types.SimpleNamespace(name=library, filepath=f"//{library}", session_uid=next(_LIBRARY_UIDS))
        if library
        else None
    )
    return types.SimpleNamespace(name=name, library=lib, override_library=object() if override else None)


def test_the_local_override_wins_over_a_linked_original_listed_first() -> None:
    """After Route C, a name means the editable override, whatever order Blender keeps."""
    linked = _obj("HeroCam", "canon.blend")
    override = _obj("HeroCam", override=True)

    assert _object_lookup().find_object(_Objects(linked, override), "HeroCam") is override


def test_a_single_linked_object_is_returned_when_no_local_one_has_the_name() -> None:
    """A linked-only name is still unambiguous and is resolved."""
    linked = _obj("Prop", "canon.blend")

    assert _object_lookup().find_object(_Objects(linked), "Prop") is linked


def test_two_linked_objects_with_one_name_and_no_local_one_are_refused() -> None:
    """Choosing a library would be a guess; the refusal names each by leaf and uid."""
    first, second = _obj("Prop", "/studio/a/canon.blend"), _obj("Prop", "b.blend")

    with pytest.raises(ValueError, match="more than one library") as refusal:
        _object_lookup().find_object(_Objects(first, second), "Prop")

    message = str(refusal.value)
    assert "/studio" not in message
    assert f"'canon.blend' (session_uid {first.library.session_uid})" in message
    assert f"'b.blend' (session_uid {second.library.session_uid})" in message
    assert "2 of them" in message


def test_the_ambiguity_refusal_is_bounded_however_many_libraries_link_the_name() -> None:
    """However many libraries share the name, the refusal lists `MAX_CANDIDATES` and counts the rest."""
    objects = _Objects(*(_obj("HeroCam", f"lib{index:03d}.blend") for index in range(300)))
    shown = load_addon_source_module("candidates.py", "addon_candidates_for_lookup").MAX_CANDIDATES
    # Ten entries of about 30 bytes each plus the fixed text.
    ceiling_bytes = 1_000

    with pytest.raises(ValueError) as refusal:
        _object_lookup().find_object(objects, "HeroCam")

    message = str(refusal.value)
    assert "300 of them" in message
    assert message.count("session_uid") == shown
    assert f", and {300 - shown} more" in message
    assert len(message.encode()) < ceiling_bytes


def test_a_missing_name_is_none() -> None:
    """Callers keep their own not-found message."""
    assert _object_lookup().find_object(_Objects(_obj("A")), "B") is None
