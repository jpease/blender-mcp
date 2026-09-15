"""
Drive a live GUI Blender over the addon's own socket, for evidence that needs a real event loop.

**What this rig reaches, and what it does not.** This script speaks the *addon's*
newline-delimited JSON socket protocol and **stands up no MCP server at all**. A
scenario driven through it therefore calls `get_addon_info` the *addon command*,
and cannot call `get_addon_status` the *MCP tool*; the latter is unreachable here
by construction, because it lives server-side (`server/tools/core.py`) and calls
`force_addon_handshake()` inside the MCP process. The container rig
(`docker/blender/docker-compose.yml`) is the only mode that runs a real MCP
server, and it publishes *only* that layer - Blender's socket never leaves the
container. So there is no mode in which one scenario drives both layers. A reader
who assumes this script exercises "MCP tool -> socket -> addon" will write a
scenario that cannot run; the MCP wrapper layer is covered by ordinary `pytest`.

**Why a live Blender is needed.** The addon refuses to start under
`blender --background`, and `bpy.app.timers` never fire there, so the drain loop
that answers commands does not run. Anything that depends on a command actually
being dequeued needs a real event loop: a GUI Blender locally (this script), or
Xvfb in the container.

**Which Blender answers.** The rig picks a free ephemeral port rather than the
addon's own default, refuses to start if anything is already listening on the
port it chose, and treats readiness as a `ping` that came back *plus* a receipt
carrying this run's nonce and port, written by its own bootstrap after `start()`
reported `running`. Without all three the rig would happily adopt the developer's
live Blender - a connect to 9876 succeeds on any machine with the addon running -
and report that session's answers as its own.

**Isolation, precisely.** The addon is staged into the caller-supplied working
directory and Blender is pointed at it with **both** `BLENDER_USER_RESOURCES` and
`BLENDER_USER_SCRIPTS`, so config, datafiles and extensions resolve under the
work dir too: opening or saving a `.blend` writes `recent-files.txt` there rather
than into the user's own profile. Setting only `BLENDER_USER_SCRIPTS` leaves the
other three pointing at the user's Blender - measured, not assumed. What protects
`userpref.blend` is `--factory-startup`, which makes Blender ignore saved
preferences entirely; no environment variable does that. `TMPDIR` is redirected
to `<work-dir>/tmp`, so Blender's *session* temp dir - autosaves, `quit.blend`,
render previews - lands under the work dir as well rather than in `$TMPDIR`.

**What `BLENDERMCP_OUTPUT_ROOTS` does, and does not, do.** It makes the launched
Blender **lead with** the work dir in its handshake. It does **not** make the
work dir the only advertised root: `server_core._writable_output_roots()`
*prepends* the configured roots and then appends the open blend's directory,
`bpy.app.tempdir`, `tempfile.gettempdir()` and `~`, so `$HOME` is still in the
list (measured: `['<work-dir>', '<work-dir>/tmp/blender_*', '<work-dir>/tmp',
'/Users/<user>']`). That list is an advisory preference ranking, not an enforced
root set; narrowing the addon's own default candidates is Task 5's decision, not
this script's.

**The child's environment is pruned before the rig sets its own.** Every
inherited `BLENDER*` variable is dropped - `BLENDER_SYSTEM_SCRIPTS` shadows
Blender's *system* scripts tree exactly as `PYTHONPATH` shadows the staged addon,
and a denylist that stopped at `BLENDER_USER_*` missed it - together with
`PYTHONPATH`, `PYTHONHOME` and `PYTHONSTARTUP`. `PYTHONPATH` is the one with a
concrete precedent: this project's own container entrypoint runs the MCP server
with `PYTHONPATH=/repo/src` as a per-command prefix
(`docker/blender/entrypoint.sh`; it does not `export` it, so the container itself
is safe). A developer who exports the same thing in their own shell would make
Blender's Python resolve `blender_mcp` to the *server* package instead of the
staged addon, and the bootstrap's import would fail inside Blender.

**What "the rig writes nowhere else" actually means.** Every file this process
writes, and every Blender-side path this script controls - user resources,
scripts, the session temp dir, staged fixtures, the bootstrap and the log - lives
under `--work-dir`. `sys.dont_write_bytecode` is set before the scenario is
imported so that loading it no longer drops a `__pycache__/` beside the caller's
file. What the rig cannot promise is a *scenario* that asks Blender to write
elsewhere: the advertised roots are advisory, and a scenario may name any path.

**What the rig will delete or overwrite.** Only a directory carrying its own
`.blender-rig-owned` marker, which it writes into `--work-dir` itself. A
`--work-dir` that already has contents and no marker is refused outright, which
is what keeps a real Blender resources root (`config/`, `datafiles/`,
`extensions/`, `scripts/`, `userpref.blend`) and an auto-executing
`startup/`/`modules/` tree from being adopted. Inside a marked work dir the rig
replaces only `addons/blender_mcp` and its own `blends/` copies, never the whole
`addons/` tree, so add-ons installed alongside it survive.

Usage:
    python scripts/blender_rig.py --work-dir <dir> --scenario <scenario.py> \
        [--blend name=path ...] [--blender-script <in_blender.py> ...] \
        [--port 0] [--timeout 60] [--scenario-timeout 300] [--command-timeout 180]

A scenario module defines `run(rig)` and drives the socket through `rig.send()`.
It fails by raising; a clean return is a pass. Example:

    def run(rig):
        assert rig.send("ping")["status"] == "success"

The scenario runs in *this* process, so it cannot touch `bpy`. When a scenario
needs Blender to do something of its own accord - anything that must not arrive
as a queued socket command - pass that work as a `--blender-script`, which
Blender runs after the bootstrap. Such a script finds the working directory in
`BLENDERMCP_RIG_WORK_DIR`, which is how the two halves exchange files.

Blender's combined output is drained on a reader thread into
`<work-dir>/blender.log`; its `RIG:` lines are echoed here, and the tail of it is
quoted in any startup or shutdown failure. Leaving that pipe undrained is what
deadlocks Blender in `write()` on the very thread the drain loop runs on.
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
# 0 means "ask the kernel for a free one". Deliberately not the addon's own 9876,
# which is where a developer's live Blender already is.
_DEFAULT_PORT = 0
_DEFAULT_TIMEOUT_SECONDS = 60.0
_DEFAULT_SCENARIO_TIMEOUT_SECONDS = 300.0
# Matches the real client's socket timeout (server/connection.py), so a stall the
# rig exists to observe is reported as a stall rather than as a rig failure.
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
# What the addon prints when its own bind() lost the port after the rig's
# pre-flight said it was free. start() swallows the OSError into this print, so
# it is the only trace of the real cause of a readiness timeout.
_BIND_FAILURE_MARKER = "Failed to start server"
# A --work-dir holding any of these, with no marker, is a real Blender resources
# root or a scripts tree Blender auto-executes from. Pointing the rig at one both
# defeats the isolation and hands the launched Blender somebody else's code.
_BLENDER_RESOURCE_ENTRIES = ("config", "datafiles", "extensions", "scripts", "userpref.blend")
_AUTO_EXECUTED_SCRIPT_DIRS = ("startup", "modules")
# Inherited variables that would redirect Blender or its Python out from under
# the rig. Pruned as a prefix rather than a list: BLENDER_SYSTEM_SCRIPTS is the
# one a BLENDER_USER_-only denylist missed.
_PRUNED_ENVIRONMENT_PREFIXES = ("BLENDER",)
_PRUNED_ENVIRONMENT_NAMES = frozenset({"PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"})

# Mirrors docker/blender/start_server.py: enable the addon and start its socket
# server with no UI interaction. The container installs the addon by copying it
# into Blender's addons directory; here BLENDER_USER_SCRIPTS points Blender at
# the staged copy instead, so the user's own configuration is never touched.
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

    An undrained pipe fills at ~64 KiB - the addon prints several lines per
    connection and the rig opens one connection per command - and Blender then
    blocks in `write()` on its **main** thread, which is the thread the drain
    loop answers commands on. The rig's own logging would be what caused the hang
    it exists to detect, and the symptom would be indistinguishable from "Blender
    hung".

    So the reader's failure modes are handled rather than allowed to end the
    thread: a byte the pipe cannot decode, a work dir that will not take the log
    file, anything at all. On any failure the raw stream is still consumed to
    exhaustion so the pipe cannot fill, and the exception is kept so `_shut_down`
    can say *the rig's own log reader died* instead of blaming Blender.

    Attributes:
        failure: Whatever ended the tee, or None if it ran to EOF normally.

    """

    def __init__(self, blender: subprocess.Popen[str], log_path: Path, abandoned: threading.Event) -> None:
        """
        Prepare a reader for one launched Blender.

        Args:
            blender: The process whose combined output to drain.
            log_path: File to tee that output into.
            abandoned: Set once the scenario has been abandoned at its deadline;
                echoing stops there, so nothing lands in the transcript after
                the verdict has been printed.

        """
        self._blender = blender
        self._log_path = log_path
        self._abandoned = abandoned
        self._thread = threading.Thread(target=self._run, name="rig-blender-log", daemon=True)
        self.failure: BaseException | None = None

    def start(self) -> None:
        """Begin draining. Must be called before anything waits on the process."""
        self._thread.start()

    def join(self, timeout: float) -> None:
        """
        Wait for the reader to reach EOF.

        Args:
            timeout: Seconds to wait before giving up on it.

        """
        self._thread.join(timeout)

    def is_alive(self) -> bool:
        """
        Report whether the reader is still running.

        Returns:
            bool: True if it has not reached EOF, which means the pipe's write
                end is still open somewhere - typically a Blender grandchild that
                inherited it.

        """
        return self._thread.is_alive()

    def _run(self) -> None:
        """Tee the output, and fall back to a silent drain if that cannot be done."""
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

        Reads the byte stream underneath the text wrapper on purpose: whatever
        broke the tee may well be the decoder, and re-entering it would end this
        thread a second time.
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

    Kept together because readiness is a claim about *this* process: the port
    alone identifies nothing, and the nonce is meaningless without the work dir
    the receipt is written into.

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
            abandoned: Set when the scenario overran its deadline. The scenario
                runs on a daemon thread that is *not* killed, so without this an
                abandoned scenario keeps printing `-->`/`<--` pairs into the
                stdout that is the evidence artefact, after `RIG FAILED` has
                already been printed.

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
            RigError: If the rig has already abandoned this scenario, if Blender
                closed the connection without answering, or if it answered a
                different request than the one just sent.

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
        Exchange one newline-delimited JSON frame on a fresh connection.

        One connection per command deliberately: the addon serves each client on
        its own thread, so a scenario that reconnects proves the listener is
        still accepting, which a long-lived socket would hide.

        Args:
            request: The frame to send.

        Returns:
            dict: The decoded response frame.

        Raises:
            RigError: If the connection closed before a full frame arrived.

        """
        with socket.create_connection(("127.0.0.1", self._port), timeout=self._command_timeout) as sock:
            sock.sendall(json.dumps(request).encode("utf-8") + b"\n")
            buffer = _read_frame(sock)
        if b"\n" not in buffer:
            raise RigError(f"Blender closed the connection before answering {request['type']!r}")
        return _decode_frame(buffer)


def _read_frame(sock: socket.socket) -> bytes:
    """
    Read until the newline that terminates one protocol frame, or until EOF.

    Shared by the readiness probe and by `BlenderRig.send`, so both agree on
    what "a reply arrived" means; the caller decides whether a short read is a
    failure or merely a not-yet-ready peer.

    Args:
        sock: The connected socket to read from.

    Returns:
        bytes: Everything received, which lacks a newline if the peer hung up.

    """
    buffer = b""
    while b"\n" not in buffer:
        chunk = sock.recv(8192)
        if not chunk:
            break
        buffer += chunk
    return buffer


def _decode_frame(buffer: bytes) -> dict:
    """
    Decode the first newline-delimited JSON frame in a received buffer.

    Shared so the readiness probe and `BlenderRig.send` cannot drift apart over
    what counts as one frame.

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
    Settle on a port, preferring one the kernel says is free.

    The addon's default is 9876, which is exactly where a developer's own
    Blender is listening, so defaulting to it aims the rig at the machine's most
    likely running process instead of at a fresh one.

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

    `SO_REUSEADDR` does not let a second process bind an in-use port on macOS,
    so Blender's own bind would fail, `start()` would swallow it into a print,
    and every command would be answered by the stranger - very likely the user's
    live Blender, against which a scenario's first destructive step lands.

    Run twice: once before any staging, and again immediately before `Popen`, so
    the window in which the port can be taken is as short as this process can
    make it. It cannot be closed - only Blender's own `bind()` is authoritative -
    which is why the readiness receipt has to carry the port as well.

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

    Everything else the rig writes is inside this directory, so this is the one
    place the "writes nowhere it does not own" rule has to hold. The rule is
    simply: the directory is empty, does not exist, or already carries the rig's
    own marker. Anything else is somebody's data.

    That single rule is what keeps two specific disasters out of reach, neither
    of which a narrower check caught:

    - `~/Library/Application Support/Blender/5.2` is a plausible `--work-dir`
      and has no `addons/` child (the real one is `scripts/addons`), so a guard
      that looked only at `addons/` waved it through and then pointed
      `BLENDER_USER_RESOURCES` at the user's genuine resources root.
    - `BLENDER_USER_SCRIPTS=<work-dir>` makes Blender import every `.py` in
      `<work-dir>/startup/` at launch and put `<work-dir>/modules/` on
      `sys.path`. A reused or mistargeted work dir containing either injects
      code into the Blender the rig is about to trust - a code-execution surface
      opened by the isolation mechanism itself.

    Args:
        work_dir: The caller-supplied directory.

    Raises:
        RigError: If the path is not a directory, is a symlink, or holds
            contents the rig did not create.

    """
    if work_dir.is_symlink():
        raise RigError(f"--work-dir {work_dir} is a symlink; point it at a real directory the rig can own.")
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

    The marker records that the rig created *this directory*, which is a weaker
    claim than "the rig created everything in it" - so callers still replace only
    the specific entries they own rather than the whole tree.

    Args:
        work_dir: The claimed working directory.
        name: The subdirectory's name.

    Returns:
        Path: The subdirectory, guaranteed to be a real directory with a marker.

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

    Only `addons/blender_mcp` is removed and rebuilt, never the `addons/` tree
    itself. The marker proves the rig *created* the directory; it proves nothing
    about what has been put in it since, and a run that inherits its own marker
    from a previous run would otherwise delete add-ons a user installed through
    Blender's UI in between. This is the same discipline
    `addon_manager.install_addon` applies: remove only what you can positively
    identify as yours.

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

    A scenario is handed a path, not a policy: give it the caller's own fixture
    and the first save command overwrites the source in place. Scenarios address
    fixtures as `rig.blends[name]`, so the substitution is invisible to them.

    The destination is unlinked before it is written, because `shutil.copy2`
    follows a destination symlink and writes *through* it - the same hazard the
    staging directory is hardened against, and the reason `blends/` carries the
    rig's marker too. Duplicate `--blend` names are refused rather than silently
    clobbering each other, since both would resolve to one filename.

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

    Pruned as a prefix (`BLENDER*`) rather than as a list of known-bad names: an
    earlier denylist of `BLENDER_USER_*` and `BLENDERMCP_*` let
    `BLENDER_SYSTEM_SCRIPTS` through, which redirects Blender's *system* scripts
    tree - the same shadowing hazard one prefix over. `PYTHONPATH`, `PYTHONHOME`
    and `PYTHONSTARTUP` go for the matching reason on the Python side: any of
    them can make the bootstrap's `from blender_mcp.server_core import ...`
    resolve to the wrong tree, and the failure is invisible from out here.

    `TMPDIR` is then pointed inside the work dir so Blender's session temp
    directory - autosaves, `quit.blend`, render previews, and the first entry
    `bpy.app.tempdir` reports - lands under `--work-dir` rather than in the
    user's `$TMPDIR`.

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

    Args:
        work_dir: Directory holding the staged addon and the bootstrap script.
        port: Port the addon's socket server should listen on.
        nonce: This run's identifier, written back out in the readiness receipt.
        blender_scripts: Extra `--python` scripts, run inside Blender after the
            bootstrap. This is the only way a scenario can reach `bpy`, since the
            scenario itself runs in this process, outside Blender.

    The port pre-flight is run once more here, immediately before `Popen`, so
    the window in which something else can take the port is as narrow as this
    process can make it; it raises `RigError` through `_require_port_free` if the
    port went away in the meantime.

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
        # Blender's own output is not guaranteed UTF-8 - native library chatter, a
        # crash dump, a path in another encoding. Under the default strict decoding
        # one such byte ends the reader thread, and the pipe it was draining then
        # fills and deadlocks Blender. Replacing the byte keeps the log readable and
        # the reader alive; _OutputDrain handles the case this does not cover.
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
    Surface a swallowed bind failure, which is otherwise buried in the log tail.

    `BlenderMCPServer.start()` catches its own `OSError` and prints it, so a port
    taken between the rig's pre-flight and Blender's `bind()` reads out here as
    nothing more specific than "never answered a ping". The cause is in the log;
    this lifts it into the first sentence.

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
    Check that the readiness receipt was written by *this* run's bootstrap.

    The port is checked as well as the nonce. The bootstrap has always recorded
    it, and it is the one field that catches a receipt this run wrote for a
    *different* port - a stale file from an earlier launch in a reused work dir.

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

    `start()` binds and listens *before* registering the drain timer, and any
    `--blender-script` runs before timers begin firing, so there is a real window
    in which the port accepts and nothing dequeues. `docker/blender/healthcheck.py`
    draws the same conclusion for the container and round-trips a ping too.

    Args:
        port: The port to probe.

    Returns:
        bool: Whether a successful reply came back.

    """
    frame = json.dumps({"id": "rig-readiness", "type": "ping", "params": {}}).encode("utf-8") + b"\n"
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=_READINESS_PROBE_TIMEOUT_SECONDS) as sock:
            sock.sendall(frame)
            buffer = _read_frame(sock)
    except OSError:
        return False
    try:
        reply = _decode_frame(buffer)
    except ValueError:
        return False
    return reply.get("status") == "success"


