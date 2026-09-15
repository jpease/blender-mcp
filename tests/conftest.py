"""
Shared paths, loader, and Blender-dispatch stub for the test suite.

These tests load the bundled addon package as source (it cannot be imported
without bpy), so they need the repo root rather than the tests directory.
"""

from __future__ import annotations

import importlib.util
import sys

from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ROOT_ADDON = REPO_ROOT / "src" / "blender_mcp" / "bundled" / "addon" / "__init__.py"


def load_addon_source_module(file_name: str, alias: str) -> ModuleType:
    """
    Load one `bpy`-free module out of the bundled addon, straight from its file.

    The addon package cannot be imported outside Blender - `__init__.py` and
    `server_core.py` both import `bpy` - but individual leaf modules such as
    `output_roots.py` are deliberately free of it. Loading by path is what lets
    a test read the addon's own constants instead of retyping them. Nothing is
    cached: each call re-executes the source, so a test that mutates module
    state cannot leak into the next one.

    Args:
        file_name: The module's file name inside the addon package.
        alias: `sys.modules`-style name to execute it under. Distinct aliases
            keep two suites' copies from being mistaken for one another.

    Returns:
        ModuleType: The freshly executed module.

    Raises:
        AssertionError: If the addon no longer carries that module, which would
            make every guard written against it compare against nothing.

    """
    path = ROOT_ADDON.parent / file_name
    spec = importlib.util.spec_from_file_location(alias, path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"{path} is not an importable module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_addon_package(monkeypatch, name):
    """
    Load the bundled addon package under a scratch dotted module name.

    Internal relative imports (`from . import helpers`, `from .handlers import
    mesh`, ...) need `submodule_search_locations` and the module registered in
    `sys.modules` under `name` *before* `exec_module` runs, so those relative
    imports resolve against this package rather than failing or leaking into
    an unrelated module.

    Those relative imports also cache each submodule in `sys.modules` under
    `name.<submodule>` as a side effect - entries `monkeypatch` never touches,
    so they'd survive into the next test that reuses `name` and get reused
    stale (with a previous test's mocked bpy baked into their closures)
    instead of re-executing against the current mocks. Purge them up front.
    """
    prefix = f"{name}."
    for key in [k for k in sys.modules if k == name or k.startswith(prefix)]:
        monkeypatch.delitem(sys.modules, key, raising=False)

    spec = importlib.util.spec_from_file_location(name, ROOT_ADDON, submodule_search_locations=[str(ROOT_ADDON.parent)])
    addon = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, addon)
    spec.loader.exec_module(addon)
    return addon


# String target rather than an imported module, so monkeypatch resolves it lazily inside the
# test that asks for the fixture. Importing the server here would import it for the whole
# session at conftest load; as it stands an ambient BLENDER_MCP_TOOLSETS typo is confined to
# the modules that already import the server rather than aborting collection outright.
_PATCH_TARGET = "blender_mcp.server.tools._scene_shared.get_blender_connection"


class RecordingConnection:
    """
    Recording stand-in for the Blender socket connection.

    Attributes:
        calls: Every `(command, params)` pair a tool dispatched, in order.

    """

    def __init__(self, result: object = None) -> None:
        """
        Start with an empty recording.

        Args:
            result: Reply to return from every command. When None, echoes back a
                `changed_objects` entry named after the command's `name` parameter, or
                `"Created"` for commands that take no `name`. That is the shape the
                scene authoring tools expect.

        """
        self.calls: list[tuple[str, dict]] = []
        self._result = result

    def send_command(self, command: str, params: dict) -> object:
        """
        Record one dispatched command and return the canned reply.

        Args:
            command: Addon command name the tool dispatched.
            params: Parameters the tool sent with it.

        Returns:
            The configured reply, or the default echo when none was configured.

        """
        self.calls.append((command, params))
        if self._result is not None:
            return self._result
        return {
            "changed_objects": [params.get("name", "Created")],
        }


StubFactory = Callable[..., RecordingConnection]


@pytest.fixture
def stub_blender_connection(monkeypatch: pytest.MonkeyPatch) -> StubFactory:
    """
    Route scene tool dispatch to a recording stub.

    `scene` and `scene_authoring` both dispatch through `_scene_shared._call`, which resolves
    `get_blender_connection` in `_scene_shared`'s namespace. Patching it there is what makes
    one stub cover tools in either module; patching either tool module would silently miss.
    Any module that starts dispatching through `_scene_shared` is covered automatically.

    Returns:
        A factory taking an optional canned reply and returning the installed
        `RecordingConnection`.

    """

    def _install(result: object = None) -> RecordingConnection:
        """
        Install the stub and hand back the recorder holding its calls.

        Args:
            result: Reply every command should return, or None for the default echo.

        Returns:
            The `RecordingConnection` now serving this test.

        """
        connection = RecordingConnection(result)
        monkeypatch.setattr(_PATCH_TARGET, lambda: connection)
        return connection

    return _install
