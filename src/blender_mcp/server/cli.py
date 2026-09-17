"""
`blender-mcp` CLI entrypoint: install-addon/addon-paths subcommands, or mcp.run().

stdio is the default, and every existing client configuration depends on it. Streamable
HTTP is opt-in and supported only on loopback, reached remotely through an SSH tunnel.

The server has no authentication, so its bind address is its only access control. Widening
it takes two settings (see `_resolve_http_host`).
"""

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
HTTP_ALLOW_REMOTE_ENV = "BLENDERMCP_HTTP_ALLOW_REMOTE"
HTTP_ONLY_ENVS = (HTTP_HOST_ENV, HTTP_PORT_ENV, HTTP_ALLOW_REMOTE_ENV)
DEFAULT_HTTP_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 8000
MAX_PORT = 65535

# Need no opt-in.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
# Every interface. An empty value binds every interface too; `_resolve_http_host` rejects it.
WILDCARD_HOSTS = frozenset({"0", "0.0.0.0", "::"})
# Listed, not a presence check, so ALLOW_REMOTE=false is not consent.
TRUTHY_VALUES = frozenset({"1", "true", "yes", "on"})
# A rejected value is quoted into the log, and its length is unbounded.
MAX_ECHOED_CHARS = 40
# Checked before int(), which raises its own unhelpful error past 4300 digits.
MAX_PORT_DIGITS = len(str(MAX_PORT))


@dataclass(frozen=True)
class StdioConfig:
    """
    Serve stdio, the transport that has no address of its own.

    Frozen so the config cannot drift from the server actually running.

    Attributes:
        transport: Always "stdio"; the discriminant `main` reads.

    """

    transport: Literal["stdio"] = "stdio"


@dataclass(frozen=True)
class HttpConfig:
    """
    Serve streamable HTTP on one address that has already been validated.

    Separate from `StdioConfig` so HTTP without an address cannot be built:
    `uvicorn.Config(host=None)` binds every interface.

    Attributes:
        host: Bind address, stripped and policy-checked by `transport_from_env`.
        port: Bind port, range-checked by `transport_from_env`.
        transport: Always "streamable-http"; the discriminant `main` reads.

    """

    host: str
    port: int
    transport: Literal["streamable-http"] = "streamable-http"


type TransportConfig = StdioConfig | HttpConfig


def _echoed(raw: str) -> str:
    """
    Quote an environment value for an error message, clipped to a sane length.

    Args:
        raw: The offending value, exactly as it came from the environment.

    Returns:
        str: The value quoted, with an ellipsis if it had to be clipped.

    """
    clipped = raw[:MAX_ECHOED_CHARS]
    return f"{clipped!r}..." if len(raw) > MAX_ECHOED_CHARS else repr(clipped)


def _parse_port(env: Mapping[str, str]) -> int:
    """
    Read the HTTP port, rejecting anything that is not plainly a port number.

    `int()` alone accepts `8_000`, `+8000` and full-width digits, so the configured text
    would not be what the operator sees served. Full-width digits also pass `isdecimal()`,
    hence the ASCII check. Surrounding whitespace is allowed.

    Args:
        env: The environment to read the port variable from.

    Returns:
        int: A port in 1..MAX_PORT.

    Raises:
        ValueError: If the value is not a plain decimal port in range.

    """
    raw = env.get(HTTP_PORT_ENV, str(DEFAULT_HTTP_PORT))
    digits = raw.strip()
    port = int(digits) if len(digits) <= MAX_PORT_DIGITS and digits.isascii() and digits.isdecimal() else 0
    if not 1 <= port <= MAX_PORT:
        raise ValueError(f"{HTTP_PORT_ENV} must be a port number between 1 and {MAX_PORT}, got {_echoed(raw)}")
    return port


def _bind_reach(host: str) -> Literal["loopback", "wildcard", "remote"]:
    """
    Classify a bind address by who can reach the server through it.

    Args:
        host: A stripped, non-empty host as the operator wrote it.

    Returns:
        Literal["loopback", "wildcard", "remote"]: "loopback" for this machine
            only, "wildcard" for every interface, "remote" for a specific
            address or name that may be reachable from elsewhere.

    """
    name = host.lower()
    if name in LOOPBACK_HOSTS:
        return "loopback"
    if name in WILDCARD_HOSTS:
        return "wildcard"
    return "remote"


def _allows_remote(env: Mapping[str, str]) -> bool:
    """
    Report whether the operator has explicitly accepted a non-loopback bind.

    Args:
        env: The environment to read the opt-in variable from.

    Returns:
        bool: True only for an affirmative value; an explicit `false` or `0`
            leaves the bind closed.

    """
    return env.get(HTTP_ALLOW_REMOTE_ENV, "").strip().lower() in TRUTHY_VALUES


