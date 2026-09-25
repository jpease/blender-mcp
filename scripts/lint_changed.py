#!/usr/bin/env python
"""
Lint only the lines this branch introduces.

The repository inherited a large ruff backlog from upstream, so `ruff check .`
cannot be a hand-off gate yet. This narrows the gate to the lines a branch
actually adds or rewrites: touching a file no longer makes you responsible for
every pre-existing finding in it, but a line you wrote must be clean.

Attribution is by span, not by start line. Ruff anchors a diagnostic at the
construct's first line, which for a multi-line construct is often a line the
branch never touched: an import added at line 4 makes `I001` surface at line 3,
the import block's head. Any overlap between a finding's
`location`..`end_location` range and the owned lines counts as owned.

Span overlap still cannot see whole-scope metrics (`PLR0915`, `PLR0912`,
`PLR0914`, `C901`, ...): ruff reports those on the `def`/`class` line alone, so
sixty statements appended to an existing function leave the finding on an
untouched line and the gate says nothing. Those codes are attributed to the
enclosing scope instead: the file is parsed with `ast` and the finding is owned
when any line of that `def`'s span is owned. This is exact in both directions --
a function the branch bloated is owned because the branch wrote lines in its
body, while a function the branch never entered stays unowned even in a file it
edited elsewhere. A file `ast` cannot parse yields no spans at all, so its scope
metrics degrade to the plain start-row reading rather than being guessed at.

A whole-scope metric is a measurement, so it is also ratcheted against the base:
one the same function already carried there, at the same or a higher value, is
the inherited backlog rather than something the branch wrote. Renaming a call
inside a legacy 170-statement function therefore stays out of the gate, while
adding a branch, a statement or a local to it raises the value and is owned.
Functions are matched by qualified name, so a moved or renamed function has no
base reading and is owned whole.

Exit status is 1 when a finding lands on a changed line, 0 otherwise.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys

from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent

# `@@ -old,+count +new,+count @@`; the new-side count is absent for single lines.
HUNK_PREFIX = "@@"

# The post-image path line of a `--no-prefix` diff.
NEW_FILE_PREFIX = "+++ "

# Whole-scope metrics, reported on the `def`/`class` line rather than on the
# body that trips them. Attributed by scope span; see the module docstring. Listed
# in full even where the ruff config currently ignores one (`PLR0913`, `PLR0917`) or
# does not select it (`C901`), so enabling it later does not silently reopen the hole.
SCOPE_METRIC_CODES = frozenset(
    {
        "C901",  # complex-structure
        "PLR0904",  # too-many-public-methods
        "PLR0911",  # too-many-return-statements
        "PLR0912",  # too-many-branches
        "PLR0913",  # too-many-arguments
        "PLR0914",  # too-many-locals
        "PLR0915",  # too-many-statements
        "PLR0916",  # too-many-boolean-expressions
        "PLR0917",  # too-many-positional-arguments
    }
)

# Metrics ratcheted against the base: every whole-scope code, plus nesting depth, which
# ruff anchors on the outermost block of the chain rather than the `def` but measures
# the same way (`(6 > 5)`) and belongs to the enclosing function just as much.
RATCHETED_CODES = SCOPE_METRIC_CODES | {"PLR1702"}

# The measured value in a metric's message: `Too many branches (13 > 12)`.
_METRIC_VALUE = re.compile(r"\((\d+) > \d+\)")

# git's C-style escapes, minus `\nnn` octal which is decoded numerically.
_PATH_ESCAPES = {
    "a": "\a",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
    "\\": "\\",
    '"': '"',
}


def _decode_git_path(raw: str) -> str:
    """
    Undo the two ways git obscures an awkward path name in its plumbing output.

    A name holding a space or tab is terminated by a literal tab so the reader
    can find its end; a name holding non-ASCII bytes, a quote or a backslash is
    wrapped in double quotes with C-style escapes. Ruff reports the plain name,
    so neither form would match without this.

    Args:
        raw: Path text as git printed it.

    Returns:
        str: The path git meant.

    """
    path = raw.removesuffix("\t")
    # The closing quote is searched for past the opening one, so a lone `"` is a name, not a wrapper.
    if not (path.startswith('"') and path[1:].endswith('"')):
        return path

    # Escapes encode bytes, not characters, so a multi-byte character arrives as
    # several `\nnn` groups and can only be decoded once the run is reassembled.
    body = path[1:-1]
    decoded = bytearray()
    index = 0
    while index < len(body):
        character = body[index]
        if character != "\\" or index + 1 == len(body):
            decoded.extend(character.encode("utf-8"))
            index += 1
        elif body[index + 1] in "01234567":
            decoded.append(int(body[index + 1 : index + 4], 8))
            index += 4
        else:
            decoded.extend(_PATH_ESCAPES.get(body[index + 1], body[index + 1]).encode("utf-8"))
            index += 2
    return decoded.decode("utf-8", errors="replace")


def parse_unified_diff(diff: str) -> dict[str, set[int]]:
    """
    Read the post-image line numbers out of a `--unified=0 --no-prefix` diff.

    Args:
        diff: Raw `git diff` text.

    Returns:
        dict[str, set[int]]: Repository-relative path to the lines the diff adds
        or rewrites. Files whose only hunks delete lines are absent, so every
        value is a non-empty set.

    """
    owned: dict[str, set[int]] = defaultdict(set)
    current: str | None = None
    for line in diff.split("\n"):
        if line.startswith(NEW_FILE_PREFIX):
            current = _decode_git_path(line.removeprefix(NEW_FILE_PREFIX))
        elif line.startswith(HUNK_PREFIX) and current is not None:
            span = line.split("+", 1)[1].split(" ", 1)[0]
            start_text, _, count_text = span.partition(",")
            start, count = int(start_text), int(count_text or "1")
            # A pure deletion has count 0; recording the key would hand ruff a
            # file with nothing owned in it.
            if count:
                owned[current].update(range(start, start + count))
    return dict(owned)


def scope_spans(source: str) -> dict[int, range]:
    """
    Map each `def`/`class` line in a module to the lines that definition covers.

    The key is the `def`/`class` line rather than a decorator, because that is
    where ruff anchors a whole-scope metric.

    Args:
        source: Python module text.

    Returns:
        dict[int, range]: Definition line to the definition's full line span,
        empty when `source` does not parse.

    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        # A branch mid-edit, or a file written for a newer grammar than this
        # interpreter. Callers fall back to start-row attribution.
        return {}

    spans: dict[int, range] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            # `end_lineno` is only None on a node built by hand, never by `parse`.
            spans[node.lineno] = range(node.lineno, (node.end_lineno or node.lineno) + 1)
    return spans


