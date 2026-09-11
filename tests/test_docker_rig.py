"""
Static guards for the headless-Blender Docker rig.

The files in docker/blender only work as a unit: the Dockerfile decides
which Blender is installed, and entrypoint.sh has to install the addon into
*that* version's addons directory. Nothing at runtime catches a mismatch - the
addon simply never loads - so the wiring is asserted here.
"""

import re

from pathlib import Path

DOCKER_DIR = Path(__file__).resolve().parent.parent / "docker" / "blender"
DOCKERFILE = DOCKER_DIR / "Dockerfile"
ENTRYPOINT = DOCKER_DIR / "entrypoint.sh"
COMPOSE = DOCKER_DIR / "docker-compose.yml"


def _dockerfile_args() -> set[str]:
    return set(re.findall(r"^ARG\s+([A-Z_][A-Z0-9_]*)", DOCKERFILE.read_text(), re.MULTILINE))


def _dockerfile_envs() -> set[str]:
    return set(re.findall(r"^ENV\s+([A-Z_][A-Z0-9_]*)", DOCKERFILE.read_text(), re.MULTILINE))


def _entrypoint_variables() -> set[str]:
    return set(re.findall(r"\$\{([A-Z_][A-Z0-9_]*)", ENTRYPOINT.read_text()))


def test_build_args_the_entrypoint_reads_are_exported_as_env() -> None:
    """
    A bare ARG is build-time only, so the entrypoint would read an empty value.

    This is the exact drift that silently installs the addon into the wrong
    Blender version's directory.
    """
    shared = _dockerfile_args() & _entrypoint_variables()
    assert shared, "expected the entrypoint to consume at least one build arg"
    missing = shared - _dockerfile_envs()
    assert not missing, f"build args read at runtime but never exported via ENV: {sorted(missing)}"


def test_entrypoint_does_not_hardcode_its_own_blender_version() -> None:
    """A second hardcoded default is what lets the two files drift apart."""
    fallbacks = re.findall(r"\$\{BLENDER_MAJOR_MINOR:-([^}]*)\}", ENTRYPOINT.read_text())
    assert not fallbacks, (
        f"entrypoint.sh carries its own BLENDER_MAJOR_MINOR default ({fallbacks}); "
        "the version must come from the image so it cannot disagree with the installed Blender"
    )


def test_entrypoint_fails_loudly_when_the_version_is_missing() -> None:
    """An unset version must stop the container, not install into a guessed path."""
    text = ENTRYPOINT.read_text()
    assert re.search(r"\$\{BLENDER_MAJOR_MINOR:\?", text), (
        "entrypoint.sh should use ${BLENDER_MAJOR_MINOR:?...} so an unset version aborts"
    )


def test_dockerfile_installs_the_blender_version_it_declares() -> None:
    """The download URL must be driven by the args, not a pasted literal."""
    text = DOCKERFILE.read_text()
    assert "${BLENDER_MAJOR_MINOR}" in text and "${BLENDER_VERSION}" in text, (
        "the Blender download should interpolate both version args"
    )


def test_compose_declares_the_mounted_output_root() -> None:
    """
    The agent learns where to write from BLENDERMCP_OUTPUT_ROOTS.

    It has to name the same path the volume mounts, or renders land somewhere
    the host never sees.
    """
    text = COMPOSE.read_text()
    assert "BLENDERMCP_OUTPUT_ROOTS" in text, "compose must advertise the writable output root"
    root = re.search(r"BLENDERMCP_OUTPUT_ROOTS:\s*(\S+)", text).group(1)
    assert re.search(rf"-\s*\./output:{re.escape(root)}\b", text), (
        f"BLENDERMCP_OUTPUT_ROOTS={root} is not the path ./output is mounted at"
    )


def _published_ports() -> list[tuple[str, str, str]]:
    """
    List every port mapping in the compose file.

    Returns:
        list[tuple[str, str, str]]: (host bind, host port, container port) per mapping.

    """
    return re.findall(r'-\s*"((?:[^":]+:)?)(\d+):(\d+)"', COMPOSE.read_text())


