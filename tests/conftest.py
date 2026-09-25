"""
Shared paths, the fake Blender and add-on loaders, and the Blender-dispatch stub for the test suite.

These tests load the bundled addon package as source (it cannot be imported
without bpy), so they need the repo root rather than the tests directory.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types

from collections.abc import Callable, Mapping
from pathlib import Path
from types import ModuleType
from typing import TypedDict, Unpack

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ROOT_ADDON = REPO_ROOT / "src" / "blender_mcp" / "bundled" / "addon" / "__init__.py"


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


def _load_addon_package(monkeypatch: pytest.MonkeyPatch, name: str) -> ModuleType:
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


# The runtime contract is Blender 5.1+, so no fake claims to be anything older.
_FAKE_BLENDER_VERSION = (5, 1, 0)
# One name for every load; `_load_addon_package` purges the previous load's submodules.
_ADDON_PACKAGE = "blender_mcp_addon_test"


class FakeBlender(TypedDict, total=False):
    """
    What a test gives the fake Blender before the add-on imports it.

    Attributes:
        data: `bpy.data`'s attributes, such as `objects` or `filepath`.
        scene: `bpy.context.scene`; one with every provider flag off when omitted.
        use_global_undo: `bpy.context.preferences.edit.use_global_undo`. Omitted,
            the context has no `preferences` at all.

    """

    data: Mapping[str, object]
    scene: object
    use_global_undo: bool


def _install_fake_blender(monkeypatch: pytest.MonkeyPatch, **fake_blender: Unpack[FakeBlender]) -> ModuleType:
    """
    Put a fake Blender 5.1 in `sys.modules`: `bpy` and its submodules, `mathutils`, `bmesh` and `requests`.

    The fake holds what the add-on reads while it imports - the `bpy.types` base
    classes, `bpy.props`, `@persistent` and the handler lists, `requests.utils` - and
    the data and scene the test passes. The add-on looks everything else up when a
    handler runs, so a test sets what its handler reads on the returned module.

    Args:
        monkeypatch: Installs the modules for the duration of one test.
        **fake_blender: See `FakeBlender`.

    Returns:
        ModuleType: The fake `bpy`.

    """
    bpy = types.ModuleType("bpy")
    if "scene" in fake_blender:
        scene = fake_blender["scene"]
    else:
        scene = types.SimpleNamespace(
            blendermcp_use_polyhaven=False,
            blendermcp_use_sketchfab=False,
            blendermcp_use_nd=False,
        )
    bpy.context = types.SimpleNamespace(scene=scene)
    if "use_global_undo" in fake_blender:
        bpy.context.preferences = types.SimpleNamespace(
            edit=types.SimpleNamespace(use_global_undo=fake_blender["use_global_undo"])
        )
    bpy.data = types.SimpleNamespace(**fake_blender.get("data", {}))
    bpy.types = types.SimpleNamespace(
        AddonPreferences=object,
        Operator=object,
        Panel=object,
        Scene=type("Scene", (), {}),
    )
    bpy.ops = types.SimpleNamespace(ed=types.SimpleNamespace(undo_push=lambda **_kw: None))

    props = types.ModuleType("bpy.props")
    for name in ("BoolProperty", "EnumProperty", "FloatProperty", "IntProperty", "StringProperty"):
        setattr(props, name, lambda **_kwargs: None)
    bpy.props = props

    handlers = types.ModuleType("bpy.app.handlers")
    handlers.persistent = lambda fn: fn
    # In Blender each handler list is a plain `list` that accepts duplicate callbacks and
    # whose `remove` raises for an absent one. A stub that differed could let the
    # idempotence guard in `session.register_handlers` pass here and fail in Blender.
    for list_name in (
        "undo_post",
        "redo_post",
        "depsgraph_update_post",
        "load_pre",
        "load_post",
        "load_post_fail",
        "save_post",
        "save_post_fail",
        "blend_import_post",
    ):
        setattr(handlers, list_name, [])

    app = types.ModuleType("bpy.app")
    app.version = _FAKE_BLENDER_VERSION
    app.version_string = ".".join(map(str, _FAKE_BLENDER_VERSION))
    app.background = False
    app.handlers = handlers
    app.timers = types.SimpleNamespace(
        is_registered=lambda *_a, **_k: False,
        register=lambda *_a, **_k: None,
        unregister=lambda *_a, **_k: None,
    )
    bpy.app = app

    mathutils = types.ModuleType("mathutils")
    bvhtree = types.ModuleType("mathutils.bvhtree")
    bvhtree.BVHTree = type("BVHTree", (), {})
    kdtree = types.ModuleType("mathutils.kdtree")
    kdtree.KDTree = type("KDTree", (), {})
    mathutils.bvhtree = bvhtree
    mathutils.kdtree = kdtree

    requests = types.ModuleType("requests")
    requests.utils = types.SimpleNamespace(default_headers=dict)
    requests.exceptions = types.SimpleNamespace(Timeout=TimeoutError)

    for module in (bpy, props, app, handlers, mathutils, bvhtree, kdtree, requests, types.ModuleType("bmesh")):
        monkeypatch.setitem(sys.modules, module.__name__, module)
    return bpy


def load_addon(monkeypatch: pytest.MonkeyPatch, **fake_blender: Unpack[FakeBlender]) -> tuple[ModuleType, ModuleType]:
    """
    Load the bundled add-on package against a fresh fake Blender.

    Args:
        monkeypatch: Installs the fake modules for the duration of one test.
        **fake_blender: See `FakeBlender`.

    Returns:
        tuple: The add-on package and the fake `bpy` it imported.

    """
    bpy = _install_fake_blender(monkeypatch, **fake_blender)
    return _load_addon_package(monkeypatch, _ADDON_PACKAGE), bpy


def load_addon_for_module(**fake_blender: Unpack[FakeBlender]) -> tuple[ModuleType, ModuleType]:
    """
    Load the add-on once, while a test module is imported, and put `sys.modules` back.

    For a suite whose module-level names come from the add-on. The fake modules leave
    `sys.modules` again at once, so they cannot change what the next module collected
    imports; the add-on keeps the `bpy` it imported.

    Args:
        **fake_blender: See `FakeBlender`.

    Returns:
        tuple: The add-on package and the fake `bpy` it imported.

    """
    monkeypatch = pytest.MonkeyPatch()
    try:
        return load_addon(monkeypatch, **fake_blender)
    finally:
        monkeypatch.undo()


def load_session(monkeypatch: pytest.MonkeyPatch) -> tuple[ModuleType, ModuleType]:
    """
    Execute the add-on's `session.py` alone against a fresh fake Blender.

    Alone, so each call starts from a fresh epoch counter without loading the package.

    Args:
        monkeypatch: Installs the fake modules for the duration of one test.

    Returns:
        tuple: The freshly executed session module and the fake `bpy` its handlers
        read and mutate.

    """
    bpy = _install_fake_blender(monkeypatch, data={"filepath": "", "is_dirty": False, "libraries": []})
    return load_addon_source_module("session.py", "blender_mcp_addon_session_test"), bpy


def load_liquid_handler(monkeypatch: pytest.MonkeyPatch) -> tuple[ModuleType, ModuleType]:
    """
    Load the add-on and hand back its liquid handler module.

    Args:
        monkeypatch: Installs the fake modules for the duration of one test.

    Returns:
        tuple: The add-on package and its `handlers.liquid` module.

    """
    addon, _bpy = load_addon(monkeypatch, data={})
    return addon, sys.modules[f"{addon.__name__}.handlers.liquid"]


def _blender_abspath(bpy: ModuleType, path: str) -> str:
    """
    Expand a path the way `bpy.path.abspath` does, including when no file is open.

    Args:
        bpy: The fake module, for `data.filepath`.
        path: The path to expand.

    Returns:
        str: `//x` joined to the open file's directory, or the bare `x` when no
        file is open.

    """
    if not path.startswith("//"):
        return path
    base = bpy.data.filepath
    return os.path.join(os.path.dirname(base), path[2:]) if base else path[2:]


def load_file_command_addon(
    monkeypatch: pytest.MonkeyPatch, *, data: Mapping[str, object], auto_execute: object = False
) -> tuple[ModuleType, ModuleType]:
    """
    Load the add-on the way the file-lifecycle and linking commands meet Blender.

    No file or output roots are configured, global undo is on, and `bpy.path.abspath`
    expands `//` against the open file.

    Args:
        monkeypatch: Installs the fake modules for the duration of one test.
        data: `bpy.data`'s attributes.
        auto_execute: The `use_scripts_auto_execute` preference value.

    Returns:
        tuple: The add-on package and the fake `bpy` it imported.

    """
    monkeypatch.delenv("BLENDERMCP_FILE_ROOTS", raising=False)
    monkeypatch.delenv("BLENDERMCP_OUTPUT_ROOTS", raising=False)
    addon, bpy = load_addon(monkeypatch, data=data)
    bpy.context.preferences = types.SimpleNamespace(
        filepaths=types.SimpleNamespace(use_scripts_auto_execute=auto_execute),
        edit=types.SimpleNamespace(use_global_undo=True),
    )
    # `library=` is how the add-on resolves a linked datablock's path; the real
    # `bpy.path.abspath` takes it as a keyword, so the stub must too.
    bpy.path = types.SimpleNamespace(abspath=lambda path, library=None: _blender_abspath(bpy, path))
    return addon, bpy


def run_command(server: object, cmd_type: str, **params: object) -> dict:
    """
    Dispatch a command through production's own `execute_command`.

    Args:
        server: The add-on's `BlenderMCPServer`.
        cmd_type: The command.
        **params: Its parameters.

    Returns:
        dict: The response envelope.

    """
    return server.execute_command({"type": cmd_type, "params": params})


# A string target, resolved when the fixture runs. Importing the server here would import it
# for the whole session, so a bad BLENDER_MCP_TOOLSETS would abort collection.
_PATCH_TARGET = "blender_mcp.server.tools._dispatch.get_blender_connection"


class RecordingConnection:
    """
    Recording stand-in for the Blender socket connection.

    Attributes:
        calls: Every `(command, params)` pair a tool dispatched, in order.

    """

    def __init__(self, result: object = None, error: Exception | None = None) -> None:
        """
        Start with an empty recording.

        Args:
            result: Reply to return from every command. When None, echoes a
                `changed_objects` entry named after the `name` parameter, or
                `"Created"` when there is none, as the scene authoring tools expect.
            error: Raised by every command after it is recorded, standing in for a
                refusal or a dropped socket; `result` is then never returned.

        """
        self.calls: list[tuple[str, dict]] = []
        self._result = result
        self._error = error

    def send_command(self, command: str, params: dict) -> object:
        """
        Record one dispatched command and return the canned reply.

        Args:
            command: Addon command name the tool dispatched.
            params: Parameters the tool sent with it.

        Returns:
            The configured reply, or the default echo when none was configured.

        Raises:
            Exception: The configured `error`, when one was given.

        """
        self.calls.append((command, params))
        if self._error is not None:
            raise self._error
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

    Patched in `_dispatch`, the one module that resolves `get_blender_connection`, so
    one stub covers every tool in every package; patching a tool module instead would
    miss, since none of them touches the socket itself.

    Returns:
        A factory taking an optional canned reply and returning the installed
        `RecordingConnection`.

    """

    def _install(result: object = None, error: Exception | None = None) -> RecordingConnection:
        """
        Install the stub and hand back the recorder holding its calls.

        Args:
            result: Reply every command should return, or None for the default echo.
            error: Exception every command should raise instead of replying, or None.

        Returns:
            The `RecordingConnection` now serving this test.

        """
        connection = RecordingConnection(result, error)
        monkeypatch.setattr(_PATCH_TARGET, lambda: connection)
        return connection

    return _install
