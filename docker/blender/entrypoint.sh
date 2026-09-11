#!/bin/bash
# Installs the bind-mounted addon source fresh on every start (so addon-side
# edits are picked up without rebuilding the image), then launches Blender
# under Xvfb and starts the BlenderMCP socket server via start_server.py.
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

# Blender's bundled Python block-buffers stdout when it isn't a TTY (which it
# never is under Docker), so print()s from the addon's background threads sit
# unflushed and never reach `docker logs`. PYTHONUNBUFFERED forces line
# buffering so logs appear in real time.
export PYTHONUNBUFFERED=1
exec blender --python /opt/start_server.py
