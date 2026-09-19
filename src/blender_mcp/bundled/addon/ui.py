import bpy

from . import ADDON_ID
from .helpers import get_blendermcp_addon_preferences
from .server_core import BlenderMCPServer


class BLENDERMCP_AddonPreferences(bpy.types.AddonPreferences):
    """Expose BlenderMCP configuration in Blender's addon preferences."""

    bl_idname = ADDON_ID

    # Blender's property idiom: the annotation is a call, which is invalid as a
    # type expression but is how bpy.props declares RNA properties.
    sketchfab_api_key: bpy.props.StringProperty(  # pyright: ignore[reportInvalidTypeForm]
        name="Sketchfab API Key",
        subtype="PASSWORD",
        description="Persistent Sketchfab API Key",
        default="",
    )

    def draw(self, context) -> None:
        layout = self.layout

        layout.label(text="Persistent API Credentials:", icon="LOCKED")
        cred_box = layout.box()
        cred_box.prop(self, "sketchfab_api_key", text="Sketchfab API Key")


# Blender UI Panel
class BLENDERMCP_PT_Panel(bpy.types.Panel):
    """Render BlenderMCP controls in the 3D View sidebar."""

    bl_label = "Blender MCP"
    bl_idname = "BLENDERMCP_PT_Panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "BlenderMCP"

    def draw(self, context) -> None:
        layout = self.layout
        scene = context.scene
        prefs = get_blendermcp_addon_preferences(context)

        layout.prop(scene, "blendermcp_port")
        layout.prop(scene, "blendermcp_use_polyhaven", text="Use assets from Poly Haven")

        layout.prop(scene, "blendermcp_use_sketchfab", text="Use assets from Sketchfab")
        if scene.blendermcp_use_sketchfab:
            if prefs:
                layout.prop(prefs, "sketchfab_api_key", text="API Key")
            else:
                layout.prop(scene, "blendermcp_sketchfab_api_key", text="API Key")

        layout.prop(
            scene,
            "blendermcp_use_nd",
            text="Use ND (non-destructive hard-surface tools)",
        )

        if not scene.blendermcp_server_running:
            layout.operator("blendermcp.start_server", text="Connect to MCP server")
        else:
            layout.operator("blendermcp.stop_server", text="Disconnect from MCP server")
            layout.label(text=f"Running on port {scene.blendermcp_port}")

        # Feedback section
        layout.separator()
        feedback_box = layout.box()

        col = feedback_box.column(align=True)
        col.label(text="Feedback", icon="URL")
        col.label(text="bit.ly/blender-mcp-form")
        col.separator()
        col.label(text="Schedule a call", icon="URL")
        col.label(text="bit.ly/blender-mcp-call")
        col.label(text="(we'll credit you in the repo!)")


# Operator to start the server
class BLENDERMCP_OT_StartServer(bpy.types.Operator):
    """Start the BlenderMCP server from the Blender interface."""

    bl_idname = "blendermcp.start_server"
    bl_label = "Connect to Claude"
    bl_description = "Start the BlenderMCP server to connect with Claude"

    def execute(self, context):
        scene = context.scene

        # Create a new server instance
        if not hasattr(bpy.types, "blendermcp_server") or not bpy.types.blendermcp_server:
            bpy.types.blendermcp_server = BlenderMCPServer(port=scene.blendermcp_port)

        # Start the server
        bpy.types.blendermcp_server.start()
        scene.blendermcp_server_running = bpy.types.blendermcp_server.running

        return {"FINISHED"}


# Operator to stop the server
class BLENDERMCP_OT_StopServer(bpy.types.Operator):
    """Stop the BlenderMCP server from the Blender interface."""

    bl_idname = "blendermcp.stop_server"
    bl_label = "Stop the connection to Claude"
    bl_description = "Stop the connection to Claude"

    def execute(self, context):
        scene = context.scene

        # Stop the server if it exists
        if hasattr(bpy.types, "blendermcp_server") and bpy.types.blendermcp_server:
            bpy.types.blendermcp_server.stop()
            del bpy.types.blendermcp_server

        scene.blendermcp_server_running = False

        return {"FINISHED"}
