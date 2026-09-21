"""
Rollback that survives a file swap or a library reload, and tracks `libraries`.

These invert the regression guards in `tests/test_mutation_transaction.py`: a
transaction that snapshotted one database and rolls back in another removes that
database's contents. The stubs encode which handlers Blender fires for a load, a
link, a reload and an unlink, and which session_uids move.
"""

from __future__ import annotations

import contextlib
import sys

from collections.abc import Iterator
from types import ModuleType

import pytest

from test_mutation_transaction import (
    _LINKED_FROM_CANON,
    _TRACKED_COLLECTIONS,
    FakeCollection,
    FakeDatablock,
    FakeLinkedDatablock,
    FakeMesh,
    FakeMutableObject,
    _load_addon,
    _reload_library_contents,
    _replace_whole_database,
)
from test_session_state import _load_session

_LIBRARY_COMMANDS = ("reload_library", "relocate_library", "unlink_libraries")


class RecordingCollection(FakeCollection):
    """A `bpy.data` collection stub that records every `remove()` it is asked for."""

    def __init__(self) -> None:
        """Start with no datablocks and no recorded removals."""
        super().__init__()
        self.removed: list[object] = []

    def remove(self, db: object, do_unlink: bool = True) -> None:
        """
        Record the call, then remove the datablock.

        Args:
            db: The datablock to remove.
            do_unlink: Passed through, as Blender's API takes it.

        """
        self.removed.append(db)
        super().remove(db, do_unlink=do_unlink)


class FakeLibraries(RecordingCollection):
    """
    `bpy.data.libraries`: removing a library also frees every datablock linked from it.

    In Blender a later `remove()` on a freed datablock raises `ReferenceError`, so
    freed datablocks are marked here to make such a call visible instead of hidden
    by `suppress(Exception)`.
    """

    def __init__(self, data: dict[str, FakeCollection]) -> None:
        """
        Hold the other collections, so a library removal can free what it linked.

        Args:
            data: Every stub `bpy.data` collection, this one included.

        """
        super().__init__()
        self._data = data

    def remove(self, db: object, do_unlink: bool = True) -> None:
        """
        Free every datablock linked from the library, then remove the library.

        Args:
            db: The library to remove.
            do_unlink: Passed through, as Blender's API takes it.

        """
        for collection in self._data.values():
            if collection is self:
                continue
            for linked in [item for item in collection if getattr(item, "library", None) is db]:
                FakeCollection.remove(collection, linked)
                linked.freed = True
        super().remove(db, do_unlink=do_unlink)


def _data_with_libraries() -> dict[str, FakeCollection]:
    """
    Build stub `bpy.data` collections, recording removals, with a cascading `libraries`.

    Returns:
        dict[str, FakeCollection]: Collection name mapped to its stub.

    """
    data: dict[str, FakeCollection] = {name: RecordingCollection() for name in _TRACKED_COLLECTIONS}
    data["libraries"] = FakeLibraries(data)
    return data


def _modules(addon: ModuleType) -> tuple[object, ModuleType, ModuleType, ModuleType]:
    """
    Resolve the server and the three addon modules a test drives.

    Args:
        addon: The addon package `_load_addon` returned.

    Returns:
        tuple: A fresh server, and the addon's `server_core`, `session` and `transaction` modules.

    """
    server_core = sys.modules[f"{addon.__name__}.server_core"]
    return (
        server_core.BlenderMCPServer(),
        server_core,
        sys.modules[f"{addon.__name__}.session"],
        sys.modules[f"{addon.__name__}.transaction"],
    )


def _fire(bpy: ModuleType, list_name: str, argument: object = "") -> None:
    """
    Call every callback on one handler list with the two positional arguments Blender passes.

    Args:
        bpy: The `bpy` stub.
        list_name: The handler list to fire.
        argument: The first argument; the second is always None.

    """
    for handler in list(getattr(bpy.app.handlers, list_name)):
        handler(argument, None)


