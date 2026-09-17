r"""
Print which file handlers fire, and which session_uids change, for each library operation.

Rollback across a library reload depends on this table. Expected:
`lib.reload()` and a relocate (a `filepath` assignment, then reload) fire
`blend_import_pre` and `blend_import_post` but never `load_post`, and give every
datablock linked from the library a new session_uid. `libraries.load()` fires
the same two handlers. A failed reload, `orphans_purge` and `libraries.remove`
fire nothing, and a failed reload changes no uids. If `load_post` fires on a
reload, the rollback design needs rethinking, not patching.

Builds its own canon `.blend` (one collection, object, mesh and material) in a
temporary directory. From the repository root::

    /opt/homebrew/bin/blender --background --factory-startup \
        --python scripts/blender_probes/library_replace_handlers.py
"""

import os
import shutil
import tempfile

from collections.abc import Callable

import bpy

_EVENTS = ("load_pre", "load_post", "load_post_fail", "blend_import_pre", "blend_import_post")

print("=== BLENDER ===", bpy.app.version_string)
work = tempfile.mkdtemp(prefix="t4_libreplace_")
canon = os.path.join(work, "canon.blend")
moved = os.path.join(work, "canon_moved.blend")

bpy.ops.wm.read_factory_settings(use_empty=True)
hero = bpy.data.collections.new("CanonHero")
bpy.context.scene.collection.children.link(hero)
mesh = bpy.data.meshes.new("HeroMesh")
mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
mesh.materials.append(bpy.data.materials.new("HeroPaint"))
hero.objects.link(bpy.data.objects.new("HeroBody", mesh))
bpy.ops.wm.save_as_mainfile(filepath=canon, compress=False, relative_remap=False)
shutil.copyfile(canon, moved)

# read_factory_settings itself fires load_post; install the probe handlers after it.
bpy.ops.wm.read_factory_settings(use_empty=True)
fired: list[str] = []
argument_shapes: set[tuple[str, ...]] = set()


def _recorder(event: str) -> Callable[..., None]:
    """
    Build a handler that records its event name and the types of its arguments.

    Args:
        event: The handler list this recorder is attached to.

    Returns:
        Callable[..., None]: The handler.

    """

    def record(*args: object) -> None:
        fired.append(event)
        argument_shapes.add((event, *(type(arg).__name__ for arg in args)))

    return record


for event in _EVENTS:
    getattr(bpy.app.handlers, event).append(_recorder(event))


def _id_collection_names() -> list[str]:
    """
    List the `bpy.data` ID collections except `all_ids`, which would count every datablock twice.

    Returns:
        list[str]: Attribute names on `bpy.data`.

    """
    names = []
    for attr in dir(bpy.data):
        if attr == "all_ids" or attr.startswith("_"):
            continue
        if isinstance(getattr(bpy.data, attr, None), bpy.types.bpy_prop_collection):
            names.append(attr)
    return names


def linked_uids(library: bpy.types.Library) -> dict[str, int]:
    """
    Map every datablock linked from `library` to its current session_uid.

    Scans the `bpy.data` ID collections rather than `Library.users_id`, so the
    indirectly linked mesh behind the object is counted too.

    Args:
        library: The library whose linked datablocks to read.

    Returns:
        dict[str, int]: `"<collection>/<name>"` mapped to that datablock's session_uid.

    """
    found = {}
    for attr in _id_collection_names():
        collection = getattr(bpy.data, attr)
        for datablock in collection:
            if isinstance(datablock, bpy.types.ID) and datablock.library == library:
                found[f"{attr}/{datablock.name}"] = datablock.session_uid
    return found


def local_uids() -> set[int]:
    """
    Read the session_uid of every local (non-linked) datablock.

    Returns:
        set[int]: The session_uids.

    """
    uids = set()
    for attr in _id_collection_names():
        collection = getattr(bpy.data, attr)
        uids.update(d.session_uid for d in collection if isinstance(d, bpy.types.ID) and d.library is None)
    return uids


def report(label: str, before: dict[str, int], after: dict[str, int]) -> None:
    """
    Print the handlers fired and the session_uid churn for one operation.

    Args:
        label: The operation measured.
        before: `linked_uids` taken before it.
        after: `linked_uids` taken after it.

    """
    churned = sum(1 for key, uid in before.items() if key in after and after[key] != uid)
    print(f"\n=== {label} ===")
    print("  handlers fired:", fired or "NONE")
    print(f"  linked datablocks before/after: {len(before)}/{len(after)}")
    print(f"  session_uid changed: {churned} of {len(before)}")
    if "load_post" in fired:
        print("  !!! load_post FIRED - STOP: the reload-rollback design's premise is false on this build")


fired.clear()
local_before = local_uids()
with bpy.data.libraries.load(canon, link=True) as (_src, dst):  # pyright: ignore[reportGeneralTypeIssues]  # blender-python-stubs types libraries.load() as None; it is a context manager
    dst.collections = ["CanonHero"]
lib = bpy.data.libraries[0]
bpy.context.scene.collection.children.link(next(c for c in bpy.data.collections if c.library == lib))
linked = linked_uids(lib)
report("libraries.load(link=True)", {}, linked)
print("  new linked datablocks:", sorted(linked))

fired.clear()
with bpy.data.libraries.load(canon, link=False) as (_src, dst):  # pyright: ignore[reportGeneralTypeIssues]  # blender-python-stubs types libraries.load() as None; it is a context manager
    dst.materials = ["HeroPaint"]
report("libraries.load(link=False) (append)", {}, {})
print("  local datablocks gained:", len(local_uids() - local_before))

fired.clear()
before = linked_uids(lib)
lib.reload()
report("lib.reload()", before, linked_uids(lib))

fired.clear()
before = linked_uids(lib)
lib.filepath = moved
lib.reload()
report("relocate: lib.filepath = <copy>; lib.reload()", before, linked_uids(lib))

fired.clear()
before = linked_uids(lib)
lib.filepath = os.path.join(work, "gone.blend")
try:
    lib.reload()
except RuntimeError:
    print("\n  (failed reload raised RuntimeError; message withheld, it embeds the absolute path)")
else:
    print("\n  failed reload did NOT raise")
report("failed lib.reload() (invalid path)", before, linked_uids(lib))
lib.filepath = moved

fired.clear()
purged = bpy.data.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)
print(f"\n=== bpy.data.orphans_purge(...) -> {purged} ===")
print("  handlers fired:", fired or "NONE")

fired.clear()
library_count = len(bpy.data.libraries)
bpy.data.libraries.remove(lib)
print(f"\n=== bpy.data.libraries.remove(lib): libraries {library_count} -> {len(bpy.data.libraries)} ===")
print("  handlers fired:", fired or "NONE")
print("  linked collections left:", [c.name for c in bpy.data.collections if c.library is not None])
print("\nhandler argument shapes (event, *argument types):", sorted(argument_shapes))


def _duplicate(*_args: object) -> None:
    """Stand in for a callback registered twice."""


import_post = bpy.app.handlers.blend_import_post
start = len(import_post)
import_post.append(_duplicate)
import_post.append(_duplicate)
print(f"\nblend_import_post is a {type(import_post).__name__}; len {start} -> {len(import_post)} after appending twice")
import_post.remove(_duplicate)
import_post.remove(_duplicate)
try:
    import_post.remove(_duplicate)
except ValueError:
    print("  remove() of an absent callback raises ValueError")
