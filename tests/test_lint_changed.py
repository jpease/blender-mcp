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
