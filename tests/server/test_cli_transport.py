"""
Transport selection for the `blender-mcp` entrypoint.

stdio is the contract every existing MCP client config depends on, so it must
stay the default. Streamable HTTP is opt-in and loopback-only: the server has no
authentication of any kind, so its bind address is the whole of its access
control, and every test here exists to keep that address deliberate.
"""

import logging
import subprocess
import sys

from typing import Any

import anyio
import httpx
import pytest

from blender_mcp.server import cli
from blender_mcp.server.app import mcp
from blender_mcp.server.cli import (
    DEFAULT_HTTP_PORT,
    HTTP_ALLOW_REMOTE_ENV,
    HTTP_HOST_ENV,
    HTTP_PORT_ENV,
    TRANSPORT_ENV,
    HttpConfig,
    StdioConfig,
    transport_from_env,
)

HTTP_ENV = {TRANSPORT_ENV: "http"}
ALLOWED_REMOTE_ENV = {TRANSPORT_ENV: "http", HTTP_ALLOW_REMOTE_ENV: "1"}
# An error naming a variable and quoting a clip of its value fits well inside this.
MAX_SANE_ERROR_CHARS = 200
FULLWIDTH_ZERO = 0xFF10


def _fullwidth(digits: str) -> str:
    """
    Rewrite ASCII digits as their full-width equivalents, U+FF10 to U+FF19.

    Built rather than pasted because the glyphs are near-indistinguishable from
    ASCII in a source file - which is exactly why `int()` accepting them is worth
    a test, and why pasting them here would be its own small trap.

    Args:
        digits: ASCII decimal digits.

    Returns:
        str: The same digits as full-width characters.

    """
    return "".join(chr(FULLWIDTH_ZERO + int(digit)) for digit in digits)


def test_stdio_is_the_default() -> None:
    """Existing client configs launch the server with no transport settings."""
    assert transport_from_env({}) == StdioConfig()


def test_http_defaults_to_loopback_on_port_8000() -> None:
    """Opting into HTTP alone must not expose the server beyond this machine."""
    assert transport_from_env(HTTP_ENV) == HttpConfig(host="127.0.0.1", port=8000)


def test_http_host_and_port_are_configurable() -> None:
    """A container needs 0.0.0.0 so Docker's port forward can reach the server."""
    config = transport_from_env(ALLOWED_REMOTE_ENV | {HTTP_HOST_ENV: "0.0.0.0", HTTP_PORT_ENV: "9000"})
    assert config == HttpConfig(host="0.0.0.0", port=9000)


def test_transport_name_ignores_case_and_whitespace() -> None:
    """Env values often carry stray casing or padding from shell and compose files."""
    assert transport_from_env({TRANSPORT_ENV: " HTTP "}).transport == "streamable-http"


def test_http_settings_are_ignored_under_stdio(caplog: pytest.LogCaptureFixture) -> None:
    """
    A stray port must not break the default path it has no meaning for.

    It must not pass unremarked either. Setting HTTP variables and forgetting
    `BLENDERMCP_TRANSPORT` is the likelier mistake than misspelling the
    transport, and it produces exactly the outcome the exit on a bad transport
    exists to prevent: an operator watching a port nothing will ever listen on.

    Args:
        caplog: Fixture used to read the warning that names the ignored variable.

    """
    with caplog.at_level(logging.WARNING):
        assert transport_from_env({HTTP_PORT_ENV: "not-a-port"}) == StdioConfig()

    assert HTTP_PORT_ENV in caplog.text and TRANSPORT_ENV in caplog.text
    assert "stdio" in caplog.text


def test_stdio_without_http_settings_warns_about_nothing(caplog: pytest.LogCaptureFixture) -> None:
    """
    The default path must stay silent, or the warning above becomes noise to ignore.

    Args:
        caplog: Fixture used to assert no warning was emitted.

    """
    with caplog.at_level(logging.WARNING):
        transport_from_env({})

    assert caplog.records == []


def test_unknown_transport_is_rejected() -> None:
    """A typo must fail loudly rather than silently serving stdio nobody reads."""
    with pytest.raises(ValueError, match=TRANSPORT_ENV):
        transport_from_env({TRANSPORT_ENV: "sse"})


@pytest.mark.parametrize("port", ["abc", "0", "65536", "-1"])
def test_invalid_http_port_is_rejected(port: str) -> None:
    """
    A bad port is reported by its variable name before anything binds.

    Args:
        port: The rejected value.

    """
    with pytest.raises(ValueError, match=HTTP_PORT_ENV):
        transport_from_env(HTTP_ENV | {HTTP_PORT_ENV: port})


