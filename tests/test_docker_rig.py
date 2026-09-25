"""
Guards for the headless-Blender Docker rig, run without Docker.

The files in docker/blender only work together: the entrypoint must install the addon
for the Blender version the Dockerfile installs, and a mismatch fails silently at
runtime. Each guard runs the rig's own code where it can - the entrypoint's shell
functions, its start-up sequence, the Dockerfile's download step, the healthcheck and
`start_server.py` - against stand-ins for Blender and the MCP server, and otherwise reads
the Dockerfile and compose file as the instructions and settings they declare.
Environment-variable names are imported from the code that reads them, so a rename
cannot slip past a retyped copy.
"""

import contextlib
import http.server
import json
import os
import re
import runpy
import shlex
import socket
import string
import subprocess
import sys
import threading
import types

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from conftest import load_addon, load_addon_source_module

from blender_mcp.server.app import mcp
from blender_mcp.server.bundles import TOOLSETS_ENV_VAR, resolve_toolset_modules
from blender_mcp.server.cli import transport_from_env
from blender_mcp.server.connection import DEFAULT_PORT

DOCKER_DIR = Path(__file__).resolve().parent.parent / "docker" / "blender"
DOCKERFILE = DOCKER_DIR / "Dockerfile"
ENTRYPOINT = DOCKER_DIR / "entrypoint.sh"
COMPOSE = DOCKER_DIR / "docker-compose.yml"
HEALTHCHECK = DOCKER_DIR / "healthcheck.py"
START_SERVER = DOCKER_DIR / "start_server.py"

# How long the shell tests below give a teardown, and how long before calling a run
# hung. Loose enough that a loaded or emulated machine does not fail on scheduling noise.
GRACE_SECONDS = 3
HARNESS_TIMEOUT_SECONDS = 20

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

# A Blender release other than the one the Dockerfile pins, as `--build-arg` would ask for.
_OVERRIDDEN_BLENDER = {"BLENDER_MAJOR_MINOR": "4.9", "BLENDER_VERSION": "4.9.3"}


def _output_roots_module() -> types.ModuleType:
    """
    Load the addon's output-roots reader straight from its source file.

    The addon package imports `bpy`, which does not exist outside Blender.

    Returns:
        types.ModuleType: The module that parses the output-roots variable.

    """
    return load_addon_source_module("output_roots.py", "addon_output_roots_for_docker_guard")


def _dockerfile_instructions() -> list[tuple[str, str]]:
    """
    Read the Dockerfile as the instructions `docker build` runs.

    Returns:
        list[tuple[str, str]]: (instruction, arguments) in file order, continuation lines
        joined and comments dropped.

    """
    instructions: list[tuple[str, str]] = []
    pending = ""
    for line in DOCKERFILE.read_text().splitlines():
        stripped = line.strip()
        if not pending and (not stripped or stripped.startswith("#")):
            continue
        if stripped.endswith("\\"):
            pending += stripped[:-1] + " "
            continue
        keyword, _, arguments = (pending + stripped).partition(" ")
        instructions.append((keyword.upper(), arguments.strip()))
        pending = ""
    return instructions


def _build_args(**overrides: str) -> dict[str, str]:
    """
    Resolve the build args the way `docker build --build-arg` does.

    Args:
        **overrides: Build args given on the command line.

    Returns:
        dict[str, str]: Every declared ARG, at its default unless overridden.

    """
    declared = {
        name: default
        for name, _, default in (
            arguments.partition("=") for keyword, arguments in _dockerfile_instructions() if keyword == "ARG"
        )
    }
    return declared | overrides


def _image_environment(build_args: dict[str, str]) -> dict[str, str]:
    """
    List what the image exports to every container it starts.

    Args:
        build_args: The args the image was built with.

    Returns:
        dict[str, str]: Each ENV, with its `${NAME}` references resolved.

    """
    environment: dict[str, str] = {}
    for keyword, arguments in _dockerfile_instructions():
        if keyword == "ENV":
            name, value = arguments.split("=", 1)
            environment[name] = string.Template(value).substitute(build_args | environment)
    return environment


