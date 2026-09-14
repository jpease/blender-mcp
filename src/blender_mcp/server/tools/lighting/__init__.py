"""
Lighting tools grouped by inspection, construction, environment, and rendering responsibility.

Submodules are NOT star-imported here. `bundles.py`'s `lighting` bundle selects three of the
four by dotted name (e.g. `lighting.environment`) so it can exclude light construction while
`lighting-construction` opts back in; see `.._lazy_package` for why eager star-imports -- the
previous behaviour -- would defeat that split. Package-level attribute access and `dir(lighting)`
are resolved lazily via the PEP 562 hooks below, which parse submodule source to find a name
rather than importing to check, so only the one submodule that actually defines it is imported.
"""

# This file's job is exactly this lazy-attribute machinery, over which `bundles.py`'s
# `lighting` and `lighting-construction` bundles split the package; the docstrings-and-reexports
# convention doesn't apply, the same reason `tools/__init__.py` carries the same suppression.
# ruff: file-ignore[non-empty-init-module]

from typing import Any

from .._lazy_package import lazy_dir, lazy_getattr

# `construction` deliberately last -- see camera's `_SUBMODULES` comment in `..camera` for why
# order is load-bearing whenever a name collides across submodules, not merely a tie-break.
# `render_lighting_preview` is the concrete collision here: defined in `rendering.py` and
# re-exported by `construction.py` (`from .rendering import render_lighting_preview`, because
# `create_studio_lighting` calls it). With `rendering` checked before `construction`,
# `lighting.render_lighting_preview` resolves via `rendering` and never imports `construction`
# to get it. `.._lazy_package`'s docstring covers why parsing rather than importing is what
# keeps `dir(lighting)` and a missing-name lookup from importing `construction` unasked-for.
# Separately, `construction` itself imports `rendering` (the same call above), so
# `lighting-construction` explicitly carries `lighting.rendering` too in `bundles.py` -- see the
# comment there.
_SUBMODULES: tuple[str, ...] = ("environment", "inspection", "rendering", "construction")


def __getattr__(name: str) -> Any:  # ruff: ignore[any-type] -- see `.._lazy_package.lazy_getattr`
    """
    Resolve `lighting.<name>` by delegating to `.._lazy_package.lazy_getattr`.

    Propagates `AttributeError` unchanged if no submodule in `_SUBMODULES` defines `name`.

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
