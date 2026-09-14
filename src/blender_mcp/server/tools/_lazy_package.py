"""
Shared PEP 562 lazy-submodule machinery for split tool packages.

A package whose `__init__.py` star-imports every submodule defeats a dotted bundle entry (see
`..bundles`): Python always runs a package's `__init__.py` before importing any of its
submodules, so requesting one submodule via `importlib.import_module` still imports -- and
registers the `@mcp.tool()`-decorated tools of -- every sibling the `__init__.py` star-imports.
That is exactly how `camera`/`lighting` behaved before this module existed: naming `camera.core`
still registered all 23 camera tools, because `camera/__init__.py` star-imported `rigs` too.

`lazy_getattr`/`lazy_dir` let such a package's `__init__.py` stay import-light -- no submodule
is *executed* until something actually asks for one of its names -- while `package.SomeName`
attribute access (used throughout this repo's `tests/server/tools/*/test_tools.py` files) and
`dir(package)` keep working exactly as they did under the old star-import.

Both resolve names by parsing each candidate submodule's source with `ast` rather than by
importing it to check with `hasattr`: an earlier version of this module did the latter, and
Task 5's cycle-2 review caught that it defeats the whole split two ways `hasattr`-checking
cannot avoid -- `dir(package)` unconditionally imported (and registered the tools of) every
submodule including the one a bundle deliberately excludes, and any missing or mistyped name
walked all the way to that submodule before raising `AttributeError`, importing it along the
way. Parsing a file's top-level definitions costs no import and therefore no tool registration,
no matter which submodule is asked about or whether the name exists at all. Verified to agree
with the real runtime attribute set for every submodule these two packages currently split
across, both by hand and by
`test_ast_derived_submodule_names_match_the_real_runtime_attributes` in `test_bundles.py`,
which re-checks this on every test run rather than trusting a one-time check to stay true.
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

    Covers every way a module-level name currently reaches a module's own `__dict__` in these
    packages: `def`/`async def`/`class` statements, plain and annotated assignments, and
    `import`/`from ... import ...` (including `as` aliases) -- the same names `hasattr`/`vars()`
    would see after actually importing the module, without needing to import it to find out.
    `test_ast_derived_submodule_names_match_the_real_runtime_attributes` in `test_bundles.py`
    checks this claim against every current camera/lighting submodule, but the check is scoped
    to those files: tuple-unpacking assignment, augmented assignment, and a name bound inside a
    module-level `if`/`try`/`for`/`with` block are not handled and would under-report if any of
    these submodules ever grew one -- causing a false `AttributeError` for that name, not a leak.

    Args:
        package_file: The lazy package's own `__file__`, used to locate `<submodule_name>.py`
            as a sibling of the package's `__init__.py`.
        submodule_name: The submodule's bare name (no package prefix, no `.py` suffix).

    Returns:
        Every name the submodule binds at module scope. Cached: each file is parsed at most
        once no matter how many names are looked up against it.

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
    Resolve `<package>.<name>` by importing only the submodule that actually defines it.

    Checks each of `submodule_names` against `_submodule_top_level_names` first, so only the
    one that defines `name` is ever imported -- a name that exists nowhere in `submodule_names`
    raises without importing any of them. Intended as the body of a package's PEP 562
    module-level `__getattr__`, which Python calls only when normal attribute lookup on the
    package has already failed, so this never runs for a name the package's own `__init__.py`
    imports at module scope.

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

    Intended as the body of a package's PEP 562 module-level `__dir__`, so introspection
    (`dir(package)`, IDE completion) sees the same names `lazy_getattr` can resolve, even though
    none of them are imported at module scope -- and calling `dir()` never itself imports (and
    so never registers the tools of) a submodule a bundle deliberately excluded. Unlike
    `lazy_getattr`, no relative-import anchor is needed here since nothing is imported.

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