def _blender_install(build_args: dict[str, str]) -> dict[str, list[str]]:
    """
    Run the Dockerfile's Blender download step with its commands stubbed out.

    The step runs in `sh` with the build args in its environment, as `docker build`
    runs a RUN; `wget`, `tar`, `rm` and `ln` only report the arguments they were given.

    Args:
        build_args: The args the image is built with.

    Returns:
        dict[str, list[str]]: Each command the step ran, mapped to its arguments.

    """
    [step] = [
        arguments
        for keyword, arguments in _dockerfile_instructions()
        if keyword == "RUN" and "download.blender.org" in arguments
    ]
    stubs = "".join(f'{command}() {{ echo "{command} $*"; }}\n' for command in ("wget", "tar", "rm", "ln"))
    result = subprocess.run(
        ["sh", "-c", stubs + step],
        env={"PATH": os.environ["PATH"], **build_args},
        capture_output=True,
        text=True,
        timeout=HARNESS_TIMEOUT_SECONDS,
        check=True,
    )
    return {command: arguments for command, *arguments in (line.split() for line in result.stdout.splitlines())}


def _installed_major_minor(build_args: dict[str, str]) -> str:
    """
    Name the Blender release line the download step installs, which is where Blender looks for addons.

    Args:
        build_args: The args the image is built with.

    Returns:
        str: `major.minor` of the Blender the `blender` command runs.

    """
    _flag, target, _link = _blender_install(build_args)["ln"]
    version = Path(target).parent.name.removeprefix("blender-").removesuffix("-linux-x64")
    return ".".join(version.split(".")[:2])


def _copies() -> list[tuple[list[str], str]]:
    """
    List every build-context copy the Dockerfile makes (multi-stage copies excluded).

    Returns:
        list[tuple[list[str], str]]: Each COPY's sources and its destination.

    """
    copies: list[tuple[list[str], str]] = []
    for keyword, arguments in _dockerfile_instructions():
        if keyword == "COPY" and not arguments.startswith("--from"):
            *sources, destination = arguments.split()
            copies.append((sources, destination))
    return copies


def _compose_entries(key: str) -> list[str]:
    """
    Read one setting of the compose file's only service, comments dropped.

    Args:
        key: The service-level key, such as `ports` or `environment`.

    Returns:
        list[str]: The key's list items or mapping lines, stripped.

    """
    lines = COMPOSE.read_text().splitlines()
    entries: list[str] = []
    for line in lines[lines.index(f"    {key}:") + 1 :]:
        content = line.strip()
        if not content or content.startswith("#"):
            continue
        if len(line) - len(line.lstrip()) <= len("    "):
            break
        entries.append(content)
    return entries


def _compose_environment() -> dict[str, str]:
    """
    Read the environment compose starts the container with.

    Returns:
        dict[str, str]: Each variable mapped to its value.

    """
    return {
        name.strip(): value.strip()
        for name, value in (entry.split(":", 1) for entry in _compose_entries("environment"))
    }


def _compose_volumes() -> list[tuple[str, str, list[str]]]:
    """
    Read the bind mounts compose gives the container.

    Returns:
        list[tuple[str, str, list[str]]]: (host path, container path, options) per mount.

    """
    volumes: list[tuple[str, str, list[str]]] = []
    for entry in _compose_entries("volumes"):
        host, container, *options = entry.removeprefix("- ").split(":")
        volumes.append((host, container, options))
    return volumes


def _published_ports() -> list[tuple[str, str, str]]:
    """
    Read every port mapping compose publishes.

    Returns:
        list[tuple[str, str, str]]: (host address, host port, container port) per mapping;
        the address is empty when the mapping names none, which binds every interface.

    """
    published: list[tuple[str, str, str]] = []
    for entry in _compose_entries("ports"):
        *address, host_port, container_port = entry.removeprefix("- ").strip('"').split(":")
        published.append((":".join(address), host_port, container_port))
    return published


def _entrypoint_prologue() -> str:
    """
    Lift everything the entrypoint runs before it first touches the filesystem.

    Its settings, function definitions and traps; nothing in it starts a process.

    Returns:
        str: The entrypoint up to the line that creates the addons directory.

    """
    text = ENTRYPOINT.read_text()
    return text[: text.index('\nmkdir -p "$ADDONS_DIR"')]


