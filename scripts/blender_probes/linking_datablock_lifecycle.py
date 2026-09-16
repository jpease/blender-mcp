r"""
Measure what linking, unlinking and purging do to datablocks, so Task 7's handlers are built on facts.

Plan Task 7's handlers make choices this probe decides. Sections:

A. **Persistence.** A linked collection nobody uses is not written: link without
   instancing, save, reopen, and print whether the library survives.
B. **A failed link.** Which requested-name failures still create a `Library`:
   a name absent from the file, and a raise inside the `libraries.load` block.
   Also the raw exception a truncated `.blend` (valid magic) raises.
C. **`relative=True` in a never-saved session** - what `Library.filepath` holds.
D. **Unlinking.** `libraries.remove` on a *direct* library whose file links an
   *indirect* one: does the indirect library survive? And after a Route C
   override: what the removal leaves (override ids, orphans), and what
   `orphans_purge` removes - including an unrelated zero-user local datablock
   the user made, which a scoped unlink must never touch.
E. **Relocate failure.** Assign a missing `filepath` and `reload()`: the raised
   exception, what the library's contents look like after, and whether
   restoring the old `filepath` leaves a working library.
F. **Relocate success.** `Library.name` before and after a data-API relocate
   to a differently named file.
G. **Which session_uids a transacted command churns.** A second link from an
   already-linked library, and `override_hierarchy_create`: do the datablocks
   that existed before keep their uids? If they did not, a rollback inside
   `mutation_transaction` would read them as created and remove them.
H. **`bpy.data.batch_remove`** exists and removes exactly the ids it is given.

From the repository root::

    /opt/homebrew/bin/blender --background --factory-startup \
        --python scripts/blender_probes/linking_datablock_lifecycle.py
"""

import os
import shutil
import tempfile

from typing import cast

import bpy

print("=== BLENDER ===", bpy.app.version_string)
work = tempfile.mkdtemp(prefix="t7_lifecycle_")
canon = os.path.join(work, "canon.blend")
props = os.path.join(work, "props.blend")
renamed = os.path.join(work, "canon_v2.blend")
truncated = os.path.join(work, "truncated.blend")
shot = os.path.join(work, "shot.blend")


def build(path: str, collection: str, body: str, link_from: str | None = None) -> None:
    """
    Write a library `.blend` holding one collection with one object, optionally linking another file.

    Args:
        path: Where to write it.
        collection: The collection name.
        body: The object name.
        link_from: A `.blend` whose `Props` collection this file links (making it indirect for a user of this one).

    """
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.data.scenes[0]
    coll = bpy.data.collections.new(collection)
    scene.collection.children.link(coll)
    mesh = bpy.data.meshes.new(f"{body}Mesh")
    mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
    coll.objects.link(bpy.data.objects.new(body, mesh))
    if link_from:
        with bpy.data.libraries.load(link_from, link=True) as (_f, to):  # pyright: ignore[reportGeneralTypeIssues]  # libraries.load() is a context manager at runtime
            to.collections = ["Props"]
        coll.children.link(cast("bpy.types.Collection", to.collections[0]))
    bpy.ops.wm.save_as_mainfile(filepath=path, compress=False, relative_remap=False)


def link(path: str, name: str) -> bpy.types.Collection:
    """
    Link one collection by name from a library file.

    Args:
        path: The library.
        name: The collection name inside it.

    Returns:
        The linked collection; at runtime None when the file had no such collection.

    """
    with bpy.data.libraries.load(path, link=True) as (_f, to):  # pyright: ignore[reportGeneralTypeIssues]  # libraries.load() is a context manager at runtime
        to.collections = [name]
    return cast("bpy.types.Collection", to.collections[0])


def libraries() -> list[tuple[str, int, bool]]:
    """
    Summarize the open database's libraries.

    Returns:
        list: `(name, session_uid, is_missing)` per library.

    """
    return [(lib.name, lib.session_uid, lib.is_missing) for lib in bpy.data.libraries]


build(props, "Props", "Crate")
build(canon, "CanonHero", "HeroBody", link_from=props)
shutil.copyfile(canon, renamed)
with open(canon, "rb") as source, open(truncated, "wb") as target:
    target.write(source.read(64))

print("\n=== A. an uninstanced linked collection across save/reopen ===")
bpy.ops.wm.read_factory_settings(use_empty=True)
link(canon, "CanonHero")
print("  after link:", libraries())
bpy.ops.wm.save_as_mainfile(filepath=shot, compress=False, relative_remap=False)
bpy.ops.wm.open_mainfile(filepath=shot)
print("  REOPENED  :", libraries())

print("\n=== B. failed links ===")
bpy.ops.wm.read_factory_settings(use_empty=True)
print("  absent name returned:", repr(link(canon, "NoSuchCollection")), " libraries:", libraries())
bpy.ops.wm.read_factory_settings(use_empty=True)
try:
    with bpy.data.libraries.load(canon, link=True) as (_f, to):  # pyright: ignore[reportGeneralTypeIssues]  # libraries.load() is a context manager at runtime
        raise ValueError("refused inside the block")
except ValueError as exc:
    print("  raise inside the block ->", exc, " libraries:", libraries())
bpy.ops.wm.read_factory_settings(use_empty=True)
try:
    link(truncated, "CanonHero")
    print("  truncated .blend linked?! libraries:", libraries())
except Exception as exc:
    print(f"  truncated .blend RAW {type(exc).__name__} -> {str(exc)!r}  libraries: {libraries()}")

