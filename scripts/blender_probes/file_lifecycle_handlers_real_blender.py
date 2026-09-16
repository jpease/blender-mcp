r"""
Drive the real Task 6 handlers - `open_shot`, `save_shot`, `reset_session` - against real files in Blender.

Plan Task 6 Step 2b and Step 5. The handlers are the addon's own
`handlers/file_lifecycle.py`, loaded with its real `session.py`, `file_paths.py`
and `text_hygiene.py` (no socket server: the addon refuses to start one under
`--background`). Sections:

A. **Step 2b.** `save_shot` over an existing `.blend` without
   `confirm_overwrite`: refused, and the file's bytes (SHA-256 and mtime) are
   unchanged - including the in-place form, where the target is the open file.
B. **Step 5 round trip.** Open a fixture, save it to a new path (header checked:
   `compress=False` must beat `use_file_compression=True`), reopen it, save in
   place with confirmation, reset, and confirm the scene is empty and the epoch
   moved once per swap. It also prints `is_dirty` after an edit: under
   `--background` it stays False, so the unsaved-work refusal is shown live by
   `scripts/rig_scenarios/scenario_file_lifecycle.py`, not here.
C. **Step 5 failure modes, raw operator text vs what the client gets.** Missing,
   directory, non-`.blend`, corrupt magic and empty path through
   `wm.open_mainfile`; the unwritable destination through both save operators;
   a 64-byte truncated `.blend` (passes validation, fails in the operator).
   Each prints the operator's own `RuntimeError`, then the handler's response.
D. **Step 4b.** `use_scripts_auto_execute` on refuses the load; off allows it.
E. **Cycle-1 repair.** A `//libs/lib.blend` link saved from `projA/` to `projB/`
   with `relative_remap=False`: `save_shot` warns, the open session still reports
   the library found, and the reopened file reports it missing; with
   `relative_remap=True` there is no warning and the link resolves.
E2/E3. **Cycle-2 repair.** A `//textures/t2.png` image with no library warns
   and is missing on reopen; an indirect `//` library linked through an
   absolute direct link is not counted, and still resolves on reopen.
F. **Cycle-1 repair.** With roots set, a `<target>@` symlink planted to a file
   outside them: the save is refused and the outside file is unchanged.

From the repository root::

    /opt/homebrew/bin/blender --background --factory-startup \
        --python scripts/blender_probes/file_lifecycle_handlers_real_blender.py
"""

import hashlib
import importlib
import os
import pathlib
import shutil
import stat
import sys
import tempfile
import types

from collections.abc import Callable

import bpy

ROOT = pathlib.Path(__file__).resolve().parents[2]
ADDON_DIR = ROOT / "src/blender_mcp/bundled/addon"
FIXTURE = ROOT / "tests/fixtures/blend/empty_zstd.blend"
# A bare parent package, so the handler module imports its siblings relatively
# without executing the addon's `__init__.py` (which would register UI classes).
_PACKAGE = types.ModuleType("probe_addon")
_PACKAGE.__path__ = [str(ADDON_DIR)]  # type: ignore[attr-defined]
sys.modules["probe_addon"] = _PACKAGE
session = importlib.import_module("probe_addon.session")
file_lifecycle = importlib.import_module("probe_addon.handlers.file_lifecycle")


class ProbeServer(file_lifecycle.FileLifecycleHandlersMixin):
    """The mixin with the one server method it reads: an empty, stable capability table."""

    @staticmethod
    def _build_command_handlers() -> dict[str, object]:
        """
        Stand in for the server's dispatch table.

        Returns:
            dict[str, object]: Always empty, so `capabilities_changed` reads False.

        """
        return {}


def digest(path: pathlib.Path) -> tuple[str, int]:
    """
    Fingerprint a file's bytes and modification time.

    Args:
        path: The file.

    Returns:
        tuple[str, int]: SHA-256 prefix and `st_mtime_ns`.

    """
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16], path.stat().st_mtime_ns


def attempt(label: str, call: Callable[[], object]) -> None:
    """
    Run one handler call and print what a client would receive.

    Args:
        label: What is printed.
        call: The handler call.

    """
    try:
        print(f"  [{label}] OK -> {call()!r}")
    except (ValueError, RuntimeError) as exc:
        print(f"  [{label}] {type(exc).__name__} -> {str(exc)!r}")


def raw_operator(label: str, call: Callable[[], object]) -> None:
    """
    Run one operator call and print its raw outcome, which never reaches a client.

    Args:
        label: What is printed.
        call: The operator call.

    """
    try:
        print(f"  [{label}] RAW returned {call()!r}  (not a RuntimeError!)")
    except RuntimeError as exc:
        print(f"  [{label}] RAW RuntimeError -> {str(exc).strip()!r}")


