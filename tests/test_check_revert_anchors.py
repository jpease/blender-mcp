"""
Tests for `scripts/check_revert_anchors.py`, the revert-matrix check `just check` runs.

The matrix quotes source text, so editing a quoted line silently disables a row. The
checker's exit status is what stops a hand-off that left a row behind its source.
"""

from __future__ import annotations

import subprocess
import sys

from pathlib import Path

import pytest

CHECKER = Path(__file__).resolve().parents[1] / "scripts" / "check_revert_anchors.py"

# A stand-in for `scripts/revert_matrix.py`: the checker reads only `ROOT` and each row's
# label, path, old, new and also.
_MATRIX = """
import pathlib

from dataclasses import dataclass

ROOT = pathlib.Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Row:
    label: str
    path: pathlib.Path
    old: str | None
    new: str
    also: str = ""


REVERTS = [Row("the row", ROOT / "target.py", {old!r}, {new!r})]
"""

# `return 42` occurs twice, so a row anchored on it is ambiguous.
_TARGET = "def answer():\n    return 42\n\n\ndef again():\n    return 42\n"


@pytest.mark.parametrize(
    ("old", "new", "expected_status"),
    [
        pytest.param("def answer():", "def answer(value=None):", 0, id="intact"),
        pytest.param("return 42", "return 0", 0, id="ambiguous is reported, not refused"),
        pytest.param("return 41", "return 0", 1, id="anchor gone"),
        pytest.param("def answer():", "def answer(:", 1, id="reverted form does not parse"),
    ],
)
def test_only_a_row_that_cannot_run_fails_the_check(tmp_path: Path, old: str, new: str, expected_status: int) -> None:
    """A broken or unparseable row fails the gate; an ambiguous one is only reported."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "revert_matrix.py").write_text(_MATRIX.format(old=old, new=new), encoding="utf-8")
    (tmp_path / "target.py").write_text(_TARGET, encoding="utf-8")

    result = subprocess.run([sys.executable, str(CHECKER)], cwd=tmp_path, capture_output=True, text=True, check=False)

    assert result.returncode == expected_status, result.stdout + result.stderr
