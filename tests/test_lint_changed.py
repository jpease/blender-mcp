"""
Tests for `scripts/lint_changed.py`, the lint gate restricted to a branch's own lines.

A bug here is silent in the worst direction: a gate that maps hunks to the wrong
lines either passes work that added findings, or blames a contributor for the
inherited backlog in a file they only reformatted. Both readings are wrong, and
neither announces itself.
"""

from __future__ import annotations

import importlib.util
import subprocess

from pathlib import Path
from types import ModuleType

import pytest

LINT_CHANGED_PATH = Path(__file__).resolve().parents[1] / "scripts" / "lint_changed.py"

# Stand-in repository root for the attribution tests; ruff emits absolute paths.
OWNED_ROOT = Path("/repo")


def _load_lint_changed() -> ModuleType:
    """
    Load the gate script by path; `scripts/` is deliberately not importable.

    Returns:
        ModuleType: The freshly executed `lint_changed` module.

    """
    spec = importlib.util.spec_from_file_location("lint_changed_under_test", LINT_CHANGED_PATH)
    assert spec is not None and spec.loader is not None, f"{LINT_CHANGED_PATH} is not loadable"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lint_changed = _load_lint_changed()


def _git(repository: Path, *arguments: str) -> str:
    """
    Run git inside a scratch repository.

    Args:
        repository: The repository's working tree.
        arguments: Command arguments following `git`.

    Returns:
        str: Captured stdout.

    """
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    Build a one-commit repository and point the gate at it.

    `diff.mnemonicPrefix` is on because this repository sets it: it replaces the
    `a/`/`b/` diff prefixes with per-source letters, and a parser that assumes
    `+++ b/` then silently sees no changes at all.

    Args:
        tmp_path: pytest's scratch directory.
        monkeypatch: Used to retarget the module's repository root.

    Returns:
        Path: The scratch repository's working tree.

    """
    _git(tmp_path, "init", "--quiet")
    _git(tmp_path, "config", "user.email", "gate@example.test")
    _git(tmp_path, "config", "user.name", "Gate Test")
    _git(tmp_path, "config", "diff.mnemonicPrefix", "true")
    (tmp_path / "kept.py").write_text("one\ntwo\nthree\nfour\nfive\n", encoding="utf-8")
    _git(tmp_path, "add", "kept.py")
    _git(tmp_path, "commit", "--quiet", "-m", "base")
    monkeypatch.setattr(lint_changed, "REPOSITORY_ROOT", tmp_path)
    return tmp_path


def test_only_the_rewritten_line_is_owned(repository: Path) -> None:
    """Editing one line in a file does not make its untouched lines the branch's."""
    (repository / "kept.py").write_text("one\ntwo\nCHANGED\nfour\nfive\n", encoding="utf-8")

    owned = lint_changed._added_lines("HEAD")

    assert owned == {"kept.py": {3}}


def test_an_inserted_run_is_owned_line_by_line(repository: Path) -> None:
    """A multi-line insertion claims exactly the inserted lines, not the hunk's neighbours."""
    (repository / "kept.py").write_text("one\ntwo\nadded-a\nadded-b\nthree\nfour\nfive\n", encoding="utf-8")

    owned = lint_changed._added_lines("HEAD")

    assert owned == {"kept.py": {3, 4}}


def test_an_untracked_file_is_owned_whole(repository: Path) -> None:
    """A file the branch introduces is owned end to end, including its final line."""
    (repository / "fresh.py").write_text("alpha\nbeta\n", encoding="utf-8")

    owned = lint_changed._added_lines("HEAD")

    assert owned == {"fresh.py": {1, 2}}


def test_an_unchanged_tree_owns_nothing(repository: Path) -> None:
    """With no edits the gate has nothing to judge, so it must not invent ownership."""
    assert lint_changed._added_lines("HEAD") == {}


def test_deleted_files_are_not_reported_as_owned(repository: Path) -> None:
    """A removed file has no lines to lint; naming it would make ruff fail on a missing path."""
    (repository / "kept.py").unlink()

    assert lint_changed._added_lines("HEAD") == {}


def test_non_python_changes_are_ignored(repository: Path) -> None:
    """The gate runs ruff, so only Python paths may reach it."""
    (repository / "notes.md").write_text("prose\n", encoding="utf-8")

    assert lint_changed._added_lines("HEAD") == {}


