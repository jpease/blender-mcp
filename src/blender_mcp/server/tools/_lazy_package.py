"""
Shared PEP 562 lazy-submodule machinery for split tool packages.

Importing any submodule runs the package `__init__.py` first, so an `__init__.py` that
star-imports its submodules registers all their tools and defeats a dotted bundle entry (see
`..bundles`). These hooks keep `package.Name` and `dir(package)` working without that.

Names are found by parsing submodule source, not by importing to check: importing would
register the tools of submodules a bundle excludes, on `dir()` or on a missing name.
"""

import ast
import functools
import importlib

from pathlib import Path
from typing import Any


@functools.cache
def _submodule_top_level_names(package_file: str, submodule_name: str) -> frozenset[str]:
    """
    Names a submodule binds at module scope, found by parsing its source -- no import.

    Handles only the binding forms these submodules use today. A name bound by tuple
    unpacking or inside a module-level `if`/`try`/`for`/`with` is missed, and looking it up
    on the package raises `AttributeError`.

    Args:
        package_file: The lazy package's own `__file__`, used to locate `<submodule_name>.py`
            as a sibling of the package's `__init__.py`.
        submodule_name: The submodule's bare name (no package prefix, no `.py` suffix).

    Returns:
        Every name the submodule binds at module scope.

    """
    source_path = Path(package_file).parent / f"{submodule_name}.py"
    names: set[str] = set()
    for node in ast.parse(source_path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(target.id for target in node.targets if isinstance(target, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.ImportFrom):
            names.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
    return frozenset(names)


def lazy_getattr(package_name: str, package_file: str, submodule_names: tuple[str, ...], name: str) -> Any:  # ruff: ignore[any-type] -- a lazily resolved attribute can be any type a submodule defines
    """
    Resolve `<package>.<name>` by importing only the submodule that defines it.

    The body of a package's PEP 562 `__getattr__`. A name no submodule defines raises
    without importing anything.

    Args:
        package_name: The calling package's `__name__`, used as the relative-import anchor.
        package_file: The calling package's own `__file__`.
        submodule_names: Submodules to search, in the order to try them.
        name: The attribute requested on the package.

    Returns:
        The matching attribute from the one submodule in `submodule_names` that defines it.

    Raises:
        AttributeError: If no submodule in `submodule_names` defines `name`.

    """
    for submodule_name in submodule_names:
        if name in _submodule_top_level_names(package_file, submodule_name):
            submodule = importlib.import_module(f".{submodule_name}", package_name)
            return getattr(submodule, name)
    raise AttributeError(f"module {package_name!r} has no attribute {name!r}")


def lazy_dir(package_file: str, submodule_names: tuple[str, ...], package_globals: dict[str, Any]) -> list[str]:
    """
    List a lazy package's own names plus every name its submodules define, without importing any.

    The body of a package's PEP 562 `__dir__`, listing the names `lazy_getattr` can resolve.

    Args:
        package_file: The calling package's own `__file__`.
        submodule_names: Submodules whose exported names should be included.
        package_globals: The calling package's own `globals()`.

    Returns:
        Sorted, deduplicated attribute names.

    """
    names = set(package_globals)
    for submodule_name in submodule_names:
        names.update(_submodule_top_level_names(package_file, submodule_name))
    return sorted(names)
