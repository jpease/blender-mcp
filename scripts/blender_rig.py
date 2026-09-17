r"""
Drive a live GUI Blender over the addon's own socket, for tests that need a real event loop.

The addon will not start under `blender --background`, where timers never fire and
no command is dequeued, so the rig launches a GUI Blender. It is macOS only; on
Linux use the container rig (`docker/blender/docker-compose.yml`). The rig speaks
only the addon's socket protocol and runs no MCP server, so a scenario can call
addon commands such as `get_addon_info` but not MCP tools such as `get_addon_status`.

Usage:
    python scripts/blender_rig.py --work-dir <dir> --scenario <scenario.py> \
        [--blend name=path ...] [--blender-script <in_blender.py> ...] \
        [--port 0] [--timeout 60] [--scenario-timeout 300] [--command-timeout 180]

A scenario module defines `run(rig)` and drives the socket through `rig.send()`.
It fails by raising; a clean return is a pass. The rig prints `RIG PASSED` and exits
0, or prints `RIG FAILED` and exits 1. Example:

    def run(rig):
        assert rig.send("ping")["status"] == "success"

The scenario runs in this process, so it cannot touch `bpy`. Work that must happen
inside Blender, rather than arrive as a queued socket command, goes in a
`--blender-script`, which Blender runs after the bootstrap. Such a script finds the
work dir in `BLENDERMCP_RIG_WORK_DIR`, where the two halves exchange files.

By default the rig picks a free port, refuses a port that already has a listener,
and treats Blender as ready only when a ping succeeds and its bootstrap has written
a receipt with this run's nonce and port. Otherwise a developer's own running
Blender could answer in its place.

Blender runs with `--factory-startup`, and `BLENDER_USER_RESOURCES`,
`BLENDER_USER_SCRIPTS` and `TMPDIR` point into `--work-dir`, so its config, recent
files, autosaves and temp files stay out of the user's profile. Inherited `BLENDER*`,
`PYTHONPATH`, `PYTHONHOME` and `PYTHONSTARTUP` are dropped first, since any of them
can load other scripts or the wrong `blender_mcp` package. `BLENDERMCP_OUTPUT_ROOTS`
confines `.blend` file commands to the work dir; nothing confines other writes a
scenario asks Blender for, and the handshake still lists `~` as a writable root.

The rig writes only into a `--work-dir` that is new, empty, or carries its
`.blender-rig-owned` marker, and there replaces only `addons/blender_mcp` and its
`blends/` copies. It refuses a populated unmarked directory, so it never adopts a
real Blender resources root or a `startup/` tree Blender would execute.

Blender's combined output goes to `<work-dir>/blender.log`; its `RIG:` and
`RIG-BLENDER:` lines are echoed here, and failures quote the log's tail. Give this
process a stdout that something reads (a terminal, a file, `| tee`): if the echo
blocks, the rig stops draining Blender's pipe and Blender deadlocks in `write()`.
"""

import argparse
import contextlib
import json
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time

from collections.abc import Callable
from dataclasses import dataclass
from importlib import util as importlib_util
from pathlib import Path
from types import ModuleType
from typing import IO

_REPO_ROOT = Path(__file__).resolve().parents[1]
_BUNDLED_ADDON = _REPO_ROOT / "src" / "blender_mcp" / "bundled" / "addon"
_BLENDER = Path("/opt/homebrew/bin/blender")
_INSTALLED_ADDON_NAME = "blender_mcp"
# 0 asks the kernel for a free port. The addon's own 9876 is where a developer's
# running Blender usually listens.
_DEFAULT_PORT = 0
_DEFAULT_TIMEOUT_SECONDS = 60.0
_DEFAULT_SCENARIO_TIMEOUT_SECONDS = 300.0
# The real client's socket timeout (server/connection.py), so a stall reads as a
# stall rather than as a rig failure.
_DEFAULT_COMMAND_TIMEOUT_SECONDS = 180.0
_POLL_INTERVAL_SECONDS = 0.25
_READINESS_PROBE_TIMEOUT_SECONDS = 5.0
_SHUTDOWN_GRACE_SECONDS = 10.0
_RECEIPT_FILE_NAME = "rig_ready.json"
_LOG_FILE_NAME = "blender.log"
_OWNED_MARKER_NAME = ".blender-rig-owned"
_OWNED_MARKER_TEXT = "Created by scripts/blender_rig.py; this directory is rebuilt on every run.\n"
_LOG_TAIL_LINES = 40
_ECHOED_PREFIXES = ("RIG:", "RIG-BLENDER:")
_FIXTURE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
# The addon prints this when its bind() fails, instead of raising, so it is the
# only trace of why readiness timed out.
_BIND_FAILURE_MARKER = "Failed to start server"
# An unmarked --work-dir holding any of these is a Blender resources root or a
# scripts tree Blender executes from.
_BLENDER_RESOURCE_ENTRIES = ("config", "datafiles", "extensions", "scripts", "userpref.blend")
_AUTO_EXECUTED_SCRIPT_DIRS = ("startup", "modules")
# Inherited variables that could redirect Blender or its Python away from the
# staged addon. A prefix also catches BLENDER_SYSTEM_SCRIPTS.
_PRUNED_ENVIRONMENT_PREFIXES = ("BLENDER",)
_PRUNED_ENVIRONMENT_NAMES = frozenset({"PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"})