def _wait_until_ready(launch: _Launch, timeout: float) -> None:
    """
    Block until *this* Blender is answering commands, or give up with its log.

    Readiness is three things together: the process is alive, its bootstrap wrote
    a receipt carrying this run's nonce and port, and a `ping` came back. Any one
    alone is satisfiable by the wrong process or by a half-started one - a bare
    connect most of all, which succeeds against any listener on the machine.

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
            # The pipe is at EOF now, so the reader will finish; joining first is
            # what makes the quoted tail include Blender's own last words.
            launch.drain.join(_SHUTDOWN_GRACE_SECONDS)
            raise RigError(
                f"Blender exited with status {launch.process.returncode} "
                f"before answering on port {launch.port}."
                f"{_bind_failure_note(launch.log_path)}{_log_tail(launch.log_path)}"
            )
        if _receipt_matches(receipt, launch.nonce, launch.port) and _ping_answers(launch.port):
            return
        time.sleep(_POLL_INTERVAL_SECONDS)
    # Blender is still running here, so the reader cannot be joined - it has no
    # EOF to reach. The tee flushes every line as it writes it, which is what
    # makes the tail current anyway.
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

    The rig's documented cross-process synchronization is "poll for a file",
    which is the canonical way to wait forever. A harness whose whole purpose is
    to show that Blender did not hang must not be able to hang itself. The
    scenario runs on a daemon thread so an abandoned one cannot keep the
    interpreter alive after the rig has reported.

    A daemon thread is abandoned, not stopped, so the deadline also *silences*
    it: the event set here makes `BlenderRig.send` refuse and stop printing, and
    stops the log reader echoing. Without that, `-->`/`<--` pairs keep arriving
    in the transcript after `RIG FAILED`, and the transcript is the artefact.

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
        # Deliberately broad: whatever the scenario raised is re-raised on the calling
        # thread below, so the rig reports the scenario's own failure, not a thread crash.
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


def _shut_down(blender: subprocess.Popen[str], drain: _OutputDrain, log_path: Path) -> None:
    """
    Stop Blender, escalating to a kill if it ignores the polite request.

    Deliberately not `with subprocess.Popen(...)`: CPython's `Popen.__exit__`
    calls `self.wait()` with **no timeout** on every path but `KeyboardInterrupt`,
    so the one teardown failure this function anticipates - a process that
    survived `SIGKILL` - would propagate into `__exit__` and block forever on
    exactly the process that just proved it will not die. A harness that cannot
    hang must not own an unbounded wait.

    The reader is joined after the process has been asked to stop, not before,
    and every diagnosis is built after that join so the quoted tail holds
    Blender's last words rather than whatever had been flushed when it was asked
    to stop.

    Args:
        blender: The process to stop.
        drain: The reader draining its output.
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
        drain.join(_SHUTDOWN_GRACE_SECONDS)

    problems = []
    if survived_kill:
        problems.append(f"Blender (pid {blender.pid}) survived SIGKILL")
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
    else:
        _close_quietly(blender.stdout)

    if problems:
        raise RigError("; ".join(problems) + _log_tail(log_path))


