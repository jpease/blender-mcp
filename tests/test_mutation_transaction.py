"""Regression coverage for the mutation_transaction rollback/checkpoint contract."""

import itertools
import sys
import types

import pytest

from conftest import install_file_lifecycle_handler_lists, load_addon_package

_UID = itertools.count(1)


def _next_uid():
    return next(_UID)


class FakeMatrix:
    """Stand-in for a mathutils.Matrix: only .copy()/.inverted() are exercised."""

    def __init__(self, value=(0.0, 0.0, 0.0)) -> None:
        self.value = value

    def copy(self):
        return FakeMatrix(self.value)

    def inverted(self):
        return FakeMatrix(self.value)


class FakeDatablock:
    def __init__(self, name) -> None:
        self.name = name
        self.session_uid = _next_uid()


class FakeMesh:
    """Mesh datablock whose .copy() registers a fresh backup in its collection."""

    def __init__(self, name, meshes) -> None:
        self.name = name
        self.session_uid = _next_uid()
        self._meshes = meshes

    def copy(self):
        clone = FakeMesh(f"{self.name}.backup", self._meshes)
        self._meshes[clone.name] = clone
        return clone


class FakeCollection:
    """
    Stand-in for a bpy.data.* collection.

    Real Blender collections are identity-based: renaming a datablock and then
    allocating a new one under the freed name leaves *both* present. A
    name-keyed dict would clobber the renamed entry, hiding exactly the bug the
    identity-tracking fix guards against - so this holds items in a list and
    resolves lookups by each datablock's current .name.
    """

    def __init__(self) -> None:
        self._items = []

    def __setitem__(self, _name, db) -> None:
        # Setup convenience (tests seed collections with objects[name] = obj);
        # mirrors Blender allocating a datablock, so it just appends.
        self._items.append(db)

    def __getitem__(self, name):
        for db in self._items:
            if db.name == name:
                return db
        raise KeyError(name)

    def get(self, name, default=None):
        for db in self._items:
            if db.name == name:
                return db
        return default

    def new(self, name, *_args, **_kwargs):
        db = FakeDatablock(name)
        self._items.append(db)
        return db

    def remove(self, db, do_unlink=True) -> None:
        for index, existing in enumerate(self._items):
            if existing is db:
                del self._items[index]
                return

    def __contains__(self, name) -> bool:
        return any(db.name == name for db in self._items)

    def __iter__(self):
        return iter(list(self._items))

    def values(self) -> list:
        """
        List the datablocks, as `bpy_prop_collection.values` does.

        Returns:
            list: Every datablock, in allocation order.

        """
        return list(self._items)

    def __len__(self) -> int:
        return len(self._items)


class FakeSlot:
    def __init__(self, material) -> None:
        self.material = material


class FakeModifier:
    def __init__(self, name) -> None:
        self.name = name


class FakeModifierStack(list):
    def remove(self, mod) -> None:
        try:
            list.remove(self, mod)
        except ValueError:
            pass


class FakeMutableObject:
    """Object rich enough for object_state.ObjectState to capture and restore."""

    def __init__(self, name, *, mesh=None) -> None:
        self.name = name
        self.session_uid = _next_uid()
        self.data = mesh
        self.matrix_basis = FakeMatrix()
        self.parent = None
        self.matrix_parent_inverse = FakeMatrix()
        self.material_slots = []
        self.modifiers = FakeModifierStack()


# Every collection name transaction._TRACKED_COLLECTIONS iterates - a
# mutating-path test must stock all of these or _snapshot_ids() raises
# AttributeError, which is exactly how the read-only tests below prove the
# wrapper was skipped: their bpy.data has none of these attributes at all.
_TRACKED_COLLECTIONS = (
    "objects",
    "meshes",
    "curves",
    "materials",
    "textures",
    "images",
    "node_groups",
    "worlds",
    "actions",
    "armatures",
    "cameras",
    "lights",
    "collections",
)