def _link_library(data: dict[str, FakeCollection], name: str) -> list[FakeDatablock]:
    """
    Model `bpy.data.libraries.load(link=True)`: a new `Library` plus the datablocks linked from it.

    Args:
        data: The stub `bpy.data` collections.
        name: The library's name.

    Returns:
        list[FakeDatablock]: The library first, then its linked datablocks.

    """
    library = FakeDatablock(name)
    data["libraries"][name] = library
    created: list[FakeDatablock] = [library]
    for coll_name, db_name in (("collections", "CanonHero"), ("objects", "HeroBody"), ("meshes", "HeroMesh")):
        linked = FakeLinkedDatablock(db_name, library)
        data[coll_name][db_name] = linked
        created.append(linked)
    return created


def _uids(data: dict[str, FakeCollection]) -> set[int]:
    """
    Read every session_uid present across the stub collections.

    Args:
        data: The stub `bpy.data` collections.

    Returns:
        set[int]: The session_uids.

    """
    return {db.session_uid for collection in data.values() for db in collection}


# ---------------------------------------------------------------------------
# The library commands bypass the transaction; the link does not
# ---------------------------------------------------------------------------


def test_the_datablock_replacing_set_is_the_three_library_commands_and_nothing_read_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A dedicated spec field, disjoint from the swap flag and from `read_only`.

    The swap set drives the drain loop's file-swap barrier; merging these into it
    would discard a batch on every reload. Adding them to the read-only set would
    misdescribe commands that replace and free datablocks.
    """
    data = _data_with_libraries()
    addon, _bpy = _load_addon(monkeypatch, data=data)
    _server, server_core, _session, _txn = _modules(addon)

    replacing = {name for name, spec in server_core.COMMANDS.items() if spec.datablock_replacing}
    assert replacing == set(_LIBRARY_COMMANDS)
    assert not {name for name in replacing if server_core.COMMANDS[name].session_swap}
    assert not {name for name in replacing if server_core.COMMANDS[name].read_only}
    assert "link_canon_library" not in replacing


def test_a_library_replacing_command_never_reaches_mutation_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Each of the three is named literally, so emptying the constant cannot make this pass without testing.

    `unlink_libraries` fires no handler, so this routing is the only thing keeping
    a rollback away from its freed datablocks.
    """
    data = _data_with_libraries()
    addon, _bpy = _load_addon(monkeypatch, data=data)
    server, server_core, _session, _txn = _modules(addon)
    entered: list[str] = []

    def recording_transaction(cmd_type: str, _targets: object, _capture_geometry: bool) -> None:
        """
        Fail loudly if the dispatcher wraps a command.

        Args:
            cmd_type: The command being wrapped.
            _targets: Ignored.
            _capture_geometry: Ignored.

        Raises:
            AssertionError: Always.

        """
        entered.append(cmd_type)
        raise AssertionError(f"{cmd_type} was wrapped in mutation_transaction")

    monkeypatch.setattr(server_core, "mutation_transaction", recording_transaction)

    for cmd_type in _LIBRARY_COMMANDS:
        assert server._run_handler(cmd_type, lambda: {"ok": True}, {}) == {"ok": True}
    assert entered == []


def test_link_canon_library_still_enters_mutation_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other direction: its new datablocks are this request's, so its failure must roll back."""
    data = _data_with_libraries()
    addon, _bpy = _load_addon(monkeypatch, data=data)
    server, server_core, _session, _txn = _modules(addon)
    real_transaction = server_core.mutation_transaction
    entered: list[str] = []

    @contextlib.contextmanager
    def recording_transaction(cmd_type: str, targets: object = (), capture_geometry: bool = False) -> Iterator[object]:
        """
        Record the wrapped command, then run the real transaction.

        Args:
            cmd_type: The command being wrapped.
            targets: Passed through.
            capture_geometry: Passed through.

        Yields:
            object: The real transaction.

        """
        entered.append(cmd_type)
        with real_transaction(cmd_type, targets, capture_geometry) as txn:
            yield txn

    monkeypatch.setattr(server_core, "mutation_transaction", recording_transaction)

    assert server._run_handler("link_canon_library", lambda: {"ok": True}, {}) == {"ok": True}
    assert entered == ["link_canon_library"]