print("\n=== C. relative=True with no saved file ===")
bpy.ops.wm.read_factory_settings(use_empty=True)
with bpy.data.libraries.load(canon, link=True, relative=True) as (_f, to):  # pyright: ignore[reportGeneralTypeIssues]  # libraries.load() is a context manager at runtime
    to.collections = ["CanonHero"]
print(
    "  filepath is absolute:",
    os.path.isabs(bpy.data.libraries[0].filepath),
    " startswith //:",
    bpy.data.libraries[0].filepath.startswith("//"),
)

print("\n=== D1. remove the DIRECT library; the indirect one is not named ===")
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.data.scenes[0]
scene.collection.children.link(link(canon, "CanonHero"))
print(
    "  before:",
    libraries(),
    " indirect flags:",
    [(lib.name, [i.is_library_indirect for i in lib.users_id]) for lib in bpy.data.libraries],
)
bpy.data.libraries.remove(next(lib for lib in bpy.data.libraries if lib.name == "canon.blend"))
print("  after removing canon.blend:", libraries())

print("\n=== D2. remove the library after a Route C override, then purge ===")
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.data.scenes[0]
user_orphan = bpy.data.materials.new("UserScratchMaterial")
print("  user's zero-user material users =", user_orphan.users)
override = link(canon, "CanonHero").override_hierarchy_create(scene, scene.view_layers[0], do_fully_editable=True)
override_uid = override.session_uid
print(
    "  before remove:",
    libraries(),
    " objects:",
    [(o.name, o.library is not None, o.override_library is not None) for o in bpy.data.objects],
)
bpy.data.libraries.remove(next(lib for lib in bpy.data.libraries if lib.name == "canon.blend"))
print("  after remove :", libraries())
print(
    "  objects      :",
    [(o.name, o.session_uid, o.library is not None, o.override_library is not None, o.users) for o in bpy.data.objects],
)
print(
    "  collections  :", [(c.name, c.session_uid, c.override_library is not None, c.users) for c in bpy.data.collections]
)
print("  meshes       :", [(m.name, m.library is not None, m.users) for m in bpy.data.meshes])
print(
    "  override collection still in the scene:", any(c.session_uid == override_uid for c in scene.collection.children)
)
print(
    "  orphans_purge(do_local_ids=False, do_linked_ids=True) ->",
    bpy.data.orphans_purge(do_local_ids=False, do_linked_ids=True, do_recursive=False),
)
print("  user's material survives linked-only purge:", "UserScratchMaterial" in bpy.data.materials)
print(
    "  orphans_purge(do_local_ids=True, do_linked_ids=True) ->",
    bpy.data.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=False),
)
print("  user's material survives local purge:", "UserScratchMaterial" in bpy.data.materials)

print("\n=== E. relocate to a missing file, then restore ===")
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.data.scenes[0].collection.children.link(link(canon, "CanonHero"))
lib = next(lib for lib in bpy.data.libraries if lib.name == "canon.blend")
old = lib.filepath
lib.filepath = os.path.join(work, "gone.blend")
try:
    lib.reload()
except Exception as exc:
    print(f"  RAW {type(exc).__name__} -> {str(exc)!r}")
print(
    "  after failed reload: name",
    lib.name,
    "is_missing",
    lib.is_missing,
    "users_id",
    [(i.name, i.session_uid) for i in lib.users_id],
)
lib.filepath = old
print("  restored: is_missing", lib.is_missing, "users_id", len(lib.users_id))
lib.reload()
print("  reload after restore: is_missing", lib.is_missing, "users_id", len(lib.users_id))

print("\n=== F. relocate to a differently named file ===")
print("  name before:", lib.name, " uid", lib.session_uid)
lib.filepath = renamed
lib.reload()
print(
    "  name after :",
    lib.name,
    " uid",
    lib.session_uid,
    " filepath is renamed:",
    lib.filepath == renamed,
    " users_id",
    len(lib.users_id),
)

print("\n=== G. uid churn inside the two transacted commands ===")
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.data.scenes[0]
first = link(canon, "CanonHero")
scene.collection.children.link(first)
before = {(type(i).__name__, i.name): i.session_uid for lib in bpy.data.libraries for i in lib.users_id}
with bpy.data.libraries.load(canon, link=True) as (_f, to):  # pyright: ignore[reportGeneralTypeIssues]  # libraries.load() is a context manager at runtime
    to.objects = ["HeroBody"]
after = {(type(i).__name__, i.name): i.session_uid for lib in bpy.data.libraries for i in lib.users_id}
print(
    "  second link from the same file: libraries",
    libraries(),
    " churned:",
    sorted(k for k in before if after.get(k) != before[k]),
)
first.override_hierarchy_create(scene, scene.view_layers[0], do_fully_editable=True)
after_override = {(type(i).__name__, i.name): i.session_uid for lib in bpy.data.libraries for i in lib.users_id}
print("  override_hierarchy_create churned:", sorted(k for k in after if after_override.get(k) != after[k]))

print("\n=== H. batch_remove ===")
keep = bpy.data.materials.new("Keep")
drop = bpy.data.materials.new("Drop")
print("  'batch_remove' in bpy.data:", "batch_remove" in dir(bpy.data))
bpy.data.batch_remove(ids=[drop])
print("  materials after batch_remove([Drop]):", [m.name for m in bpy.data.materials], " keep valid:", keep.name)