def _entrypoint_functions() -> str:
    """
    Lift every shell function out of the entrypoint so a test can run the real one.

    The defects these tests cover, an unbounded wait or a delayed trap, are timing
    behaviors that correct-looking text can still have. Defining functions has no side
    effects, so all are sourced.

    Returns:
        str: The definitions, in file order, ready to paste into a bash script.

    """
    blocks = re.findall(r"^[a-z_]+\(\) \{\n.*?^\}$", ENTRYPOINT.read_text(), re.MULTILINE | re.DOTALL)
    assert blocks, "expected entrypoint.sh to define its start-up and teardown steps as functions"
    return "\n".join(blocks)


def _entrypoint_startup_steps() -> str:
    """
    Lift the entrypoint's start-up sequence, from Blender's launch to the MCP server's.

    Returns:
        str: Everything from `blender --python` through the line that records the MCP server's pid.

    """
    text = ENTRYPOINT.read_text()
    end = text.index("mcp_pid=$!")
    return text[text.index("blender --python") : end + len("mcp_pid=$!")]


def _entrypoint_teardown_steps() -> str:
    """
    Lift the steps the entrypoint runs once a tracked process has exited.

    Sliced from the script, not taken by function name, so a rewrite that drops the
    bound from this sequence cannot still pass.

    Returns:
        str: Everything between the `wait -n` line and the final `exit`.

    """
    text = ENTRYPOINT.read_text()
    after_wait = text.index("\n", text.index('wait -n "$blender_pid"')) + 1
    return text[after_wait : text.index('exit "$status"', after_wait)]


def _run_shell(script: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """
    Run a bash script, treating a run that does not finish as the failure it is.

    Args:
        script: The script to run.
        env: The environment to run it in; the test process's own when omitted.

    Returns:
        subprocess.CompletedProcess[str]: The finished run, stdout and stderr captured.

    """
    try:
        return subprocess.run(
            ["bash", "-c", script],
            env=env,
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


def _addons_directory_run(environment: dict[str, str], home: Path) -> subprocess.CompletedProcess[str]:
    """
    Run the entrypoint's prologue in a container environment and print where it installs the addon.

    Args:
        environment: What the container starts with, beyond PATH and HOME.
        home: The container user's home directory.

    Returns:
        subprocess.CompletedProcess[str]: The run; its stdout is the addons directory, if it got that far.

    """
    return _run_shell(
        _entrypoint_prologue() + '\nprintf "%s\\n" "$ADDONS_DIR"\n',
        env={"PATH": os.environ["PATH"], "HOME": str(home), **environment},
    )


@contextlib.contextmanager
def _blender_stand_in(*, answers: bool) -> Iterator[int]:
    """
    Listen on a free loopback port in place of Blender's addon socket.

    Each request is read in full, so the client's send has landed before anything else
    happens, and then either answered the way the addon answers a command or hung up on.

    Args:
        answers: Reply to each request, or close without a word, as a socket the kernel
            has accepted before the addon is serving it does.

    Yields:
        int: The port it listens on.

    """
    listener = socket.create_server(("127.0.0.1", 0))
    listener.settimeout(0.1)
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            try:
                connection, _address = listener.accept()
            except TimeoutError:
                continue
            with connection:
                connection.settimeout(HARNESS_TIMEOUT_SECONDS)
                if connection.recv(4096) and answers:
                    connection.sendall(b'{"status": "success", "result": {}}\n')

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield listener.getsockname()[1]
    finally:
        stop.set()
        thread.join()
        listener.close()


class _McpStandInHandler(http.server.BaseHTTPRequestHandler):
    """Answer only what opens an MCP session - an `initialize` at the streamable-HTTP path - as FastMCP does."""

    def do_POST(self) -> None:
        """Accept an `initialize` request and refuse every other one."""
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        opens_a_session = self.path == mcp.settings.streamable_http_path and body.get("method") == "initialize"
        self.send_response(200 if opens_a_session else 400)
        self.send_header("Content-Length", "0")
        self.end_headers()


@contextlib.contextmanager
def _mcp_stand_in() -> Iterator[int]:
    """
    Serve `_McpStandInHandler` on a free loopback port in place of the MCP server.

    Yields:
        int: The port it listens on.

    """
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _McpStandInHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def _start_the_container(tmp_path: Path, blender_port: int) -> subprocess.CompletedProcess[str]:
    """
    Run the entrypoint's start-up sequence with Blender and the MCP server stood in for.

    Blender is a process that only sleeps. The MCP server's interpreter prints the
    environment it was started with instead of serving; the readiness probe the same
    interpreter runs is passed to this one, so it really dials `blender_port`.

    Args:
        tmp_path: Where the stand-in interpreter is written.
        blender_port: Where the readiness probe dials Blender.

    Returns:
        subprocess.CompletedProcess[str]: The run; see `_mcp_server_environment`.

    """
    interpreter = tmp_path / "python"
    python = shlex.quote(sys.executable)
    interpreter.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "-" ]; then exec {python} "$@"; fi\n'
        f"exec {python} -c 'import json, os; print(\"MCP_SERVER_STARTED\", json.dumps(dict(os.environ)))'\n"
    )
    interpreter.chmod(0o755)
    return _run_shell(
        f"""
        set -euo pipefail
        {_entrypoint_functions()}
        MCP_PYTHON={interpreter}
        BLENDER_SOCKET_PORT={blender_port}
        BLENDER_READY_TIMEOUT_SECONDS=2
        SHUTDOWN_GRACE_SECONDS={GRACE_SECONDS}
        blender_pid=
        mcp_pid=
        xvfb_pid=
        readiness_pid=
        blender() {{ exec sleep {HARNESS_TIMEOUT_SECONDS} >/dev/null 2>&1; }}
        {_entrypoint_startup_steps()}
        wait "$mcp_pid"
        terminate_children
        """,
        env={"PATH": os.environ["PATH"], "HOME": str(tmp_path)},
    )


