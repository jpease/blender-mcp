# ruff: file-ignore[import-private-name, missing-return-type-private-function, missing-type-function-argument, undocumented-public-function, yoda-conditions]
"""
Regression coverage for the ten file-lifecycle and linking tools.

Tests patch the tool module's own `get_blender_connection` with a `_Connection` stand-in and
call each tool coroutine directly. The `stub_blender_connection` fixture does not apply,
because it patches only `_scene_shared`.
"""

import asyncio

import pytest

from mcp.server.fastmcp.exceptions import ToolError
from test_mutation_transaction import _load_addon

from blender_mcp.server.tools import _documentation, file_lifecycle

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
    assert {"get_session_info", "list_libraries"} <= server._READ_ONLY_COMMANDS
    assert not FILE_LIFECYCLE_COMMANDS - {"get_session_info", "list_libraries"} & server._READ_ONLY_COMMANDS


def test_get_session_info_forwards_no_params(monkeypatch) -> None:
    connection = _Connection()
    monkeypatch.setattr(file_lifecycle, "get_blender_connection", lambda: connection)

    asyncio.run(file_lifecycle.get_session_info(ctx=None))

    command, params = connection.calls[0]
    assert command == "get_session_info"
    assert params == {}


def test_open_shot_defaults(monkeypatch) -> None:
    """The unsaved-work guard's safe default is False -- an unpinned default here loses work silently."""
    connection = _Connection()
    monkeypatch.setattr(file_lifecycle, "get_blender_connection", lambda: connection)

    asyncio.run(file_lifecycle.open_shot(ctx=None, filepath="/canon/shot.blend"))

    _command, params = connection.calls[0]
    assert params == {"filepath": "/canon/shot.blend", "load_ui": False, "discard_unsaved": False}


def test_open_shot_forwards_every_parameter(monkeypatch) -> None:
    connection = _Connection()
    monkeypatch.setattr(file_lifecycle, "get_blender_connection", lambda: connection)

    asyncio.run(file_lifecycle.open_shot(ctx=None, filepath="/canon/shot.blend", load_ui=True, discard_unsaved=True))

    command, params = connection.calls[0]
    assert command == "open_shot"
    assert params == {"filepath": "/canon/shot.blend", "load_ui": True, "discard_unsaved": True}


