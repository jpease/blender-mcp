"""
Build the rig scenarios' swap fixture: one object named RigFixtureCube, nothing else.

Runs inside Blender. From the repository root::

    /opt/homebrew/bin/blender --background --factory-startup \
        --python scripts/rig_scenarios/make_fixture.py -- <work>/fixture.blend

The scenarios expect exactly `["RigFixtureCube"]` after the swap, which a factory
startup scene cannot match by accident, so a swap that did nothing fails.
"""

import sys

import bpy

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_cube_add()
cube = bpy.context.active_object
cube.name = "RigFixtureCube"
cube.data.name = "RigFixtureMesh"
target = sys.argv[sys.argv.index("--") + 1]
# Explicit because the factory preference `use_file_compression` overrides the
# operator's False default, so a bare save writes a compressed file.
bpy.ops.wm.save_as_mainfile(filepath=target, compress=False, relative_remap=False)
print("FIXTURE: objects =", sorted(o.name for o in bpy.data.objects))
print("FIXTURE: saved to", bpy.data.filepath)
