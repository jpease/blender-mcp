"""
Static guards for the headless-Blender Docker rig.

The files in docker/blender only work as a unit: the Dockerfile decides
which Blender is installed, and entrypoint.sh has to install the addon into
*that* version's addons directory. Nothing at runtime catches a mismatch - the
addon simply never loads - so the wiring is asserted here.

Every environment-variable name is imported from the code that reads it rather
than retyped. A guard that carries its own copy of the name it exists to keep in
sync cannot detect a rename, which is the drift it was written for.
"""

import re
import subprocess

from pathlib import Path

import pytest

from conftest import load_addon_source_module

from blender_mcp.server.bundles import TOOLSETS_ENV_VAR, resolve_toolset_modules
from blender_mcp.server.cli import HTTP_HOST_ENV, HTTP_ONLY_ENVS, TRANSPORT_ENV, transport_from_env

DOCKER_DIR = Path(__file__).resolve().parent.parent / "docker" / "blender"
DOCKERFILE = DOCKER_DIR / "Dockerfile"
ENTRYPOINT = DOCKER_DIR / "entrypoint.sh"
COMPOSE = DOCKER_DIR / "docker-compose.yml"

# What the shell guards below allow a bounded teardown, and how long they give it
# before calling the run hung. Short enough to keep the suite quick, long enough
# that an emulated or loaded machine does not fail on scheduling noise alone.
GRACE_SECONDS = 3
HARNESS_TIMEOUT_SECONDS = 20


def _output_roots_env_var() -> str:
    """
    Read the addon's own name for the output-roots variable, straight from source.

    Imported from the file rather than the package: `bundled/addon/__init__.py`
    imports `bpy`, which does not exist outside Blender.

    Returns:
        str: The environment variable the addon reads its roots from.

    """
    module = load_addon_source_module("output_roots.py", "addon_output_roots_for_docker_guard")
    return str(module.OUTPUT_ROOTS_ENV_VAR)


def _dockerfile_args() -> set[str]:
    """
    List the Dockerfile's build args.

    Returns:
        set[str]: Every name declared with ARG.

    """
    return set(re.findall(r"^ARG\s+([A-Z_][A-Z0-9_]*)", DOCKERFILE.read_text(), re.MULTILINE))


def _dockerfile_envs() -> set[str]:
    """
    List the variables the image exports to runtime.

    Returns:
        set[str]: Every name declared with ENV.

    """
    return set(re.findall(r"^ENV\s+([A-Z_][A-Z0-9_]*)", DOCKERFILE.read_text(), re.MULTILINE))