@pytest.mark.parametrize("port", ["8_000", "+8000", _fullwidth("123"), _fullwidth("0008000")])
def test_port_syntax_no_port_includes_is_rejected(port: str) -> None:
    """
    `int()` is far more liberal than port syntax, so the value logged is not the value used.

    It accepts PEP 515 underscores, a leading sign and any Unicode decimal digit,
    each of which silently renames the port: `8_000` and seven full-width digits
    both become 8000. An operator reading `8_000` back out of a config has no way
    to tell which of those the server actually bound.

    Args:
        port: The rejected value.

    """
    with pytest.raises(ValueError, match=HTTP_PORT_ENV):
        transport_from_env(HTTP_ENV | {HTTP_PORT_ENV: port})


def test_padded_http_port_is_accepted() -> None:
    """Padding is the one liberty worth keeping: compose files add it routinely."""
    padded = f"  {DEFAULT_HTTP_PORT}  "
    assert transport_from_env(HTTP_ENV | {HTTP_PORT_ENV: padded}).port == DEFAULT_HTTP_PORT


def test_a_rejected_value_is_not_echoed_whole_into_the_log() -> None:
    """
    A variable is attacker- or accident-shaped input, so the error quotes only a clip of it.

    Both halves are asserted, and the second one is why. A length check alone
    could not tell this function's own error from CPython's: `int()` refuses a
    string of more than 4300 digits with a ValueError of its own, about 140
    characters long, which sailed under the limit while naming neither the
    variable nor a clip of its value. The revert matrix caught that - the row
    for the length bound survived - so the contract is now pinned by what the
    message *says*, not only by how long it is.
    """
    with pytest.raises(ValueError) as caught:
        transport_from_env(HTTP_ENV | {HTTP_PORT_ENV: "9" * 5000})

    assert len(str(caught.value)) < MAX_SANE_ERROR_CHARS
    assert HTTP_PORT_ENV in str(caught.value), (
        f"the rejection must name the variable rather than leaving CPython to explain it: {caught.value}"
    )


@pytest.mark.parametrize("host", ["", " ", "\t\n"])
def test_empty_http_host_is_rejected(host: str) -> None:
    """
    A set-but-empty variable is not the default: `bind("")` binds every interface.

    `env.get(name, default)` falls back only when the key is absent, and an empty
    value arrives from several ordinary accidents - a compose key with nothing
    after the colon, `-e BLENDERMCP_HTTP_HOST=`, an unexpanded `${MCP_HOST}`. Each
    one used to bind 0.0.0.0 while looking like it had asked for nothing at all.

    Args:
        host: The rejected value.

    """
    with pytest.raises(ValueError, match=HTTP_HOST_ENV):
        transport_from_env(HTTP_ENV | {HTTP_HOST_ENV: host})


def test_an_empty_host_is_rejected_even_with_remote_binds_allowed() -> None:
    """Allowing remote binds authorises an address, not a variable that failed to expand."""
    with pytest.raises(ValueError, match=HTTP_HOST_ENV):
        transport_from_env(ALLOWED_REMOTE_ENV | {HTTP_HOST_ENV: ""})


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost", "LOCALHOST"])
def test_loopback_hosts_need_no_opt_in(host: str) -> None:
    """
    The supported deployment binds loopback, so it must stay the frictionless one.

    Args:
        host: A loopback spelling, including the case DNS treats as equivalent.

    """
    assert transport_from_env(HTTP_ENV | {HTTP_HOST_ENV: f" {host} "}).host == host


@pytest.mark.parametrize("host", ["0", "0.0.0.0", "::", "192.168.1.5", "example.internal"])
def test_a_non_loopback_bind_is_refused_without_the_opt_in(host: str) -> None:
    """
    Reaching past loopback is an authentication decision, and there is no authentication.

    `0` and `::` matter as much as `0.0.0.0`: all three are "every interface" to
    the socket layer, so they are classified as wildcards rather than treated as
    ordinary addresses that happen to work.

    Args:
        host: A wildcard or routable address that requires the opt-in.

    """
    with pytest.raises(ValueError, match=HTTP_HOST_ENV) as caught:
        transport_from_env(HTTP_ENV | {HTTP_HOST_ENV: host})

    assert HTTP_ALLOW_REMOTE_ENV in str(caught.value)
    assert "authentication" in str(caught.value)


@pytest.mark.parametrize("allow", ["0", "false", "no", "off", "", " ", "maybe"])
def test_a_falsy_opt_in_does_not_open_the_bind(allow: str) -> None:
    """
    `ALLOW_REMOTE=false` must not read as consent, which a mere presence check would.

    Args:
        allow: A value that does not grant the opt-in.

    """
    with pytest.raises(ValueError, match=HTTP_ALLOW_REMOTE_ENV):
        transport_from_env(HTTP_ENV | {HTTP_HOST_ENV: "0.0.0.0", HTTP_ALLOW_REMOTE_ENV: allow})


