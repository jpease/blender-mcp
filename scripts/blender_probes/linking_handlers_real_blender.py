r"""
Drive the real Task 7 handlers - `handlers/linking.py` - against real files in Blender (plan Task 7 Step 5).

The handlers are the addon's own mixins, loaded with the real `session.py`,
`transaction.py`, `file_paths.py` and `text_hygiene.py` (no socket server: the
addon refuses to start one under `--background`). Sections:

A. **The gate, two commands.** `link_canon_library` (instanced) ->
   `create_override(collection_uid)` -> `save_shot(relative_remap=False)` ->
   `open_shot`: the reopened library is `is_missing=False`, and an object inside
   the override is `is_editable=True`, `is_system_override=False`. The linked
   instance the override replaced is gone from the scene, and the linked
   collection still survives the save.
B. **The gate, one command.** `link_canon_library(as_override=True)` -> save -> reopen.
C. **Criterion 8, captured.** A library whose file was deleted: the raw
   `lib.reload()` text, then what `reload_library` returns - for an absolute
   link and for a `//` link inside a directory named `Smith, John`. Then
   `relocate_library` to a missing file (refused before Blender) and to a
   truncated `.blend` that passes the magic check (Blender fails; the previous
   path is restored and the library still works).
D. **A failed link through the real `mutation_transaction`.** A name absent from
   the file (refused inside `libraries.load`: no Library), and a failure
   injected after a successful link (the transaction removes the Library).
E. **Unlink.** Two libraries, a user's zero-user material, and a local material
   used only by an override object; unlink one library with `purge_orphans`:
   the other library and the user's material survive, the material the unlink
   orphaned is purged, and the report counts by collection.
F. **Scripts.** With `use_scripts_auto_execute` on, `link_canon_library` and
   `reload_library` refuse.
G. **Cycle-1 repair: a failed multi-collection override keeps a pre-existing placement.**
   A saved shot already holds `CanonHero` instanced and `CanonProp` overridden;
   `link_canon_library([CanonHero, CanonProp], as_override=True)` inside the real
   transaction is refused, and the scene root is unchanged, also after save and reopen.
H. **Cycle-1 repair: relocate to a file lacking the linked datablocks** reports
   them missing, with a warning.
I. **Cycle-1 repair: relocate of an indirect library** is refused.
J. **Cycle-1 repair: a hostile `Library.name`** holding an absolute path never
   reaches the client from a failed reload.
K. **Cycle-2 repair: a nested request.** `nest.blend` holds `Parent` containing
   `Child`. `link_canon_library([Parent, Child], as_override=True)` and
   `[Child, Parent]`, each on a fresh shot and again with both already linked
   and placed: overriding the parent overrides the child, so the request must
   not build a second copy of it. Printed: the result or refusal, every
   collection and `ChildBody` object with its override state, and the root.
L. **Cycle-2 repair: a newline in a hostile `Library.name`** never lets its
   directory reach the client.
M. **Cycle-3 repair: an inner collection already overridden.** With `Child`
   overridden first, `create_override(Parent)` and
   `link_canon_library(["Parent"], as_override=True)` must be refused rather
   than override `Child` a second time; the probe saves and reopens to show what
   persists.

From the repository root::

    /opt/homebrew/bin/blender --background --factory-startup \
        --python scripts/blender_probes/linking_handlers_real_blender.py
"""

import importlib
import os
import pathlib
import shutil
import sys
import tempfile
import types

from collections.abc import Callable

import bpy

ROOT = pathlib.Path(__file__).resolve().parents[2]
ADDON_DIR = ROOT / "src/blender_mcp/bundled/addon"
# A bare parent package, so the handler modules import their siblings relatively
# without executing the addon's `__init__.py` (which would register UI classes).
_PACKAGE = types.ModuleType("probe_addon")
_PACKAGE.__path__ = [str(ADDON_DIR)]  # type: ignore[attr-defined]
sys.modules["probe_addon"] = _PACKAGE
session = importlib.import_module("probe_addon.session")
transaction = importlib.import_module("probe_addon.transaction")
file_lifecycle = importlib.import_module("probe_addon.handlers.file_lifecycle")
linking = importlib.import_module("probe_addon.handlers.linking")


class ProbeServer(file_lifecycle.FileLifecycleHandlersMixin, linking.LinkingHandlersMixin):
    """The two mixins with the one server method they read: an empty, stable capability table."""

    @staticmethod
    def _build_command_handlers() -> dict[str, object]:
        """
        Stand in for the server's dispatch table.

        Returns:
            dict[str, object]: Always empty.

        """
        return {}


