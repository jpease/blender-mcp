"""
Import the tool submodules selected by BLENDER_MCP_TOOLSETS for their @mcp.tool() registration side effect.

Every tool registered here is sent to every connected MCP client in its `tools/list`
response. With all domains always imported, that response alone can be large enough to
exhaust a client's context before any real work starts. BLENDER_MCP_TOOLSETS (a
comma-separated list of bundle names from `..bundles.BUNDLES`, or `all`) lets a server
process register only the domains a client's config asks for; `core` modules are always
included. See ../bundles.py and README.md for the bundle table and client config examples.
"""

# This file's job is exactly this conditional registration; the docstrings-and-reexports
# convention doesn't apply.
# ruff: file-ignore[non-empty-init-module]

import importlib
import os

from ..bundles import resolve_toolset_modules

for _module_name in resolve_toolset_modules(os.getenv("BLENDER_MCP_TOOLSETS")):
    importlib.import_module(f".{_module_name}", package=__name__)

# Imported last and only after the modules above so the documentation pass covers
# exactly the tools this process actually registered.
from . import _documentation as _documentation  # ruff: ignore[module-import-not-at-top-of-file]
