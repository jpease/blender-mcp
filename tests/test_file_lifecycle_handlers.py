"""
The file-lifecycle commands `open_shot`, `save_shot` and `reset_session`, against a stub `bpy`.

The stub `bpy.ops.wm` records each operator's keyword arguments as passed, because an
omitted argument can do the opposite of a false one: `save_as_mainfile`'s `relative_remap`
defaults to True, and factory settings compress a bare save. Like the real operators, each
stub raises `RuntimeError` on failure rather than returning `{'CANCELLED'}`.

Overwrite tests use real files under `tmp_path`, because the guard is `os.path.exists`.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import shutil
import sys
import types

from collections.abc import Sequence
from pathlib import Path

import pytest

from conftest import REPO_ROOT
from test_mutation_transaction import FakeCollection, _load_addon

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "blend" / "empty_gzip.blend"
SERVER_SRC = REPO_ROOT / "src" / "blender_mcp" / "server"
FILE_COMMANDS = ("open_shot", "save_shot", "reset_session")


class _RecordingWm:
    """
    Stand-in for `bpy.ops.wm` that records calls and fires the session handlers Blender fires.

    Attributes:
        calls: `(operator name, kwargs)` for every operator call, in order.
        failures: Operator name -> the `RuntimeError` it raises instead of running.

    """

    def __init__(self, bpy: types.ModuleType) -> None:
        """
        Bind to the stub `bpy` whose data the operators change.

        Args:
            bpy: The stub module.

        """
        self._bpy = bpy
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.failures: dict[str, RuntimeError] = {}
        self.scene_flags_after_load: dict[str, bool] = {}

    def _fire(self, list_name: str, path: str) -> None:
        for handler in list(getattr(self._bpy.app.handlers, list_name)):
            handler(path, None)

    def _load(self, name: str, path: str, kwargs: dict[str, object]) -> set[str]:
        self.calls.append((name, kwargs))
        self._fire("load_pre", path)
        if name in self.failures:
            self._fire("load_post_fail", path)
            raise self.failures[name]
        self._bpy.data.filepath = path
        self._bpy.data.is_dirty = False
        for flag, value in self.scene_flags_after_load.items():
            setattr(self._bpy.context.scene, flag, value)
        self._fire("load_post", path)
        return {"FINISHED"}

    def _save(self, name: str, path: str, kwargs: dict[str, object]) -> set[str]:
        self.calls.append((name, kwargs))
        if name in self.failures:
            self._fire("save_post_fail", path)
            raise self.failures[name]
        Path(path).write_bytes(b"BLENDER-stub-save")
        self._bpy.data.filepath = path
        self._bpy.data.is_dirty = False
        self._fire("save_post", path)
        return {"FINISHED"}

    def open_mainfile(self, **kwargs: object) -> set[str]:
        """
        Record the call and swap the stub database.

        Args:
            **kwargs: Exactly what the handler passed.

        Returns:
            set[str]: `{'FINISHED'}`.

        """
        return self._load("open_mainfile", str(kwargs.get("filepath", "")), kwargs)

    def read_homefile(self, **kwargs: object) -> set[str]:
        """
        Record the call and swap to an unsaved, empty database.

        Args:
            **kwargs: Exactly what the handler passed.

        Returns:
            set[str]: `{'FINISHED'}`.

        """
        self._bpy.data.objects = []
        self._bpy.context.scene.objects = []
        return self._load("read_homefile", "", kwargs)

    def read_factory_settings(self, **kwargs: object) -> set[str]:
        """
        Record the call; production must never use it, because it also disables every enabled add-on.

        Args:
            **kwargs: Exactly what the handler passed.

        Returns:
            set[str]: `{'FINISHED'}`.

        """
        return self._load("read_factory_settings", "", kwargs)

    def save_mainfile(self, **kwargs: object) -> set[str]:
        """
        Record the call and write the target.

        Args:
            **kwargs: Exactly what the handler passed.

        Returns:
            set[str]: `{'FINISHED'}`.

        """
        return self._save("save_mainfile", str(kwargs.get("filepath") or self._bpy.data.filepath), kwargs)

    def save_as_mainfile(self, **kwargs: object) -> set[str]:
        """
        Record the call and write the target.

        Args:
            **kwargs: Exactly what the handler passed.

        Returns:
            set[str]: `{'FINISHED'}`.

        """
        return self._save("save_as_mainfile", str(kwargs.get("filepath", "")), kwargs)


def _abspath(bpy: types.ModuleType, path: str) -> str:
    """
    Mimic `bpy.path.abspath`, including its behaviour when no file is open.

    Args:
        bpy: The stub module, for `data.filepath`.
        path: The path to expand.

    Returns:
        str: `//x` joined to the open file's directory, or the bare `x` when no
        file is open.

    """
    if not path.startswith("//"):
        return path
    base = bpy.data.filepath
    return os.path.join(os.path.dirname(base), path[2:]) if base else path[2:]


class _StubScene:
    """
    A scene stub with Blender's ID custom-property protocol and its `library` link.

    `save_shot` writes the provenance block as `scene["blender_mcp"]` and skips linked
    scenes, so a stub without the mapping protocol or without `library` cannot exercise
    either rule.
    """

    def __init__(self, name: str = "Scene", *, library: object = None, **attributes: object) -> None:
        """
        Build a scene.

        Args:
            name: The scene's name.
            library: The library it is linked from, or None for a local scene.
            **attributes: Extra attributes, such as `render`, for the delivery scan.

        """
        self.name = name
        self.library = library
        self._custom_properties: dict[str, object] = {}
        for key, value in attributes.items():
            setattr(self, key, value)

    def get(self, key: str, default: object = None) -> object:
        return self._custom_properties.get(key, default)

    def __contains__(self, key: str) -> bool:
        return key in self._custom_properties

    def __getitem__(self, key: str) -> object:
        return self._custom_properties[key]

    def __setitem__(self, key: str, value: object) -> None:
        self._custom_properties[key] = value

    def __delitem__(self, key: str) -> None:
        del self._custom_properties[key]


def _scene_collection(*scenes: _StubScene) -> FakeCollection:
    """
    Build a `bpy.data.scenes` stand-in.

    Args:
        *scenes: The scenes to hold; one plain local `Scene` when none are given.

    Returns:
        FakeCollection: Keyed by each scene's current name, like Blender's.

    """
    collection = FakeCollection()
    for scene in scenes or (_StubScene(),):
        collection[scene.name] = scene
    return collection


def _server(
    monkeypatch: pytest.MonkeyPatch,
    *,
    filepath: str = "",
    is_dirty: bool = False,
    auto_execute: object = False,
) -> tuple[object, types.ModuleType, _RecordingWm]:
    """
    Build a real server over the full addon with a recording `bpy.ops.wm`.

    Args:
        monkeypatch: The test's monkeypatch.
        filepath: The open file, `""` for an unsaved session.
        is_dirty: Whether the session holds unsaved work.
        auto_execute: The `use_scripts_auto_execute` preference value.

    Returns:
        tuple: The server, the stub `bpy`, and the recording operators.

    """
    monkeypatch.delenv("BLENDERMCP_FILE_ROOTS", raising=False)
    monkeypatch.delenv("BLENDERMCP_OUTPUT_ROOTS", raising=False)
    addon, bpy = _load_addon(
        monkeypatch,
        data={
            "filepath": filepath,
            "is_dirty": is_dirty,
            "libraries": [],
            "objects": ["a", "b"],
            "scenes": _scene_collection(),
        },
    )
    bpy.context.scene.name = "Scene"
    # The scene holds one of the two object datablocks, which is what a linked hierarchy looks
    # like from here: `bpy.data.objects` counts the whole file, `scene.objects` counts the shot.
    bpy.context.scene.objects = ["a"]
    bpy.context.preferences = types.SimpleNamespace(
        filepaths=types.SimpleNamespace(use_scripts_auto_execute=auto_execute),
        edit=types.SimpleNamespace(use_global_undo=True),
    )
    # `library=` is how the add-on resolves a linked datablock's path; the real
    # `bpy.path.abspath` takes it as a keyword, so the stub must too.
    bpy.path = types.SimpleNamespace(abspath=lambda path, library=None: _abspath(bpy, path))
    bpy.utils = types.SimpleNamespace(blend_paths=lambda **_flags: [])
    wm = _RecordingWm(bpy)
    bpy.ops.wm = wm
    addon.session.register_handlers()
    return addon.BlenderMCPServer(), bpy, wm


def _shot(tmp_path: Path, name: str = "shot.blend") -> Path:
    """
    Copy the committed real `.blend` fixture into the test's directory.

    Args:
        tmp_path: Where to put it.
        name: Its file name.

    Returns:
        Path: The copy.

    """
    target = tmp_path / name
    shutil.copyfile(FIXTURE, target)
    return target


def _run(server: object, cmd_type: str, **params: object) -> dict:
    """
    Dispatch a command through production's own `execute_command`.

    Args:
        server: The server.
        cmd_type: The command.
        **params: Its parameters.

    Returns:
        dict: The response envelope.

    """
    return server.execute_command({"type": cmd_type, "params": params})  # type: ignore[attr-defined]


def _assert_no_absolute_path(text: str, *paths: object) -> None:
    """
    Assert that none of the given paths, nor a common absolute prefix, is in the text.

    Args:
        text: The client-visible text.
        *paths: Paths the call knew.

    """
    for path in paths:
        assert str(path) not in text
    assert "/private/" not in text and "/var/" not in text and "/Users/" not in text and "/tmp" not in text


# ---------------------------------------------------------------------------
# open_shot
# ---------------------------------------------------------------------------


def test_open_shot_passes_use_scripts_false_explicitly(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An omitted `use_scripts` is a critical defect, even though the operator default is False."""
    server, _bpy, wm = _server(monkeypatch)
    shot = _shot(tmp_path)

    response = _run(server, "open_shot", filepath=str(shot))

    assert response["status"] == "success", response
    assert [name for name, _ in wm.calls] == ["open_mainfile"]
    kwargs = wm.calls[0][1]
    assert "use_scripts" in kwargs, "use_scripts was inherited from the operator default, not passed"
    assert kwargs["use_scripts"] is False


