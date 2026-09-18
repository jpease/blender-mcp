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

    The populated control catches a payload that hardcodes the empty list, which the first
    assertion alone would miss.
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

    Agents learn the payload from the docstring, so an unmentioned key is invisible to them.
    """
    _install_handshake(monkeypatch, _handshake())

    payload = asyncio.run(core.get_addon_status(ctx=None))["data"]  # pyright: ignore[reportArgumentType]

    documented = core.get_addon_status.__doc__ or ""
    undocumented = sorted(key for key in payload if f'"{key}"' not in documented)
    assert not undocumented, f"payload keys missing from the docstring: {undocumented}"


# Any non-zero epoch.
_EPOCH = 7


def test_get_addon_status_reports_the_session_epoch_and_the_open_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The session epoch and open file reach the tool's payload.

    The live rig tests only the addon socket, so this covers the MCP-side wrapper.
    """
    _install_handshake(monkeypatch, _handshake(session_epoch=_EPOCH, current_filepath="/shots/sq010.blend"))

    payload = asyncio.run(core.get_addon_status(ctx=None))["data"]  # pyright: ignore[reportArgumentType]

    assert payload["session_epoch"] == _EPOCH
    assert payload["current_filepath"] == "/shots/sq010.blend"


def test_get_addon_status_reports_the_session_id_the_epoch_is_only_comparable_within(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The epoch is only meaningful next to the id, so the payload carries both.

    The epoch restarts at 0 with the addon, so after a restart and one swap it can repeat.
    """
    _install_handshake(monkeypatch, _handshake(session_epoch=_EPOCH, session_id="c0ffee"))

    payload = asyncio.run(core.get_addon_status(ctx=None))["data"]  # pyright: ignore[reportArgumentType]

    assert payload["session_id"] == "c0ffee"
    assert payload["session_epoch"] == _EPOCH


def test_get_addon_status_reports_no_epoch_for_an_addon_that_does_not_send_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    An addon that predates the field must not break the payload.

    The populated control catches a payload that hardcodes None.
    """
    _install_handshake(monkeypatch, _handshake())

    payload = asyncio.run(core.get_addon_status(ctx=None))["data"]  # pyright: ignore[reportArgumentType]

    assert payload["session_epoch"] is None
    assert payload["current_filepath"] is None

    _install_handshake(monkeypatch, _handshake(session_epoch=0, current_filepath="/shots/sq010.blend"))

    payload = asyncio.run(core.get_addon_status(ctx=None))["data"]  # pyright: ignore[reportArgumentType]

    assert payload["session_epoch"] == 0, "the None above came from the payload hardcoding one, not the handshake"
    assert payload["current_filepath"] == "/shots/sq010.blend"


def test_get_addon_status_reports_an_indeterminate_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    An indeterminate session reaches the tool's payload.

    While it is set the addon refuses most commands, and without the flag an agent cannot tell
    that from a broken addon.
    """
    _install_handshake(monkeypatch, _handshake(session_indeterminate=True))

    payload = asyncio.run(core.get_addon_status(ctx=None))["data"]  # pyright: ignore[reportArgumentType]

    assert payload["session_indeterminate"] is True


def test_get_addon_status_reports_a_healthy_session_as_determinate(monkeypatch: pytest.MonkeyPatch) -> None:
    """A healthy session reports the flag as False, catching a hardcoded True."""
    _install_handshake(monkeypatch, _handshake())

    payload = asyncio.run(core.get_addon_status(ctx=None))["data"]  # pyright: ignore[reportArgumentType]

    assert payload["session_indeterminate"] is False


def test_get_addon_status_reports_the_file_path_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Permissive-when-unset is only acceptable if the agent can read which mode it is in."""
    _install_handshake(monkeypatch, _handshake(file_roots=["/canon"], file_roots_enforced=True))

    payload = asyncio.run(core.get_addon_status(ctx=None))["data"]  # pyright: ignore[reportArgumentType]

    assert payload["file_roots"] == ["/canon"]
    assert payload["file_roots_enforced"] is True


# A handshake from an addon with both optional integrations that gate command handlers on.
_CAPABILITIES = ["get_addon_info", "import_polyhaven_asset", "nd_boolean", "open_shot", "ping"]


def test_get_addon_status_summarizes_the_capabilities_instead_of_listing_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """291 command names on every handshake is the list the server gates on, not something an agent acts on."""
    _install_handshake(monkeypatch, _handshake(capabilities=_CAPABILITIES))

    payload = asyncio.run(core.get_addon_status(ctx=None))["data"]  # pyright: ignore[reportArgumentType]

    assert payload["capability_count"] == len(_CAPABILITIES)
    assert payload["integrations_available"] == {"polyhaven": True, "sketchfab": False, "nd": True}
    assert "capabilities" not in payload


def test_get_addon_status_lists_the_command_names_only_on_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """The full list stays reachable: an agent debugging a refused command needs the exact names."""
    _install_handshake(monkeypatch, _handshake(capabilities=_CAPABILITIES))

    payload = asyncio.run(core.get_addon_status(ctx=None, detail=True))["data"]  # pyright: ignore[reportArgumentType]

    assert payload["capabilities"] == _CAPABILITIES
    assert payload["capability_count"] == len(_CAPABILITIES)


def test_get_addon_status_reports_an_addon_with_no_optional_integrations(monkeypatch: pytest.MonkeyPatch) -> None:
    """All-False catches a hardcoded True, and an empty capability list is a real handshake state."""
    _install_handshake(monkeypatch, _handshake(capabilities=[]))

    payload = asyncio.run(core.get_addon_status(ctx=None))["data"]  # pyright: ignore[reportArgumentType]

    assert payload["capability_count"] == 0
    assert payload["integrations_available"] == {"polyhaven": False, "sketchfab": False, "nd": False}