def _load_addon(monkeypatch, *, data=None, use_global_undo=None):
    bpy = types.ModuleType("bpy")
    context = types.SimpleNamespace(
        scene=types.SimpleNamespace(
            blendermcp_use_polyhaven=False,
            blendermcp_use_sketchfab=False,
            blendermcp_use_nd=False,
        ),
    )
    if use_global_undo is not None:
        context.preferences = types.SimpleNamespace(edit=types.SimpleNamespace(use_global_undo=use_global_undo))
    bpy.context = context
    bpy.data = types.SimpleNamespace(**(data or {}))
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
    handlers.undo_post = []
    handlers.redo_post = []
    handlers.depsgraph_update_post = []
    # The four file-lifecycle lists come from the one helper, not from four more
    # hand-rolled assignments: this is the third of the three sites its own
    # docstring says it has, and it was the one still hand-rolling them.
    install_file_lifecycle_handler_lists(handlers)

    app = types.ModuleType("bpy.app")
    app.version = (5, 1, 0)
    app.version_string = "5.1.0"
    app.background = False
    app.handlers = handlers
    app.timers = types.SimpleNamespace(
        is_registered=lambda *_a, **_k: False,
        register=lambda *_a, **_k: None,
        unregister=lambda *_a, **_k: None,
    )
    bpy.app = app

    monkeypatch.setitem(sys.modules, "bpy", bpy)
    monkeypatch.setitem(sys.modules, "bpy.props", props)
    monkeypatch.setitem(sys.modules, "bpy.app", app)
    monkeypatch.setitem(sys.modules, "bpy.app.handlers", handlers)
    mathutils = types.ModuleType("mathutils")
    bvhtree = types.ModuleType("mathutils.bvhtree")
    bvhtree.BVHTree = type("BVHTree", (), {})
    kdtree = types.ModuleType("mathutils.kdtree")
    kdtree.KDTree = type("KDTree", (), {})
    mathutils.bvhtree = bvhtree
    mathutils.kdtree = kdtree
    monkeypatch.setitem(sys.modules, "mathutils", mathutils)
    monkeypatch.setitem(sys.modules, "mathutils.bvhtree", bvhtree)
    monkeypatch.setitem(sys.modules, "mathutils.kdtree", kdtree)
    monkeypatch.setitem(sys.modules, "bmesh", types.ModuleType("bmesh"))

    requests = types.ModuleType("requests")
    requests.utils = types.SimpleNamespace(default_headers=dict)
    requests.exceptions = types.SimpleNamespace(Timeout=TimeoutError)
    monkeypatch.setitem(sys.modules, "requests", requests)

    addon = load_addon_package(monkeypatch, "blender_mcp_addon_transaction_test")
    return addon, bpy


def test_failed_mutating_handler_rolls_back_created_datablocks(monkeypatch) -> None:
    data = {name: FakeCollection() for name in _TRACKED_COLLECTIONS}
    addon, bpy = _load_addon(monkeypatch, data=data)
    server = addon.BlenderMCPServer()

    def fake_handler():
        bpy.data.materials.new(name="Orphan")
        raise RuntimeError("boom")

    monkeypatch.setattr(server, "_build_command_handlers", lambda: {"do_mutate": fake_handler})
    undo_calls = []
    monkeypatch.setattr(bpy.ops.ed, "undo_push", lambda **kw: undo_calls.append(kw))

    response = server.execute_command_internal({"type": "do_mutate", "params": {}})

    assert response == {"status": "error", "message": "boom"}
    assert "Orphan" not in bpy.data.materials
    assert undo_calls == []


def test_successful_mutating_handler_pushes_one_undo_checkpoint(monkeypatch) -> None:
    data = {name: FakeCollection() for name in _TRACKED_COLLECTIONS}
    addon, bpy = _load_addon(monkeypatch, data=data)
    server = addon.BlenderMCPServer()

    def fake_handler():
        bpy.data.materials.new(name="Kept")
        return {"ok": True}

    monkeypatch.setattr(server, "_build_command_handlers", lambda: {"do_mutate": fake_handler})
    undo_calls = []
    monkeypatch.setattr(bpy.ops.ed, "undo_push", lambda **kw: undo_calls.append(kw))

    response = server.execute_command_internal({"type": "do_mutate", "params": {}})

    assert response == {"status": "success", "result": {"ok": True}}
    assert "Kept" in bpy.data.materials
    assert len(undo_calls) == 1
    assert "do_mutate" in undo_calls[0]["message"]