# Mirrors docker/blender/start_server.py. Blender finds the staged addon through
# BLENDER_USER_SCRIPTS, so the user's own configuration is never touched.
_BOOTSTRAP = '''\
"""Enable the staged BlenderMCP addon and start its socket server (written by scripts/blender_rig.py)."""

import json
import os

import bpy

bpy.ops.preferences.addon_enable(module="{addon}")

from blender_mcp.server_core import BlenderMCPServer  # ruff: ignore[module-import-not-at-top-of-file]

bpy.types.blendermcp_server = BlenderMCPServer(port={port})
bpy.types.blendermcp_server.start()
bpy.context.scene.blendermcp_server_running = bpy.types.blendermcp_server.running
print("RIG: server running =", bpy.types.blendermcp_server.running)

# The receipt is how the rig knows the process answering the port is the Blender
# it launched. start() swallows a failed bind into a print, so without this a
# stranger already on the port would answer every command in Blender's place.
if bpy.types.blendermcp_server.running:
    receipt = os.path.join(os.environ["BLENDERMCP_RIG_WORK_DIR"], "{receipt}")
    with open(receipt, "w", encoding="utf-8") as handle:
        json.dump(dict(nonce="{nonce}", pid=os.getpid(), port={port}), handle)
    print("RIG: readiness receipt written to", receipt)
'''


class RigError(RuntimeError):
    """The rig could not be brought up, or a command did not come back."""


class _OutputDrain:
    """
    Blender's combined output, read on a thread that must never stop early.

    If the pipe fills, at about 64 KiB, Blender blocks in `write()` on its main
    thread, where the drain loop answers commands, and the rig causes the hang it
    exists to detect. So on any failure the reader still consumes the raw stream
    to EOF, and keeps the exception so `_shut_down` can blame the reader, not Blender.

    Attributes:
        failure: Whatever ended the tee, or None if it ran to EOF normally.

    """

    def __init__(self, blender: subprocess.Popen[str], log_path: Path, abandoned: threading.Event) -> None:
        """
        Prepare a reader for one launched Blender.

        Args:
            blender: The process whose combined output to drain.
            log_path: File to tee that output into.
            abandoned: Set once the scenario is abandoned at its deadline; echoing
                stops then, so nothing prints after the verdict.

        """
        self._blender = blender
        self._log_path = log_path
        self._abandoned = abandoned
        self._thread = threading.Thread(target=self._run, name="rig-blender-log", daemon=True)
        self._started = False
        self.failure: BaseException | None = None

    def start(self) -> None:
        """Begin draining. Must be called before anything waits on the process."""
        self._thread.start()
        self._started = True

    def join(self, timeout: float) -> None:
        """
        Wait for the reader to reach EOF, if it was ever started.

        `start()` can fail, and joining an unstarted thread raises `RuntimeError`,
        which would replace that failure with a misleading teardown error.

        Args:
            timeout: Seconds to wait before giving up on it.

        """
        if not self._started:
            return
        self._thread.join(timeout)

    def is_alive(self) -> bool:
        """
        Report whether the reader is still running.

        Returns:
            bool: True until EOF. Alive after Blender exits means something still
                holds the pipe's write end, usually a Blender grandchild.

        """
        return self._thread.is_alive()

    def _run(self) -> None:
        """Tee the output, and fall back to a silent drain if that fails."""
        try:
            self._tee()
        # Deliberately BaseException: the only outcome worse than losing the log is
        # leaving the pipe unread, which deadlocks Blender on its main thread.
        except BaseException as failure:
            self.failure = failure
            self._drain_silently()

    def _tee(self) -> None:
        """Copy every line to the log file, echoing the rig's own markers."""
        stream = self._blender.stdout
        if stream is None:
            return
        with self._log_path.open("w", encoding="utf-8") as log:
            for line in stream:
                log.write(line)
                log.flush()
                if line.startswith(_ECHOED_PREFIXES) and not self._abandoned.is_set():
                    print(line.rstrip(), flush=True)

    def _drain_silently(self) -> None:
        """
        Consume the raw pipe to EOF, discarding it, so it can never fill.

        Reads the bytes under the text wrapper, because the decoder may be what
        broke the tee.
        """
        stream = self._blender.stdout
        if stream is None:
            return
        raw: IO[bytes] | IO[str] = getattr(stream, "buffer", stream)
        try:
            while raw.read(8192):
                pass
        # Nothing is left to try; the pipe is already closed or unreadable.
        except BaseException:
            pass


@dataclass(frozen=True)
class _Launch:
    """
    One launched Blender, and the identity the rig will hold it to.

    Readiness is a claim about this process, which the port alone does not identify.

    Attributes:
        process: The Blender the rig started.
        port: Port its addon socket was told to listen on.
        work_dir: Directory both halves exchange files through.
        nonce: This run's identifier, echoed back in the readiness receipt.
        log_path: Where Blender's combined output is being teed.
        drain: The reader doing that teeing, joined before a failure quotes the log.

    """

    process: subprocess.Popen[str]
    port: int
    work_dir: Path
    nonce: str
    log_path: Path
    drain: _OutputDrain


