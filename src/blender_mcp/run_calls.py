"""
`blender-mcp run-calls`: run calls files against a background Blender, through the server's tool layer.

A calls file is a JSON object with a `calls` list, or a bare list, of
`{"tool": <MCP tool name>, "args": {...}}`; any other top-level key is ignored. Every call goes
through an in-process MCP client session with this server, so argument validation, strict
arguments and the response envelope are exactly what an MCP client gets.

Tools mount when `blender_mcp` is imported, so `blender-mcp run-calls` (`server/cli.py`) sets
`BLENDER_MCP_TOOLSETS` and re-executes this module with `python -m blender_mcp.run_calls`.

stdout carries only JSON lines: one per attempted call, then `{"applied": n, "total": m}`. The run
stops at the first call that is not ok. Exit codes: 0 every call ok, 1 a call failed, 2 a usage or
input error or a Blender that would not serve, 130 interrupted (no summary). Blender's own output
goes to stderr.
"""

import argparse
import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time

from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from types import FrameType

from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import CallToolResult, TextContent

from .addon_manager import get_bundled_addon_path
from .server.app import mcp

PROG = "blender-mcp run-calls"
DEFAULT_TOOLSETS = "shot"
READY_PREFIX = "BLENDERMCP_RUN_CALLS_READY"
READY_TIMEOUT_SECONDS = 120
STOP_TIMEOUT_SECONDS = 10
LOOPBACK = "127.0.0.1"
BOOTSTRAP = Path(__file__).with_name("run_calls_bootstrap.py")
# A Python environment meant for this interpreter can load the wrong package into Blender's.
_BLENDER_UNSAFE_ENV = ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP")
_EMPTY_SCENE_ENV = "BLENDERMCP_RUN_CALLS_EMPTY_SCENE"
_INTERRUPTED = 130
_INTERRUPT_SIGNALS = (signal.SIGINT, signal.SIGTERM)


@dataclass(frozen=True, slots=True)
class Call:
    """One call read from a calls file, numbered across every file of the run."""

    index: int
    tool: str
    args: Mapping[str, object]
    source: str


@dataclass(frozen=True, slots=True)
class CallOutcome:
    """What one call came to, as its output line reports it."""

    index: int
    tool: str
    ok: bool
    warnings: tuple[str, ...]
    error: str | None


class CallsFileError(ValueError):
    """A calls file that cannot be read or does not have the calls-file shape."""


type CallTool = Callable[[str, dict[str, object]], Awaitable[CallToolResult]]
type StartBlender = Callable[[list[str], dict[str, str]], subprocess.Popen[str]]


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """
    Define the `run-calls` arguments on `parser`.

    The one definition: the `blender-mcp --help` listing and the runner both build from it.

    Args:
        parser: The parser to add them to.

    """
    parser.add_argument("--blender", required=True, help="Blender executable")
    parser.add_argument("--open", help="a .blend to open; without it the calls start from an empty scene")
    parser.add_argument(
        "--toolsets",
        default=DEFAULT_TOOLSETS,
        help=f"comma-separated modes/bundles, as BLENDER_MCP_TOOLSETS (default: {DEFAULT_TOOLSETS})",
    )
    parser.add_argument(
        "--file-roots",
        help=f"{os.pathsep!r}-separated directories Blender's file commands may touch (BLENDERMCP_FILE_ROOTS)",
    )
    parser.add_argument("calls_files", nargs="+", type=Path, metavar="calls.json", help="calls files, run in order")


def build_parser() -> argparse.ArgumentParser:
    """
    Build the `blender-mcp run-calls` argument parser.

    Returns:
        argparse.ArgumentParser: The parser.

    """
    parser = argparse.ArgumentParser(
        prog=PROG, description="Run calls files against a background Blender through the MCP tool layer."
    )
    add_arguments(parser)
    return parser


