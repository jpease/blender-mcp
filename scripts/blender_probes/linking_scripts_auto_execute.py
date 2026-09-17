r"""
Check that linking, reloading, overriding or appending a hostile `.blend` never runs its Python.

Builds a library with two payloads that each write a marker file when run: a
registered text block (`use_module=True`), linked explicitly, and a scripted
driver on the object in `CanonHero`, whose expression needs script execution.
Then, with `use_scripts_auto_execute` off (the control) and on, opens a fresh
shot with the matching `use_scripts`, which sets the session's script-execution
flag, and runs:

0. A positive control: a local object with the same driver, after a depsgraph
   update. It must run with the preference on and not off, or the "on" rows
   prove nothing.
1. `libraries.load(link=True)` of the collection and the text, then an update.
2. `lib.reload()`, then an update.
3. The Route C override, then an update.
4. `libraries.load(link=False)`, an append as `handlers/polyhaven.py` does for a
   downloaded `.blend`, with the objects linked into the scene, then an update.

Each step prints the markers present, then deletes them. Drivers re-evaluate on
every update, so a driver marker after step 2 or 3 shows execution was still
possible then, not that the call caused it.

From the repository root::

    /opt/homebrew/bin/blender --background --factory-startup \
        --python scripts/blender_probes/linking_scripts_auto_execute.py
"""

import os
import tempfile

from typing import cast

import bpy

print("=== BLENDER ===", bpy.app.version_string)
work = tempfile.mkdtemp(prefix="t7_autoexec_")
canon = os.path.join(work, "hostile_canon.blend")
MARKERS = {name: os.path.join(work, f"marker_{name}") for name in ("text", "driver", "local_driver")}


def driver_expression(marker: str) -> str:
    """
    Build a driver expression that touches a marker file and evaluates to 0.

    Args:
        marker: The file to create.

    Returns:
        str: The expression; not a "simple expression", so it needs script execution.

    """
    return f"(__import__('pathlib').Path(r'{marker}').touch(), 0)[1]"


def add_driver(obj: bpy.types.Object, marker: str) -> None:
    """
    Drive the object's X location with the marker-writing expression.

    Args:
        obj: The object.
        marker: The marker file.

    """
    driver = obj.driver_add("location", 0).driver
    driver.type = "SCRIPTED"
    driver.expression = driver_expression(marker)


def markers(label: str) -> None:
    """
    Print which payloads have run, then clear the markers.

    Args:
        label: The step.

    """
    fired = sorted(name for name, path in MARKERS.items() if os.path.exists(path))
    print(f"    {label:<44} payloads that RAN: {fired or 'none'}")
    for path in MARKERS.values():
        if os.path.exists(path):
            os.remove(path)


def update() -> None:
    """Force a depsgraph evaluation, which is when drivers run in `--background`."""
    scene = bpy.data.scenes[0]
    scene.frame_set(scene.frame_current + 1)
    scene.view_layers[0].update()


# Build the hostile library with script execution off, so building it runs nothing.
bpy.ops.wm.read_factory_settings(use_empty=True)
text = bpy.data.texts.new("payload.py")
text.write(f"import pathlib\npathlib.Path(r'{MARKERS['text']}').touch()\n")
text.use_module = True
hero = bpy.data.collections.new("CanonHero")
bpy.data.scenes[0].collection.children.link(hero)
body = bpy.data.objects.new("HeroBody", None)
hero.objects.link(body)
add_driver(body, MARKERS["driver"])
bpy.ops.wm.save_as_mainfile(filepath=canon, compress=False, relative_remap=False)
markers("(building the library)")

for enabled in (False, True):
    print(f"\n=== use_scripts_auto_execute = {enabled} ===")
    bpy.context.preferences.filepaths.use_scripts_auto_execute = enabled
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.context.preferences.filepaths.use_scripts_auto_execute = enabled
    shot = os.path.join(work, f"shot_{enabled}.blend")
    bpy.ops.wm.save_as_mainfile(filepath=shot)
    bpy.ops.wm.open_mainfile(filepath=shot, use_scripts=enabled)
    print("    preference after open:", bpy.context.preferences.filepaths.use_scripts_auto_execute)
    scene = bpy.data.scenes[0]
    local = bpy.data.objects.new("LocalControl", None)
    scene.collection.objects.link(local)
    add_driver(local, MARKERS["local_driver"])
    update()
    markers("0. positive control (local driver)")
    scene.collection.objects.unlink(local)
    bpy.data.objects.remove(local)
    update()
    markers("   (control removed)")

    with bpy.data.libraries.load(canon, link=True) as (_f, to):  # pyright: ignore[reportGeneralTypeIssues]  # libraries.load() is a context manager at runtime
        to.collections = ["CanonHero"]
        to.texts = ["payload.py"]
    linked = cast("bpy.types.Collection", to.collections[0])
    scene.collection.children.link(linked)
    markers("1a. libraries.load(link=True), no update")
    update()
    markers("1b. ... after a depsgraph update")

    library = linked.library
    library.reload()
    markers("2a. lib.reload(), no update")
    update()
    markers("2b. ... after a depsgraph update")

    linked = next(c for c in bpy.data.collections if c.library is not None)
    linked.override_hierarchy_create(scene, scene.view_layers[0], do_fully_editable=True)
    markers("3a. Route C override, no update")
    update()
    markers("3b. ... after a depsgraph update")

    for collection in list(bpy.data.collections):
        bpy.data.collections.remove(collection)
    for library in list(bpy.data.libraries):
        bpy.data.libraries.remove(library)
    update()
    markers("   (linked data removed)")
    with bpy.data.libraries.load(canon, link=False) as (source, to):  # pyright: ignore[reportGeneralTypeIssues]  # libraries.load() is a context manager at runtime
        to.objects = source.objects
        to.texts = ["payload.py"]
    markers("4a. libraries.load(link=False) append, no update")
    for appended in to.objects:
        scene.collection.objects.link(cast("bpy.types.Object", appended))
    update()
    markers("4b. ... objects in the scene, after an update")
    print("    bpy.app.autoexec_fail:", bpy.app.autoexec_fail)
