"""Coverage for the core/meta tools' handshake reporting."""

import asyncio

import pytest

from mcp.server.fastmcp.exceptions import ToolError

from blender_mcp.addon_manager import EXPECTED_ADDON_PROTOCOL_VERSION, AddonHandshake
from blender_mcp.server.connection import BlenderTransportError
from blender_mcp.server.mount_map import CORE_BUNDLE, bundle_tool_names, known_tool_names
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


def test_get_addon_status_reports_render_devices_with_the_machine_list_only_on_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What Cycles renders on is a Blender-machine fact; a GPU request without one renders on the CPU."""
    devices = {
        "compute_device_type": "NONE",
        "enabled_devices": [],
        "available_devices": [{"name": "RTX 4090", "type": "OPTIX", "use": True}],
    }
    _install_handshake(monkeypatch, _handshake(render_devices=devices))

    summary = asyncio.run(core.get_addon_status(ctx=None))["data"]  # pyright: ignore[reportArgumentType]
    detailed = asyncio.run(
        core.get_addon_status(ctx=None, detail=True)  # pyright: ignore[reportArgumentType]
    )["data"]

    assert summary["render_devices"] == {"compute_device_type": "NONE", "enabled_devices": []}
    assert detailed["render_devices"] == devices

    _install_handshake(monkeypatch, _handshake())
    older = asyncio.run(core.get_addon_status(ctx=None))["data"]  # pyright: ignore[reportArgumentType]

    assert older["render_devices"] is None


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
    """Every command name on every handshake is the list the server gates on, not something an agent acts on."""
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


def test_get_addon_status_reports_a_dead_socket_as_a_transport_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    A handshake that never completed raises instead of being reported as a version verdict.

    The live incident: the first call after connecting hit a socket Blender had already
    closed and came back `up_to_date: false`, `capability_count: 0`,
    `protocol_version: null` - every field an agent reads saying "the add-on is missing or
    outdated, reinstall it" when nothing whatsoever had been learned about it.
    """
    monkeypatch.setattr(core, "get_blender_connection", object)

    def _never_answered(_blender: object) -> AddonHandshake:
        raise BlenderTransportError("Connection closed before receiving any data")

    monkeypatch.setattr(core, "force_addon_handshake", _never_answered)

    with pytest.raises(ToolError) as failure:
        asyncio.run(core.get_addon_status(ctx=None))  # pyright: ignore[reportArgumentType]

    message = str(failure.value)
    assert "transport failure" in message, f"the failure must not read as a version verdict: {message}"
    assert "do not reinstall" in message
    assert "Connection closed before receiving any data" in message, "the underlying fault is not named"


# The rehearsal's tool: registered, dispatch-wired and tested, and mounted only by `camera-rigs`.
_UNMOUNTED_TOOL = "create_dolly_camera_rig"


def test_tool_lookup_separates_an_unmounted_tool_from_an_unknown_one() -> None:
    """
    The four situations behind "I cannot call this tool" need four different responses.

    A client reports all of them as one unknown-tool error, and an agent that reads "not
    mounted" as "not implemented" abandons work the server can do - which is what a rehearsal
    did with `create_dolly_camera_rig`, after confirming its absence from the mounted surface.
    """
    mounted = frozenset({"get_addon_status"})

    unmounted = core._tool_lookup(_UNMOUNTED_TOOL, (), mounted)
    assert (unmounted["mounted"], unmounted["in_this_build"]) == (False, True)
    assert unmounted["bundles"] == ["camera-rigs"]
    assert "BLENDER_MCP_TOOLSETS=camera-rigs" in str(unmounted["verdict"])

    unknown = core._tool_lookup("create_teapot", (), mounted)
    assert (unknown["mounted"], unknown["in_this_build"], unknown["addon_command"]) == (False, False, False)

    stale_server = core._tool_lookup("create_teapot", ("create_teapot",), mounted)
    assert stale_server["in_this_build"] is False and stale_server["addon_command"] is True
    assert "Upgrade the server" in str(stale_server["verdict"])

    assert core._tool_lookup("get_addon_status", (), mounted)["mounted"] is True


