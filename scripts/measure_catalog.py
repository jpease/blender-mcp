"""
Measure the advertised `tools/list` payload for a given BLENDER_MCP_TOOLSETS value.

The measurement harness for the catalog-reduction work. See `bundles.py` for why this
number is a bundle selection's permanent context cost rather than a startup cost.

Usage:
    python scripts/measure_catalog.py            # core only (the default process)
    python scripts/measure_catalog.py all        # every bundle
    python scripts/measure_catalog.py camera,rendering

"""

import asyncio
import os
import subprocess
import sys

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from blender_mcp.server.catalog_metrics import PayloadReport

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC_ROOT = _REPO_ROOT / "src"


def _measure(selection: str) -> "PayloadReport":
    """
    Import this repo's `blender_mcp` with the given bundle selection and measure its catalog.

    BLENDER_MCP_TOOLSETS must be set *before* `blender_mcp` is imported: importing the
    package eagerly imports `.server`, which eagerly imports `.server.tools`, which reads
    this environment variable at import time to decide which tool bundles to register.
    Importing first and setting the variable afterwards silently measures core-only for
    every selection. Doing this inside a function rather than at module scope keeps the
    module import-safe, so importing it never mutates `sys.path` or the environment and
    never parses a caller's `sys.argv`.

    Args:
        selection: The BLENDER_MCP_TOOLSETS value to measure; `""` means core only.

    Returns:
        The payload report for the tools that selection advertises.

    Two ValueErrors propagate rather than originate here: a misspelled bundle name raises
    from `resolve_toolset_modules` during `import blender_mcp` (the selection is validated
    at import time), and two tools sharing a name raise from `payload_report`.

    Raises:
        SystemExit: If called twice in one process, or if `blender_mcp` resolves to an
            installed copy rather than this repo - a byte count measured against the wrong
            source is worse than no number.

    """
    # A second call in one process cannot work: `blender_mcp` reads the selection once, at
    # import time, so re-importing is a no-op and this would confidently report the first
    # selection's numbers under the second selection's name. Refuse rather than mislead.
    if "blender_mcp" in sys.modules:
        raise SystemExit("refusing to measure: blender_mcp is already imported; run one selection per process")

    sys.path.insert(0, str(_SRC_ROOT))
    # Spelled literally, not imported from `bundles.TOOLSETS_ENV_VAR`: importing that constant
    # would import `blender_mcp`, which reads this variable at import time -- the very thing
    # that has to happen after this assignment.
    os.environ["BLENDER_MCP_TOOLSETS"] = selection

    import blender_mcp  # ruff: ignore[import-outside-top-level]

    if not Path(blender_mcp.__file__).is_relative_to(_SRC_ROOT):
        raise SystemExit(f"refusing to measure: imported {blender_mcp.__file__}, expected under {_SRC_ROOT}")

    from blender_mcp.server.app import mcp  # ruff: ignore[import-outside-top-level]
    from blender_mcp.server.catalog_metrics import payload_report  # ruff: ignore[import-outside-top-level]

    return payload_report(asyncio.run(mcp.list_tools()))


def _git_revision() -> str:
    """
    Identify the git revision this measurement was taken against.

    A byte count without its revision is not reproducible: this project has already
    published wrong numbers by measuring the right checkout at the wrong commit. Marks an
    unclean working tree as dirty, since a number measured against uncommitted changes is
    not the same fact as one measured at a clean commit.

    Returns:
        A short revision like `270958a` or `270958a (dirty)`, or a message explaining why
        no revision could be determined (git missing, or not a repository), rather than
        raising. If `git status` fails after `rev-parse` succeeded, reports
        `270958a (cleanliness unknown)` rather than silently claiming a clean tree.

    """
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown (git unavailable or not a repository)"

    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if status.returncode != 0:
        return f"{sha} (cleanliness unknown)"
    return f"{sha} (dirty)" if status.stdout.strip() else sha


def main() -> None:
    """
    Print a payload report for the tool selection named on the command line.

    Reads the bundle selection from `sys.argv[1]`, imports the server with that selection
    applied, lists the tools FastMCP would advertise, and prints the measured revision,
    total size, a schema/description split, and the ten heaviest tools by wire bytes.
    Propagates `_measure`'s ValueError for a misspelled bundle name, and `payload_report`'s
    if two tools share a name.

    """
    selection = sys.argv[1] if len(sys.argv) > 1 else ""
    report = _measure(selection)
    print(f"revision  : {_git_revision()}")
    print(f"selection : {selection or '(core only)'}")
    print(f"tools     : {report.tool_count}")
    print(f"bytes     : {report.total_bytes:,}")
    print(f"tokens    : ~{report.total_tokens:,.0f}")
    print(f"  schemas : {report.schema_bytes:,} ({100 * report.schema_bytes // max(report.total_bytes, 1)}%)")
    print(f"  descrs  : {report.description_bytes:,}")
    print("\nheaviest tools:")
    for name, size in sorted(report.per_tool.items(), key=lambda kv: -kv[1])[:10]:
        print(f"  {name:<40}{size:>8,} B")


if __name__ == "__main__":
    main()
