"""
A session enables tool bundles for itself, through `manage_toolsets`, without restarting the server.

Each test runs in a fresh server process, as `test_bundles.py` does: enabling a bundle imports its
modules and registers its tools for the whole process, so doing it here would change what every
later test in this process sees. Inside that process the calls go through a real MCP client
session over the SDK's in-memory transport, so the tool list, the refusals and the list-changed
notification are what a client receives, not what a method returns.
"""

import json
import os
import subprocess
import sys
import textwrap

import pytest

from mcp.server.fastmcp import FastMCP

from blender_mcp.server.bundles import TOOLSETS_ENV_VAR
from blender_mcp.server.mount_map import CORE_BUNDLE, bundle_tool_names, module_tool_names
from blender_mcp.server.tools._documentation import finalize_tool_documentation

# Shared by every scenario: no Blender, a canned handshake, and helpers that read what a client
# was sent. `scenario()`'s body is the test's own source, indented under it.
_PRELUDE = '''
import asyncio, json
import mcp.types as mcp_types
from mcp.shared.memory import create_connected_server_and_client_session
from blender_mcp.addon_manager import AddonHandshake
from blender_mcp.server import app, connection, integrations, mcp
from blender_mcp.server.tools import core


def _no_blender(*_args):
    raise ConnectionError("no Blender in this test")


def handshake(*capabilities):
    return AddonHandshake(
        up_to_date=True, protocol_version=None, addon_version=None,
        capabilities=["get_addon_info", *capabilities], blender_version=None, source="native",
    )


app.get_blender_connection = _no_blender
app.check_addon_status_on_startup = _no_blender
integrations.get_blender_connection = object
core.get_blender_connection = object
core.force_addon_handshake = lambda _blender: connection._addon_handshake


class Client:
    """One client session and the tools/list_changed notifications it was sent."""

    def __init__(self):
        self.announced = 0

    async def handle(self, message):
        if isinstance(message, mcp_types.ServerNotification) and isinstance(
            message.root, mcp_types.ToolListChangedNotification
        ):
            self.announced += 1

    def connect(self):
        return create_connected_server_and_client_session(mcp, message_handler=self.handle)


async def names(session):
    return sorted(tool.name for tool in (await session.list_tools()).tools)


async def call(session, name, arguments):
    result = await session.call_tool(name, arguments)
    text = "".join(block.text for block in result.content if block.type == "text")
    data = None if result.isError else json.loads(text)["data"]
    return {"error": result.isError, "text": text, "data": data}


async def toolsets(session, action, *names):
    return await call(session, "manage_toolsets", {"action": action, "toolsets": list(names)})


async def scenario():
'''

_EPILOGUE = "\nprint(json.dumps(asyncio.run(scenario())))\n"

_RIGGING = "character-rigging"
# Implemented in the rigging bundle only, so it is unregistered until that bundle is enabled.
_RIGGING_TOOL = "create_armature"


def _run(body: str, toolsets: str | None = None) -> dict:
    """
    Run `body` as the coroutine `scenario()` in a fresh server process, and decode what it returns.

    Args:
        body: The coroutine's statements, at any indentation; it must return a JSON value.
        toolsets: The process's BLENDER_MCP_TOOLSETS value, or None to leave it unset.

    Returns:
        dict: The scenario's return value.

    """
    env = {key: value for key, value in os.environ.items() if key != TOOLSETS_ENV_VAR}
    if toolsets is not None:
        env[TOOLSETS_ENV_VAR] = toolsets
    script = _PRELUDE + textwrap.indent(textwrap.dedent(body).strip(), "    ") + _EPILOGUE
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _expected(*bundles: str) -> list[str]:
    """
    Name every tool core plus `bundles` lists, sorted as `names()` returns them.

    Returns:
        list[str]: The tool names.

    """
    by_bundle = bundle_tool_names()
    return sorted(by_bundle[CORE_BUNDLE].union(*(by_bundle[bundle] for bundle in bundles)))


@pytest.mark.parametrize("toolsets", [None, "camera-rigs"])
def test_a_fresh_session_lists_the_env_selection(toolsets: str | None) -> None:
    """Before any `manage_toolsets` call a session sees what the process was started with."""
    listed = _run(
        """
        async with Client().connect() as session:
            return await names(session)
        """,
        toolsets,
    )

    assert listed == _expected(*([toolsets] if toolsets else []))
    assert "manage_toolsets" in listed, "the tool that changes the list must be in every list"


