"""
Enable the BlenderMCP addon and start its socket server, no UI interaction needed.

The addon's own start_server operator (ui.py) always binds `localhost`, which is correct
for real usage (server and client on the same machine) but unreachable from outside this
container via Docker's port-forwarding - a socket bound to 127.0.0.1 only accepts
connections whose source is the loopback interface inside this container's own network
namespace, and the forwarded connection from the host arrives on a different interface.
So this test rig calls BlenderMCPServer directly with host="0.0.0.0" instead of going
through the operator, rather than changing the addon's real default.
"""

import bpy

bpy.ops.preferences.addon_enable(module="blender_mcp")

from blender_mcp.server_core import BlenderMCPServer  # noqa: E402

port = bpy.context.scene.blendermcp_port
bpy.types.blendermcp_server = BlenderMCPServer(host="0.0.0.0", port=port)
bpy.types.blendermcp_server.start()
bpy.context.scene.blendermcp_server_running = bpy.types.blendermcp_server.running
