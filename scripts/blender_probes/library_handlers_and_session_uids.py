"""Re-verify the library handler-firing table, session_uid churn, users_id and override routes on 5.2.2."""

import os
import tempfile

import bpy

print("=== BLENDER ===", bpy.app.version_string)
work = tempfile.mkdtemp(prefix="api522d_")
canon = os.path.join(work, "canon.blend")

# --- build the canon library: a collection with one object ---
bpy.ops.wm.read_factory_settings(use_empty=True)
coll = bpy.data.collections.new("CanonHero")
bpy.context.scene.collection.children.link(coll)
mesh = bpy.data.meshes.new("HeroMesh")
mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
obj = bpy.data.objects.new("HeroBody", mesh)
coll.objects.link(obj)
bpy.ops.wm.save_as_mainfile(filepath=canon, compress=False, relative_remap=False)
print("built canon library at", canon)

# --- fresh scene, install handlers, link the library ---
bpy.ops.wm.read_factory_settings(use_empty=True)
fired = []
for name in ("load_pre", "load_post", "blend_import_pre", "blend_import_post"):
    handler = getattr(bpy.app.handlers, name)
    handler.append(lambda *_args, _n=name: fired.append(_n))

print("\n=== libraries.load(link=True) ===")
fired.clear()
with bpy.data.libraries.load(canon, link=True) as (src, dst):  # pyright: ignore[reportGeneralTypeIssues]  # blender-python-stubs types libraries.load() as None; it is a context manager
    dst.collections = ["CanonHero"]
print("  handlers fired:", fired or "NONE")
linked = bpy.data.collections["CanonHero"]
bpy.context.scene.collection.children.link(linked)
lib = bpy.data.libraries[0]

print("\n=== Library.users_id (not in bl_rna.properties, but works) ===")
print("  'users_id' in bl_rna.properties ->", "users_id" in [p.identifier for p in bpy.types.Library.bl_rna.properties])
print("  'users_id' in dir(instance)     ->", "users_id" in dir(lib))
print("  lib.users_id ->", list(lib.users_id))

before = {d.name: d.session_uid for d in lib.users_id}
print("\n  session_uids before reload:", before)

print("\n=== lib.reload() ===")
fired.clear()
lib.reload()
print("  handlers fired:", fired or "NONE")
after = {d.name: d.session_uid for d in bpy.data.libraries[0].users_id}
print("  session_uids after reload :", after)
churned = sum(1 for k in before if after.get(k) != before[k])
print(f"  CHURNED: {churned} of {len(before)} datablocks got a fresh session_uid")

print("\n=== failed lib.reload() (invalid path) ===")
fired.clear()
saved = lib.filepath
lib.filepath = os.path.join(work, "gone.blend")
try:
    lib.reload()
except RuntimeError as exc:
    print(f"  RAISED RuntimeError: {exc}")
else:
    print("  did not raise")
print("  handlers fired:", fired or "NONE")
lib.filepath = saved

print("\n=== orphans_purge / libraries.remove fire nothing ===")
fired.clear()
purged = bpy.data.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=False)
print(f"  orphans_purge -> {purged}; handlers fired: {fired or 'NONE'}")
