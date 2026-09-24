# ruff: file-ignore[yoda-conditions]
"""
Regression coverage for the eleven file-lifecycle and linking tools.

Tests patch `_dispatch.get_blender_connection` - the one seam every tool dispatches through -
with a `_Connection` stand-in and call each tool coroutine directly. The
`stub_blender_connection` fixture patches the same name; these tests want a per-test reply
rather than its recorded echo.
"""

import asyncio

from typing import get_type_hints

import pytest

from mcp.server.fastmcp.exceptions import ToolError
from pydantic import TypeAdapter, ValidationError
from test_mutation_transaction import _load_addon

from blender_mcp.server.connection import BlenderOperationError
from blender_mcp.server.tools import _dispatch, _documentation, file_lifecycle
from blender_mcp.server.tools.envelope import CHANGED_OBJECTS_LIMIT

FILE_LIFECYCLE_COMMANDS = {
    "get_session_info",
    "open_shot",
    "save_shot",
    "reset_session",
    "link_canon_library",
    "create_override",
    "list_libraries",
    "reload_library",
    "relocate_library",
    "unlink_libraries",
    "inspect_delivery",
}


class _Connection:
    def __init__(self, result: object = None) -> None:
        self.calls = []
        self._result = result

    def send_command(self, command, params):
        self.calls.append((command, params))
        return {} if self._result is None else self._result


class _FailingConnection:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def send_command(self, _command, _params):
        raise self._exc


def test_file_lifecycle_tools_are_registered_and_dispatched(monkeypatch) -> None:
    addon, _bpy = _load_addon(monkeypatch, data={})
    server = addon.BlenderMCPServer()

    assert FILE_LIFECYCLE_COMMANDS <= set(file_lifecycle.mcp._tool_manager._tools)
    assert FILE_LIFECYCLE_COMMANDS <= set(server._build_command_handlers())
    read_only = {"get_session_info", "list_libraries", "inspect_delivery"}
    assert all(server.command_spec(name).read_only for name in read_only)
    assert not {name for name in FILE_LIFECYCLE_COMMANDS - read_only if server.command_spec(name).read_only}


def test_get_session_info_forwards_no_params(monkeypatch) -> None:
    connection = _Connection()
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    asyncio.run(file_lifecycle.get_session_info(ctx=None))

    command, params = connection.calls[0]
    assert command == "get_session_info"
    assert params == {}


def test_open_shot_defaults(monkeypatch) -> None:
    """The unsaved-work guard's safe default is False -- an unpinned default here loses work silently."""
    connection = _Connection()
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    asyncio.run(file_lifecycle.open_shot(ctx=None, filepath="/canon/shot.blend"))

    _command, params = connection.calls[0]
    assert params == {"filepath": "/canon/shot.blend", "load_ui": False, "discard_unsaved": False}


def test_open_shot_forwards_every_parameter(monkeypatch) -> None:
    connection = _Connection()
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    asyncio.run(file_lifecycle.open_shot(ctx=None, filepath="/canon/shot.blend", load_ui=True, discard_unsaved=True))

    command, params = connection.calls[0]
    assert command == "open_shot"
    assert params == {"filepath": "/canon/shot.blend", "load_ui": True, "discard_unsaved": True}


def test_save_shot_forwards_every_parameter(monkeypatch) -> None:
    connection = _Connection()
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    asyncio.run(
        file_lifecycle.save_shot(
            ctx=None,
            filepath="/canon/shot.blend",
            compress=True,
            relative_remap=True,
            confirm_overwrite=True,
            create_directories=True,
            write_provenance=False,
            provenance_checksums=False,
        )
    )

    command, params = connection.calls[0]
    assert command == "save_shot"
    assert params == {
        "filepath": "/canon/shot.blend",
        "compress": True,
        "relative_remap": True,
        "confirm_overwrite": True,
        "create_directories": True,
        "write_provenance": False,
        "provenance_checksums": False,
    }


def test_save_shot_default_filepath_is_none(monkeypatch) -> None:
    connection = _Connection()
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    asyncio.run(file_lifecycle.save_shot(ctx=None))

    command, params = connection.calls[0]
    assert command == "save_shot"
    assert params == {
        "filepath": None,
        "compress": False,
        "relative_remap": False,
        "confirm_overwrite": False,
        "create_directories": False,
        "write_provenance": True,
        "provenance_checksums": False,
    }


