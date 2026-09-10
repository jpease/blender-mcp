# Code created by Siddharth Ahuja: www.github.com/ahujasid © 2025

from contextlib import suppress

import bpy

from bpy.props import IntProperty

# Split across submodules, __name__ resolves to this package's dotted module
# name only here, at the top level - every other file must import ADDON_ID
# from this module rather than reading its own __name__, which would resolve
# to the wrong (submodule-local) dotted name.
ADDON_ID = __name__

bl_info = {
    "name": "Blender MCP",
    "author": "BlenderMCP",
    "version": (2, 0, 0),
    "blender": (5, 1, 0),
    "location": "View3D > Sidebar > BlenderMCP",
    "description": "Connect Blender to Claude via MCP",
    "category": "Interface",
}

# Keep in sync with blender_mcp.addon_manager.EXPECTED_ADDON_PROTOCOL_VERSION.
# The server reads handshake fields without comparing versions, so an older
# addon that omits writable_output_roots gets an empty list.
ADDON_PROTOCOL_VERSION = 37

from . import session  # ruff: ignore[module-import-not-at-top-of-file]
from .server_core import BlenderMCPServer  # ruff: ignore[module-import-not-at-top-of-file]
from .ui import (  # ruff: ignore[module-import-not-at-top-of-file]
    BLENDERMCP_AddonPreferences,
    BLENDERMCP_OT_StartServer,
    BLENDERMCP_OT_StopServer,
    BLENDERMCP_PT_Panel,
)


# Registration functions
def register() -> None:
    bpy.types.Scene.blendermcp_port = IntProperty(
        name="Port",
        description="Port for the BlenderMCP server",
        default=9876,
        min=1024,
        max=65535,
    )

    bpy.types.Scene.blendermcp_server_running = bpy.props.BoolProperty(name="Server Running", default=False)

    bpy.types.Scene.blendermcp_auto_start_server = bpy.props.BoolProperty(
        name="Auto-Start Server",
        description="Automatically start the MCP server when Blender loads",
        default=False,
    )

    bpy.types.Scene.blendermcp_use_polyhaven = bpy.props.BoolProperty(
        name="Use Poly Haven",
        description="Enable Poly Haven asset integration",
        default=False,
    )

    bpy.types.Scene.blendermcp_use_sketchfab = bpy.props.BoolProperty(
        name="Use Sketchfab",
        description="Enable Sketchfab asset integration",
        default=False,
    )

    bpy.types.Scene.blendermcp_sketchfab_api_key = bpy.props.StringProperty(
        name="Sketchfab API Key",
        subtype="PASSWORD",
        description="API Key provided by Sketchfab",
        default="",
    )

    bpy.types.Scene.blendermcp_use_nd = bpy.props.BoolProperty(
        name="Use ND",
        description="Enable HugeMenace ND non-destructive workflow tools",
        default=False,
    )

    # Before the server can start, so the session epoch counts every file load
    # a client could see. Safe to repeat: enabling an already-enabled addon
    # unregisters it first, so the handlers are gone before this runs again.
    session.register_handlers()

    # Register preferences class
    bpy.utils.register_class(BLENDERMCP_AddonPreferences)

    bpy.utils.register_class(BLENDERMCP_PT_Panel)
    bpy.utils.register_class(BLENDERMCP_OT_StartServer)
    bpy.utils.register_class(BLENDERMCP_OT_StopServer)

    # Auto-start the server so the MCP client can connect without manual UI interaction
    scene = getattr(bpy.context, "scene", None)
    if scene is not None:
        port = scene.blendermcp_port
        auto_start = scene.blendermcp_auto_start_server
    else:
        port = 9876
        auto_start = False

    if auto_start and (not hasattr(bpy.types, "blendermcp_server") or not bpy.types.blendermcp_server):
        bpy.types.blendermcp_server = BlenderMCPServer(port=port)
    if auto_start and not bpy.types.blendermcp_server.running:
        bpy.types.blendermcp_server.start()
        with suppress(AttributeError):
            bpy.context.scene.blendermcp_server_running = bpy.types.blendermcp_server.running

    print("BlenderMCP addon registered")


def unregister() -> None:
    # Stop the server if it's running
    if hasattr(bpy.types, "blendermcp_server") and bpy.types.blendermcp_server:
        bpy.types.blendermcp_server.stop()
        del bpy.types.blendermcp_server

    session.unregister_handlers()

    bpy.utils.unregister_class(BLENDERMCP_PT_Panel)
    bpy.utils.unregister_class(BLENDERMCP_OT_StartServer)
    bpy.utils.unregister_class(BLENDERMCP_OT_StopServer)
    bpy.utils.unregister_class(BLENDERMCP_AddonPreferences)

    del bpy.types.Scene.blendermcp_port
    del bpy.types.Scene.blendermcp_server_running
    del bpy.types.Scene.blendermcp_auto_start_server
    del bpy.types.Scene.blendermcp_use_polyhaven
    del bpy.types.Scene.blendermcp_use_sketchfab
    del bpy.types.Scene.blendermcp_sketchfab_api_key
    del bpy.types.Scene.blendermcp_use_nd

    print("BlenderMCP addon unregistered")


if __name__ == "__main__":
    register()
