"""
Regenerate `src/blender_mcp/catalog_sizes.json`, every bundle's committed tool count and catalog bytes.

`manage_toolsets(action="LIST")` reports these figures so an agent can weigh a bundle before
enabling it. Run this after changing any tool's name, signature or description;
`tests/server/test_catalog_sizes.py` fails until you do.

The figures are built by `tests/server/test_catalog_sizes.build_catalog_sizes`, not here, so the
file this writes and the file that test compares against can never be computed two different
ways. That helper measures with `catalog_metrics.payload_report` in a process started with every
bundle, as `scripts/measure_catalog.py all` does.

Usage:
    just catalog-sizes
    python scripts/update_catalog_sizes.py

"""

import sys

from pathlib import Path

from blender_mcp.server.toolsets_runtime import CATALOG_SIZES_PATH

_REPO_ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    """Measure every bundle and write the snapshot."""
    # Necessarily deferred: the test module is not importable until its directory is on the path.
    sys.path.insert(0, str(_REPO_ROOT / "tests" / "server"))
    from test_catalog_sizes import build_catalog_sizes, render_catalog_sizes  # ruff: ignore[import-outside-top-level]

    sizes = build_catalog_sizes()
    CATALOG_SIZES_PATH.write_text(render_catalog_sizes(sizes), encoding="utf-8")
    print(f"wrote {CATALOG_SIZES_PATH.relative_to(_REPO_ROOT)}")
    for bundle, figures in sizes["bundles"].items():
        print(f"  {bundle:<24}{figures['tool_count']:>4} tools {figures['catalog_bytes']:>9,} B")


if __name__ == "__main__":
    main()