def _entrypoint_variables() -> set[str]:
    """
    List the variables the entrypoint expands at container start.

    Returns:
        set[str]: Every ${NAME} the entrypoint reads.

    """
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
    variable = _output_roots_env_var()
    assert variable in text, "compose must advertise the writable output root"
    root = re.search(rf"{variable}:\s*(\S+)", text).group(1)
    assert re.search(rf"-\s*\./output:{re.escape(root)}\b", text), (
        f"{variable}={root} is not the path ./output is mounted at"
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


def _entrypoint_transport_settings() -> dict[str, str]:
    """
    Read the transport variables the entrypoint sets on the MCP server.

    Returns:
        dict[str, str]: Each transport variable found, mapped to its value.

    """
    # Built from HTTP_ONLY_ENVS rather than a hand-listed few, so a variable the
    # server learns to read cannot be silently dropped from what this scrapes.
    # It was: the remote-bind opt-in was added to both the server and the
    # entrypoint while this regex still named three variables, so the scrape
    # handed transport_from_env a 0.0.0.0 bind with the consent stripped out and
    # the test failed on a contract the container in fact satisfied.
    names = "|".join((TRANSPORT_ENV, *HTTP_ONLY_ENVS))
    return dict(re.findall(rf"({names})=(\S+)", ENTRYPOINT.read_text()))


def test_entrypoint_serves_the_mcp_server_over_http_on_the_published_port() -> None:
    """
    The server must speak HTTP, on the one port compose forwards.

    Checked by running the server's own `transport_from_env` over the values the
    entrypoint sets, rather than by matching the literal `BLENDERMCP_TRANSPORT=http`:
    the container came up unhealthy once with that literal present and no code on
    `main` that could read it, so a text match here proved only that the container
    half of the contract was written down.
    """
    config = transport_from_env(_entrypoint_transport_settings())
    assert config.transport == "streamable-http", (
        f"the container's MCP server must use the HTTP transport, not {config.transport}"
    )
    published = {container for _host, _port, container in _published_ports()}
    assert {str(config.port)} == published, (
        f"the server would listen on {config.port}, but compose publishes {sorted(published)}"
    )


def test_entrypoint_waits_for_blender_before_starting_the_mcp_server() -> None:
    """
    The MCP server makes one connection attempt at start-up and never retries.

    Started beside a Blender that has not opened its socket yet it throws the
    addon handshake away, and under emulation that is the normal case rather than
    a race: Blender takes minutes. A fixed sleep would only be a longer guess, so
    the gate has to round-trip a real command the way the healthcheck does.
    """
    text = ENTRYPOINT.read_text()
    gate = text.index("if ! wait_for_blender; then")
    assert text.index("blender --python") < gate < text.index(f"{TRANSPORT_ENV}="), (
        "the readiness gate must sit between Blender's launch and the MCP server's"
    )
    readiness = text[: text.index(f"{TRANSPORT_ENV}=")]
    assert '"type": "ping"' in readiness and "recv" in readiness, (
        "the gate must require a reply from Blender's socket; a TCP connect is accepted before the addon can answer"
    )


def test_entrypoint_waits_only_on_the_processes_it_started() -> None:
    """
    A bare `wait` waits for every child, and Xvfb never exits on its own.

    That is what let the container sit `Up (unhealthy)` indefinitely with both
    Blender and the MCP server dead - the exact opposite of the claim the line
    above it makes.
    """
    waits = re.findall(r"^\s*wait\b(.*)$", ENTRYPOINT.read_text(), re.MULTILINE)
    assert waits, "expected the entrypoint to wait on the processes it started"
    for arguments in waits:
        assert re.search(r'"\$\w+"', arguments), (
            f"`wait{arguments}` names no pid, so it blocks on every child including Xvfb"
        )


def _entrypoint_functions() -> str:
    """
    Lift every shell function out of the entrypoint so a test can run the real one.

    The guards below are behavioural: they execute the entrypoint's own teardown
    and readiness code rather than matching text, because the defects they cover
    (an unbounded wait, an undeliverable trap) are timing properties that a
    correct-looking script exhibits anyway. Defining a function is side-effect
    free, so all of them are sourced and the test calls the one it is about.

    Returns:
        str: The definitions, in file order, ready to paste into a bash script.

    """
    blocks = re.findall(r"^[a-z_]+\(\) \{\n.*?^\}$", ENTRYPOINT.read_text(), re.MULTILINE | re.DOTALL)
    assert blocks, "expected entrypoint.sh to define its start-up and teardown steps as functions"
    return "\n".join(blocks)


def _entrypoint_teardown_steps() -> str:
    """
    Lift the steps the entrypoint runs once a tracked process has exited.

    Read as a slice of the script rather than by function name: the defect is
    that *this sequence* could not finish, and naming the function that fixes it
    would let a future rewrite drop the bound and still pass.

    Returns:
        str: Everything between the `wait -n` line and the final `exit`.

    """
    text = ENTRYPOINT.read_text()
    after_wait = text.index("\n", text.index('wait -n "$blender_pid"')) + 1
    return text[after_wait : text.index('exit "$status"', after_wait)]


def _run_shell(script: str) -> subprocess.CompletedProcess[str]:
    """
    Run a bash script, treating a run that does not finish as the failure it is.

    Args:
        script: The script to run.

    Returns:
        subprocess.CompletedProcess[str]: The finished run, stdout and stderr captured.

    """
    try:
        return subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            timeout=HARNESS_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as expired:
        pytest.fail(
            f"the entrypoint's own code did not finish within {HARNESS_TIMEOUT_SECONDS}s.\n"
            f"stdout: {(expired.stdout or b'').decode(errors='replace')}\n"
            f"stderr: {(expired.stderr or b'').decode(errors='replace')}"
        )


def test_teardown_finishes_even_when_a_child_ignores_sigterm(tmp_path: Path) -> None:
    """
    SIGTERM is a request. Teardown has to be able to stop asking and insist.

    Blender wedged in a C call, or holding its own handler, keeps running after
    SIGTERM, and an unbounded `wait` then parks on exactly the process that has
    just proved it will not leave. Because the container is exiting on its own
    rather than being stopped, no Docker grace period rescues it: it sits `Up
    (unhealthy)` with the MCP server dead - the state the signal-forwarding above
    it exists to prevent. So the sequence is run here against a child that
    ignores SIGTERM, and is required to finish and to have killed it.

    Args:
        tmp_path: Where the stand-in child reports that it is wedged.

    """
    wedged = tmp_path / "ignoring-sigterm"
    result = _run_shell(f"""
        set -euo pipefail
        {_entrypoint_functions()}
        SHUTDOWN_GRACE_SECONDS={GRACE_SECONDS}
        blender_pid=
        mcp_pid=
        xvfb_pid=
        readiness_pid=
        status=0
        ( trap '' TERM; touch {wedged}; while :; do sleep 0.2; done ) &
        blender_pid=$!
        # Not optional: a SIGTERM that arrives before the child has installed the
        # ignore kills it on the default disposition, and the run then proves
        # nothing. Wait for the child to say it is wedged before tearing down.
        while [ ! -f {wedged} ]; do sleep 0.1; done
        {_entrypoint_teardown_steps()}
        kill -0 "$blender_pid" 2>/dev/null && echo WEDGED_CHILD_SURVIVED || echo WEDGED_CHILD_GONE
        echo "TEARDOWN_RETURNED $status"
    """)
    assert "TEARDOWN_RETURNED 0" in result.stdout, (
        f"teardown did not run to completion.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "WEDGED_CHILD_GONE" in result.stdout, (
        "teardown returned while the child that ignored SIGTERM was still running, "
        f"so nothing escalated to SIGKILL.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_teardown_costs_nothing_when_every_child_has_already_gone() -> None:
    """
    The escalation must be a cap on a wedged teardown, not a delay on every one.

    Also covers the start-up window, where a pid is still unset: with `set -u`
    every one of them is an empty word, and teardown has to walk that list
    without crashing and without waiting out the grace period for processes that
    were never started.
    """
    result = _run_shell(f"""
        set -euo pipefail
        {_entrypoint_functions()}
        SHUTDOWN_GRACE_SECONDS={GRACE_SECONDS}
        blender_pid=
        mcp_pid=
        xvfb_pid=
        readiness_pid=
        status=0
        before=$SECONDS
        {_entrypoint_teardown_steps()}
        echo "TEARDOWN_TOOK $((SECONDS - before))"
    """)
    assert "SIGKILL" not in result.stderr, f"teardown escalated with nothing left to kill: {result.stderr}"
    took = re.search(r"TEARDOWN_TOOK (\d+)", result.stdout)
    assert took, f"teardown crashed on unset pids.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    assert int(took.group(1)) < GRACE_SECONDS, (
        f"teardown spent {took.group(1)}s on children that had already gone; "
        "the grace period is a bound on a wedged shutdown, not a cost on every one"
    )


def test_a_stop_signal_during_the_readiness_wait_is_handled_at_once(tmp_path: Path) -> None:
    """
    Bash defers a trap until the current *foreground* command returns.

    The readiness gate is allowed to take BLENDER_READY_TIMEOUT_SECONDS - 600 by
    default, because Blender starts slowly under emulation - so waiting for it in
    the foreground made `docker stop` reach nothing for minutes. Docker's own 10s
    grace expires first and SIGKILL takes Blender, Xvfb and the server with no
    cleanup, which is exactly what the forwarded signal exists to avoid. The gate
    is run here against a probe that never answers, with a stop signal arriving a
    second in, and the handler has to run then rather than at the deadline.

    Args:
        tmp_path: Where the stand-in probe that never answers is written.

    """
    probe = tmp_path / "probe-that-never-answers"
    # Reads the heredoc the entrypoint pipes in, then stalls. stdout is dropped so
    # this stand-in cannot hold the harness's pipe open past the run.
    probe.write_text(f"#!/bin/sh\nexec >/dev/null 2>&1\ncat >/dev/null\nsleep {HARNESS_TIMEOUT_SECONDS * 2}\n")
    probe.chmod(0o755)
    result = _run_shell(f"""
        set -uo pipefail
        {_entrypoint_functions()}
        MCP_PYTHON={probe}
        BLENDER_SOCKET_PORT=9876
        BLENDER_READY_TIMEOUT_SECONDS={HARNESS_TIMEOUT_SECONDS * 2}
        SHUTDOWN_GRACE_SECONDS={GRACE_SECONDS}
        blender_pid=
        mcp_pid=
        xvfb_pid=
        readiness_pid=
        trap 'echo "STOP_HANDLED_AT $SECONDS"; terminate_children' TERM
        sleep {HARNESS_TIMEOUT_SECONDS * 2} &
        blender_pid=$!
        ( sleep 1; kill -TERM $$ ) &
        wait_for_blender || echo "GATE_GAVE_UP_AT $SECONDS"
    """)
    handled = re.search(r"STOP_HANDLED_AT (\d+)", result.stdout)
    assert handled, (
        "the stop signal was never handled: the readiness wait is not interruptible.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert int(handled.group(1)) <= GRACE_SECONDS, (
        f"the stop signal was handled {handled.group(1)}s in, not when it arrived; "
        "scaled to the real deadline that is minutes of ignoring `docker stop`"
    )
    assert re.search(r"GATE_GAVE_UP_AT (\d+)", result.stdout), (
        "the readiness gate did not return after the stop signal, so the container "
        f"would still sit out the deadline.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )


def _comment_above(needle: str) -> str:
    """
    Read the comment block immediately above a line of the entrypoint.

    Args:
        needle: Text identifying the line the comment explains.

    Returns:
        str: The contiguous comment lines preceding it.

    """
    lines = ENTRYPOINT.read_text().splitlines()
    index = next(number for number, line in enumerate(lines) if needle in line and not line.startswith("#"))
    block: list[str] = []
    while index > 0 and lines[index - 1].lstrip().startswith("#"):
        index -= 1
        block.append(lines[index])
    return "\n".join(reversed(block))


def test_binding_all_interfaces_records_the_publish_that_makes_it_safe() -> None:
    """
    The MCP server binds 0.0.0.0, and nothing in this file keeps that private.

    What does is one line in another file: compose's loopback publish. Run the
    image any other way - `docker run -p 8000:8000`, or `-P` - and the bind hands
    an unauthenticated Blender driver to the network. The reason the bind is safe
    therefore has to be written where the bind is, naming the mapping it depends
    on, or the next person to write a `docker run` has no way to know.
    """
    host_part, host_port, container_port = _published_ports()[0]
    note = _comment_above(f"{HTTP_HOST_ENV}=0.0.0.0")
    assert f"{host_part}{host_port}:{container_port}" in note, (
        f"the 0.0.0.0 bind is safe only because compose publishes {host_part}{host_port}:{container_port}; "
        f"say so where the bind is, not only in the compose file. Comment found:\n{note}"
    )
    assert "docker run -p" in note, (
        f"the note should name how this goes wrong - publishing the port with no host part. Comment found:\n{note}"
    )


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


def test_compose_pins_a_toolset_selection_the_server_can_resolve() -> None:
    """
    The rig's whole claim is that it exercises the shot pipeline's surface.

    That rests on one environment variable spelled correctly in a YAML file:
    a typo in the key leaves the server on its small `core` default, and a typo
    in the value stops it starting at all. Neither is visible without a running
    container, so both are checked here against the code that reads them.
    """
    text = COMPOSE.read_text()
    assert f"{TOOLSETS_ENV_VAR}:" in text, f"compose must pin the tool selection through {TOOLSETS_ENV_VAR}"
    selection = re.search(rf"{TOOLSETS_ENV_VAR}:\s*(\S+)", text).group(1)
    modules = resolve_toolset_modules(selection)
    assert modules, f"{TOOLSETS_ENV_VAR}={selection} resolves to no tool modules"
