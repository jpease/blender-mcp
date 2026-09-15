"""Chase the two 5.2.2 deviations: the compress default, and whether error shape 3 still exists."""

import gzip
import os
import tempfile

import bpy

print("=== BLENDER ===", bpy.app.version_string)
work = tempfile.mkdtemp(prefix="api522c_")

print("\n=== compress default on save_as_mainfile / save_mainfile ===")
for op in ("save_as_mainfile", "save_mainfile"):
    rna = getattr(bpy.ops.wm, op).get_rna_type()
    print(f"  wm.{op}.compress default = {rna.properties['compress'].default}")
print("  preferences.filepaths.use_file_compression =", bpy.context.preferences.filepaths.use_file_compression)

print("\n=== headers actually written ===")
for label, compress in (
    ("default (no compress arg)", None),
    ("compress=False", False),
    ("compress=True", True),
):
    path = os.path.join(work, f"{label.split()[0]}.blend")
    # Spelled out rather than unpacked, because "omit the argument" is one of the
    # three cases under test and a **kwargs dict cannot express it distinctly.
    if compress is None:
        bpy.ops.wm.save_as_mainfile(filepath=path, relative_remap=False)
    else:
        bpy.ops.wm.save_as_mainfile(filepath=path, relative_remap=False, compress=compress)
    with open(path, "rb") as h:
        print(f"  {label:26} -> {h.read(12)!r}")

print("\n=== is error shape 3 (Missing DNA block) still reachable? ===")
uncompressed = os.path.join(work, "uncompressed.blend")
bpy.ops.wm.save_as_mainfile(filepath=uncompressed, compress=False, relative_remap=False)
for n in (64, 512, 4096):
    with open(uncompressed, "rb") as h:
        head = h.read(n)
    trunc = os.path.join(work, f"trunc{n}.blend")
    with open(trunc, "wb") as h:
        h.write(head)
    try:
        bpy.ops.wm.open_mainfile(filepath=trunc, use_scripts=False)
    except RuntimeError as exc:
        print(f"  truncated to {n:5} bytes (magic {head[:7]!r}): {exc}")
    else:
        print(f"  truncated to {n:5} bytes: OPENED (!)")

print("\n=== legacy gzip acceptance ===")

gz = os.path.join(work, "legacy_gzip.blend")
with open(uncompressed, "rb") as src, gzip.open(gz, "wb") as dst:
    dst.write(src.read())
with open(gz, "rb") as h:
    print(f"  gzip header = {h.read(4)!r}")
try:
    print("  open gzip ->", sorted(bpy.ops.wm.open_mainfile(filepath=gz, use_scripts=False)))
except RuntimeError as exc:
    print(f"  open gzip RAISED: {exc}")
