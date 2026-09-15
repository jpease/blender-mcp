"""
Report which revert-matrix rows no longer anchor on their target file.

Run after any repair that edits a file the matrix reverts. A row whose `old`
text has vanished raises SystemExit inside the matrix itself, so this reports
the same condition up front, per row, instead of one row at a time.

Also flags rows whose reverted form does not PARSE, which is the defect class
found in row A2: pytest then fails the node for an IndentationError rather than
for the behaviour, and `run_nodes()` credits the row anyway.
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

missing, unparseable, ok = [], [], 0
for row in rm.REVERTS:
    text = row.path.read_text() if row.path.is_file() else ""
    if row.old is not None and row.old not in text:
        missing.append(row)
        continue
    ok += 1
    if row.path.suffix == ".py" and row.old is not None:
        reverted = text.replace(row.old, row.new) + row.also
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