def test_read_only_command_never_snapshots_or_checkpoints(monkeypatch) -> None:
    # Deliberately no bpy.data.* collections at all - if the read-only path
    # ever touched mutation_transaction, _snapshot_ids() would raise
    # AttributeError and this test would error out instead of passing.
    addon, bpy = _load_addon(monkeypatch, data={})
    server = addon.BlenderMCPServer()

    def fake_readonly_handler():
        return {"objects": []}

    monkeypatch.setattr(server, "_build_command_handlers", lambda: {"list_scene_objects": fake_readonly_handler})
    undo_calls = []
    monkeypatch.setattr(bpy.ops.ed, "undo_push", lambda **kw: undo_calls.append(kw))

    response = server.execute_command_internal({"type": "list_scene_objects", "params": {}})

    assert response == {"status": "success", "result": {"objects": []}}
    assert undo_calls == []


def test_renamed_preexisting_datablock_survives_failed_mutation(monkeypatch) -> None:
    # The core audit fix: identity (session_uid), not name, decides "created".
    # The handler renames a pre-existing mesh to a fresh name AND creates a new
    # mesh reusing the freed name, then fails. A name-based diff would do the
    # opposite of what's correct - delete the renamed survivor and keep the new
    # orphan. Identity tracking gets both right.
    data = {name: FakeCollection() for name in _TRACKED_COLLECTIONS}
    addon, bpy = _load_addon(monkeypatch, data=data)
    original = bpy.data.meshes.new(name="Mesh")
    original_uid = original.session_uid
    server = addon.BlenderMCPServer()

    def fake_handler():
        original.name = "Mesh.old"  # rename the pre-existing datablock
        bpy.data.meshes.new(name="Mesh")  # new datablock reusing the freed name
        raise RuntimeError("boom")

    monkeypatch.setattr(server, "_build_command_handlers", lambda: {"do_mutate": fake_handler})
    undo_calls = []
    monkeypatch.setattr(bpy.ops.ed, "undo_push", lambda **kw: undo_calls.append(kw))

    response = server.execute_command_internal({"type": "do_mutate", "params": {}})

    assert response == {"status": "error", "message": "boom"}
    surviving_uids = {m.session_uid for m in bpy.data.meshes}
    assert original_uid in surviving_uids  # renamed pre-existing datablock kept
    assert surviving_uids == {original_uid}  # the name-reusing new datablock removed
    assert undo_calls == []


def test_handler_returning_error_shape_rolls_back_and_reports_error(monkeypatch) -> None:
    data = {name: FakeCollection() for name in _TRACKED_COLLECTIONS}
    addon, bpy = _load_addon(monkeypatch, data=data)
    server = addon.BlenderMCPServer()

    def fake_handler():
        bpy.data.images.new(name="HalfImportedTexture")
        return {"error": "download failed"}

    monkeypatch.setattr(server, "_build_command_handlers", lambda: {"import_asset": fake_handler})
    undo_calls = []
    monkeypatch.setattr(bpy.ops.ed, "undo_push", lambda **kw: undo_calls.append(kw))

    response = server.execute_command_internal({"type": "import_asset", "params": {}})

    assert response == {"status": "error", "message": "download failed"}
    assert "HalfImportedTexture" not in bpy.data.images
    assert undo_calls == []


def test_cancelled_result_does_not_create_undo_checkpoint(monkeypatch) -> None:
    # {"cancelled": True} is a legitimate ok:false outcome (an ND operator the
    # user pressed Esc on), not a failure - it must not create a misleading undo checkpoint.
    data = {name: FakeCollection() for name in _TRACKED_COLLECTIONS}
    addon, bpy = _load_addon(monkeypatch, data=data)
    server = addon.BlenderMCPServer()

    def fake_handler():
        bpy.data.materials.new(name="IdMaterial")
        return {"cancelled": True, "name": "IdMaterial"}

    monkeypatch.setattr(server, "_build_command_handlers", lambda: {"nd_op": fake_handler})
    undo_calls = []
    monkeypatch.setattr(bpy.ops.ed, "undo_push", lambda **kw: undo_calls.append(kw))

    response = server.execute_command_internal({"type": "nd_op", "params": {}})

    assert response == {"status": "success", "result": {"cancelled": True, "name": "IdMaterial"}}
    assert "IdMaterial" in bpy.data.materials
    assert undo_calls == []


