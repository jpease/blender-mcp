"""`blender-mcp` CLI entrypoint: install-addon/addon-paths subcommands, or mcp.run()."""

import logging
import os
import sys

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from ..addon_manager import run_cli as run_addon_cli
from .app import mcp

logger = logging.getLogger("BlenderMCPServer")

TRANSPORT_ENV = "BLENDERMCP_TRANSPORT"
HTTP_HOST_ENV = "BLENDERMCP_HTTP_HOST"
HTTP_PORT_ENV = "BLENDERMCP_HTTP_PORT"
DEFAULT_HTTP_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 8000
MAX_PORT = 65535


@dataclass(frozen=True)
class TransportConfig:
    """
    How the MCP server talks to its client; host/port apply to HTTP only.

    Frozen because it is read once at startup and then used to bind a socket:
    a later mutation would describe a server that is not the one running.

    Attributes:
        transport: The transport `mcp.run()` is asked to serve.
        host: Bind address, set for HTTP only; None under stdio, which has none.
        port: Bind port, set for HTTP only; None under stdio, which has none.

    """

    transport: Literal["stdio", "streamable-http"]
    host: str | None = None
    port: int | None = None


def _parse_port(raw: str) -> int:
    # A port is rejected here, before anything binds, so the operator sees the
    # variable name rather than a socket error from deep inside the server.
    try:
        port = int(raw)
    except ValueError:
        port = 0
    if not 1 <= port <= MAX_PORT:
        raise ValueError(f"{HTTP_PORT_ENV} must be a port number between 1 and {MAX_PORT}, got {raw!r}")
    return port


def transport_from_env(env: Mapping[str, str]) -> TransportConfig:
    """
    Choose the client transport: stdio by default, streamable HTTP on request.

    stdio is the contract every existing MCP client configuration depends on, so
    it stays the default and is chosen by the absence of any setting. HTTP is for
    hosting the server beside a Blender on another machine (the Docker rig models
    this). It binds loopback unless told otherwise, and has no authentication of
    its own, so widening the bind address is always a deliberate act.

    Args:
        env: The environment to read, passed in rather than read from
            `os.environ` so the choice can be tested without mutating a process.

    Returns:
        TransportConfig: The transport to serve, with host/port set for HTTP.

    Raises:
        ValueError: If the transport name or HTTP port is invalid.

    """
    name = env.get(TRANSPORT_ENV, "stdio").strip().lower()
    if name == "stdio":
        return TransportConfig(transport="stdio")
    if name == "http":
        return TransportConfig(
            transport="streamable-http",
            host=env.get(HTTP_HOST_ENV, DEFAULT_HTTP_HOST),
            port=_parse_port(env.get(HTTP_PORT_ENV, str(DEFAULT_HTTP_PORT))),
        )
    raise ValueError(f"{TRANSPORT_ENV} must be 'stdio' or 'http', got {name!r}")


def _log_stdio_hint_when_interactive() -> None:
    # When run by hand (stdin is a TTY) the server appears to "hang" while it
    # silently waits for an MCP client; log a hint so that state is obvious.
    # Launched by a client, stdin is a pipe so this is skipped, and logging goes
    # to stderr, never to the stdio protocol on stdout.
    try:
        interactive = sys.stdin.isatty()
    except (AttributeError, OSError):
        interactive = False
    if interactive:
        logger.info(
            "BlenderMCP is an MCP server and is meant to be launched by your MCP "
            "client (Claude Desktop, Cursor, VS Code, ...), not run by hand. "
            "It will now wait silently for a client on stdin -- that is normal, "
            "not a hang. Press Ctrl-C to exit. "
            "Setup guide: https://github.com/ahujasid/blender-mcp#installation "
            "(if the addon is outdated this logs how to update it: blender-mcp install-addon)"
        )


def _serve_http(host: str, port: int) -> None:
    # Only the bind address changes. FastMCP decided its DNS-rebinding
    # protection when `mcp` was built for loopback, and that must survive a
    # 0.0.0.0 bind (e.g. inside a container behind a loopback port publish),
    # or a malicious web page could drive Blender through the published port.
    mcp.settings.host = host
    mcp.settings.port = port
    logger.info(f"BlenderMCP serving streamable HTTP at http://{host}:{port}{mcp.settings.streamable_http_path}")
    mcp.run(transport="streamable-http")


def main() -> None:
    """
    Run the MCP server, or addon install CLI subcommands.

    Raises:
        SystemExit: If the operation cannot be completed.

    """
    if len(sys.argv) > 1 and sys.argv[1] in {
        "install-addon",
        "addon-paths",
        "-h",
        "--help",
    }:
        code = run_addon_cli(sys.argv[1:])
        if code >= 0:
            raise SystemExit(code)

    try:
        config = transport_from_env(os.environ)
    except ValueError as exc:
        # Exit on the configuration itself rather than serving the default
        # transport: a typo that silently fell back to stdio would leave the
        # operator watching a port nothing will ever listen on.
        raise SystemExit(f"blender-mcp: {exc}") from exc

    if config.transport == "streamable-http":
        # transport_from_env only ever returns HTTP with both set; narrowing is
        # for the type checker, and a wrong answer here would bind the default
        # loopback address while the operator believes they asked for another.
        assert config.host is not None and config.port is not None
        _serve_http(config.host, config.port)
        return

    _log_stdio_hint_when_interactive()
    mcp.run()


if __name__ == "__main__":
    main()