def test_open_shot_passes_load_ui_false_explicitly_by_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`open_mainfile`'s own `load_ui` default is True, so the command's False must be passed."""
    server, _bpy, wm = _server(monkeypatch)

    _run(server, "open_shot", filepath=str(_shot(tmp_path)))

    kwargs = wm.calls[0][1]
    assert "load_ui" in kwargs, "load_ui was inherited: the operator's own default is True"
    assert kwargs["load_ui"] is False


def test_no_file_command_takes_a_use_scripts_parameter(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`use_scripts` is exposed nowhere: an unexpected keyword is refused before any operator runs."""
    server, _bpy, wm = _server(monkeypatch)

    for name in FILE_COMMANDS:
        assert "use_scripts" not in inspect.signature(getattr(server, name)).parameters
    response = _run(server, "open_shot", filepath=str(_shot(tmp_path)), use_scripts=True)

    assert response["status"] == "error"
    assert wm.calls == []


def test_use_scripts_appears_in_no_server_side_schema() -> None:
    """No MCP tool schema under `server/` may carry `use_scripts`, so a client can never enable embedded scripts."""
    offenders = [str(path) for path in SERVER_SRC.rglob("*.py") if "use_scripts" in path.read_text(encoding="utf-8")]

    assert offenders == []


def test_open_shot_refuses_a_dirty_session_without_discard_unsaved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An open destroys unsaved work without asking, so it needs an explicit discard."""
    server, _bpy, wm = _server(monkeypatch, is_dirty=True)

    response = _run(server, "open_shot", filepath=str(_shot(tmp_path)))

    assert response["status"] == "error"
    assert "discard_unsaved" in response["message"]
    assert wm.calls == []


def test_open_shot_opens_a_dirty_session_when_discard_unsaved_is_true(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The discard flag is the whole consent, and the result says work was discarded."""
    server, _bpy, wm = _server(monkeypatch, is_dirty=True)

    response = _run(server, "open_shot", filepath=str(_shot(tmp_path)), discard_unsaved=True)

    assert response["status"] == "success", response
    assert response["result"]["discarded_unsaved_changes"] is True
    assert [name for name, _ in wm.calls] == ["open_mainfile"]


def test_open_shot_refuses_while_scripts_auto_execute_is_enabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Refuse branch: the preference on means a .blend could run embedded scripts."""
    server, _bpy, wm = _server(monkeypatch, auto_execute=True)

    response = _run(server, "open_shot", filepath=str(_shot(tmp_path)))

    assert response["status"] == "error"
    assert "use_scripts_auto_execute" in response["message"]
    assert wm.calls == []


def test_open_shot_proceeds_while_scripts_auto_execute_is_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Allow branch: the factory default (False) does not block a load."""
    server, _bpy, wm = _server(monkeypatch, auto_execute=False)

    response = _run(server, "open_shot", filepath=str(_shot(tmp_path)))

    assert response["status"] == "success", response
    assert len(wm.calls) == 1


def test_open_shot_refuses_when_the_auto_execute_preference_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unreadable preference fails closed, as if it were on."""
    server, bpy, wm = _server(monkeypatch)
    bpy.context.preferences = types.SimpleNamespace()

    response = _run(server, "open_shot", filepath=str(_shot(tmp_path)))

    assert response["status"] == "error"
    assert "use_scripts_auto_execute" in response["message"]
    assert wm.calls == []


@pytest.mark.parametrize(
    "make_path",
    [
        pytest.param(lambda tmp: tmp / "missing.blend", id="missing"),
        pytest.param(lambda tmp: tmp, id="directory"),
        pytest.param(lambda tmp: (tmp / "notes.txt", (tmp / "notes.txt").write_text("x"))[0], id="non-blend"),
        pytest.param(lambda tmp: (tmp / "bad.blend", (tmp / "bad.blend").write_bytes(b"NOPE" * 8))[0], id="bad-magic"),
        pytest.param(lambda _tmp: "", id="empty"),
    ],
)
def test_open_shot_validates_the_path_before_any_operator_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, make_path: object
) -> None:
    """Every refusable path is refused before `open_mainfile`, with no path in the message."""
    server, _bpy, wm = _server(monkeypatch)
    path = make_path(tmp_path)  # type: ignore[operator]

    response = _run(server, "open_shot", filepath=str(path))

    assert response["status"] == "error"
    assert wm.calls == []
    _assert_no_absolute_path(response["message"], tmp_path)


def test_open_shot_refuses_a_blender_relative_path_in_an_unsaved_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Unsaved, `abspath('//x')` returns bare `x`, which would resolve against the process CWD."""
    server, _bpy, wm = _server(monkeypatch, filepath="")
    monkeypatch.chdir(tmp_path)
    _shot(tmp_path)

    response = _run(server, "open_shot", filepath="//shot.blend")

    assert response["status"] == "error"
    assert "Blender-relative" in response["message"] and "never been saved" in response["message"]
    assert wm.calls == []


def test_open_shot_expands_a_blender_relative_path_against_the_open_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With a saved file open, `//` resolves beside it and the canonical form reaches the operator."""
    current = _shot(tmp_path, "current.blend")
    target = _shot(tmp_path, "next.blend")
    server, _bpy, wm = _server(monkeypatch, filepath=str(current))

    response = _run(server, "open_shot", filepath="//next.blend")

    assert response["status"] == "success", response
    assert wm.calls[0][1]["filepath"] == os.path.realpath(target)


def test_open_shot_enforces_the_configured_roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A load outside `BLENDERMCP_FILE_ROOTS` is refused; inside is allowed."""
    inside = tmp_path / "inside"
    outside = tmp_path / "outside"
    inside.mkdir()
    outside.mkdir()
    server, _bpy, wm = _server(monkeypatch)
    monkeypatch.setenv("BLENDERMCP_FILE_ROOTS", str(inside))

    refused = _run(server, "open_shot", filepath=str(_shot(outside)))
    allowed = _run(server, "open_shot", filepath=str(_shot(inside)))

    assert refused["status"] == "error" and "BLENDERMCP_FILE_ROOTS" in refused["message"]
    assert allowed["status"] == "success", allowed
    assert len(wm.calls) == 1