def test_captured_object_state_restored_on_failure(monkeypatch) -> None:
    data = {name: FakeCollection() for name in _TRACKED_COLLECTIONS}
    addon, bpy = _load_addon(monkeypatch, data=data)
    target = FakeMutableObject("Widget")
    original_material = FakeDatablock("Steel")
    target.material_slots = [FakeSlot(original_material)]
    target.modifiers = FakeModifierStack([FakeModifier("Bevel")])
    original_matrix_value = target.matrix_basis.value
    bpy.data.objects["Widget"] = target
    server = addon.BlenderMCPServer()

    def fake_handler(object_names):
        target.name = "Widget.renamed"
        target.matrix_basis = FakeMatrix((9.0, 9.0, 9.0))
        target.modifiers.append(FakeModifier("Array"))
        target.material_slots[0].material = FakeDatablock("Gold")
        raise RuntimeError("boom")

    monkeypatch.setattr(server, "_build_command_handlers", lambda: {"manage_modifiers": fake_handler})
    monkeypatch.setattr(bpy.ops.ed, "undo_push", lambda **kw: None)

    response = server.execute_command_internal({"type": "manage_modifiers", "params": {"object_names": ["Widget"]}})

    assert response == {"status": "error", "message": "boom"}
    assert target.name == "Widget"
    # restore assigns a copy of the captured transform, so compare by value
    assert target.matrix_basis.value == original_matrix_value
    assert [mod.name for mod in target.modifiers] == ["Bevel"]  # added modifier removed
    assert target.material_slots[0].material is original_material


def test_geometry_backup_swapped_back_on_failed_geometry_edit(monkeypatch) -> None:
    data = {name: FakeCollection() for name in _TRACKED_COLLECTIONS}
    addon, bpy = _load_addon(monkeypatch, data=data)
    original_mesh = FakeMesh("WidgetMesh", bpy.data.meshes)
    original_mesh_uid = original_mesh.session_uid
    bpy.data.meshes["WidgetMesh"] = original_mesh
    target = FakeMutableObject("Widget", mesh=original_mesh)
    bpy.data.objects["Widget"] = target
    server = addon.BlenderMCPServer()

    def fake_handler(object_names):
        # A real mesh edit mutates obj.data in place; simulate that by marking
        # the live mesh, leaving the pristine backup as the only clean copy.
        target.data.name = "WidgetMesh.edited"
        raise RuntimeError("boom")

    monkeypatch.setattr(server, "_build_command_handlers", lambda: {"mesh_bevel": fake_handler})
    monkeypatch.setattr(bpy.ops.ed, "undo_push", lambda **kw: None)

    response = server.execute_command_internal({"type": "mesh_bevel", "params": {"object_names": ["Widget"]}})

    assert response == {"status": "error", "message": "boom"}
    # The pristine backup is swapped in and renamed to the original data name;
    # the mutated original mesh is removed.
    assert target.data.name == "WidgetMesh"
    surviving_uids = {m.session_uid for m in bpy.data.meshes}
    assert original_mesh_uid not in surviving_uids
    assert target.data.session_uid in surviving_uids