def attempt(label: str, call: Callable[[], object]) -> object:
    """
    Run one handler call and print what a client would receive.

    Args:
        label: What is printed.
        call: The handler call.

    Returns:
        object: The result, or None when it raised.

    """
    try:
        result = call()
    except (ValueError, RuntimeError) as exc:
        print(f"  [{label}] {type(exc).__name__} -> {str(exc)!r}")
        return None
    print(f"  [{label}] OK")
    return result


def build(path: pathlib.Path, collection: str, body: str) -> None:
    """
    Write a canon `.blend` holding one collection with one mesh object.

    Args:
        path: Where to write it.
        collection: The collection name.
        body: The object name.

    """
    bpy.ops.wm.read_homefile(use_empty=True, use_factory_startup=True, load_ui=False)
    coll = bpy.data.collections.new(collection)
    bpy.data.scenes[0].collection.children.link(coll)
    mesh = bpy.data.meshes.new(f"{body}Mesh")
    mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
    coll.objects.link(bpy.data.objects.new(body, mesh))
    bpy.ops.wm.save_as_mainfile(filepath=str(path), compress=False, relative_remap=False)


def fresh_shot(path: pathlib.Path) -> None:
    """
    Start an empty, saved shot so `//` paths and in-place saves have a file to be relative to.

    Args:
        path: The shot file.

    """
    bpy.ops.wm.read_homefile(use_empty=True, use_factory_startup=True, load_ui=False)
    bpy.ops.wm.save_as_mainfile(filepath=str(path), compress=False, relative_remap=False)


def override_objects_state() -> list[tuple[str, int, bool, bool, object]]:
    """
    Read every object's override state straight from Blender, independent of the handlers.

    Returns:
        list: `(name, session_uid, linked, is_editable, is_system_override)` per object.

    """
    return [
        (
            obj.name,
            obj.session_uid,
            obj.library is not None,
            obj.is_editable,
            obj.override_library.is_system_override if obj.override_library else None,
        )
        for obj in bpy.data.objects
    ]


for name in ("BLENDERMCP_FILE_ROOTS", "BLENDERMCP_OUTPUT_ROOTS"):
    os.environ.pop(name, None)
session.register_handlers()
server = ProbeServer()
work = pathlib.Path(tempfile.mkdtemp(prefix="t7_handlers_"))
canon, props = work / "canon.blend", work / "props.blend"
print("=== BLENDER ===", bpy.app.version_string)
build(canon, "CanonHero", "HeroBody")
build(props, "Props", "Crate")
print("  'id_type' on an ID:", bpy.data.scenes[0].id_type, " batch_remove present:", hasattr(bpy.data, "batch_remove"))

print("\n=== A. link (instanced) -> create_override -> save_shot -> open_shot ===")
shot = work / "shot_a.blend"
fresh_shot(shot)
linked = server.link_canon_library(str(canon), collections=["CanonHero"])
collection_uid = linked["collections"][0]["session_uid"]
print("  link result:", linked["library"], linked["collections"])
print("  scene children after link:", [(c.name, c.library is not None) for c in bpy.data.scenes[0].collection.children])
override = server.create_override(collection_uid)
print("  override:", override["override"], " replaced_instances:", override["replaced_instances"])
print("  override objects:", override["objects"])
print(
    "  scene children after override:",
    [(c.name, c.library is not None) for c in bpy.data.scenes[0].collection.children],
)
attempt("save_shot in place", lambda: server.save_shot(confirm_overwrite=True, relative_remap=False))
attempt("open_shot", lambda: server.open_shot(str(shot)))
listing = server.list_libraries()
print(
    "  REOPENED list_libraries:",
    [(lib["name"], lib["is_missing"], lib["version"], lib["datablock_count"]) for lib in listing["libraries"]],
)
print("  REOPENED objects (name, uid, linked, is_editable, is_system_override):", override_objects_state())
print(
    "  REOPENED collections:",
    [(c.name, c.library is not None, c.override_library is not None) for c in bpy.data.collections],
)

print("\n=== B. link_canon_library(as_override=True) -> save_shot -> open_shot ===")
shot = work / "shot_b.blend"
fresh_shot(shot)
one_call = server.link_canon_library(str(canon), collections=["CanonHero"], as_override=True)
print("  overrides:", [report["override"] for report in one_call["overrides"]])
attempt("save_shot in place", lambda: server.save_shot(confirm_overwrite=True, relative_remap=False))
attempt("open_shot", lambda: server.open_shot(str(shot)))
print("  REOPENED libraries:", [(lib["name"], lib["is_missing"]) for lib in server.list_libraries()["libraries"]])
print("  REOPENED objects:", override_objects_state())