def _close_quietly(stream: IO[str] | None) -> None:
    """
    Release the pipe once nothing is reading it any more.

    Closing it while the reader is still in `readline` raises `ValueError` on
    that thread and leaks the log handle, which is why the caller checks first.

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

    Byte-code writing is turned off first: `exec_module` otherwise drops a
    `__pycache__/` next to the *caller's* scenario file, which is outside
    `--work-dir` and contradicts the rig's own containment claim. The container
    half already does this with `PYTHONDONTWRITEBYTECODE=1`
    (`docker/blender/entrypoint.sh`).

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

    The name becomes a filename inside the work dir, so it is restricted to
    characters that cannot walk out of it.

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
    blender = _launch_blender(work_dir, port, nonce, blender_scripts)
    drain = _OutputDrain(blender, log_path, abandoned)
    try:
        drain.start()
        _wait_until_ready(_Launch(blender, port, work_dir, nonce, log_path, drain), arguments.timeout)
        print(f"RIG: Blender (pid {blender.pid}) up on 127.0.0.1:{port}, work dir {work_dir}", flush=True)
        rig = BlenderRig(work_dir, blends, port, arguments.command_timeout, abandoned)
        _run_scenario_with_deadline(scenario.run, rig, arguments.scenario_timeout, abandoned)
    finally:
        _shut_down(blender, drain, log_path)


def main(argv: list[str] | None = None) -> int:
    """
    Run one scenario and report whether it passed.

    Catches `BaseException`, not `Exception`: the rig's exit code *is* its
    verdict, and a scenario that calls `sys.exit(0)` would otherwise end this
    process with status 0 having printed neither `RIG PASSED` nor `RIG FAILED` -
    a pass it never earned, in the artefact eight tasks are gated on. Argument
    parsing stays outside the `try` so `--help` still exits 0.

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