def test_checkpoint_unavailable_is_reported_not_suppressed(monkeypatch) -> None:
    data = {name: FakeCollection() for name in _TRACKED_COLLECTIONS}
    addon, bpy = _load_addon(monkeypatch, data=data, use_global_undo=False)
    server = addon.BlenderMCPServer()

    def fake_handler():
        bpy.data.materials.new(name="Kept")
        return {"ok": True}

    monkeypatch.setattr(server, "_build_command_handlers", lambda: {"do_mutate": fake_handler})
    undo_calls = []
    monkeypatch.setattr(bpy.ops.ed, "undo_push", lambda **kw: undo_calls.append(kw))

    response = server.execute_command_internal({"type": "do_mutate", "params": {}})

    assert response["status"] == "success"
    assert "Kept" in bpy.data.materials
    warnings = response["result"]["warnings"]
    assert any("Undo checkpoint unavailable" in w for w in warnings)
    assert undo_calls == []  # never attempted when undo is known-unavailable


class FakeObject:
    def __init__(self, name) -> None:
        self.name = name
        self.data = types.SimpleNamespace(name=name, materials=FakeMaterialSlots())


class FakeMaterialSlots(list):
    def clear(self) -> None:
        del self[:]


def _objects_collection(*names):
    objects = FakeCollection()
    for name in names:
        objects[name] = FakeObject(name)
    return objects


def test_sync_data_name_validates_all_names_before_mutating_any(monkeypatch) -> None:
    addon, bpy = _load_addon(monkeypatch, data={"objects": _objects_collection("Existing")})
    server = addon.BlenderMCPServer()
    existing = bpy.data.objects["Existing"]
    existing.data.name = "OldData"  # object name and data name now differ, as if freshly duplicated

    with pytest.raises(ValueError, match="Object not found: Missing"):
        server.sync_data_name(object_names=["Existing", "Missing"])

    assert existing.data.name == "OldData"  # sync did not run: validation failed first


def test_clear_materials_validates_all_names_before_mutating_any(monkeypatch) -> None:
    addon, bpy = _load_addon(monkeypatch, data={"objects": _objects_collection("Existing")})
    server = addon.BlenderMCPServer()
    existing = bpy.data.objects["Existing"]
    existing.data.materials.append(FakeDatablock("Paint"))

    with pytest.raises(ValueError, match="Object not found: Missing"):
        server.clear_materials(object_names=["Existing", "Missing"])

    assert list(existing.data.materials) == [existing.data.materials[0]]


class FakeUtilObject:
    def __init__(self, name) -> None:
        self.name = name
        self.display_type = "SOLID"
        self.hide_render = False
        self.visible_camera = True
        self.visible_diffuse = True
        self.visible_glossy = True
        self.visible_shadow = True
        self.visible_transmission = True
        self.visible_volume_scatter = True


def test_nd_mark_as_util_validates_all_names_before_mutating_any(monkeypatch) -> None:
    objects = FakeCollection()
    existing = FakeUtilObject("Existing")
    objects["Existing"] = existing
    addon, _bpy = _load_addon(monkeypatch, data={"objects": objects})
    server = addon.BlenderMCPServer()

    with pytest.raises(ValueError, match="Object not found: Missing"):
        server.nd_mark_as_util(object_names=["Existing", "Missing"])

    assert existing.display_type == "SOLID"
    assert existing.hide_render is False


# ---------------------------------------------------------------------------
# Task 4: a rollback that outlives the database it snapshotted
# ---------------------------------------------------------------------------


# The datablocks each reproduction links from its stub library.
_LINKED_FROM_CANON = (("objects", "HeroBody"), ("meshes", "HeroMesh"), ("collections", "CanonHero"))


class FakeLinkedDatablock(FakeDatablock):
    """A datablock linked from a library: `library` names the `Library` it came from."""

    def __init__(self, name: str, library: FakeDatablock) -> None:
        """
        Allocate the datablock with a fresh session_uid.

        Args:
            name: The datablock's name.
            library: The stub `Library` it is linked from.

        """
        super().__init__(name)
        self.library = library


def _replace_whole_database(data: dict[str, FakeCollection]) -> dict[str, list[int]]:
    """
    Model a file load: every tracked datablock is freed and the new file's take their place.

    Each replacement gets a fresh `session_uid`, which is what a load does to
    every datablock in the database - including ones with the same name.

    Args:
        data: The stub `bpy.data` collections, keyed by collection name.

    Returns:
        dict[str, list[int]]: The new file's session_uids, per collection.

    """
    loaded = {}
    for coll_name, collection in data.items():
        names = [db.name for db in collection] or [f"{coll_name}.from_new_file"]
        for db in list(collection):
            collection.remove(db)
        loaded[coll_name] = [collection.new(name).session_uid for name in names]
    return loaded


