r"""
Run the addon's real `transaction.py` and `session.py` through a link, a reload and a load in Blender.

`tests/test_transaction_session_swap.py` covers the same rollback with stubs.
Expected results, on a fixture built here:

A. A failed link inside `mutation_transaction`: the Library and everything
   linked from it are removed, and every datablock that existed before keeps
   its session_uid.
B. A reload inside `replacing_library_contents`, then a raise:
   `blend_import_post` invalidates the transaction, the linked contents
   survive, and the error carries the rollback-skipped warning.
C. The same reload outside that context, the hazard B guards against: rollback
   removes the reloaded contents, while the Library predates the transaction
   and stays.
D. A file load inside a transaction holding a geometry backup, then a raise:
   `load_post` invalidates it, the loaded file stays whole, and the freed backup
   is never passed to `remove()`.
E. Why libraries are removed last: `libraries.remove` frees the datablocks
   linked from it, and a later `remove()` on a reference to one raises
   `ReferenceError`, which `_remove_datablocks` would suppress.

From the repository root::

    /opt/homebrew/bin/blender --background --factory-startup \
        --python scripts/blender_probes/transaction_library_rollback.py
"""

import contextlib
import importlib
import os
import pathlib
import sys
import tempfile
import types

import bpy

ADDON_DIR = pathlib.Path(__file__).resolve().parents[2] / "src/blender_mcp/bundled/addon"
# A bare parent package lets these modules import their siblings without
# running the addon's `__init__.py`.
_PACKAGE = types.ModuleType("probe_addon")
_PACKAGE.__path__ = [str(ADDON_DIR)]  # type: ignore[attr-defined]
# `handlers.file_lifecycle` stamps the add-on version and protocol into a saved file's
# provenance block. The real `__init__` would register Blender classes, so only the two
# constants are provided, with fixed values so a version bump cannot move this transcript.
_PACKAGE.ADDON_PROTOCOL_VERSION = 0  # type: ignore[attr-defined]
_PACKAGE.bl_info = {"version": (0, 0, 0)}  # type: ignore[attr-defined]
sys.modules["probe_addon"] = _PACKAGE
session = importlib.import_module("probe_addon.session")
transaction = importlib.import_module("probe_addon.transaction")

print("=== BLENDER ===", bpy.app.version_string)
work = tempfile.mkdtemp(prefix="t4_txn_")
canon = os.path.join(work, "canon.blend")
shot = os.path.join(work, "shot.blend")


def id_census() -> set[tuple[str, int, str]]:
    """
    Read every datablock in the tracked collections as (collection, session_uid, name).

    Returns:
        set[tuple[str, int, str]]: One entry per datablock.

    """
    return {
        (name, datablock.session_uid, datablock.name)
        for name in transaction._TRACKED_COLLECTIONS
        for datablock in getattr(bpy.data, name)
    }


def link_canon() -> bpy.types.Library:
    """
    Link the canon collection into the scene, as `link_canon_library` does.

    Returns:
        bpy.types.Library: The library datablock the link created.

    """
    with bpy.data.libraries.load(canon, link=True) as (_src, dst):  # pyright: ignore[reportGeneralTypeIssues]  # blender-python-stubs types libraries.load() as None; it is a context manager
        dst.collections = ["CanonHero"]
    library = bpy.data.libraries[-1]
    bpy.context.scene.collection.children.link(next(c for c in bpy.data.collections if c.library == library))
    return library


def linked_names() -> list[str]:
    """
    List the tracked datablocks that are linked from any library.

    Returns:
        list[str]: `"<collection>/<name>"`, sorted.

    """
    return sorted(
        f"{name}/{datablock.name}"
        for name in transaction._TRACKED_COLLECTIONS
        if name != "libraries"
        for datablock in getattr(bpy.data, name)
        if datablock.library is not None
    )


# --- fixture: a canon library, and a shot with local data of its own ---
bpy.ops.wm.read_factory_settings(use_empty=True)
hero = bpy.data.collections.new("CanonHero")
bpy.context.scene.collection.children.link(hero)
hero_mesh = bpy.data.meshes.new("HeroMesh")
hero_mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
hero_mesh.materials.append(bpy.data.materials.new("HeroPaint"))
hero.objects.link(bpy.data.objects.new("HeroBody", hero_mesh))
bpy.ops.wm.save_as_mainfile(filepath=canon, compress=False, relative_remap=False)

bpy.ops.wm.read_factory_settings(use_empty=True)
local_mesh = bpy.data.meshes.new("LocalMesh")
local_mesh.from_pydata([(0, 0, 0), (0, 0, 1), (1, 0, 0)], [], [(0, 1, 2)])
local = bpy.data.objects.new("LocalBody", local_mesh)
bpy.context.scene.collection.objects.link(local)
bpy.data.materials.new("LocalPaint").use_fake_user = True
bpy.ops.wm.save_as_mainfile(filepath=shot, compress=False, relative_remap=False)
session.register_handlers()