def _mcp_server_environment(run: subprocess.CompletedProcess[str]) -> dict[str, str] | None:
    """
    Read the environment the stand-in MCP server reported, if it was started at all.

    Args:
        run: `_start_the_container`'s result.

    Returns:
        dict[str, str] | None: The server's environment, or None when it never started.

    """
    for line in run.stdout.splitlines():
        if line.startswith("MCP_SERVER_STARTED "):
            return json.loads(line.removeprefix("MCP_SERVER_STARTED "))
    return None


def test_the_entrypoint_installs_the_addon_for_the_blender_the_image_downloads(tmp_path: Path) -> None:
    """
    The entrypoint installs the addon where the image's own Blender looks for it.

    A bare ARG is build-time only, so without its ENV the entrypoint would read no
    version at all; and a `--build-arg` override must move the addon with the Blender,
    or the addon silently never loads.

    Args:
        tmp_path: The container user's home directory.

    """
    for overrides in ({}, _OVERRIDDEN_BLENDER):
        build_args = _build_args(**overrides)
        run = _addons_directory_run(_image_environment(build_args), tmp_path)
        expected = f"{tmp_path}/.config/blender/{_installed_major_minor(build_args)}/scripts/addons\n"
        assert run.stdout == expected, (
            f"built with {overrides or 'the default args'}, the entrypoint installs the addon at "
            f"{run.stdout.strip() or 'nowhere'}, not where that Blender reads addons.\nstderr: {run.stderr}"
        )


def test_entrypoint_fails_loudly_when_the_version_is_missing(tmp_path: Path) -> None:
    """
    An unset or empty version stops the container before it installs anything.

    Otherwise the addon goes into a guessed path such as `blender//scripts/addons`.

    Args:
        tmp_path: The container user's home directory.

    """
    for environment in ({}, {"BLENDER_MAJOR_MINOR": ""}):
        run = _addons_directory_run(environment, tmp_path)
        assert run.returncode != 0 and not run.stdout, (
            f"with {environment or 'no version'} the entrypoint went on to install into {run.stdout.strip()}"
        )


def test_dockerfile_installs_the_blender_version_it_declares() -> None:
    """The download step fetches and links the Blender release the build args name, not a pasted literal."""
    install = _blender_install(_build_args(**_OVERRIDDEN_BLENDER))
    major_minor, version = _OVERRIDDEN_BLENDER["BLENDER_MAJOR_MINOR"], _OVERRIDDEN_BLENDER["BLENDER_VERSION"]

    [url] = [argument for argument in install["wget"] if argument.startswith("https://")]
    tarball = f"blender-{version}-linux-x64.tar.xz"
    assert url == f"https://download.blender.org/release/Blender{major_minor}/{tarball}"
    unpacked_into = install["tar"][install["tar"].index("-C") + 1]
    assert install["ln"][1] == f"{unpacked_into}/{tarball.removesuffix('.tar.xz')}/blender", (
        "the `blender` command must run the release the step just unpacked"
    )


