"""Distinguish 'override_create failed' from 'succeeded but returns None' on 5.2.2."""

import os
import tempfile

import bpy

print("=== BLENDER ===", bpy.app.version_string)
work = tempfile.mkdtemp(prefix="api522f_")
canon = os.path.join(work, "canon.blend")

bpy.ops.wm.read_factory_settings(use_empty=True)
coll = bpy.data.collections.new("CanonHero")
bpy.context.scene.collection.children.link(coll)
mesh = bpy.data.meshes.new("HeroMesh")
mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
coll.objects.link(bpy.data.objects.new("HeroBody", mesh))
bpy.ops.wm.save_as_mainfile(filepath=canon, compress=False, relative_remap=False)

bpy.ops.wm.read_factory_settings(use_empty=True)
with bpy.data.libraries.load(canon, link=True) as (src, dst):  # pyright: ignore[reportGeneralTypeIssues]  # blender-python-stubs types libraries.load() as None; it is a context manager
    dst.collections = ["CanonHero"]
linked_coll = next(c for c in bpy.data.collections if c.library)
bpy.context.scene.collection.children.link(linked_coll)
obj = next(o for o in bpy.data.objects if o.library)

print("\n=== state of the linked object before override_create ===")
print("  name              :", obj.name)
print("  library           :", obj.library)
print("  is_editable       :", obj.is_editable)
print("  is_library_indirect:", obj.is_library_indirect)
print("  override_library  :", obj.override_library)
print("  in view layer     :", obj.name in bpy.context.view_layer.objects)

n_before = len(bpy.data.objects)
uids_before = {o.session_uid for o in bpy.data.objects}
returned = obj.override_create()
n_after = len(bpy.data.objects)
uids_after = {o.session_uid for o in bpy.data.objects}

print("\n=== did it actually DO anything? ===")
print(f"  returned                 : {returned!r}")
print(f"  bpy.data.objects count   : {n_before} -> {n_after}")
print(f"  new session_uids         : {uids_after - uids_before or 'NONE'}")
for o in bpy.data.objects:
    print(
        f"    name={o.name!r} uid={o.session_uid} linked={bool(o.library)} "
        f"override={bool(o.override_library)} editable={o.is_editable}"
    )

print("\n=== same call with remap_local_usages=True ===")
bpy.ops.wm.read_factory_settings(use_empty=True)
with bpy.data.libraries.load(canon, link=True) as (src, dst):  # pyright: ignore[reportGeneralTypeIssues]  # blender-python-stubs types libraries.load() as None; it is a context manager
    dst.collections = ["CanonHero"]
lc = next(c for c in bpy.data.collections if c.library)
bpy.context.scene.collection.children.link(lc)
o2 = next(o for o in bpy.data.objects if o.library)
n0 = len(bpy.data.objects)
r2 = o2.override_create(remap_local_usages=True)
print(f"  returned={r2!r}  objects {n0} -> {len(bpy.data.objects)}")

print("\n=== and on the linked COLLECTION rather than the object ===")
bpy.ops.wm.read_factory_settings(use_empty=True)
with bpy.data.libraries.load(canon, link=True) as (src, dst):  # pyright: ignore[reportGeneralTypeIssues]  # blender-python-stubs types libraries.load() as None; it is a context manager
    dst.collections = ["CanonHero"]
lc = next(c for c in bpy.data.collections if c.library)
bpy.context.scene.collection.children.link(lc)
n0 = len(bpy.data.collections)
r3 = lc.override_create()
print(f"  returned={r3!r}  collections {n0} -> {len(bpy.data.collections)}")

print("\n=== control: override_hierarchy_create's return value ===")
bpy.ops.wm.read_factory_settings(use_empty=True)
with bpy.data.libraries.load(canon, link=True) as (src, dst):  # pyright: ignore[reportGeneralTypeIssues]  # blender-python-stubs types libraries.load() as None; it is a context manager
    dst.collections = ["CanonHero"]
lc = next(c for c in bpy.data.collections if c.library)
bpy.context.scene.collection.children.link(lc)
r4 = lc.override_hierarchy_create(bpy.context.scene, bpy.context.view_layer, do_fully_editable=True)
print(f"  override_hierarchy_create returned {r4!r}")