def test_enabling_a_bundle_lists_its_tools_and_announces_the_change() -> None:
    """ENABLE shows the bundle at once, DISABLE takes it away again, and each is announced."""
    seen = _run(
        f"""
        client = Client()
        async with client.connect() as session:
            before = await names(session)
            enabled = await toolsets(session, "ENABLE", "{_RIGGING}")
            after_enable = await names(session)
            announced_on_enable = client.announced
            disabled = await toolsets(session, "DISABLE", "{_RIGGING}")
            return {{
                "before": before,
                "enabled": enabled,
                "after_enable": after_enable,
                "announced_on_enable": announced_on_enable,
                "disabled": disabled,
                "after_disable": await names(session),
                "announced": client.announced,
            }}
        """
    )

    assert seen["before"] == _expected()
    assert not seen["enabled"]["error"], seen["enabled"]["text"]
    assert seen["enabled"]["data"]["enabled_bundles"] == [CORE_BUNDLE, _RIGGING]
    assert seen["after_enable"] == _expected(_RIGGING)
    assert seen["announced_on_enable"] == 1, "a client that was not told keeps the list it had"
    assert not seen["disabled"]["error"], seen["disabled"]["text"]
    assert seen["after_disable"] == _expected()
    assert seen["announced"] == 2


def test_a_second_session_is_unaffected_and_refused_with_the_bundle_named() -> None:
    """Enabling registers tools process-wide; only the session that asked may list or call them."""
    seen = _run(
        f"""
        async with Client().connect() as first, Client().connect() as second:
            await toolsets(first, "ENABLE", "{_RIGGING}")
            return {{
                "first": await names(first),
                "second": await names(second),
                "refusal": await call(second, "{_RIGGING_TOOL}", {{"name": "Rig", "collection_name": "Rigs"}}),
            }}
        """
    )

    assert _RIGGING_TOOL in seen["first"]
    assert seen["second"] == _expected()
    assert seen["refusal"]["error"]
    assert _RIGGING in seen["refusal"]["text"]
    assert "manage_toolsets" in seen["refusal"]["text"]


def test_an_unregistered_tool_is_unknown_and_points_at_get_addon_status() -> None:
    """Before any session enables its bundle a tool is not registered, and the error says where to look."""
    refusal = _run(
        f"""
        async with Client().connect() as session:
            return await call(session, "{_RIGGING_TOOL}", {{}})
        """
    )

    assert refusal["error"]
    assert f"Unknown tool: {_RIGGING_TOOL}" in refusal["text"]
    assert f'get_addon_status(tool_name="{_RIGGING_TOOL}")' in refusal["text"]


def test_core_cannot_be_disabled_and_an_unknown_name_lists_the_valid_ones() -> None:
    """Disabling core would leave a session unable to turn anything back on."""
    seen = _run(
        """
        async with Client().connect() as session:
            return {
                "core": await toolsets(session, "DISABLE", "core"),
                "unknown": await toolsets(session, "ENABLE", "rigging"),
                "listed": await names(session),
            }
        """
    )

    assert seen["core"]["error"]
    assert "core cannot be disabled" in seen["core"]["text"]
    assert seen["unknown"]["error"]
    assert "rigging" in seen["unknown"]["text"]
    assert "Available modes: asset, shot." in seen["unknown"]["text"]
    assert _RIGGING in seen["unknown"]["text"]
    assert seen["listed"] == _expected(), "a refused call changed the list"


def test_withheld_integrations_stay_hidden_after_enable() -> None:
    """Enabling `assets` lists Sketchfab's tools but not Poly Haven's while its checkbox is off."""
    listed = _run(
        """
        connection._addon_handshake = handshake("import_sketchfab_model", "nd_boolean")
        async with Client().connect() as session:
            await toolsets(session, "ENABLE", "assets")
            return await names(session)
        """
    )

    assert module_tool_names("sketchfab") <= set(listed)
    assert module_tool_names("polyhaven").isdisjoint(listed)


