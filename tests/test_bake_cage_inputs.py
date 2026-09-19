"""
`create_bake_cage` rejects a missing high-poly list instead of tripping over it.

The handler defaults `high_poly_object_names` to None and then iterated it
directly, so a payload that omitted the field raised `TypeError: 'NoneType'
object is not iterable` one line before the ValueError written to explain the
mistake. Its sibling `bake_retopology_maps` had the guard; this one did not.
"""

from __future__ import annotations

import sys
import types

from typing import Any

import pytest

from test_mutation_transaction import _load_addon


def _server(monkeypatch: pytest.MonkeyPatch) -> Any:
    """
    Load the add-on over a fake bpy holding one mesh object named "LowPoly".

    Args:
        monkeypatch: Fixture used to install the stubs.

    Returns:
        Any: A `BlenderMCPServer` whose retopology handlers are reachable.

    """
    low_poly = types.SimpleNamespace(name="LowPoly", type="MESH")
    # `bpy.data.objects` is a name-keyed collection, and the handler reaches it
    # only through `.get`.
    objects = types.SimpleNamespace(get={"LowPoly": low_poly}.get)
    addon, _bpy = _load_addon(monkeypatch, data={"objects": objects})
    server_core = sys.modules[f"{addon.__name__}.server_core"]
    return server_core.BlenderMCPServer()


def test_an_omitted_high_poly_list_is_refused_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """The caller learns which field is missing, rather than seeing an iteration failure."""
    server = _server(monkeypatch)

    with pytest.raises(ValueError, match="high_poly_object_names must contain at least one mesh"):
        server.create_bake_cage("LowPoly")


def test_an_empty_high_poly_list_is_refused_the_same_way(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty list and an omitted one are the same mistake and must read the same."""
    server = _server(monkeypatch)

    with pytest.raises(ValueError, match="high_poly_object_names must contain at least one mesh"):
        server.create_bake_cage("LowPoly", high_poly_object_names=[])
