"""
Load the bundled addon as a package, for the `tests/blender_*_smoke.py` scripts only.

Every smoke script runs inside a headless Blender (`blender --background --python ...`),
where `bpy` is real and the addon's `__init__` can execute; nothing here can run under
pytest. Blender's `--python` does not put a script's directory on `sys.path`, so each
script appends `tests/` itself before importing `load_addon` from this module.

Each script loads the addon under its own package name, so its `ADDON_ID` and every
`sys.modules` key it registers stay distinct from an installed copy of the addon.
"""

import importlib.util
import sys

from pathlib import Path
from types import ModuleType

ADDON_INIT = Path(__file__).resolve().parents[1] / "src" / "blender_mcp" / "bundled" / "addon" / "__init__.py"


def load_addon(package_name: str) -> ModuleType:
    """
    Execute the bundled addon from source as the package `package_name`.

    The package is registered in `sys.modules` before its `__init__` runs, so the
    addon's own relative imports and `ADDON_ID = __name__` resolve under that name,
    and a caller can then import `<package_name>.handlers...` normally.

    Args:
        package_name: Top-level name to register the addon package under.

    Returns:
        ModuleType: The executed addon package.

    Raises:
        ImportError: If no module spec can be built for the addon's `__init__.py`.

    """
    spec = importlib.util.spec_from_file_location(
        package_name,
        ADDON_INIT,
        submodule_search_locations=[str(ADDON_INIT.parent)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot build a module spec for {ADDON_INIT}", name=package_name, path=str(ADDON_INIT))
    addon = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = addon
    spec.loader.exec_module(addon)
    return addon
