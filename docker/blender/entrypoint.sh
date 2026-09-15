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

# Where the addon listens, and the same port the MCP server dials by default
# (server/connection.py's DEFAULT_PORT). Never published - see docker-compose.yml.
BLENDER_SOCKET_PORT=9876
# How long Blender may take to open that socket before the container gives up.
# Minutes rather than seconds because this image is linux/amd64 and runs under
# emulation on arm64 hosts, where Blender's start-up is an order of magnitude
# slower. Settable from compose so a slow host needs no image rebuild.
BLENDER_READY_TIMEOUT_SECONDS="${BLENDER_READY_TIMEOUT_SECONDS:-600}"
# How long a child may take to honour SIGTERM before teardown stops being
# polite. Only ever spent when something is already wedged, so it is a cap on
# how long the container takes to die, not a delay on the normal path.
SHUTDOWN_GRACE_SECONDS="${SHUTDOWN_GRACE_SECONDS:-10}"
MCP_PYTHON=/opt/blender-mcp/venv/bin/python

blender_pid=
mcp_pid=
xvfb_pid=
readiness_pid=

# Forward `docker stop` to every child; bash as PID 1 would otherwise ignore it.
# Xvfb is included, which teardown previously left out: measured 2026-09-15, an
# unsignalled Xvfb plus the bare `wait` this file used to end with is what kept a
# container "Up (unhealthy)" with both Blender and the MCP server already dead.
# Either half alone closes that; both are fixed, because each is wrong on its own.
# The pids are expanded unquoted on purpose: one that has not been started yet
# is an empty word and must vanish rather than become an empty argument, and it
# must never be a literal 0, which would signal the whole process group.
terminate_children() {
    # shellcheck disable=SC2086
    kill -TERM $blender_pid $mcp_pid $xvfb_pid $readiness_pid 2>/dev/null || true
}
trap terminate_children TERM INT

# Reap what terminate_children asked to leave, and never wait on it forever.
#
# SIGTERM is a request, not a guarantee: a Blender wedged in a C call, or one
# that installed its own handler, keeps running. `wait "$pid"` has no bound, so
# teardown used to park on exactly the process that had just proved it would not
# leave. Measured 2026-09-15 in this base image (bash 5.1.8), with a child that
# traps SIGTERM and stays up and an MCP server that exits 7: `wait -n` returned
# at 2s and the script was still alive 14s later, i.e. the container stays "Up
# (unhealthy)" with the MCP server dead - the very state the unsignalled-Xvfb
# fix above claimed to close. Nothing external rescues it, because the container
# is exiting on its own: `docker stop`'s grace period never applies.
#
# The bound is a background watchdog rather than `timeout`, which this minimal
# image does not ship, and rather than polling `kill -0`, which cannot tell a
# still-running child from one bash has not reaped yet. It is killed as soon as
# the reaping finishes, so the grace period costs nothing when children behave.
# Same escalation, and same reason for it, as scripts/blender_rig.py's _shut_down.
reap_children() {
    local watchdog_pid
    (
        sleep "$SHUTDOWN_GRACE_SECONDS"
        echo "entrypoint: a child ignored SIGTERM for ${SHUTDOWN_GRACE_SECONDS}s; sending SIGKILL" >&2
        # shellcheck disable=SC2086
        kill -KILL $blender_pid $mcp_pid $xvfb_pid $readiness_pid 2>/dev/null || true
    ) &
    watchdog_pid=$!
    # Wait only on the pids this script started, one at a time. A bare `wait`
    # waits for *every* child, and Xvfb never exits on its own: that is what used
    # to keep the container alive and merely unhealthy with Blender and the
    # server both dead. The watchdog is excluded here on purpose - waiting on it
    # would reintroduce the very delay it exists to impose on wedged children.
    for pid in "$blender_pid" "$mcp_pid" "$xvfb_pid" "$readiness_pid"; do
        wait "$pid" 2>/dev/null || true
    done
    kill -TERM "$watchdog_pid" 2>/dev/null || true
    wait "$watchdog_pid" 2>/dev/null || true
}

mkdir -p "$ADDONS_DIR"
rm -rf "$ADDONS_DIR/blender_mcp"
cp -r /repo/src/blender_mcp/bundled/addon "$ADDONS_DIR/blender_mcp"
rm -rf "$ADDONS_DIR/blender_mcp/__pycache__"

Xvfb :99 -screen 0 1280x720x24 &
xvfb_pid=$!
export DISPLAY=:99
sleep 2

# Python block-buffers stdout when it isn't a TTY (which it never is under
# Docker), so print()s from background threads sit unflushed and never reach
# `docker logs`. PYTHONUNBUFFERED forces line buffering so logs appear in real
# time. It applies to both Blender's bundled Python and the MCP server.
export PYTHONUNBUFFERED=1

