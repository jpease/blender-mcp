"""
A mounted integration tool follows whether the open .blend enables that integration.

Driven through a real MCP client session over the SDK's in-memory transport, so the tool list,
the refusal and the list-changed notification are what a client receives, not what a method
returns. The handshake is the only fake: it is what says whether Poly Haven is enabled.
"""

import asyncio

import mcp.types as mcp_types
import pytest

from mcp.shared.memory import create_connected_server_and_client_session

from blender_mcp.addon_manager import AddonHandshake
from blender_mcp.server import app, connection, integrations
from blender_mcp.server.app import mcp
from blender_mcp.server.mount_map import module_tool_names
from blender_mcp.server.tools import core, polyhaven

# A Poly Haven tool that reads the catalog; mounted by the import above.
_TOOL = polyhaven.list_polyhaven_assets.__name__
_POLYHAVEN_TOOLS = module_tool_names("polyhaven")


def _handshake(*, polyhaven_enabled: bool) -> AddonHandshake:
    capabilities = ["get_addon_info", "ping", "nd_boolean"]
    if polyhaven_enabled:
        capabilities.append("import_polyhaven_asset")
    return AddonHandshake(
        up_to_date=True,
        protocol_version=None,
        addon_version=None,
        capabilities=capabilities,
        blender_version=None,
        source="native",
    )


@pytest.fixture
def handshakes(monkeypatch: pytest.MonkeyPatch):
    """
    Stand in for the add-on's handshakes.

    Returns:
        A setter taking the cached handshake and the one a fresh handshake would return.

    """
    fresh: dict[str, AddonHandshake | None] = {"next": None}

    def force(_blender) -> AddonHandshake | None:
        monkeypatch.setattr(connection, "_addon_handshake", fresh["next"])
        return fresh["next"]

    def unreachable():
        raise ConnectionError("no Blender in this test")

    # The server's startup connects to Blender; here it must find none rather than a live one.
    monkeypatch.setattr(app, "get_blender_connection", unreachable)
    monkeypatch.setattr(app, "check_addon_status_on_startup", unreachable)
    monkeypatch.setattr(integrations, "get_blender_connection", object)
    monkeypatch.setattr(integrations, "force_addon_handshake", force)

    def install(cached: AddonHandshake | None, then: AddonHandshake | None) -> None:
        monkeypatch.setattr(connection, "_addon_handshake", cached)
        fresh["next"] = then

    return install


async def _listed_names() -> set[str]:
    async with create_connected_server_and_client_session(mcp) as client:
        return {tool.name for tool in (await client.list_tools()).tools}


@pytest.mark.parametrize(
    ("cached", "listed"),
    [
        pytest.param(None, True, id="no-handshake-yet"),
        pytest.param(_handshake(polyhaven_enabled=True), True, id="enabled"),
        pytest.param(_handshake(polyhaven_enabled=False), False, id="disabled"),
    ],
)
def test_the_tool_list_carries_an_integrations_tools_unless_the_handshake_shows_it_disabled(
    handshakes, cached, listed
) -> None:
    handshakes(cached, cached)
    names = asyncio.run(_listed_names())
    assert (names >= _POLYHAVEN_TOOLS) is listed
    assert _POLYHAVEN_TOOLS.isdisjoint(names) is not listed
    assert "get_addon_status" in names


def test_a_call_to_a_disabled_integration_is_refused_before_dispatch(handshakes, stub_blender_connection) -> None:
    handshakes(_handshake(polyhaven_enabled=False), _handshake(polyhaven_enabled=False))
    recorder = stub_blender_connection()

    async def call() -> mcp_types.CallToolResult:
        async with create_connected_server_and_client_session(mcp) as client:
            return await client.call_tool(_TOOL, {})

    result = asyncio.run(call())
    assert result.isError
    text = " ".join(block.text for block in result.content if isinstance(block, mcp_types.TextContent))
    assert "Use assets from Poly Haven" in text
    assert recorder.calls == []


def test_an_integration_enabled_since_the_cached_handshake_is_not_refused(handshakes, stub_blender_connection) -> None:
    handshakes(_handshake(polyhaven_enabled=False), _handshake(polyhaven_enabled=True))
    recorder = stub_blender_connection(
        {
            "total_count": 0,
            "returned_count": 0,
            "assets": {},
            "offset": 0,
            "limit": 20,
            "truncated": False,
            "next_offset": None,
        }
    )

    async def call() -> mcp_types.CallToolResult:
        async with create_connected_server_and_client_session(mcp) as client:
            return await client.call_tool(_TOOL, {})

    assert not asyncio.run(call()).isError
    assert [command for command, _params in recorder.calls] == ["list_polyhaven_assets"]


def test_a_session_that_listed_tools_is_told_when_a_handshake_withholds_some(
    handshakes, stub_blender_connection
) -> None:
    # Listed before Blender answered anything, then a call's handshake shows Poly Haven off.
    handshakes(None, _handshake(polyhaven_enabled=False))
    stub_blender_connection()
    received: list[object] = []

    # The SDK awaits a message handler, so it is async with nothing of its own to await.
    async def handle(message) -> None:  # ruff: ignore[unused-async]
        received.append(message)

    async def session() -> set[str]:
        async with create_connected_server_and_client_session(mcp, message_handler=handle) as client:
            assert {tool.name for tool in (await client.list_tools()).tools} >= _POLYHAVEN_TOOLS
            await client.call_tool(_TOOL, {})
            relisted = {tool.name for tool in (await client.list_tools()).tools}
            # Once re-listed, a call that changes nothing announces nothing further.
            await client.call_tool(_TOOL, {})
            return relisted

    relisted = asyncio.run(session())
    announced = [
        message
        for message in received
        if isinstance(message, mcp_types.ServerNotification)
        and isinstance(message.root, mcp_types.ToolListChangedNotification)
    ]
    assert len(announced) == 1
    assert _POLYHAVEN_TOOLS.isdisjoint(relisted)


def test_the_server_tells_clients_its_tool_list_can_change() -> None:
    """A client ignores tools/list_changed from a server that said its list never changes."""
    capabilities = mcp._mcp_server.create_initialization_options().capabilities
    assert capabilities.tools is not None
    assert capabilities.tools.listChanged is True


@pytest.mark.parametrize("polyhaven_enabled", [True, False], ids=["enabled", "disabled"])
def test_a_tool_lookup_names_the_checkbox_a_withheld_tool_is_waiting_on(
    monkeypatch: pytest.MonkeyPatch, polyhaven_enabled: bool
) -> None:
    """A bare "Mounted" would send an agent to call a tool that is refused until a box is ticked."""
    monkeypatch.setattr(core, "get_blender_connection", object)
    monkeypatch.setattr(core, "force_addon_handshake", lambda _blender: _handshake(polyhaven_enabled=polyhaven_enabled))

    lookup = asyncio.run(core.get_addon_status(ctx=None, tool_name=_TOOL))["data"]["tool_lookup"]  # pyright: ignore[reportArgumentType]

    assert lookup["mounted"] is True
    assert ("Use assets from Poly Haven" in lookup["verdict"]) is not polyhaven_enabled
