"""
Print Blender's real path-bearing error texts beside `sanitize_blender_error`'s output.

Sanitizer tests should copy these texts rather than invent them: the format is
not a contract, and each shape breaks a different plausible sanitizer. Every
shape runs in a plain work directory and in one whose name has a space and an
apostrophe, which defeats naive quoting.

Also prints what `bpy.path.abspath` returns for `//` forms with and without an
open file, and opens the committed zstd and gzip fixtures in
`tests/fixtures/blend/`.

Run from the repository root; the empty-path shape reports the process CWD::

    /opt/homebrew/bin/blender --background --factory-startup \
        --python scripts/blender_probes/file_path_error_shapes.py
"""

import importlib.util
import os
import pathlib
import sys
import tempfile
import types

from collections.abc import Callable
from typing import Any

import bpy

ADDON = pathlib.Path.cwd() / "src/blender_mcp/bundled/addon"
FIXTURES = pathlib.Path.cwd() / "tests/fixtures/blend"

# A bare parent package: `file_paths` imports `text_hygiene` relatively.
_PACKAGE = types.ModuleType("probe_file_paths_pkg")
_PACKAGE.__path__ = [str(ADDON)]  # type: ignore[attr-defined]
sys.modules["probe_file_paths_pkg"] = _PACKAGE
_spec = importlib.util.spec_from_file_location("probe_file_paths_pkg.file_paths", ADDON / "file_paths.py")
if _spec is None or _spec.loader is None:
    raise SystemExit("run this from the repository root")
file_paths = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(file_paths)

print("=== BLENDER ===", bpy.app.version_string)
print("=== CWD ===", os.getcwd())


def capture(label: str, call: Callable[[], object]) -> None:
    """
    Run one failing call and print its raw and sanitized error text.

    Args:
        label: Which shape and mode this is.
        call: The Blender call expected to raise.

    """
    try:
        result = call()
    except RuntimeError as exc:
        print(f"[{label}]")
        print(f"  RAW:       {str(exc)!r}")
        print(f"  SANITIZED: {file_paths.sanitize_blender_error(exc)!r}")
    else:
        print(f"[{label}] DID NOT RAISE: {result!r}")


def reload_from(missing: str) -> None:
    """
    Point the one linked library at a missing file and reload it.

    Args:
        missing: The invalid library path to set.

    """
    library = bpy.data.libraries[0]
    library.filepath = missing
    library.reload()


def capture_shapes(work: str) -> None:
    """
    Produce the five known shapes inside one work directory.

    Args:
        work: The directory to build fixtures in.

    """
    print(f"\n=== work dir {work!r} ===")
    bpy.ops.wm.read_homefile(use_factory_startup=True)
    good = os.path.join(work, "good.blend")
    bpy.ops.wm.save_as_mainfile(filepath=good, compress=False, relative_remap=False)
    with open(good, "rb") as handle:
        head = handle.read(64)
    truncated = os.path.join(work, "truncated.blend")
    with open(truncated, "wb") as handle:
        handle.write(head)
    corrupt = os.path.join(work, "corrupt.blend")
    with open(corrupt, "wb") as handle:
        handle.write(b"NOTABLEND" + head)
    plain = os.path.join(work, "plain.txt")
    with open(plain, "w", encoding="utf-8") as handle:
        handle.write("hello")

    missing = os.path.join(work, "missing.blend")
    capture("1 open missing", lambda: bpy.ops.wm.open_mainfile(filepath=missing, use_scripts=False))
    capture("2 open directory", lambda: bpy.ops.wm.open_mainfile(filepath=work, use_scripts=False))
    capture("2 open non-blend", lambda: bpy.ops.wm.open_mainfile(filepath=plain, use_scripts=False))
    capture("2 open corrupt magic", lambda: bpy.ops.wm.open_mainfile(filepath=corrupt, use_scripts=False))
    capture("3 open truncated", lambda: bpy.ops.wm.open_mainfile(filepath=truncated, use_scripts=False))
    unwritable = os.path.join(work, "no", "such", "dir", "x.blend")
    capture("4 save unwritable", lambda: bpy.ops.wm.save_as_mainfile(filepath=unwritable, relative_remap=False))

    bpy.ops.wm.read_homefile(use_empty=True, use_factory_startup=True)
    # `Any` because the stubs type `libraries.load` as returning None; it is a context manager.
    linker: Any = bpy.data.libraries.load(good, link=True)
    with linker as (data_from, data_to):
        data_to.objects = list(data_from.objects)
    capture("5 library reload", lambda: reload_from(os.path.join(work, "gone.blend")))


capture("2 open EMPTY PATH (process cwd)", lambda: bpy.ops.wm.open_mainfile(filepath="", use_scripts=False))
capture_shapes(tempfile.mkdtemp(prefix="fp_shapes_"))
capture_shapes(tempfile.mkdtemp(prefix="fp shapes o'brien "))

print("\n=== bpy.path.abspath on '//' forms ===")
bpy.ops.wm.read_homefile(use_factory_startup=True)
print(f"  no file open: bpy.data.filepath={bpy.data.filepath!r}")
for form in ("//shot.blend", "//../escape.blend", "relative.blend"):
    print(f"    {form!r:22} -> {bpy.path.abspath(form)!r}")
saved = os.path.join(tempfile.mkdtemp(prefix="fp_abspath_"), "fx", "shot.blend")
os.makedirs(os.path.dirname(saved))
bpy.ops.wm.save_as_mainfile(filepath=saved, relative_remap=False)
print(f"  file open: bpy.data.filepath={bpy.data.filepath!r}")
for form in ("//shot.blend", "//../escape.blend", "relative.blend"):
    print(f"    {form!r:22} -> {bpy.path.abspath(form)!r}")

print("\n=== the committed fixtures open, and carry the header their names claim ===")
for name in ("empty_zstd.blend", "empty_gzip.blend"):
    path = FIXTURES / name
    print(f"  {name}: header={path.read_bytes()[:4]!r}")
    print(f"    open -> {sorted(bpy.ops.wm.open_mainfile(filepath=str(path), use_scripts=False))}")
