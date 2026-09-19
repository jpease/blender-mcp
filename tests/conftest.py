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

# The `bpy.app.handlers` lists `addon/session.py` attaches to, shared so every
# test module's `bpy` stub picks up a newly added event.
FILE_LIFECYCLE_HANDLER_LISTS = (
    "load_pre",
    "load_post",
    "load_post_fail",
    "save_post",
    "save_post_fail",
    "blend_import_post",
)


def install_file_lifecycle_handler_lists(handlers: ModuleType) -> ModuleType:
    """
    Give a stub `bpy.app.handlers` the empty lists `session.py` binds to.

    In Blender each is a plain `list` that accepts duplicate callbacks and whose
    `remove` raises for an absent one. A stub that differs could let the
    idempotence guard in `session.register_handlers` pass here and fail in Blender.

    Args:
        handlers: The stub module standing in for `bpy.app.handlers`.

    Returns:
        ModuleType: The same module, so a caller can build and populate it in
        one expression.

    """
    for list_name in FILE_LIFECYCLE_HANDLER_LISTS:
        setattr(handlers, list_name, [])
    return handlers


def load_addon_source_module(file_name: str, alias: str) -> ModuleType:
    """
    Load one `bpy`-free module out of the bundled addon, straight from its file.

    The addon package imports `bpy`, but leaf modules such as `output_roots.py` do
    not, so a test can read the addon's own constants instead of retyping them.
    Each call re-executes the source, so module state cannot leak between tests.

    A throwaway parent package is registered during the load so relative imports
    of `bpy`-free siblings resolve; it and every module loaded under it are removed
    afterwards.

    Args:
        file_name: The module's file name inside the addon package.
        alias: `sys.modules`-style name to execute it under. Distinct aliases
            keep two suites' copies apart.

    Returns:
        ModuleType: The freshly executed module.

    Raises:
        AssertionError: If the file is not an importable module.

    """
    path = ROOT_ADDON.parent / file_name
    package_name = f"{alias}__pkg"
    package = ModuleType(package_name)
    package.__path__ = [str(ROOT_ADDON.parent)]  # type: ignore[attr-defined]
    sys.modules[package_name] = package
    try:
        spec = importlib.util.spec_from_file_location(f"{package_name}.{path.stem}", path)
        if spec is None or spec.loader is None:
            raise AssertionError(f"{path} is not an importable module")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        module.__name__ = alias
        return module
    finally:
        for registered in [name for name in sys.modules if name == package_name or name.startswith(package_name + ".")]:
            del sys.modules[registered]


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
    assert spec is not None and spec.loader is not None, f"{ROOT_ADDON} is not loadable"
    addon = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, addon)
    spec.loader.exec_module(addon)
    return addon


# A string target, resolved when the fixture runs. Importing the server here would import it
# for the whole session, so a bad BLENDER_MCP_TOOLSETS would abort collection.
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
            result: Reply to return from every command. When None, echoes a
                `changed_objects` entry named after the `name` parameter, or
                `"Created"` when there is none, as the scene authoring tools expect.

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

    Patched in `_scene_shared`, whose `_call` resolves `get_blender_connection`,
    so one stub covers every tool module that dispatches through it; patching a
    tool module instead would miss.

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