def scope_qualnames(source: str) -> list[tuple[range, str]]:
    """
    List every `def`/`class` in a module with its line span and qualified name.

    Args:
        source: Python module text.

    Returns:
        list[tuple[range, str]]: Span and dotted qualified name (`Class.method`)
        per definition, empty when `source` does not parse.

    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    named: list[tuple[range, str]] = []

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                qualname = f"{prefix}{child.name}"
                named.append((range(child.lineno, (child.end_lineno or child.lineno) + 1), qualname))
                visit(child, f"{qualname}.")
            else:
                visit(child, prefix)

    visit(tree, "")
    return named


def metric_key(finding: Mapping[str, Any], qualnames: Sequence[tuple[range, str]]) -> tuple[str, str] | None:
    """
    Identify a ratcheted metric by its code and the innermost definition holding it.

    Args:
        finding: One ruff JSON finding.
        qualnames: `scope_qualnames` of the finding's file.

    Returns:
        tuple[str, str] | None: `(code, qualname)`, or None for a code that is
        not ratcheted or a finding outside every definition.

    """
    if finding.get("code") not in RATCHETED_CODES:
        return None
    row: int = finding["location"]["row"]
    holding = [(len(span), name) for span, name in qualnames if row in span]
    if not holding:
        return None
    return str(finding["code"]), min(holding)[1]


def metric_value(finding: Mapping[str, Any]) -> int | None:
    """
    Read the measured value out of a metric finding's message.

    Args:
        finding: One ruff JSON finding.

    Returns:
        int | None: The value left of `>`, or None when the message carries none.

    """
    matched = _METRIC_VALUE.search(str(finding.get("message", "")))
    return int(matched.group(1)) if matched else None


def base_metrics(
    findings: Sequence[Mapping[str, Any]], qualnames: Sequence[tuple[range, str]]
) -> dict[tuple[str, str], int]:
    """
    Map each ratcheted metric one file carried at the base to its highest value.

    Args:
        findings: Ruff JSON findings for the base version of the file.
        qualnames: `scope_qualnames` of that base version.

    Returns:
        dict[tuple[str, str], int]: `(code, qualname)` to the largest value measured.

    """
    measured: dict[tuple[str, str], int] = {}
    for finding in findings:
        key = metric_key(finding, qualnames)
        value = metric_value(finding)
        if key is not None and value is not None:
            measured[key] = max(value, measured.get(key, value))
    return measured


def inherited(
    finding: Mapping[str, Any],
    qualnames: Sequence[tuple[range, str]],
    base: Mapping[tuple[str, str], int],
) -> bool:
    """
    Say whether a metric finding only repeats what the same function measured at the base.

    Args:
        finding: One owned ruff JSON finding in the branch's version of a file.
        qualnames: `scope_qualnames` of the branch's version of that file.
        base: `base_metrics` of the base version of that file.

    Returns:
        bool: True when the base carried the same code on the same function at a
        value no lower than this one; False for anything the branch made worse,
        introduced, or that cannot be measured.

    """
    key = metric_key(finding, qualnames)
    value = metric_value(finding)
    if key is None or value is None or key not in base:
        return False
    return value <= base[key]


def owned_findings(
    findings: Sequence[Mapping[str, Any]],
    owned: Mapping[str, set[int]],
    repository_root: Path,
    scopes: Mapping[str, Mapping[int, range]],
) -> list[dict[str, Any]]:
    """
    Keep the findings this branch is answerable for.

    A finding counts when any line of its span is owned. For a whole-scope metric
    the span is widened to the whole definition it was reported on; the module
    docstring explains why.

    Args:
        findings: Ruff JSON findings, each with an absolute `filename`.
        owned: Repository-relative path to the line numbers the branch owns.
        repository_root: Root the findings' absolute paths are relative to.
        scopes: Repository-relative path to that file's `scope_spans` result.
            A path absent here, or a file that did not parse, leaves its scope
            metrics on the start-row reading.

    Returns:
        list[dict[str, Any]]: The subset of `findings` the branch owns, in order.

    """
    hits: list[dict[str, Any]] = []
    for finding in findings:
        path = str(Path(finding["filename"]).relative_to(repository_root))
        lines = owned.get(path)
        if not lines:
            continue
        start: int = finding["location"]["row"]
        # `end_location` is absent on a finding ruff cannot bound, and never
        # precedes the start, so a single-line span is the safe reading.
        end = max(start, (finding.get("end_location") or {}).get("row", start))
        span = range(start, end + 1)
        if finding.get("code") in SCOPE_METRIC_CODES:
            file_scopes = scopes.get(path)
            if file_scopes is not None:
                span = file_scopes.get(start, span)
        if not lines.isdisjoint(span):
            hits.append(dict(finding))
    return hits


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
    # `--no-prefix` because this repository enables `diff.mnemonicPrefix`, which
    # replaces the usual `a/`/`b/` with per-source letters such as `c/` and `w/`.
    owned = parse_unified_diff(
        _git("diff", "--unified=0", "--no-prefix", "--diff-filter=ACMR", base_commit, "--", "*.py")
    )

    for listed in _git("ls-files", "--others", "--exclude-standard", "--", "*.py").split("\n"):
        if not listed:
            continue
        path = _decode_git_path(listed)
        text = (REPOSITORY_ROOT / path).read_text(encoding="utf-8", errors="replace")
        length = len(text.splitlines())
        if length:
            owned.setdefault(path, set()).update(range(1, length + 1))

    return owned


def _findings(paths: list[str]) -> list[dict[str, Any]]:
    """
    Run ruff over the given paths.

    Args:
        paths: Repository-relative Python file paths.

    Returns:
        list[dict[str, Any]]: Ruff's JSON findings, empty when ruff reports nothing.

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