def header(path: pathlib.Path) -> bytes:
    """
    Read a `.blend`'s first bytes.

    Args:
        path: The file.

    Returns:
        bytes: The first 12 bytes.

    """
    with path.open("rb") as handle:
        return handle.read(12)


for name in ("BLENDERMCP_FILE_ROOTS", "BLENDERMCP_OUTPUT_ROOTS"):
    os.environ.pop(name, None)
session.register_handlers()
server = ProbeServer()
work = pathlib.Path(tempfile.mkdtemp(prefix="t6_handlers_"))
prefs = bpy.context.preferences.filepaths
print("=== BLENDER ===", bpy.app.version_string)
print(
    "=== use_file_compression =",
    prefs.use_file_compression,
    " use_scripts_auto_execute =",
    prefs.use_scripts_auto_execute,
)

print("\n=== A. Step 2b: an existing .blend is not overwritten without confirm_overwrite ===")
existing = work / "existing.blend"
shutil.copyfile(FIXTURE, existing)
before = digest(existing)
attempt("save_shot(filepath=existing)", lambda: server.save_shot(filepath=str(existing)))
print(f"  bytes+mtime before={before} after={digest(existing)} unchanged={before == digest(existing)}")
attempt("open_shot(existing)", lambda: server.open_shot(str(existing)))
bpy.ops.mesh.primitive_cube_add()
before = digest(existing)
print(f"  open file now {pathlib.Path(bpy.data.filepath).name}, is_dirty={bpy.data.is_dirty}")
attempt("save_shot() in place, unconfirmed", server.save_shot)
print(f"  bytes+mtime before={before} after={digest(existing)} unchanged={before == digest(existing)}")

print("\n=== B. Step 5 round trip ===")
bpy.ops.wm.read_homefile(use_factory_startup=True)
fixture_copy = work / "fixture.blend"
shutil.copyfile(FIXTURE, fixture_copy)
epoch = session.session_snapshot()["session_epoch"]
attempt("open_shot(fixture)", lambda: server.open_shot(str(fixture_copy)))
bpy.ops.mesh.primitive_monkey_add()
# Under --background no undo step is pushed, and is_dirty stays False after an
# edit (printed here, not assumed); the dirty refusal is evidenced by the GUI rig.
print(f"  after adding a mesh under --background: is_dirty={bpy.data.is_dirty}")
saved = work / "saved.blend"
attempt("save_shot(saved)", lambda: server.save_shot(filepath=str(saved)))
print(f"  saved exists={saved.exists()} header={header(saved)!r} fixture header={header(FIXTURE)!r}")
attempt("open_shot(saved)", lambda: server.open_shot(str(saved)))
print(f"  reopened objects={sorted(o.name for o in bpy.data.objects)}")
attempt("save_shot() in place, confirmed", lambda: server.save_shot(confirm_overwrite=True))
print(f"  in-place header={header(saved)!r}")
attempt("reset_session()", server.reset_session)
attempt("reset_session(confirm=True)", lambda: server.reset_session(confirm=True))
print(f"  after reset: objects={len(bpy.data.objects)} filepath={bpy.data.filepath!r}")
print(f"  epoch {epoch} -> {session.session_snapshot()['session_epoch']} across 3 swaps (2 opens, 1 reset)")

print("\n=== C. Step 5 failure modes: raw operator text, then what the client receives ===")
plain = work / "notes.txt"
plain.write_text("hello", encoding="utf-8")
corrupt = work / "corrupt.blend"
corrupt.write_bytes(b"NOTABLEND" * 8)
bpy.ops.wm.read_homefile(use_factory_startup=True)
uncompressed = work / "uncompressed.blend"
bpy.ops.wm.save_as_mainfile(filepath=str(uncompressed), compress=False, relative_remap=False)
truncated = work / "truncated.blend"
truncated.write_bytes(uncompressed.read_bytes()[:64])
bpy.ops.wm.read_homefile(use_factory_startup=True)
open_cases = (
    ("missing", work / "missing.blend"),
    ("directory", work),
    ("non-blend", plain),
    ("corrupt magic", corrupt),
    ("empty path", ""),
    ("truncated to 64 B", truncated),
)
for label, path in open_cases:
    raw_operator(f"open {label}", lambda p=path: bpy.ops.wm.open_mainfile(filepath=str(p), use_scripts=False))
    attempt(f"open_shot {label}", lambda p=path: server.open_shot(str(p)))