print("\n=== C. criterion 8: failures that carry a path ===")
gone = work / "gone_canon.blend"
shutil.copyfile(canon, gone)
fresh_shot(work / "shot_c.blend")
library_uid = server.link_canon_library(str(gone), collections=["CanonHero"])["library"]["session_uid"]
os.remove(gone)
library = next(lib for lib in bpy.data.libraries if lib.session_uid == library_uid)
try:
    library.reload()
except RuntimeError as exc:
    print(f"  RAW absolute   : {str(exc)!r}")
attempt("reload_library, absolute link", lambda: server.reload_library(library_uid))
smith = work / "Smith, John"
smith.mkdir()
shutil.copyfile(canon, smith / "canon.blend")
fresh_shot(smith / "shot.blend")
library_uid = server.link_canon_library(str(smith / "canon.blend"), collections=["CanonHero"], relative=True)[
    "library"
]["session_uid"]
library = next(lib for lib in bpy.data.libraries if lib.session_uid == library_uid)
print("  relative link stored as:", repr(library.filepath))
os.remove(smith / "canon.blend")
try:
    library.reload()
except RuntimeError as exc:
    print(f"  RAW relative   : {str(exc)!r}")
attempt("reload_library, // link in 'Smith, John'", lambda: server.reload_library(library_uid))
attempt("relocate_library to a missing file", lambda: server.relocate_library(library_uid, str(work / "nope.blend")))
truncated = smith / "truncated.blend"
truncated.write_bytes(canon.read_bytes()[:64])
shutil.copyfile(canon, smith / "canon.blend")
before = library.filepath
attempt("relocate_library to a truncated .blend", lambda: server.relocate_library(library_uid, str(truncated)))
print("  filepath restored:", library.filepath == before, " is_missing:", library.is_missing)
reloaded = attempt("reload_library after the restore", lambda: server.reload_library(library_uid))
print("  datablocks after the restore:", reloaded and [d["name"] for d in reloaded["datablocks"]])  # type: ignore[index]
attempt(
    "link_canon_library of a missing file",
    lambda: server.link_canon_library(str(work / "nope.blend"), collections=["X"]),
)

print("\n=== D. failed links through the real mutation_transaction ===")
fresh_shot(work / "shot_d.blend")
with_transaction = transaction.mutation_transaction


def transacted(call: Callable[[], object]) -> Callable[[], object]:
    """
    Run a handler call inside the real transaction, as `server_core._run_handler` does.

    Args:
        call: The handler call.

    Returns:
        Callable[[], object]: The wrapped call.

    """

    def run() -> object:
        with with_transaction("link_canon_library"):
            return call()

    return run


attempt(
    "absent name",
    transacted(lambda: server.link_canon_library(str(canon), collections=["CanonHero", "NoSuchHero"])),
)
print("  libraries after:", [lib.name for lib in bpy.data.libraries])
real_override = linking._override_hierarchy


def failing_override(collection: object, *_rest: object) -> dict[str, object]:
    """
    Fail after the link succeeded, the way any later step could.

    Args:
        collection: Ignored.
        *_rest: Ignored.

    Raises:
        RuntimeError: Always.

    """
    raise RuntimeError(f"injected failure after linking {collection!r}")


linking._override_hierarchy = failing_override
attempt(
    "failure after the link",
    transacted(lambda: server.link_canon_library(str(canon), collections=["CanonHero"], as_override=True)),
)
linking._override_hierarchy = real_override
print(
    "  libraries after:",
    [lib.name for lib in bpy.data.libraries],
    " collections:",
    [c.name for c in bpy.data.collections],
)

print("\n=== E. unlink one of two libraries, with purge_orphans ===")
fresh_shot(work / "shot_e.blend")
scratch = bpy.data.materials.new("UserScratchMaterial")
first = server.link_canon_library(str(canon), collections=["CanonHero"])
second = server.link_canon_library(str(props), collections=["Props"])
override_report = server.create_override(first["collections"][0]["session_uid"])
# A local material used only by the override object, through an ID property: the unlink frees the
# override object (measured), which leaves this material with no users - the one thing purge takes.
paint = bpy.data.materials.new("OnlyUsedByTheOverride")
override_body = next(o for o in bpy.data.objects if o.session_uid == override_report["objects"][0]["session_uid"])
override_body["paint"] = paint
print("  before: OnlyUsedByTheOverride users =", paint.users, " UserScratchMaterial users =", scratch.users)
second_uids = {
    second["library"]["session_uid"],
    *(d["session_uid"] for d in server.list_libraries()["libraries"][1]["datablocks"]),
}
attempt("unlink without confirm", lambda: server.unlink_libraries([first["library"]["session_uid"]]))
report = server.unlink_libraries([first["library"]["session_uid"]], confirm=True, purge_orphans=True)
print("  report:", report)
alive = {datablock.session_uid for name, datablock in linking._iter_ids()}
print("  unnamed library and its datablocks all alive:", second_uids <= alive)
print("  user's zero-user material alive:", "UserScratchMaterial" in bpy.data.materials, scratch.name)
print("  orphaned material purged:", "OnlyUsedByTheOverride" not in bpy.data.materials)
print("  libraries now:", [lib.name for lib in bpy.data.libraries])

