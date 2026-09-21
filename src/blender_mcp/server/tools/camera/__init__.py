"""
Camera tools grouped by workflow responsibility: core, targeting, animation, shots, inspection, rigs.

Submodules are resolved lazily, not star-imported, so the `camera` bundle can leave out
`rigs`; see `.._lazy_package`.
"""

# This module exists for lazy attribute resolution, so the re-export convention does not apply.
# ruff: file-ignore[non-empty-init-module]

from typing import Any

from .._lazy_package import lazy_dir, lazy_getattr

# First match wins. `rigs` stays last because it shares `FollowForwardAxis` and `UpAxis` with
# `targeting`; checked earlier, it would be imported, registering its tools, to serve them.
_SUBMODULES: tuple[str, ...] = ("animation", "core", "inspection", "shots", "targeting", "rigs")


def __getattr__(name: str) -> Any:  # ruff: ignore[any-type] -- see `.._lazy_package.lazy_getattr`
    """
    Resolve `camera.<name>` by delegating to `.._lazy_package.lazy_getattr`.

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