def test_a_reload_that_fails_after_churning_its_library_removes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    The library-reload regression guard inverted: the same reload shape through the dispatcher, then a raise.

    No handler fires and no flag is set here, so the only thing keeping the
    reloaded contents alive is the command bypassing the transaction.
    """
    data = _data_with_libraries()
    addon, bpy = _load_addon(monkeypatch, data=data)
    server, _core, _session, _txn = _modules(addon)
    library = FakeDatablock("canon.blend")
    data["libraries"]["canon.blend"] = library
    for coll_name, name in _LINKED_FROM_CANON:
        data[coll_name][name] = FakeLinkedDatablock(name, library)
    local = bpy.data.materials.new("LocalPaint")
    present = _uids(data)

    def reload_library() -> None:
        _reload_library_contents(data, library)
        for collection in data.values():
            collection.removed.clear()
        raise RuntimeError("failed after replacing the contents")

    monkeypatch.setattr(server, "_build_command_handlers", lambda: {"reload_library": reload_library})

    response = server.execute_command_internal({"type": "reload_library", "params": {}})

    assert response == {"status": "error", "message": "failed after replacing the contents"}
    assert len(_uids(data)) == len(present)
    assert local.session_uid in _uids(data)
    assert all(not collection.removed for collection in data.values())


# ---------------------------------------------------------------------------
# load_post invalidates a transaction that is open during a swap
# ---------------------------------------------------------------------------


def test_a_swap_inside_an_open_transaction_is_not_rolled_back_and_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    The file-swap regression guard inverted: Blender's `load_post` reaches the open transaction.

    A handler that loads a file as a side effect and then fails must leave the
    new file whole, and the client must be told its partial work was not undone.
    """
    data = _data_with_libraries()
    addon, bpy = _load_addon(monkeypatch, data=data)
    server, _core, session, transaction = _modules(addon)
    session.register_handlers()
    bpy.data.objects["Hero"] = FakeDatablock("Hero")

    def loads_then_fails() -> dict[str, str]:
        _replace_whole_database({name: data[name] for name in _TRACKED_COLLECTIONS})
        for collection in data.values():
            collection.removed.clear()
        _fire(bpy, "load_post", "/shots/sq010.blend")
        return {"error": "boom"}

    monkeypatch.setattr(server, "_build_command_handlers", lambda: {"do_mutate": loads_then_fails})

    response = server.execute_command_internal({"type": "do_mutate", "params": {}})

    assert response["status"] == "error"
    assert response["message"].startswith("boom")
    assert transaction.ROLLBACK_SKIPPED_WARNING in response["message"]
    assert "not rolled back" in response["message"]
    assert len(_uids(data)) == len(_TRACKED_COLLECTIONS)
    assert all(not collection.removed for collection in data.values())


def test_an_invalidated_geometry_backup_is_dropped_without_remove(monkeypatch: pytest.MonkeyPatch) -> None:
    """After a load the backup mesh is freed; `remove()` on it would be a use-after-free `suppress` hides."""
    data = _data_with_libraries()
    addon, bpy = _load_addon(monkeypatch, data=data)
    server, _core, session, _txn = _modules(addon)
    session.register_handlers()
    mesh = FakeMesh("WidgetMesh", data["meshes"])
    data["meshes"]["WidgetMesh"] = mesh
    data["objects"]["Widget"] = FakeMutableObject("Widget", mesh=mesh)

    def bevel_that_loads_then_raises(**_params: object) -> None:
        _replace_whole_database({name: data[name] for name in _TRACKED_COLLECTIONS})
        for collection in data.values():
            collection.removed.clear()
        _fire(bpy, "load_post", "/shots/sq020.blend")
        raise RuntimeError("boom")

    monkeypatch.setattr(server, "_build_command_handlers", lambda: {"mesh_bevel": bevel_that_loads_then_raises})
    monkeypatch.setattr(bpy.ops.ed, "undo_push", lambda **_kw: None)

    response = server.execute_command_internal({"type": "mesh_bevel", "params": {"object_names": ["Widget"]}})

    assert response["status"] == "error"
    assert all(not collection.removed for collection in data.values())