def _reload_library_contents(data: dict[str, FakeCollection], library: FakeDatablock) -> list[int]:
    """
    Model `lib.reload()`: every datablock linked from `library` comes back with a fresh `session_uid`.

    Measured on 5.2.2 by `scripts/blender_probes/library_replace_handlers.py`.
    Local datablocks and the `Library` itself keep theirs.

    Args:
        data: The stub `bpy.data` collections, keyed by collection name.
        library: The stub library being reloaded.

    Returns:
        list[int]: The reloaded datablocks' new session_uids.

    """
    reloaded = []
    for collection in data.values():
        for db in [db for db in collection if getattr(db, "library", None) is library]:
            collection.remove(db)
            fresh = FakeLinkedDatablock(db.name, library)
            collection[db.name] = fresh
            reloaded.append(fresh.session_uid)
    return reloaded


def test_regression_guard_a_transaction_unaware_of_a_file_swap_removes_the_whole_new_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Task 4 Step 1's reproduction, kept as a guard on the mechanism the fix defends against.

    `Transaction.begin()` snapshots the *pre-load* session_uids; a load gives
    every datablock a fresh one; a raise then makes `_new_datablocks()` read the
    whole new file as "created by this request". This passed against the
    unmodified code and still passes: nothing here tells the transaction a swap
    happened. It stays so that the tests proving the fix (a swap command
    bypassing the transaction; `load_post` invalidating one) cannot pass on a
    harness that never reproduced the hazard in the first place.

    Raises:
        RuntimeError: Inside the transaction, to trigger the rollback.

    """
    data = {name: FakeCollection() for name in _TRACKED_COLLECTIONS}
    addon, bpy = _load_addon(monkeypatch, data=data)
    transaction = sys.modules[f"{addon.__name__}.transaction"]
    bpy.data.objects["Hero"] = FakeDatablock("Hero")
    bpy.data.meshes["HeroMesh"] = FakeDatablock("HeroMesh")

    with pytest.raises(RuntimeError, match="after the load"), transaction.mutation_transaction("do_mutate"):
        loaded = _replace_whole_database(data)
        assert sum(len(uids) for uids in loaded.values()) == len(_TRACKED_COLLECTIONS)
        raise RuntimeError("after the load")

    assert {name: len(collection) for name, collection in data.items()} == dict.fromkeys(data, 0)


def test_regression_guard_a_transaction_unaware_of_a_library_reload_removes_the_reloaded_contents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Task 4 Step 1b's reproduction: the reload shape, reachable by a command that is not a session swap.

    Only the datablocks linked from one library get fresh session_uids - the
    shape `lib.reload()` produces - and a raise then removes exactly those,
    while the local mesh beside them survives. Passed against the unmodified
    code and still passes, for the same reason as the Step 1 guard above: the
    transaction here is told nothing. `_DATABLOCK_REPLACING_COMMANDS` and the
    replace-in-progress flag are what keep a real reload out of this shape.

    Raises:
        RuntimeError: Inside the transaction, to trigger the rollback.

    """
    data = {name: FakeCollection() for name in _TRACKED_COLLECTIONS}
    addon, bpy = _load_addon(monkeypatch, data=data)
    transaction = sys.modules[f"{addon.__name__}.transaction"]
    library = FakeDatablock("canon.blend")
    for coll_name, name in _LINKED_FROM_CANON:
        data[coll_name][name] = FakeLinkedDatablock(name, library)
    local = bpy.data.meshes.new("LocalMesh")

    with pytest.raises(RuntimeError, match="after the reload"), transaction.mutation_transaction("do_mutate"):
        reloaded = _reload_library_contents(data, library)
        assert len(reloaded) == len(_LINKED_FROM_CANON)
        raise RuntimeError("after the reload")

    survivors = {db.session_uid for collection in data.values() for db in collection}
    assert survivors == {local.session_uid}