readonly = work / "readonly"
readonly.mkdir()
readonly.chmod(stat.S_IRUSR | stat.S_IXUSR)
target = readonly / "x.blend"
raw_operator(
    "save_as read-only dir",
    lambda: bpy.ops.wm.save_as_mainfile(filepath=str(target), compress=False, relative_remap=False),
)
print(
    "  client form of that text:",
    repr(
        file_lifecycle._operator_failure_message(
            "save_shot",
            RuntimeError(f"Error: Cannot open file {target}@ for writing: Permission denied"),
            (str(target),),
        )
    ),
)
attempt("save_shot read-only dir", lambda: server.save_shot(filepath=str(target)))
in_place = work / "inplace.blend"
bpy.ops.wm.save_as_mainfile(filepath=str(in_place), compress=False, relative_remap=False)
readonly_inplace = work / "inplace_ro"
readonly_inplace.mkdir()
moved = readonly_inplace / "inplace.blend"
bpy.ops.wm.save_as_mainfile(filepath=str(moved), compress=False, relative_remap=False)
readonly_inplace.chmod(stat.S_IRUSR | stat.S_IXUSR)
raw_operator(
    "save_mainfile read-only dir",
    lambda: bpy.ops.wm.save_mainfile(filepath=str(moved), compress=False, relative_remap=False),
)
attempt("save_shot() in place, read-only dir", lambda: server.save_shot(confirm_overwrite=True))
readonly.chmod(stat.S_IRWXU)
readonly_inplace.chmod(stat.S_IRWXU)

print("\n=== D. Step 4b: use_scripts_auto_execute ===")
bpy.ops.wm.read_homefile(use_factory_startup=True)
prefs.use_scripts_auto_execute = True
attempt("open_shot with the preference ON", lambda: server.open_shot(str(fixture_copy)))
prefs.use_scripts_auto_execute = False
attempt("open_shot with the preference OFF", lambda: server.open_shot(str(fixture_copy)))
print("  current_filepath is absolute (T3-14 success field):", os.path.isabs(bpy.data.filepath))

print("\n=== E. relative_remap=False and //-relative library links (cycle-1 repair) ===")
bpy.ops.wm.read_homefile(use_factory_startup=True)
project_a, project_b, project_c = (work / name for name in ("projA", "projB", "projC"))
for directory in (project_a / "libs", project_b, project_c):
    directory.mkdir(parents=True)
library_file = project_a / "libs" / "lib.blend"
bpy.ops.wm.save_as_mainfile(filepath=str(library_file), compress=False, relative_remap=False)
bpy.ops.wm.read_homefile(use_empty=True, use_factory_startup=True)
shot_a = project_a / "shot.blend"
bpy.ops.wm.save_as_mainfile(filepath=str(shot_a), compress=False, relative_remap=False)
linker = bpy.data.libraries.load(str(library_file), link=True, relative=True)
with linker as (data_from, data_to):  # pyright: ignore[reportGeneralTypeIssues]  # stubs type libraries.load() as None
    data_to.objects = list(data_from.objects)
for linked in data_to.objects:
    bpy.context.scene.collection.objects.link(linked)  # a user, so the link survives the save
print("  linked objects:", sorted(obj.name for obj in data_to.objects))
bpy.ops.wm.save_mainfile(filepath=str(shot_a), compress=False, relative_remap=False)


def libraries_now() -> list[tuple[str, bool]]:
    """
    Read every linked library's link path and whether Blender finds it.

    Returns:
        list[tuple[str, bool]]: `(filepath, is_missing)` per library.

    """
    return [(library.filepath, library.is_missing) for library in bpy.data.libraries]


attempt("open_shot(projA/shot)", lambda: server.open_shot(str(shot_a))["filepath"])
print("  projA libraries:", libraries_now())
attempt("save_shot(projB/shot) remap off", lambda: server.save_shot(filepath=str(project_b / "shot.blend")))
print("  open session still reports:", libraries_now())
attempt("open_shot(projB/shot)", lambda: server.open_shot(str(project_b / "shot.blend"))["filepath"])
print("  projB libraries after reopen:", libraries_now())
attempt("open_shot(projA/shot)", lambda: server.open_shot(str(shot_a))["filepath"])
attempt(
    "save_shot(projC/shot) remap on",
    lambda: server.save_shot(filepath=str(project_c / "shot.blend"), relative_remap=True),
)
attempt("open_shot(projC/shot)", lambda: server.open_shot(str(project_c / "shot.blend"))["filepath"])
print("  projC libraries after reopen:", libraries_now())