class BlenderRig:
    """
    A live Blender's addon socket, plus the fixtures a scenario was handed.

    Attributes:
        work_dir: The caller-supplied directory; the only place the rig writes.
        blends: `.blend` fixtures by the name the caller gave them. These are the
            rig's own copies, not the caller's originals.

    """

    def __init__(
        self,
        work_dir: Path,
        blends: dict[str, Path],
        port: int,
        command_timeout: float,
        abandoned: threading.Event | None = None,
    ) -> None:
        """
        Bind the rig to a running Blender's socket.

        Args:
            work_dir: The caller-supplied directory; the only place the rig writes.
            blends: `.blend` fixture copies by the name the caller gave them.
            port: Port the addon's socket server is listening on.
            command_timeout: Seconds to wait for one command's reply.
            abandoned: Set when the scenario overran its deadline. Its thread keeps
                running, so `send` must then refuse and stop printing.

        """
        self.work_dir = work_dir
        self.blends = blends
        self._port = port
        self._command_timeout = command_timeout
        self._abandoned = abandoned if abandoned is not None else threading.Event()
        self._sequence = 0

    def send(self, command_type: str, params: dict | None = None) -> dict:
        """
        Send one addon command and return its response, printing both.

        Args:
            command_type: The addon command name, e.g. "ping".
            params: Command parameters; omitted means none.

        Returns:
            dict: The decoded response, including the echoed request id.

        Raises:
            RigError: If the scenario was abandoned, the command went unanswered
                within the command timeout (it may still run inside Blender), the
                transport failed, or the reply is for a different request.

        """
        self._refuse_if_abandoned(command_type)
        self._sequence += 1
        request = {"id": f"rig-{self._sequence}", "type": command_type, "params": params or {}}
        self._echo(f"--> {json.dumps(request)}")
        response = self._round_trip(request)
        self._echo(f"<-- {json.dumps(response)}")
        if response.get("id") != request["id"]:
            raise RigError(f"response id {response.get('id')!r} does not match request id {request['id']!r}")
        return response

    def _refuse_if_abandoned(self, command_type: str) -> None:
        """
        Stop a scenario the rig has already given a verdict on.

        Args:
            command_type: The command that was about to be sent.

        Raises:
            RigError: If the scenario's deadline has passed.

        """
        if self._abandoned.is_set():
            raise RigError(
                f"the rig abandoned this scenario at its deadline, so {command_type!r} was not sent; "
                "the verdict has already been reported"
            )

    def _echo(self, line: str) -> None:
        """
        Print one transcript line, unless the verdict has already been given.

        Args:
            line: The line to print.

        """
        if not self._abandoned.is_set():
            print(line, flush=True)

    def _round_trip(self, request: dict) -> dict:
        """
        Exchange one newline-delimited JSON frame, turning socket failures into `RigError`.

        A bare socket `TimeoutError` names neither the command nor the port, and
        reads as a failure when the outcome is unknown.

        Args:
            request: The frame to send.

        Returns:
            dict: The decoded response frame.

        Raises:
            RigError: If the command was not answered in time, if the transport
                failed, or if Blender closed the connection mid-frame.

        """
        try:
            buffer = self._exchange(request)
        except TimeoutError as expiry:
            raise RigError(self._unanswered_message(request)) from expiry
        except OSError as failure:
            raise RigError(
                f"the rig could not exchange {request['type']!r} with Blender on 127.0.0.1:{self._port}: "
                f"{type(failure).__name__}: {failure}"
            ) from failure
        if b"\n" not in buffer:
            raise RigError(
                f"Blender closed the connection on 127.0.0.1:{self._port} before answering {request['type']!r}"
            )
        return _decode_frame(buffer)

    def _exchange(self, request: dict) -> bytes:
        """
        Send one frame on a fresh connection and read the reply frame back.

        A new connection per command shows the listener still accepts, which a
        long-lived socket would hide. The connect and the reply share one deadline,
        so `--command-timeout` bounds the whole command. Socket errors propagate to
        `_round_trip`, which describes them.

        Args:
            request: The frame to send.

        Returns:
            bytes: Everything received, lacking a newline if the peer hung up.

        """
        deadline = time.monotonic() + self._command_timeout
        with socket.create_connection(("127.0.0.1", self._port), timeout=self._command_timeout) as sock:
            sock.sendall(json.dumps(request).encode("utf-8") + b"\n")
            return _read_frame(sock, max(0.0, deadline - time.monotonic()))

    def _unanswered_message(self, request: dict) -> str:
        """
        Describe a command whose outcome is unknown, rather than calling it a failure.

        Nothing cancels a command the addon has queued, so it can still run and
        change the scene after the rig gives up, and its reply is lost.

        Args:
            request: The frame that went unanswered.

        Returns:
            str: The error text, naming the command, the port and the doubt.

        """
        return (
            f"{request['type']!r} (request {request['id']}) was not answered by the Blender on "
            f"127.0.0.1:{self._port} within {self._command_timeout:g}s. The rig has closed that connection, but "
            "nothing cancels a command the addon has already queued, so it MAY STILL BE EXECUTING inside Blender "
            "and may already have changed the scene: treat this as an unknown outcome, not as 'it did not run'. "
            "Its reply goes nowhere and the addon will usually log nothing about it. Inspect the scene before "
            "trusting it, and raise --command-timeout if this command is legitimately slower."
        )


