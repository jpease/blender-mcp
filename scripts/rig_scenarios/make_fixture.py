"""
Build the rig's Step 5 fixture: one object named RigFixtureCube, nothing else.

Runs inside Blender, because it needs `bpy`. From the repository root::

    /opt/homebrew/bin/blender --background --factory-startup \
        --python scripts/rig_scenarios/make_fixture.py -- <work>/fixture.blend

The single distinctive object is the whole design: the scenario asserts the
post-swap object list is exactly `["RigFixtureCube"]`, which a factory startup's
`[Camera, Cube, Light]` cannot satisfy by accident. That is what makes the swap
observable rather than a no-op a post-load ping would trivially survive.
"""

import sys

import bpy

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_cube_add()
cube = bpy.context.active_object
cube.name = "RigFixtureCube"
cube.data.name = "RigFixtureMesh"
target = sys.argv[sys.argv.index("--") + 1]
# compress is passed explicitly because the operator's own default (False) is not
# what you get: `preferences.filepaths.use_file_compression` is True at factory
# settings and wins, so a bare save writes zstd. Measured on 5.2.2; the same trap
# applies to Task 6's save_shot.
bpy.ops.wm.save_as_mainfile(filepath=target, compress=False, relative_remap=False)
print("FIXTURE: objects =", sorted(o.name for o in bpy.data.objects))
print("FIXTURE: saved to", bpy.data.filepath)
