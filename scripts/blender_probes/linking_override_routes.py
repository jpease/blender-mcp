r"""
Measure the three library-override routes against a fixture built here (plan Task 7 Step 2).

For each route, and for the collection *and* one object inside it, print
`library`, `override_library`, `is_editable` and `is_system_override`. The
plan's expected table: Route A leaves the objects locked; Route B makes them
editable but system overrides; Route C (`do_fully_editable=True`) makes them
editable user overrides. **No route here reads `bpy.context`**: the scene and
view layer come from `bpy.data.scenes[0]` / `scene.view_layers[0]`, which is
what "no operator context needed" means (`rg -n 'bpy.context' <this file>` finds
only this sentence). Each route is also run with the linked collection first
instanced in the scene and without, because `create_override` receives a linked
collection in either state.

A final section measures the Route C edge cases `create_override` is built on:
where the override goes when the linked collection is instanced inside a local
sub-collection, what a second call on the same linked collection does, and what
the call returns on an override collection and on a local one.

From the repository root::

    /opt/homebrew/bin/blender --background --factory-startup \
        --python scripts/blender_probes/linking_override_routes.py
"""

import os
import tempfile

import bpy

print("=== BLENDER ===", bpy.app.version_string)
work = tempfile.mkdtemp(prefix="t7_routes_")
canon = os.path.join(work, "canon.blend")

bpy.ops.wm.read_factory_settings(use_empty=True)
hero = bpy.data.collections.new("CanonHero")
bpy.data.scenes[0].collection.children.link(hero)
mesh = bpy.data.meshes.new("HeroMesh")
mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
hero.objects.link(bpy.data.objects.new("HeroBody", mesh))
bpy.ops.wm.save_as_mainfile(filepath=canon, compress=False, relative_remap=False)


def row(kind: str, datablock: bpy.types.ID) -> str:
    """
    Format the four measured attributes of one datablock.

    Args:
        kind: `COLLECTION` or `OBJECT`.
        datablock: The datablock.

    Returns:
        str: One line.

    """
    override = datablock.override_library
    return (
        f"    {kind:<10} {datablock.name!r:<12} uid={datablock.session_uid:<4} "
        f"library={datablock.library is not None!s:<5} "
        f"override_library={override is not None!s:<5} is_editable={datablock.is_editable!s:<5} "
        f"is_system_override={override.is_system_override if override else None}"
    )


for route in ("A", "B", "C"):
    for instanced_first in (True, False):
        print(f"\n=== Route {route}, linked collection instanced in the scene first: {instanced_first} ===")
        bpy.ops.wm.read_factory_settings(use_empty=True)
        scene = bpy.data.scenes[0]
        view_layer = scene.view_layers[0]
        flags = {"create_liboverrides": True, "reuse_liboverrides": True} if route == "A" else {}
        with bpy.data.libraries.load(canon, link=True, **flags) as (_f, to):  # pyright: ignore[reportGeneralTypeIssues]  # libraries.load() is a context manager at runtime
            to.collections = ["CanonHero"]
        linked = next(c for c in bpy.data.collections if c.library is not None)
        if instanced_first:
            scene.collection.children.link(linked)
        if route == "B":
            returned = linked.override_hierarchy_create(scene, view_layer)
            print("    returned:", repr(returned))
        elif route == "C":
            returned = linked.override_hierarchy_create(scene, view_layer, do_fully_editable=True)
            print("    returned:", repr(returned))
        for coll in bpy.data.collections:
            print(row("COLLECTION", coll))
        for obj in bpy.data.objects:
            print(row("OBJECT", obj))
        print(
            "    scene children:",
            [(c.name, c.library is not None, c.override_library is not None) for c in scene.collection.children],
        )
        print("    scene objects :", [(o.name, o.type) for o in scene.collection.objects])

print("\n=== Route C edge cases ===")
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.data.scenes[0]
with bpy.data.libraries.load(canon, link=True) as (_f, to):  # pyright: ignore[reportGeneralTypeIssues]  # libraries.load() is a context manager at runtime
    to.collections = ["CanonHero"]
linked = next(c for c in bpy.data.collections if c.library is not None)
chars = bpy.data.collections.new("Chars")
scene.collection.children.link(chars)
chars.children.link(linked)
first = linked.override_hierarchy_create(scene, scene.view_layers[0], do_fully_editable=True)
print("    instanced in 'Chars', first call returned:", repr(first))
print(
    "    'Chars' children:", [(c.name, c.library is not None, c.override_library is not None) for c in chars.children]
)
print("    scene root children:", [c.name for c in scene.collection.children])
second = linked.override_hierarchy_create(scene, scene.view_layers[0], do_fully_editable=True)
print("    second call on the same linked collection returned:", repr(second))
print(
    "    on the override collection returned:",
    repr(first.override_hierarchy_create(scene, scene.view_layers[0], do_fully_editable=True)),
)
print(
    "    on a local collection returned:",
    repr(chars.override_hierarchy_create(scene, scene.view_layers[0], do_fully_editable=True)),
)