def test_compose_declares_the_mounted_output_root() -> None:
    """
    Every output root the addon is told about is the container side of the `./output` mount.

    Otherwise the agent writes renders somewhere the host never sees.
    """
    roots = _output_roots_module().configured_roots(_compose_environment())
    assert roots, "compose must advertise the writable output root"
    writable_mounts = {container: host for host, container, options in _compose_volumes() if "ro" not in options}
    assert all(writable_mounts.get(root) == "./output" for root in roots), (
        f"advertised roots {roots} are not all where ./output is mounted: {writable_mounts}"
    )


def test_compose_publishes_only_on_loopback() -> None:
    """Ports publish on loopback only, because neither the MCP server nor Blender has authentication."""
    published = _published_ports()
    assert published, "expected the compose file to publish the MCP server's port"
    for address, host_port, _container_port in published:
        assert address in LOOPBACK_HOSTS, (
            f"port {host_port} is published on {address or 'every interface'}; bind it to 127.0.0.1"
        )


def test_compose_exposes_the_mcp_server_but_not_blender() -> None:
    """
    Compose publishes the MCP server's port, never Blender's.

    Blender's socket is unauthenticated, and the MCP server is its only intended client.
    """
    container_ports = {container for _address, _port, container in _published_ports()}
    assert container_ports == {"8000"}, f"expected only the MCP port 8000 published, got {sorted(container_ports)}"


def test_entrypoint_serves_the_mcp_server_over_http_on_the_published_port(tmp_path: Path) -> None:
    """
    The server the entrypoint starts speaks HTTP on the one port compose forwards.

    Read from the environment the started process actually receives, through the
    server's own `transport_from_env`.

    Args:
        tmp_path: Where the stand-in interpreter is written.

    """
    with _blender_stand_in(answers=True) as blender_port:
        run = _start_the_container(tmp_path, blender_port)
    environment = _mcp_server_environment(run)
    assert environment is not None, f"the MCP server never started.\nstdout: {run.stdout}\nstderr: {run.stderr}"

    config = transport_from_env(environment)
    assert config.transport == "streamable-http", (
        f"the container's MCP server must use the HTTP transport, not {config.transport}"
    )
    published = {container for _address, _port, container in _published_ports()}
    assert {str(config.port)} == published, (
        f"the server would listen on {config.port}, but compose publishes {sorted(published)}"
    )


def test_entrypoint_waits_for_blender_before_starting_the_mcp_server(tmp_path: Path) -> None:
    """
    The entrypoint starts the MCP server only once Blender's addon has answered.

    The server tries its Blender connection once at startup and never retries, and under
    emulation Blender takes minutes to start, so a fixed sleep would only be a longer guess.
    A socket that accepts and hangs up is what the kernel offers before the addon serves,
    so it must not count as ready.

    Args:
        tmp_path: Where the stand-in interpreter is written.

    """
    with _blender_stand_in(answers=False) as blender_port:
        unanswered = _start_the_container(tmp_path, blender_port)
    assert _mcp_server_environment(unanswered) is None, (
        f"the MCP server started although Blender never answered.\nstdout: {unanswered.stdout}"
    )
    assert unanswered.returncode != 0, "a Blender that never answers must fail the container, not leave it running"

    with _blender_stand_in(answers=True) as blender_port:
        answered = _start_the_container(tmp_path, blender_port)
    assert _mcp_server_environment(answered) is not None, (
        f"Blender answered, but the MCP server never started.\nstdout: {answered.stdout}\nstderr: {answered.stderr}"
    )


