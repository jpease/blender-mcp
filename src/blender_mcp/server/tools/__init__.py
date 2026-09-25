"""
Import the tool submodules selected by BLENDER_MCP_TOOLSETS for their @mcp.tool() registration side effect.

See `..bundles` for the selection rules and why the advertised catalog is kept small.
"""

# This module exists to register tools conditionally, so the re-export convention does not apply.
# ruff: file-ignore[non-empty-init-module]

import importlib
import os

from ..app import mcp
from ..bundles import TOOLSETS_ENV_VAR, resolve_toolset_modules
from ._strict_args import harden_tool_arguments

for _module_name in resolve_toolset_modules(os.getenv(TOOLSETS_ENV_VAR)):
    importlib.import_module(f".{_module_name}", package=__name__)

# The SDK generates each tool's top-level argument model with pydantic's defaults, `extra="ignore"`
# and `allow_inf_nan=True`, so an unknown key would be dropped and a NaN coordinate forwarded to
# Blender. Harden the tools just registered; see `._strict_args` for why.
harden_tool_arguments(mcp)

# Last, so the documentation pass sees exactly the tools registered above.
from . import _documentation as _documentation  # ruff: ignore[module-import-not-at-top-of-file]
