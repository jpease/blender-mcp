"""
Report revert-matrix rows that no longer apply cleanly to their target file.

Run from the repository root after editing any file the matrix reverts. It lists
three kinds of row: BROKEN, whose `old` text is gone (the matrix would stop at the
first); UNPARSEABLE, whose reverted file does not compile, so its test would fail
on syntax rather than behaviour; and AMBIGUOUS, whose `old` text occurs more than
once, so `apply()` reverts the first copy, which may not be the site the row
means. Ambiguous rows are reported, not refused; fix one by adding a line of
context to its anchor.
"""

import importlib.util
import pathlib
import py_compile
import sys
import tempfile

MATRIX = pathlib.Path("scripts/revert_matrix.py")
spec = importlib.util.spec_from_file_location("rm", MATRIX)
if spec is None or spec.loader is None:
    raise SystemExit(f"could not load {MATRIX} - run this from the repository root")
rm = importlib.util.module_from_spec(spec)
sys.modules["rm"] = rm
spec.loader.exec_module(rm)

missing, unparseable, ambiguous, ok = [], [], [], 0
for row in rm.REVERTS:
    text = row.path.read_text() if row.path.is_file() else ""
    if row.old is not None and row.old not in text:
        missing.append(row)
        continue
    ok += 1
    if row.old is not None and text.count(row.old) > 1:
        ambiguous.append((row, text.count(row.old)))
    if row.path.suffix == ".py" and row.old is not None:
        # Mirror `apply()` exactly, or this checks a file the harness never runs.
        reverted = text.replace(row.old, row.new, 1) + row.also
        tmp = pathlib.Path(tempfile.mkdtemp()) / "r.py"
        tmp.write_text(reverted)
        try:
            py_compile.compile(str(tmp), doraise=True)
        except py_compile.PyCompileError as exc:
            unparseable.append((row, str(exc).strip().splitlines()[-1]))

print(f"rows: {len(rm.REVERTS)}   anchors intact: {ok}   anchors BROKEN: {len(missing)}")
for row in missing:
    print(f"  BROKEN  {row.label}\n            in {row.path.relative_to(rm.ROOT)}")
print(f"\nrows whose reverted form does not parse: {len(unparseable)}")
for row, err in unparseable:
    print(f"  UNPARSEABLE  {row.label}\n                 {err}")
print(f"\nrows whose anchor is not unique (apply() takes the first): {len(ambiguous)}")
for row, count in ambiguous:
    print(f"  AMBIGUOUS x{count}  {row.label}\n                  in {row.path.relative_to(rm.ROOT)}")