def test_entrypoint_waits_only_on_the_processes_it_started() -> None:
    """
    Teardown waits only on the processes the entrypoint tracks.

    A bare `wait` also waits on every other child - Xvfb never exits on its own - leaving
    the container `Up (unhealthy)` with Blender and the MCP server dead.
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
        sleep {GRACE_SECONDS * 3} >/dev/null 2>&1 &
        untracked_pid=$!
        before=$SECONDS
        {_entrypoint_teardown_steps()}
        echo "TEARDOWN_TOOK $((SECONDS - before))"
        kill "$untracked_pid" 2>/dev/null || true
    """)
    took = re.search(r"TEARDOWN_TOOK (\d+)", result.stdout)
    assert took, f"teardown did not run to completion.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    assert int(took.group(1)) < GRACE_SECONDS, f"teardown spent {took.group(1)}s waiting on a child it never started"


def test_teardown_finishes_even_when_a_child_ignores_sigterm(tmp_path: Path) -> None:
    """
    Teardown kills a child that ignores SIGTERM, and finishes.

    A wedged Blender would otherwise keep an unbounded `wait` parked. The container is
    exiting on its own, so no Docker grace period steps in, and it sits
    `Up (unhealthy)` with the MCP server dead.

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
    Teardown with every child already gone, or never started, is immediate.

    With `set -u`, an unset pid must not crash teardown or make it wait out the grace
    period.
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
    A stop signal during the readiness wait is handled at once.

    Bash runs a trap only after the foreground command returns, and the gate may wait
    600 s by default. In the foreground, `docker stop` would reach nothing until Docker's
    10 s grace ran out and SIGKILL took every process without cleanup.

    Args:
        tmp_path: Where the stand-in probe that never answers is written.

    """
    probe = tmp_path / "probe-that-never-answers"
    # Reads the heredoc the entrypoint pipes in, then stalls. Its output goes to
    # /dev/null so it cannot hold the harness's pipe open.
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


def test_blender_keeps_its_socket_on_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    `start_server.py` starts the real addon server on loopback: only the MCP server listens beyond it.

    Run against the bundled addon with the server's `start` recorded instead of binding.

    Args:
        monkeypatch: Installs the fake Blender and the addon under the name Blender gives it.

    """
    scene = types.SimpleNamespace(blendermcp_port=4321, blendermcp_server_running=False)
    addon, bpy = load_addon(monkeypatch, scene=scene)
    enabled: list[str] = []

    def addon_enable(*, module: str) -> None:
        enabled.append(module)

    bpy.ops.preferences = types.SimpleNamespace(addon_enable=addon_enable)
    server_core = sys.modules[f"{addon.__name__}.server_core"]
    started: list[tuple[str, int]] = []

    def start(server: Any) -> None:
        started.append((server.host, server.port))
        server.running = True

    monkeypatch.setattr(server_core.BlenderMCPServer, "start", start)
    monkeypatch.setitem(sys.modules, "blender_mcp.server_core", server_core)

    runpy.run_path(str(START_SERVER))

    assert enabled == ["blender_mcp"]
    [(host, port)] = started
    assert host in LOOPBACK_HOSTS, f"start_server.py binds Blender's socket to {host}"
    assert port == scene.blendermcp_port
    assert scene.blendermcp_server_running is True


def test_server_dependencies_are_installed_from_the_poetry_lock() -> None:
    """
    The image installs only the locked runtime dependencies.

    Resolving from pyproject.toml alone would drift from the versions CI tests, and the
    dev group has no business in the image.
    """
    [step] = [
        shlex.split(arguments) for keyword, arguments in _dockerfile_instructions() if "poetry install" in arguments
    ]
    commands: list[list[str]] = [[]]
    for token in step:
        if token == "&&":
            commands.append([])
        else:
            commands[-1].append(token)
    directory = next(command[1] for command in commands if command[0] == "cd")
    [install] = [
        command
        for command in commands
        if "install" in command and command[command.index("install") - 1].endswith("poetry")
    ]

    assert any(
        "poetry.lock" in sources and destination.rstrip("/") == directory for sources, destination in _copies()
    ), f"poetry installs in {directory}, but poetry.lock is not copied there"
    options = install[install.index("install") + 1 :]
    assert "--only" in options and options[options.index("--only") + 1] == "main", (
        f"`poetry install {' '.join(options)}` installs more than the locked runtime group"
    )


def test_build_context_ignore_file_admits_everything_the_dockerfile_copies() -> None:
    """The allowlist must track the COPYs, or the build fails on a missing file."""
    allowed = {
        line[1:] for line in (DOCKER_DIR / "Dockerfile.dockerignore").read_text().splitlines() if line.startswith("!")
    }
    copied = {source for sources, _destination in _copies() for source in sources}
    assert copied, "expected the Dockerfile to COPY files from the build context"
    assert copied <= allowed, f"COPY sources excluded from the build context: {sorted(copied - allowed)}"


# Runs `healthcheck.py` unmodified in a child interpreter whose loopback connections go only
# to the stand-ins: argv is the script, then (port the script dials, stand-in port) pairs. Any
# other address is refused, so the check can never reach a real Blender.
_REDIRECTED_HEALTHCHECK = """
import runpy, socket, sys
ports = [int(port) for port in sys.argv[2:]]
redirect = {("127.0.0.1", dialled): ("127.0.0.1", served) for dialled, served in zip(ports[::2], ports[1::2])}
direct = socket.create_connection
def create_connection(address, *args, **kwargs):
    if address not in redirect:
        raise ConnectionRefusedError(f"nothing stands in for {address}")
    return direct(redirect[address], *args, **kwargs)