def test_save_shot_forwards_every_parameter(monkeypatch) -> None:
    connection = _Connection()
    monkeypatch.setattr(file_lifecycle, "get_blender_connection", lambda: connection)

    asyncio.run(
        file_lifecycle.save_shot(
            ctx=None,
            filepath="/canon/shot.blend",
            compress=True,
            relative_remap=True,
            confirm_overwrite=True,
            create_directories=True,
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
    }


def test_save_shot_default_filepath_is_none(monkeypatch) -> None:
    connection = _Connection()
    monkeypatch.setattr(file_lifecycle, "get_blender_connection", lambda: connection)

    asyncio.run(file_lifecycle.save_shot(ctx=None))

    command, params = connection.calls[0]
    assert command == "save_shot"
    assert params == {
        "filepath": None,
        "compress": False,
        "relative_remap": False,
        "confirm_overwrite": False,
        "create_directories": False,
    }


def test_reset_session_defaults(monkeypatch) -> None:
    """The whole-database discard's safe default is False -- an unpinned default here discards work silently."""
    connection = _Connection()
    monkeypatch.setattr(file_lifecycle, "get_blender_connection", lambda: connection)

    asyncio.run(file_lifecycle.reset_session(ctx=None))

    _command, params = connection.calls[0]
    assert params == {"confirm": False}


def test_reset_session_forwards_confirm(monkeypatch) -> None:
    connection = _Connection()
    monkeypatch.setattr(file_lifecycle, "get_blender_connection", lambda: connection)

    asyncio.run(file_lifecycle.reset_session(ctx=None, confirm=True))

    command, params = connection.calls[0]
    assert command == "reset_session"
    assert params == {"confirm": True}


def test_link_canon_library_forwards_every_parameter(monkeypatch) -> None:
    connection = _Connection()
    monkeypatch.setattr(file_lifecycle, "get_blender_connection", lambda: connection)

    asyncio.run(
        file_lifecycle.link_canon_library(
            ctx=None,
            filepath="/canon/hero.blend",
            collections=["CanonHero"],
            objects=["Prop"],
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
        "as_override": True,
        "relative": True,
        "scene_uid": 7,
    }


def test_link_canon_library_defaults(monkeypatch) -> None:
    connection = _Connection()
    monkeypatch.setattr(file_lifecycle, "get_blender_connection", lambda: connection)

    asyncio.run(file_lifecycle.link_canon_library(ctx=None, filepath="/canon/hero.blend"))

    _command, params = connection.calls[0]
    assert params == {
        "filepath": "/canon/hero.blend",
        "collections": None,
        "objects": None,
        "as_override": False,
        "relative": False,
        "scene_uid": None,
    }


def test_create_override_defaults(monkeypatch) -> None:
    connection = _Connection()
    monkeypatch.setattr(file_lifecycle, "get_blender_connection", lambda: connection)

    asyncio.run(file_lifecycle.create_override(ctx=None, collection_uid=42))

    _command, params = connection.calls[0]
    assert params == {"collection_uid": 42, "scene_uid": None}


def test_create_override_forwards_every_parameter(monkeypatch) -> None:
    connection = _Connection()
    monkeypatch.setattr(file_lifecycle, "get_blender_connection", lambda: connection)

    asyncio.run(file_lifecycle.create_override(ctx=None, collection_uid=42, scene_uid=7))

    command, params = connection.calls[0]
    assert command == "create_override"
    assert params == {"collection_uid": 42, "scene_uid": 7}


def test_list_libraries_defaults(monkeypatch) -> None:
    connection = _Connection()
    monkeypatch.setattr(file_lifecycle, "get_blender_connection", lambda: connection)

    asyncio.run(file_lifecycle.list_libraries(ctx=None))

    _command, params = connection.calls[0]
    assert params == {"limit": 25, "offset": 0}


def test_list_libraries_forwards_pagination(monkeypatch) -> None:
    connection = _Connection()
    monkeypatch.setattr(file_lifecycle, "get_blender_connection", lambda: connection)

    asyncio.run(file_lifecycle.list_libraries(ctx=None, limit=10, offset=20))

    command, params = connection.calls[0]
    assert command == "list_libraries"
    assert params == {"limit": 10, "offset": 20}


def test_reload_library_forwards_uid(monkeypatch) -> None:
    connection = _Connection()
    monkeypatch.setattr(file_lifecycle, "get_blender_connection", lambda: connection)

    asyncio.run(file_lifecycle.reload_library(ctx=None, library_uid=99))

    command, params = connection.calls[0]
    assert command == "reload_library"
    assert params == {"library_uid": 99}


def test_relocate_library_forwards_uid_and_filepath(monkeypatch) -> None:
    connection = _Connection()
    monkeypatch.setattr(file_lifecycle, "get_blender_connection", lambda: connection)

    asyncio.run(file_lifecycle.relocate_library(ctx=None, library_uid=99, filepath="/canon/new.blend"))

    command, params = connection.calls[0]
    assert command == "relocate_library"
    assert params == {"library_uid": 99, "filepath": "/canon/new.blend"}


def test_unlink_libraries_forwards_every_parameter(monkeypatch) -> None:
    connection = _Connection()
    monkeypatch.setattr(file_lifecycle, "get_blender_connection", lambda: connection)

    asyncio.run(file_lifecycle.unlink_libraries(ctx=None, library_uids=[1, 2], confirm=True, purge_orphans=True))

    command, params = connection.calls[0]
    assert command == "unlink_libraries"
    assert params == {"library_uids": [1, 2], "confirm": True, "purge_orphans": True}


def test_unlink_libraries_defaults(monkeypatch) -> None:
    connection = _Connection()
    monkeypatch.setattr(file_lifecycle, "get_blender_connection", lambda: connection)

    asyncio.run(file_lifecycle.unlink_libraries(ctx=None, library_uids=[1]))

    _command, params = connection.calls[0]
    assert params == {"library_uids": [1], "confirm": False, "purge_orphans": False}


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
    An addon-raised failure propagates through `_call` unmodified and reaches the client as `ToolError`.

    The addon has already removed paths from the message, so `_call` must add nothing. The
    test runs FastMCP's own `Tool.run` conversion.
    """
    message = f"{tool_name} failed: a sanitized reason with no filesystem path"
    connection = _FailingConnection(ValueError(message))
    monkeypatch.setattr(file_lifecycle, "get_blender_connection", lambda: connection)

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
    }[tool_name]

    with pytest.raises(ToolError) as excinfo:
        asyncio.run(tool.run(arguments))
    assert message in str(excinfo.value)
