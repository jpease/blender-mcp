"""
Which bundle would mount a tool, for a tool this process did not register.

A client sees only the tools its process mounted (`bundles.py`), so an absent tool is
indistinguishable from one the project never implemented -- the two call for opposite
responses, and a rehearsal lost a scene to that ambiguity by concluding a shipped
`create_dolly_camera_rig` did not exist. `get_addon_status` answers it from here.

Names are read by parsing each tool module's source, never by importing it: importing a
module registers its tools, which is the exact thing a bundle selection exists to avoid.
`tests/server/test_bundles.py` checks the parse against real registration, so the shortcut
cannot drift away from what a process actually mounts.
"""

import ast
import functools

from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

from .bundles import BUNDLES, CORE_MODULES

# The bundle name reported for a tool in `CORE_MODULES`. Not a key of `BUNDLES`: core is
# mounted unconditionally, so it is never something to add to the environment variable.
CORE_BUNDLE = "core"

_TOOLS_DIR = Path(__file__).parent / "tools"


def _is_mcp_tool(node: ast.AsyncFunctionDef | ast.FunctionDef) -> bool:
    """
    Decide whether a module-level function is registered as an MCP tool.

    Matches both spellings the tool modules use, `@mcp.tool()` and a bare `@mcp.tool`.

    Args:
        node: A module-level function definition.

    Returns:
        Whether one of its decorators is `mcp.tool`.

    """
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Attribute) and target.attr == "tool":
            return True
    return False


def _module_source_files(module_name: str) -> tuple[Path, ...]:
    """
    Locate the source a bundle's module entry registers tools from.

    A dotted entry names one file. A bare entry that is a package names every public
    submodule, because such a package star-imports them all; the packages that resolve
    submodules lazily are only ever named dotted (see `bundles.py`), and
    `tests/server/test_bundles.py` fails if that stops being true.

    Args:
        module_name: A module entry from `BUNDLES` or `CORE_MODULES`.

    Returns:
        The files to parse, in a stable order.

    """
    base = _TOOLS_DIR.joinpath(*module_name.split("."))
    single_file = base.with_suffix(".py")
    if single_file.is_file():
        return (single_file,)
    return tuple(sorted(path for path in base.glob("*.py") if not path.name.startswith("_")))


@functools.cache
def _module_tool_names(module_name: str) -> frozenset[str]:
    """
    Read the tool names one module entry registers, by parsing rather than importing.

    Args:
        module_name: A module entry from `BUNDLES` or `CORE_MODULES`.

    Returns:
        Every `@mcp.tool()` function name it defines at module scope.

    """
    names: set[str] = set()
    for path in _module_source_files(module_name):
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and _is_mcp_tool(node):
                names.add(node.name)
    return frozenset(names)


@functools.cache
def bundle_tool_names() -> Mapping[str, frozenset[str]]:
    """
    Map every bundle, plus `core`, to the tool names it mounts.

    Bundles overlap: `lighting` and `lighting-construction` share a module, so a name can
    appear under both. That is the truth a caller needs -- either bundle mounts it.

    Returns:
        Bundle name to tool names. Cached; the sources cannot change while a process runs.

    """
    mapping = {CORE_BUNDLE: frozenset().union(*(_module_tool_names(name) for name in CORE_MODULES))}
    for bundle, modules in BUNDLES.items():
        mapping[bundle] = frozenset().union(*(_module_tool_names(name) for name in modules))
    return MappingProxyType(mapping)


@functools.cache
def known_tool_names() -> frozenset[str]:
    """
    Every tool name this build can register, under any selection.

    Returns:
        The union of every bundle's tools and core's.

    """
    return frozenset().union(*bundle_tool_names().values())


def bundles_providing(tool_name: str) -> tuple[str, ...]:
    """
    Name the bundles that would mount one tool.

    Args:
        tool_name: The tool asked about.

    Returns:
        Bundle names, sorted, or empty if this build has no such tool. `core` here means the
        tool is mounted by every process, so its absence is a fault rather than a selection.

    """
    return tuple(sorted(bundle for bundle, names in bundle_tool_names().items() if tool_name in names))


def unmounted_bundle_counts(mounted: frozenset[str]) -> dict[str, int]:
    """
    Count, per bundle, the tools this process left unmounted.

    Derived from the mounted names rather than from the requested bundle names, so a bundle
    whose modules another selection already pulled in is not reported as missing.

    Args:
        mounted: The tool names actually registered in this process.

    Returns:
        Bundle name to how many of its tools are absent, for bundles missing at least one,
        ordered as `BUNDLES` declares them.

    """
    counts = {}
    for bundle in BUNDLES:
        absent = len(bundle_tool_names()[bundle] - mounted)
        if absent:
            counts[bundle] = absent
    return counts
