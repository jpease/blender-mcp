"""
Shared helpers for the rig scenarios that drive the shipped MCP server over stdio.

Two scenarios now spawn `.venv/bin/blender-mcp` as a child process and talk to it as a real
MCP host would - `scenario_render_progress.py` (progress notifications and cancellation) and
`scenario_render_overhead.py` (orchestrated vs single-call timing). Everything they share
about *being* that host lives here: how the child is launched, how one tool reply is read
out of a `CallToolResult`, and how an anyio task-group failure is reduced to the exception
that actually failed.

Scenarios are loaded by absolute path (`scripts/blender_rig.py`), so their directory is not
on `sys.path` and a plain `import` of this module does not resolve. Each consumer loads it
with `importlib.util.spec_from_file_location`, the same way scenarios already load
`scenario_file_swap_barrier.py`.
"""

import json

from pathlib import Path
from typing import Protocol

from mcp import StdioServerParameters, types

REPO_ROOT = Path(__file__).resolve().parents[2]
# The shipped console script (pyproject's [project.scripts]), not an ad hoc -c invocation:
# what an MCP host would actually launch.
SERVER_BINARY = REPO_ROOT / ".venv" / "bin" / "blender-mcp"
STARTUP_TIMEOUT_SECONDS = 60.0


class Rig(Protocol):
    """The part of `BlenderRig` these scenarios use (a Protocol: the rig loads scenarios by path)."""

    work_dir: Path

    def send(self, command_type: str, params: dict | None = None) -> dict:
        """
        Send one addon command and return its decoded response.

        Args:
            command_type: The addon command name.
            params: Command parameters; omitted means none.

        Returns:
            dict: The decoded response.

        """
        ...


def server_parameters(port: int, toolsets: str) -> StdioServerParameters:
    """
    Describe the server child process, aimed at the rig's Blender.

    `BLENDER_PORT`/`BLENDER_HOST` are the only knobs `get_blender_connection` reads
    (`server/connection.py`), so a private port needs no code change - but `stdio_client`
    does not inherit this process's environment, it merges its own default subset with
    whatever is passed here, so they must be named explicitly.

    `BLENDER_MCP_TOOLSETS` is just as load-bearing: the default catalog is core-only and
    carries no `render_scene` at all, so without it a scenario would connect to a healthy
    server that simply does not advertise the tool under test - which is what an MCP host's
    own config selects, not something the harness may skip.

    Args:
        port: Port the rig's Blender addon is listening on.
        toolsets: Value for `BLENDER_MCP_TOOLSETS`, naming the catalog the tool lives in.

    Returns:
        StdioServerParameters: Launch description for the shipped console script.

    """
    return StdioServerParameters(
        command=str(SERVER_BINARY),
        env={"BLENDER_HOST": "localhost", "BLENDER_PORT": str(port), "BLENDER_MCP_TOOLSETS": toolsets},
        cwd=str(REPO_ROOT),
    )


def payload(result: types.CallToolResult) -> dict:
    """
    Read one tool reply's envelope out of an MCP result.

    Raises `RuntimeError`, never `SystemExit`: this runs inside anyio task groups, which
    bundle a `BaseException` into an `ExceptionGroup` the rig can only report as "a request
    to stop this process", losing the message that says what actually failed.

    Args:
        result: The `tools/call` result.

    Returns:
        dict: The envelope the tool returned.

    Raises:
        RuntimeError: If the call failed, or carried no readable JSON payload.

    """
    if result.isError:
        text = "; ".join(getattr(item, "text", "") for item in result.content)
        raise RuntimeError(f"tool call failed: {text}")
    body = result.structuredContent
    if body is None:
        for item in result.content:
            text = getattr(item, "text", None)
            if text:
                body = json.loads(text)
                break
    if not isinstance(body, dict):
        raise RuntimeError(f"tool reply carried no JSON object: {result.content}")
    # FastMCP wraps a non-model return under "result" when it has to; unwrap to the envelope.
    if "ok" not in body and isinstance(body.get("result"), dict):
        body = body["result"]
    if body.get("ok") is not True:
        raise RuntimeError(f"tool reported failure: {body}")
    return body


def unwrapped(error: BaseException) -> BaseException:
    """
    Reduce an anyio task-group failure to the exception that actually failed.

    Every failure inside a task group arrives as an `ExceptionGroup`, and the rig reports one
    as `ExceptionGroup: unhandled errors in a TaskGroup` with no message from the assertion
    that fired - which makes a red run unreadable. Nested groups are flattened; the first
    leaf is the failure, since these scenarios never run two checks concurrently.

    Args:
        error: The exception the scenario's event loop raised.

    Returns:
        BaseException: The innermost non-group exception, or `error` itself.

    """
    while isinstance(error, BaseExceptionGroup) and error.exceptions:
        error = error.exceptions[0]
    return error