def _read_frame(sock: socket.socket, timeout: float) -> bytes:
    """
    Read until the newline that ends one protocol frame, or until EOF.

    The timeout bounds the whole frame, not each `recv`, so a peer that trickles
    bytes without a newline cannot hold the rig past its deadline. The readiness
    probe and `BlenderRig.send` share this, so they agree on what a reply is.

    Args:
        sock: The connected socket to read from.
        timeout: Seconds the whole frame may take.

    Returns:
        bytes: Everything received, which lacks a newline if the peer hung up.

    Raises:
        TimeoutError: If no complete frame arrived before the deadline. It is an
            `OSError`, so the readiness probe treats it as not ready yet.

    """
    deadline = time.monotonic() + timeout
    buffer = b""
    while b"\n" not in buffer:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"no complete frame within {timeout:g}s ({len(buffer)} bytes received)")
        sock.settimeout(remaining)
        chunk = sock.recv(8192)
        if not chunk:
            break
        buffer += chunk
    return buffer


def _decode_frame(buffer: bytes) -> dict:
    """
    Decode the first newline-delimited JSON frame in a received buffer.

    Args:
        buffer: Bytes read from the socket.

    Returns:
        dict: The decoded frame.

    """
    return json.loads(buffer.split(b"\n", 1)[0].decode("utf-8"))


def _require_local_blender() -> None:
    """
    Refuse to run anywhere the local GUI mode does not apply.

    Raises:
        RigError: On a non-macOS host, or when Blender is not where expected.

    """
    if platform.system() != "Darwin":
        raise RigError(
            f"the local rig is macOS-only (this host is {platform.system()}); "
            "on Linux/CI use the container rig: "
            "docker compose -f docker/blender/docker-compose.yml up --wait"
        )
    if not _BLENDER.is_file():
        raise RigError(f"no Blender at {_BLENDER}; install it or use the container rig")


def _choose_port(requested: int) -> int:
    """
    Settle on a port: the requested one, or a free one from the kernel.

    Args:
        requested: The `--port` value; 0 means "pick a free one".

    Returns:
        int: The port the rig will tell Blender to listen on.

    """
    if requested:
        return requested
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _require_port_free(port: int) -> None:
    """
    Abort rather than adopt a process that is already listening.

    Blender's own bind would fail, `start()` would only print the error, and every
    command would reach the other process, most likely the user's own Blender.
    Checked again just before launch to narrow the race; the port in the
    readiness receipt catches what remains.

    Args:
        port: The port the rig intends to use.

    Raises:
        RigError: If anything accepts a connection there.

    """
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            pass
    except OSError:
        return
    raise RigError(
        f"something is already listening on 127.0.0.1:{port}; refusing to adopt it. "
        "The rig would otherwise drive that process - most likely your own running Blender - "
        "and report its answers as the rig's. Stop it, or pass --port with a free port."
    )


def _claim_work_dir(work_dir: Path) -> None:
    """
    Take ownership of `--work-dir`, or refuse to touch it at all.

    The directory must be missing, empty, or already carry the rig's marker;
    anything else is someone's data. This stops the rig pointing
    `BLENDER_USER_RESOURCES` at a real resources root such as
    `~/Library/Application Support/Blender/5.2`, which has no `addons/` child, and
    launching Blender with a `startup/` or `modules/` tree it would execute.

    A symlinked `--work-dir` is allowed: `main()` has resolved it, so the rule
    applies to the real directory.

    Args:
        work_dir: The caller-supplied directory, already resolved by `main()`.

    Raises:
        RigError: If the path exists and is not a directory, or holds contents
            the rig did not create.

    """
    if work_dir.exists() and not work_dir.is_dir():
        raise RigError(f"--work-dir {work_dir} exists and is not a directory.")
    marker = work_dir / _OWNED_MARKER_NAME
    if work_dir.is_dir() and not marker.is_file():
        existing = sorted(entry.name for entry in work_dir.iterdir())
        if existing:
            raise RigError(_foreign_work_dir_message(work_dir, existing))
    work_dir.mkdir(parents=True, exist_ok=True)
    marker.write_text(_OWNED_MARKER_TEXT, encoding="utf-8")


def _foreign_work_dir_message(work_dir: Path, existing: list[str]) -> str:
    """
    Explain why a populated, unmarked `--work-dir` was refused.

    Args:
        work_dir: The directory that was refused.
        existing: Its entry names.

    Returns:
        str: The error text, naming the specific hazard when there is one.

    """
    hazards = []
    if any(entry in _BLENDER_RESOURCE_ENTRIES for entry in existing):
        hazards.append(
            "it looks like a real Blender resources root, and the rig would point "
            "BLENDER_USER_RESOURCES straight at your own configuration"
        )
    if any(entry in _AUTO_EXECUTED_SCRIPT_DIRS for entry in existing):
        hazards.append(
            "it carries startup/ or modules/, which Blender auto-executes and auto-imports "
            "from BLENDER_USER_SCRIPTS - the rig would be running that code, not just its own addon"
        )
    detail = f" In particular, {'; and '.join(hazards)}." if hazards else ""
    return (
        f"--work-dir {work_dir} already has contents and carries no {_OWNED_MARKER_NAME}, "
        f"so the rig did not create it and will not write into it (found: {', '.join(existing[:10])})."
        f"{detail} Point --work-dir at an empty or rig-created directory."
    )


