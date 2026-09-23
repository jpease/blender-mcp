"""
Tool bundles: which domain modules a server process registers.

A client carries every advertised tool definition in the model's context on every turn, so
each tool costs context for the whole session, not once at startup. With every bundle
registered (304 tools, per `scripts/measure_catalog.py all`) that alone can fill a client's
context. `BLENDER_MCP_TOOLSETS` selects a subset per process. A test parses the tool count
above; get byte figures from `scripts/measure_catalog.py` instead, since they go stale.

A module belongs in `CORE_MODULES` only if nearly every workflow needs it or a client cannot
inspect the scene without it. `file_lifecycle` is core because `shot` and `asset` both need
to open and save files, and the two modes may not share a bundle.

`BUNDLES` names one domain each. `MODES` are artist-facing presets that expand to several
bundles. `resolve_toolset_modules` accepts both kinds of name, mixed.
"""

from collections.abc import Iterable, Mapping
from types import MappingProxyType

CORE_MODULES: tuple[str, ...] = (
    "core",
    "scene",
    "object_animation",
    "viewport",
    "animation",
    "file_lifecycle",
)

# A dotted entry names one submodule of a package. The package `__init__.py` must stay lazy
# (see `tools/_lazy_package.py`), because importing a submodule runs it first.
_BUNDLES_BASE: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "core-authoring": ("mesh", "model"),
        # `rigs` is left to `camera-rigs`: a shot rarely builds a new rig.
        "camera": ("camera.animation", "camera.core", "camera.inspection", "camera.shots", "camera.targeting"),
        "camera-rigs": ("camera.rigs",),
        "scene-authoring": ("scene_authoring",),
        "cloth": ("cloth",),
        "liquid": ("liquid",),
        "rigid-body": ("rigid_body", "scene_physics"),
        "geometry-nodes": ("geometry_nodes", "nd"),
        "character-rigging": (
            "character_rigging.foundation",
            "character_rigging.controls",
            "character_rigging.deformation",
            "character_rigging.posing",
        ),
        # Posing a linked character is shot work; building or binding a rig is not.
        "character-posing": ("character_rigging.posing",),
        "retopology": ("retopology",),
        # `construction` is left to `lighting-construction`.
        "lighting": ("lighting.environment", "lighting.inspection", "lighting.rendering"),
        # Lists `lighting.rendering` because `construction.py` imports it, so its tools register
        # with this bundle anyway.
        "lighting-construction": ("lighting.construction", "lighting.rendering"),
        "texture": ("texture",),
        "rendering": ("rendering",),
        "assets": ("polyhaven", "sketchfab"),
    }
)

# `texture-lighting` is a deprecated alias so existing client configs keep working. Built from
# the split bundles so it cannot drift from them. It is excluded from `CANONICAL_BUNDLES`
# because it names no tool the split bundles do not, and a report that listed both would
# offer an agent two spellings of one choice.
DEPRECATED_BUNDLE_ALIASES: frozenset[str] = frozenset({"texture-lighting"})

BUNDLES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        **_BUNDLES_BASE,
        "texture-lighting": (
            _BUNDLES_BASE["texture"] + _BUNDLES_BASE["lighting"] + _BUNDLES_BASE["lighting-construction"]
        ),
    }
)

# Every bundle an agent should be offered by name, alias-free.
CANONICAL_BUNDLES: tuple[str, ...] = tuple(name for name in BUNDLES if name not in DEPRECATED_BUNDLE_ALIASES)

# Import these rather than respelling them. `scripts/measure_catalog.py` cannot; see why there.
TOOLSETS_ENV_VAR = "BLENDER_MCP_TOOLSETS"
ALL_SENTINEL = "all"

# Artist-facing presets, chosen once per MCP client entry before the process starts. `shot`
# assembles, poses, lights and renders; `asset` authors or revises canon. The modes must not
# overlap outside core: assembling a shot authors no materials, and authoring canon lights no
# shot. Placing lights is shot work, since an interior has no usable light without it; camera-rig
# construction stays opt-in, e.g. `shot,camera-rigs`.
MODES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "shot": ("camera", "lighting", "lighting-construction", "rendering", "character-posing"),
        "asset": ("core-authoring", "scene-authoring", "texture", "retopology", "geometry-nodes"),
    }
)


def _format_names(names: Iterable[str]) -> str:
    """
    Render names sorted and comma-joined for an error message, or `(none)` if there are none.

    Args:
        names: Names to render.

    Returns:
        A sorted, comma-joined string, or the literal `(none)` for an empty input.

    """
    return ", ".join(sorted(names)) or "(none)"


def _check_modes_are_well_formed() -> None:
    """
    Validate MODES against BUNDLES and ALL_SENTINEL at import time.

    Raises rather than asserts so the check survives `python -O`. Otherwise a bad MODES edit
    fails later and obscurely: a mode silently shadows a bundle, or a missing bundle shows up
    as a bare `KeyError`.

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
    Replace each mode name with its bundle names; pass bundle names through unchanged.

    Does not validate: an unknown name comes back as if it were a bundle, so validate first.

    Args:
        names: Requested names, each either a mode or a bundle.

    Returns:
        Bundle names only, modes expanded, in first-seen order with repeats collapsed.

    """
    return _ordered_unique(bundle for name in names for bundle in MODES.get(name, (name,)))


def resolve_toolset_bundles(raw_value: str | None) -> tuple[str, ...]:
    """
    Resolve a raw `BLENDER_MCP_TOOLSETS` value into the bundle names it selects.

    `core` is not among them: it is implicit and always mounted, and naming it here would
    imply it could be deselected. `all` expands to every canonical bundle, so the deprecated
    alias never appears unless it was the thing asked for.

    Args:
        raw_value: The raw environment variable value, or None if unset.

    Returns:
        Bundle names, modes expanded, in first-seen order with repeats collapsed.

    Raises:
        ValueError: If a requested name is not a known mode or bundle.

    """
    requested = [name.strip() for name in (raw_value or "").split(",") if name.strip()]
    if not requested:
        return ()
    # Before the `all` check, so `all,rendring` still reports the typo.
    known = set(BUNDLES) | set(MODES)
    unknown = sorted({name for name in requested if name not in known and name.lower() != ALL_SENTINEL})
    if unknown:
        raise ValueError(
            f"Unknown {TOOLSETS_ENV_VAR} name(s): {', '.join(unknown)}. "
            f"Available modes: {_format_names(MODES)}. "
            f"Available bundles: {_format_names(BUNDLES)}, or {ALL_SENTINEL!r}."
        )
    if any(name.lower() == ALL_SENTINEL for name in requested):
        return CANONICAL_BUNDLES
    return _expand_modes(requested)


def resolve_toolset_modules(raw_value: str | None) -> tuple[str, ...]:
    """
    Resolve a raw `BLENDER_MCP_TOOLSETS` value into the tool submodules to import.

    `core` modules are always included; an unset or empty value selects only them. `all`, in
    any case, selects every bundle; other names are case-sensitive. Modes and bundles can be
    mixed, e.g. `shot,retopology`.

    Args:
        raw_value: The raw environment variable value, or None if unset.

    Returns:
        Submodule names to import, `core` modules first, in a stable order.

    Raises:
        ValueError: If a requested name is not a known mode or bundle.

    """
    bundle_names = resolve_toolset_bundles(raw_value)
    return _ordered_unique(CORE_MODULES + tuple(module for name in bundle_names for module in BUNDLES[name]))