def test_an_untracked_file_with_an_escaped_name_is_owned(repository: Path) -> None:
    """Git quotes a non-ASCII path in `ls-files`; reading it verbatim would not find the file."""
    (repository / "naïve.py").write_text("alpha\n", encoding="utf-8")

    owned = lint_changed._added_lines("HEAD")

    assert owned == {"naïve.py": {1}}


def test_a_single_line_addition_has_no_explicit_count() -> None:
    """Git omits `,count` for a one-line hunk, which must read as one line and not as zero."""
    diff = "diff --git fresh.py fresh.py\n--- /dev/null\n+++ fresh.py\n@@ -0,0 +1 @@\n+alpha\n"

    assert lint_changed.parse_unified_diff(diff) == {"fresh.py": {1}}


def test_a_multi_line_hunk_claims_its_whole_run() -> None:
    """The hunk header's count, not the number of `+` lines, delimits what the branch wrote."""
    diff = "--- kept.py\n+++ kept.py\n@@ -2,0 +3,3 @@ two\n+added-a\n+added-b\n+added-c\n"

    assert lint_changed.parse_unified_diff(diff) == {"kept.py": {3, 4, 5}}


def test_several_hunks_in_one_file_accumulate() -> None:
    """A second hunk must add to the file's owned lines rather than replace the first's."""
    diff = "--- kept.py\n+++ kept.py\n@@ -1 +1 @@\n-one\n+ONE\n@@ -7,0 +8,2 @@ seven\n+added-a\n+added-b\n"

    assert lint_changed.parse_unified_diff(diff) == {"kept.py": {1, 8, 9}}


def test_each_file_in_a_diff_keeps_its_own_lines() -> None:
    """Line numbers are per file; carrying them across a file boundary would blame the wrong lines."""
    diff = (
        "diff --git first.py first.py\n"
        "--- first.py\n"
        "+++ first.py\n"
        "@@ -4 +4 @@\n"
        "-four\n"
        "+FOUR\n"
        "diff --git second.py second.py\n"
        "--- second.py\n"
        "+++ second.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+alpha\n"
        "+beta\n"
    )

    assert lint_changed.parse_unified_diff(diff) == {"first.py": {4}, "second.py": {1, 2}}


def test_a_deletion_only_hunk_owns_nothing() -> None:
    """A `+n,0` hunk added no line; naming the file would report it as changed with nothing to lint."""
    diff = "--- kept.py\n+++ kept.py\n@@ -3 +2,0 @@ two\n-three\n"

    assert lint_changed.parse_unified_diff(diff) == {}


def test_a_path_holding_a_space_drops_its_tab_terminator() -> None:
    """Git terminates a space-bearing path with a literal tab; ruff reports the path without it."""
    diff = "--- /dev/null\n+++ my file.py\t\n@@ -0,0 +1 @@\n+alpha\n"

    assert lint_changed.parse_unified_diff(diff) == {"my file.py": {1}}


def test_a_quoted_path_is_unescaped() -> None:
    """A path git C-style quotes arrives escaped and tab-terminated, and must match ruff's plain name."""
    diff = '--- /dev/null\n+++ "say \\"h\\303\\251\\".py"\t\n@@ -0,0 +1 @@\n+alpha\n'

    assert lint_changed.parse_unified_diff(diff) == {'say "hé".py': {1}}


def _finding(code: str, row: int, end_row: int | None = None, name: str = "mod.py") -> dict[str, object]:
    """
    Build a ruff-shaped finding under the fixed repository root used by these tests.

    Args:
        code: Ruff rule code.
        row: Start row of the diagnostic span.
        end_row: End row of the span; omitted entirely when None.
        name: Repository-relative file name.

    Returns:
        dict[str, object]: A finding as ruff's JSON output shapes it.

    """
    finding: dict[str, object] = {
        "cell": None,
        "code": code,
        "filename": str(OWNED_ROOT / name),
        "fix": None,
        "location": {"column": 1, "row": row},
        "message": "synthetic",
        "noqa_row": row,
        "url": f"https://docs.astral.sh/ruff/rules/{code}",
    }
    if end_row is not None:
        finding["end_location"] = {"column": 1, "row": end_row}
    return finding


