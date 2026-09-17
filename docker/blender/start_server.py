"""
Enable the BlenderMCP addon and start its socket server without the UI.

The socket stays on loopback: its protocol has no authentication, and the only
client is the MCP server inside this container.
"""

import bpy

bpy.ops.preferences.addon_enable(module="blender_mcp")

from blender_mcp.server_core import BlenderMCPServer  # ruff: ignore[module-import-not-at-top-of-file]

port = bpy.context.scene.blendermcp_port
bpy.types.blendermcp_server = BlenderMCPServer(port=port)
bpy.types.blendermcp_server.start()
bpy.context.scene.blendermcp_server_running = bpy.types.blendermcp_server.running