print("\n=== F. use_scripts_auto_execute on ===")
bpy.context.preferences.filepaths.use_scripts_auto_execute = True
attempt("link_canon_library", lambda: server.link_canon_library(str(canon), collections=["CanonHero"]))
attempt("reload_library", lambda: server.reload_library(second["library"]["session_uid"]))
bpy.context.preferences.filepaths.use_scripts_auto_execute = False


def root_children() -> list[tuple[str, str, int]]:
    """
    Read the first scene's root children with their link state and users.

    Returns:
        list: `(name, 'L' linked | 'O' override | 'local', users)`.

    """
    return [
        (c.name, "L" if c.library else "O" if c.override_library else "local", c.users)
        for c in bpy.data.scenes[0].collection.children
    ]


print("\n=== G. failed multi-collection as_override link over a pre-existing placement ===")
two = work / "canon_two.blend"
bpy.ops.wm.read_homefile(use_empty=True, use_factory_startup=True, load_ui=False)
for coll_name, body_name in (("CanonHero", "HeroBody"), ("CanonProp", "PropBody")):
    coll = bpy.data.collections.new(coll_name)
    bpy.data.scenes[0].collection.children.link(coll)
    coll.objects.link(bpy.data.objects.new(body_name, bpy.data.meshes.new(f"{body_name}Mesh")))
bpy.ops.wm.save_as_mainfile(filepath=str(two), compress=False, relative_remap=False)
shot = work / "shot_g.blend"
fresh_shot(shot)
placed = server.link_canon_library(str(two), collections=["CanonHero", "CanonProp"])
server.create_override(placed["collections"][1]["session_uid"])
attempt("save_shot in place", lambda: server.save_shot(confirm_overwrite=True, relative_remap=False))
print("  root before:", root_children())
attempt(
    "as_override link of both, transacted",
    transacted(lambda: server.link_canon_library(str(two), collections=["CanonHero", "CanonProp"], as_override=True)),
)
print("  root after :", root_children())
attempt("save_shot in place", lambda: server.save_shot(confirm_overwrite=True, relative_remap=False))
attempt("open_shot", lambda: server.open_shot(str(shot)))
print("  root REOPENED:", root_children())

print("\n=== H. relocate to a file lacking the linked datablocks ===")
fresh_shot(work / "shot_h.blend")
library_uid = server.link_canon_library(str(canon), collections=["CanonHero"])["library"]["session_uid"]
relocated = attempt("relocate_library to props.blend", lambda: server.relocate_library(library_uid, str(props)))
if relocated:
    print("  library is_missing:", relocated["library"]["is_missing"])  # type: ignore[index]
    print("  datablocks:", relocated["datablocks"])  # type: ignore[index]
    print("  warnings:", relocated.get("warnings"))  # type: ignore[union-attr]

print("\n=== I. relocate of an indirect library ===")
mid = work / "mid.blend"
bpy.ops.wm.read_homefile(use_empty=True, use_factory_startup=True, load_ui=False)
mid_coll = bpy.data.collections.new("Mid")
bpy.data.scenes[0].collection.children.link(mid_coll)
with bpy.data.libraries.load(str(props), link=True) as (_f, to):  # pyright: ignore[reportGeneralTypeIssues]  # libraries.load() is a context manager at runtime
    to.collections = ["Props"]
mid_coll.children.link(to.collections[0])  # type: ignore[arg-type]
bpy.ops.wm.save_as_mainfile(filepath=str(mid), compress=False, relative_remap=False)
fresh_shot(work / "shot_i.blend")
server.link_canon_library(str(mid), collections=["Mid"])
base = next(lib for lib in bpy.data.libraries if lib.name == "props.blend")
print("  props.blend reached through mid.blend; indirect flags:", [i.is_library_indirect for i in base.users_id])
attempt("relocate_library of the indirect library", lambda: server.relocate_library(base.session_uid, str(canon)))