def test_compose_publishes_only_on_loopback() -> None:
    """
    Neither the MCP server nor Blender has authentication.

    Publishing as "8000:8000" binds every host interface, which puts scene
    control plus file read/write in reach of anyone on the network.
    """
    published = _published_ports()
    assert published, "expected the compose file to publish the MCP server's port"
    for host_part, host_port, _container_port in published:
        assert host_part in {"127.0.0.1:", "localhost:"}, (
            f"port published as {host_part}{host_port}, which binds all interfaces; bind it to 127.0.0.1"
        )


def test_compose_exposes_the_mcp_server_but_not_blender() -> None:
    """
    The container models a remote host: clients reach the MCP server, never Blender.

    Blender's raw socket is the server's private upstream; publishing it would
    reopen the unauthenticated path the co-located server exists to hide.
    """
    container_ports = {container for _host, _port, container in _published_ports()}
    assert container_ports == {"8000"}, f"expected only the MCP port 8000 published, got {sorted(container_ports)}"


def test_entrypoint_serves_the_mcp_server_over_http_on_the_published_port() -> None:
    """The server must speak HTTP, on the one port compose forwards."""
    text = ENTRYPOINT.read_text()
    assert "BLENDERMCP_TRANSPORT=http" in text, "the container's MCP server must use the HTTP transport"
    assert "BLENDERMCP_HTTP_PORT=8000" in text, "the server must listen on the port compose publishes"


def test_blender_keeps_its_socket_on_loopback() -> None:
    """Only the MCP server listens beyond the container's loopback."""
    assert "0.0.0.0" not in (DOCKER_DIR / "start_server.py").read_text(), (
        "start_server.py must not bind Blender's socket to all interfaces"
    )


def _copied_sources() -> set[str]:
    """
    List every build-context file the Dockerfile COPYs (multi-stage copies excluded).

    Returns:
        set[str]: The source paths of every COPY instruction.

    """
    sources: set[str] = set()
    for line in re.findall(r"^COPY\s+(?!--from)(.+)$", DOCKERFILE.read_text(), re.MULTILINE):
        *srcs, _dest = line.split()
        sources.update(srcs)
    return sources


def test_server_dependencies_are_installed_from_the_poetry_lock() -> None:
    """
    The image must run the exact versions CI locks and tests.

    Resolving from pyproject.toml alone picks whatever is newest at build time,
    so the container silently drifts from poetry.lock.
    """
    text = DOCKERFILE.read_text()
    assert "poetry.lock" in _copied_sources(), "the image must copy poetry.lock to install from it"
    assert re.search(r"poetry install\b.*--only main", text), (
        "the image should `poetry install --only main` so only the locked runtime dependencies are installed"
    )


def test_build_context_ignore_file_admits_everything_the_dockerfile_copies() -> None:
    """The allowlist must track the COPYs, or the build fails on a missing file."""
    allowed = {
        line[1:] for line in (DOCKER_DIR / "Dockerfile.dockerignore").read_text().splitlines() if line.startswith("!")
    }
    copied = _copied_sources()
    assert copied, "expected the Dockerfile to COPY files from the build context"
    assert copied <= allowed, f"COPY sources excluded from the build context: {sorted(copied - allowed)}"


def test_compose_healthcheck_round_trips_both_blender_and_the_mcp_server() -> None:
    """
    Docker accepts on a published port before the container is listening.

    A connect-only check would report healthy while Blender is still starting,
    so the healthcheck has to send a command to each process and require a reply.
    """
    assert "healthcheck:" in COMPOSE.read_text(), "compose needs a healthcheck so `up --wait` can gate on readiness"
    assert "/opt/healthcheck.py" in COMPOSE.read_text().split("healthcheck:", 1)[1]
    script = (DOCKER_DIR / "healthcheck.py").read_text()
    assert '"type": "ping"' in script and "recv" in script, "should require a reply from Blender's socket"
    assert '"initialize"' in script and "8000/mcp" in script, "should require the MCP server to answer initialize"
