r"""
Confirm plan Task 7's `reload` ruling: the data API works, the operators are the awkward route (Step 2b).

Sections:

A. `reload` is in `bpy.types.Library.bl_rna.functions` (while `dir()` on the
   type object, the invalid check, is printed beside it for the record).
B. `lib.filepath = <new>; lib.reload()` end to end, with the datablock listing
   (type, name, session_uid) before and after, against a replacement file whose
   object has a different name, so the swap is visible.
C. `wm.lib_reload` / `wm.lib_relocate` with the argument shapes an earlier plan
   draft used and with `directory` + `filename`, plus a bogus library name:
   each raw outcome, `RuntimeError` or a returned set.
D. The raw `reload()` failure text for a missing file, for an absolute link and
   for a `//`-relative link in a saved shot inside a directory named
   `Smith, John` - which path form Blender embeds decides what
   `sanitize_blender_error`'s `known_paths` must contain.

From the repository root::

    /opt/homebrew/bin/blender --background --factory-startup \
        --python scripts/blender_probes/linking_reload_ruling.py
"""

import os
import shutil
import tempfile

from collections.abc import Callable
from typing import cast

import bpy

print("=== BLENDER ===", bpy.app.version_string)
work = tempfile.mkdtemp(prefix="t7_reload_")
canon = os.path.join(work, "canon.blend")
replacement = os.path.join(work, "replacement.blend")
smith = os.path.join(work, "Smith, John")
os.makedirs(smith)


def build(path: str, body: str) -> None:
    """
    Write a library holding collection `CanonHero` with one object.

    Args:
        path: Where to write it.
        body: The object's name.

    """
    bpy.ops.wm.read_factory_settings(use_empty=True)
    coll = bpy.data.collections.new("CanonHero")
    bpy.data.scenes[0].collection.children.link(coll)
    mesh = bpy.data.meshes.new(f"{body}Mesh")
    mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
    coll.objects.link(bpy.data.objects.new(body, mesh))
    bpy.ops.wm.save_as_mainfile(filepath=path, compress=False, relative_remap=False)


def fresh_link(path: str, **flags: bool) -> bpy.types.Library:
    """
    Reset, link `CanonHero` from a file and instance it.

    Args:
        path: The library file.
        **flags: Extra `libraries.load` flags (`relative`).

    Returns:
        bpy.types.Library: The library the link made.

    """
    with bpy.data.libraries.load(path, link=True, **flags) as (_f, to):  # pyright: ignore[reportGeneralTypeIssues]  # libraries.load() is a context manager at runtime
        to.collections = ["CanonHero"]
    linked = cast("bpy.types.Collection", to.collections[0])
    bpy.data.scenes[0].collection.children.link(linked)
    return cast("bpy.types.Library", linked.library)


def listing(lib: bpy.types.Library) -> list[tuple[str, str, int]]:
    """
    List the datablocks linked from a library.

    Args:
        lib: The library.

    Returns:
        list: `(type, name, session_uid)` per datablock.

    """
    return [(type(i).__name__, i.name, i.session_uid) for i in lib.users_id]


def raw(label: str, call: Callable[[], object]) -> None:
    """
    Print one call's raw outcome.

    Args:
        label: What is printed.
        call: The call.

    """
    try:
        print(f"  {label:<58} -> returned {call()!r}")
    except Exception as exc:
        print(f"  {label:<58} -> {type(exc).__name__}: {str(exc).strip()!r}")


build(canon, "HeroBody")
build(replacement, "ReplacementBody")

print("\n=== A. is reload an RNA function? ===")
print("  'reload' in Library.bl_rna.functions:", "reload" in [f.identifier for f in bpy.types.Library.bl_rna.functions])
print("  'reload' in dir(bpy.types.Library)  :", "reload" in dir(bpy.types.Library), "(the invalid check)")

print("\n=== B. lib.filepath = new; lib.reload() ===")
bpy.ops.wm.read_factory_settings(use_empty=True)
lib = fresh_link(canon)
print("  before:", lib.name, listing(lib))
lib.filepath = replacement
lib.reload()
print("  after :", lib.name, listing(lib), " is_missing", lib.is_missing)

print("\n=== C. the operators ===")
for label, call in (
    ("lib_reload(library=name)", lambda: bpy.ops.wm.lib_reload(library="canon.blend")),
    ("lib_reload(library=name, filepath=...)", lambda: bpy.ops.wm.lib_reload(library="canon.blend", filepath=canon)),
    (
        "lib_reload(library, directory, filename)",
        lambda: bpy.ops.wm.lib_reload(library="canon.blend", directory=work + os.sep, filename="canon.blend"),
    ),
    (
        "lib_reload(bogus library, directory, filename)",
        lambda: bpy.ops.wm.lib_reload(library="nope.blend", directory=work + os.sep, filename="canon.blend"),
    ),
    (
        "lib_relocate(library=name, filepath=...)",
        lambda: bpy.ops.wm.lib_relocate(library="canon.blend", filepath=replacement),
    ),
    (
        "lib_relocate(library, directory, filename)",
        lambda: bpy.ops.wm.lib_relocate(library="canon.blend", directory=work + os.sep, filename="replacement.blend"),
    ),
):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    fresh_link(canon)
    raw(label, call)
    print(f"  {'':<58}    libraries now: {[lib.name for lib in bpy.data.libraries]}")

print("\n=== D. raw reload() failure text for a missing file ===")
bpy.ops.wm.read_factory_settings(use_empty=True)
lib = fresh_link(canon)
lib.filepath = os.path.join(work, "gone.blend")
raw("absolute link to gone.blend", lib.reload)
smith_canon = os.path.join(smith, "canon.blend")
shutil.copyfile(canon, smith_canon)
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.wm.save_as_mainfile(filepath=os.path.join(smith, "shot.blend"))
lib = fresh_link(smith_canon, relative=True)
print("  relative link stored as:", repr(lib.filepath))
os.remove(smith_canon)
raw("relative link, file removed", lib.reload)
print("  bpy.path.abspath(filepath):", repr(bpy.path.abspath(lib.filepath)))
