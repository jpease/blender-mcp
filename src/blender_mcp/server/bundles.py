"""
Tool bundle definitions used to select which domain modules a server process registers.

Every MCP client connection receives the full `tools/list` response for whatever is
registered on the process's `FastMCP` app, and the client then carries those definitions in
the model's context on every turn -- so an advertised tool is permanent context occupancy,
not a one-time startup cost. This module is the single statement of that cost model; other
modules point here rather than restate it. With every bundle registered (285 tools, per
`scripts/measure_catalog.py all`), that response alone can blow out a client's context
before any real work starts. `BLENDER_MCP_TOOLSETS` lets a client config select a subset of
domains per process instead.

A module belongs in `CORE_MODULES` only if essentially every workflow needs it, or if a
client cannot discover the scene without it. Everything else is a bundle. `CORE_MODULES` is
pre-split residue and has not been re-derived against that test: the seven animation tools
still in core are its single heaviest block and the obvious next candidates. Byte figures
belong in `scripts/measure_catalog.py`'s output, not in this prose - one quoted here drifts
the moment a tool's description is edited, and only the tool count above is pinned by a test.
"""

from collections.abc import Iterable, Mapping
from types import MappingProxyType

CORE_MODULES: tuple[str, ...] = (
    "core",
    "scene",
    "mesh",
    "model",
    "object_animation",
    "viewport",
    "animation",
)

# Bundle name -> tool submodules under `blender_mcp.server.tools` it registers.
# `core` is always included regardless of selection; it is not a selectable name.
BUNDLES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "camera": ("camera",),
        "scene-authoring": ("scene_authoring",),
        "cloth": ("cloth",),
        "liquid": ("liquid",),
        "rigid-body": ("rigid_body", "scene_physics"),
        "geometry-nodes": ("geometry_nodes", "nd"),
        "character-rigging": ("character_rigging",),
        "retopology": ("retopology",),
        "texture-lighting": ("texture", "lighting"),
        "rendering": ("rendering",),
        "assets": ("polyhaven", "sketchfab"),
    }
)


def _ordered_unique(modules: Iterable[str]) -> tuple[str, ...]:
    """
    Drop duplicates while preserving first-seen order.

    The single definition of how a selection is assembled, so the `all` sentinel and the
    named-bundle path cannot diverge if two bundles ever name the same module.

    Args:
        modules: Module names, possibly with repeats.

    Returns:
        The same names, deduplicated, in first-seen order.

    """
    return tuple(dict.fromkeys(modules))


ALL_MODULES: tuple[str, ...] = _ordered_unique(
    CORE_MODULES + tuple(module for modules in BUNDLES.values() for module in modules)
)

# Public: `tools/__init__.py` and the bundle tests import the env var name so it is spelled
# once. `scripts/measure_catalog.py` is the one deliberate exception -- see the comment
# there for why it cannot import from this module. `ALL_SENTINEL` is exported alongside it
# so the `all` check below and the error message it appears in cannot drift apart.
TOOLSETS_ENV_VAR = "BLENDER_MCP_TOOLSETS"
ALL_SENTINEL = "all"


def resolve_toolset_modules(raw_value: str | None) -> tuple[str, ...]:
    """
    Resolve a raw `BLENDER_MCP_TOOLSETS` value into the tool submodules to import.

    `core` modules are always included. An unset or empty value selects `core` only.
    The sentinel `all` (any case; bundle names themselves are case-sensitive) selects every
    bundle, matching the pre-bundle behavior.

    Args:
        raw_value: The raw environment variable value, or None if unset.

    Returns:
        Submodule names to import, `core` modules first, in a stable order.

    Raises:
        ValueError: If a requested bundle name is not in `BUNDLES`.

    """
    requested = [name.strip() for name in (raw_value or "").split(",") if name.strip()]
    if not requested:
        return CORE_MODULES
    # Validate before the sentinel short-circuits, so `all,rendring` reports the typo
    # instead of silently selecting everything.
    unknown = sorted({name for name in requested if name not in BUNDLES and name.lower() != ALL_SENTINEL})
    if unknown:
        available = ", ".join(sorted(BUNDLES)) or "(none)"
        raise ValueError(
            f"Unknown {TOOLSETS_ENV_VAR} bundle(s): {', '.join(unknown)}. "
            f"Available bundles: {available}, or {ALL_SENTINEL!r}."
        )

    if any(name.lower() == ALL_SENTINEL for name in requested):
        return ALL_MODULES

    return _ordered_unique(CORE_MODULES + tuple(module for name in requested for module in BUNDLES[name]))