print("\n=== E2. a //-relative image with no library (cycle-2 repair) ===")
bpy.ops.wm.read_homefile(use_empty=True, use_factory_startup=True)
(project_a / "textures").mkdir()
texture = project_a / "textures" / "t2.png"
image = bpy.data.images.new("t2", 4, 4)
image.filepath_raw = str(texture)
image.file_format = "PNG"
image.save()
image_shot = project_a / "image_shot.blend"
bpy.ops.wm.save_as_mainfile(filepath=str(image_shot), compress=False, relative_remap=False)
image.filepath = bpy.path.relpath(str(texture))
image.use_fake_user = True
bpy.ops.wm.save_mainfile(filepath=str(image_shot), compress=False, relative_remap=False)
attempt("open_shot(projA/image_shot)", lambda: server.open_shot(str(image_shot))["filepath"])
print("  libraries:", libraries_now(), " blend_paths:", bpy.utils.blend_paths(absolute=False, packed=False, local=True))
attempt("save_shot(projB/image_shot) remap off", lambda: server.save_shot(filepath=str(project_b / "image_shot.blend")))
attempt("open_shot(projB/image_shot)", lambda: server.open_shot(str(project_b / "image_shot.blend"))["filepath"])
print(
    "  projB image after reopen:",
    [(img.filepath, os.path.exists(bpy.path.abspath(img.filepath))) for img in bpy.data.images],
)

print("\n=== E3. an indirect //-relative library behind an absolute direct link (cycle-2 repair) ===")
bpy.ops.wm.read_homefile(use_factory_startup=True)
deep = project_a / "libs" / "deep"
deep.mkdir()
lib2 = deep / "lib2.blend"
bpy.ops.wm.save_as_mainfile(filepath=str(lib2), compress=False, relative_remap=False)
bpy.ops.wm.read_homefile(use_empty=True, use_factory_startup=True)
lib1 = project_a / "libs" / "lib1.blend"
bpy.ops.wm.save_as_mainfile(filepath=str(lib1), compress=False, relative_remap=False)
linker = bpy.data.libraries.load(str(lib2), link=True, relative=True)
with linker as (data_from, data_to):  # pyright: ignore[reportGeneralTypeIssues]  # stubs type libraries.load() as None
    data_to.objects = list(data_from.objects)
holder = bpy.data.collections.new("L1Coll")
bpy.context.scene.collection.children.link(holder)
for linked in data_to.objects:
    holder.objects.link(linked)
bpy.ops.wm.save_mainfile(filepath=str(lib1), compress=False, relative_remap=False)
bpy.ops.wm.read_homefile(use_empty=True, use_factory_startup=True)
indirect_shot = project_a / "indirect_shot.blend"
bpy.ops.wm.save_as_mainfile(filepath=str(indirect_shot), compress=False, relative_remap=False)
linker = bpy.data.libraries.load(str(lib1), link=True, relative=False)
with linker as (data_from, data_to):  # pyright: ignore[reportGeneralTypeIssues]  # stubs type libraries.load() as None
    data_to.collections = ["L1Coll"]
for linked in data_to.collections:
    bpy.context.scene.collection.children.link(linked)  # pyright: ignore[reportArgumentType]  # names become Collections
bpy.ops.wm.save_mainfile(filepath=str(indirect_shot), compress=False, relative_remap=False)
attempt("open_shot(projA/indirect_shot)", lambda: server.open_shot(str(indirect_shot))["filepath"])
print(
    "  libraries (relative?, leaf, indirect by users_id):",
    [
        (
            library.filepath.startswith("//"),
            pathlib.Path(library.filepath).name,
            all(user.is_library_indirect for user in library.users_id),
        )
        for library in bpy.data.libraries
    ],
)
relative_reported = [
    path for path in bpy.utils.blend_paths(absolute=False, packed=False, local=True) if path.startswith("//")
]
print("  blend_paths(local=True), relative entries:", relative_reported)
attempt(
    "save_shot(projB/indirect_shot) remap off",
    lambda: server.save_shot(filepath=str(project_b / "indirect_shot.blend")),
)
attempt("open_shot(projB/indirect_shot)", lambda: server.open_shot(str(project_b / "indirect_shot.blend"))["filepath"])
print(
    "  projB libraries after reopen (leaf, missing):",
    [(pathlib.Path(path).name, missing) for path, missing in libraries_now()],
)

print("\n=== F. a planted <target>@ symlink outside the roots (cycle-1 repair) ===")
bpy.ops.wm.read_homefile(use_factory_startup=True)
root = work / "root"
outside = work / "outside"
root.mkdir()
outside.mkdir()
victim = outside / "victim.txt"
victim.write_text("do not overwrite", encoding="utf-8")
(root / "fresh.blend@").symlink_to(victim)
os.environ["BLENDERMCP_FILE_ROOTS"] = str(root)
before = digest(victim)
attempt("save_shot(root/fresh.blend)", lambda: server.save_shot(filepath=str(root / "fresh.blend")))
os.environ.pop("BLENDERMCP_FILE_ROOTS")
print(
    f"  victim unchanged={digest(victim) == before} content={victim.read_text(encoding='utf-8')!r} "
    f"fresh.blend exists={(root / 'fresh.blend').exists()}"
)
