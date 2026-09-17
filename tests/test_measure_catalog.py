"""
Tests for the revision stamp `scripts/measure_catalog.py` prints with each measurement.

`scripts/` is outside `testpaths`, so nothing else runs `_git_revision`.
"""

import subprocess
import sys

from collections.abc import Callable

import pytest

from conftest import REPO_ROOT

# Appended so `scripts/` cannot shadow a stdlib module for the rest of the session.
sys.path.append(str(REPO_ROOT / "scripts"))

import measure_catalog

_Run = Callable[..., subprocess.CompletedProcess[str]]


def _fake_run(*, sha: str, status_out: str, status_code: int = 0) -> _Run:
    """
    Build a `subprocess.run` stand-in answering the two git calls `_git_revision` makes.

    Args:
        sha: Stdout for `git rev-parse`.
        status_out: Stdout for `git status --porcelain`.
        status_code: Return code for `git status --porcelain`.

    Returns:
        The stand-in.

    """

    def run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        """
        Answer `rev-parse` with the canned sha, and anything else as `git status`.

        Args:
            args: The git argv `_git_revision` passed.
            _kwargs: Ignored; mirrors `subprocess.run`'s keyword arguments.

        Returns:
            A `CompletedProcess` carrying the canned stdout and return code.

        """
        if "rev-parse" in args:
            return subprocess.CompletedProcess(args, 0, stdout=sha, stderr="")
        return subprocess.CompletedProcess(args, status_code, stdout=status_out, stderr="")

    return run


def test_clean_tree_reports_a_bare_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    """A clean tree is stamped with the bare revision."""
    monkeypatch.setattr(subprocess, "run", _fake_run(sha="270958a\n", status_out=""))
    assert measure_catalog._git_revision() == "270958a"


def test_dirty_tree_is_marked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Uncommitted changes are stamped, since the number is not the commit's."""
    monkeypatch.setattr(subprocess, "run", _fake_run(sha="270958a\n", status_out=" M src/x.py"))
    assert measure_catalog._git_revision() == "270958a (dirty)"


def test_failed_status_is_not_reported_as_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    """A `git status` that fails must not read the same as a clean tree."""
    monkeypatch.setattr(subprocess, "run", _fake_run(sha="270958a\n", status_out="", status_code=129))
    assert measure_catalog._git_revision() == "270958a (cleanliness unknown)"


def test_missing_git_degrades_instead_of_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    """No git at all yields an explanation, not a traceback in the middle of a report."""

    def explode(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        """
        Fail the way a missing git binary does.

        Args:
            _args: Ignored.
            _kwargs: Ignored.

        Raises:
            OSError: Always, standing in for an absent `git` executable.

        """
        raise OSError("git not found")

    monkeypatch.setattr(subprocess, "run", explode)
    assert "unknown" in measure_catalog._git_revision()