def _rig_owned_subdirectory(work_dir: Path, name: str) -> Path:
    """
    Create, or re-adopt, a subdirectory of the work dir the rig may rebuild.

    The marker shows the rig created the directory, not everything in it, so
    callers replace only the entries they own.

    Args:
        work_dir: The claimed working directory.
        name: The subdirectory's name.

    Returns:
        Path: The subdirectory, a real directory carrying the marker.

    Raises:
        RigError: If the path exists as a symlink, as a regular file, or as a
            directory the rig did not create.

    """
    directory = work_dir / name
    marker = directory / _OWNED_MARKER_NAME
    if directory.is_symlink():
        raise RigError(f"{directory} is a symlink; refusing to write through it. Use a --work-dir the rig owns.")
    if directory.exists() and not directory.is_dir():
        raise RigError(f"{directory} exists and is not a directory; the rig will not replace it.")
    if directory.is_dir() and not marker.is_file():
        raise RigError(
            f"{directory} exists but carries no {_OWNED_MARKER_NAME}, so the rig did not create it "
            "and will not write into it. Point --work-dir at an empty or rig-created directory."
        )
    directory.mkdir(parents=True, exist_ok=True)
    marker.write_text(_OWNED_MARKER_TEXT, encoding="utf-8")
    return directory


def _stage_addon(work_dir: Path) -> None:
    """
    Copy this checkout's addon where a `BLENDER_USER_SCRIPTS` Blender will find it.

    Only `addons/blender_mcp` is replaced, so add-ons a user installed through
    Blender's UI between runs survive.

    Args:
        work_dir: The caller-supplied directory to stage into.

    Raises:
        RigError: If `<work-dir>/addons` is a symlink, a regular file, or a
            directory the rig did not create.

    """
    addons = _rig_owned_subdirectory(work_dir, "addons")
    staged = addons / _INSTALLED_ADDON_NAME
    if staged.is_symlink():
        raise RigError(f"{staged} is a symlink; refusing to delete through it.")
    if staged.exists():
        shutil.rmtree(staged)
    shutil.copytree(
        _BUNDLED_ADDON,
        staged,
        ignore=shutil.ignore_patterns("__pycache__"),
    )


def _stage_blends(work_dir: Path, blends: list[tuple[str, Path]]) -> dict[str, Path]:
    """
    Copy each `.blend` fixture into the work dir and hand the scenario the copy.

    A scenario's first save would otherwise overwrite the caller's fixture. The
    destination is unlinked first because `shutil.copy2` writes through a symlink.

    Args:
        work_dir: The caller-supplied directory to copy into.
        blends: `(name, source path)` pairs from `--blend`.

    Returns:
        dict[str, Path]: Fixture name to the rig's own copy.

    Raises:
        RigError: If two fixtures share a name, or the destination is a symlink.

    """
    staged_dir = _rig_owned_subdirectory(work_dir, "blends")
    copies: dict[str, Path] = {}
    for name, source in blends:
        if name in copies:
            raise RigError(f"--blend {name} was given twice; the two copies would overwrite each other.")
        destination = staged_dir / f"{name}.blend"
        if destination.is_symlink():
            raise RigError(f"{destination} is a symlink; refusing to write through it.")
        destination.unlink(missing_ok=True)
        shutil.copy2(source, destination)
        copies[name] = destination
    return copies


def _child_environment(work_dir: Path) -> dict[str, str]:
    """
    Build Blender's environment from a pruned copy of this process's.

    The dropped variables could load another scripts tree or make the bootstrap
    import `blender_mcp` from the wrong place, a failure invisible from here.
    `TMPDIR` moves Blender's session temp files, such as autosaves and
    `quit.blend`, into the work dir.

    Args:
        work_dir: The directory every Blender-side path should land under.

    Returns:
        dict[str, str]: The child's environment.

    """
    inherited = {
        key: value
        for key, value in os.environ.items()
        if key not in _PRUNED_ENVIRONMENT_NAMES and not key.startswith(_PRUNED_ENVIRONMENT_PREFIXES)
    }
    return {
        **inherited,
        "BLENDER_USER_RESOURCES": str(work_dir),
        "BLENDER_USER_SCRIPTS": str(work_dir),
        "BLENDERMCP_OUTPUT_ROOTS": str(work_dir),
        "BLENDERMCP_RIG_WORK_DIR": str(work_dir),
        "TMPDIR": str(work_dir / "tmp"),
        "PYTHONUNBUFFERED": "1",
    }


