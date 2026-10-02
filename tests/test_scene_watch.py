"""
The undo/redo signal and the coarse outside-edit flag every add-on reply carries (`scene_watch.py`).

Driven the way Blender drives it: the handlers are fired off the `bpy.app.handlers` lists, and a
command runs through `server_core.execute_command_internal`. The clock is the module's own
`time`, replaced per test, so the quiet period is crossed by moving a number, not by sleeping.
"""

from __future__ import annotations

import sys
import types

from types import ModuleType

import pytest

from conftest import load_addon
from datablock_doubles import TRACKED_COLLECTIONS, FakeCollection


class _Clock:
    """A `time` stand-in whose `monotonic()` reads a value the test moves."""

    def __init__(self) -> None:
        """Start at an arbitrary non-zero reading."""
        self.now = 1_000.0

    def monotonic(self) -> float:
        """
        Read the current reading.

        Returns:
            float: Seconds.

        """
        return self.now


class _FakeId:
    """The original datablock a depsgraph update points at."""

    _next_uid = 1

    def __init__(self, name: str, id_type: str = "OBJECT", properties: dict[str, object] | None = None) -> None:
        """
        Name a datablock and give it a fresh session_uid.

        Args:
            name: Its name.
            id_type: Blender's `ID.id_type`, such as "OBJECT" or "SCENE".
            properties: Its custom properties.

        """
        self.name = name
        self.id_type = id_type
        self.session_uid = _FakeId._next_uid
        _FakeId._next_uid += 1
        self._properties = properties or {}

    def get(self, key: str, default: object = None) -> object:
        """
        Read a custom property, as `ID.get` does.

        Args:
            key: The property name.
            default: Returned when it is absent.

        Returns:
            object: The value.

        """
        return self._properties.get(key, default)


def _update(original: _FakeId, *, transform: bool = False, geometry: bool = False, shading: bool = False) -> object:
    """
    Build one `DepsgraphUpdate`: its `id` is the evaluated copy, whose `original` is the datablock.

    Args:
        original: The original datablock.
        transform: `is_updated_transform`.
        geometry: `is_updated_geometry`.
        shading: `is_updated_shading`.

    Returns:
        object: The update.

    """
    evaluated = types.SimpleNamespace(original=original, session_uid=0, name=original.name)
    return types.SimpleNamespace(
        id=evaluated, is_updated_transform=transform, is_updated_geometry=geometry, is_updated_shading=shading
    )


def _fire(bpy: ModuleType, list_name: str, *args: object) -> None:
    """
    Call every callback on one handler list, as Blender does.

    Args:
        bpy: The fake `bpy`.
        list_name: The handler list.
        *args: The positional arguments Blender passes.

    """
    for handler in list(getattr(bpy.app.handlers, list_name)):
        handler(*args)


def _edit(bpy: ModuleType, *originals: _FakeId) -> None:
    """
    Fire `depsgraph_update_post` for a transform edit of each datablock.

    Args:
        bpy: The fake `bpy`.
        *originals: The edited datablocks.

    """
    depsgraph = types.SimpleNamespace(updates=[_update(original, transform=True) for original in originals])
    _fire(bpy, "depsgraph_update_post", object(), depsgraph)


def _setup(monkeypatch: pytest.MonkeyPatch) -> tuple[object, ModuleType, ModuleType, _Clock]:
    """
    Load the add-on with a database a transaction can snapshot, and register the watch.

    Args:
        monkeypatch: Installs the fakes.

    Returns:
        tuple: A server, the fake `bpy`, the `scene_watch` module and its clock.

    """
    data = {name: FakeCollection() for name in TRACKED_COLLECTIONS}
    addon, bpy = load_addon(monkeypatch, data=data, use_global_undo=True)
    watch = sys.modules[f"{addon.__name__}.scene_watch"]
    clock = _Clock()
    monkeypatch.setattr(watch, "time", clock)
    watch.register_handlers()
    server = sys.modules[f"{addon.__name__}.server_core"].BlenderMCPServer()
    return server, bpy, watch, clock


def _install(monkeypatch: pytest.MonkeyPatch, server: object, handlers: dict[str, object]) -> None:
    """
    Replace the server's dispatch table for one test.

    Args:
        monkeypatch: Restores it afterwards.
        server: The server.
        handlers: Command name -> handler.

    """
    monkeypatch.setattr(server, "_build_command_handlers", lambda: handlers)


def _run(server: object, cmd_type: str) -> dict:
    """
    Dispatch one command through `execute_command_internal`.

    Args:
        server: The server.
        cmd_type: The command.

    Returns:
        dict: Its response.

    """
    return server.execute_command_internal({"type": cmd_type, "params": {}})


def _warnings(response: dict) -> list[str]:
    """
    Read the warnings a success response's result carries.

    Args:
        response: The response.

    Returns:
        list[str]: The warnings, empty when there are none.

    """
    assert response["status"] == "success", response
    return list(response["result"].get("warnings", []))


