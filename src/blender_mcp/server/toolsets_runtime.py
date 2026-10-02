"""
Register tool bundles on demand, and report what each one costs a client's context.

A process registers its `BLENDER_MCP_TOOLSETS` selection when `tools` is imported, and nothing
else: importing a bundle's modules is what registers its tools, and an unselected bundle stays
unimported (`bundles.py` says why). `manage_toolsets` then lets one client session add a bundle
without a restart. Its tools are registered process-wide the first time any session asks, and
`BlenderFastMCP` shows them only to the sessions that enabled them.

Both passes that follow a registration - argument hardening and the documentation pass - run over
exactly the tools that registration added. The documentation pass is not reversible, and two
bundles can share a module (`lighting`, `lighting-construction`), so a tool must be documented
once however many requests name it.

The import runs on the event loop. That is deliberate: importing on a worker thread would mutate
the tool registry while the loop iterates it for another session's `tools/list`. It happens once
per bundle per process.

What a bundle costs is read from `catalog_sizes.json`, committed beside the package: a tool's
schema exists only once its module is imported, so an unselected bundle cannot be measured here
without registering it. `tests/server/test_catalog_sizes.py` keeps the snapshot current.
"""

import functools
import importlib
import json
import os

from collections.abc import Iterable, Mapping
from pathlib import Path
from types import MappingProxyType

from .app import mcp
from .bundles import BUNDLES, CORE_MODULES, TOOLSETS_ENV_VAR, resolve_toolset_bundles
from .tools._documentation import finalize_tool_documentation
from .tools._strict_args import harden_tool_arguments

_TOOLS_PACKAGE = f"{__package__}.tools"

# The bundles this process was started with: what a session lists until it enables or disables one.
STARTUP_BUNDLES: tuple[str, ...] = resolve_toolset_bundles(os.getenv(TOOLSETS_ENV_VAR))

# Every bundle's tool count and advertised bytes, measured in a process started with every bundle.
# Shipped as package data (pyproject.toml); `just catalog-sizes` rewrites it.
CATALOG_SIZES_PATH = Path(__file__).resolve().parents[1] / "catalog_sizes.json"


def register_tool_modules(modules: Iterable[str]) -> frozenset[str]:
    """
    Import tool modules, then harden and document exactly the tools that import registered.

    Args:
        modules: Module entries from `CORE_MODULES` or `BUNDLES`; one already imported adds nothing.

    Returns:
        frozenset[str]: The tool names this call registered.

    """
    registry = mcp._tool_manager._tools
    before = set(registry)
    for module in dict.fromkeys(modules):
        importlib.import_module(f".{module}", package=_TOOLS_PACKAGE)
    added = frozenset(registry.keys() - before)
    # The SDK generates each tool's top-level argument model with pydantic's defaults, `extra="ignore"`
    # and `allow_inf_nan=True`, so an unknown key would be dropped and a NaN coordinate forwarded to
    # Blender. Harden the tools just registered; see `tools/_strict_args.py` for why.
    harden_tool_arguments(mcp, added)
    finalize_tool_documentation(mcp, added)
    return added


def register_startup_selection() -> frozenset[str]:
    """
    Register core and the bundles `BLENDER_MCP_TOOLSETS` selected, as the process starts.

    Returns:
        frozenset[str]: The tool names registered.

    """
    return register_tool_modules(CORE_MODULES + tuple(module for name in STARTUP_BUNDLES for module in BUNDLES[name]))


def ensure_bundles_registered(bundles: Iterable[str]) -> frozenset[str]:
    """
    Register the bundles a session asked for, importing any module no earlier request imported.

    Args:
        bundles: Bundle names, already validated against `BUNDLES`.

    Returns:
        frozenset[str]: The tool names this call registered; empty when every module was imported.

    """
    added = register_tool_modules(module for name in bundles for module in BUNDLES[name])
    mcp.record_registered_on_demand(added)
    return added


def resolve_requested_bundles(names: Iterable[str]) -> tuple[str, ...]:
    """
    Resolve the names a `manage_toolsets` call gave, in the `BLENDER_MCP_TOOLSETS` vocabulary.

    Args:
        names: Mode names, bundle names, or `all`.

    Returns:
        tuple[str, ...]: Bundle names, modes expanded, in first-seen order.

    Raises:
        ValueError: If a name is not a mode, a bundle or `all`; the message lists the valid ones.

    """
    requested = [name.strip() for name in names]
    if any("," in name for name in requested):
        raise ValueError("name one toolset per list item, without commas")
    return resolve_toolset_bundles(",".join(requested))


@functools.cache
def bundle_catalog() -> Mapping[str, Mapping[str, int]]:
    """
    Report each bundle's tool count and the bytes its tools add to `tools/list`.

    Read once per process from the committed snapshot, the figures `scripts/measure_catalog.py`
    would report for a process started with every bundle.

    Returns:
        Mapping[str, Mapping[str, int]]: Bundle name, `core` included, to `tool_count` and
        `catalog_bytes`.

    """
    bundles = json.loads(CATALOG_SIZES_PATH.read_text(encoding="utf-8"))["bundles"]
    return MappingProxyType({name: MappingProxyType(sizes) for name, sizes in bundles.items()})