def _launch_blender(work_dir: Path, port: int, nonce: str, blender_scripts: list[Path]) -> subprocess.Popen[str]:
    """
    Start a GUI Blender running the staged addon's socket server.

    Checks the port again just before `Popen`, to narrow the window in which
    something else can take it.

    Args:
        work_dir: Directory holding the staged addon and the bootstrap script.
        port: Port the addon's socket server should listen on.
        nonce: This run's identifier, written back out in the readiness receipt.
        blender_scripts: Extra `--python` scripts run inside Blender after the
            bootstrap; a scenario's only way to reach `bpy`.

    Returns:
        subprocess.Popen[str]: The running Blender process, its combined output
            on a pipe that the caller must start draining immediately.

    """
    bootstrap = work_dir / "rig_start_server.py"
    bootstrap.write_text(
        _BOOTSTRAP.format(addon=_INSTALLED_ADDON_NAME, port=port, nonce=nonce, receipt=_RECEIPT_FILE_NAME),
        encoding="utf-8",
    )
    (work_dir / "tmp").mkdir(parents=True, exist_ok=True)
    command = [str(_BLENDER), "--factory-startup", "--python", str(bootstrap)]
    for script in blender_scripts:
        command += ["--python", str(script)]
    _require_port_free(port)
    return subprocess.Popen(
        command,
        env=_child_environment(work_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        # Blender's output is not always UTF-8. Under strict decoding one bad byte
        # would end the reader, and the undrained pipe would deadlock Blender.
        errors="replace",
    )


def _log_tail(log_path: Path) -> str:
    """
    Quote the end of Blender's log, for errors that would otherwise say nothing.

    Args:
        log_path: The log to read.

    Returns:
        str: A block ready to append to a message, or "" if there is no log.

    """
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    if not lines:
        return ""
    tail = "\n".join(lines[-_LOG_TAIL_LINES:])
    return f"\n--- last {min(len(lines), _LOG_TAIL_LINES)} lines of {log_path} ---\n{tail}\n--- end of log ---"


def _bind_failure_note(log_path: Path) -> str:
    """
    Report a swallowed bind failure, which is otherwise buried in the log tail.

    `BlenderMCPServer.start()` prints its bind error instead of raising, so a port
    taken after the pre-flight would read only as a ping that never came back.

    Args:
        log_path: Blender's log.

    Returns:
        str: A clause naming the bind failure, or "" if there was none.

    """
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if _BIND_FAILURE_MARKER not in text:
        return ""
    return (
        f" Blender's own log says {_BIND_FAILURE_MARKER!r}, so its socket bind failed rather than the"
        " server being slow: something took the port after the rig's pre-flight said it was free."
    )


def _receipt_matches(receipt: Path, nonce: str, port: int) -> bool:
    """
    Check that the readiness receipt was written by this run's bootstrap, for this port.

    Args:
        receipt: The receipt file the bootstrap writes.
        nonce: The nonce this run handed the bootstrap.
        port: The port this run launched Blender on.

    Returns:
        bool: True only for a complete receipt carrying that nonce and port.

    """
    try:
        written = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(written, dict) and written.get("nonce") == nonce and written.get("port") == port


def _ping_answers(port: int) -> bool:
    """
    Round-trip a `ping`, because accepting a connection is not being ready.

    The socket listens before the drain timer is registered, and any
    `--blender-script` runs before timers fire, so the port can accept while
    nothing answers.

    Args:
        port: The port to probe.

    Returns:
        bool: Whether a successful reply came back.

    """
    frame = json.dumps({"id": "rig-readiness", "type": "ping", "params": {}}).encode("utf-8") + b"\n"
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=_READINESS_PROBE_TIMEOUT_SECONDS) as sock:
            sock.sendall(frame)
            buffer = _read_frame(sock, _READINESS_PROBE_TIMEOUT_SECONDS)
    except OSError:
        return False
    try:
        reply = _decode_frame(buffer)
    except ValueError:
        return False
    return reply.get("status") == "success"


def _wait_until_ready(launch: _Launch, timeout: float) -> None:
    """
    Block until this Blender is answering commands, or give up with its log.

    Ready means the process is alive, a receipt carries this run's nonce and
    port, and a ping succeeds. The wrong or a half-started process can pass any
    one of them alone.

    Args:
        launch: The Blender the rig started, and the identity it must prove.
        timeout: Seconds to wait before giving up.

    Raises:
        RigError: If Blender exited, or never answered in time.

    """
    receipt = launch.work_dir / _RECEIPT_FILE_NAME
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if launch.process.poll() is not None:
            # The pipe is at EOF, so join first to get Blender's last lines into the tail.
            launch.drain.join(_SHUTDOWN_GRACE_SECONDS)
            raise RigError(
                f"Blender exited with status {launch.process.returncode} "
                f"before answering on port {launch.port}."
                f"{_bind_failure_note(launch.log_path)}{_log_tail(launch.log_path)}"
            )
        if _receipt_matches(receipt, launch.nonce, launch.port) and _ping_answers(launch.port):
            return
        time.sleep(_POLL_INTERVAL_SECONDS)
    # Blender is still running, so the reader cannot be joined. It flushes every
    # line, so the tail is current anyway.
    raise RigError(
        f"the Blender this rig launched never answered a ping on port {launch.port} within {timeout:.0f}s."
        f"{_bind_failure_note(launch.log_path)}{_log_tail(launch.log_path)}"
    )