def _call_at(position: int, item: object, source: str, index: int) -> Call:
    """
    Validate one calls-file entry.

    Args:
        position: Its position in this file, from 0, for messages.
        item: The decoded entry.
        source: The file it came from.
        index: Its number across the whole run.

    Returns:
        Call: The validated call.

    Raises:
        CallsFileError: If the entry is not `{"tool": <non-empty string>, "args": {...}}`.

    """
    if not isinstance(item, dict):
        raise CallsFileError(f'{source}: call {position}: expected an object with "tool" and "args"')
    tool = item.get("tool")
    if not isinstance(tool, str) or not tool:
        raise CallsFileError(f'{source}: call {position}: "tool" must be a non-empty string')
    args = item.get("args")
    if not isinstance(args, dict):
        raise CallsFileError(f'{source}: call {position}: "args" must be an object')
    return Call(index=index, tool=tool, args=args, source=source)


def parse_calls(text: str, source: str, first_index: int) -> tuple[Call, ...]:
    """
    Parse one calls file.

    Args:
        text: The file's text.
        source: Where it came from, prefixed to every message.
        first_index: The run-wide number of its first call.

    Returns:
        tuple[Call, ...]: Its calls, numbered from `first_index`.

    Raises:
        CallsFileError: If the text is not JSON or not a calls file.

    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CallsFileError(f"{source}: not valid JSON: {exc}") from exc
    entries = data.get("calls") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        raise CallsFileError(f'{source}: expected an object with a "calls" list, or a list')
    return tuple(_call_at(position, item, source, first_index + position) for position, item in enumerate(entries))


def first_unmounted(calls: Sequence[Call], mounted: frozenset[str]) -> Call | None:
    """
    Find the first call naming a tool this process did not mount.

    Args:
        calls: The run's calls.
        mounted: The mounted tool names.

    Returns:
        Call | None: The first such call, or None when every tool is mounted.

    """
    return next((call for call in calls if call.tool not in mounted), None)


def unmounted_outcome(call: Call, toolsets: str) -> CallOutcome:
    """
    Describe a call refused because its tool is not mounted.

    Args:
        call: The call.
        toolsets: The `--toolsets` value the run mounted.

    Returns:
        CallOutcome: A failed outcome naming the tool and the toolsets.

    """
    return CallOutcome(
        index=call.index,
        tool=call.tool,
        ok=False,
        warnings=(),
        error=f"tool {call.tool} is not mounted under --toolsets {toolsets}",
    )


def _texts(result: CallToolResult) -> list[str]:
    """
    Collect a result's text blocks.

    Args:
        result: The tool result.

    Returns:
        list[str]: Each `TextContent` block's text, in order.

    """
    return [block.text for block in result.content if isinstance(block, TextContent)]


def _envelope(result: CallToolResult) -> Mapping[str, object]:
    """
    Find a successful result's response envelope.

    The tools return the envelope as a JSON text block (after any images), not as structured
    content; structured content is read first in case a tool ever declares it.

    Args:
        result: The tool result.

    Returns:
        Mapping[str, object]: The envelope, or an empty mapping when the result carries none.

    """
    if isinstance(result.structuredContent, dict):
        return result.structuredContent
    for text in reversed(_texts(result)):
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return {}


def outcome_of(call: Call, result: CallToolResult) -> CallOutcome:
    """
    Classify one tool result.

    Args:
        call: The call that produced it.
        result: The tool result.

    Returns:
        CallOutcome: Failed with the error text when the tool raised; otherwise ok exactly when
        the envelope's `ok` is true, with its warnings.

    """
    if result.isError:
        return CallOutcome(call.index, call.tool, ok=False, warnings=(), error=" ".join(_texts(result)))
    envelope = _envelope(result)
    ok = envelope.get("ok") is True
    raw_warnings = envelope.get("warnings")
    warnings = tuple(str(warning) for warning in raw_warnings) if isinstance(raw_warnings, list) else ()
    return CallOutcome(call.index, call.tool, ok=ok, warnings=warnings, error=None if ok else "tool reported ok: false")


def outcome_line(outcome: CallOutcome) -> str:
    """
    Format one call's output line.

    Args:
        outcome: The call's outcome.

    Returns:
        str: The JSON line.

    """
    return json.dumps(
        {
            "index": outcome.index,
            "tool": outcome.tool,
            "ok": outcome.ok,
            "warnings": list(outcome.warnings),
            "error": outcome.error,
        }
    )


def summary_line(applied: int, total: int) -> str:
    """
    Format the run's summary line.

    Args:
        applied: Calls that succeeded.
        total: Calls in the run.

    Returns:
        str: The JSON line.

    """
    return json.dumps({"applied": applied, "total": total})


def blender_argv(blender: str, open_blend: str | None, bootstrap: str) -> list[str]:
    """
    Build the background Blender command line.

    Args:
        blender: The Blender executable.
        open_blend: A .blend to open, or None.
        bootstrap: The bootstrap script Blender runs.

    Returns:
        list[str]: The argv.

    """
    return [blender, "-b", "--factory-startup", *([open_blend] if open_blend else []), "--python", bootstrap]


def blender_env(
    base: Mapping[str, str], *, file_roots: str | None, addon_dir: str, port: int, empty_scene: bool
) -> dict[str, str]:
    """
    Build the background Blender's environment.

    Args:
        base: The environment to start from.
        file_roots: `BLENDERMCP_FILE_ROOTS` for Blender, or None to leave `base`'s as it is.
        addon_dir: The add-on package directory the bootstrap loads.
        port: The port the add-on serves on.
        empty_scene: Whether the bootstrap empties the scene first.

    Returns:
        dict[str, str]: `base` without Python's own variables, plus the bootstrap's.

    """
    env = {key: value for key, value in base.items() if key not in _BLENDER_UNSAFE_ENV and key != _EMPTY_SCENE_ENV}
    env["BLENDERMCP_RUN_CALLS_ADDON"] = addon_dir
    env["BLENDERMCP_RUN_CALLS_PORT"] = str(port)
    if file_roots is not None:
        env["BLENDERMCP_FILE_ROOTS"] = file_roots
    if empty_scene:
        env[_EMPTY_SCENE_ENV] = "1"
    return env


def ready_port(line: str) -> int | None:
    """
    Read the bootstrap's ready line.

    Args:
        line: One line of Blender's output.

    Returns:
        int | None: The port it serves on, or None for any other line.

    """
    prefix, _, port = line.strip().partition(" ")
    return int(port) if prefix == READY_PREFIX and port.isdigit() else None


async def apply_calls(call_tool: CallTool, calls: Sequence[Call], emit: Callable[[str], None]) -> int:
    """
    Run in order; emit one line per call; stop at the first not-ok; return the count applied.

    Args:
        call_tool: Calls one MCP tool by name with its arguments.
        calls: The calls to run.
        emit: Receives each output line.

    Returns:
        int: The calls that succeeded.

    """
    applied = 0
    for call in calls:
        outcome = outcome_of(call, await call_tool(call.tool, dict(call.args)))
        emit(outcome_line(outcome))
        if not outcome.ok:
            break
        applied += 1
    return applied


def read_calls_files(paths: Sequence[Path]) -> tuple[Call, ...]:
    """
    Read and parse every calls file, numbering calls across them.

    Args:
        paths: The files, in run order.

    Returns:
        tuple[Call, ...]: Every call.

    Raises:
        CallsFileError: If a file cannot be read or is not a calls file.

    """
    calls: list[Call] = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise CallsFileError(f"{path}: cannot read: {exc.strerror}") from exc
        except UnicodeDecodeError as exc:
            raise CallsFileError(f"{path}: cannot read: not UTF-8 ({exc.reason})") from exc
        calls.extend(parse_calls(text, str(path), len(calls)))
    return tuple(calls)


def free_port() -> int:
    """
    Pick a free loopback port.

    Returns:
        int: A port nothing was listening on a moment ago.

    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((LOOPBACK, 0))
        return probe.getsockname()[1]


