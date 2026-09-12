"""
Tool bundle definitions used to select which domain modules a server process registers.

Every MCP client connection receives the full `tools/list` response for whatever is
registered on the process's `FastMCP` app. With all ~286 tools always registered, that
response alone can blow out a client's context before any real work starts. `BLENDER_MCP_TOOLSETS`
lets a client config select a subset of domains per process instead.
"""

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
BUNDLES: dict[str, tuple[str, ...]] = {
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

ALL_MODULES: tuple[str, ...] = CORE_MODULES + tuple(module for modules in BUNDLES.values() for module in modules)

_TOOLSETS_ENV_VAR = "BLENDER_MCP_TOOLSETS"
_ALL_SENTINEL = "all"


def resolve_toolset_modules(raw_value: str | None) -> tuple[str, ...]:
    """
    Resolve a raw `BLENDER_MCP_TOOLSETS` value into the tool submodules to import.

    `core` modules are always included. An unset or empty value selects `core` only.
    The literal `all` selects every bundle (parity with the pre-bundle behavior).

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
    if any(name.lower() == _ALL_SENTINEL for name in requested):
        return ALL_MODULES

    unknown = sorted({name for name in requested if name not in BUNDLES})
    if unknown:
        available = ", ".join(sorted(BUNDLES)) or "(none)"
        raise ValueError(
            f"Unknown {_TOOLSETS_ENV_VAR} bundle(s): {', '.join(unknown)}. Available bundles: {available}, or 'all'."
        )

    selected_modules: list[str] = list(CORE_MODULES)
    for name in requested:
        for module in BUNDLES[name]:
            if module not in selected_modules:
                selected_modules.append(module)
    return tuple(selected_modules)
