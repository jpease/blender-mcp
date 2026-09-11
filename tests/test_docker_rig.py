"""
Static guards for the headless-Blender Docker rig.

The three files in docker/blender only work as a unit: the Dockerfile decides
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


def test_compose_publishes_the_socket_on_loopback_only() -> None:
    """
    The MCP protocol has no authentication.

    Publishing as "9876:9876" binds every host interface, which puts scene
    control plus file read/write in reach of anyone on the network.
    """
    published = re.findall(r'-\s*"([^"]*:)?(\d+):9876"', COMPOSE.read_text())
    assert published, "expected the compose file to publish the MCP port"
    for host_part, _container_port in published:
        assert host_part in {"127.0.0.1:", "localhost:"}, (
            f"MCP port published as {host_part or ''}<port>:9876, which binds all interfaces; bind it to 127.0.0.1"
        )


def test_compose_healthcheck_does_a_real_protocol_round_trip() -> None:
    """
    Docker accepts on a published port before the container is listening.

    A connect-only check would report healthy while Blender is still starting,
    so the healthcheck has to send a command and require a reply.
    """
    text = COMPOSE.read_text()
    assert "healthcheck:" in text, "compose needs a healthcheck so `up --wait` can gate on readiness"
    healthcheck = text.split("healthcheck:", 1)[1]
    assert '"type": "ping"' in healthcheck, "the healthcheck should send a real MCP command"
    assert "recv" in healthcheck, "the healthcheck should require a reply, not just a connection"
