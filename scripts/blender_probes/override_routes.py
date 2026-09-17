"""Print override_create's return value and what each of the three override routes produces."""

import os
import tempfile

import bpy

print("=== BLENDER ===", bpy.app.version_string)
work = tempfile.mkdtemp(prefix="api522e_")
canon = os.path.join(work, "canon.blend")

bpy.ops.wm.read_factory_settings(use_empty=True)
coll = bpy.data.collections.new("CanonHero")
bpy.context.scene.collection.children.link(coll)
mesh = bpy.data.meshes.new("HeroMesh")
mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
coll.objects.link(bpy.data.objects.new("HeroBody", mesh))
bpy.ops.wm.save_as_mainfile(filepath=canon, compress=False, relative_remap=False)


def fresh_link(**load_kwargs: bool) -> "bpy.types.Collection":
    """
    Empty the scene and link CanonHero back in.

    Args:
        **load_kwargs: Extra flags for `bpy.data.libraries.load`, e.g.
            `create_liboverrides=True` for route A.

    Returns:
        The linked collection, already hooked into the scene.

    """
    bpy.ops.wm.read_factory_settings(use_empty=True)
    with bpy.data.libraries.load(canon, link=True, **load_kwargs) as (_src, dst):  # pyright: ignore[reportGeneralTypeIssues]  # blender-python-stubs types libraries.load() as None; it is a context manager
        dst.collections = ["CanonHero"]
    linked = next(c for c in bpy.data.collections if c.library)
    bpy.context.scene.collection.children.link(linked)
    return linked


print("\n=== override_create return value: local (non-overridable) vs linked ===")
local = bpy.data.collections.new("PurelyLocal")
print("  local collection  .override_create() ->", repr(local.override_create()))

linked = fresh_link()
obj = next(o for o in bpy.data.objects if o.library)
returned = obj.override_create()
print("  LINKED object     .override_create() ->", repr(returned))
if returned is not None:
    print("    .override_library.reference ->", repr(returned.override_library.reference))
print("  >>> the 'returns None' claim is", "FALSE" if returned is not None else "TRUE", "on a linked object")

print("\n=== Route A: libraries.load(create_liboverrides=True) ===")
linked = fresh_link(create_liboverrides=True)
objs = list(bpy.data.objects)
print("  objects:", [(o.name, o.is_editable, bool(o.library)) for o in objs])
print("  collections named CanonHero:", sum(1 for c in bpy.data.collections if c.name == "CanonHero"))

print("\n=== Route B: override_hierarchy_create(scene, view_layer) ===")
linked = fresh_link()
linked.override_hierarchy_create(bpy.context.scene, bpy.context.view_layer)
objs = list(bpy.data.objects)
print(
    "  objects:",
    [(o.name, o.is_editable, bool(o.override_library and o.override_library.is_system_override)) for o in objs],
)

print("\n=== Route C: override_hierarchy_create(..., do_fully_editable=True)  [the route the linking handlers use] ===")
linked = fresh_link()
linked.override_hierarchy_create(bpy.context.scene, bpy.context.view_layer, do_fully_editable=True)
print("  objects:")
for o in bpy.data.objects:
    sysov = o.override_library.is_system_override if o.override_library else None
    print(
        f"    name={o.name!r} uid={o.session_uid} editable={o.is_editable} "
        f"linked={bool(o.library)} system_override={sysov}"
    )
print("  collections:")
for c in bpy.data.collections:
    print(f"    name={c.name!r} uid={c.session_uid} editable={c.is_editable} linked={bool(c.library)}")
names = [o.name for o in bpy.data.objects]
print(f"  >>> duplicate OBJECT names: {len(names) != len(set(names))}  ({names})")
cnames = [c.name for c in bpy.data.collections]
print(f"  >>> duplicate COLLECTION names: {len(cnames) != len(set(cnames))}  ({cnames})")