def test_reset_session_defaults(monkeypatch) -> None:
    """The whole-database discard's safe default is False -- an unpinned default here discards work silently."""
    connection = _Connection()
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    asyncio.run(file_lifecycle.reset_session(ctx=None))

    _command, params = connection.calls[0]
    assert params == {"confirm": False}


def test_reset_session_forwards_confirm(monkeypatch) -> None:
    connection = _Connection()
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    asyncio.run(file_lifecycle.reset_session(ctx=None, confirm=True))

    command, params = connection.calls[0]
    assert command == "reset_session"
    assert params == {"confirm": True}


def test_link_canon_library_forwards_every_parameter(monkeypatch) -> None:
    connection = _Connection()
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    asyncio.run(
        file_lifecycle.link_canon_library(
            ctx=None,
            filepath="/canon/hero.blend",
            collections=["CanonHero"],
            objects=["Prop"],
            world="CanonWorld",
            as_override=True,
            relative=True,
            scene_uid=7,
        )
    )

    command, params = connection.calls[0]
    assert command == "link_canon_library"
    assert params == {
        "filepath": "/canon/hero.blend",
        "collections": ["CanonHero"],
        "objects": ["Prop"],
        "world": "CanonWorld",
        "as_override": True,
        "relative": True,
        "scene_uid": 7,
    }


def test_changed_objects_move_from_the_addon_result_into_the_envelope(monkeypatch) -> None:
    connection = _Connection({"overrides": [], "changed_objects": ["HeroBody"]})
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    envelope = asyncio.run(file_lifecycle.link_canon_library(ctx=None, filepath="/canon/hero.blend"))

    assert envelope["changed_objects"] == ["HeroBody"]
    assert "changed_objects" not in envelope["data"]


def test_changed_objects_are_bounded_and_the_total_is_reported(monkeypatch) -> None:
    """Linking a whole set must not put every object name into the agent's context."""
    names = [f"Part{index:03d}_geo" for index in range(480)]
    connection = _Connection({"overrides": [], "changed_objects": names})
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    envelope = asyncio.run(file_lifecycle.link_canon_library(ctx=None, filepath="/canon/house.blend"))

    assert envelope["changed_objects"] == names[:CHANGED_OBJECTS_LIMIT]
    assert any("480" in warning for warning in envelope["warnings"]), envelope["warnings"]


def test_create_override_defaults(monkeypatch) -> None:
    connection = _Connection()
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    asyncio.run(file_lifecycle.create_override(ctx=None, collection_uid=42))

    _command, params = connection.calls[0]
    assert params == {"collection_uid": 42, "scene_uid": None, "detail": False}


def test_create_override_forwards_every_parameter(monkeypatch) -> None:
    connection = _Connection()
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    asyncio.run(file_lifecycle.create_override(ctx=None, collection_uid=42, scene_uid=7, detail=True))

    command, params = connection.calls[0]
    assert command == "create_override"
    assert params == {"collection_uid": 42, "scene_uid": 7, "detail": True}


def test_list_libraries_defaults(monkeypatch) -> None:
    connection = _Connection()
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    asyncio.run(file_lifecycle.list_libraries(ctx=None))

    _command, params = connection.calls[0]
    assert params == {"limit": 25, "offset": 0, "detail": False}


def test_list_libraries_forwards_pagination(monkeypatch) -> None:
    connection = _Connection()
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    asyncio.run(file_lifecycle.list_libraries(ctx=None, limit=10, offset=20, detail=True))

    command, params = connection.calls[0]
    assert command == "list_libraries"
    assert params == {"limit": 10, "offset": 20, "detail": True}


def test_reload_library_forwards_uid(monkeypatch) -> None:
    connection = _Connection()
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    asyncio.run(file_lifecycle.reload_library(ctx=None, library_uid=99))

    command, params = connection.calls[0]
    assert command == "reload_library"
    assert params == {"library_uid": 99, "detail": False}


def test_relocate_library_forwards_uid_and_filepath(monkeypatch) -> None:
    connection = _Connection()
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    asyncio.run(file_lifecycle.relocate_library(ctx=None, library_uid=99, filepath="/canon/new.blend", detail=True))

    command, params = connection.calls[0]
    assert command == "relocate_library"
    assert params == {"library_uid": 99, "filepath": "/canon/new.blend", "detail": True}


