"""
`src/blender_mcp/catalog_sizes.json` is what each tool bundle costs a client's context.

`manage_toolsets(action="LIST")` reports it per bundle so an agent can weigh a bundle before
enabling it. A tool's schema exists only once its module is imported, and importing registers it,
which is what a bundle left unselected exists to avoid; so the figures are measured here, in a
process started with every bundle, and committed. They move with every tool description, which
is why these tests compare the snapshot against a live measurement: a stale figure misleads an
agent deciding what to enable as surely as a wrong one.

Regenerate with `just catalog-sizes` (`scripts/update_catalog_sizes.py`), which writes the file
from `build_catalog_sizes` below, so the generator and this check are one code path.
"""

import functools
import json
import os
import subprocess
import sys

from blender_mcp.server.bundles import ALL_SENTINEL, TOOLSETS_ENV_VAR
from blender_mcp.server.mount_map import bundle_tool_names
from blender_mcp.server.toolsets_runtime import CATALOG_SIZES_PATH

_REGENERATE = "Run `just catalog-sizes` to regenerate src/blender_mcp/catalog_sizes.json, and commit it."

# Every advertised tool's bytes, measured as `scripts/measure_catalog.py` measures a selection.
# Outside a request `mcp.list_tools` is the process's own selection, which here is every bundle.
_MEASURE_EVERY_TOOL = (
    "import asyncio, json\n"
    "from blender_mcp.server import mcp\n"
    "from blender_mcp.server.catalog_metrics import payload_report\n"
    "print(json.dumps(dict(payload_report(asyncio.run(mcp.list_tools())).per_tool)))\n"
)


@functools.cache
def build_catalog_sizes() -> dict[str, dict[str, dict[str, int]]]:
    """
    Measure every bundle's tool count and advertised bytes in a process started with every bundle.

    Returns:
        dict: `{"bundles": {bundle: {"tool_count": n, "catalog_bytes": b}}}`, `core` included, in
        `bundle_tool_names()` order. Bundles share tools, and each counts every tool it lists.

    """
    env = {**os.environ, TOOLSETS_ENV_VAR: ALL_SENTINEL}
    result = subprocess.run(
        [sys.executable, "-c", _MEASURE_EVERY_TOOL], capture_output=True, text=True, env=env, check=True
    )
    per_tool: dict[str, int] = json.loads(result.stdout)
    return {
        "bundles": {
            bundle: {"tool_count": len(names), "catalog_bytes": sum(per_tool[name] for name in names)}
            for bundle, names in bundle_tool_names().items()
        }
    }


def render_catalog_sizes(sizes: dict[str, dict[str, dict[str, int]]]) -> str:
    """
    Serialize the snapshot exactly as the generator writes it.

    Args:
        sizes: The document `build_catalog_sizes` returns.

    Returns:
        str: Sorted, indented JSON with a trailing newline, so one changed tool moves one line.

    """
    return json.dumps(sizes, indent=2, sort_keys=True) + "\n"


def _committed() -> dict[str, dict[str, dict[str, int]]]:
    return json.loads(CATALOG_SIZES_PATH.read_text(encoding="utf-8"))


def test_committed_catalog_sizes_match_a_process_started_with_every_bundle() -> None:
    """Every bundle's committed figures are what the catalog measures now, and no bundle is missing or extra."""
    committed, live = _committed()["bundles"], build_catalog_sizes()["bundles"]

    drift = [
        f"{bundle}: committed {committed.get(bundle)}, measured {live.get(bundle)}"
        for bundle in sorted(committed.keys() | live.keys())
        if committed.get(bundle) != live.get(bundle)
    ]
    assert not drift, "\n".join(["", *drift, _REGENERATE])


def test_committed_catalog_sizes_are_serialized_the_way_the_generator_writes_them() -> None:
    """A hand-edited snapshot would drift from the generator's output and hide real diffs."""
    assert CATALOG_SIZES_PATH.read_text(encoding="utf-8") == render_catalog_sizes(build_catalog_sizes()), (
        f"src/blender_mcp/catalog_sizes.json is not what the generator would write. {_REGENERATE}"
    )
