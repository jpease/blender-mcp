"""
Drive one add-on provider fetch from a tool: start it, report its progress, cancel it with the call.

The add-on runs Poly Haven and Sketchfab network I/O on worker threads
(`bundled/addon/provider_fetches.py`), so no command holds Blender's main thread for a
download. A tool therefore sends a `start_*` command, which answers at once with the
fetch's status, and polls `get_provider_fetch` until it finishes, relaying the byte
count to the client as MCP progress. If the client cancels the call, the fetch is
cancelled in Blender too. Every reply's `warnings` - the add-on's scene notice may be on
any of them - is collected so the tool can put each in its envelope.
"""

import asyncio
import logging

from typing import Any

from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError

from ._dispatch import send_blender_command

logger = logging.getLogger("BlenderMCPServer")

_FIRST_POLL_SECONDS = 0.1
_MAX_POLL_SECONDS = 1.0
_POLL_BACKOFF = 1.5


async def _report_progress(ctx: Context | None, status: dict[str, Any], reported: list[int]) -> None:
    """
    Relay a fetch's byte count to the client, once per change so progress only grows.

    Args:
        ctx: The call's context, or None when a tool is called directly.
        status: The latest status reply.
        reported: One-element holder of the last byte count sent.

    """
    received = int(status.get("bytes_received") or 0)
    if ctx is None or received <= reported[0]:
        return
    reported[0] = received
    total = status.get("bytes_total") or None
    await ctx.report_progress(received, total, message=f"Blender is {status.get('stage', 'fetching')}")


async def _discard(fetch_id: str, notices: list[str]) -> None:
    """
    Cancel or release one fetch in Blender, best effort.

    Args:
        fetch_id: The fetch.
        notices: Where the reply's warnings are collected.

    """
    try:
        reply = await send_blender_command("cancel_provider_fetch", {"fetch_id": fetch_id})
    except Exception as exc:
        # The fetch still expires on its own; a dead socket must not hide the original outcome.
        logger.warning("Could not release provider fetch %s: %s", fetch_id, exc)
        return
    notices.extend(reply.get("warnings", []))


async def _until_finished(
    ctx: Context | None, fetch_id: str, status: dict[str, Any], notices: list[str]
) -> dict[str, Any]:
    """
    Poll one fetch until it leaves RUNNING.

    Args:
        ctx: The call's context, for progress.
        fetch_id: The fetch.
        status: The status the start command answered with.
        notices: Where each reply's warnings are collected.

    Returns:
        dict[str, Any]: The final status.

    """
    reported = [0]
    delay = _FIRST_POLL_SECONDS
    while status.get("state") == "RUNNING":
        await _report_progress(ctx, status, reported)
        await asyncio.sleep(delay)
        delay = min(delay * _POLL_BACKOFF, _MAX_POLL_SECONDS)
        status = await send_blender_command("get_provider_fetch", {"fetch_id": fetch_id})
        notices.extend(status.get("warnings", []))
    await _report_progress(ctx, status, reported)
    return status


async def run_provider_fetch(
    ctx: Context | None, command: str, params: dict[str, Any]
) -> tuple[str, dict[str, Any], list[str]]:
    """
    Start one fetch and wait for it to succeed.

    A fetch that fails or is cancelled is discarded in Blender and raised. A call the
    client cancels cancels the fetch in Blender before the cancellation propagates.

    Args:
        ctx: The call's context, for progress.
        command: The add-on's `start_*` command.
        params: Its parameters.

    Returns:
        tuple[str, dict[str, Any], list[str]]: The fetch id, its final status, and every
        reply's warnings so far.

    Raises:
        ToolError: When the start is refused, or the fetch failed or was cancelled; the
            message carries any notice the replies held.

    """
    status = await send_blender_command(command, params)
    notices = list(status.get("warnings", []))
    fetch_id = str(status["fetch_id"])
    try:
        status = await _until_finished(ctx, fetch_id, status, notices)
    except (asyncio.CancelledError, Exception):
        await asyncio.shield(_discard(fetch_id, notices))
        raise
    if status.get("state") != "SUCCEEDED":
        await _discard(fetch_id, notices)
        failure = status.get("failure") or f"The provider fetch was {str(status.get('state')).lower()} in Blender."
        raise ToolError(" ".join([failure, *notices]))
    return fetch_id, status, notices


async def fetch_provider_result(ctx: Context | None, command: str, params: dict[str, Any]) -> dict[str, Any]:
    """
    Run one provider query to completion and return its result.

    Args:
        ctx: The call's context, for progress.
        command: The add-on's `start_*` query command.
        params: Its parameters.

    Returns:
        dict[str, Any]: The query's result, with every reply's notice under `warnings`
        as if it were one reply. The fetch is released in Blender before this returns.

    Raises:
        ToolError: As `run_provider_fetch` raises it.

    """
    fetch_id, status, notices = await run_provider_fetch(ctx, command, params)
    result = dict(status.get("result") or {})
    await _discard(fetch_id, notices)
    result["warnings"] = [*notices, *result.get("warnings", [])]
    return result


async def import_fetched(
    ctx: Context | None, start: str, params: dict[str, Any], command: str, **extra: object
) -> dict:
    """
    Download through one start command, then hand the finished fetch to its import command.

    Args:
        ctx: The call's context, for progress.
        start: The add-on's `start_*` download command.
        params: Its parameters.
        command: The import command that takes the finished `fetch_id`.
        **extra: The import command's other parameters.

    Returns:
        dict: The import's reply, with the fetch's notices ahead of its own `warnings`.

    Raises:
        ToolError: As `run_provider_fetch` raises it, or when the import is refused.

    """
    fetch_id, _status, notices = await run_provider_fetch(ctx, start, params)
    try:
        result = await send_blender_command(command, {"fetch_id": fetch_id, **extra})
    except (asyncio.CancelledError, Exception):
        # An import refused before it took the fetch leaves it held; releasing one it took is a no-op.
        await asyncio.shield(_discard(fetch_id, notices))
        raise
    if isinstance(result, dict):
        result = {**result, "warnings": [*notices, *result.get("warnings", [])]}
    return result