socket.create_connection = create_connection
runpy.run_path(sys.argv[1], run_name="__main__")
"""


def _healthcheck_exit_status(redirect: dict[int, int]) -> int:
    """
    Run the container healthcheck with its loopback ports sent to stand-ins.

    Args:
        redirect: Each port the healthcheck may dial, mapped to the stand-in serving it.

    Returns:
        int: The healthcheck's exit status; 0 is healthy.

    """
    ports = [str(port) for pair in redirect.items() for port in pair]
    return subprocess.run(
        [sys.executable, "-c", _REDIRECTED_HEALTHCHECK, str(HEALTHCHECK), *ports],
        # `no_proxy` keeps urllib off any proxy this machine configures, which the redirect would refuse.
        env={"PATH": os.environ["PATH"], "no_proxy": "*"},
        capture_output=True,
        timeout=HARNESS_TIMEOUT_SECONDS,
        check=False,
    ).returncode


def test_compose_healthcheck_round_trips_both_blender_and_the_mcp_server() -> None:
    """
    The healthcheck compose runs passes only when both Blender and the MCP server answer.

    An open port does not mean either is serving yet: Blender must reply on the port the
    MCP server dials, and the MCP server must open a session on the port compose publishes.
    """
    [test] = [
        json.loads(entry.removeprefix("test:"))
        for entry in _compose_entries("healthcheck")
        if entry.startswith("test:")
    ]
    [installed_at] = [destination for sources, destination in _copies() if sources == ["docker/blender/healthcheck.py"]]
    assert test[0] == "CMD" and test[-1] == installed_at, f"compose's healthcheck {test} does not run {installed_at}"
    [mcp_port] = {int(container) for _address, _port, container in _published_ports()}

    with _blender_stand_in(answers=True) as blender, _mcp_stand_in() as server:
        healthy = _healthcheck_exit_status({DEFAULT_PORT: blender, mcp_port: server})
    with _blender_stand_in(answers=False) as blender, _mcp_stand_in() as server:
        blender_silent = _healthcheck_exit_status({DEFAULT_PORT: blender, mcp_port: server})
    with _blender_stand_in(answers=True) as blender:
        server_down = _healthcheck_exit_status({DEFAULT_PORT: blender})

    assert healthy == 0, "the healthcheck failed although both Blender and the MCP server answered"
    assert blender_silent != 0, "the healthcheck passed on a Blender socket that never replied"
    assert server_down != 0, "the healthcheck passed with no MCP server listening"


def test_compose_pins_a_toolset_selection_the_server_can_resolve() -> None:
    """
    Compose's tool selection uses the right variable name and a value the server resolves.

    A wrong key leaves the server on the `core` default, and a bad value stops it
    starting; neither shows without a running container.
    """
    environment = _compose_environment()
    assert TOOLSETS_ENV_VAR in environment, f"compose must pin the tool selection through {TOOLSETS_ENV_VAR}"
    modules = resolve_toolset_modules(environment[TOOLSETS_ENV_VAR])
    assert modules, f"{TOOLSETS_ENV_VAR}={environment[TOOLSETS_ENV_VAR]} resolves to no tool modules"