def _read_only(monkeypatch: pytest.MonkeyPatch, server: object) -> None:
    """
    Serve `get_object_info`, a read-only command that bypasses the transaction, from a stub.

    Args:
        monkeypatch: Restores the table afterwards.
        server: The server.

    """
    _install(monkeypatch, server, {"get_object_info": lambda: {"name": "Cube"}})


def test_undo_and_redo_between_commands_are_counted_and_a_load_resets_them(monkeypatch: pytest.MonkeyPatch) -> None:
    server, bpy, _watch, _clock = _setup(monkeypatch)
    _read_only(monkeypatch, server)

    _fire(bpy, "undo_post", object(), None)
    _fire(bpy, "undo_post", object(), None)
    _fire(bpy, "redo_post", object(), None)
    assert _warnings(_run(server, "get_object_info")) == [
        "The scene was undone 2 times and redone once since the last command - re-inspect anything you read before."
    ]

    _fire(bpy, "undo_post", object(), None)
    _fire(bpy, "load_post", "", None)
    assert _warnings(_run(server, "get_object_info")) == []


def test_edits_during_a_command_or_within_the_quiet_period_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    server, bpy, watch, clock = _setup(monkeypatch)
    cube = _FakeId("Cube")

    def edits_while_running() -> dict:
        _edit(bpy, cube)
        return {"name": "Cube"}

    _install(monkeypatch, server, {"get_object_info": edits_while_running})
    assert _warnings(_run(server, "get_object_info")) == []

    # The command's own re-evaluation, a moment after it answered.
    clock.now += watch.QUIET_PERIOD_SECONDS / 2
    _edit(bpy, cube)
    _read_only(monkeypatch, server)
    assert _warnings(_run(server, "get_object_info")) == []

    clock.now += watch.QUIET_PERIOD_SECONDS + 0.01
    _edit(bpy, cube)
    assert _warnings(_run(server, "get_object_info")) == [
        "The scene was edited outside this session since the last command (Cube) - "
        "re-read them before relying on earlier values."
    ]


def test_an_outside_edit_names_five_distinct_objects_and_counts_the_rest(monkeypatch: pytest.MonkeyPatch) -> None:
    server, bpy, watch, clock = _setup(monkeypatch)
    _read_only(monkeypatch, server)
    _run(server, "get_object_info")
    clock.now += watch.QUIET_PERIOD_SECONDS + 0.01
    objects = [_FakeId(name) for name in ("Cube", "Lamp", "Chair", "Table", "Rug", "Vase", "Book")]

    _edit(bpy, *objects[:3])
    _edit(bpy, *objects)
    _edit(bpy, objects[0], _FakeId("Material", id_type="MATERIAL"))

    assert _warnings(_run(server, "get_object_info")) == [
        "The scene was edited outside this session since the last command "
        "(Cube, Lamp, Chair, Table, Rug and 2 more) - re-read them before relying on earlier values."
    ]


def test_the_notice_is_given_once_and_the_state_resets_after_the_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    server, bpy, watch, clock = _setup(monkeypatch)
    _read_only(monkeypatch, server)
    clock.now += watch.QUIET_PERIOD_SECONDS + 0.01
    _edit(bpy, _FakeId("Cube"))
    _fire(bpy, "undo_post", object(), None)

    first = _warnings(_run(server, "get_object_info"))
    second = _warnings(_run(server, "get_object_info"))

    assert first == ["The scene was undone once since the last command - re-inspect anything you read before."], (
        "an undo re-evaluates every object, so its notice stands for the edit flag too"
    )
    assert second == []


def test_an_edit_to_no_object_still_flags_the_scene_without_naming_anything(monkeypatch: pytest.MonkeyPatch) -> None:
    server, bpy, watch, clock = _setup(monkeypatch)
    _read_only(monkeypatch, server)
    clock.now += watch.QUIET_PERIOD_SECONDS + 0.01

    _edit(bpy, _FakeId("World", id_type="WORLD"))

    assert _warnings(_run(server, "get_object_info")) == [
        "The scene was edited outside this session since the last command - "
        "re-read anything you rely on before using earlier values."
    ]