def test_tool_lookup_appends_the_missing_bundle_to_the_selection_already_in_force(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The suggested value must replace the whole variable, keeping what the client already asked for.

    Suggesting the bare bundle would silently unmount the mode the session is running, so the
    remedy would cost the agent every other tool it was using.
    """
    monkeypatch.setenv("BLENDER_MCP_TOOLSETS", "shot")

    verdict = str(core._tool_lookup(_UNMOUNTED_TOOL, (), frozenset())["verdict"])

    assert "BLENDER_MCP_TOOLSETS=shot,camera-rigs" in verdict


def test_toolset_payload_counts_what_a_selection_left_out() -> None:
    """
    A bundle whose tools are absent must be named with a count; one fully present must not.

    Measured against an explicit mounted set rather than this test process's own registrations,
    which any other test importing a tool module would change.
    """
    only_core = frozenset(bundle_tool_names()[CORE_BUNDLE])

    payload = core._toolset_payload(only_core)

    assert payload["mounted_bundles"] == [CORE_BUNDLE], "a bundle with no tools mounted read as mounted"
    assert payload["mounted_tool_count"] == len(only_core)
    expected_absent = {
        bundle: len(names - only_core)
        for bundle, names in bundle_tool_names().items()
        if bundle != CORE_BUNDLE and names - only_core
    }
    assert payload["unmounted_bundles"] == expected_absent
    assert expected_absent["camera-rigs"] == len(bundle_tool_names()["camera-rigs"])
    assert payload["unmounted_tool_count"] == len(known_tool_names() - only_core)

    everything = known_tool_names()
    assert core._toolset_payload(everything)["unmounted_bundles"] == {}, "nothing is missing when all is mounted"


def test_get_addon_status_reports_the_mount_state_without_being_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    The mount state must reach the client on the plain call, since that is the setup check.

    `capability_count` next to a short tool list already looked like a registration fault; this
    is the field that explains the gap instead of leaving it to be misread. The lookup stays
    opt-in, because naming one tool is a question only the caller has.
    """
    _install_handshake(monkeypatch, _handshake())

    payload = asyncio.run(core.get_addon_status(ctx=None))["data"]  # pyright: ignore[reportArgumentType]

    toolsets = payload["toolsets"]
    assert toolsets["mounted_bundles"][0] == CORE_BUNDLE
    assert toolsets["mounted_tool_count"] == len(core._mounted_tool_names())
    assert toolsets["env_var"] == "BLENDER_MCP_TOOLSETS"
    assert "tool_lookup" not in payload, "the lookup must stay opt-in"


# Five stand-in tool names, written out of order so a page that hands back the registry's own
# import order instead of a sorted one cannot pass.
_REGISTERED = frozenset({"save_shot", "create_light", "get_addon_status", "open_shot", "aim_light"})


def _names_page(monkeypatch: pytest.MonkeyPatch, **paging: int) -> dict:
    """
    Ask for one page of mounted tool names over a fixed registry.

    Args:
        monkeypatch: Fixture used to replace the tool module's collaborators.
        **paging: `tool_limit`/`tool_offset` for this page.

    Returns:
        dict: The reply's "mounted_tools" page.

    """
    _install_handshake(monkeypatch, _handshake())
    monkeypatch.setattr(core, "_mounted_tool_names", lambda: _REGISTERED)
    payload = asyncio.run(
        core.get_addon_status(ctx=None, mounted_tools=True, **paging)  # pyright: ignore[reportArgumentType]
    )["data"]
    return payload["mounted_tools"]


def test_get_addon_status_keeps_the_mounted_tool_names_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Every name costs bytes in a reply that most callers make for the version verdict alone.

    The counts and the bundle summary are what the plain call is for; the list is a question only
    a caller chasing a specific absent tool has.
    """
    _install_handshake(monkeypatch, _handshake())

    payload = asyncio.run(core.get_addon_status(ctx=None))["data"]  # pyright: ignore[reportArgumentType]

    assert "mounted_tools" not in payload, "the enumeration must stay opt-in"


def test_get_addon_status_enumerates_exactly_the_tools_this_process_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Until the names shipped, a documented tool absent from a build was found only by calling it.

    Read off the live registry rather than a stand-in set: a list that disagreed with what the
    client can actually call would answer the question wrongly while looking right, and the count
    beside it must be the same number the toolset summary reports.
    """
    _install_handshake(monkeypatch, _handshake())

    payload = asyncio.run(
        core.get_addon_status(ctx=None, mounted_tools=True, tool_limit=300)  # pyright: ignore[reportArgumentType]
    )["data"]

    page = payload["mounted_tools"]
    mounted = core._mounted_tool_names()
    assert page["total"] == len(mounted) == payload["toolsets"]["mounted_tool_count"]
    assert page["items"] == sorted(mounted)[: page["returned_count"]]
    assert "get_addon_status" in page["items"], "the tool answering the question left itself out"


def test_get_addon_status_pages_the_tool_names_deterministically(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Following next_offset must walk the whole list once: same order, no repeats, nothing skipped.

    `truncated` and `next_offset` are what a caller loops on, so a page calling itself the last one
    early would hide exactly the tools this enumeration exists to reveal.
    """
    names = sorted(_REGISTERED)

    first = _names_page(monkeypatch, tool_limit=2)

    assert first["items"] == names[:2]
    assert (first["total"], first["offset"], first["limit"], first["returned_count"]) == (len(names), 0, 2, 2)
    assert first["truncated"] is True
    assert first["next_offset"] == 2

    second = _names_page(monkeypatch, tool_limit=2, tool_offset=first["next_offset"])

    assert second["items"] == names[2:4]
    assert second["offset"] == 2
    assert second["truncated"] is True
    assert second["next_offset"] == 4

    last = _names_page(monkeypatch, tool_limit=2, tool_offset=second["next_offset"])

    assert last["items"] == names[4:]
    assert last["returned_count"] == 1
    assert last["truncated"] is False, "a page ending on the total must not ask to be continued"
    assert last["next_offset"] is None
    assert first["items"] + second["items"] + last["items"] == names


def test_get_addon_status_reports_an_offset_past_the_last_name_as_the_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """An over-run must read as the end of the list, not wrap round to the names already seen."""
    page = _names_page(monkeypatch, tool_offset=len(_REGISTERED))

    assert page["items"] == []
    assert page["returned_count"] == 0
    assert page["truncated"] is False
    assert page["next_offset"] is None
    assert page["total"] == len(_REGISTERED), "the total must stay the list's, not the page's"