def _resolve_http_host(env: Mapping[str, str]) -> str:
    """
    Read the HTTP bind address, and refuse to widen it by accident.

    An empty value is refused even with the opt-in: the default applies only when the
    variable is absent, so an empty compose key or unset `${VAR}` would bind every
    interface. A non-loopback host also needs `BLENDERMCP_HTTP_ALLOW_REMOTE`, because
    the server has no authentication.

    Args:
        env: The environment to read the host and opt-in variables from.

    Returns:
        str: The bind address, stripped of the padding env values collect.

    Raises:
        ValueError: If the host is empty, or is non-loopback without the opt-in.

    """
    raw = env.get(HTTP_HOST_ENV, DEFAULT_HTTP_HOST)
    host = raw.strip()
    if not host:
        raise ValueError(
            f"{HTTP_HOST_ENV} is set but empty, which binds every network interface rather than "
            f"falling back to {DEFAULT_HTTP_HOST}; unset it, or give it an address"
        )

    reach = _bind_reach(host)
    if reach == "loopback":
        return host

    widening = "binds every network interface" if reach == "wildcard" else "may be reachable from other machines"
    if not _allows_remote(env):
        raise ValueError(
            f"{HTTP_HOST_ENV}={_echoed(raw)} {widening}, and this server has no authentication: "
            f"anything that can reach it can drive Blender and write files. Bind {DEFAULT_HTTP_HOST} and "
            f"reach it through an SSH tunnel, or set {HTTP_ALLOW_REMOTE_ENV}=1 to accept that risk"
        )
    logger.warning(
        f"{HTTP_ALLOW_REMOTE_ENV} is set, so BlenderMCP will bind {host}, which {widening}. "
        f"This server has no authentication: anyone who can reach this address can drive Blender "
        f"and write files as this user."
    )
    return host


def _warn_about_ignored_http_settings(env: Mapping[str, str]) -> None:
    """
    Say so when HTTP settings are present but the transport was left at stdio.

    Otherwise an operator who forgot the transport waits on a port nothing listens on.
    A warning, not an error, because the variables are harmless under stdio.

    Args:
        env: The environment to check for HTTP-only variables.

    """
    ignored = [name for name in HTTP_ONLY_ENVS if name in env]
    if ignored:
        logger.warning(
            f"{', '.join(ignored)} set, but {TRANSPORT_ENV} is not 'http', so the server is serving "
            f"stdio and nothing will listen on an HTTP port. Set {TRANSPORT_ENV}=http to serve HTTP."
        )


def transport_from_env(env: Mapping[str, str]) -> TransportConfig:
    """
    Choose the client transport: stdio by default, streamable HTTP on request.

    HTTP settings are validated here, before anything binds, so an error names the
    variable rather than surfacing later as a library error.

    Args:
        env: The environment to read.

    Returns:
        TransportConfig: `StdioConfig`, or an `HttpConfig` carrying an address
            that has already been normalised, range-checked and authorised.

    Raises:
        ValueError: If the transport name, HTTP host or HTTP port is invalid, or
            if a non-loopback bind was asked for without the opt-in.

    """
    name = env.get(TRANSPORT_ENV, "stdio").strip().lower()
    if name == "stdio":
        _warn_about_ignored_http_settings(env)
        return StdioConfig()
    if name == "http":
        return HttpConfig(host=_resolve_http_host(env), port=_parse_port(env))
    raise ValueError(f"{TRANSPORT_ENV} must be 'stdio' or 'http', got {name!r}")


def _log_stdio_hint_when_interactive() -> None:
    # Run by hand, the server looks hung while it waits for a client. Logging goes to
    # stderr, so it cannot corrupt the protocol on stdout.
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
    """
    Serve streamable HTTP on an address the environment has already authorised.

    FastMCP enables DNS-rebinding protection in its constructor, from the default
    loopback host, so changing `settings.host` here keeps it on even for 0.0.0.0 in
    Docker. That check is not access control: any non-browser client can send
    `Host: 127.0.0.1`. Only a loopback bind keeps other machines out.

    Args:
        host: The bind address, already stripped and authorised.
        port: The bind port, already range-checked.

    """
    mcp.settings.host = host
    mcp.settings.port = port
    # Announced as an attempt, not as an outcome: mcp.run() blocks until the
    # server stops, so there is no later point at which to report a successful
    # bind, and a bind that fails must not leave a log claiming to serve the
    # address.
    logger.info(f"BlenderMCP starting streamable HTTP on http://{host}:{port}{mcp.settings.streamable_http_path}")
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
        # Not a stdio fallback: a typo would leave the operator waiting on a dead port.
        raise SystemExit(f"blender-mcp: {exc}") from exc

    if config.transport == "streamable-http":
        _serve_http(config.host, config.port)
        return

    _log_stdio_hint_when_interactive()
    mcp.run()


if __name__ == "__main__":
    main()
