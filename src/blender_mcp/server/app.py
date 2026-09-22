"""The shared FastMCP app instance and server lifespan management."""

import logging

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.fastmcp import FastMCP

from .. import __version__
from ..addon_manager import check_addon_status_on_startup, format_handshake_log
from .connection import disconnect_blender, get_blender_connection, get_last_handshake

logger = logging.getLogger("BlenderMCPServer")


@asynccontextmanager
async def server_lifespan(_server: FastMCP) -> AsyncIterator[dict[str, Any]]:
    """
    Manage server startup and shutdown lifecycle.

    Args:
        _server: FastMCP instance supplied by the SDK.

    Yields:
        AsyncIterator[dict[str, Any]]: Result produced by the operation.

    """
    # We don't need to create a connection here since we're using the global connection
    # for resources and tools

    try:
        logger.info("BlenderMCP server starting up")

        try:
            status = check_addon_status_on_startup()
            if status.needs_action:
                logger.warning(status.message)
            elif status.message:
                logger.info(status.message)
        except Exception as e:
            logger.debug(f"Addon status check skipped: {e}")

        # Try to connect to Blender on startup to verify it's available
        try:
            # This will initialize the global connection if needed
            get_blender_connection()
            logger.info("Successfully connected to Blender on startup")
            handshake = get_last_handshake()
            if handshake and not handshake.up_to_date:
                logger.warning(format_handshake_log(handshake))
        except Exception as e:
            logger.warning(f"Could not connect to Blender on startup: {e!s}")
            logger.warning("Make sure the Blender addon is running before using Blender resources or tools")

        # Return an empty context - we're using the global connection
        yield {}
    finally:
        # Clean up the global connection on shutdown
        disconnect_blender()
        logger.info("BlenderMCP server shut down")


SERVER_INSTRUCTIONS = """
Every tool below returns one of two shapes:

1. Most tools return a single dict envelope:
   {"ok": bool, "data": ..., "error": None, "warnings": [...], "changed_objects": [...],
    "changed_resources": [...]}
   - Check "ok" before trusting anything else. "ok" is False when the request reached
     Blender but produced no effect - for example an ND tool the user cancelled
     interactively (Esc) - in which case "error" is still None (this is not a transport
     failure) and "warnings" explains why nothing changed.
   - "changed_objects" lists Blender *objects* created/modified/deleted by the call.
     "changed_resources" lists non-object datablocks touched (materials, images, worlds,
     node groups). A name appearing in one of these means it actually changed - not
     merely that it was in the request.
   - "warnings" carries non-fatal notices, most commonly that a topology-changing
     operation invalidated vertex/edge/face indices returned by an earlier
     get_mesh_data call - call get_mesh_data again before reusing indices.
   - A tool-specific failure Blender rejects (bad name, invalid input) raises an MCP
     tool error instead of returning ok:false - stop and fix the input rather than retrying
     the same call.

2. get_viewport_screenshot, get_sketchfab_model_preview, render_lighting_preview,
   render_pbr_material_preview, and inspect_render_output
   return image content followed by the same ok() dict described above. A lighting preview
   returns inline images in CYCLES/EEVEE order for engines without explicit output paths;
   fully explicit output paths return only the envelope. PBR previews likewise return inline images
   for engines without explicit paths, followed by their envelope. Read the final item for metadata
   and warnings rather than inspecting only the image content.
   - get_viewport_screenshot is a live viewport capture, not a render - it will not match
     final render output (engine, lighting, color management). render_lighting_preview and
     render_pbr_material_preview do render, but a disposable staging scene, not the user's
     actual one. render_scene renders the user's actual scene but only writes files to disk
     and never returns pixels itself - only its written-path/size/status metadata. To actually
     see render_scene's pixels, call inspect_render_output(output_path=<its "last_file", or one
     of its detail=true "files" paths>) afterward, or call it with no arguments to read the
     in-memory Render Result directly (which only ever holds the most recently rendered frame).

For any tool exposing limit/offset parameters, pagination metadata is inside the envelope's "data"
dict. Continue with the returned "next_offset" while "truncated" is true. Independent limit/offset
pairs page independent result sets. A validation tool that reports truncation without a continuation
offset must be rerun with a narrower object or collection scope. Catalog tools without limit/offset
return only the provider-bounded result set; do not assume the result is a complete provider catalog.

Replies are bounded. A reply returns what changed, the identifiers needed to find the rest, and
the facts the call asked for; full state is a `detail=true` request, not the default. Every reply
is also capped at 8 KiB, so the largest page of records in it may arrive shortened - the warning
says so and names the offset to continue from. Identifiers are never dropped to make room: a
shortened page still carries whole records, and the names of what changed stay complete.

Before editing, inspect the scene (list_scene_objects, get_object_info, get_mesh_data)
rather than assuming which object is active or selected. Prefer non-destructive tools
(live modifiers, ND) over apply=True/cleanup tools, which are irreversible from this
server's perspective even though Blender's own undo history can still revert them
locally.

Animation is authored into one named action: pass the same `action_name` to
`keyframe_object_transform` and the pose tools, because an ID holds one action and a second one
silently stops the first driving the rig. The pose and transform keyers take many frames per call
(`keyframe_character_pose(keys=[{"frame": ..., "poses": [...]}, ...])`), so a stride is one call.
`set_scene_frame` is how any inspection tool or screenshot is pointed at another frame - they all
report the current frame, so without it you are reviewing frame 1 forever. A contact that must hold
still while the body moves over it - a planted foot, a hand on a prop - is held by
`keyframe_bone_reach`, which re-solves the IK against the evaluated body pose at each frame;
repeated FK rotation slides it instead.

Object names after a library override: a name shared with the linked original resolves to the
editable override. A name linked from several libraries with no local object is refused with each
library's session_uid - override one with create_override. On an overridden rig a bare custom-property
write is not durable: Blender records an override for a pose bone's transform channels and nothing
for a bare ID property write, so the value reverts to the library's on reopen even when the source
file defines it. Keying the property instead survives, because the action is local data - a property
the source rig does not define at all is lost either way. The pose tools warn when a call bare-writes
one.

Tool parameter naming follows three conventions, so a name is guessable rather than something to
fail once and learn: a tool that creates a new object of some kind names that kind parameter
`<noun>_type` (light_type, primitive_type, domain_type), never bare `type`. A tool that creates a
new object always takes `collection_name` explicitly - it is looked up or created, never defaulted
from scene state. A tool that patches fields on an object that already exists (set_object_transform,
configure_light, configure_camera, ...) takes one nested argument named `patch` (or a named group
such as `optics`/`display`) instead of flat top-level keywords - read that argument's own schema for
its field list before calling.
""".strip()

# Create the MCP server with lifespan support
mcp = FastMCP("BlenderMCP", lifespan=server_lifespan, instructions=SERVER_INSTRUCTIONS)
# FastMCP 1.x does not expose its low-level server's version in the public
# constructor. Set it explicitly so MCP initialize responses advertise this
# package's version instead of falling back to the unrelated MCP SDK version.
mcp._mcp_server.version = __version__