def test_unlink_libraries_forwards_every_parameter(monkeypatch) -> None:
    connection = _Connection()
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    asyncio.run(file_lifecycle.unlink_libraries(ctx=None, library_uids=[1, 2], confirm=True, purge_orphans=True))

    command, params = connection.calls[0]
    assert command == "unlink_libraries"
    assert params == {"library_uids": [1, 2], "confirm": True, "purge_orphans": True}


def test_unlink_libraries_defaults(monkeypatch) -> None:
    connection = _Connection()
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    asyncio.run(file_lifecycle.unlink_libraries(ctx=None, library_uids=[1]))

    _command, params = connection.calls[0]
    assert params == {"library_uids": [1], "confirm": False, "purge_orphans": False}


def test_inspect_delivery_forwards_every_parameter(monkeypatch) -> None:
    connection = _Connection()
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    asyncio.run(
        file_lifecycle.inspect_delivery(
            ctx=None, scene_name="Scene", limit=10, offset=20, hash_libraries=True, max_hash_bytes=1024
        )
    )

    command, params = connection.calls[0]
    assert command == "inspect_delivery"
    assert params == {
        "scene_name": "Scene",
        "limit": 10,
        "offset": 20,
        "hash_libraries": True,
        "max_hash_bytes": 1024,
    }


def test_inspect_delivery_defaults_do_not_read_linked_files(monkeypatch) -> None:
    """Hashing reads linked .blend files on Blender's main thread, so it must stay opt-in."""
    connection = _Connection()
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    asyncio.run(file_lifecycle.inspect_delivery(ctx=None, scene_name="Scene"))

    _command, params = connection.calls[0]
    assert params == {
        "scene_name": "Scene",
        "limit": 50,
        "offset": 0,
        "hash_libraries": False,
        "max_hash_bytes": 268_435_456,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [("limit", 0), ("limit", 201), ("offset", -1), ("max_hash_bytes", 0), ("max_hash_bytes", 8 * 1024**3 + 1)],
)
def test_inspect_delivery_schema_rejects_out_of_range_paging(field, value) -> None:
    """The bounds are declared on the tool, so a bad page never reaches Blender's main thread."""
    hints = get_type_hints(file_lifecycle.inspect_delivery, include_extras=True)

    with pytest.raises(ValidationError):
        TypeAdapter(hints[field]).validate_python(value)


def test_save_shot_destructive_hint_is_explicit_not_schema_derived() -> None:
    """
    `save_shot` is explicitly in `_DESTRUCTIVE_TOOLS`, so the hint survives losing `confirm_overwrite`.

    Its schema's `confirm_overwrite` would mark it destructive anyway, so the test passes an
    empty schema to check the explicit entry alone.
    """
    assert _documentation._is_destructive("save_shot", {})


@pytest.mark.parametrize("tool_name", sorted(FILE_LIFECYCLE_COMMANDS))
def test_addon_failure_reaches_the_client_as_a_tool_error_unchanged(monkeypatch, tool_name) -> None:
    """
    An addon-raised refusal reaches the client as `ToolError`, unmodified.

    It propagates through the shared dispatch untouched: the addon has already removed paths
    from the message, so nothing on the way out may add to it. The test runs FastMCP's own
    `Tool.run` conversion.

    The raised type is `BlenderOperationError` on purpose. A plain `ValueError` also shows up as
    `ToolError` - FastMCP converts anything a tool raises - so it cannot tell the dispatch's own
    conversion from FastMCP's, and the revert-matrix row for that conversion survived being
    reverted while this test kept passing.
    """
    message = f"{tool_name} failed: a sanitized reason with no filesystem path"
    connection = _FailingConnection(BlenderOperationError(message))
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    tool = file_lifecycle.mcp._tool_manager._tools[tool_name]
    arguments = {
        "open_shot": {"filepath": "x.blend"},
        "save_shot": {},
        "reset_session": {},
        "link_canon_library": {"filepath": "x.blend", "collections": ["A"]},
        "create_override": {"collection_uid": 1},
        "list_libraries": {},
        "reload_library": {"library_uid": 1},
        "relocate_library": {"library_uid": 1, "filepath": "x.blend"},
        "unlink_libraries": {"library_uids": [1]},
        "get_session_info": {},
        "inspect_delivery": {"scene_name": "Scene"},
    }[tool_name]

    with pytest.raises(ToolError) as excinfo:
        asyncio.run(tool.run(arguments))
    assert message in str(excinfo.value)
