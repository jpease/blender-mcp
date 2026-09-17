r"""Print which file-operator failures raise rather than return {'CANCELLED'}, and the .blend magic bytes."""

import os
import tempfile

from collections.abc import Callable

import bpy

print("=== BLENDER ===", bpy.app.version_string)
work = tempfile.mkdtemp(prefix="api522_")


def attempt(label: str, fn: Callable[[], set[str]]) -> None:
    """
    Run `fn`, reporting whether it raised or returned an operator status set.

    These operators raise `RuntimeError` on failure instead of returning
    `{'CANCELLED'}`, so a handler that checks only for CANCELLED misses failures.

    Args:
        label: Name for the case, printed with the outcome.
        fn: The operator call to attempt.

    """
    try:
        result = fn()
    except RuntimeError as exc:
        print(f"  [{label}] RAISED RuntimeError: {exc}")
    except Exception as exc:
        print(f"  [{label}] RAISED {type(exc).__name__}: {exc}")
    else:
        print(f"  [{label}] RETURNED {sorted(result)}")


print("\n=== FACT 10 + error SHAPES 1-4: open/save failure discipline ===")
missing = os.path.join(work, "nope.blend")
attempt("shape1 open missing", lambda: bpy.ops.wm.open_mainfile(filepath=missing, use_scripts=False))
attempt("shape2 open directory", lambda: bpy.ops.wm.open_mainfile(filepath=work, use_scripts=False))

notblend = os.path.join(work, "plain.txt")
with open(notblend, "w", encoding="utf-8") as h:
    h.write("hello")
attempt("shape2 open non-blend", lambda: bpy.ops.wm.open_mainfile(filepath=notblend, use_scripts=False))
attempt("shape2 open EMPTY PATH", lambda: bpy.ops.wm.open_mainfile(filepath="", use_scripts=False))

# A real .blend truncated to 64 bytes: passes magic + extension validation, fails to load.
good = os.path.join(work, "good.blend")
bpy.ops.wm.save_as_mainfile(filepath=good, relative_remap=False)
with open(good, "rb") as h:
    head = h.read(64)
trunc = os.path.join(work, "trunc.blend")
with open(trunc, "wb") as h:
    h.write(head)
attempt("shape3 open truncated", lambda: bpy.ops.wm.open_mainfile(filepath=trunc, use_scripts=False))

unwritable = os.path.join(work, "no", "such", "dir", "x.blend")
attempt("shape4 save unwritable", lambda: bpy.ops.wm.save_as_mainfile(filepath=unwritable, relative_remap=False))

print("\n=== check_existing is inert: does save_as overwrite silently? ===")
victim = os.path.join(work, "victim.blend")
with open(victim, "wb") as h:
    h.write(b"ORIGINAL BYTES, NOT A BLEND")
before = os.path.getsize(victim)
attempt(
    "save_as onto existing",
    lambda: bpy.ops.wm.save_as_mainfile(filepath=victim, check_existing=True, relative_remap=False),
)
after = os.path.getsize(victim)
print(f"  victim size {before} -> {after}; overwritten = {before != after}")

print("\n=== FACT: the three .blend magic signatures ===")
for name in ("good.blend",):
    with open(os.path.join(work, name), "rb") as h:
        print(f"  uncompressed header = {h.read(16)!r}")
compressed = os.path.join(work, "zstd.blend")
bpy.ops.wm.save_as_mainfile(filepath=compressed, compress=True, relative_remap=False)
with open(compressed, "rb") as h:
    print(f"  compressed header   = {h.read(16)!r}")
print(f"  reopen compressed   -> {sorted(bpy.ops.wm.open_mainfile(filepath=compressed, use_scripts=False))}")