@pytest.mark.parametrize("allow", ["1", "true", "TRUE", " yes ", "on"])
def test_the_opt_in_honours_the_usual_spellings_of_yes(allow: str) -> None:
    """
    An operator who means yes should not have to guess which word this reader accepts.

    Args:
        allow: A value that grants the opt-in.

    """
    assert transport_from_env(HTTP_ENV | {HTTP_HOST_ENV: "0.0.0.0", HTTP_ALLOW_REMOTE_ENV: allow}).host == "0.0.0.0"


def test_an_allowed_non_loopback_bind_says_what_it_costs(caplog: pytest.LogCaptureFixture) -> None:
    """
    The opt-in is granted, so the only remaining defence is the operator understanding it.

    Args:
        caplog: Fixture used to read the warning the widened bind must emit.

    """
    with caplog.at_level(logging.WARNING):
        transport_from_env(ALLOWED_REMOTE_ENV | {HTTP_HOST_ENV: "0.0.0.0"})

    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1, f"expected exactly one warning, got {[record.message for record in warnings]}"
    assert "authentication" in warnings[0].message
    assert "Blender" in warnings[0].message and "files" in warnings[0].message


def test_a_loopback_bind_is_not_warned_about(caplog: pytest.LogCaptureFixture) -> None:
    """
    A warning on the supported path would teach operators to skip reading warnings.

    Args:
        caplog: Fixture used to assert the loopback path is silent.

    """
    with caplog.at_level(logging.WARNING):
        transport_from_env(ALLOWED_REMOTE_ENV)

    assert caplog.records == []


def _run_python(*arguments: str) -> subprocess.CompletedProcess[str]:
    """
    Run this interpreter with the given arguments, capturing what it reports.

    Args:
        arguments: Interpreter arguments, e.g. `-O` followed by `-c` and a program.

    Returns:
        subprocess.CompletedProcess[str]: The finished process, output decoded.

    """
    return subprocess.run([sys.executable, *arguments], capture_output=True, text=True, check=False)


def test_an_http_config_without_an_address_cannot_be_built_even_under_o() -> None:
    """
    The host/port invariant has to be structural, because `python -O` deletes `assert`.

    The invariant used to be an `assert` in `main()` guarding an
    `Optional`-shaped config, so under `-O` an HTTP config with no address was
    constructible and `uvicorn.Config(host=None)` bound every interface on both
    address families. Splitting the config by transport removes the state
    instead of checking for it: `HttpConfig` cannot exist without an address, and
    the type checker narrows on the transport literal with no `assert` to strip.
    """
    result = _run_python("-O", "-c", "from blender_mcp.server.cli import HttpConfig; HttpConfig()")

    assert result.returncode != 0, "an HTTP config with no bind address must not be constructible"
    assert "TypeError" in result.stderr, result.stderr or result.stdout


def test_the_o_flag_really_is_in_effect_for_that_check() -> None:
    """A subprocess that quietly ignored `-O` would make the test above prove nothing."""
    result = _run_python("-O", "-c", "print(__debug__)")

    assert result.stdout.strip() == "False", result.stderr


