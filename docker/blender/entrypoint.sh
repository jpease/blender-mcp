#!/bin/bash
# Models a "remote" Blender host: headless Blender plus the blender-mcp server
# beside it. Blender's socket stays on the container's loopback; the only way in
# from outside is the MCP server's streamable-HTTP port.
#
# The addon and server both run from the read-only /repo bind mount, so edits to
# either are picked up on restart without rebuilding the image.
set -euo pipefail

# Baked into the image by the Dockerfile. Deliberately has no default here:
# a second hardcoded version is exactly what drifts from the installed Blender,
# and the failure is silent (the addon just never loads). Abort instead.
BLENDER_MAJOR_MINOR="${BLENDER_MAJOR_MINOR:?must be set by the image (see Dockerfile ENV)}"
ADDONS_DIR="$HOME/.config/blender/${BLENDER_MAJOR_MINOR}/scripts/addons"

mkdir -p "$ADDONS_DIR"
rm -rf "$ADDONS_DIR/blender_mcp"
cp -r /repo/src/blender_mcp/bundled/addon "$ADDONS_DIR/blender_mcp"
rm -rf "$ADDONS_DIR/blender_mcp/__pycache__"

Xvfb :99 -screen 0 1280x720x24 &
export DISPLAY=:99
sleep 2

# Python block-buffers stdout when it isn't a TTY (which it never is under
# Docker), so print()s from background threads sit unflushed and never reach
# `docker logs`. PYTHONUNBUFFERED forces line buffering so logs appear in real
# time. It applies to both Blender's bundled Python and the MCP server.
export PYTHONUNBUFFERED=1

blender --python /opt/start_server.py &
blender_pid=$!

# Binds 0.0.0.0 only because Docker's port forward arrives on the container's
# external interface; compose publishes it on the host's loopback alone. The
# server's first Blender connection retries while Blender finishes starting.
BLENDERMCP_TRANSPORT=http BLENDERMCP_HTTP_HOST=0.0.0.0 BLENDERMCP_HTTP_PORT=8000 \
    PYTHONPATH=/repo/src PYTHONDONTWRITEBYTECODE=1 \
    /opt/blender-mcp/venv/bin/python -c "from blender_mcp.server import main; main()" &
mcp_pid=$!

# Forward `docker stop` to both processes; bash as PID 1 would otherwise ignore it.
trap 'kill -TERM "$blender_pid" "$mcp_pid" 2>/dev/null || true' TERM INT

# Either process exiting takes the container down with it, so a dead Blender is
# never hidden behind an MCP port that still answers.
status=0
wait -n "$blender_pid" "$mcp_pid" || status=$?
kill -TERM "$blender_pid" "$mcp_pid" 2>/dev/null || true
wait || true
exit "$status"