# Block until Blender's addon answers a real command, or fail the container.
#
# The MCP server makes exactly ONE connection attempt at start-up and does not
# retry (server/connection.py's get_blender_connection raises on the first
# failure), so launching it beside a Blender that has not opened its socket yet
# throws the addon handshake away: the logs carry "Could not connect to Blender
# on startup" instead of the addon's protocol version, and the first tool call
# pays for the reconnect. Under emulation that is the normal case, not a race.
#
# A TCP connect is not enough - the kernel accepts into the listen backlog
# before the addon is ready to reply - so this round-trips a `ping`, the same
# check healthcheck.py makes. The deadline means a Blender that never starts
# fails the container instead of hanging it, and the liveness check means a
# Blender that has already died is reported immediately rather than at the
# deadline, so its own error is what shows in the logs.
blender_readiness_probe() {
    "$MCP_PYTHON" - "$BLENDER_SOCKET_PORT" "$BLENDER_READY_TIMEOUT_SECONDS" "$blender_pid" <<'PY'
import os
import socket
import sys
import time

port, timeout, pid = int(sys.argv[1]), float(sys.argv[2]), int(sys.argv[3])
deadline = time.monotonic() + timeout
while time.monotonic() < deadline:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            sock.sendall(b'{"id": "entrypoint", "type": "ping", "params": {}}\n')
            if sock.recv(256):
                print(f"entrypoint: Blender answered a ping on port {port}", flush=True)
                raise SystemExit(0)
    except OSError:
        pass
    try:
        os.kill(pid, 0)
    except OSError:
        raise SystemExit(f"entrypoint: Blender (pid {pid}) exited before it opened port {port}") from None
    time.sleep(1)
raise SystemExit(f"entrypoint: Blender did not answer a ping on port {port} within {timeout:.0f}s")
PY
}

# Run that probe in the background and block in `wait`, which is interruptible.
#
# Bash defers a trap until the current *foreground* command finishes, and the
# probe is a foreground process lasting up to BLENDER_READY_TIMEOUT_SECONDS (600
# by default), so `docker stop` during Blender's start-up reached nothing at all.
# Measured 2026-09-15 in this base image: with the handler installed, a SIGTERM
# sent 3s into a 20s foreground command ran the handler at 20s, not 3s. Scaled to
# the real deadline, Docker's 10s grace expires first and SIGKILL takes Blender,
# Xvfb and the server down with no cleanup - defeating the forwarding above.
#
# The readiness contract is unchanged: the same probe, round-tripping the same
# `ping`, with the same deadline and the same liveness check, still has to
# succeed before the MCP server is launched. Only who waits for it moves.
wait_for_blender() {
    local status=0
    blender_readiness_probe &
    readiness_pid=$!
    wait "$readiness_pid" || status=$?
    readiness_pid=
    return "$status"
}

blender --python /opt/start_server.py &
blender_pid=$!

if ! wait_for_blender; then
    terminate_children
    exit 1
fi

# Binds 0.0.0.0 only because Docker's port forward arrives on the container's
# external interface; compose publishes it on the host's loopback alone.
#
# That loopback publish is the *whole* reason this is safe, and it lives in a
# different file. docker-compose.yml maps `127.0.0.1:8000:8000`; run this image
# any other way - `docker run -p 8000:8000`, or `-P`, or a compose override that
# drops the host part - and 0.0.0.0 here becomes an unauthenticated Blender
# driver (53 tools under BLENDER_MCP_TOOLSETS=shot, including file read/write)
# reachable by anyone who can route to the host. There is no credential to stop
# them. Keep the publish on 127.0.0.1, or put a proxy that authenticates in front.
#
# BLENDERMCP_HTTP_ALLOW_REMOTE=1 is what makes the server accept that bind at all.
# The server refuses any non-loopback address unless this is set, precisely so a
# wildcard bind cannot happen by accident - an empty BLENDERMCP_HTTP_HOST used to
# be enough to get one. This is the one deployment where it is the right answer,
# because the container's own interface is the only address Docker's forward can
# arrive on, and the comment above is the argument for why that is contained. Do
# not copy this line to a host that is not behind a loopback publish.
BLENDERMCP_TRANSPORT=http BLENDERMCP_HTTP_HOST=0.0.0.0 BLENDERMCP_HTTP_PORT=8000 \
    BLENDERMCP_HTTP_ALLOW_REMOTE=1 \
    PYTHONPATH=/repo/src PYTHONDONTWRITEBYTECODE=1 \
    "$MCP_PYTHON" -c "from blender_mcp.server import main; main()" &
mcp_pid=$!

# Either tracked process exiting takes the container down with it, so a dead
# Blender is never hidden behind an MCP port that still answers.
status=0
wait -n "$blender_pid" "$mcp_pid" || status=$?
terminate_children
reap_children
exit "$status"
