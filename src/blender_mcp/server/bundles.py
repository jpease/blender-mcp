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

Two tiers select from this catalog. `BUNDLES` is the technical catalog: one domain, one name.
`MODES` sits above it as a curated, artist-facing preset ("shot", "asset") that expands to a
handful of bundles at once, because the add-on -- not a human -- is the client choosing the
surface. `resolve_toolset_modules` accepts either kind of name, and freely mixes them.
"""

from collections.abc import Iterable, Mapping
from types import MappingProxyType

CORE_MODULES: tuple[str, ...] = (
    "core",
    "scene",
    "object_animation",
    "viewport",
    "animation",
)

# Bundle name -> tool submodules under `blender_mcp.server.tools` it registers. A dotted entry
# (e.g. "camera.core") names one submodule of a package rather than the whole package; the
# package's own `__init__.py` must resolve it lazily (see `tools/_lazy_package.py`) or the
# split is defeated -- importing any one submodule still runs the parent `__init__.py` first.
# `core` is always included regardless of selection; it is not a selectable name.
_BUNDLES_BASE: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "core-authoring": ("mesh", "model"),
        # Six camera submodules total; `rigs` (construction) is reachable only via
        # `camera-rigs`, the way a shot rarely needs to build a new rig.
        "camera": ("camera.animation", "camera.core", "camera.inspection", "camera.shots", "camera.targeting"),
        "camera-rigs": ("camera.rigs",),
        "scene-authoring": ("scene_authoring",),
        "cloth": ("cloth",),
        "liquid": ("liquid",),
        "rigid-body": ("rigid_body", "scene_physics"),
        "geometry-nodes": ("geometry_nodes", "nd"),
        "character-rigging": ("character_rigging",),
        "retopology": ("retopology",),
        # Four lighting submodules total; `construction` is reachable only via
        # `lighting-construction`, the way presets replace it for most shots.
        "lighting": ("lighting.environment", "lighting.inspection", "lighting.rendering"),
        # Carries `lighting.rendering` too, not just `lighting.construction`: `create_studio_lighting`
        # calls `render_lighting_preview` directly (construction.py:13), so `lighting-construction`
        # cannot actually function without it. Verified by measuring `BLENDER_MCP_TOOLSETS=lighting-
        # construction` live rather than trusting the module list -- omitting `lighting.rendering` here
        # would still transitively import and register it (Python imports the whole module `construction`
        # imports from), just without saying so, which is the bug this line exists to avoid repeating.
        "lighting-construction": ("lighting.construction", "lighting.rendering"),
        "texture": ("texture",),
        "rendering": ("rendering",),
        "assets": ("polyhaven", "sketchfab"),
    }
)

# `texture-lighting` is the pre-Phase-1-Task-5 fused bundle name, kept as a deprecated alias so
# an existing client config keeps working unchanged. Computed from the split bundles above
# rather than hand-listed, so it cannot silently drift from the sum of the parts it replaces;
# `ALL_MODULES` below dedupes the resulting repeat, which is the deliberate canary
# `test_all_sentinel_selects_every_module` exists to catch, not a bug.
BUNDLES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        **_BUNDLES_BASE,
        "texture-lighting": (
            _BUNDLES_BASE["texture"] + _BUNDLES_BASE["lighting"] + _BUNDLES_BASE["lighting-construction"]
        ),
    }
)

# Public: `tools/__init__.py` and the bundle tests import the env var name so it is spelled
# once. `scripts/measure_catalog.py` is the one deliberate exception -- see the comment
# there for why it cannot import from this module. `ALL_SENTINEL` is exported alongside it
# so the `all` check below and the error message it appears in cannot drift apart. Defined
# before MODES because `_check_modes_are_well_formed` below needs ALL_SENTINEL to already exist.
TOOLSETS_ENV_VAR = "BLENDER_MCP_TOOLSETS"
ALL_SENTINEL = "all"

# Artist-facing presets over BUNDLES. Selection is a human/config decision made once per MCP
# client entry, before the server process starts (README.md "Tool Bundles": one entry per
# mode/bundle set, `BLENDER_MCP_TOOLSETS` set on that entry) - not something the add-on or a
# live session decides at runtime. Modes exist so that config wants one word for the surface an
# artist is working in, not a comma list of bundles.
# (docs/superpowers/specs/2026-09-11-episode-consistency-architecture-design.md Sec 4.3 defines
# exactly these two working surfaces: `shot` assembles, animates, lights and renders; `asset`
# authors or revises canon. Sec 4.7 is unrelated - it covers the addon's thread architecture, not
# toolset selection.) A mode still resolves to modules through BUNDLES, so it adds no module of
# its own and cannot move `all`'s tool count.
#
# The two modes are disjoint outside core (enforced by
# `test_shot_and_asset_modes_share_only_the_core_surface`): `shot` takes `lighting`, not
# `texture`, since assembling a shot does not author new materials; `asset` takes `texture`, not
# `lighting`, since authoring canon does not light or render a specific shot. Neither takes the
# construction-workflow bundles (`camera-rigs`, `lighting-construction`) -- both are opt-in
# extras a mode can still compose with, e.g. `shot,camera-rigs`.
MODES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "shot": ("camera", "lighting", "rendering"),
        "asset": ("core-authoring", "scene-authoring", "texture", "retopology", "geometry-nodes"),
    }
)


def _format_names(names: Iterable[str]) -> str:
    """
    Render names sorted and comma-joined for an error message, or `(none)` if there are none.

    One definition so every "available names" or "offending names" clause in this module's
    validation and error-message code stays formatted identically if the style is ever tweaked.

    Args:
        names: Names to render.

    Returns:
        A sorted, comma-joined string, or the literal `(none)` for an empty input.

    """
    return ", ".join(sorted(names)) or "(none)"


def _check_modes_are_well_formed() -> None:
    """
    Validate MODES against BUNDLES and ALL_SENTINEL at import time, not only in tests.

    Raises rather than asserts so the check still fires under `python -O`. Run once, right
    below both dicts, so a maintainer editing MODES gets one specific failure here instead of
    a shadowed name silently winning inside `_expand_modes`, a bare `KeyError` surfacing later
    out of `resolve_toolset_modules`, a mode silently unreachable because the sentinel branch
    claims it first, or a repeated bundle within one mode's own tuple silently absorbed by
    `_ordered_unique` downstream instead of flagged as the authoring typo it almost certainly is.

    Raises:
        ValueError: If a mode name collides with a bundle name or the `all` sentinel, a mode
            names a bundle that does not exist, or a mode's own tuple repeats a bundle name.

    """
    collisions = set(MODES) & set(BUNDLES)
    if collisions:
        raise ValueError(f"mode name(s) shadow a bundle name: {_format_names(collisions)}")
    sentinel_collisions = {name for name in MODES if name.lower() == ALL_SENTINEL}
    if sentinel_collisions:
        raise ValueError(f"mode name(s) shadow the {ALL_SENTINEL!r} sentinel: {_format_names(sentinel_collisions)}")
    unknown_bundles = {bundle for members in MODES.values() for bundle in members if bundle not in BUNDLES}
    if unknown_bundles:
        raise ValueError(f"MODES references bundle(s) that do not exist in BUNDLES: {_format_names(unknown_bundles)}")
    duplicated = {mode for mode, members in MODES.items() if len(members) != len(set(members))}
    if duplicated:
        raise ValueError(f"mode(s) repeat a bundle name in their own tuple: {_format_names(duplicated)}")


_check_modes_are_well_formed()


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


def _expand_modes(names: Iterable[str]) -> tuple[str, ...]:
    """
    Replace each mode name with the bundle names it curates; pass bundle names through unchanged.

    Expanding modes here, ahead of the bundle lookup, is what lets `shot,retopology` compose: a
    mode is resolved to bundles once, then treated exactly like a hand-picked bundle list.

    Assumes every name is already a known mode or bundle: an unrecognized name falls through
    `MODES.get`'s default and is returned as if it were a bundle name, so the caller must
    validate first. `resolve_toolset_modules` is the only caller and does exactly that.

    Args:
        names: Requested names, each either a mode or a bundle.

    Returns:
        Bundle names only, modes expanded, in first-seen order with repeats collapsed.

    """
    return _ordered_unique(bundle for name in names for bundle in MODES.get(name, (name,)))


def resolve_toolset_modules(raw_value: str | None) -> tuple[str, ...]:
    """
    Resolve a raw `BLENDER_MCP_TOOLSETS` value into the tool submodules to import.

    `core` modules are always included. An unset or empty value selects `core` only.
    The sentinel `all` (any case; bundle and mode names themselves are case-sensitive) selects
    every bundle, matching the pre-bundle behavior. A name may be a mode (`shot`, `asset`) or a
    bundle, and the two lists never share a name, so a client can mix them freely, e.g.
    `shot,retopology`.

    Args:
        raw_value: The raw environment variable value, or None if unset.

    Returns:
        Submodule names to import, `core` modules first, in a stable order.

    Raises:
        ValueError: If a requested name is not a known mode or bundle.

    """
    requested = [name.strip() for name in (raw_value or "").split(",") if name.strip()]
    if not requested:
        return CORE_MODULES
    # Validate before the sentinel short-circuits, so `all,rendring` reports the typo
    # instead of silently selecting everything.
    known = set(BUNDLES) | set(MODES)
    unknown = sorted({name for name in requested if name not in known and name.lower() != ALL_SENTINEL})
    if unknown:
        raise ValueError(
            f"Unknown {TOOLSETS_ENV_VAR} name(s): {', '.join(unknown)}. "
            f"Available modes: {_format_names(MODES)}. "
            f"Available bundles: {_format_names(BUNDLES)}, or {ALL_SENTINEL!r}."
        )

    if any(name.lower() == ALL_SENTINEL for name in requested):
        return ALL_MODULES

    bundle_names = _expand_modes(requested)
    return _ordered_unique(CORE_MODULES + tuple(module for name in bundle_names for module in BUNDLES[name]))
