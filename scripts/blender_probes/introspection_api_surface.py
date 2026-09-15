"""Re-verify the handoff's 5.2.1 API facts against 5.2.2: introspection half."""

import bpy

print("=== BLENDER ===", bpy.app.version_string)

print("\n=== FACT 1: override methods exist on Collection and ID ===")
for cls in (bpy.types.Collection, bpy.types.ID, bpy.types.Object):
    names = sorted(f.identifier for f in cls.bl_rna.functions)
    print(f"{cls.__name__}.bl_rna.functions -> {names}")

print("\n=== FACT 1b: signatures via __doc__ ===")
c = bpy.data.collections.new("probe")
print("override_hierarchy_create.__doc__:", c.override_hierarchy_create.__doc__.split("\n")[0])
print("override_create.__doc__          :", c.override_create.__doc__.split("\n")[0])

print("\n=== FACT: dir() on a TYPE object is blind (the methodology trap) ===")
print("'copy' in dir(bpy.types.Collection) ->", "copy" in dir(bpy.types.Collection))
print("'copy' in dir(instance)            ->", "copy" in dir(c))

print("\n=== FACT 5: Library.reload exists ===")
print("Library.bl_rna.functions ->", sorted(f.identifier for f in bpy.types.Library.bl_rna.functions))

print("\n=== FACT 9: bpy.app.handlers ===")
for h in (
    "load_pre",
    "load_post",
    "load_post_fail",
    "save_pre",
    "save_post",
    "save_post_fail",
    "blend_import_pre",
    "blend_import_post",
    "persistent",
):
    print(f"  {h}: {hasattr(bpy.app.handlers, h)}")

print("\n=== FACT 8: operator defaults ===")
for op, prop in (
    ("save_as_mainfile", "relative_remap"),
    ("save_mainfile", "relative_remap"),
    ("save_as_mainfile", "check_existing"),
    ("save_mainfile", "check_existing"),
    ("open_mainfile", "use_scripts"),
):
    rna = getattr(bpy.ops.wm, op).get_rna_type()
    print(f"  wm.{op}.{prop} default = {rna.properties[prop].default}")
print("  preferences.filepaths.use_scripts_auto_execute =", bpy.context.preferences.filepaths.use_scripts_auto_execute)

print("\n=== FACT 11: bpy.path.abspath does not normalise or absolutize ===")
print("  abspath('//../escape.blend') ->", bpy.path.abspath("//../escape.blend"))
print("  abspath('relative.blend')    ->", bpy.path.abspath("relative.blend"))

print("\n=== FACT 12: orphans_purge signature ===")
print(" ", bpy.data.orphans_purge.__doc__.split("\n")[0])