print("\n=== J. a hostile Library.name in a failed reload ===")
moved = work / "moved_canon.blend"
shutil.copyfile(canon, moved)
fresh_shot(work / "shot_j.blend")
library_uid = server.link_canon_library(str(moved), collections=["CanonHero"])["library"]["session_uid"]
library = next(lib for lib in bpy.data.libraries if lib.session_uid == library_uid)
library.name = "/Users/victim/shots/canon.blend"
print("  Library.name accepted:", repr(library.name))
os.remove(moved)
try:
    library.reload()
except RuntimeError as exc:
    print(f"  RAW: {str(exc)!r}")
attempt("reload_library", lambda: server.reload_library(library_uid))


def census_k() -> tuple[list[tuple[str, str]], int, list[tuple[str, str]]]:
    """
    Read what a nested override request left behind.

    Returns:
        tuple: collections as `(name, L|O|local)`, how many `ChildBody` overrides exist, and the root.

    """
    collections = [(c.name, "L" if c.library else "O" if c.override_library else "local") for c in bpy.data.collections]
    child_overrides = sum(1 for o in bpy.data.objects if o.name.startswith("ChildBody") and o.override_library)
    return collections, child_overrides, [(name, kind) for name, kind, _users in root_children()]


print("\n=== K. nested request: Parent contains Child ===")
nest = work / "nest.blend"
bpy.ops.wm.read_homefile(use_empty=True, use_factory_startup=True, load_ui=False)
parent_coll = bpy.data.collections.new("Parent")
child_coll = bpy.data.collections.new("Child")
bpy.data.scenes[0].collection.children.link(parent_coll)
parent_coll.children.link(child_coll)
child_coll.objects.link(bpy.data.objects.new("ChildBody", bpy.data.meshes.new("ChildMesh")))
bpy.ops.wm.save_as_mainfile(filepath=str(nest), compress=False, relative_remap=False)
for order in (["Parent", "Child"], ["Child", "Parent"]):
    for placed_first in (False, True):
        fresh_shot(work / f"shot_k_{'_'.join(order)}_{placed_first}.blend")
        if placed_first:
            server.link_canon_library(str(nest), collections=list(order))
        before = root_children()
        result = attempt(
            f"{order} as_override, placed first={placed_first}",
            transacted(lambda order=order: server.link_canon_library(str(nest), collections=order, as_override=True)),
        )
        if result:
            print(
                "    overrides:",
                [(r["override"]["name"], r["override"]["session_uid"]) for r in result["overrides"]],  # type: ignore[index]
            )  # type: ignore[index]
        collections, child_overrides, root = census_k()
        print(f"    collections: {collections}  ChildBody overrides: {child_overrides}")
        print(f"    root before: {[(n, k) for n, k, _u in before]}  after: {root}")

print("\n=== L. a newline in a hostile Library.name ===")
moved = work / "moved_canon_l.blend"
shutil.copyfile(canon, moved)
fresh_shot(work / "shot_l.blend")
library_uid = server.link_canon_library(str(moved), collections=["CanonHero"])["library"]["session_uid"]
library = next(lib for lib in bpy.data.libraries if lib.session_uid == library_uid)
library.name = "/Users/victim/a\n/b.blend"
print("  Library.name accepted:", repr(library.name))
os.remove(moved)
try:
    library.reload()
except RuntimeError as exc:
    print(f"  RAW: {str(exc)!r}")
attempt("reload_library", lambda: server.reload_library(library_uid))


print("\n=== M. Child overridden first, then Parent ===")
for via_link in (False, True):
    shot = work / f"shot_m_{via_link}.blend"
    fresh_shot(shot)
    child = server.link_canon_library(str(nest), collections=["Child"])["collections"][0]["session_uid"]
    server.create_override(child)
    if via_link:
        label = "link_canon_library(['Parent'], as_override=True)"
        call = transacted(lambda: server.link_canon_library(str(nest), collections=["Parent"], as_override=True))
    else:
        parent = server.link_canon_library(str(nest), collections=["Parent"])["collections"][0]["session_uid"]
        label = "create_override(Parent)"
        call = transacted(lambda parent=parent: server.create_override(parent))
    attempt(label, call)
    attempt("save_shot in place", lambda: server.save_shot(confirm_overwrite=True, relative_remap=False))
    attempt("open_shot", lambda shot=shot: server.open_shot(str(shot)))
    collections, child_overrides, root = census_k()
    print(f"    REOPENED collections: {collections}  ChildBody overrides: {child_overrides}  root: {root}")
