"""
Print the advertised `tools/list` payload for a given BLENDER_MCP_TOOLSETS value.

Usage:
    python scripts/measure_catalog.py            # core only (the default process)
    python scripts/measure_catalog.py all        # every bundle
    python scripts/measure_catalog.py camera,rendering

"""

import asyncio
import os
import sys

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# BLENDER_MCP_TOOLSETS must be set before `blender_mcp` is imported: importing the package
# eagerly imports `.server`, which eagerly imports `.server.tools`, which reads this
# environment variable at import time to decide which tool bundles to register. Importing
# `blender_mcp` first and setting the variable afterwards silently measures core-only for
# every selection.
os.environ["BLENDER_MCP_TOOLSETS"] = sys.argv[1] if len(sys.argv) > 1 else ""

import blender_mcp

_EXPECTED_ROOT = Path(__file__).resolve().parents[1] / "src"
if not Path(blender_mcp.__file__).is_relative_to(_EXPECTED_ROOT):
    raise SystemExit(f"refusing to measure: imported {blender_mcp.__file__}, expected under {_EXPECTED_ROOT}")

from blender_mcp.server.app import mcp  # ruff: ignore[module-import-not-at-top-of-file]
from blender_mcp.server.catalog_metrics import payload_report  # ruff: ignore[module-import-not-at-top-of-file]


def main() -> None:
    """
    Print a payload report for the tool selection named on the command line.

    Reads the bundle selection from `sys.argv[1]` (already applied to
    `BLENDER_MCP_TOOLSETS` above), lists the tools FastMCP would advertise for that
    selection, and prints total size, a schema/description split, and the ten heaviest
    tools by wire bytes.

    """
    report = payload_report(asyncio.run(mcp.list_tools()))
    selection = os.environ["BLENDER_MCP_TOOLSETS"] or "(core only)"
    print(f"selection : {selection}")
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
