"""
Transport selection for the `blender-mcp` entrypoint.

stdio is the contract every existing MCP client config depends on, so it must
stay the default. Streamable HTTP is opt-in, for hosting the server next to a
Blender on another box (the Docker rig models this).
"""

from typing import Any

import pytest

from blender_mcp.server import cli
from blender_mcp.server.app import mcp
from blender_mcp.server.cli import TransportConfig, transport_from_env


def test_stdio_is_the_default() -> None:
    """Existing client configs launch the server with no transport settings."""
    assert transport_from_env({}) == TransportConfig(transport="stdio")


def test_http_defaults_to_loopback_on_port_8000() -> None:
    """Opting into HTTP alone must not expose the server beyond this machine."""
    assert transport_from_env({"BLENDERMCP_TRANSPORT": "http"}) == TransportConfig(
        transport="streamable-http", host="127.0.0.1", port=8000
    )


def test_http_host_and_port_are_configurable() -> None:
    """A container needs 0.0.0.0 so Docker's port forward can reach the server."""
    config = transport_from_env(
        {
            "BLENDERMCP_TRANSPORT": "http",
            "BLENDERMCP_HTTP_HOST": "0.0.0.0",
            "BLENDERMCP_HTTP_PORT": "9000",
        }
    )
    assert config == TransportConfig(transport="streamable-http", host="0.0.0.0", port=9000)


def test_transport_name_ignores_case_and_whitespace() -> None:
    """Env values often carry stray casing or padding from shell and compose files."""
    assert transport_from_env({"BLENDERMCP_TRANSPORT": " HTTP "}).transport == "streamable-http"


def test_http_settings_are_ignored_under_stdio() -> None:
    """A stray port must not break the default path it has no meaning for."""
    assert transport_from_env({"BLENDERMCP_HTTP_PORT": "not-a-port"}) == TransportConfig(transport="stdio")


def test_unknown_transport_is_rejected() -> None:
    """A typo must fail loudly rather than silently serving stdio nobody reads."""
    with pytest.raises(ValueError, match="BLENDERMCP_TRANSPORT"):
        transport_from_env({"BLENDERMCP_TRANSPORT": "sse"})


@pytest.mark.parametrize("port", ["abc", "0", "65536", "-1"])
def test_invalid_http_port_is_rejected(port: str) -> None:
    """A bad port is reported by its variable name before anything binds."""
    with pytest.raises(ValueError, match="BLENDERMCP_HTTP_PORT"):
        transport_from_env({"BLENDERMCP_TRANSPORT": "http", "BLENDERMCP_HTTP_PORT": port})


@pytest.fixture
def run_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """
    Capture mcp.run() instead of serving, and restore any settings main() changes.

    Returns:
        list[dict[str, Any]]: The keyword arguments of each mcp.run() call.

    """
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(mcp, "run", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(mcp.settings, "host", mcp.settings.host)
    monkeypatch.setattr(mcp.settings, "port", mcp.settings.port)
    monkeypatch.setattr(cli.sys, "argv", ["blender-mcp"])
    for name in ("BLENDERMCP_TRANSPORT", "BLENDERMCP_HTTP_HOST", "BLENDERMCP_HTTP_PORT"):
        monkeypatch.delenv(name, raising=False)
    return calls


def test_main_serves_stdio_by_default(run_calls: list[dict[str, Any]]) -> None:
    """The no-configuration entrypoint behaves exactly as it did before HTTP existed."""
    cli.main()
    assert run_calls == [{}]


def test_main_serves_http_on_the_configured_address(
    run_calls: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """main() applies the configured bind address before serving HTTP."""
    monkeypatch.setenv("BLENDERMCP_TRANSPORT", "http")
    monkeypatch.setenv("BLENDERMCP_HTTP_HOST", "0.0.0.0")
    monkeypatch.setenv("BLENDERMCP_HTTP_PORT", "9000")

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
    """
    monkeypatch.setenv("BLENDERMCP_TRANSPORT", "http")
    monkeypatch.setenv("BLENDERMCP_HTTP_HOST", "0.0.0.0")

    cli.main()

    security = mcp.settings.transport_security
    assert security is not None and security.enable_dns_rebinding_protection
    assert "127.0.0.1:*" in security.allowed_hosts


def test_main_reports_a_bad_transport_without_serving(
    run_calls: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invalid configuration exits with the variable named, and never starts serving."""
    monkeypatch.setenv("BLENDERMCP_TRANSPORT", "carrier-pigeon")

    with pytest.raises(SystemExit, match="BLENDERMCP_TRANSPORT"):
        cli.main()

    assert run_calls == []