def test_open_shot_refuses_outside_the_roots_before_saying_whether_the_file_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Outside the roots a missing file and a directory get one message: no existence oracle."""
    inside = tmp_path / "inside"
    inside.mkdir()
    server, _bpy, _wm = _server(monkeypatch)
    monkeypatch.setenv("BLENDERMCP_FILE_ROOTS", str(inside))

    missing = _run(server, "open_shot", filepath=str(tmp_path / "nope.blend"))
    directory = _run(server, "open_shot", filepath=str(tmp_path))

    assert missing["message"] == directory["message"]
    assert "BLENDERMCP_FILE_ROOTS" in missing["message"]


def test_a_runtime_error_from_open_mainfile_is_a_clean_error_response(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A load error carrying the path twice keeps its cause and loses both paths."""
    server, _bpy, wm = _server(monkeypatch)
    shot = _shot(tmp_path)
    canonical = os.path.realpath(shot)
    wm.failures["open_mainfile"] = RuntimeError(
        f"Error: Loading \"{canonical}\" failed: Failed to read blend file '{canonical}': Missing DNA block\n"
    )

    response = _run(server, "open_shot", filepath=str(shot))

    assert response["status"] == "error"
    assert "Missing DNA block" in response["message"]
    _assert_no_absolute_path(response["message"], shot, canonical)
    assert len(wm.calls) == 1


