r"""
Print every RNA property and default of the file operators the handlers call or avoid.

Read from RNA rather than the docs, because several defaults surprise:
`save_as_mainfile.relative_remap` is True, and `check_existing` is True but has
no effect from Python. Also prints `use_file_compression`, which overrides a
bare save's `compress` default, and `use_scripts_auto_execute`, which
`open_shot` checks before a load. `wm.read_factory_settings` is included because
`reset_session` avoids it: it also loads factory preferences.

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