def test_a_finding_anchored_above_an_owned_line_is_still_owned() -> None:
    """Ruff reports `I001` at the import block's head, so an import added below it looks untouched."""
    unsorted_imports = {
        "cell": None,
        "code": "I001",
        "end_location": {"column": 1, "row": 5},
        "filename": str(OWNED_ROOT / "mod.py"),
        "fix": {"applicability": "safe", "edits": [], "message": "Organize imports"},
        "location": {"column": 1, "row": 3},
        "message": "Import block is un-sorted or un-formatted",
        "noqa_row": 3,
        "url": "https://docs.astral.sh/ruff/rules/unsorted-imports",
    }

    hits = lint_changed.owned_findings([unsorted_imports], {"mod.py": {4}}, OWNED_ROOT, {})

    assert hits == [unsorted_imports]


def test_a_finding_clear_of_every_owned_line_is_not_reported() -> None:
    """The inherited backlog lives on untouched lines and must stay out of the gate."""
    assert lint_changed.owned_findings([_finding("E501", 10, 10)], {"mod.py": {4, 20}}, OWNED_ROOT, {}) == []


def test_a_finding_without_an_end_location_falls_back_to_its_start_row() -> None:
    """Ruff omits `end_location` for some diagnostics; assuming it is present would crash the gate."""
    hits = lint_changed.owned_findings([_finding("E501", 4)], {"mod.py": {4}}, OWNED_ROOT, {})

    assert hits == [_finding("E501", 4)]


# A function starting at line 7 and running to line 40, as `scope_spans` reports it.
BLOATED_SCOPE = {"mod.py": {7: range(7, 41)}}


def test_a_scope_metric_is_owned_when_the_branch_wrote_inside_the_function() -> None:
    """`PLR0915` sits on the `def`, so statements appended to a body land on no reported line."""
    hits = lint_changed.owned_findings([_finding("PLR0915", 7, 7)], {"mod.py": {30}}, OWNED_ROOT, BLOATED_SCOPE)

    assert hits == [_finding("PLR0915", 7, 7)]


def test_a_scope_metric_on_an_untouched_function_stays_in_the_backlog() -> None:
    """Editing one end of a legacy file must not hand the branch the metrics of functions at the other."""
    hits = lint_changed.owned_findings([_finding("PLR0912", 7, 7)], {"mod.py": {80}}, OWNED_ROOT, BLOATED_SCOPE)

    assert hits == []


def test_a_scope_metric_in_an_untouched_file_stays_in_the_backlog() -> None:
    """Ruff is run over changed files, but a file with no owned lines is nobody's responsibility."""
    finding = _finding("PLR0912", 7, 7, name="other.py")
    hits = lint_changed.owned_findings([finding], {"mod.py": {4}}, OWNED_ROOT, {"other.py": {7: range(7, 41)}})

    assert hits == []


def test_a_scope_metric_in_an_unparseable_file_falls_back_to_its_start_row() -> None:
    """`scope_spans` yields nothing for a file mid-edit, which must narrow the gate, not widen it."""
    unparseable = {"mod.py": {}}

    assert lint_changed.owned_findings([_finding("PLR0915", 7, 7)], {"mod.py": {7}}, OWNED_ROOT, unparseable) == [
        _finding("PLR0915", 7, 7)
    ]
    assert lint_changed.owned_findings([_finding("PLR0915", 7, 7)], {"mod.py": {30}}, OWNED_ROOT, unparseable) == []


def test_a_scope_span_is_keyed_on_the_def_line_not_its_decorator() -> None:
    """Ruff anchors a scope metric on `def`, so a span keyed on the decorator would never be found."""
    source = "import functools\n\n\n@functools.cache\ndef outer(value: int) -> int:\n    return value\n"

    assert lint_changed.scope_spans(source) == {5: range(5, 7)}


def test_scope_spans_covers_nested_and_class_scopes() -> None:
    """Metrics are reported on methods and classes too, each of which needs its own span."""
    source = (
        "class Holder:\n"
        "    def method(self) -> int:\n"
        "        def inner() -> int:\n"
        "            return 1\n"
        "\n"
        "        return inner()\n"
    )

    assert lint_changed.scope_spans(source) == {1: range(1, 7), 2: range(2, 7), 3: range(3, 5)}


def test_scope_spans_of_unparseable_source_is_empty() -> None:
    """A file being edited need not parse; raising here would abort the whole gate run."""
    assert lint_changed.scope_spans("def broken(:\n") == {}