def test_the_known_path_closes_what_structural_detection_leaves_behind(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A `, ` in a directory name leaves a relative tail structurally (a known limit); the known path removes it."""
    directory = tmp_path / "Smith, John"
    directory.mkdir()
    shot = _shot(directory)
    canonical = os.path.realpath(shot)
    server, _bpy, wm = _server(monkeypatch)
    wm.failures["open_mainfile"] = RuntimeError(f"Error: Cannot open file {canonical}@ for reading: Input/output error")

    message = _run(server, "open_shot", filepath=str(shot))["message"]

    assert "John" not in message and "shot.blend" not in message, message
    assert "Input/output error" in message


def test_a_swap_that_landed_is_still_reported_as_a_success_when_its_report_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """After an irreversible load, a failing report must not tell the client the open failed."""
    server, _bpy, wm = _server(monkeypatch)
    real_build = server._build_command_handlers  # type: ignore[attr-defined]

    def build_then_fail() -> dict:
        if wm.calls:
            raise RuntimeError("scene went away")
        return real_build()

    monkeypatch.setattr(server, "_build_command_handlers", build_then_fail)

    response = _run(server, "open_shot", filepath=str(_shot(tmp_path)))

    assert response["status"] == "success", response
    assert response["result"]["rehandshake_required"] is True
    assert response["result"]["capabilities_changed"] is True
    assert response["result"]["warnings"]


def test_open_shot_reports_the_new_session_and_asks_for_a_rehandshake(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The epoch moves once (via `load_post`) and the result tells the client to re-handshake."""
    server, bpy, _wm = _server(monkeypatch)
    before = server.get_session_info()["session_epoch"]  # type: ignore[attr-defined]
    shot = _shot(tmp_path)

    result = _run(server, "open_shot", filepath=str(shot))["result"]

    assert result["session_epoch"] == before + 1
    assert result["filepath"] == os.path.realpath(shot)
    assert result["scene_name"] == "Scene"
    # The shot's own objects, which is the number `list_scene_objects` reports for the same
    # moment; the file's object datablocks are a different, larger question under their own
    # name. Reporting the second under the first had two tools disagreeing by the linked set.
    assert result["object_count"] == len(bpy.context.scene.objects) == 1
    assert result["datablock_object_count"] == len(bpy.data.objects) == 2
    assert result["libraries"] == []
    assert result["capabilities_changed"] is False
    assert result["rehandshake_required"] is True
    assert result["discarded_unsaved_changes"] is False
    json.dumps(result)


def test_open_shot_reports_that_the_capability_set_followed_the_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Capabilities are scene-gated, so a file with different flags reports the change."""
    server, _bpy, wm = _server(monkeypatch)
    wm.scene_flags_after_load["blendermcp_use_polyhaven"] = True

    result = _run(server, "open_shot", filepath=str(_shot(tmp_path)))["result"]

    assert result["capabilities_changed"] is True


@pytest.mark.parametrize(
    ("cmd_type", "flag"),
    [
        ("open_shot", "load_ui"),
        ("open_shot", "discard_unsaved"),
        ("save_shot", "compress"),
        ("save_shot", "relative_remap"),
        ("save_shot", "confirm_overwrite"),
        ("save_shot", "create_directories"),
        ("reset_session", "confirm"),
    ],
)
def test_a_flag_that_is_not_a_real_bool_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cmd_type: str, flag: str
) -> None:
    """`"true"` is refused rather than read as truthy, before any operator runs."""
    server, _bpy, wm = _server(monkeypatch, filepath=str(_shot(tmp_path, "open.blend")))
    params: dict[str, object] = {flag: "true"}
    if cmd_type == "open_shot":
        params["filepath"] = str(_shot(tmp_path))
    if cmd_type == "save_shot":
        params["filepath"] = str(tmp_path / "new.blend")

    response = _run(server, cmd_type, **params)

    assert response["status"] == "error"
    assert flag in response["message"]
    assert wm.calls == []


# ---------------------------------------------------------------------------
# save_shot
# ---------------------------------------------------------------------------


def test_save_shot_passes_compress_and_relative_remap_false_explicitly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Omitted, `relative_remap` is True and factory prefs compress, so both are passed as False."""
    server, _bpy, wm = _server(monkeypatch)
    target = tmp_path / "new.blend"

    response = _run(server, "save_shot", filepath=str(target))

    assert response["status"] == "success", response
    assert [name for name, _ in wm.calls] == ["save_as_mainfile"]
    kwargs = wm.calls[0][1]
    assert "relative_remap" in kwargs, "relative_remap inherited save_as_mainfile's default, which is True"
    assert kwargs["relative_remap"] is False
    assert "compress" in kwargs, "compress inherited: use_file_compression is True at factory settings and wins"
    assert kwargs["compress"] is False
    assert kwargs["filepath"] == os.path.realpath(target)


def test_save_shot_forwards_an_explicit_opt_in(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An explicit True reaches the operator unchanged."""
    server, _bpy, wm = _server(monkeypatch)

    _run(server, "save_shot", filepath=str(tmp_path / "new.blend"), compress=True, relative_remap=True)

    assert wm.calls[0][1]["compress"] is True
    assert wm.calls[0][1]["relative_remap"] is True


def test_saving_over_an_existing_file_without_confirmation_calls_no_operator(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`check_existing` is inert programmatically; the real file's bytes must be unchanged."""
    server, _bpy, wm = _server(monkeypatch)
    existing = _shot(tmp_path)
    before = existing.read_bytes()

    response = _run(server, "save_shot", filepath=str(existing))

    assert response["status"] == "error"
    assert "confirm_overwrite" in response["message"]
    assert wm.calls == [], "check_existing is inert programmatically; the pre-check must run before the operator"
    assert existing.read_bytes() == before
    _assert_no_absolute_path(response["message"], existing)


def test_saving_over_an_existing_file_with_confirmation_writes_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Confirmation lets the overwrite through and the result reports it."""
    server, _bpy, wm = _server(monkeypatch)
    existing = _shot(tmp_path)

    response = _run(server, "save_shot", filepath=str(existing), confirm_overwrite=True)

    assert response["status"] == "success", response
    assert response["result"]["overwrote_existing"] is True
    assert [name for name, _ in wm.calls] == ["save_as_mainfile"]


def test_save_shot_in_place_on_an_unsaved_session_is_refused_with_an_actionable_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An in-place save needs a file; the message says to pass a filepath."""
    server, _bpy, wm = _server(monkeypatch, filepath="")

    response = _run(server, "save_shot")

    assert response["status"] == "error"
    assert "filepath" in response["message"] and "never been saved" in response["message"]
    assert wm.calls == []


def test_save_shot_in_place_needs_confirmation_because_it_overwrites_the_file_on_disk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Saving in place replaces the on-disk copy, so it takes the same confirmation."""
    current = _shot(tmp_path)
    before = current.read_bytes()
    server, _bpy, wm = _server(monkeypatch, filepath=str(current), is_dirty=True)

    response = _run(server, "save_shot")

    assert response["status"] == "error"
    assert "confirm_overwrite" in response["message"]
    assert wm.calls == []
    assert current.read_bytes() == before


def test_save_shot_in_place_uses_save_mainfile_with_explicit_arguments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The in-place path uses `save_mainfile` with the validated path and both flags explicit."""
    current = _shot(tmp_path)
    server, _bpy, wm = _server(monkeypatch, filepath=str(current), is_dirty=True)

    response = _run(server, "save_shot", confirm_overwrite=True)

    assert response["status"] == "success", response
    assert [name for name, _ in wm.calls] == ["save_mainfile"]
    kwargs = wm.calls[0][1]
    assert kwargs["filepath"] == os.path.realpath(current)
    assert "compress" in kwargs and kwargs["compress"] is False
    assert "relative_remap" in kwargs and kwargs["relative_remap"] is False
    assert response["result"]["saved_in_place"] is True


@pytest.mark.parametrize("in_place", [False, True], ids=["save_as_mainfile", "save_mainfile"])
def test_a_runtime_error_from_a_save_operator_is_a_clean_error_response(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, in_place: bool
) -> None:
    """The derived `<path>@` temp name is removed from both save operators' errors."""
    current = _shot(tmp_path)
    server, _bpy, wm = _server(monkeypatch, filepath=str(current))
    target = current if in_place else tmp_path / "new.blend"
    canonical = os.path.realpath(target)
    operator = "save_mainfile" if in_place else "save_as_mainfile"
    wm.failures[operator] = RuntimeError(f"Error: Cannot open file {canonical}@ for writing: Permission denied\n")
    params: dict[str, object] = {"confirm_overwrite": True}
    if not in_place:
        params["filepath"] = str(target)

    response = _run(server, "save_shot", **params)

    assert response["status"] == "error"
    assert "Permission denied" in response["message"]
    _assert_no_absolute_path(response["message"], target, canonical)
    assert [name for name, _ in wm.calls] == [operator]


def test_save_shot_enforces_the_roots_for_an_explicit_target_and_for_the_open_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Both an explicit target and the open file are held to the roots."""
    inside = tmp_path / "inside"
    outside = tmp_path / "outside"
    inside.mkdir()
    outside.mkdir()
    current = _shot(outside)
    server, _bpy, wm = _server(monkeypatch, filepath=str(current))
    monkeypatch.setenv("BLENDERMCP_FILE_ROOTS", str(inside))

    explicit = _run(server, "save_shot", filepath=str(outside / "new.blend"))
    in_place = _run(server, "save_shot", confirm_overwrite=True)
    allowed = _run(server, "save_shot", filepath=str(inside / "new.blend"))

    assert explicit["status"] == "error" and "BLENDERMCP_FILE_ROOTS" in explicit["message"]
    assert in_place["status"] == "error" and "BLENDERMCP_FILE_ROOTS" in in_place["message"]
    assert allowed["status"] == "success", allowed
    assert [name for name, _ in wm.calls] == ["save_as_mainfile"]


def test_save_shot_refuses_a_missing_directory_unless_asked_to_create_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A canon/shots layout needs no pre-created directories, but only on an explicit opt-in."""
    server, _bpy, wm = _server(monkeypatch)
    target = tmp_path / "canon" / "shots" / "sh010.blend"

    refused = _run(server, "save_shot", filepath=str(target))

    assert refused["status"] == "error" and "create_directories=true" in refused["message"]
    assert wm.calls == [] and not (tmp_path / "canon").exists()
    _assert_no_absolute_path(refused["message"], target)

    created = _run(server, "save_shot", filepath=str(target), create_directories=True)

    assert created["status"] == "success", created
    assert created["result"]["created_directory"] is True
    assert target.is_file()
    assert [name for name, _ in wm.calls] == ["save_as_mainfile"]


def test_save_shot_creates_no_directory_outside_the_roots_or_on_a_refusal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Directories are made only after the roots and every other refusal have passed."""
    inside = tmp_path / "inside"
    inside.mkdir()
    server, _bpy, wm = _server(monkeypatch)
    monkeypatch.setenv("BLENDERMCP_FILE_ROOTS", str(inside))

    outside = _run(server, "save_shot", filepath=str(tmp_path / "outside" / "x.blend"), create_directories=True)
    escape = _run(server, "save_shot", filepath=str(inside / ".." / "escape" / "x.blend"), create_directories=True)
    not_bool = _run(server, "save_shot", filepath=str(inside / "shots" / "x.blend"), create_directories="yes")

    for response in (outside, escape, not_bool):
        assert response["status"] == "error", response
    assert "BLENDERMCP_FILE_ROOTS" in outside["message"] and "BLENDERMCP_FILE_ROOTS" in escape["message"]
    assert not (tmp_path / "outside").exists() and not (tmp_path / "escape").exists()
    assert not (inside / "shots").exists()
    assert wm.calls == []


def test_save_shot_reports_no_created_directory_when_it_already_existed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`created_directory` is False when the opt-in had nothing to do."""
    server, _bpy, _wm = _server(monkeypatch)

    response = _run(server, "save_shot", filepath=str(tmp_path / "x.blend"), create_directories=True)

    assert response["status"] == "success", response
    assert response["result"]["created_directory"] is False


def test_save_shot_refuses_a_blender_relative_path_in_an_unsaved_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unsaved `//` save would write into the process CWD; nothing is written."""
    server, _bpy, wm = _server(monkeypatch, filepath="")
    monkeypatch.chdir(tmp_path)

    response = _run(server, "save_shot", filepath="//new.blend")

    assert response["status"] == "error"
    assert "Blender-relative" in response["message"]
    assert wm.calls == []
    assert not (tmp_path / "new.blend").exists()


# ---------------------------------------------------------------------------
# reset_session
# ---------------------------------------------------------------------------


def test_reset_session_without_confirm_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset discards the session, so it needs `confirm=True`."""
    server, _bpy, wm = _server(monkeypatch, is_dirty=True)

    response = _run(server, "reset_session")

    assert response["status"] == "error"
    assert "confirm" in response["message"]
    assert wm.calls == []


def test_reset_session_reads_the_empty_factory_startup_file_and_never_factory_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`read_factory_settings` unregisters every add-on, so reset uses `read_homefile`."""
    server, _bpy, wm = _server(monkeypatch, filepath="/shots/sq010.blend", is_dirty=True)
    before = server.get_session_info()["session_epoch"]  # type: ignore[attr-defined]

    response = _run(server, "reset_session", confirm=True)

    assert response["status"] == "success", response
    assert [name for name, _ in wm.calls] == ["read_homefile"]
    assert wm.calls[0][1] == {"use_empty": True, "use_factory_startup": True, "load_ui": False}
    result = response["result"]
    assert result["session_epoch"] == before + 1, "load_post moves the epoch once; the handler must not add one"
    assert result["filepath"] is None
    assert result["object_count"] == 0
    assert result["discarded_unsaved_changes"] is True
    assert result["rehandshake_required"] is True


def test_a_runtime_error_from_the_reset_operator_is_a_clean_error_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reset failure carries no path either."""
    server, _bpy, wm = _server(monkeypatch, filepath="/Users/someone/shots/sq010.blend")
    wm.failures["read_homefile"] = RuntimeError('Error: Cannot read file "/Users/someone/shots/sq010.blend"\n')

    response = _run(server, "reset_session", confirm=True)

    assert response["status"] == "error"
    _assert_no_absolute_path(response["message"], "/Users/someone/shots/sq010.blend")


# ---------------------------------------------------------------------------
# contract and liveness
# ---------------------------------------------------------------------------


def test_the_file_commands_are_dispatchable_and_advertised_beside_get_session_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Handshake parity: all three file commands and `get_session_info` are advertised and dispatchable."""
    server, _bpy, _wm = _server(monkeypatch)

    capabilities = server.get_addon_info()["capabilities"]  # type: ignore[attr-defined]

    for name in (*FILE_COMMANDS, "get_session_info"):
        assert name in capabilities
        assert name in server._build_command_handlers()  # type: ignore[attr-defined]
        assert not server.command_spec(name).read_only or name == "get_session_info"  # type: ignore[attr-defined]


class _RecordingClient:
    """Duck-typed socket that keeps every frame written to it."""

    def __init__(self) -> None:
        """Start with nothing written."""
        self.writes: list[bytes] = []

    def sendall(self, payload: bytes) -> None:
        """
        Record one write.

        Args:
            payload: The framed bytes.

        """
        self.writes.append(payload)

    def frames(self) -> list[dict]:
        """
        Decode every frame written.

        Returns:
            list[dict]: One response per frame.

        """
        return [json.loads(line) for line in b"".join(self.writes).split(b"\n") if line]


def _liveness_case(case: str, tmp_path: Path, wm: _RecordingWm) -> tuple[str, dict[str, object], str]:
    """
    Build one liveness case, arming the operator failure it needs.

    Args:
        case: The parametrize id.
        tmp_path: The test directory.
        wm: The recording operators.

    Returns:
        tuple: Command type, params, and the expected response status.

    """
    shot = _shot(tmp_path, "target.blend")
    cases: dict[str, tuple[str, dict[str, object], str]] = {
        "open ok": ("open_shot", {"filepath": str(shot)}, "success"),
        "open refused": ("open_shot", {"filepath": str(tmp_path / "missing.blend")}, "error"),
        "open raises": ("open_shot", {"filepath": str(shot)}, "error"),
        "save ok": ("save_shot", {"filepath": str(tmp_path / "saved.blend")}, "success"),
        "save refused": ("save_shot", {"filepath": str(shot)}, "error"),
        "save raises": ("save_shot", {"filepath": str(tmp_path / "saved.blend")}, "error"),
        "reset ok": ("reset_session", {"confirm": True}, "success"),
        "reset refused": ("reset_session", {}, "error"),
        "reset raises": ("reset_session", {"confirm": True}, "error"),
    }
    if case.endswith("raises"):
        operator = {"open_shot": "open_mainfile", "save_shot": "save_as_mainfile", "reset_session": "read_homefile"}
        wm.failures[operator[cases[case][0]]] = RuntimeError(f'Error: Cannot read file "{tmp_path}/x.blend"')
    return cases[case]


@pytest.mark.parametrize(
    "case",
    [
        "open ok",
        "open refused",
        "open raises",
        "save ok",
        "save refused",
        "save raises",
        "reset ok",
        "reset refused",
        "reset raises",
    ],
)
def test_each_file_command_is_answered_exactly_once_through_the_drain_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str
) -> None:
    """Success, refusal and operator failure each yield exactly one frame through `_drain_batch`."""
    server, _bpy, wm = _server(monkeypatch)
    cmd_type, params, status = _liveness_case(case, tmp_path, wm)
    client = _RecordingClient()
    command = {"type": cmd_type, "id": "req-1", "params": params}
    server._stamp_session(command)  # type: ignore[attr-defined]
    server.command_queue.put_nowait((command, client))  # type: ignore[attr-defined]

    server._drain_batch()  # type: ignore[attr-defined]

    frames = client.frames()
    assert len(frames) == 1, frames
    assert frames[0]["id"] == "req-1"
    assert frames[0]["status"] == status, frames[0]
    _assert_no_absolute_path(json.dumps(frames[0].get("message", "")), tmp_path)


# ---------------------------------------------------------------------------
# save_shot: dirty-flag timing, relative links, temp save names
# ---------------------------------------------------------------------------


def test_the_drain_tick_ends_after_a_save_so_a_queued_edit_runs_after_blender_clears_the_dirty_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Blender clears `is_dirty` after the tick, so a command queued behind a save runs in the next tick."""
    server, _bpy, _wm = _server(monkeypatch)
    client = _RecordingClient()
    for request_id, cmd_type, params in (
        ("save", "save_shot", {"filepath": str(tmp_path / "new.blend")}),
        ("behind", "ping", {}),
    ):
        command = {"type": cmd_type, "id": request_id, "params": params}
        server._stamp_session(command)  # type: ignore[attr-defined]
        server.command_queue.put_nowait((command, client))  # type: ignore[attr-defined]

    server._drain_batch()  # type: ignore[attr-defined]
    first_tick = [frame["id"] for frame in client.frames()]
    server._drain_batch()  # type: ignore[attr-defined]

    assert first_tick == ["save"]
    assert [frame["id"] for frame in client.frames()] == ["save", "behind"]
    assert client.frames()[1]["status"] == "success", "the command behind a save must run, not be discarded"


def test_save_shot_does_not_report_a_dirty_flag_blender_has_not_cleared_yet(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """In the GUI the flag clears after the tick, so a same-tick `is_dirty` is untrue; it is not reported."""
    server, _bpy, _wm = _server(monkeypatch)

    result = _run(server, "save_shot", filepath=str(tmp_path / "new.blend"))["result"]

    assert "is_dirty" not in result


def _with_external_paths(
    bpy: types.ModuleType, libraries: tuple[tuple[str, bool], ...], others: tuple[str, ...] = ()
) -> None:
    """
    Give the stub session libraries and external file paths, as `bpy.utils.blend_paths` reports them.

    Like Blender, the stub lists image paths and every library path, indirect ones included; an
    indirect library is one whose `users_id` are all `is_library_indirect`.

    Args:
        bpy: The stub module.
        libraries: `(Library.filepath, is indirect)` per library.
        others: Non-library external paths (images, sounds, ...).

    """
    bpy.data.libraries = [
        types.SimpleNamespace(
            filepath=path,
            name=path,
            session_uid=i,
            users_id=[types.SimpleNamespace(is_library_indirect=indirect)],
        )
        for i, (path, indirect) in enumerate(libraries)
    ]
    reported = [*others, *(path for path, _indirect in libraries)]

    def blend_paths(*, absolute: bool = False, packed: bool = False, local: bool = False) -> list[str]:
        assert (absolute, packed, local) == (False, False, True), "blend_paths called with the wrong flags"
        return list(reported)

    bpy.utils = types.SimpleNamespace(blend_paths=blend_paths)


def _with_relative_library(bpy: types.ModuleType, *filepaths: str) -> None:
    """
    Give the stub session directly linked libraries with the given link paths.

    Args:
        bpy: The stub module.
        *filepaths: One `Library.filepath` per library.

    """
    _with_external_paths(bpy, tuple((path, False) for path in filepaths))


def test_saving_to_another_directory_warns_about_relative_links_that_will_not_resolve(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`relative_remap=False` keeps `//libs/x.blend` verbatim, which breaks from a new directory."""
    (tmp_path / "projA").mkdir()
    (tmp_path / "projB").mkdir()
    current = _shot(tmp_path / "projA")
    server, bpy, _wm = _server(monkeypatch, filepath=str(current))
    _with_relative_library(bpy, "//libs/lib.blend", "/abs/canon.blend")

    moved = _run(server, "save_shot", filepath=str(tmp_path / "projB" / "shot.blend"))["result"]

    assert len(moved["warnings"]) == 1
    assert "1 external file path " in moved["warnings"][0]
    assert "relative_remap" in moved["warnings"][0]


@pytest.mark.parametrize("case", ["same directory", "relative_remap", "in place"])
def test_no_relative_link_warning_when_the_links_still_resolve(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str
) -> None:
    """Same directory, an explicit remap, or an in-place save all leave `//` links resolvable."""
    current = _shot(tmp_path)
    server, bpy, _wm = _server(monkeypatch, filepath=str(current))
    _with_relative_library(bpy, "//libs/lib.blend")
    params: dict[str, object] = {
        "same directory": {"filepath": str(tmp_path / "other.blend")},
        "relative_remap": {"filepath": str(tmp_path / "sub.blend"), "relative_remap": True},
        "in place": {"confirm_overwrite": True},
    }[case]
    if case == "relative_remap":
        (tmp_path / "sub").mkdir()
        params["filepath"] = str(tmp_path / "sub" / "shot.blend")

    result = _run(server, "save_shot", **params)["result"]

    assert "warnings" not in result, result


@pytest.mark.parametrize("kind", ["file", "dangling symlink", "directory"])
@pytest.mark.parametrize("in_place", [False, True], ids=["explicit", "in place"])
def test_a_leftover_temp_save_name_beside_the_target_refuses_the_save(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kind: str, in_place: bool
) -> None:
    """Blender writes `<target>@` then renames it, so a planted `@` symlink would redirect the write."""
    current = _shot(tmp_path)
    server, _bpy, wm = _server(monkeypatch, filepath=str(current))
    target = current if in_place else tmp_path / "fresh.blend"
    temp = Path(f"{os.path.realpath(target)}@")
    victim = tmp_path / "outside-victim.txt"
    if kind == "file":
        temp.write_text("x", encoding="utf-8")
    elif kind == "dangling symlink":
        temp.symlink_to(victim)
    else:
        temp.mkdir()
    params: dict[str, object] = {"confirm_overwrite": True}
    if not in_place:
        params["filepath"] = str(target)

    response = _run(server, "save_shot", **params)

    assert response["status"] == "error"
    assert "temporary" in response["message"]
    _assert_no_absolute_path(response["message"], tmp_path)
    assert wm.calls == []
    assert not victim.exists()


def test_a_relative_image_path_counts_as_an_external_path_that_will_not_resolve(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A `//` image path counts too, so a file with no libraries still gets the warning."""
    (tmp_path / "projA").mkdir()
    (tmp_path / "projB").mkdir()
    server, bpy, _wm = _server(monkeypatch, filepath=str(_shot(tmp_path / "projA")))
    _with_external_paths(bpy, (), ("//textures/t2.png", "/abs/hdri.exr"))

    result = _run(server, "save_shot", filepath=str(tmp_path / "projB" / "shot.blend"))["result"]

    assert result["warnings"] == [
        "1 external file path (images, libraries, etc.) is Blender-relative and will not resolve from the new "
        "directory, because relative_remap is false; save again with relative_remap=true, or relink"
    ]


def test_an_indirect_library_is_not_counted_because_blender_rederives_it_from_its_parent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`//libs/deep/lib2.blend`, linked through `lib1.blend`, re-resolves from its parent on reopen."""
    (tmp_path / "projA").mkdir()
    (tmp_path / "projB").mkdir()
    server, bpy, _wm = _server(monkeypatch, filepath=str(_shot(tmp_path / "projA")))
    _with_external_paths(bpy, (("//libs/lib1.blend", False), ("//libs/deep/lib2.blend", True)), ("//textures/t2.png",))

    result = _run(server, "save_shot", filepath=str(tmp_path / "projB" / "shot.blend"))["result"]

    assert result["warnings"][0].startswith("2 external file paths (images, libraries, etc.) are Blender-relative")


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(PermissionError(13, "Permission denied"), id="EACCES"),
        pytest.param(OSError(63, "File name too long"), id="ENAMETOOLONG"),
    ],
)
@pytest.mark.parametrize("in_place", [False, True], ids=["explicit", "in place"])
def test_a_temp_save_name_that_cannot_be_checked_refuses_the_save(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error: OSError, in_place: bool
) -> None:
    """An `lstat` failure other than not-found refuses: "cannot tell" must not read as "clear"."""
    current = _shot(tmp_path)
    server, _bpy, wm = _server(monkeypatch, filepath=str(current))
    real_lstat = os.lstat

    def lstat(path: object, *args: object, **kwargs: object) -> os.stat_result:
        if str(path).endswith("@"):
            error.filename = str(path)
            raise error
        return real_lstat(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "lstat", lstat)
    params: dict[str, object] = {"confirm_overwrite": True}
    if not in_place:
        params["filepath"] = str(tmp_path / "fresh.blend")

    response = _run(server, "save_shot", **params)

    assert response["status"] == "error"
    assert "cannot be created or checked" in response["message"]
    _assert_no_absolute_path(response["message"], tmp_path)
    assert wm.calls == []


def test_an_occupied_temp_save_name_says_it_may_be_left_by_an_interrupted_save(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The exists branch names its likely cause; the unreadable branch does not claim a file exists."""
    server, _bpy, _wm = _server(monkeypatch)
    Path(f"{os.path.realpath(tmp_path / 'fresh.blend')}@").write_text("x", encoding="utf-8")

    message = _run(server, "save_shot", filepath=str(tmp_path / "fresh.blend"))["message"]

    assert "possibly left by an interrupted save" in message
    assert "cannot be created or checked" not in message


# ---------------------------------------------------------------------------
# inspect_delivery
# ---------------------------------------------------------------------------


def _image(name: str, filepath: str, *, packed: bool = False, source: str = "FILE") -> types.SimpleNamespace:
    """
    Build a stub image datablock with the fields the delivery scan reads.

    Args:
        name: Datablock name.
        filepath: Its path, as Blender would report it.
        packed: Whether the pixels live inside the .blend.
        source: The image's source enum.

    Returns:
        types.SimpleNamespace: The stub.

    """
    return types.SimpleNamespace(
        name=name,
        filepath=filepath,
        library=None,
        packed_file=object() if packed else None,
        source=source,
        users=1,
        is_dirty=False,
    )


def _delivery_scene(name: str = "Scene", output: str = "//renders/sh010_") -> _StubScene:
    """
    Build a stub scene with the render output and rigid-body fields the scan reads.

    Args:
        name: Scene name.
        output: `scene.render.filepath`.

    Returns:
        _StubScene: The stub, custom-property protocol included.

    """
    return _StubScene(
        name,
        render=types.SimpleNamespace(
            filepath=output, image_settings=types.SimpleNamespace(file_format="OPEN_EXR_MULTILAYER")
        ),
        rigidbody_world=None,
    )


def _authored_ledger(server: object) -> types.ModuleType:
    """
    Reach the add-on's own `authored` module, the one its dispatch records into.

    A separately loaded copy would have its own list, so a test that seeded it would prove
    nothing about what a save writes.

    Args:
        server: The server built by `_server`.

    Returns:
        types.ModuleType: The live ledger module.

    """
    return sys.modules[type(server).__module__].authored


def _delivery_server(
    monkeypatch: pytest.MonkeyPatch,
    *,
    filepath: str = "/shots/hero/shot.blend",
    images: Sequence[object] | None = None,
    objects: Sequence[object] | None = None,
    libraries: Sequence[object] | None = None,
    scene: object | None = None,
) -> tuple[object, types.ModuleType]:
    """
    Build a server whose stub `bpy.data` carries every collection the scan walks.

    Args:
        monkeypatch: The test's monkeypatch.
        filepath: The open .blend, "" for an unsaved session.
        images: Stub images.
        objects: Stub objects, for simulation caches.
        libraries: Stub libraries.
        scene: The scene to inspect; a default one when omitted.

    Returns:
        tuple: The server and the stub `bpy`.

    """
    server, bpy, _wm = _server(monkeypatch, filepath=filepath)
    scenes = FakeCollection()
    subject = scene if scene is not None else _delivery_scene()
    scenes[subject.name] = subject
    bpy.data.scenes = scenes
    bpy.data.images = list(images or [])
    bpy.data.fonts = []
    bpy.data.sounds = []
    bpy.data.objects = list(objects or [])
    bpy.data.libraries = list(libraries or [])
    return server, bpy


def _entries_by_name(result: dict) -> dict[str, dict]:
    return {entry["name"]: entry for entry in result["entries"]}


def test_inspect_delivery_reports_an_absolute_image_by_leaf_not_by_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An absolute texture is the defect; publishing its directory would ship the studio's layout."""
    texture = tmp_path / "prop_basecolor.png"
    texture.write_bytes(b"x")
    server, _bpy = _delivery_server(monkeypatch, images=[_image("Prop", str(texture))])

    result = server.inspect_delivery("Scene")

    entry = _entries_by_name(result)["Prop"]
    assert entry["verdict"] == "ABSOLUTE"
    assert entry["absolute"] is True
    assert entry["path"] == "prop_basecolor.png"
    _assert_no_absolute_path(json.dumps(result), tmp_path)
    assert result["portable"] is False
    assert result["classes"]["IMAGE"] == {"total": 1, "unportable": 1}


def test_inspect_delivery_does_not_mistake_a_rooted_triple_slash_path_for_a_relative_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`///Users/...` starts with `//` and names an absolute location; `startswith` gets it wrong."""
    texture = tmp_path / "secret.png"
    texture.write_bytes(b"x")
    server, _bpy = _delivery_server(monkeypatch, images=[_image("Rooted", f"//{texture}")])

    entry = _entries_by_name(server.inspect_delivery("Scene"))["Rooted"]

    assert entry["verdict"] == "ABSOLUTE"
    assert entry["absolute"] is True
    assert entry["path"] == "secret.png"


def test_inspect_delivery_reports_a_packed_image_as_portable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Packed pixels travel inside the file, so they cost nothing at delivery."""
    server, _bpy = _delivery_server(monkeypatch, images=[_image("Packed", "/gone/tex.png", packed=True)])

    entry = _entries_by_name(server.inspect_delivery("Scene"))["Packed"]

    assert entry["verdict"] == "PACKED"
    assert entry["absolute"] is False
    assert server.inspect_delivery("Scene")["classes"]["IMAGE"]["unportable"] == 0


def test_inspect_delivery_reports_an_image_whose_file_is_gone_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A broken link is unportable here and everywhere else."""
    server, _bpy = _delivery_server(monkeypatch, images=[_image("Gone", "/absent/tex.png")])

    result = server.inspect_delivery("Scene")

    assert _entries_by_name(result)["Gone"]["verdict"] == "MISSING"
    assert result["portable"] is False


def test_inspect_delivery_judges_portability_over_every_entry_not_the_returned_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A one-entry page must not report a file portable because the defect sits on page two."""
    images = [_image(f"Tex{index:02d}", f"//textures/tex{index:02d}.png", packed=True) for index in range(4)]
    images.append(_image("Zz_Absolute", "/elsewhere/tex.png"))
    server, _bpy = _delivery_server(monkeypatch, images=images)

    result = server.inspect_delivery("Scene", limit=1)

    assert [entry["name"] for entry in result["entries"]] == ["Tex00"]
    assert result["truncated"] is True
    assert result["next_offset"] == 1
    assert result["portable"] is False
    assert result["classes"]["IMAGE"]["unportable"] == 1


def test_inspect_delivery_reports_a_memory_only_point_cache_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing is on disk, so nothing can fail to travel; calling that ABSOLUTE would be a false alarm."""
    cache = types.SimpleNamespace(
        name="Cache",
        index=0,
        filepath="",
        use_disk_cache=False,
        use_external=False,
        is_baked=False,
        is_outdated=False,
    )
    cloth = types.SimpleNamespace(type="CLOTH", name="Cloth", point_cache=cache)
    obj = types.SimpleNamespace(name="Cloak", library=None, modifiers=[cloth])
    server, _bpy = _delivery_server(monkeypatch, objects=[obj])

    entry = _entries_by_name(server.inspect_delivery("Scene"))["Cloak:Cache"]

    assert entry["verdict"] == "UNSET"
    assert entry["absolute"] is False
    assert entry["detail"]["use_disk_cache"] is False


def test_inspect_delivery_reports_an_unset_fluid_cache_directory_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mantaflow has no in-memory cache: an empty cache_directory is a defect, not a default."""
    domain = types.SimpleNamespace(cache_directory="", has_cache_baked_any=False)
    fluid = types.SimpleNamespace(type="FLUID", name="Fluid", fluid_type="DOMAIN", domain_settings=domain)
    obj = types.SimpleNamespace(name="Splash", library=None, modifiers=[fluid])
    server, _bpy = _delivery_server(monkeypatch, objects=[obj])

    result = server.inspect_delivery("Scene")

    assert _entries_by_name(result)["Splash"]["verdict"] == "MISSING"
    assert result["portable"] is False


def test_inspect_delivery_refuses_to_hash_libraries_without_configured_file_roots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Library.filepath comes out of the opened file; unconfined hashing is a read oracle."""
    server, _bpy = _delivery_server(monkeypatch)

    with pytest.raises(ValueError, match="requires BLENDERMCP_FILE_ROOTS"):
        server.inspect_delivery("Scene", hash_libraries=True)


def test_inspect_delivery_skips_hashing_a_library_outside_the_configured_roots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The confinement is the point: a library outside the roots is reported, never read."""
    inside = tmp_path / "roots"
    inside.mkdir()
    linked = inside / "canon.blend"
    linked.write_bytes(b"BLENDER-canon")
    outside = _shot(tmp_path, "outside.blend")
    libraries = [
        types.SimpleNamespace(name="canon.blend", filepath=str(linked), is_missing=False, parent=None, users_id=()),
        types.SimpleNamespace(name="outside.blend", filepath=str(outside), is_missing=False, parent=None, users_id=()),
    ]
    server, _bpy = _delivery_server(monkeypatch, libraries=libraries)
    monkeypatch.setenv("BLENDERMCP_FILE_ROOTS", str(inside))

    entries = _entries_by_name(server.inspect_delivery("Scene", hash_libraries=True))

    assert entries["canon.blend"]["detail"]["sha256"] == hashlib.sha256(b"BLENDER-canon").hexdigest()
    assert not entries["canon.blend"]["detail"]["hash_skipped"]
    assert not entries["outside.blend"]["detail"]["sha256"]
    assert entries["outside.blend"]["detail"]["hash_skipped"] == "outside the configured file roots"


def test_inspect_delivery_refuses_an_out_of_range_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bounds are checked before anything is walked, so a bad page costs no scan."""
    server, _bpy = _delivery_server(monkeypatch)

    with pytest.raises(ValueError, match=r"limit must be an integer in \[1, 200\]"):
        server.inspect_delivery("Scene", limit=0)
    with pytest.raises(ValueError, match="Scene not found"):
        server.inspect_delivery("Nope")


# ---------------------------------------------------------------------------
# Provenance: who authored this file, written into it and read back
# ---------------------------------------------------------------------------


_PROVENANCE = "blender_mcp"


def test_save_shot_writes_a_json_provenance_block_into_every_local_scene(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The block has to survive the save, so it is written before the operator, not after."""
    linked = _StubScene("Canon Scene", library=object())
    server, bpy, _wm = _server(monkeypatch)
    bpy.data.scenes = _scene_collection(_StubScene("Scene"), linked)

    response = _run(server, "save_shot", filepath=str(tmp_path / "shot.blend"))

    assert response["status"] == "success", response
    assert response["result"]["provenance_written"] is True
    assert response["result"]["provenance_ingredients"] == 0
    block = json.loads(bpy.data.scenes["Scene"][_PROVENANCE])
    assert block["claim_generator"].startswith("blender-mcp/")
    assert block["actions"][0]["action"] == "c2pa.edited"
    assert _PROVENANCE not in linked, "a linked scene belongs to another file"


def test_save_shot_names_the_datablocks_this_session_authored(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The ledger is the point: the file records what the add-on made, not what the names suggest."""
    server, bpy, _wm = _server(monkeypatch)
    ledger = _authored_ledger(server)
    ledger.clear()
    ledger.record([{"collection": "actions", "name": "Hero_Walk"}])

    _run(server, "save_shot", filepath=str(tmp_path / "shot.blend"))

    block = json.loads(bpy.data.scenes["Scene"][_PROVENANCE])
    assert block["actions"][0]["datablocks"] == ["actions:Hero_Walk"]
    assert block["actions"][0]["datablocks_truncated"] is False
    ledger.clear()


def test_a_failed_save_leaves_no_scene_claiming_provenance(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A file Blender never wrote must not leave its scenes asserting they were saved."""
    server, bpy, wm = _server(monkeypatch)
    bpy.data.scenes = _scene_collection(_StubScene("Fresh"), _StubScene("Kept"))
    bpy.data.scenes["Kept"][_PROVENANCE] = "{}"
    wm.failures["save_as_mainfile"] = RuntimeError("cannot write")

    response = _run(server, "save_shot", filepath=str(tmp_path / "shot.blend"))

    assert response["status"] == "error"
    assert _PROVENANCE not in bpy.data.scenes["Fresh"]
    assert bpy.data.scenes["Kept"][_PROVENANCE] == "{}"


def test_save_shot_writes_nothing_when_provenance_is_declined(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A canon publish has to be byte-stable, so the block must be refusable."""
    server, bpy, _wm = _server(monkeypatch)

    response = _run(server, "save_shot", filepath=str(tmp_path / "shot.blend"), write_provenance=False)

    assert response["result"]["provenance_written"] is False
    assert "provenance_ingredients" not in response["result"]
    assert _PROVENANCE not in bpy.data.scenes["Scene"]


def test_save_shot_refuses_checksums_without_configured_file_roots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Hashing a library path out of the opened file is a read oracle unless it is confined."""
    server, bpy, _wm = _server(monkeypatch)

    response = _run(server, "save_shot", filepath=str(tmp_path / "shot.blend"), provenance_checksums=True)

    assert response["status"] == "error"
    assert "BLENDERMCP_FILE_ROOTS" in response["message"]
    assert _PROVENANCE not in bpy.data.scenes["Scene"]


def _addon_module(server: object, name: str) -> types.ModuleType:
    """
    Reach one module inside the add-on package this server was loaded from.

    Args:
        server: The server built by `_server`.
        name: The module's dotted name below the package, such as `handlers.provenance`.

    Returns:
        types.ModuleType: The live module.

    """
    return sys.modules[f"{type(server).__module__.rsplit('.', 1)[0]}.{name}"]


def _counted_reads(monkeypatch: pytest.MonkeyPatch, module: types.ModuleType) -> list[int]:
    """
    Record every byte the digest module reads, by shadowing the builtin `open` in its globals.

    Args:
        monkeypatch: The test's monkeypatch.
        module: The add-on's `file_digest` module.

    Returns:
        list[int]: Grows by one entry per chunk read; empty when nothing was opened.

    """
    counts: list[int] = []
    real_open = open

    class _Counting:
        def __init__(self, handle: object) -> None:
            self._handle = handle

        def read(self, size: int = -1) -> bytes:
            chunk = self._handle.read(size)  # type: ignore[attr-defined]
            counts.append(len(chunk))
            return chunk

        def __enter__(self) -> _Counting:
            return self

        def __exit__(self, *_exc: object) -> bool:
            self._handle.close()  # type: ignore[attr-defined]
            return False

    monkeypatch.setattr(module, "open", lambda path, mode="rb": _Counting(real_open(path, mode)), raising=False)
    return counts


def test_save_shot_bounds_each_library_hash_by_the_per_file_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    The per-file bound is per file, not the call's whole byte budget.

    Handed the aggregate budget as its per-file bound, this path would read one library for as
    long as the entire request was allowed to, on the thread that runs every queued command.
    """
    canon = tmp_path / "canon"
    canon.mkdir()
    library_file = canon / "canon.blend"
    library_file.write_bytes(b"BLENDER-canon" + b"x" * 4096)
    server, bpy, _wm = _server(monkeypatch)
    bpy.data.libraries = [
        types.SimpleNamespace(name="canon.blend", filepath=str(library_file), is_missing=False, parent=None)
    ]
    monkeypatch.setenv("BLENDERMCP_FILE_ROOTS", str(canon))
    monkeypatch.setattr(_addon_module(server, "handlers.provenance"), "MAX_DIGEST_FILE_BYTES", 64)
    read = _counted_reads(monkeypatch, _addon_module(server, "file_digest"))

    response = _run(server, "save_shot", filepath=str(canon / "shot.blend"), provenance_checksums=True)

    assert response["status"] == "success", response
    (ingredient,) = json.loads(bpy.data.scenes["Scene"][_PROVENANCE])["ingredients"]
    assert not ingredient["sha256"]
    assert ingredient["skipped"] == "larger than max_hash_bytes"
    assert sum(read) <= 64, "the library was read past the per-file bound"


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        pytest.param("not json at all", "unparseable", id="not json at all"),
        pytest.param("[1, 2, 3]", "unparseable", id="[1, 2, 3]"),
        # Valid JSON, so only the size bound can refuse it.
        pytest.param(
            json.dumps({"claim_generator": "x" * 20_000}),
            "not a JSON string of the expected size",
            id="oversized",
        ),
    ],
)
def test_inspect_delivery_reports_a_hostile_provenance_block_as_invalid(
    monkeypatch: pytest.MonkeyPatch, value: str, reason: str
) -> None:
    """A .blend anyone could have authored must not inject text into the agent's context."""
    scene = _delivery_scene()
    scene[_PROVENANCE] = value
    server, _bpy = _delivery_server(monkeypatch, scene=scene)

    result = server.inspect_delivery("Scene")

    assert result["provenance"] == {"present": True, "valid": False, "reason": reason}
    assert json.dumps(result)


def test_inspect_delivery_bounds_a_valid_provenance_block(monkeypatch: pytest.MonkeyPatch) -> None:
    """Known keys only, each bounded: an oversized ingredient list cannot spend the reply budget."""
    scene = _delivery_scene()
    scene[_PROVENANCE] = json.dumps(
        {
            "claim_generator": "blender-mcp/2.0.0",
            "protocol_version": 31,
            "blender_version": "5.2.2 LTS",
            "saved_utc": "2026-01-01T00:00:00+00:00",
            "ingredients": [{"name": f"lib{index}.blend", "filepath": "//canon.blend"} for index in range(80)],
            "actions": [{"action": "c2pa.edited", "datablocks": [f"actions:a{index}" for index in range(80)]}],
            "unexpected": "dropped",
        }
    )
    server, _bpy = _delivery_server(monkeypatch, scene=scene)

    provenance = server.inspect_delivery("Scene")["provenance"]

    assert provenance["valid"] is True
    assert provenance["claim_generator"] == "blender-mcp/2.0.0"
    assert len(provenance["ingredients"]) == 50
    assert len(provenance["actions"][0]["datablocks"]) == 50
    assert "unexpected" not in provenance


def test_inspect_delivery_reports_no_provenance_for_a_file_without_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """Absent is not invalid: a hand-authored .blend has no block and that is fine."""
    server, _bpy = _delivery_server(monkeypatch)

    assert server.inspect_delivery("Scene")["provenance"] is None


def test_inspect_delivery_warns_that_an_unsaved_session_cannot_resolve_relative_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`//` has nothing to resolve against before the first save, so `portable` cannot be true."""
    server, _bpy = _delivery_server(monkeypatch, filepath="")

    result = server.inspect_delivery("Scene")

    assert result["saved"] is False
    assert result["portable"] is False
    assert any("never been saved" in warning for warning in result["warnings"])
