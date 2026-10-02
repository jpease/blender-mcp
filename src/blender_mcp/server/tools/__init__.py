"""
Register core and the tool bundles BLENDER_MCP_TOOLSETS selected, as the server process starts.

See `..bundles` for the selection rules and why the advertised catalog is kept small, and
`..toolsets_runtime` for how a session registers another bundle later.
"""

# This module exists to register tools conditionally, so the re-export convention does not apply.
# ruff: file-ignore[non-empty-init-module]

from ..toolsets_runtime import register_startup_selection

register_startup_selection()
