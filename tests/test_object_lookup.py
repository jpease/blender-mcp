"""
`object_lookup.find_object`: one deterministic object per name after a library override.

Blender 5.2.2 keys `bpy.data.objects` by `(name, library_filepath)` as well as by
name, with `None` meaning local. The stub below models exactly that, so the rule
is tested without relying on list order: the linked original is inserted first.
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


def _obj(name: str, library: str | None = None, *, override: bool = False) -> types.SimpleNamespace:
    lib = types.SimpleNamespace(name=library, filepath=f"//{library}") if library else None
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
    """Choosing between libraries would be a guess; the refusal names the libraries, not paths."""
    objects = _Objects(_obj("Prop", "/studio/a/canon.blend"), _obj("Prop", "b.blend"))

    with pytest.raises(ValueError, match="more than one library") as refusal:
        _object_lookup().find_object(objects, "Prop")

    assert "/studio" not in str(refusal.value)
    assert "canon.blend" in str(refusal.value) and "b.blend" in str(refusal.value)


def test_a_missing_name_is_none() -> None:
    """Callers keep their own not-found message."""
    assert _object_lookup().find_object(_Objects(_obj("A")), "B") is None
