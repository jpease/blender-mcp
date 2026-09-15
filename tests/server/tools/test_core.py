"""Coverage for the core/meta tools' handshake reporting."""

import asyncio

import pytest

from blender_mcp.addon_manager import EXPECTED_ADDON_PROTOCOL_VERSION, AddonHandshake
from blender_mcp.server.tools import core


def _install_handshake(monkeypatch: pytest.MonkeyPatch, handshake: AddonHandshake) -> None:
    """
    Serve `get_addon_status` a canned handshake instead of a live Blender.

    Args:
        monkeypatch: Fixture used to replace the tool module's collaborators.
        handshake: Handshake result `force_addon_handshake` should return.

    """
    monkeypatch.setattr(core, "get_blender_connection", object)
    monkeypatch.setattr(core, "force_addon_handshake", lambda _blender: handshake)


def _handshake(**overrides: object) -> AddonHandshake:
    """
    Build a healthy native handshake, overriding any field a test cares about.

    Args:
        **overrides: Field values replacing the healthy defaults.

    Returns:
        AddonHandshake: The handshake to serve.

    """
    fields: dict[str, object] = {
        "up_to_date": True,
        "protocol_version": EXPECTED_ADDON_PROTOCOL_VERSION,
        "addon_version": [2, 0, 0],
        "capabilities": ["get_addon_info"],
        "blender_version": "5.2.1",
        "source": "native",
    }
    return AddonHandshake(**{**fields, **overrides})  # pyright: ignore[reportArgumentType]


def test_get_addon_status_reports_the_writable_output_roots(monkeypatch: pytest.MonkeyPatch) -> None:
    """The roots only help an agent if they survive the trip to the client."""
    _install_handshake(monkeypatch, _handshake(writable_output_roots=["/output", "/tmp"]))

    payload = asyncio.run(core.get_addon_status(ctx=None))["data"]  # pyright: ignore[reportArgumentType]

    assert payload["writable_output_roots"] == ["/output", "/tmp"]


def test_get_addon_status_reports_no_roots_for_an_addon_that_does_not_send_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    An addon one protocol behind sends no roots, and an empty list says so.

    The fixture is built at `EXPECTED_ADDON_PROTOCOL_VERSION - 1` with
    `up_to_date=False`, because that is the addon the test is named for; the
    healthy default would have been a *current* addon, which is a different
    claim from the one the name makes.

    The `== []` assertion alone cannot tell "the handshake's empty roots reached
    the payload" from "the payload hardcodes an empty list", since
    `AddonHandshake.writable_output_roots` is itself a
    `field(default_factory=list)`. The populated control below is what makes the
    first assertion mean something: both answers come out of the same payload
    line, so hardcoding it fails this test rather than leaving it quietly
    passing. `tests/test_addon_manager.py` pairs its two the same way, one layer
    down.
    """
    older = {"protocol_version": EXPECTED_ADDON_PROTOCOL_VERSION - 1, "up_to_date": False}
    _install_handshake(monkeypatch, _handshake(**older))

    payload = asyncio.run(core.get_addon_status(ctx=None))["data"]  # pyright: ignore[reportArgumentType]

    assert payload["writable_output_roots"] == []

    _install_handshake(monkeypatch, _handshake(**older, writable_output_roots=["/output"]))

    payload = asyncio.run(core.get_addon_status(ctx=None))["data"]  # pyright: ignore[reportArgumentType]

    assert payload["writable_output_roots"] == ["/output"], (
        "the empty list above came from the payload hardcoding one, not from the handshake"
    )


def test_get_addon_status_documents_every_key_it_returns(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Every key the payload carries is named in the docstring.

    The docstring enumerates the payload, so a key added without a mention is a
    documentation defect the next reader has no way to catch.
    """
    _install_handshake(monkeypatch, _handshake())

    payload = asyncio.run(core.get_addon_status(ctx=None))["data"]  # pyright: ignore[reportArgumentType]

    documented = core.get_addon_status.__doc__ or ""
    undocumented = sorted(key for key in payload if f'"{key}"' not in documented)
    assert not undocumented, f"payload keys missing from the docstring: {undocumented}"
