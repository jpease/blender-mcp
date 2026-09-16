r"""
Build the phase-2 gate scenario's three `.blend` fixtures (plan Task 10, "What to build").

Runs inside Blender, because it needs `bpy`. From the repository root::

    /opt/homebrew/bin/blender --background --factory-startup \
        --python scripts/rig_scenarios/make_phase2_gate_fixtures.py -- \
        <work>/canon.blend <work>/shot.blend

Two files, both saved with `compress=False, relative_remap=False` (the same
trap `make_fixture.py` documents: `use_file_compression` is True at factory
settings and wins over the operator's own default unless passed explicitly):

- ``canon.blend`` -- a named collection ``CanonHero`` holding one mesh object
  ``HeroBody``, standing in for a canon asset library. This is what
  ``link_canon_library`` links *from*.
- ``shot.blend`` -- an empty scene with no libraries, standing in for the shot
  ``open_shot`` opens. It must be a distinct file from ``canon.blend``: the
  scenario links the canon file *into* the scene this file opens, then saves
  the result to a third path the scenario itself picks (the rig's work dir),
  so only these two need to exist up front.
"""

import sys

import bpy


def _build_canon(path: str) -> None:
    """
    Write a canon library: one collection, one mesh object.

    Args:
        path: Where to save it.

    """
    bpy.ops.wm.read_homefile(use_empty=True, use_factory_startup=True, load_ui=False)
    collection = bpy.data.collections.new("CanonHero")
    bpy.context.scene.collection.children.link(collection)
    mesh = bpy.data.meshes.new("HeroBodyMesh")
    mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
    body = bpy.data.objects.new("HeroBody", mesh)
    collection.objects.link(body)
    bpy.ops.wm.save_as_mainfile(filepath=path, compress=False, relative_remap=False)
    print("FIXTURE: canon collections =", sorted(c.name for c in bpy.data.collections))
    print("FIXTURE: canon saved to", bpy.data.filepath)


def _build_shot(path: str) -> None:
    """
    Write an empty shot with no libraries.

    Args:
        path: Where to save it.

    """
    bpy.ops.wm.read_homefile(use_empty=True, use_factory_startup=True, load_ui=False)
    bpy.ops.wm.save_as_mainfile(filepath=path, compress=False, relative_remap=False)
    print("FIXTURE: shot objects =", sorted(o.name for o in bpy.data.objects))
    print("FIXTURE: shot saved to", bpy.data.filepath)


_EXPECTED_ARG_COUNT = 2
_ARGS = sys.argv[sys.argv.index("--") + 1 :]
if len(_ARGS) != _EXPECTED_ARG_COUNT:
    raise SystemExit(f"expected <canon.blend> <shot.blend>, got {_ARGS!r}")
_canon_path, _shot_path = _ARGS
_build_canon(_canon_path)
_build_shot(_shot_path)
