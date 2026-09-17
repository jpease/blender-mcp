"""
Report which revert-matrix rows no longer anchor on their target file.

Run after any repair that edits a file the matrix reverts. A row whose `old`
text has vanished raises SystemExit inside the matrix itself, so this reports
the same condition up front, per row, instead of one row at a time.

Also flags rows whose reverted form does not PARSE, which is the defect class
found in row A2: pytest then fails the node for an IndentationError rather than
for the behaviour, and `run_nodes()` credits the row anyway.

And flags rows whose anchor appears **more than once** in its file, which is a
third way a row can prove nothing. `revert_matrix.apply()` replaces only the
first occurrence, so such a row edits whichever site comes first in the file -
not necessarily the one it names. Found the hard way post-Phase-2: the
`get_object_info` row's 8-space `obj = find_object(...)` anchor also matches the
16-space copy in `_resolve_targets` as a suffix, and the earlier one won, so the
row reverted a site whose behaviour its node does not cover and was reported as
a SURVIVOR by a full run. An ambiguous anchor is reported rather than refused:
four rows predate this check and each still breaks its own nodes, because the
first occurrence happens to be the right one. Make an anchor unique by carrying
one more line of context.
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
        # One replacement, exactly as `apply()` does it: checking a mutation the
        # harness does not perform is its own kind of false assurance.
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
