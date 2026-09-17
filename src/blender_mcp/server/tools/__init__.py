"""
Import the tool submodules selected by BLENDER_MCP_TOOLSETS for their @mcp.tool() registration side effect.

See `..bundles` for the selection rules and why the advertised catalog is kept small.
"""

# This module exists to register tools conditionally, so the re-export convention does not apply.
# ruff: file-ignore[non-empty-init-module]

import importlib
import os

from ..bundles import TOOLSETS_ENV_VAR, resolve_toolset_modules

for _module_name in resolve_toolset_modules(os.getenv(TOOLSETS_ENV_VAR)):
    importlib.import_module(f".{_module_name}", package=__name__)

# Last, so the documentation pass sees exactly the tools registered above.
from . import _documentation as _documentation  # ruff: ignore[module-import-not-at-top-of-file]
