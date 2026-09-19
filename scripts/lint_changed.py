#!/usr/bin/env python
"""
Lint only the lines this branch introduces.

The repository inherited a large ruff backlog from upstream, so `ruff check .`
cannot be a hand-off gate yet. This narrows the gate to the lines a branch
actually adds or rewrites: touching a file no longer makes you responsible for
every pre-existing finding in it, but a line you wrote must be clean.

Exit status is 1 when a finding lands on a changed line, 0 otherwise.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

from collections import defaultdict
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent

# `@@ -old,+count +new,+count @@`; the new-side count is absent for single lines.
HUNK_PREFIX = "@@"

# The post-image path line of a `--no-prefix` diff.
NEW_FILE_PREFIX = "+++ "


def _git(*arguments: str) -> str:
    """
    Run a git command in the repository and return its stdout.

    Args:
        arguments: Command arguments following `git`.

    Returns:
        str: Captured stdout, stripped of the trailing newline.

    Raises:
        SystemExit: When git itself fails, carrying git's own message.

    """
    completed = subprocess.run(
        ["git", *arguments],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        sys.exit(f"git {' '.join(arguments)} failed: {completed.stderr.strip()}")
    return completed.stdout.rstrip("\n")


def _merge_base(base: str) -> str:
    """
    Resolve the commit a branch diverged from.

    Args:
        base: Branch or revision the work is measured against.

    Returns:
        str: The merge base, or `base` itself when no common ancestor is found.

    """
    completed = subprocess.run(
        ["git", "merge-base", base, "HEAD"],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else base


def _added_lines(base_commit: str) -> dict[str, set[int]]:
    """
    Map each changed Python file to the line numbers this branch owns.

    Committed work, unstaged edits, and untracked files are all included, so the
    gate reports the same findings before and after a commit.

    Args:
        base_commit: Commit the working tree is compared against.

    Returns:
        dict[str, set[int]]: Repository-relative path to owned line numbers.

    """
    owned: dict[str, set[int]] = defaultdict(set)

    # `--no-prefix` because this repository enables `diff.mnemonicPrefix`, which
    # replaces the usual `a/`/`b/` with per-source letters such as `c/` and `w/`.
    diff = _git("diff", "--unified=0", "--no-prefix", "--diff-filter=ACMR", base_commit, "--", "*.py")
    current: str | None = None
    for line in diff.split("\n"):
        if line.startswith(NEW_FILE_PREFIX):
            current = line.removeprefix(NEW_FILE_PREFIX)
        elif line.startswith(HUNK_PREFIX) and current is not None:
            span = line.split("+", 1)[1].split(" ", 1)[0]
            start_text, _, count_text = span.partition(",")
            start, count = int(start_text), int(count_text or "1")
            owned[current].update(range(start, start + count))

    for path in _git("ls-files", "--others", "--exclude-standard", "--", "*.py").split("\n"):
        if not path:
            continue
        text = (REPOSITORY_ROOT / path).read_text(encoding="utf-8", errors="replace")
        owned[path].update(range(1, len(text.splitlines()) + 1))

    return owned


def _findings(paths: list[str]) -> list[dict]:
    """
    Run ruff over the given paths.

    Args:
        paths: Repository-relative Python file paths.

    Returns:
        list[dict]: Ruff's JSON findings, empty when ruff reports nothing.

    Raises:
        SystemExit: When ruff cannot be executed or emits unparsable output.

    """
    completed = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--force-exclude", "--output-format", "json", *paths],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        return json.loads(completed.stdout or "[]")
    except json.JSONDecodeError:
        sys.exit(f"ruff produced no usable output: {completed.stderr.strip()}")


def main() -> int:
    """
    Report ruff findings that fall on lines this branch introduced.

    Returns:
        int: 1 when any owned line has a finding, otherwise 0.

    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="origin/main", help="revision this branch is measured against")
    arguments = parser.parse_args()

    owned = _added_lines(_merge_base(arguments.base))
    if not owned:
        print(f"no Python changes against {arguments.base}")
        return 0

    hits = [
        finding
        for finding in _findings(sorted(owned))
        if finding["location"]["row"] in owned.get(str(Path(finding["filename"]).relative_to(REPOSITORY_ROOT)), set())
    ]

    scanned = f"{len(owned)} changed file(s), {sum(len(lines) for lines in owned.values())} owned line(s)"
    if not hits:
        print(f"clean: {scanned}")
        return 0

    for finding in sorted(hits, key=lambda item: (item["filename"], item["location"]["row"])):
        path = Path(finding["filename"]).relative_to(REPOSITORY_ROOT)
        location = f"{path}:{finding['location']['row']}:{finding['location']['column']}"
        print(f"{location}: {finding['code']} {finding['message']}")
    print(f"\n{len(hits)} finding(s) on lines this branch owns ({scanned})")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