print("\n=== A: a failed link inside mutation_transaction ===")
before = id_census()
try:
    with transaction.mutation_transaction("link_canon_library"):
        link_canon()
        print("  inside: libraries =", len(bpy.data.libraries), "linked =", linked_names())
        raise RuntimeError("linked collection not found")
except RuntimeError as exc:
    print(f"  raised {type(exc).__name__}: {exc}")
after = id_census()
print("  after rollback: libraries =", len(bpy.data.libraries), "linked =", linked_names())
print("  pre-existing datablocks all present with the same session_uid:", before <= after)
print("  anything left that did not exist before:", sorted(after - before) or "NOTHING")
print("  pre-existing datablocks removed:", sorted(before - after) or "NONE")

print("\n=== B: lib.reload() inside replacing_library_contents, then a raise ===")
library = link_canon()
linked_before = linked_names()
local_before = {uid for coll, uid, _name in id_census() if coll != "libraries"} - {
    datablock.session_uid
    for name in transaction._TRACKED_COLLECTIONS
    for datablock in getattr(bpy.data, name)
    if getattr(datablock, "library", None) is not None
}
try:
    with transaction.mutation_transaction("do_mutate") as txn:
        with transaction.replacing_library_contents():
            library.reload()
        print("  transaction invalidated by blend_import_post:", txn.invalidated)
        raise RuntimeError("boom")
except transaction.RollbackSkippedError as exc:
    print(f"  raised RollbackSkippedError; carries the warning: {transaction.ROLLBACK_SKIPPED_WARNING in str(exc)}")
print("  linked contents before/after:", len(linked_before), "/", len(linked_names()))
print("  replace flag cleared:", not transaction.library_replace_in_progress())
local_after = {uid for _coll, uid, _name in id_census()}
print("  local session_uids unchanged by the reload:", local_before <= local_after)

print("\n=== C: the same reload WITHOUT the flag (the hazard), then a raise ===")
try:
    with transaction.mutation_transaction("do_mutate") as txn:
        library.reload()
        print("  transaction invalidated:", txn.invalidated)
        raise RuntimeError("boom")
except RuntimeError as exc:
    print(f"  raised {type(exc).__name__}")
print("  linked contents before/after:", len(linked_before), "/", len(linked_names()))

print("\n=== D: open_mainfile inside a transaction holding a geometry backup, then a raise ===")
loaded: set[tuple[str, int, str]] = set()


def load_then_fail() -> None:
    """
    Open the shot from inside a geometry-capturing transaction, then fail.

    Raises:
        RuntimeError: Always, after the load; the transaction converts it.

    """
    with transaction.mutation_transaction("mesh_bevel", [bpy.data.objects["LocalBody"]], capture_geometry=True) as txn:
        print("  backups captured:", len(transaction.backup_datablock_ids(txn._states)))
        pre_load = {uid for _name, uid, _db in id_census()}
        bpy.ops.wm.open_mainfile(filepath=shot, load_ui=False, use_scripts=False)
        reused = pre_load & {uid for _name, uid, _db in id_census()}
        print(f"  session_uids surviving the load: {len(reused)} of {len(pre_load)}")
        print("  transaction invalidated by load_post:", txn.invalidated, "; states held:", len(txn._states))
        loaded.update(id_census())
        raise RuntimeError("boom")


try:
    load_then_fail()
except transaction.RollbackSkippedError as exc:
    print(f"  raised RollbackSkippedError; carries the warning: {transaction.ROLLBACK_SKIPPED_WARNING in str(exc)}")
print("  loaded file intact after the failed command:", id_census() == loaded)
print("  active transaction cleared:", transaction.active_transaction() is None)

print("\n=== E: removing a Library first, then its linked datablocks ===")
library = link_canon()
references = [
    (name, datablock)
    for name in ("objects", "meshes", "materials", "collections")
    for datablock in getattr(bpy.data, name)
    if datablock.library == library
]
bpy.data.libraries.remove(library, do_unlink=True)
print("  after libraries.remove: linked left =", linked_names())
for name, datablock in references:
    try:
        getattr(bpy.data, name).remove(datablock, do_unlink=True)
    except ReferenceError as exc:
        print(f"  {name}.remove(<freed>) raised ReferenceError: {exc}")
    else:
        print(f"  {name}.remove(<freed>) did not raise")

with contextlib.suppress(Exception):
    session.unregister_handlers()
print("\nprobe completed")
