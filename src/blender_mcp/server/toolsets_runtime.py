"""
Register tool bundles on demand, and measure what each one costs a client's context.

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
"""

import importlib
import json
import os
import subprocess
import sys
import threading

from collections.abc import Iterable, Sequence

from mcp.types import Tool as MCPTool

from .app import mcp
from .bundles import ALL_SENTINEL, BUNDLES, CORE_MODULES, TOOLSETS_ENV_VAR, resolve_toolset_bundles
from .catalog_metrics import payload_report
from .mount_map import bundle_tool_names
from .tools._documentation import finalize_tool_documentation
from .tools._strict_args import harden_tool_arguments

_TOOLS_PACKAGE = f"{__package__}.tools"

# The bundles this process was started with: what a session lists until it enables or disables one.
STARTUP_BUNDLES: tuple[str, ...] = resolve_toolset_bundles(os.getenv(TOOLSETS_ENV_VAR))

# Measures every tool's advertised bytes in a process started with every bundle. `mcp.list_tools`
# outside a request is the process's own selection, which there is everything.
_MEASURE_EVERY_TOOL = (
    "import asyncio, json\n"
    "from blender_mcp.server import mcp\n"
    "from blender_mcp.server.catalog_metrics import payload_report\n"
    "print(json.dumps(dict(payload_report(asyncio.run(mcp.list_tools())).per_tool)))\n"
)
_MEASURE_TIMEOUT_SECONDS = 60

# Tool name to its advertised `tools/list` bytes, filled once per name and never invalidated: a
# tool's advertisement is fixed once its registration has been documented.
_tool_bytes: dict[str, int] = {}
_measure_lock = threading.Lock()


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


def _measure_unregistered_tools() -> dict[str, int]:
    """
    Measure every tool's advertised bytes in a process started with every bundle.

    A tool's schema exists only once its module is imported, and importing here would register it.

    Returns:
        dict[str, int]: Tool name to bytes.

    Raises:
        RuntimeError: If the measuring process fails or times out.

    """
    env = {**os.environ, TOOLSETS_ENV_VAR: ALL_SENTINEL}
    try:
        result = subprocess.run(
            [sys.executable, "-c", _MEASURE_EVERY_TOOL],
            capture_output=True,
            text=True,
            env=env,
            timeout=_MEASURE_TIMEOUT_SECONDS,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"could not measure the unregistered bundles' catalog bytes: {exc}") from exc
    return {str(name): int(size) for name, size in json.loads(result.stdout).items()}


def bundle_catalog(registered: Sequence[MCPTool]) -> dict[str, dict[str, int]]:
    """
    Report each bundle's tool count and the bytes its tools add to `tools/list`.

    Measured once per tool per process, with `catalog_metrics.payload_report` as
    `scripts/measure_catalog.py` measures: from the registered schemas where the tool is
    registered, otherwise in one child process started with every bundle, the first time any
    tool is unmeasured. Blocks for that child, so call it off the event loop.

    Args:
        registered: Every registered tool's `tools/list` entry, unfiltered by session.

    Returns:
        dict[str, dict[str, int]]: Bundle name, `core` first, to `tool_count` and `catalog_bytes`.

    """
    by_bundle = bundle_tool_names()
    with _measure_lock:
        _tool_bytes.update(payload_report([tool for tool in registered if tool.name not in _tool_bytes]).per_tool)
        if any(name not in _tool_bytes for names in by_bundle.values() for name in names):
            measured = _measure_unregistered_tools()
            _tool_bytes.update({name: size for name, size in measured.items() if name not in _tool_bytes})
    return {
        bundle: {"tool_count": len(names), "catalog_bytes": sum(_tool_bytes[name] for name in names)}
        for bundle, names in by_bundle.items()
    }
