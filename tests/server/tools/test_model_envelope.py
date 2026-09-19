"""
Server-layer coverage for model.py's apply=True/False STALE_INDEX_WARNING behavior.

Unlike tests/test_mesh_model_tools.py (which exercises the Blender-side addon handlers
via a faked bpy), these tests call the MCP tool functions in
blender_mcp.server.tools.model directly, with get_blender_connection replaced by a stub -
mirroring tests/test_nd_tool_outcome.py's approach of testing server-layer envelope logic
without a real socket.
"""

import asyncio

import pytest

from blender_mcp.server.tools import model
from blender_mcp.server.tools.envelope import STALE_INDEX_WARNING


class _StubConnection:
    def __init__(self, result: dict) -> None:
        self.result = result

    def send_command(self, _command: str, _params: dict | None = None) -> dict:
        return self.result


APPLY_CAPABLE_TOOLS = [
    (model.add_radial_array_modifier, {"object_name": "Cube", "radius": 2.0}),
]


@pytest.mark.parametrize("tool_fn,kwargs", APPLY_CAPABLE_TOOLS)
def test_apply_true_includes_stale_index_warning(monkeypatch, tool_fn, kwargs) -> None:
    monkeypatch.setattr(model, "get_blender_connection", lambda: _StubConnection({"name": "Cube"}))

    result = asyncio.run(tool_fn(ctx=None, apply=True, **kwargs))

    assert result["warnings"] == [STALE_INDEX_WARNING]


@pytest.mark.parametrize("tool_fn,kwargs", APPLY_CAPABLE_TOOLS)
def test_apply_false_omits_stale_index_warning(monkeypatch, tool_fn, kwargs) -> None:
    monkeypatch.setattr(model, "get_blender_connection", lambda: _StubConnection({"name": "Cube"}))

    result = asyncio.run(tool_fn(ctx=None, apply=False, **kwargs))

    assert result["warnings"] == []
