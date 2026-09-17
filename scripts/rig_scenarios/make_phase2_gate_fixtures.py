r"""
Build the two `.blend` fixtures `scenario_phase2_gate.py` needs.

Runs inside Blender. From the repository root::

    /opt/homebrew/bin/blender --background --factory-startup \
        --python scripts/rig_scenarios/make_phase2_gate_fixtures.py -- \
        <work>/canon.blend <work>/shot.blend

Both pass `compress=False` explicitly, because the factory `use_file_compression`
preference overrides the operator's default.

- ``canon.blend`` -- collection ``CanonHero`` holding mesh object ``HeroBody``: the
  canon library ``link_canon_library`` links from.
- ``shot.blend`` -- an empty scene with no libraries, for ``open_shot`` to open. The
  scenario links the canon into it and saves the result under the rig's work dir.
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
