"""
The RNA patch helpers every simulation handler shares: what a patch refuses, and what a failure puts back.

Cloth, liquid and rigid bodies each had a copy of these, and the copies had drifted apart, so each
test here states the one behaviour the shared version keeps.
"""

import sys
import types

import pytest

from conftest import load_addon


def _rna_patch(monkeypatch):
    addon, _bpy = load_addon(monkeypatch, data={})
    return sys.modules[f"{addon.__name__}.handlers.rna_patch"]


class _FakeRnaProperty:
    def __init__(self, *, prop_type="FLOAT", minimum=0.0, maximum=10.0) -> None:
        self.type = prop_type
        self.hard_min = minimum
        self.hard_max = maximum
        self.is_readonly = False
        self.is_array = False
        self.array_length = 0
        self.enum_items = []


def _rna(**properties):
    return types.SimpleNamespace(properties=properties)


class _SequenceOnly:
    """Iterates the way a mathutils Vector does: `__len__` and `__getitem__`, and no `__iter__`."""

    def __init__(self, *items) -> None:
        self._items = items

    def __len__(self) -> int:
        return len(self._items)

    def __getitem__(self, index):
        return self._items[index]


def test_a_patch_with_one_out_of_range_value_writes_nothing(monkeypatch) -> None:
    rna_patch = _rna_patch(monkeypatch)
    owner = types.SimpleNamespace(
        first=1.0, second=2.0, bl_rna=_rna(first=_FakeRnaProperty(), second=_FakeRnaProperty())
    )

    with pytest.raises(ValueError, match="outside Blender's RNA range"):
        rna_patch.patch_rna(owner, {"first": 5.0, "second": 99.0}, {"first", "second"})

    assert owner.first == pytest.approx(1.0)
    assert owner.second == pytest.approx(2.0)


def test_a_write_that_fails_puts_back_the_values_already_written(monkeypatch) -> None:
    rna_patch = _rna_patch(monkeypatch)

    class Owner:
        first = 1.0
        second = 2.0
        bl_rna = _rna(first=_FakeRnaProperty(), second=_FakeRnaProperty())

        def __setattr__(self, name, value) -> None:
            if name == "second" and value == pytest.approx(4.0):
                raise RuntimeError("assignment failed")
            object.__setattr__(self, name, value)

    owner = Owner()
    with pytest.raises(RuntimeError, match="assignment failed"):
        rna_patch.patch_rna(owner, {"first": 3.0, "second": 4.0}, {"first", "second"})

    assert owner.first == pytest.approx(1.0)
    assert owner.second == pytest.approx(2.0)


def test_restore_leaves_a_pointer_to_the_caller_that_holds_its_datablock(monkeypatch) -> None:
    """
    A report records a pointer in reply form, which cannot be assigned back to it.

    Cloth's collision and effector-weight handlers record `collection` that way and reassign the
    datablock they held after restoring; a recorded None written back first would clear it.
    """
    rna_patch = _rna_patch(monkeypatch)
    held = object()
    owner = types.SimpleNamespace(
        distance_min=0.5,
        collection=held,
        bl_rna=_rna(distance_min=_FakeRnaProperty(), collection=_FakeRnaProperty(prop_type="POINTER")),
    )

    rna_patch.restore_rna(
        owner,
        {"distance_min": {"old": 0.015, "new": 0.5}, "collection": {"old": None, "new": "Colliders"}},
    )

    assert owner.distance_min == pytest.approx(0.015)
    assert owner.collection is held


def test_a_vector_read_back_is_its_numbers_not_its_repr(monkeypatch) -> None:
    """A mathutils Vector has no `__iter__`, so a serializer that asks for one reports its repr instead."""
    rna_patch = _rna_patch(monkeypatch)
    settings = types.SimpleNamespace(gravity=_SequenceOnly(0.0, 0.0, -9.81))

    assert rna_patch.read_fields(settings, {"gravity"}) == {"gravity": [0.0, 0.0, -9.81]}
