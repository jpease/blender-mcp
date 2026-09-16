r"""
Read the full RNA property lists, with defaults, of the three file-lifecycle operators Task 6 calls.

Plan Task 6 Step 1: the defaults are read off the operator RNA, not off the
docs, because the load-bearing facts here are defaults that differ from what a
reader assumes (`save_as_mainfile.relative_remap` is True, `check_existing` is
True and inert programmatically). It also prints the two preferences that
override or widen an operator argument - `use_file_compression`, which beats a
bare save's `compress` default, and `use_scripts_auto_execute`, which `open_shot`
checks before a load - and `wm.read_factory_settings`, `reset_session`'s operator.

From the repository root::

    /opt/homebrew/bin/blender --background --factory-startup \
        --python scripts/blender_probes/file_lifecycle_operator_defaults.py
"""

import bpy

print("=== BLENDER ===", bpy.app.version_string)

for operator_name in ("open_mainfile", "save_mainfile", "save_as_mainfile", "read_factory_settings"):
    rna = getattr(bpy.ops.wm, operator_name).get_rna_type()
    print(f"\n=== wm.{operator_name} ===")
    for prop in rna.properties:
        if prop.identifier == "rna_type":
            continue
        default = getattr(prop, "default", "<no default>")
        print(f"  {prop.identifier:40} {prop.type:8} default={default!r}")

filepaths = bpy.context.preferences.filepaths
print("\n=== preferences.filepaths ===")
print("  use_scripts_auto_execute =", filepaths.use_scripts_auto_execute)
print("  use_file_compression     =", filepaths.use_file_compression)