def _forward_output(proc: subprocess.Popen[str], ready: threading.Event) -> None:
    """
    Copy Blender's output to stderr for as long as it runs, flagging the ready line.

    Args:
        proc: The Blender process.
        ready: Set once the bootstrap's ready line appears.

    """
    for line in proc.stdout or ():
        sys.stderr.write(line)
        sys.stderr.flush()
        if ready_port(line) is not None:
            ready.set()


def _serving_failure(proc: subprocess.Popen[str], ready: threading.Event) -> str | None:
    """
    Wait for the bootstrap to serve.

    Args:
        proc: The Blender process.
        ready: Set by the output reader once the socket listens.

    Returns:
        str | None: Why Blender is not serving, or None once it is.

    """
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    while not ready.wait(0.1):
        if proc.poll() is not None:
            return f"Blender exited with code {proc.returncode} before serving"
        if time.monotonic() >= deadline:
            return f"Blender did not start serving within {READY_TIMEOUT_SECONDS} s"
    return None


def start_blender(argv: list[str], env: dict[str, str]) -> subprocess.Popen[str]:
    """
    Start the background Blender and wait until its socket serves.

    Blender runs in its own session, so a Ctrl-C at the terminal reaches only this process,
    whose cleanup stops Blender's whole process group.

    Args:
        argv: The Blender command line.
        env: Its environment.

    Returns:
        subprocess.Popen[str]: The serving Blender.

    Raises:
        SystemExit: Exit 2, after stopping Blender, if it cannot be launched, exits, or does not
            serve in time.

    """
    try:
        proc = subprocess.Popen(
            argv,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
    except OSError as exc:
        print(f"{PROG}: cannot start Blender {argv[0]}: {exc.strerror}", file=sys.stderr)
        raise SystemExit(2) from exc
    ready = threading.Event()
    threading.Thread(target=_forward_output, args=(proc, ready), daemon=True).start()
    try:
        failure = _serving_failure(proc, ready)
    except KeyboardInterrupt:
        stop_blender(proc)
        raise
    if failure is not None:
        stop_blender(proc)
        print(f"{PROG}: {failure}", file=sys.stderr)
        raise SystemExit(2)
    return proc


def stop_blender(proc: subprocess.Popen[str]) -> None:
    """
    Stop Blender's process group: SIGTERM, then SIGKILL after `STOP_TIMEOUT_SECONDS`.

    A Blender already reaped is left alone: its pid, and so its group id, may belong to
    another process by now.

    Args:
        proc: The Blender process, the leader of its own process group.

    """
    if proc.returncode is not None:
        return
    with suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=STOP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()


def _emit(line: str) -> None:
    """
    Write one output line to stdout.

    Args:
        line: The line, without its newline.

    """
    print(line, flush=True)


def _interrupt(_signum: int, _frame: FrameType | None) -> None:
    """
    Turn SIGTERM into the KeyboardInterrupt a Ctrl-C raises, so both reach the same cleanup.

    Raises:
        KeyboardInterrupt: Always.

    """
    raise KeyboardInterrupt


def _interrupt_after_stopping(proc: subprocess.Popen[str]) -> Callable[[int, FrameType | None], None]:
    """
    Build the interrupt handler for while Blender serves: stop Blender, then interrupt.

    Blender must be gone before anything unwinds. A call in flight leaves a dispatch thread
    waiting on Blender's reply, and unwinding the MCP session closes that thread's socket: a
    socket closed under a waiting reader is not woken by Blender exiting afterwards, so the event
    loop's close would wait out the reply's whole timeout. Blender exiting first ends the wait.

    Args:
        proc: The serving Blender.

    Returns:
        The handler. It puts `_interrupt` back first, so a second signal during the stop
        interrupts the stop instead of re-entering it.

    """

    def handle(_signum: int, _frame: FrameType | None) -> None:
        for signum in _INTERRUPT_SIGNALS:
            signal.signal(signum, _interrupt)
        stop_blender(proc)
        raise KeyboardInterrupt

    return handle


async def _mounted_tools() -> frozenset[str]:
    """
    List the tools this process mounted, as a client sees them.

    Returns:
        frozenset[str]: The tool names.

    """
    async with create_connected_server_and_client_session(mcp) as client:
        return frozenset(tool.name for tool in (await client.list_tools()).tools)


async def _apply_through_client(calls: Sequence[Call]) -> int:
    """
    Run the calls through one in-process MCP client session.

    Args:
        calls: The calls.

    Returns:
        int: The calls that succeeded.

    """
    async with create_connected_server_and_client_session(mcp) as client:
        return await apply_calls(client.call_tool, calls, _emit)


def _run(args: argparse.Namespace, calls: Sequence[Call], start: StartBlender) -> int:
    """
    Check every tool is mounted, then run the calls against a Blender started for them.

    Args:
        args: The parsed arguments.
        calls: The run's calls.
        start: Starts Blender and waits until it serves.

    Returns:
        int: 0 every call ok, 1 one failed.

    """
    # Set before the first session, whose startup connects to Blender: that attempt must find
    # this run's port, never another Blender serving on the default one.
    port = free_port()
    os.environ["BLENDER_HOST"] = LOOPBACK
    os.environ["BLENDER_PORT"] = str(port)
    with asyncio.Runner() as runner:
        unmounted = first_unmounted(calls, runner.run(_mounted_tools()))
        if unmounted is not None:
            _emit(outcome_line(unmounted_outcome(unmounted, args.toolsets)))
            _emit(summary_line(0, len(calls)))
            return 1
        env = blender_env(
            os.environ,
            file_roots=args.file_roots,
            addon_dir=str(get_bundled_addon_path()),
            port=port,
            empty_scene=args.open is None,
        )
        proc = start(blender_argv(args.blender, args.open, str(BOOTSTRAP)), env)
        for signum in _INTERRUPT_SIGNALS:
            signal.signal(signum, _interrupt_after_stopping(proc))
        try:
            applied = runner.run(_apply_through_client(calls))
        finally:
            # Before the runner closes: see `_interrupt_after_stopping`.
            stop_blender(proc)
    _emit(summary_line(applied, len(calls)))
    return 0 if applied == len(calls) else 1


def main(argv: Sequence[str] | None = None, *, start: StartBlender = start_blender) -> int:
    """
    Run `blender-mcp run-calls`.

    Args:
        argv: The arguments after `run-calls`; None reads `sys.argv`.
        start: Starts Blender and waits until it serves.

    Returns:
        int: The exit code: 0 every call ok, 1 one failed, 2 an unusable calls file, 130 interrupted.
        A Blender that cannot be started or does not serve exits 2 from `start` instead.

    """
    args = build_parser().parse_args(argv)
    try:
        calls = read_calls_files(args.calls_files)
    except CallsFileError as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return 2
    # SIGTERM takes Ctrl-C's path, so either one reaches the cleanup that stops Blender. SIGINT
    # gets the same handler, which raises what Python's default one does, so that asyncio does
    # not swap in its own, which cancels the session before Blender can be stopped.
    previous_handlers = {signum: signal.signal(signum, _interrupt) for signum in _INTERRUPT_SIGNALS}
    try:
        return _run(args, calls, start)
    except KeyboardInterrupt:
        return _INTERRUPTED
    except BaseExceptionGroup as group:
        # Raised inside the MCP session, the interrupt arrives wrapped by its task groups.
        if group.subgroup(KeyboardInterrupt) is None:
            raise
        return _INTERRUPTED
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