@pytest.fixture
def run_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """
    Capture mcp.run() instead of serving, and restore any settings main() changes.

    `mcp` is a module-level singleton shared by the whole suite, so a test that
    let main() bind a socket would hang, and one that left `settings.host`
    rewritten would leak into every later test that reads it.

    Args:
        monkeypatch: Fixture used to restore the singleton after each test.

    Returns:
        list[dict[str, Any]]: The keyword arguments of each mcp.run() call.

    """
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(mcp, "run", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(mcp.settings, "host", mcp.settings.host)
    monkeypatch.setattr(mcp.settings, "port", mcp.settings.port)
    monkeypatch.setattr(cli.sys, "argv", ["blender-mcp"])
    for name in (TRANSPORT_ENV, HTTP_HOST_ENV, HTTP_PORT_ENV, HTTP_ALLOW_REMOTE_ENV):
        monkeypatch.delenv(name, raising=False)
    return calls


def test_main_serves_stdio_by_default(run_calls: list[dict[str, Any]]) -> None:
    """
    The no-configuration entrypoint behaves exactly as it did before HTTP existed.

    Args:
        run_calls: Fixture capturing the transport mcp.run() was asked to serve.

    """
    cli.main()
    assert run_calls == [{}]


def test_main_serves_http_on_the_configured_address(
    run_calls: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    main() applies the configured bind address before serving HTTP.

    Args:
        run_calls: Fixture capturing the transport mcp.run() was asked to serve.
        monkeypatch: Fixture used to set the transport variables.

    """
    monkeypatch.setenv(TRANSPORT_ENV, "http")
    monkeypatch.setenv(HTTP_HOST_ENV, "0.0.0.0")
    monkeypatch.setenv(HTTP_PORT_ENV, "9000")
    monkeypatch.setenv(HTTP_ALLOW_REMOTE_ENV, "1")

    cli.main()

    assert run_calls == [{"transport": "streamable-http"}]
    assert (mcp.settings.host, mcp.settings.port) == ("0.0.0.0", 9000)


@pytest.mark.usefixtures("run_calls")
def test_binding_all_interfaces_keeps_dns_rebinding_protection(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    FastMCP only enables Host-header validation when built for a loopback host.

    The container binds 0.0.0.0 so Docker can forward to it; the protection
    decided at construction must survive that, or a malicious web page could
    drive Blender through the published port via DNS rebinding.

    Args:
        monkeypatch: Fixture used to set the transport variables.

    """
    monkeypatch.setenv(TRANSPORT_ENV, "http")
    monkeypatch.setenv(HTTP_HOST_ENV, "0.0.0.0")
    monkeypatch.setenv(HTTP_ALLOW_REMOTE_ENV, "1")

    cli.main()

    security = mcp.settings.transport_security
    assert security is not None and security.enable_dns_rebinding_protection
    assert "127.0.0.1:*" in security.allowed_hosts


def test_main_reports_a_bad_transport_without_serving(
    run_calls: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Invalid configuration exits with the variable named, and never starts serving.

    Args:
        run_calls: Fixture asserting mcp.run() was never reached.
        monkeypatch: Fixture used to set the transport variables.

    """
    monkeypatch.setenv(TRANSPORT_ENV, "carrier-pigeon")

    with pytest.raises(SystemExit, match=TRANSPORT_ENV):
        cli.main()

    assert run_calls == []


def test_main_refuses_a_wildcard_bind_without_serving(
    run_calls: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    The refusal has to reach the entrypoint, not just the parser.

    Args:
        run_calls: Fixture asserting mcp.run() was never reached.
        monkeypatch: Fixture used to set the transport variables.

    """
    monkeypatch.setenv(TRANSPORT_ENV, "http")
    monkeypatch.setenv(HTTP_HOST_ENV, "0.0.0.0")

    with pytest.raises(SystemExit, match=HTTP_ALLOW_REMOTE_ENV):
        cli.main()

    assert run_calls == []


INITIALIZE_REQUEST = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test-cli-transport", "version": "0"},
    },
}


REMOTE_HOST_HEADER = "192.0.2.10:8000"
LOOPBACK_HOST_HEADER = "127.0.0.1:8000"


@pytest.fixture(scope="module")
def status_by_host_header() -> dict[str, int]:
    """
    Post one `initialize` per `Host` header to the real ASGI app, and report the statuses.

    Driven in-process through `httpx.ASGITransport`, so the whole request path
    runs - FastMCP's transport-security middleware included - without binding a
    socket or leaving a server to hang. Every header goes through a single app
    and a single lifespan because `StreamableHTTPSessionManager.run()` may only
    be entered once per instance, and `mcp` is a module-level singleton.

    Returns:
        dict[str, int]: The status answered for each `Host` header sent.

    """

    async def statuses() -> dict[str, int]:
        app = mcp.streamable_http_app()
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://asgi") as client:
                return {
                    header: (
                        await client.post(
                            mcp.settings.streamable_http_path,
                            json=INITIALIZE_REQUEST,
                            headers={
                                "Host": header,
                                "Content-Type": "application/json",
                                "Accept": "application/json, text/event-stream",
                            },
                        )
                    ).status_code
                    for header in (REMOTE_HOST_HEADER, LOOPBACK_HOST_HEADER)
                }

    return anyio.run(statuses)


def test_a_remote_host_header_is_refused_by_the_running_app(status_by_host_header: dict[str, int]) -> None:
    """
    421 for a remote `Host` is the intended shape of a loopback-only server.

    Asserted against the app actually built from `app.py` rather than against
    `settings.transport_security`, because the settings object only says the
    check was configured, not that a request ever reaches it.

    Args:
        status_by_host_header: Fixture holding the status per `Host` header.

    """
    assert status_by_host_header[REMOTE_HOST_HEADER] == httpx.codes.MISDIRECTED_REQUEST


def test_a_forged_loopback_host_header_is_served_so_it_is_no_access_control(
    status_by_host_header: dict[str, int],
) -> None:
    """
    `Host` is chosen by the client, so the same check that answers 421 lets a forgery in.

    This is the honest half of the DNS-rebinding story and the reason the bind
    address carries the whole weight: the middleware stops a browser being used
    as a confused deputy, and stops nothing else. A test that only asserted the
    421 would read as proof that a widened bind is defended.

    Args:
        status_by_host_header: Fixture holding the status per `Host` header.

    """
    assert status_by_host_header[LOOPBACK_HOST_HEADER] == httpx.codes.OK