def test_object_state_invalidate_releases_every_live_reference_without_touching_bpy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every RNA reference goes, the backup is never removed, and a later discard is a no-op too."""
    data = _data_with_libraries()
    addon, _bpy = _load_addon(monkeypatch, data=data)
    object_state = sys.modules[f"{addon.__name__}.object_state"]
    mesh = FakeMesh("WidgetMesh", data["meshes"])
    widget = FakeMutableObject("Widget", mesh=mesh)
    widget.parent = FakeDatablock("Rig")
    widget.material_slots = []
    state = object_state.ObjectState(widget, capture_geometry=True)
    assert state.geometry_backup is not None

    state.invalidate()
    state.discard_backup()

    assert (state.obj, state.parent, state.geometry_backup) == (None, None, None)
    assert (state.collections, state.materials) == ([], [])
    assert data["meshes"].removed == []


# ---------------------------------------------------------------------------
# blend_import_post invalidates only while a replace is in progress
# ---------------------------------------------------------------------------


def test_blend_import_post_during_a_flagged_reload_invalidates_the_open_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defence in depth for a future handler that reloads a library from inside a transaction."""
    data = _data_with_libraries()
    addon, bpy = _load_addon(monkeypatch, data=data)
    server, _core, session, transaction = _modules(addon)
    session.register_handlers()
    library = FakeDatablock("canon.blend")
    data["libraries"]["canon.blend"] = library
    linked = [FakeLinkedDatablock(name, library) for _coll_name, name in _LINKED_FROM_CANON]
    for (coll_name, name), datablock in zip(_LINKED_FROM_CANON, linked, strict=True):
        data[coll_name][name] = datablock

    def mutates_with_a_reload_inside() -> None:
        with transaction.replacing_library_contents():
            _reload_library_contents(data, library)
            for collection in data.values():
                collection.removed.clear()
            _fire(bpy, "blend_import_post", object())
        raise RuntimeError("boom")

    monkeypatch.setattr(server, "_build_command_handlers", lambda: {"do_mutate": mutates_with_a_reload_inside})

    response = server.execute_command_internal({"type": "do_mutate", "params": {}})

    assert response["status"] == "error"
    assert transaction.ROLLBACK_SKIPPED_WARNING in response["message"]
    assert len(_uids(data)) == 1 + len(linked)
    assert all(not collection.removed for collection in data.values())


def test_blend_import_post_without_the_flag_leaves_the_transaction_armed(monkeypatch: pytest.MonkeyPatch) -> None:
    """An import that is not a replace (an append, a link) must not disarm the rollback."""
    data = _data_with_libraries()
    addon, bpy = _load_addon(monkeypatch, data=data)
    server, _core, session, transaction = _modules(addon)
    session.register_handlers()

    def appends_then_fails() -> None:
        bpy.data.materials.new("Appended")
        _fire(bpy, "blend_import_post", object())
        raise RuntimeError("boom")

    monkeypatch.setattr(server, "_build_command_handlers", lambda: {"do_mutate": appends_then_fails})

    response = server.execute_command_internal({"type": "do_mutate", "params": {}})

    assert response == {"status": "error", "message": "boom"}
    assert "Appended" not in bpy.data.materials
    assert transaction.library_replace_in_progress() is False


