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


# An epoch the addon could plausibly be at; any non-zero value would do.
_EPOCH = 7


def test_get_addon_status_reports_the_session_epoch_and_the_open_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The tool-wrapper half of the epoch's end-to-end path, proven where it can be.

    The live rig speaks the addon socket only, so it can demonstrate
    `get_addon_info` carrying the epoch but never `get_addon_status` surfacing
    it - that layer runs inside the MCP process. This is that layer: the fields
    have to survive `AddonHandshake` and reach the payload the agent reads.
    """
    _install_handshake(monkeypatch, _handshake(session_epoch=_EPOCH, current_filepath="/shots/sq010.blend"))

    payload = asyncio.run(core.get_addon_status(ctx=None))["data"]  # pyright: ignore[reportArgumentType]

    assert payload["session_epoch"] == _EPOCH
    assert payload["current_filepath"] == "/shots/sq010.blend"


def test_get_addon_status_reports_the_session_id_the_epoch_is_only_comparable_within(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The epoch was surfaced without the id it is only meaningful next to.

    The docstring tells the agent to re-read capabilities when the epoch moves,
    and the addon's counter restarts at 0 with the process - so epoch 1 ->
    restart -> 0 -> one swap -> 1 reads as "nothing happened" to an agent holding
    only the number. That is the ABA case the *pair* exists to close, and the
    payload published exactly the half that cannot close it.
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

    The `is None` assertion alone cannot tell "the handshake's None reached the
    payload" from "the payload hardcodes None", because the dataclass field
    defaults to None too. The populated control below is what makes the first
    assertion mean something - the same pairing
    `test_get_addon_status_reports_no_roots_for_an_addon_that_does_not_send_them`
    uses one field over.
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
    The tool-wrapper half of the latch, proven the way this repo proves every wrapper.

    The rig speaks the addon socket only, so what it can show live is
    `get_addon_info` and `get_session_info` carrying the flag. This is the other
    half: the field has to survive `AddonHandshake` and reach the payload an
    agent reads, because while it is set the addon is refusing almost every
    command and the open .blend must not be saved over. A refusal an agent
    cannot explain is indistinguishable from a broken addon.
    """
    _install_handshake(monkeypatch, _handshake(session_indeterminate=True))

    payload = asyncio.run(core.get_addon_status(ctx=None))["data"]  # pyright: ignore[reportArgumentType]

    assert payload["session_indeterminate"] is True


def test_get_addon_status_reports_a_healthy_session_as_determinate(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    The negative direction, which is the one that catches a hardcoded True.

    A field that is always true is not a signal, and an agent that stops trusting
    it is back where it started.
    """
    _install_handshake(monkeypatch, _handshake())

    payload = asyncio.run(core.get_addon_status(ctx=None))["data"]  # pyright: ignore[reportArgumentType]

    assert payload["session_indeterminate"] is False


def test_get_addon_status_reports_the_file_path_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Permissive-when-unset is only acceptable if the agent can read which mode it is in."""
    _install_handshake(monkeypatch, _handshake(file_roots=["/canon"], file_roots_enforced=True))

    payload = asyncio.run(core.get_addon_status(ctx=None))["data"]  # pyright: ignore[reportArgumentType]

    assert payload["file_roots"] == ["/canon"]
    assert payload["file_roots_enforced"] is True
