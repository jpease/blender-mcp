"""
Enable the BlenderMCP addon and start its socket server, no UI interaction needed.

The socket keeps the addon's own loopback default: the MCP server runs beside
Blender inside this container and is the only thing that connects to it, so the
unauthenticated Blender protocol is never reachable from outside the container.
"""

import bpy

bpy.ops.preferences.addon_enable(module="blender_mcp")

from blender_mcp.server_core import BlenderMCPServer  # ruff: ignore[module-import-not-at-top-of-file]

port = bpy.context.scene.blendermcp_port
bpy.types.blendermcp_server = BlenderMCPServer(port=port)
bpy.types.blendermcp_server.start()
bpy.context.scene.blendermcp_server_running = bpy.types.blendermcp_server.running
