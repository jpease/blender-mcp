"""Regression coverage for the mutation_transaction rollback/checkpoint contract."""

import sys
import types

import pytest

from conftest import load_addon
from datablock_doubles import (
    LINKED_FROM_CANON,
    TRACKED_COLLECTIONS,
    FakeCollection,
    FakeDatablock,
    FakeLinkedDatablock,
    FakeMatrix,
    FakeMesh,
    FakeModifierStack,
    FakeMutableObject,
    reload_library_contents,
    replace_whole_database,
)


class FakeSlot:
    def __init__(self, material) -> None:
        self.material = material


class FakeModifier:
    def __init__(self, name) -> None:
        self.name = name


def test_failed_mutating_handler_rolls_back_created_datablocks(monkeypatch) -> None:
    data = {name: FakeCollection() for name in TRACKED_COLLECTIONS}
    addon, bpy = load_addon(monkeypatch, data=data)
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


def test_a_refused_request_is_logged_without_a_traceback(monkeypatch, caplog) -> None:
    """
    A handler refusing the client's own arguments is not a fault, and must not read as one.

    Every unconfirmed `reset_session` and every path outside the file roots used to print
    `Command X failed in its handler` plus a full traceback into Blender's console, which is
    where an actual fault also appears. An operator who learns to skim those skims the one
    that matters. The client is told exactly the same thing either way.
    """
    addon, _bpy = load_addon(monkeypatch, data={})
    server = addon.BlenderMCPServer()

    def refuses():
        raise ValueError("reset_session discards the open file; pass confirm_reset=true")

    def breaks():
        raise RuntimeError("boom")

    monkeypatch.setattr(server, "_build_command_handlers", lambda: {"list_scene_objects": refuses, "broken": breaks})

    with caplog.at_level("DEBUG"):
        refusal = server.execute_command_internal({"type": "list_scene_objects", "params": {}})
        fault = server.execute_command_internal({"type": "broken", "params": {}})

    assert refusal == {"status": "error", "message": "reset_session discards the open file; pass confirm_reset=true"}
    assert fault == {"status": "error", "message": "boom"}

    by_message = {record.getMessage(): record for record in caplog.records}
    refused = next(record for message, record in by_message.items() if "refused" in message)
    failed = next(record for message, record in by_message.items() if "failed in its handler" in message)
    assert refused.exc_info is None, "a refusal still carries a traceback"
    assert "pass confirm_reset=true" in refused.getMessage(), "the refusal does not say what was refused"
    assert failed.exc_info is not None, "an unexpected failure lost its traceback"


def test_successful_mutating_handler_pushes_one_undo_checkpoint(monkeypatch) -> None:
    data = {name: FakeCollection() for name in TRACKED_COLLECTIONS}
    addon, bpy = load_addon(monkeypatch, data=data)
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
    addon, bpy = load_addon(monkeypatch, data={})
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
    data = {name: FakeCollection() for name in TRACKED_COLLECTIONS}
    addon, bpy = load_addon(monkeypatch, data=data)
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
    data = {name: FakeCollection() for name in TRACKED_COLLECTIONS}
    addon, bpy = load_addon(monkeypatch, data=data)
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
    data = {name: FakeCollection() for name in TRACKED_COLLECTIONS}
    addon, bpy = load_addon(monkeypatch, data=data)
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
    data = {name: FakeCollection() for name in TRACKED_COLLECTIONS}
    addon, bpy = load_addon(monkeypatch, data=data)
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
    data = {name: FakeCollection() for name in TRACKED_COLLECTIONS}
    addon, bpy = load_addon(monkeypatch, data=data)
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
    data = {name: FakeCollection() for name in TRACKED_COLLECTIONS}
    addon, bpy = load_addon(monkeypatch, data=data, use_global_undo=False)
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


