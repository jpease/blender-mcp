"""
Measure the advertised `tools/list` payload for a given BLENDER_MCP_TOOLSETS value.

`bundles.py` explains why this size is a per-turn context cost for a client.

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

    The package registers tools when imported, reading BLENDER_MCP_TOOLSETS then, so the
    variable must be set first; otherwise every selection measures as core only. Done in a
    function so importing this module changes neither `sys.path` nor the environment.

    Args:
        selection: The BLENDER_MCP_TOOLSETS value to measure; `""` means core only.

    Returns:
        The payload report for the tools that selection advertises.

    A misspelled bundle name or two tools sharing a name raise ValueError from the import
    or from `payload_report`.

    Raises:
        SystemExit: If called twice in one process, or if `blender_mcp` resolves to an
            installed copy instead of this repo.

    """
    # The selection is read once, at import, so a second call would report the first
    # selection's numbers under the second one's name.
    if "blender_mcp" in sys.modules:
        raise SystemExit("refusing to measure: blender_mcp is already imported; run one selection per process")

    sys.path.insert(0, str(_SRC_ROOT))
    # Not imported from `bundles.TOOLSETS_ENV_VAR`: that import would load `blender_mcp`,
    # which reads the variable, before it is set.
    os.environ["BLENDER_MCP_TOOLSETS"] = selection

    import blender_mcp  # ruff: ignore[import-outside-top-level]

    if not Path(blender_mcp.__file__).is_relative_to(_SRC_ROOT):
        raise SystemExit(f"refusing to measure: imported {blender_mcp.__file__}, expected under {_SRC_ROOT}")

    from blender_mcp.server.app import mcp  # ruff: ignore[import-outside-top-level]
    from blender_mcp.server.catalog_metrics import payload_report  # ruff: ignore[import-outside-top-level]

    return payload_report(asyncio.run(mcp.list_tools()))


def _git_revision() -> str:
    """
    Identify the git revision this measurement was taken against, so it can be reproduced.

    Returns:
        A short revision like `270958a`, suffixed ` (dirty)` for uncommitted changes or
        ` (cleanliness unknown)` if `git status` fails. Without git or a repository, a
        message saying so instead of raising.

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

    Prints the revision, total size, the schema/description split, and the ten heaviest
    tools by wire bytes.

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
