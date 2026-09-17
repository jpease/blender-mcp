"""
Check `save_shot(create_directories=...)` and `object_lookup.find_object` against real Blender.

Each check prints ok or FAIL, and the probe exits 1 if any failed.

A. Saving to `canon/shots/sh010.blend` whose directories do not exist: refused
   without the opt-in, with nothing created; saved with it (`created_directory`
   True, and the file reopens); `created_directory` False for a second save
   into the now-existing directory; refused outside the configured roots, with
   nothing created.
B. A canon `HeroCam` linked, overridden, saved and reopened: `bpy.data.objects.get`
   order, the `(name, None)` key, and what `find_object` and the scene tools'
   `_object` return. Then two libraries each linking a `Prop`, with no local
   one: `find_object` refuses, naming each library by leaf and real
   `session_uid` with the true total, and `Library.id_type` is `'LIBRARY'`,
   which `candidates.display_name` branches on. A missing name gives None.

From the repository root::

    /opt/homebrew/bin/blender --background --factory-startup \
        --python scripts/blender_probes/shot_directories_and_override_names.py
"""

import importlib
import os
import pathlib
import sys
import tempfile
import types

import bpy

ROOT = pathlib.Path(__file__).resolve().parents[2]
ADDON_DIR = ROOT / "src/blender_mcp/bundled/addon"
_PACKAGE = types.ModuleType("probe_addon")
_PACKAGE.__path__ = [str(ADDON_DIR)]  # type: ignore[attr-defined]
# `helpers`, imported by `handlers.scene`, reads the package's `ADDON_ID`; the real
# `__init__` would register Blender classes, so only the constant is provided.
_PACKAGE.ADDON_ID = "blender_mcp_probe"  # type: ignore[attr-defined]
sys.modules["probe_addon"] = _PACKAGE
file_lifecycle = importlib.import_module("probe_addon.handlers.file_lifecycle")
object_lookup = importlib.import_module("probe_addon.object_lookup")
scene_handlers = importlib.import_module("probe_addon.handlers.scene")

FAILURES: list[str] = []


def check(label: str, condition: bool) -> None:
    """
    Print one measured claim and remember it when it does not hold.

    Args:
        label: The claim.
        condition: Whether it held.

    """
    print(f"  [{'ok' if condition else 'FAIL'}] {label}")
    if not condition:
        FAILURES.append(label)


def refusal(call: object) -> str:
    """
    Run a call expected to be refused and return its message.

    Args:
        call: A zero-argument callable.

    Returns:
        str: The `ValueError` text, or `<not refused>`.

    """
    try:
        call()  # type: ignore[operator]
    except ValueError as exc:
        return str(exc)
    return "<not refused>"


def canon(path: pathlib.Path, collection: str, *object_names: str) -> None:
    """
    Write a canon `.blend` holding one collection of empties.

    Args:
        path: Where to save it.
        collection: The collection's name.
        *object_names: The objects inside it.

    """
    bpy.ops.wm.read_homefile(use_empty=True, use_factory_startup=True)
    target = bpy.data.collections.new(collection)
    bpy.context.scene.collection.children.link(target)
    for name in object_names:
        target.objects.link(bpy.data.objects.new(name, None))
    bpy.ops.wm.save_as_mainfile(filepath=str(path), compress=False, relative_remap=False)


def section_a(work: pathlib.Path) -> None:
    """Save into directories that do not exist yet."""
    print("A. save_shot(create_directories)")
    save = file_lifecycle.FileLifecycleHandlersMixin.save_shot
    bpy.ops.wm.read_homefile(use_empty=True, use_factory_startup=True)
    target = work / "project" / "canon" / "shots" / "sh010.blend"

    message = refusal(lambda: save(filepath=str(target)))
    check(f"refused without the opt-in: {message!r}", "create_directories=true" in message)
    check("nothing was created by the refusal", not (work / "project").exists())

    result = save(filepath=str(target), create_directories=True)
    check(f"saved with the opt-in, created_directory={result['created_directory']}", result["created_directory"])
    check("the saved file exists", target.is_file())
    bpy.ops.wm.open_mainfile(filepath=str(target), use_scripts=False)
    check("the saved file reopens", bpy.data.filepath == str(target.resolve()))

    again = save(filepath=str(target.parent / "sh020.blend"), create_directories=True)
    check("created_directory is False for an existing directory", again["created_directory"] is False)

    os.environ["BLENDERMCP_FILE_ROOTS"] = str(work / "project")
    try:
        outside = work / "elsewhere" / "shots" / "x.blend"
        message = refusal(lambda: save(filepath=str(outside), create_directories=True))
        check(f"refused outside the roots: {message!r}", "BLENDERMCP_FILE_ROOTS" in message)
        check("nothing was created outside the roots", not (work / "elsewhere").exists())
    finally:
        del os.environ["BLENDERMCP_FILE_ROOTS"]