def test_a_flagless_scene_update_such_as_a_selection_is_not_an_edit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Selecting, activating or hiding fires a Scene-only update with every `is_updated_*` False."""
    server, bpy, watch, clock = _setup(monkeypatch)
    _read_only(monkeypatch, server)
    clock.now += watch.QUIET_PERIOD_SECONDS + 0.01
    scene = _FakeId("Scene", id_type="SCENE")

    _fire(bpy, "depsgraph_update_post", object(), types.SimpleNamespace(updates=[_update(scene)]))
    assert _warnings(_run(server, "get_object_info")) == []

    clock.now += watch.QUIET_PERIOD_SECONDS + 0.01
    _fire(bpy, "depsgraph_update_post", object(), types.SimpleNamespace(updates=[_update(scene, transform=True)]))
    assert len(_warnings(_run(server, "get_object_info"))) == 1


def test_an_object_an_async_job_is_writing_is_not_an_outside_edit(monkeypatch: pytest.MonkeyPatch) -> None:
    server, bpy, watch, clock = _setup(monkeypatch)
    _read_only(monkeypatch, server)
    clock.now += watch.QUIET_PERIOD_SECONDS + 0.01
    domain = _FakeId("Domain", properties={watch.JOB_MARKER_KEY: {"stage_action": "BAKE_DATA"}})
    finished = _FakeId("OldDomain", properties={watch.JOB_MARKER_KEY: ""})

    _edit(bpy, domain)
    assert _warnings(_run(server, "get_object_info")) == []

    clock.now += watch.QUIET_PERIOD_SECONDS + 0.01
    _edit(bpy, finished)
    assert _warnings(_run(server, "get_object_info")) == [
        "The scene was edited outside this session since the last command (OldDomain) - "
        "re-read them before relying on earlier values."
    ]


def test_the_liquid_bake_marks_its_domain_with_the_watchs_job_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    addon, _bpy = load_addon(monkeypatch, data={})
    watch = sys.modules[f"{addon.__name__}.scene_watch"]
    simulation = sys.modules[f"{addon.__name__}.handlers.liquid.simulation"]

    assert simulation._PENDING_BAKE_KEY == watch.JOB_MARKER_KEY


def test_a_transacted_command_reply_carries_the_notice_beside_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    server, bpy, _watch, _clock = _setup(monkeypatch)
    _install(monkeypatch, server, {"do_mutate": lambda: {"done": True, "warnings": ["own notice"]}})
    _fire(bpy, "undo_post", object(), None)

    response = _run(server, "do_mutate")

    warnings = _warnings(response)
    assert response["result"]["done"] is True
    assert warnings[0] == "own notice"
    assert warnings[-1] == "The scene was undone once since the last command - re-inspect anything you read before."


def test_an_error_reply_carries_the_notice_in_its_message(monkeypatch: pytest.MonkeyPatch) -> None:
    server, bpy, _watch, _clock = _setup(monkeypatch)

    def refuses() -> dict:
        raise ValueError("Object 'Nope' not found")

    _install(monkeypatch, server, {"get_object_info": refuses})
    _fire(bpy, "redo_post", object(), None)

    response = _run(server, "get_object_info")

    assert response["status"] == "error"
    assert response["message"] == (
        "Object 'Nope' not found The scene was redone once since the last command - "
        "re-inspect anything you read before."
    )
    _read_only(monkeypatch, server)
    assert _warnings(_run(server, "get_object_info")) == []


@pytest.mark.parametrize("swap", ["open_shot", "reset_session"])
def test_a_session_swap_resets_the_state_without_a_notice(monkeypatch: pytest.MonkeyPatch, swap: str) -> None:
    server, bpy, _watch, _clock = _setup(monkeypatch)
    _install(monkeypatch, server, {swap: lambda: {"opened": True}, "get_object_info": lambda: {"name": "Cube"}})
    _fire(bpy, "undo_post", object(), None)

    assert _warnings(_run(server, swap)) == []
    assert _warnings(_run(server, "get_object_info")) == []


def test_the_handshake_neither_carries_nor_consumes_the_notice(monkeypatch: pytest.MonkeyPatch) -> None:
    """The server sends `get_addon_info` on its own behalf; no agent ever reads that reply."""
    server, bpy, _watch, _clock = _setup(monkeypatch)
    _install(monkeypatch, server, {"get_addon_info": lambda: {"version": [1]}, "get_object_info": dict})
    _fire(bpy, "undo_post", object(), None)

    assert _warnings(_run(server, "get_addon_info")) == []
    assert len(_warnings(_run(server, "get_object_info"))) == 1


def test_a_read_only_failure_shape_carries_the_notice_in_its_error_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """The server reports a `{"error": ...}` result by that text alone, dropping the result's warnings."""
    server, bpy, _watch, _clock = _setup(monkeypatch)
    _install(monkeypatch, server, {"get_object_info": lambda: {"error": "No such object"}})
    _fire(bpy, "undo_post", object(), None)

    response = _run(server, "get_object_info")

    assert response["result"]["error"] == (
        "No such object The scene was undone once since the last command - re-inspect anything you read before."
    )


def test_registration_is_idempotent_and_unregistration_removes_every_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    _server, bpy, watch, _clock = _setup(monkeypatch)
    lists = ("undo_post", "redo_post", "load_post", "depsgraph_update_post")

    watch.register_handlers()
    once = {name: len(getattr(bpy.app.handlers, name)) for name in lists}
    watch.unregister_handlers()

    assert once == dict.fromkeys(lists, 1)
    assert all(not getattr(bpy.app.handlers, name) for name in lists)


def test_the_addon_registers_and_unregisters_the_watch(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no scene there is no auto-start setting to read, so `register()` starts no server."""
    addon, bpy = load_addon(monkeypatch, scene=None)
    bpy.utils = types.SimpleNamespace(register_class=lambda _cls: None, unregister_class=lambda _cls: None)
    bpy.app.handlers.render_init = []
    bindings = addon.scene_watch._HANDLER_BINDINGS

    addon.register()
    registered = [getattr(bpy.app.handlers, name).count(handler) for name, handler in bindings]
    addon.unregister()

    assert registered == [1] * len(bindings)
    assert [getattr(bpy.app.handlers, name).count(handler) for name, handler in bindings] == [0] * len(bindings)
