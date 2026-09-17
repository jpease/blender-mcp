"""
Lighting tools grouped by inspection, construction, environment, and rendering responsibility.

Submodules are resolved lazily, not star-imported, so the `lighting` bundle can leave out
`construction`; see `.._lazy_package`.
"""

# This module exists for lazy attribute resolution, so the re-export convention does not apply.
# ruff: file-ignore[non-empty-init-module]

from typing import Any

from .._lazy_package import lazy_dir, lazy_getattr

# First match wins. `construction` stays last because it also exposes
# `render_lighting_preview`; checked earlier, it would be imported, registering its tools.
_SUBMODULES: tuple[str, ...] = ("environment", "inspection", "rendering", "construction")


def __getattr__(name: str) -> Any:  # ruff: ignore[any-type] -- see `.._lazy_package.lazy_getattr`
    """
    Resolve `lighting.<name>` by delegating to `.._lazy_package.lazy_getattr`.

    Args:
        name: The attribute requested on this package.

    Returns:
        The matching attribute from the one submodule in `_SUBMODULES` that defines it.

    """
    return lazy_getattr(__name__, __file__, _SUBMODULES, name)


def __dir__() -> list[str]:
    """
    List this package's own names plus every name its submodules define.

    Returns:
        Sorted, deduplicated attribute names; see `.._lazy_package.lazy_dir`.

    """
    return lazy_dir(__file__, _SUBMODULES, globals())