def _scopes(findings: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[int, range]]:
    """
    Read and parse only the files that actually carry a whole-scope metric.

    Args:
        findings: Ruff JSON findings, each with an absolute `filename`.

    Returns:
        dict[str, Mapping[int, range]]: Repository-relative path to its
        `scope_spans` result, for the files needing scope attribution.

    """
    named = {finding["filename"] for finding in findings if finding.get("code") in SCOPE_METRIC_CODES}
    return {
        str(Path(name).relative_to(REPOSITORY_ROOT)): scope_spans(
            Path(name).read_text(encoding="utf-8", errors="replace")
        )
        for name in named
    }


def _base_source(base_commit: str, path: str) -> str | None:
    """
    Read one file as it stood at the base.

    Args:
        base_commit: Commit the working tree is compared against.
        path: Repository-relative path.

    Returns:
        str | None: The file's text at `base_commit`, or None when it did not exist there.

    """
    completed = subprocess.run(
        ["git", "show", f"{base_commit}:{path}"],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout if completed.returncode == 0 else None


def _findings_in_source(path: str, source: str) -> list[dict[str, Any]]:
    """
    Run ruff over text that is not on disk, under the path whose config it takes.

    Args:
        path: Repository-relative path the text belongs to.
        source: Python module text.

    Returns:
        list[dict[str, Any]]: Ruff's JSON findings for `source`.

    Raises:
        SystemExit: When ruff emits unparsable output.

    """
    completed = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--output-format", "json", "--stdin-filename", path, "-"],
        cwd=REPOSITORY_ROOT,
        input=source,
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        return json.loads(completed.stdout or "[]")
    except json.JSONDecodeError:
        sys.exit(f"ruff produced no usable output for {path} at the base: {completed.stderr.strip()}")


def _without_inherited_metrics(hits: Sequence[Mapping[str, Any]], base_commit: str) -> list[dict[str, Any]]:
    """
    Drop the ratcheted metrics a function already carried at the base at the same or a higher value.

    Args:
        hits: Owned findings, each with an absolute `filename`.
        base_commit: Commit the working tree is compared against.

    Returns:
        list[dict[str, Any]]: `hits` minus the inherited metrics, in order.

    """
    measured = {
        str(Path(finding["filename"]).relative_to(REPOSITORY_ROOT))
        for finding in hits
        if finding.get("code") in RATCHETED_CODES
    }
    head_names: dict[str, list[tuple[range, str]]] = {}
    base: dict[str, dict[tuple[str, str], int]] = {}
    for path in measured:
        head_names[path] = scope_qualnames((REPOSITORY_ROOT / path).read_text(encoding="utf-8", errors="replace"))
        source = _base_source(base_commit, path)
        if source is not None:
            base[path] = base_metrics(_findings_in_source(path, source), scope_qualnames(source))

    kept: list[dict[str, Any]] = []
    for finding in hits:
        path = str(Path(finding["filename"]).relative_to(REPOSITORY_ROOT))
        if path in base and inherited(finding, head_names[path], base[path]):
            continue
        kept.append(dict(finding))
    return kept


def main() -> int:
    """
    Report ruff findings that fall on lines this branch introduced.

    Returns:
        int: 1 when any owned line has a finding, otherwise 0.

    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="origin/main", help="revision this branch is measured against")
    arguments = parser.parse_args()

    base_commit = _merge_base(arguments.base)
    owned = _added_lines(base_commit)
    if not owned:
        print(f"no Python changes against {arguments.base}")
        return 0

    findings = _findings(sorted(owned))
    hits = _without_inherited_metrics(owned_findings(findings, owned, REPOSITORY_ROOT, _scopes(findings)), base_commit)

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
