"""
`blender-mcp` CLI entrypoint: install-addon/addon-paths subcommands, or mcp.run().

stdio is the default transport and the contract every existing MCP client
configuration depends on. Streamable HTTP is opt-in, and the only supported HTTP
deployment binds loopback: hosting this server for remote clients is explicitly
not supported.

That is not caution for its own sake. This server exposes hundreds of tools that
drive Blender and read and write files, and it has no authentication of any kind
- none is implemented anywhere in this project. Its bind address is therefore
the whole of its access control, which makes the address a security decision
rather than a convenience setting, and makes widening it something the operator
has to ask for twice (see `_resolve_http_host`).

The supported shape is a server published on loopback - directly, or from a
container whose port compose publishes onto the host's 127.0.0.1, which is what
the Docker rig does - reached from another machine through an SSH tunnel, so
that somebody else's authentication sits in front of it.

A client that reaches the port from off-box and asks for it by its real address
is answered `421 Misdirected Request`. For a loopback-only server that is the
intended result and not a defect to be worked around. It is also not what keeps
the server safe: see `_serve_http` for what FastMCP's `Host` check does and, more
importantly, does not do.
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

# Hosts that reach no further than this machine, and so need no opt-in.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
# Hosts that are not addresses at all but "every interface". Named because the
# consequence is what matters: `0` and `::` widen the bind exactly as far as
# `0.0.0.0` does, and an empty value does too - which is why `_resolve_http_host`
# rejects an empty value outright instead of letting a default paper over it.
WILDCARD_HOSTS = frozenset({"0", "0.0.0.0", "::"})
# Spelled out rather than tested for mere presence, so that ALLOW_REMOTE=false
# cannot read as consent.
TRUTHY_VALUES = frozenset({"1", "true", "yes", "on"})
# An environment variable is unbounded, and a rejected one is quoted into a log.
MAX_ECHOED_CHARS = 40
# A port is at most five digits. Anything longer is rejected before int() sees it,
# because CPython caps string->int conversion at 4300 digits and raises its own
# ValueError past that - one that names neither this variable nor a clip of its
# value, defeating the whole point of validating here. Found by the revert matrix:
# the test meant to pin the clipping passed on CPython's message instead of ours.
MAX_PORT_DIGITS = len(str(MAX_PORT))


@dataclass(frozen=True)
class StdioConfig:
    """
    Serve stdio, the transport that has no address of its own.

    Frozen because it is read once at startup and then acted on: a later
    mutation would describe a server that is not the one running.

    Attributes:
        transport: Always "stdio". Present as a literal discriminant so a config
            can be dispatched on without an `isinstance` check.

    """

    transport: Literal["stdio"] = "stdio"


@dataclass(frozen=True)
class HttpConfig:
    """
    Serve streamable HTTP on one address that has already been validated.

    `host` and `port` are required, which is the point of this class existing
    separately from `StdioConfig`. The single config it replaced carried both as
    `None`-defaulted fields, so "HTTP with no address" was constructible and had
    to be excluded by an `assert` in `main`. `python -O` strips `assert`, and the
    illegal state it was guarding is not harmless: `uvicorn.Config(host=None)`
    binds every interface on both address families. Splitting by transport
    deletes the state instead of checking for it, so the optimiser has nothing to
    remove and the type checker narrows on `transport` without an assertion.

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

    The value is quoted because a rejection that does not show what it rejected
    sends the operator back to the shell to guess. It is clipped because nothing
    bounds an environment variable's length, and a 5,000-character value should
    not write 5,000 characters into the log on its way out.

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

    Rejected here, before anything binds, so the operator sees the variable name
    instead of a socket error from deep inside the server. `int()` alone is not
    that check: it accepts PEP 515 underscores, a leading sign and any Unicode
    decimal digit, so `8_000`, `+8000` and seven full-width digits (U+FF10 to
    U+FF19) all become 8000, and the value in the config stops being the value
    in use. `str.isdecimal()` does not narrow that on its own, because full-width
    digits are decimal - hence the ASCII check beside it. Only padding is
    forgiven, because shell and compose files add it routinely.

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

    Classification is explicit rather than incidental: the old code passed the
    host straight to the socket layer, so "every interface" was something that
    happened to certain spellings rather than a case anyone had decided about.

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
        bool: True only for an affirmative value, so that an explicit `false` or
            `0` leaves the bind closed rather than merely being present.

    """
    return env.get(HTTP_ALLOW_REMOTE_ENV, "").strip().lower() in TRUTHY_VALUES


def _resolve_http_host(env: Mapping[str, str]) -> str:
    """
    Read the HTTP bind address, and refuse to widen it by accident.

    Two accidents are refused. The first is an empty value: `env.get(name,
    default)` falls back only when the key is *absent*, so a compose key with
    nothing after the colon, a bare `-e BLENDERMCP_HTTP_HOST=`, or an unexpanded
    `${MCP_HOST}` all produced `""` - which binds every interface while looking
    like it asked for nothing. An empty value is a mistake rather than a choice,
    so it is rejected even when remote binds are allowed.

    The second is reaching past loopback at all. This server has no
    authentication, so that is a decision about who may drive Blender, and it has
    to be made in words: the host variable alone is not enough, the opt-in
    variable must agree.

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

    `main` exits on a misspelt transport because a silent fall back to stdio
    would leave the operator watching a port nothing will ever listen on. Setting
    the host and port but forgetting the transport is the likelier version of
    that mistake, and it used to be entirely silent. It is a warning and not an
    error: these variables have no meaning under stdio, and stdio is the
    transport almost every client asked for on purpose.

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

    stdio is the contract every existing MCP client configuration depends on, so
    it stays the default and is chosen by the absence of any setting. HTTP is
    opt-in, loopback-only, and validated in full here - address and port both -
    so that a misconfiguration is reported by its variable name before anything
    binds, rather than as an anonymous library error afterwards.

    Args:
        env: The environment to read, passed in rather than read from
            `os.environ` so the choice can be tested without mutating a process.

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
    """
    Serve streamable HTTP on an address the environment has already authorised.

    Only the bind address is changed here. FastMCP decides its DNS-rebinding
    protection inside `FastMCP.__init__`, keyed on the constructor's `host`, and
    `app.py` builds `mcp` with no host at all - so the default 127.0.0.1 turns
    the protection on, and rewriting `settings.host` afterwards genuinely cannot
    turn it off. That is what lets the container bind 0.0.0.0 for Docker's port
    forward without losing the check.

    What that check is worth needs stating plainly, because it is easy to read as
    more than it is. `Host` and `Origin` validation exists to stop a *browser*
    being used as a confused deputy through DNS rebinding. It is not an access
    control: `Host` is a header the client chooses, so any non-browser client
    that sends `Host: 127.0.0.1` is served normally from anywhere on the network,
    while an honest remote client asking by real address gets 421. Loopback
    binding, not this check, is what makes the server unreachable, so none of
    this makes a 0.0.0.0 bind safe.

    Args:
        host: The bind address, already stripped and authorised.
        port: The bind port, already range-checked.

    """
    mcp.settings.host = host
    mcp.settings.port = port
    # Announced as an attempt, not as an outcome: mcp.run() blocks until the
    # server stops, so there is no later point at which to report a successful
    # bind, and the previous wording left a log claiming to be serving an
    # address the bind had in fact just failed to acquire.
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
        # Exit on the configuration itself rather than serving the default
        # transport: a typo that silently fell back to stdio would leave the
        # operator watching a port nothing will ever listen on.
        raise SystemExit(f"blender-mcp: {exc}") from exc

    if config.transport == "streamable-http":
        # Narrowed by the literal discriminant, not by an assertion: an HTTP
        # config cannot exist without an address, so there is nothing here for
        # `python -O` to strip and nothing that can fail open.
        _serve_http(config.host, config.port)
        return

    _log_stdio_hint_when_interactive()
    mcp.run()


if __name__ == "__main__":
    main()