def test_the_replace_flag_is_cleared_when_the_reload_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    A flag left set would disarm every later link's rollback in this process.

    Raises:
        RuntimeError: Inside the flagged block, standing in for a failed reload.

    """
    data = _data_with_libraries()
    addon, _bpy = _load_addon(monkeypatch, data=data)
    _server, _core, _session, transaction = _modules(addon)

    with pytest.raises(RuntimeError), transaction.replacing_library_contents():
        assert transaction.library_replace_in_progress() is True
        raise RuntimeError("reload failed")

    assert transaction.library_replace_in_progress() is False


# ---------------------------------------------------------------------------
# A failed link rolls its Library back, linked datablocks first
# ---------------------------------------------------------------------------


def test_libraries_are_tracked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without it a failed link leaks the `Library` datablock it created."""
    addon, _bpy = _load_addon(monkeypatch, data=_data_with_libraries())
    _server, _core, _session, transaction = _modules(addon)

    assert "libraries" in transaction._TRACKED_COLLECTIONS


def test_a_failed_link_rolls_back_its_library_with_the_file_handlers_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The negative assertion that catches "just invalidate on `blend_import_post`".

    `libraries.load(link=True)` fires `blend_import_post`, so a handler that
    invalidated on every import would leave this library behind.
    """
    data = _data_with_libraries()
    addon, bpy = _load_addon(monkeypatch, data=data)
    server, _core, session, _txn = _modules(addon)
    session.register_handlers()
    local = bpy.data.materials.new("LocalPaint")

    def link_canon_library() -> dict[str, str]:
        _link_library(data, "canon.blend")
        _fire(bpy, "blend_import_post", object())
        return {"error": "linked collection not found"}

    monkeypatch.setattr(server, "_build_command_handlers", lambda: {"link_canon_library": link_canon_library})

    response = server.execute_command_internal({"type": "link_canon_library", "params": {}})

    assert response == {"status": "error", "message": "linked collection not found"}
    assert len(data["libraries"]) == 0
    assert _uids(data) == {local.session_uid}


def test_a_failed_link_never_removes_a_datablock_its_library_removal_already_freed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Linked datablocks are removed before their library, so no `remove()` reaches a freed one."""
    data = _data_with_libraries()
    addon, _bpy = _load_addon(monkeypatch, data=data)
    server, _core, _session, _txn = _modules(addon)

    created: list[FakeDatablock] = []

    def link_canon_library() -> None:
        created.extend(_link_library(data, "canon.blend"))
        raise RuntimeError("boom")

    monkeypatch.setattr(server, "_build_command_handlers", lambda: {"link_canon_library": link_canon_library})

    server.execute_command_internal({"type": "link_canon_library", "params": {}})

    removed = [db for collection in data.values() for db in collection.removed]
    assert len(removed) == len(created)
    assert [db for db in removed if getattr(db, "freed", False)] == []
    assert _uids(data) == set()


# ---------------------------------------------------------------------------
# The active-transaction reference, and registration of blend_import_post
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("escape", [RuntimeError, KeyboardInterrupt, None], ids=["exception", "base", "success"])
def test_the_active_transaction_never_outlives_its_command(
    monkeypatch: pytest.MonkeyPatch, escape: type[BaseException] | None
) -> None:
    """A reference left behind would let a later `load_post` reach a finished command's state."""
    data = _data_with_libraries()
    addon, _bpy = _load_addon(monkeypatch, data=data)
    _server, _core, _session, transaction = _modules(addon)

    with contextlib.suppress(BaseException), transaction.mutation_transaction("do_mutate") as txn:
        assert transaction.active_transaction() is txn
        if escape is not None:
            raise escape

    assert transaction.active_transaction() is None


def test_blend_import_post_is_registered_once_across_disable_enable_cycles(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second handler is a second chance to stack duplicates; named literally, not read from the bindings."""
    session, bpy = _load_session(monkeypatch)

    for _cycle in range(3):
        session.register_handlers()
        session.register_handlers()
        assert len(bpy.app.handlers.blend_import_post) == 1
        assert len(bpy.app.handlers.load_post) == 1
        session.unregister_handlers()
        assert len(bpy.app.handlers.blend_import_post) == 0