def section_b(work: pathlib.Path) -> None:
    """Resolve names an override shares with its linked original."""
    print("B. find_object after override_hierarchy_create")
    canon_path = work / "canon.blend"
    canon(canon_path, "CanonHero", "HeroCam")
    bpy.ops.wm.read_homefile(use_empty=True, use_factory_startup=True)
    # The stubs type libraries.load() as None; it is a context manager at runtime.
    with bpy.data.libraries.load(str(canon_path), link=True) as (_source, linked):  # pyright: ignore[reportGeneralTypeIssues]
        linked.collections = ["CanonHero"]
    linked.collections[0].override_hierarchy_create(bpy.context.scene, bpy.context.view_layer)
    shot = work / "shot.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(shot), compress=False, relative_remap=False)
    bpy.ops.wm.open_mainfile(filepath=str(shot), use_scripts=False)

    order = [(obj.name, obj.library is not None) for obj in bpy.data.objects]
    print(f"  bpy.data.objects order after reopen: {order}")
    check("two objects are named HeroCam", [name for name, _ in order].count("HeroCam") == len(("override", "linked")))
    # Checked, not just printed: `object_lookup`'s docstring states both.
    check("local IDs come before linked ones in Main", order[0] == ("HeroCam", False))
    check("plain get(name) returns the local object", bpy.data.objects.get("HeroCam").library is None)
    keyed = bpy.data.objects.get(("HeroCam", None))  # pyright: ignore[reportArgumentType]
    check("the (name, None) key returns the local object", keyed.library is None)
    resolved = object_lookup.find_object(bpy.data.objects, "HeroCam")
    check("find_object returns the local override", resolved.library is None and resolved.override_library)
    check("the scene tools' _object returns the same object", scene_handlers._object("HeroCam") == resolved)
    check("a missing name is None", object_lookup.find_object(bpy.data.objects, "NoSuchObject") is None)
    check("the (name, None) key of a missing name is None", bpy.data.objects.get(("NoSuchObject", None)) is None)  # pyright: ignore[reportArgumentType]

    # Linked first but sorting last, so the checks can tell which order `get(name)` follows.
    first, second = work / "a" / "zeta.blend", work / "b" / "alpha.blend"
    first.parent.mkdir()
    second.parent.mkdir()
    canon(first, "PropsA", "Prop")
    canon(second, "PropsB", "Prop")
    bpy.ops.wm.read_homefile(use_empty=True, use_factory_startup=True)
    for path in (first, second):
        with bpy.data.libraries.load(str(path), link=True) as (_source, linked):  # pyright: ignore[reportGeneralTypeIssues]
            linked.objects = ["Prop"]
    print(f"  libraries: {[lib.name for lib in bpy.data.libraries]}")
    picked = bpy.data.objects.get("Prop").library.name
    sorted_first = sorted(lib.name for lib in bpy.data.libraries)[0]
    print(f"  plain get('Prop') picked: {picked}; first by name: {sorted_first}")
    check("get(name) follows Main insertion order: the library linked first", picked == "zeta.blend")
    check("get(name) does NOT follow library name order", sorted_first == "alpha.blend" and picked != sorted_first)
    # `candidates.display_name` shows only the leaf name on this value; the other
    # branch would publish a path.
    check("a real Library reports id_type 'LIBRARY'", all(lib.id_type == "LIBRARY" for lib in bpy.data.libraries))
    message = refusal(lambda: object_lookup.find_object(bpy.data.objects, "Prop"))
    check(f"two linked Props with no local one are refused: {message!r}", "more than one library" in message)
    check("the refusal names no directory", str(work) not in message)
    check("the refusal reports the true total", "2 of them" in message)
    check(
        "the refusal carries each library's real session_uid",
        all(f"(session_uid {lib.session_uid})" in message for lib in bpy.data.libraries),
    )


with tempfile.TemporaryDirectory() as scratch:
    section_a(pathlib.Path(scratch).resolve())
    section_b(pathlib.Path(scratch).resolve())

print(f"PROBE {'FAILED: ' + '; '.join(FAILURES) if FAILURES else 'PASSED'}")
sys.exit(1 if FAILURES else 0)
