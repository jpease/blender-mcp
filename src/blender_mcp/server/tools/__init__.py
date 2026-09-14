"""
Import the tool submodules selected by BLENDER_MCP_TOOLSETS for their @mcp.tool() registration side effect.

BLENDER_MCP_TOOLSETS (a comma-separated list of bundle names from `..bundles.BUNDLES`, or
`all`) selects which domains this process registers; `core` modules are always included.
See ../bundles.py for why an advertised payload is permanent context cost, and for the
bundle table; README.md carries the client config examples.
"""

# This file's job is exactly this conditional registration; the docstrings-and-reexports
# convention doesn't apply.
# ruff: file-ignore[non-empty-init-module]

import importlib
import os

from ..bundles import TOOLSETS_ENV_VAR, resolve_toolset_modules

for _module_name in resolve_toolset_modules(os.getenv(TOOLSETS_ENV_VAR)):
    importlib.import_module(f".{_module_name}", package=__name__)

# Imported last and only after the modules above so the documentation pass covers
# exactly the tools this process actually registered.
from . import _documentation as _documentation  # ruff: ignore[module-import-not-at-top-of-file]