def test_get_addon_status_reports_per_session() -> None:
    """A tool lookup answers for the session asking, not for whatever the process registered."""
    seen = _run(
        f"""
        connection._addon_handshake = handshake()
        async with Client().connect() as first, Client().connect() as second:
            await toolsets(first, "ENABLE", "{_RIGGING}")
            first_status = await call(first, "get_addon_status", {{"tool_name": "{_RIGGING_TOOL}"}})
            second_status = await call(second, "get_addon_status", {{"tool_name": "{_RIGGING_TOOL}"}})
            return {{"first": first_status["data"], "second": second_status["data"]}}
        """
    )

    assert seen["first"]["tool_lookup"]["mounted"] is True
    assert _RIGGING in seen["first"]["toolsets"]["mounted_bundles"]
    assert seen["second"]["tool_lookup"]["mounted"] is False
    assert _RIGGING not in seen["second"]["toolsets"]["mounted_bundles"]
    assert f'manage_toolsets(action="ENABLE", toolsets=["{_RIGGING}"])' in seen["second"]["tool_lookup"]["verdict"]


# Every tool the scenario's session lists, as `tools/list` carries it, with the registry's size.
_DUMP_LISTED = """
    listed = (await session.list_tools()).tools
    tools = {tool.name: tool.model_dump(exclude_none=True) for tool in listed}
    return {"registered": len(mcp._tool_manager.list_tools()), "tools": tools}
"""


def test_enabling_twice_registers_once_and_advertises_what_a_started_process_does() -> None:
    """
    `lighting` and `lighting-construction` share a module, and the documentation pass is not reversible.

    Running it twice over a tool appended its effects tag again and lost its `Returns:` text, so
    a bundle enabled after another that shares its module, or enabled twice, must advertise
    exactly what a process started with `shot`, which selects both, advertises.
    """
    lazily = _run(
        """
        async with Client().connect() as session:
            for name in ("lighting", "lighting", "lighting-construction", "lighting-construction"):
                reply = await toolsets(session, "ENABLE", name)
                assert not reply["error"], reply["text"]
        """
        + textwrap.indent(_DUMP_LISTED, " " * 8)
    )
    started = _run("async with Client().connect() as session:" + textwrap.indent(_DUMP_LISTED, "    "), "shot")

    lighting = _expected("lighting", "lighting-construction")
    assert lazily["registered"] == len(lighting), "a bundle enabled twice registered its tools twice"
    assert sorted(lazily["tools"]) == lighting
    assert lazily["tools"] == {name: started["tools"][name] for name in lighting}


def test_list_reports_each_bundles_tool_count_and_catalog_bytes() -> None:
    """
    The catalog cost of a bundle is what lets an agent decide whether to enable it.

    Measured once without the bundle registered and once with it, and checked against a process
    started with it: all three must agree.
    """
    seen = _run(
        """
        async with Client().connect() as session:
            before = await toolsets(session, "LIST")
            await toolsets(session, "ENABLE", "camera")
            after = await toolsets(session, "LIST")
            return {"before": before["data"], "after": after["data"]}
        """
    )
    camera_only = _run(
        """
        from blender_mcp.server.catalog_metrics import payload_report
        from blender_mcp.server.mount_map import bundle_tool_names
        report = payload_report(await mcp.list_tools())
        return sum(report.per_tool[name] for name in bundle_tool_names()["camera"])
        """,
        "camera",
    )

    before, after = seen["before"]["bundles"]["camera"], seen["after"]["bundles"]["camera"]
    assert before == {"enabled": False, "tool_count": len(bundle_tool_names()["camera"]), "catalog_bytes": camera_only}
    assert after == {**before, "enabled": True}
    assert seen["before"]["enabled_bundles"] == [CORE_BUNDLE]
    assert seen["after"]["enabled_bundles"] == [CORE_BUNDLE, "camera"]


def test_the_documentation_pass_rewrites_a_tool_once_however_often_it_is_asked() -> None:
    """
    A second pass over a documented tool appended its effects tag again and lost its `Returns:` text.

    Run on an app of its own, so this process's catalog is untouched; the tool's name is unique to
    this test because the pass remembers names process-wide.
    """
    app = FastMCP("documentation-probe")

    @app.tool()
    def get_documentation_probe_value(count: int) -> dict:
        """
        Report a value.

        Args:
            count: How many.

        Returns:
            "value", the count echoed.

        """
        return {"value": count}

    name = get_documentation_probe_value.__name__
    finalize_tool_documentation(app, [name])
    once = app._tool_manager._tools[name].model_copy(deep=True)
    finalize_tool_documentation(app, [name])
    twice = app._tool_manager._tools[name]

    assert once.description.count("[read-only]") == 1
    assert 'Data contains "value", the count echoed.' in once.description
    assert (twice.description, twice.parameters) == (once.description, once.parameters)
