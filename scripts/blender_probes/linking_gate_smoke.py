r"""
Run the link, override, save and reopen gate smoke test on Route A and Route C.

Links a canon collection, overrides it, saves the shot with
`relative_remap=False`, reopens it and prints the libraries and overrides it
holds. Route A, `libraries.load(create_liboverrides=True)`, is what the original
smoke test used and leaves objects locked. Route C,
`override_hierarchy_create(..., do_fully_editable=True)`, is what the linking
handlers use. Builds its fixture in a temporary directory, and reaches the
scene without `bpy.context`.

From the repository root::

    /opt/homebrew/bin/blender --background --factory-startup \
        --python scripts/blender_probes/linking_gate_smoke.py
"""

import os
import tempfile

import bpy

print("=== BLENDER ===", bpy.app.version_string)
work = tempfile.mkdtemp(prefix="t7_gate_")
canon = os.path.join(work, "canon_hero.blend")

bpy.ops.wm.read_factory_settings(use_empty=True)
hero = bpy.data.collections.new("CanonHero")
bpy.data.scenes[0].collection.children.link(hero)
mesh = bpy.data.meshes.new("HeroMesh")
mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
hero.objects.link(bpy.data.objects.new("HeroBody", mesh))
bpy.ops.wm.save_as_mainfile(filepath=canon, compress=False, relative_remap=False)


def describe(label: str) -> None:
    """
    Print the libraries, collections and objects the open database holds.

    Args:
        label: A heading.

    """
    print(f"  {label} libraries =", [(lib.name, lib.filepath == canon, lib.is_missing) for lib in bpy.data.libraries])
    for coll in bpy.data.collections:
        override = coll.override_library
        print(
            f"  {label} COLLECTION {coll.name!r} uid={coll.session_uid} library={coll.library is not None} "
            f"override={override is not None} system={override.is_system_override if override else None}"
        )
    for obj in bpy.data.objects:
        override = obj.override_library
        print(
            f"  {label} OBJECT {obj.name!r} uid={obj.session_uid} library={obj.library is not None} "
            f"override={override is not None} editable={obj.is_editable} "
            f"system={override.is_system_override if override else None}"
        )


for route in ("A", "C"):
    print(f"\n=== Route {route} ===")
    bpy.ops.wm.read_factory_settings(use_empty=True)
    shot = os.path.join(work, f"shot_{route}.blend")
    scene = bpy.data.scenes[0]
    if route == "A":
        with bpy.data.libraries.load(canon, link=True, create_liboverrides=True, reuse_liboverrides=True) as (_f, to):  # pyright: ignore[reportGeneralTypeIssues]  # libraries.load() is a context manager at runtime
            to.collections = ["CanonHero"]
        override_root = next(c for c in bpy.data.collections if c.override_library)
        scene.collection.children.link(override_root)
    else:
        with bpy.data.libraries.load(canon, link=True) as (_f, to):  # pyright: ignore[reportGeneralTypeIssues]  # libraries.load() is a context manager at runtime
            to.collections = ["CanonHero"]
        linked = to.collections[0]
        returned = linked.override_hierarchy_create(scene, scene.view_layers[0], do_fully_editable=True)
        print("  override_hierarchy_create returned", repr(returned), "uid", getattr(returned, "session_uid", None))
        override_root = returned
    print("  hierarchy_root =", override_root.override_library.hierarchy_root)
    describe("after override:")
    bpy.ops.wm.save_as_mainfile(filepath=shot, compress=False, relative_remap=False)
    bpy.ops.wm.open_mainfile(filepath=shot)
    describe("REOPENED:")
