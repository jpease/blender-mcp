"""
Rows guarding the container rig's static guards.

Label prefixes: `docker:`, `entrypoint:`.
"""

from .common import COMPOSE, DOCKER_START, DOCKERFILE, DOCKERIGNORE, DOCKT, ENTRYPOINT, HEALTHCHECK, Revert

ROWS: list[Revert] = [
    # --- the container rig's static guards ---
    Revert(
        "docker: the build arg the entrypoint reads is no longer exported as ENV",
        DOCKERFILE,
        "ENV BLENDER_MAJOR_MINOR=${BLENDER_MAJOR_MINOR}\n",
        "",
        (f"{DOCKT}::test_the_entrypoint_installs_the_addon_for_the_blender_the_image_downloads",),
    ),
    Revert(
        "docker: an unset Blender version no longer aborts the container",
        ENTRYPOINT,
        'BLENDER_MAJOR_MINOR="${BLENDER_MAJOR_MINOR:?must be set by the image (see Dockerfile ENV)}"',
        'BLENDER_MAJOR_MINOR="${BLENDER_MAJOR_MINOR}"',
        (f"{DOCKT}::test_entrypoint_fails_loudly_when_the_version_is_missing",),
    ),
    Revert(
        "docker: the Blender download URL hardcodes a version instead of interpolating it",
        DOCKERFILE,
        'RUN wget -q "https://download.blender.org/release/Blender${BLENDER_MAJOR_MINOR}/'
        'blender-${BLENDER_VERSION}-linux-x64.tar.xz" \\\n'
        "        -O /tmp/blender.tar.xz \\\n"
        "    && tar xf /tmp/blender.tar.xz -C /opt \\\n"
        "    && rm /tmp/blender.tar.xz \\\n"
        '    && ln -s "/opt/blender-${BLENDER_VERSION}-linux-x64/blender" /usr/local/bin/blender',
        'RUN wget -q "https://download.blender.org/release/Blender5.2/blender-5.2.1-linux-x64.tar.xz" \\\n'
        "        -O /tmp/blender.tar.xz \\\n"
        "    && tar xf /tmp/blender.tar.xz -C /opt \\\n"
        "    && rm /tmp/blender.tar.xz \\\n"
        '    && ln -s "/opt/blender-5.2.1-linux-x64/blender" /usr/local/bin/blender',
        (f"{DOCKT}::test_dockerfile_installs_the_blender_version_it_declares",),
    ),
    Revert(
        "docker: the advertised output root is not the path ./output is mounted at",
        COMPOSE,
        "      - ./output:/output",
        "      - ./renders:/output",
        (f"{DOCKT}::test_compose_declares_the_mounted_output_root",),
    ),
    Revert(
        "docker: the MCP port is published on every interface",
        COMPOSE,
        '      - "127.0.0.1:8000:8000"',
        '      - "8000:8000"',
        (f"{DOCKT}::test_compose_publishes_only_on_loopback",),
    ),
    Revert(
        "docker: Blender's own socket is published too",
        COMPOSE,
        '      - "127.0.0.1:8000:8000"',
        '      - "127.0.0.1:8000:8000"\n      - "127.0.0.1:9876:9876"',
        (f"{DOCKT}::test_compose_exposes_the_mcp_server_but_not_blender",),
    ),
    Revert(
        "docker: the MCP server listens on a port compose does not publish",
        ENTRYPOINT,
        "BLENDERMCP_HTTP_PORT=8000",
        "BLENDERMCP_HTTP_PORT=8001",
        (f"{DOCKT}::test_entrypoint_serves_the_mcp_server_over_http_on_the_published_port",),
    ),
    Revert(
        "entrypoint: the container asks for a transport compose's published port never serves",
        ENTRYPOINT,
        "BLENDERMCP_TRANSPORT=http",
        "BLENDERMCP_TRANSPORT=stdio",
        (f"{DOCKT}::test_entrypoint_serves_the_mcp_server_over_http_on_the_published_port",),
    ),
    Revert(
        "entrypoint: the readiness gate is gone, so the MCP server starts before Blender",
        ENTRYPOINT,
        "if ! wait_for_blender; then\n    terminate_children\n    exit 1\nfi\n\n",
        "",
        (f"{DOCKT}::test_entrypoint_waits_for_blender_before_starting_the_mcp_server",),
    ),
    Revert(
        # A reply, not the command it answers, is the readiness signal: a connect that the kernel
        # accepts into the backlog before the addon serves must not count.
        "entrypoint: the readiness gate stops round-tripping Blender's socket",
        ENTRYPOINT,
        "            if sock.recv(256):\n",
        "            if True:\n",
        (f"{DOCKT}::test_entrypoint_waits_for_blender_before_starting_the_mcp_server",),
    ),
    Revert(
        "entrypoint: a bare wait is back, so Xvfb keeps a dead container alive",
        ENTRYPOINT,
        '    for pid in "$blender_pid" "$mcp_pid" "$xvfb_pid" "$readiness_pid"; do\n'
        '        wait "$pid" 2>/dev/null || true\n'
        "    done",
        "wait || true",
        (f"{DOCKT}::test_entrypoint_waits_only_on_the_processes_it_started",),
    ),
    Revert(
        "docker: Blender's socket bound to all interfaces",
        DOCKER_START,
        "BlenderMCPServer(port=port)",
        'BlenderMCPServer(host="0.0.0.0", port=port)',
        (f"{DOCKT}::test_blender_keeps_its_socket_on_loopback",),
    ),
    Revert(
        "docker: dependencies resolved rather than installed from poetry.lock",
        DOCKERFILE,
        "poetry install --only main --no-root --no-interaction",
        "poetry install --no-root --no-interaction",
        (f"{DOCKT}::test_server_dependencies_are_installed_from_the_poetry_lock",),
    ),
    Revert(
        "docker: the build context excludes a file the Dockerfile COPYs",
        DOCKERIGNORE,
        "!docker/blender/healthcheck.py\n",
        "",
        (f"{DOCKT}::test_build_context_ignore_file_admits_everything_the_dockerfile_copies",),
    ),
    Revert(
        "docker: the healthcheck stops round-tripping Blender's socket",
        HEALTHCHECK,
        "        return bool(sock.recv(256))\n",
        "        return True\n",
        (f"{DOCKT}::test_compose_healthcheck_round_trips_both_blender_and_the_mcp_server",),
    ),
    Revert(
        "docker: the compose toolsets key is mistyped",
        COMPOSE,
        "      BLENDER_MCP_TOOLSETS: shot",
        "      BLENDER_MCP_TOOLSET: shot",
        (f"{DOCKT}::test_compose_pins_a_toolset_selection_the_server_can_resolve",),
    ),
    Revert(
        "docker: the compose toolsets value does not resolve",
        COMPOSE,
        "      BLENDER_MCP_TOOLSETS: shot",
        "      BLENDER_MCP_TOOLSETS: shto",
        (f"{DOCKT}::test_compose_pins_a_toolset_selection_the_server_can_resolve",),
    ),
]