def test_rollback_removes_objects_first_and_libraries_last_each_in_reverse(monkeypatch) -> None:
    """
    The ordering rule a rollback removes by, checked on the ordering alone.

    A later object can reference an earlier one, and removing a Library frees
    every datablock linked from it, so a removal taken out of turn either
    raises a ReferenceError that `contextlib.suppress` hides or frees a
    datablock twice. Through `bpy.data` the order is only visible as the
    removals that happen to succeed; `removal_order` is pure, so it is visible
    directly.
    """
    addon, _bpy = load_addon(monkeypatch, data={})
    transaction = sys.modules[f"{addon.__name__}.transaction"]

    ordered = transaction.removal_order(
        [
            ("meshes", "mesh1"),
            ("objects", "obj1"),
            ("libraries", "lib1"),
            ("objects", "obj2"),
            ("materials", "mat1"),
            ("libraries", "lib2"),
        ]
    )

    assert ordered == [
        ("objects", "obj2"),
        ("objects", "obj1"),
        ("materials", "mat1"),
        ("meshes", "mesh1"),
        ("libraries", "lib2"),
        ("libraries", "lib1"),
    ]


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
    addon, bpy = load_addon(monkeypatch, data={"objects": _objects_collection("Existing")})
    server = addon.BlenderMCPServer()
    existing = bpy.data.objects["Existing"]
    existing.data.name = "OldData"  # object name and data name now differ, as if freshly duplicated

    with pytest.raises(ValueError, match="Object not found: Missing"):
        server.sync_data_name(object_names=["Existing", "Missing"])

    assert existing.data.name == "OldData"  # sync did not run: validation failed first


def test_clear_materials_validates_all_names_before_mutating_any(monkeypatch) -> None:
    addon, bpy = load_addon(monkeypatch, data={"objects": _objects_collection("Existing")})
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
    addon, _bpy = load_addon(monkeypatch, data={"objects": objects})
    server = addon.BlenderMCPServer()

    with pytest.raises(ValueError, match="Object not found: Missing"):
        server.nd_mark_as_util(object_names=["Existing", "Missing"])

    assert existing.display_type == "SOLID"
    assert existing.hide_render is False


# ---------------------------------------------------------------------------
# A rollback that outlives the database it snapshotted
# ---------------------------------------------------------------------------


def test_regression_guard_a_transaction_unaware_of_a_file_swap_removes_the_whole_new_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Reproduce the file-swap hazard, as a guard on the harness.

    A load gives every datablock a fresh session_uid, so a rollback after it, with
    the transaction told nothing, removes the whole new file. Without this guard
    the tests of the defences could pass on a harness that never reproduces the
    hazard.

    Raises:
        RuntimeError: Inside the transaction, to trigger the rollback.

    """
    data = {name: FakeCollection() for name in TRACKED_COLLECTIONS}
    addon, bpy = load_addon(monkeypatch, data=data)
    transaction = sys.modules[f"{addon.__name__}.transaction"]
    bpy.data.objects["Hero"] = FakeDatablock("Hero")
    bpy.data.meshes["HeroMesh"] = FakeDatablock("HeroMesh")

    with pytest.raises(RuntimeError, match="after the load"), transaction.mutation_transaction("do_mutate"):
        loaded = replace_whole_database(data)
        assert sum(len(uids) for uids in loaded.values()) == len(TRACKED_COLLECTIONS)
        raise RuntimeError("after the load")

    assert {name: len(collection) for name, collection in data.items()} == dict.fromkeys(data, 0)


def test_regression_guard_a_transaction_unaware_of_a_library_reload_removes_the_reloaded_contents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Reproduce the reload hazard, reachable by a command that is not a session swap.

    Only datablocks linked from the reloaded library get fresh session_uids, so a
    rollback removes exactly those and the local mesh survives. In the addon,
    `CommandSpec.datablock_replacing` and the replace-in-progress flag keep a real
    reload out of this shape.

    Raises:
        RuntimeError: Inside the transaction, to trigger the rollback.

    """
    data = {name: FakeCollection() for name in TRACKED_COLLECTIONS}
    addon, bpy = load_addon(monkeypatch, data=data)
    transaction = sys.modules[f"{addon.__name__}.transaction"]
    library = FakeDatablock("canon.blend")
    for coll_name, name in LINKED_FROM_CANON:
        data[coll_name][name] = FakeLinkedDatablock(name, library)
    local = bpy.data.meshes.new("LocalMesh")

    with pytest.raises(RuntimeError, match="after the reload"), transaction.mutation_transaction("do_mutate"):
        reloaded = reload_library_contents(data, library)
        assert len(reloaded) == len(LINKED_FROM_CANON)
        raise RuntimeError("after the reload")

    survivors = {db.session_uid for collection in data.values() for db in collection}
    assert survivors == {local.session_uid}


