"""
Camera tools grouped by workflow responsibility: core, targeting, animation, shots, inspection, rigs.

Submodules are NOT star-imported here. `bundles.py`'s `camera` bundle selects five of the six
by dotted name (e.g. `camera.core`) so it can exclude rig construction while `camera-rigs` opts
back in; see `.._lazy_package` for why eager star-imports -- the previous behaviour -- would
defeat that split. Package-level attribute access (`camera.CameraOpticsPatch`, used throughout
`tests/server/tools/camera/test_tools.py`) and `dir(camera)` still work via the PEP 562 hooks
below, which resolve names by parsing submodule source rather than importing to check, so only
the one submodule that actually defines a requested name is ever imported.
"""

# This file's job is exactly this lazy-attribute machinery, over which `bundles.py`'s `camera`
# and `camera-rigs` bundles split the package; the docstrings-and-reexports convention doesn't
# apply, the same reason `tools/__init__.py` carries the same suppression.
# ruff: file-ignore[non-empty-init-module]

from typing import Any

from .._lazy_package import lazy_dir, lazy_getattr
from ._shared import _call as _call

# `rigs` deliberately last: many names collide across these submodules (every one imports
# `Field`, `Context`, `_call`, etc. -- harmless, since `lazy_getattr` returns the identical
# imported object regardless of which submodule serves it), but two collisions are load-bearing
# for the leak this ordering exists to avoid -- `FollowForwardAxis` and `UpAxis` are defined in
# `_shared.py` and re-exported by both `rigs.py` and `targeting.py`. With `targeting` checked
# first, `camera.FollowForwardAxis`/`camera.UpAxis` resolve there and never import `rigs`; if
# `rigs` were checked first (verified: reversing the tuple order reproduces the leak), the
# identical value would still be returned, but `rigs` would be imported -- and its tools
# registered -- to get it. This is a real, cycle-3-caught correction of an earlier claim here
# that order "plays no role"; it does, for exactly these two names.
_SUBMODULES: tuple[str, ...] = ("animation", "core", "inspection", "shots", "targeting", "rigs")


def __getattr__(name: str) -> Any:  # ruff: ignore[any-type] -- see `.._lazy_package.lazy_getattr`
    """
    Resolve `camera.<name>` by delegating to `.._lazy_package.lazy_getattr`.

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