def _run_scenario_with_deadline(
    run: Callable[[BlenderRig], None],
    rig: BlenderRig,
    timeout: float,
    abandoned: threading.Event | None = None,
) -> None:
    """
    Run the scenario under a wall clock, so a wedged one still reaches teardown.

    A scenario polling for a file Blender never writes would otherwise wait
    forever. The daemon thread is abandoned, not stopped, so setting `abandoned`
    makes `BlenderRig.send` refuse and keeps the log reader from echoing after
    the verdict.

    Args:
        run: The scenario's `run(rig)`.
        rig: The rig to hand it.
        timeout: Seconds the scenario may take.
        abandoned: The event to set on expiry; created locally when omitted.

    Raises:
        RigError: If the scenario had not returned by the deadline.

    """
    raised: list[BaseException] = []
    abandoned = abandoned if abandoned is not None else threading.Event()

    def _target() -> None:
        try:
            run(rig)
        # Re-raised on the calling thread below, so the rig reports the scenario's own
        # failure, not a thread crash.
        except BaseException as failure:
            raised.append(failure)

    thread = threading.Thread(target=_target, name="rig-scenario", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        abandoned.set()
        raise RigError(
            f"the scenario did not finish within {timeout:.0f}s; abandoning it and shutting Blender down. "
            "It is still running on a daemon thread, so its output is suppressed from here on and any "
            "further rig.send() raises. Raise --scenario-timeout if the scenario is legitimately slower."
        )
    if raised:
        raise raised[0]


def _reader_problems(drain: _OutputDrain | None) -> list[str]:
    """
    Blame the rig's own reader when it was the reader, not Blender, that broke.

    No reader means an exception is already leaving `_execute`, and raising here
    would replace it with a vaguer one.

    Args:
        drain: The reader, or None if the launch never got one.

    Returns:
        list[str]: Problems to name in the teardown failure, if any.

    """
    if drain is None:
        return []
    problems = []
    if drain.failure is not None:
        problems.append(
            f"the rig's own log reader died with {type(drain.failure).__name__}: {drain.failure} "
            "(the pipe was drained anyway, so this did not hang Blender, but the log may be short)"
        )
    if drain.is_alive():
        problems.append(
            f"the rig's own log reader is still running {_SHUTDOWN_GRACE_SECONDS:.0f}s after Blender stopped, "
            "so something still holds the pipe's write end - most likely a Blender grandchild that inherited it"
        )
    return problems


def _shut_down(blender: subprocess.Popen[str], drain: _OutputDrain | None, log_path: Path) -> None:
    """
    Stop Blender, escalating to a kill if it ignores SIGTERM.

    Not `with subprocess.Popen(...)`: its `__exit__` waits with no timeout, so a
    process that survives SIGKILL would hang the rig. The reader is joined after
    the stop request, so the quoted log tail includes Blender's last lines.

    Args:
        blender: The process to stop.
        drain: The reader draining its output, or None if the launch never got
            one. That Blender still needs stopping promptly, since its undrained
            pipe will deadlock it.
        log_path: Blender's log, quoted if anything went wrong.

    Raises:
        RigError: If the process survived `SIGKILL`, or the reader did not finish.

    """
    survived_kill = False
    try:
        if blender.poll() is None:
            blender.terminate()
            try:
                blender.wait(timeout=_SHUTDOWN_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                print("RIG: Blender ignored SIGTERM; killing it", flush=True)
                blender.kill()
                try:
                    blender.wait(timeout=_SHUTDOWN_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    survived_kill = True
    finally:
        if drain is not None:
            drain.join(_SHUTDOWN_GRACE_SECONDS)

    problems = _reader_problems(drain)
    if survived_kill:
        problems.insert(0, f"Blender (pid {blender.pid}) survived SIGKILL")
    if drain is None or not drain.is_alive():
        _close_quietly(blender.stdout)

    if problems:
        raise RigError("; ".join(problems) + _log_tail(log_path))


def _close_quietly(stream: IO[str] | None) -> None:
    """
    Release the pipe once nothing is reading it any more.

    Closing it while the reader is still reading raises `ValueError` on that
    thread, so the caller checks first.

    Args:
        stream: The pipe to close, if there is one.

    """
    if stream is None:
        return
    # Nothing to do about a pipe that will not close; the process is already gone.
    with contextlib.suppress(OSError):
        stream.close()


def _load_scenario(path: Path) -> ModuleType:
    """
    Import a scenario module from an arbitrary path.

    Turns off bytecode writing first, so no `__pycache__/` appears beside the
    caller's scenario, outside `--work-dir`.

    Args:
        path: The scenario file.

    Returns:
        ModuleType: The imported module.

    Raises:
        RigError: If the file cannot be imported or defines no `run`.

    """
    sys.dont_write_bytecode = True
    spec = importlib_util.spec_from_file_location("blender_rig_scenario", path)
    if spec is None or spec.loader is None:
        raise RigError(f"{path} is not an importable Python module")
    module = importlib_util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not callable(getattr(module, "run", None)):
        raise RigError(f"{path} defines no run(rig) function")
    return module


def _parse_blend(argument: str) -> tuple[str, Path]:
    """
    Split a `name=path` fixture argument and check the file is really there.

    The name becomes a filename in the work dir, so it may only use characters
    that cannot leave it.

    Args:
        argument: The raw `--blend` value.

    Returns:
        tuple[str, Path]: The fixture's name and its resolved source path.

    Raises:
        argparse.ArgumentTypeError: If the shape is wrong, the name could escape
            the work dir, or the file is missing.

    """
    name, separator, raw_path = argument.partition("=")
    if not separator or not name:
        raise argparse.ArgumentTypeError(f"expected name=path, got {argument!r}")
    if not _FIXTURE_NAME.match(name):
        raise argparse.ArgumentTypeError(
            f"fixture name {name!r} must be letters, digits, '.', '_' or '-'; it names a file in the work dir"
        )
    path = Path(raw_path).expanduser().resolve()
    if not path.is_file():
        raise argparse.ArgumentTypeError(f"no such .blend fixture: {path}")
    return name, path


def _parse_arguments(argv: list[str] | None) -> argparse.Namespace:
    """
    Read the command line.

    Args:
        argv: Arguments to parse; None means `sys.argv`.

    Returns:
        argparse.Namespace: The parsed arguments.

    """
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--work-dir",
        required=True,
        type=Path,
        help="empty or rig-created directory the rig may write to; it writes nowhere else",
    )
    parser.add_argument("--scenario", required=True, type=Path, help="module defining run(rig)")
    parser.add_argument(
        "--blender-script",
        action="append",
        default=[],
        type=Path,
        metavar="PATH",
        help="extra --python script to run inside Blender after the bootstrap, repeatable",
    )
    parser.add_argument(
        "--blend",
        action="append",
        default=[],
        type=_parse_blend,
        metavar="NAME=PATH",
        help="a .blend fixture to copy into the work dir and hand the scenario, repeatable",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=_DEFAULT_PORT,
        help="addon socket port; 0 (the default) asks the kernel for a free one",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=_DEFAULT_TIMEOUT_SECONDS,
        help="seconds to wait for the launched Blender to answer a ping",
    )
    parser.add_argument(
        "--scenario-timeout",
        type=float,
        default=_DEFAULT_SCENARIO_TIMEOUT_SECONDS,
        help="seconds the scenario itself may take before the rig abandons it and tears Blender down",
    )
    parser.add_argument(
        "--command-timeout",
        type=float,
        default=_DEFAULT_COMMAND_TIMEOUT_SECONDS,
        help=(
            "seconds to wait for one command's reply; unrelated to --timeout, which is the startup budget. "
            "Defaults to the real client's 180s so a stall reads as a stall, not as a rig failure"
        ),
    )
    return parser.parse_args(argv)


def _execute(arguments: argparse.Namespace, work_dir: Path) -> None:
    """
    Bring Blender up, run the scenario against it, and always tear Blender down.

    Teardown depends on whether Blender was launched, not on setup finishing:
    from launch its output fills a pipe, and a Blender left behind deadlocks in
    `write()`.

    Args:
        arguments: The parsed command line.
        work_dir: The resolved caller-supplied working directory.

    """
    _require_local_blender()
    scenario = _load_scenario(arguments.scenario.expanduser().resolve())
    port = _choose_port(arguments.port)
    _require_port_free(port)
    _claim_work_dir(work_dir)
    _stage_addon(work_dir)
    blends = _stage_blends(work_dir, arguments.blend)
    blender_scripts = [script.expanduser().resolve() for script in arguments.blender_script]
    nonce = secrets.token_hex(8)
    log_path = work_dir / _LOG_FILE_NAME
    abandoned = threading.Event()
    blender: subprocess.Popen[str] | None = None
    drain: _OutputDrain | None = None
    try:
        blender = _launch_blender(work_dir, port, nonce, blender_scripts)
        drain = _OutputDrain(blender, log_path, abandoned)
        drain.start()
        _wait_until_ready(_Launch(blender, port, work_dir, nonce, log_path, drain), arguments.timeout)
        print(f"RIG: Blender (pid {blender.pid}) up on 127.0.0.1:{port}, work dir {work_dir}", flush=True)
        rig = BlenderRig(work_dir, blends, port, arguments.command_timeout, abandoned)
        _run_scenario_with_deadline(scenario.run, rig, arguments.scenario_timeout, abandoned)
    finally:
        if blender is not None:
            _shut_down(blender, drain, log_path)


def main(argv: list[str] | None = None) -> int:
    """
    Run one scenario and report whether it passed.

    Catches `BaseException` because the exit code is the verdict: a scenario that
    calls `sys.exit(0)` must not pass. Argument parsing stays outside the `try` so
    `--help` still exits 0.

    Args:
        argv: Arguments to parse; None means `sys.argv`.

    Returns:
        int: 0 if the scenario returned cleanly, 1 otherwise.

    """
    arguments = _parse_arguments(argv)
    try:
        _execute(arguments, arguments.work_dir.expanduser().resolve())
    except BaseException as failure:
        detail = ""
        if not isinstance(failure, Exception):
            detail = (
                " -- which is a request to stop this process, not a scenario result. The rig reports it as a"
                " failure because its exit code is its verdict and must never be 0 without RIG PASSED."
            )
        print(f"RIG FAILED: {type(failure).__name__}: {failure}{detail}", file=sys.stderr, flush=True)
        return 1
    print("RIG PASSED", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