# ---------------------------------------------------------------------------
# What the save will discard: the warning every mutating reply carries.
# ---------------------------------------------------------------------------


def _mutating_reply(monkeypatch: pytest.MonkeyPatch, prepare) -> dict:
    """
    Run one successful mutating handler and return its reply.

    Args:
        monkeypatch: The active monkeypatch fixture.
        prepare: Callable given `bpy`, run inside the handler, that creates the
            datablock under test.

    Returns:
        dict: The command response.

    """
    data = {name: FakeCollection() for name in TRACKED_COLLECTIONS}
    addon, bpy = load_addon(monkeypatch, data=data)
    server = addon.BlenderMCPServer()

    def fake_handler():
        prepare(bpy)
        return {"ok": True}

    monkeypatch.setattr(server, "_build_command_handlers", lambda: {"do_mutate": fake_handler})
    monkeypatch.setattr(bpy.ops.ed, "undo_push", lambda **_kw: None)
    return server.execute_command_internal({"type": "do_mutate", "params": {}})


def test_persistence_an_unreferenced_created_datablock_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """A material nothing references is lost at save, so the reply must say so."""

    def create(bpy) -> None:
        bpy.data.materials.new(name="Orphan").users = 0

    response = _mutating_reply(monkeypatch, create)

    warnings = response["result"]["warnings"]
    assert len(warnings) == 1
    assert "have no user" in warnings[0]
    assert "materials:Orphan" in warnings[0]


def test_persistence_a_fake_user_datablock_is_not_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """`ID.users` counts the fake user, so a deliberately-kept datablock is not a loss."""

    def create(bpy) -> None:
        kept = bpy.data.actions.new(name="Kept")
        kept.users = 1
        kept.use_fake_user = True

    assert _mutating_reply(monkeypatch, create)["result"] == {"ok": True}


def test_persistence_an_assigned_datablock_is_not_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """A datablock with a real user survives the save, so no warning rides along."""

    def create(bpy) -> None:
        bpy.data.materials.new(name="Assigned").users = 1

    assert _mutating_reply(monkeypatch, create)["result"] == {"ok": True}


def test_persistence_a_linked_datablock_is_never_this_commands_authorship(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A zero-user datablock belonging to another file is that file's problem, not this reply's."""

    def create(bpy) -> None:
        linked = bpy.data.meshes.new(name="CanonMesh")
        linked.users = 0
        linked.library = FakeDatablock("canon.blend")

    assert _mutating_reply(monkeypatch, create)["result"] == {"ok": True}


def test_persistence_the_warning_names_at_most_five_and_counts_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The warning shares an 8 KiB reply budget, so it is bounded at the source."""

    def create(bpy) -> None:
        for index in range(8):
            bpy.data.materials.new(name=f"Orphan{index}").users = 0

    warning = _mutating_reply(monkeypatch, create)["result"]["warnings"][0]

    assert warning.startswith("8 datablocks")
    assert warning.count("materials:") == 5
    assert "(+3 more)" in warning
